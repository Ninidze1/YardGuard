"""Telegram two-way bot: one message with the picture per incident, buttons under it, and a few
commands (/status, /snapshot, /arm, /disarm, /incidents, /mute, /help).

Long polling only, so nothing listens on this PC. Only chats the user connected in the setup are
answered; everything else is ignored without a reply. The bot token is only ever in the URL path
of the Bot API, is never logged and is scrubbed from every error text.

Threads: send() runs on the hub's worker thread for this destination; the poller thread
("yg-telegram-<id>") reads updates and answers commands and buttons; the setup dialog runs a
PairingListener ("yg-telegram-pair") while the destination's own poller is paused. Remote
actions always go through hub.request(), which runs them on the GUI thread.
"""
from __future__ import annotations

import html
import http.client
import json
import logging
import os
import re
import secrets
import select
import socket
import ssl
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import media
from .base import Channel, ChannelError, IntegrationConf, scrub
from .model import Action, CheckResult, Event, SendContext, SendResult

log = logging.getLogger("yardguard")
logging.getLogger("urllib3").setLevel(logging.WARNING)  # urllib3 debug lines would contain /bot<token>/

API_BASE = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
POLL_TIMEOUT = 25              # seconds Telegram holds a getUpdates call open
OLD_UPDATE_S = 120             # messages older than this when YardGuard starts are skipped
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
PHOTO_MAX_BYTES = 9_500_000    # Telegram's photo limit is 10 MB
VIDEO_LIMIT = 50_000_000       # bots may upload files up to 50 MB
INLINE_RETRY_S = 5.0           # a 429 asking for at most this long is waited out inside the call
ACTION_WAIT_S = 10.0           # how long a button / command waits for the GUI thread
SNAPSHOT_WAIT_S = 15.0
PAIR_SECONDS = 180.0
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}   # same as engine.SEVERITY_RANK

COMMANDS = [
    ("status", "Is monitoring on, cameras, open incidents"),
    ("snapshot", "A picture from a camera right now"),
    ("incidents", "The last open incidents"),
    ("arm", "Turn monitoring on"),
    ("disarm", "Turn monitoring off"),
    ("mute", "Pause messages for a while"),
    ("help", "What I can do"),
]
HELP_TEXT = ("I send you YardGuard alerts with a picture. The buttons under an alert mark it as handled or "
             "as a false alarm.\n\n" + "\n".join(f"/{name} - {text}" for name, text in COMMANDS))
STATE_WORDS = {"watching": "Watching", "idle": "Idle", "starting": "Starting", "offline": "No video",
               "off": "Not watched"}
CONTROL_OFF = "Control from Telegram is turned off in YardGuard."
EXPIRED = "This button has expired. Please press it again."


# --- helpers ------------------------------------------------------------------------------------

def normalize_token(text: str) -> str:
    """The token as BotFather shows it, without spaces, quotes or a leading "bot"."""
    t = re.sub(r"\s+", "", str(text or "")).strip("\"'`<>")
    if t[:3].lower() == "bot" and t[3:4].isdigit():
        t = t[3:]
    return t


def new_pair_code() -> str:
    """Six random digits for "/start <code>"."""
    return f"{secrets.randbelow(1_000_000):06d}"


def deep_link(bot_username: str, code: str) -> str:
    """Link that opens the bot in Telegram and sends "/start <code>" when the user presses Start."""
    bot = str(bot_username or "").lstrip("@")
    return f"https://t.me/{bot}?start={code}" if code else f"https://t.me/{bot}"


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _objects_text(objects: dict) -> str:
    names = {"person": ("person", "people"), "car": ("car", "cars"), "truck": ("truck", "trucks"),
             "bus": ("bus", "buses"), "motorcycle": ("motorcycle", "motorcycles"), "bicycle": ("bicycle", "bicycles")}
    parts = []
    for label, n in sorted((objects or {}).items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0]))):
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n > 0:
            one, many = names.get(str(label), (str(label), str(label) + "s"))
            parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts)


def _hhmm(ts: float, now: float | None = None) -> str:
    """Clock time for a moment in the near future or past: "22:15", "tomorrow 07:00"."""
    now = time.time() if now is None else now
    d, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now).date()
    if d.date() == today:
        return d.strftime("%H:%M")
    if d.date() == today + timedelta(days=1):
        return "tomorrow " + d.strftime("%H:%M")
    if d.date() == today - timedelta(days=1):
        return "yesterday " + d.strftime("%H:%M")
    return d.strftime("%a %H:%M")


def parse_duration(text: str) -> float | None:
    """ "1h", "8h", "30m", "90 min", "2" (hours) -> seconds; None when it cannot be read."""
    m = re.fullmatch(r"\s*(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?\s*", str(text or "").lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "h"
    seconds = n * 60 if unit.startswith("m") else n * 3600
    return float(seconds) if 0 < seconds <= 7 * 86400 else None


def _btn(text: str, data: str) -> dict | None:
    """Callback button; None when the data would not fit Telegram's 64-byte limit."""
    return {"text": text, "callback_data": data} if len(data.encode("utf-8")) <= 64 else None


def _markup(rows: list[list[dict | None]]) -> dict:
    clean = [[b for b in row if b] for row in rows]
    return {"inline_keyboard": [row for row in clean if row]}


def _status_line(e: Event) -> str:
    """ "Handled by Alex at 21:04" for incidents that are no longer open (or were just reopened)."""
    status = getattr(e, "status", "") or ""
    who = re.sub(r"\s*\(Telegram\)$", "", str(getattr(e, "status_by", "") or "")).strip()
    extra = getattr(e, "extra", None) or {}
    at = ""
    if e.kind == "incident_status":
        at = " at " + _hhmm(float(extra.get("status_ts") or time.time()))
    by = f" by {who}" if who else ""
    if status == "handled":
        return f"Handled{by}{at}" if (by or at) else "Marked as handled"
    if status == "false_alarm":
        return f"Marked as a false alarm{by}{at}"
    if status == "open" and e.kind == "incident_status":
        return f"Reopened{by}{at}"
    return ""


def caption_html(e: Event, limit: int = CAPTION_LIMIT) -> str:
    """Telegram HTML caption: media.html_caption plus our own status line (so the same wording is
    used whether the status changed here, on the PC or on a phone)."""
    line = _status_line(e)
    base_event = replace(e, status="", status_by="") if getattr(e, "status", "") else e
    room = limit - (len(line) + 8 if line else 0)
    text = media.html_caption(base_event, limit=max(64, room))
    if line:
        text += "\n<i>" + html.escape(line) + "</i>"
    return text[:limit]


def _photo_kind(msg: dict) -> str:
    return "photo" if msg.get("photo") or msg.get("video") or msg.get("document") else "text"


# --- Bot API client -----------------------------------------------------------------------------

class TelegramError(ChannelError):
    """A Bot API failure with plain-words text (never containing the token)."""

    def __init__(self, message: str, *, code: int = 0, description: str = "", retry: bool = False,
                 permanent: bool = False, retry_after: float | None = None, field: str = "",
                 conflict: bool = False) -> None:
        super().__init__(message, retry=retry, permanent=permanent, retry_after=retry_after, field=field)
        # set again: the checks below must not depend on how ChannelError stores them
        self.message, self.retry, self.permanent = message, retry, permanent
        self.retry_after, self.field = retry_after, field
        self.code, self.description, self.conflict = code, description, conflict

    def __str__(self) -> str:
        return self.message


class ChatMigrated(TelegramError):
    """The group became a supergroup with a new chat id."""

    def __init__(self, new_id: int) -> None:
        super().__init__("This group was upgraded by Telegram and has a new id.", code=400)
        self.new_id = new_id


class _Aborted(Exception):
    """The long poll was cut on purpose (stop or pause)."""


class _Again(Exception):
    """The long poll ended without an answer for a harmless reason: just poll again."""


def _parse(status: int, body: bytes, token: str, retry_after_header: str | None = None):
    """Bot API answer -> result, or TelegramError with plain words."""
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("ok") and 200 <= status < 300:
        return data.get("result")
    desc = scrub(str(data.get("description") or f"HTTP {status}"), token)
    low = desc.lower()
    params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
    try:
        code = int(data.get("error_code") or status)
    except (TypeError, ValueError):
        code = status
    if params.get("migrate_to_chat_id"):
        raise ChatMigrated(int(params["migrate_to_chat_id"]))
    if code == 400 and "message is not modified" in low:
        return {"not_modified": True}
    if code in (401, 404):    # 404 "Not Found" = the token is malformed / unknown
        raise TelegramError("The bot token is no longer valid (was the bot deleted in BotFather?)",
                            code=code, description=desc, permanent=True, field="token")
    if code == 403:
        raise TelegramError("The bot was blocked or removed from the chat. Open Telegram and press Start again.",
                            code=code, description=desc, permanent=True)
    if code == 400 and "chat not found" in low:
        raise TelegramError("Telegram does not know this chat. Open the bot in Telegram and press Start again.",
                            code=code, description=desc, permanent=True)
    if code == 409:
        raise TelegramError("Another copy of YardGuard or another program is using this bot.",
                            code=code, description=desc, retry=True, conflict=True)
    if code == 429:
        after = params.get("retry_after") or retry_after_header or 5
        try:
            after = max(1.0, float(after))
        except (TypeError, ValueError):
            after = 5.0
        raise TelegramError(f"Telegram asked to slow down for {int(after)} s.", code=code, description=desc,
                            retry=True, retry_after=after)
    if code == 413 or "too big" in low or "too large" in low:
        raise TelegramError("The file is too large for Telegram.", code=code, description=desc)
    if code >= 500:
        raise TelegramError("Telegram had a temporary problem.", code=code, description=desc, retry=True)
    raise TelegramError(f"Telegram refused it: {desc}", code=code, description=desc)


class TelegramApi:
    """Thin Bot API client; base URL configurable; the token is only ever in the URL path and is
    scrubbed from every error. One requests.Session per thread for normal calls, and one abortable
    http.client connection for getUpdates long polls (abort() cuts it from another thread)."""

    def __init__(self, token: str, api_base: str = API_BASE, timeout: float = 20) -> None:
        self.token = normalize_token(token)
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.timeout = timeout
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()
        self._poll_conn: http.client.HTTPConnection | None = None
        self._closed = False

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = "YardGuard"
            self._local.session = s
            with self._lock:
                self._sessions.append(s)
        return s

    def call(self, method: str, params: dict | None = None, files: dict | None = None,
             timeout: float | None = None):
        """One Bot API call -> its "result". files: {field: (filename, bytes | path, mime)}."""
        t = float(timeout or self.timeout)
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        handles = []
        try:
            if files:
                data = {k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)) for k, v in clean.items()}
                up = {}
                for name, (fname, content, mime) in files.items():
                    if isinstance(content, (str, Path)):
                        fh = open(content, "rb")
                        handles.append(fh)
                        content = fh
                    up[name] = (fname, content, mime)
                r = self._session().post(self._url(method), data=data, files=up, timeout=(10, t))
            else:
                r = self._session().post(self._url(method), json=clean, timeout=(10, t))
        except requests.Timeout:
            raise TelegramError(f"Telegram did not answer in {int(t)} s.", retry=True) from None
        except requests.ConnectionError:
            raise TelegramError("Could not reach Telegram. Check the internet connection.", retry=True) from None
        except OSError as e:
            raise TelegramError(scrub(f"Could not read a file to send: {e.strerror or e}", self.token)) from None
        except requests.RequestException as e:
            raise TelegramError(scrub(f"Could not reach Telegram ({type(e).__name__}).", self.token),
                                retry=True) from None
        finally:
            for fh in handles:
                fh.close()
        return _parse(r.status_code, r.content, self.token, r.headers.get("Retry-After"))

    # -- long polling ------------------------------------------------------------------------------
    def _new_conn(self, timeout: float) -> http.client.HTTPConnection:
        parts = urlsplit(self.api_base)
        if parts.scheme == "https":
            return http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)

    def poll(self, offset: int, timeout: int, allowed: list[str] | None = None, should_stop=None) -> list[dict]:
        """getUpdates long poll. Raises _Aborted when abort() cut it or should_stop() is true."""
        stop = should_stop or (lambda: False)
        body = json.dumps({"offset": int(offset), "timeout": int(timeout),
                           "allowed_updates": allowed or ALLOWED_UPDATES}).encode("utf-8")
        wait = timeout + 10
        with self._lock:
            if self._closed or stop():
                raise _Aborted()
            conn = self._poll_conn
            if conn is None:
                conn = self._poll_conn = self._new_conn(10)
            conn._yg_cut = False

        def cut() -> bool:
            return conn._yg_cut or self._closed or stop()

        reused, answered = conn.sock is not None, False
        try:
            if conn.sock is None:
                conn.connect()
            if cut():
                raise _Aborted()
            conn.sock.settimeout(wait)
            path = (urlsplit(self.api_base).path.rstrip("/")) + f"/bot{self.token}/getUpdates"
            conn.request("POST", path, body, {"Content-Type": "application/json", "User-Agent": "YardGuard"})
            # Wait for the answer in short slices so abort() / stop take effect within 0.25 s. Only this
            # thread ever touches the socket (closing it from another thread is not reliable on Windows).
            deadline = time.monotonic() + wait
            while True:
                if cut():
                    raise _Aborted()
                pending = getattr(conn.sock, "pending", None)
                if pending is not None and pending() > 0:
                    break
                readable, _, _ = select.select([conn.sock], [], [], 0.25)
                if readable:
                    break
                if time.monotonic() > deadline:
                    raise socket.timeout()
            # Readable can also mean TLS housekeeping (session tickets) without the answer: read with a
            # short timeout, and on a timeout simply poll again on a new connection.
            answered = True
            conn.sock.settimeout(8)
            resp = conn.getresponse()
            data = resp.read()
            status, after = resp.status, resp.getheader("Retry-After")
            if resp.will_close:
                self._drop_conn(conn)
        except _Aborted:
            self._drop_conn(conn)
            raise
        except Exception as e:  # noqa: BLE001 - socket errors, ssl errors, a cut connection
            self._drop_conn(conn)
            if getattr(conn, "_yg_cut", False) or self._closed or stop():
                raise _Aborted() from None
            if answered and isinstance(e, (socket.timeout, TimeoutError)):
                raise _Again() from None      # poll again at once
            if reused and isinstance(e, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
                                         ConnectionAbortedError)):
                raise _Again() from None      # the kept-alive connection was closed by the server meanwhile
            if isinstance(e, (socket.timeout, TimeoutError)):
                raise TelegramError(f"Telegram did not answer in {wait} s.", retry=True) from None
            raise TelegramError("Could not reach Telegram. Check the internet connection.", retry=True) from None
        result = _parse(status, data, self.token, after)
        return result if isinstance(result, list) else []

    def _drop_conn(self, conn) -> None:
        with self._lock:
            if self._poll_conn is conn:
                self._poll_conn = None
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def abort(self) -> None:
        """Cut a running long poll (any thread): the polling thread notices within 0.25 s and drops
        the connection itself."""
        with self._lock:
            if self._poll_conn is not None:
                self._poll_conn._yg_cut = True

    def drop_poll(self) -> None:
        """Close the long-poll connection; only from the thread that polls."""
        conn = self._poll_conn
        if conn is not None:
            self._drop_conn(conn)

    def close(self) -> None:
        """No more calls: cuts a running long poll and closes the sessions."""
        self._closed = True
        self.abort()
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for s in sessions:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


class _RateLimiter:
    """At most 1 message per second per chat and 20 per minute per group (Telegram's limits),
    plus a per-chat pause after Telegram answered 429."""

    MIN_GAP = 1.0            # seconds between two messages to the same chat
    GROUP_PER_MIN = 20

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[int, float] = {}
        self._group: dict[int, deque] = {}
        self._blocked: dict[int, float] = {}

    def delay(self, chat: int, now: float) -> float:
        d = max(0.0, self._blocked.get(chat, 0.0) - now)
        d = max(d, self._last.get(chat, 0.0) + self.MIN_GAP - now)
        if chat < 0:
            q = self._group.setdefault(chat, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.GROUP_PER_MIN:
                d = max(d, q[0] + 60 - now)
        return d

    def wait(self, chat: int, stop: threading.Event, max_wait: float = 65.0) -> None:
        end = time.monotonic() + max_wait
        while True:
            with self._lock:
                now = time.time()
                d = self.delay(chat, now)
                if d <= 0 or time.monotonic() >= end or stop.is_set():
                    self._last[chat] = now
                    if chat < 0:
                        self._group.setdefault(chat, deque()).append(now)
                    return
            stop.wait(min(d, 0.5))

    def block(self, chat: int, seconds: float) -> None:
        with self._lock:
            self._blocked[chat] = max(self._blocked.get(chat, 0.0), time.time() + seconds)


# --- the channel --------------------------------------------------------------------------------

class TelegramChannel(Channel):
    KIND = "telegram"
    CAPS = {"pictures": True, "clip": True, "edit": True, "buttons": True, "two_way": True,
            "max_upload": 50_000_000, "updates": "edit"}
    SECRET_KEYS = ("token",)

    def __init__(self, conf: IntegrationConf, hub) -> None:
        super().__init__(conf, hub)
        self.conf, self.hub = conf, hub
        if not hasattr(self, "status"):
            self.status = "off"
        if not hasattr(self, "status_text"):
            self.status_text = ""
        self._api: TelegramApi | None = None
        self._api_lock = threading.Lock()
        self._stop = threading.Event()        # replaced by start()
        self._never = threading.Event()       # send() before start(): limiter waits normally
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._limiter = _RateLimiter()
        self._mem_lock = threading.Lock()
        self._partial: OrderedDict[str, dict] = OrderedDict()   # delivery id -> {chat: [messages]} (retries)
        self._threads: OrderedDict[str, dict] = OrderedDict()   # incident id -> ref of our last send
        self._alias: dict[int, int] = {}                        # migrated group ids old -> new
        self._started_at = 0.0
        self._sticky = False      # a problem only the user can fix (bot removed from a chat): kept until restart
        self._bot_username = str(conf.params.get("bot_username") or "")

    # -- config helpers ------------------------------------------------------------------------
    @property
    def params(self) -> dict:
        return self.conf.params

    def _token(self) -> str:
        return normalize_token(self.conf.secret("token"))

    def api(self) -> TelegramApi:
        with self._api_lock:
            if self._api is None:
                self._api = TelegramApi(self._token(), self.params.get("api_base") or API_BASE)
            return self._api

    def _scrub(self, text: str) -> str:
        tok = self._api.token if self._api else ""
        return scrub(str(text), *(t for t in (tok,) if t))

    def _chat_id(self, cid) -> int:
        cid = int(cid)
        return self._alias.get(cid, cid)

    def chats(self) -> list[dict]:
        """Enabled chats, with migrated ids already applied."""
        out = []
        for c in self.params.get("chats") or []:
            if isinstance(c, dict) and c.get("enabled", True) and c.get("id") is not None:
                try:
                    out.append({**c, "id": self._chat_id(c["id"])})
                except (TypeError, ValueError):
                    continue
        return out

    def _allowed(self, cid) -> bool:
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return False
        return any(c["id"] == cid for c in self.chats())

    def _chat_title(self, cid: int) -> str:
        for c in self.chats():
            if c["id"] == cid:
                return str(c.get("title") or "this chat")
        return "this chat"

    def _allow_control(self) -> bool:
        return bool(self.params.get("allow_control", True))

    def _set_status(self, status: str, text: str = "") -> None:
        """Change status / status_text and tell the hub (base Channel.set_status -> hub.notify_status)."""
        setter = getattr(super(), "set_status", None)
        if setter is not None:
            try:
                setter(status, text)
                return
            except Exception:  # noqa: BLE001 - hub gone during shutdown
                pass
        self.status, self.status_text = status, text

    def describe(self) -> str:
        bot = self.params.get("bot_username") or ""
        names = [str(c.get("title") or "chat") for c in self.chats()]
        who = f"@{bot}" if bot else "Telegram bot"
        return f"{who} to {', '.join(names)}" if names else f"{who} · no chat connected yet"

    # -- lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._paused.clear()
        self._sticky = False
        with self._api_lock:
            self._api = None
        if not self._token():
            self._set_status("problem", "The saved token cannot be read on this Windows account. Enter it again."
                             if self.conf.has_secret("token") else "The bot token is missing.")
            return
        self._set_status("connecting", "")
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, name=f"yg-telegram-{self.conf.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        api = self._api
        if api is not None:
            api.abort()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.5)
        self._thread = None
        if self.status != "problem":
            self._set_status("off", "")

    def pause_polling(self) -> None:
        """Let a PairingListener use the same token (Telegram allows one getUpdates at a time)."""
        self._paused.set()
        api = self._api
        if api is not None:
            api.abort()

    def resume_polling(self) -> None:
        self._paused.clear()

    # -- low-level sending -----------------------------------------------------------------------
    def _call(self, method: str, params: dict, files: dict | None = None, chat: int | None = None,
              timeout: float | None = None):
        """API call with the per-chat rate limit; a short 429 is waited out once."""
        stop = self._stop if self._thread is not None else self._never
        for attempt in range(2):
            if chat is not None:
                self._limiter.wait(chat, stop)
            try:
                return self.api().call(method, params, files=files, timeout=timeout)
            except TelegramError as e:
                if e.code == 429 and e.retry_after:
                    if chat is not None:
                        self._limiter.block(chat, e.retry_after)
                    if attempt == 0 and e.retry_after <= INLINE_RETRY_S and not stop.is_set():
                        stop.wait(e.retry_after)
                        continue
                raise
        raise TelegramError("Telegram asked to slow down.", retry=True)   # not reached

    def _send_text(self, chat: int, text: str, markup: dict | None = None, reply_to: int | None = None,
                   silent: bool = False, parse_html: bool = False) -> dict:
        p = {"chat_id": chat, "text": text[:TEXT_LIMIT], "reply_markup": markup,
             "disable_notification": silent or None, "link_preview_options": {"is_disabled": True}}
        if parse_html:
            p["parse_mode"] = "HTML"
        if reply_to:
            p["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        return self._call("sendMessage", p, chat=chat) or {}

    def _reply(self, chat: int, text: str, markup: dict | None = None, reply_to: int | None = None) -> dict | None:
        """Answer in a chat from the poller; failures are logged (scrubbed) and swallowed."""
        try:
            return self._send_text(chat, text, markup, reply_to)
        except ChannelError as e:
            log.warning("Telegram: could not answer in a chat: %s", self._scrub(e))
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram: could not answer in a chat: %s", self._scrub(type(e).__name__))
        return None

    def _pictures(self, e: Event, pictures: str) -> list[tuple[str, bytes]]:
        """Prepared JPEGs for this event: [] / [one] / [crop, snapshot] for "both"."""
        if pictures == "none":
            return []
        paths = []
        if pictures == "both":
            for p in (getattr(e, "crop_path", ""), getattr(e, "snapshot_path", "")):
                if p and p not in paths and os.path.isfile(p):
                    paths.append(p)
        if not paths:
            p = e.image_path(pictures if pictures != "both" else "crop")
            if p and os.path.isfile(p):
                paths.append(p)
        out = []
        for p in paths[:2]:
            data = media.prepare_jpeg(p, 1280, 80, PHOTO_MAX_BYTES)
            if data:
                out.append((Path(p).name or "picture.jpg", data))
        return out

    def keyboard(self, e: Event, web_base: str = "") -> dict | None:
        """Inline buttons under an alert message (callback data always <= 64 bytes)."""
        rows: list[list[dict | None]] = []
        status = getattr(e, "status", "") or "open"
        inc = getattr(e, "incident_id", "") or ""
        is_incident = e.kind.startswith("incident") and bool(inc)
        closed = is_incident and status in ("handled", "false_alarm")
        if is_incident:
            if closed:
                rows.append([_btn("Reopen", f"o:{inc}")])
            else:
                rows.append([_btn("Mark handled", f"h:{inc}"), _btn("False alarm", f"f:{inc}")])
        row2: list[dict | None] = []
        alert_id = getattr(e, "alert_id", "") or ""
        if alert_id and (getattr(e, "clip_path", "") or getattr(e, "clip_pending", False)):
            row2.append(_btn("Open clip", f"c:{alert_id}"))
        if getattr(e, "channel", None) is not None and not closed and e.kind not in ("summary", "health", "monitoring"):
            row2.append(_btn("Snapshot now", f"s:{int(e.channel)}"))
        rows.append(row2)
        path = getattr(e, "web_path", "") or ""
        if web_base and web_base.startswith("https://") and path:
            rows.append([{"text": "Open in YardGuard", "url": web_base.rstrip("/") + "/" + path.lstrip("/")}])
        markup = _markup(rows)
        return markup if markup["inline_keyboard"] else None

    # -- send ----------------------------------------------------------------------------------
    def send(self, event: Event, ctx: SendContext) -> SendResult:
        t0 = time.monotonic()
        try:
            res = self._send(event, ctx)
        except ChannelError as e:
            res = SendResult(ok=False, error=self._scrub(e), retry=bool(getattr(e, "retry", False)),
                             retry_after=getattr(e, "retry_after", None),
                             permanent=bool(getattr(e, "permanent", False)))
        except Exception as e:  # noqa: BLE001 - send never raises
            log.warning("Telegram: sending failed: %s", self._scrub(f"{type(e).__name__}: {e}"))
            res = SendResult(ok=False, error=self._scrub(f"Unexpected problem: {type(e).__name__}"), retry=True)
        res.ms = int((time.monotonic() - t0) * 1000)
        if res.ok and self.status in ("problem", "connecting") and self._thread is not None and not self._sticky:
            if not (self.status == "problem" and "Another copy" in self.status_text):
                self._set_status("ok", "")
        return res

    def _send(self, e: Event, ctx: SendContext) -> SendResult:
        if not self._token():
            return SendResult(ok=False, permanent=True,
                              error="The saved token cannot be read on this Windows account. Enter it again."
                              if self.conf.has_secret("token") else "The bot token is missing.")
        chats = self.chats()
        if not chats:
            return SendResult(ok=False, permanent=True,
                              error="No Telegram chat is connected. Open the setup and press Connect.")
        ref = ctx.thread_ref if isinstance(ctx.thread_ref, dict) else None
        if isinstance(ref, dict) and not ref.get("messages"):
            ref = None
        if e.kind == "incident_status":
            if not ref:
                return SendResult(ok=True, note="No earlier message to update")
            return self._edit_all(e, ref, ctx)
        if ctx.edit and ref:
            res = self._edit_all(e, ref, ctx)
            if res.ok and ctx.send_clip:
                self._clip_replies(e, ref, ctx, res)
            return res
        if (e.kind == "incident_update" and ref and ctx.send_clip and not getattr(e, "escalated", False)
                and getattr(e, "clip_path", "")):
            # only the finished clip is new: reply with it and refresh the caption
            res = self._edit_all(e, ref, ctx)
            if res.ok:
                self._clip_replies(e, ref, ctx, res)
            return res
        return self._send_new(e, ctx, ref)

    def _remember(self, incident_id: str, ref: dict) -> None:
        if not incident_id:
            return
        with self._mem_lock:
            self._threads[incident_id] = ref
            self._threads.move_to_end(incident_id)
            while len(self._threads) > 300:
                self._threads.popitem(last=False)

    def _send_new(self, e: Event, ctx: SendContext, prev: dict | None) -> SendResult:
        with self._mem_lock:
            done = dict(self._partial.get(ctx.delivery_id) or {})
        roots = {}
        for r in (prev or {}).get("root") or (prev or {}).get("messages") or []:
            roots.setdefault(self._chat_id(r.get("chat")), r.get("id"))
        pics = self._pictures(e, ctx.pictures) if self.CAPS["pictures"] else []
        notes, errors = [], []
        for chat in self.chats():
            cid = chat["id"]
            if cid in done:
                continue
            try:
                try:
                    done[cid] = self._send_to(cid, e, ctx, pics, roots.get(cid), notes)
                except ChatMigrated as m:
                    cid2 = self._migrated(cid, m.new_id)
                    done[cid] = done[cid2] = self._send_to(cid2, e, ctx, pics, None, notes)
            except ChannelError as err:
                errors.append((chat, err))
                if not getattr(err, "retry", False):
                    done[cid] = []          # a permanent problem of this chat: do not retry it
        retry = [x for x in errors if getattr(x[1], "retry", False)]
        sent = [m for cid, ms in done.items() for m in ms]
        if retry:
            with self._mem_lock:
                self._partial[ctx.delivery_id] = done
                while len(self._partial) > 100:
                    self._partial.popitem(last=False)
            err = retry[0][1]
            return SendResult(ok=False, error=self._scrub(err), retry=True,
                              retry_after=max((getattr(x[1], "retry_after", None) or 0) for x in retry) or None)
        with self._mem_lock:
            self._partial.pop(ctx.delivery_id, None)
        if not sent:
            err = errors[0][1] if errors else TelegramError("Nothing was sent.")
            return SendResult(ok=False, error=self._scrub(err), permanent=all(
                getattr(x[1], "permanent", False) for x in errors) if errors else False)
        for chat, err in errors:
            notes.append(f"Not sent to {chat.get('title') or 'a chat'}: {self._scrub(err)}")
        if errors and any(getattr(x[1], "permanent", False) for x in errors):
            self._set_status("problem", notes[-1])
        root = [{"chat": m["chat"], "id": m["id"]} for m in sent if m.get("first")]
        if prev and prev.get("root"):
            root = prev["root"]
        msgs = [{k: m[k] for k in ("chat", "id", "kind")} for m in sent]
        if prev:    # an escalation or "each" message: later edits update the old and the new messages
            seen = {(m["chat"], m["id"]) for m in msgs}
            msgs = ([m for m in prev.get("messages") or [] if (m.get("chat"), m.get("id")) not in seen] + msgs)[-30:]
        ref = {"messages": msgs, "root": root, "alert_id": getattr(e, "alert_id", ""),
               "severity": e.severity, "status": getattr(e, "status", "") or "open"}
        self._remember(getattr(e, "incident_id", ""), ref)
        return SendResult(ok=True, ref=ref, note="; ".join(dict.fromkeys(notes)))

    def _send_to(self, cid: int, e: Event, ctx: SendContext, pics: list, reply_to: int | None,
                 notes: list) -> list[dict]:
        """New message(s) for one chat -> [{"chat", "id", "kind", "first"}]."""
        markup = self.keyboard(e, ctx.web_base)
        common = {"chat_id": cid, "disable_notification": bool(ctx.silent) or None,
                  "protect_content": bool(self.params.get("protect_content")) or None}
        if reply_to:
            common["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        out: list[dict] = []
        if len(pics) >= 2:
            head = "<b>" + html.escape(media.headline(e)) + "</b>"
            group = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(pics))]
            group[0].update({"caption": head, "parse_mode": "HTML"})
            files = {f"p{i}": (name, data, "image/jpeg") for i, (name, data) in enumerate(pics)}
            try:
                res = self._call("sendMediaGroup", {**common, "media": group}, files=files, chat=cid, timeout=60)
                out += [{"chat": cid, "id": m["message_id"], "kind": "group"} for m in res or []]
            except TelegramError as err:
                if err.retry or err.permanent:
                    raise
                notes.append("The pictures could not be sent, sent text only")
            msg = self._send_text(cid, caption_html(e, TEXT_LIMIT), markup, out[0]["id"] if out else reply_to,
                                  silent=bool(out) or bool(ctx.silent), parse_html=True)
            out.append({"chat": cid, "id": msg.get("message_id"), "kind": "text"})
        else:
            sent = None
            if pics:
                name, data = pics[0]
                try:
                    sent = self._call("sendPhoto", {**common, "caption": caption_html(e), "parse_mode": "HTML",
                                                    "reply_markup": markup},
                                      files={"photo": (name, data, "image/jpeg")}, chat=cid, timeout=60)
                    out.append({"chat": cid, "id": sent.get("message_id"), "kind": "photo"})
                except TelegramError as err:
                    if err.retry or err.permanent or isinstance(err, ChatMigrated):
                        raise
                    notes.append("The picture could not be sent, sent text only")
            elif ctx.pictures != "none" and e.image_path(ctx.pictures):
                notes.append("The picture could not be read, sent text only")
            if sent is None:
                p = {**common, "text": caption_html(e, TEXT_LIMIT), "parse_mode": "HTML", "reply_markup": markup,
                     "link_preview_options": {"is_disabled": True}}
                msg = self._call("sendMessage", p, chat=cid) or {}
                out.append({"chat": cid, "id": msg.get("message_id"), "kind": "text"})
        if out:
            out[0]["first"] = True
        if ctx.send_clip and getattr(e, "clip_path", ""):
            try:
                self._send_clip(cid, e.clip_path, out[0]["id"] if out else reply_to, silent=True)
            except ChannelError as err:
                notes.append(f"The clip was not sent: {self._scrub(err)}")
        return out

    def _send_clip(self, cid: int, path: str, reply_to: int | None, silent: bool = False,
                   caption: str = "") -> dict | None:
        try:
            size = os.path.getsize(path)
        except OSError:
            raise TelegramError("The clip file is missing.") from None
        if size > VIDEO_LIMIT:
            mb = round(size / 1_000_000)
            return self._send_text(cid, f"The clip is too large for Telegram ({mb} MB). Open it on the PC.",
                                   reply_to=reply_to, silent=silent)
        p = {"chat_id": cid, "supports_streaming": True, "disable_notification": silent or None,
             "caption": caption or None,
             "protect_content": bool(self.params.get("protect_content")) or None}
        if reply_to:
            p["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        return self._call("sendVideo", p, files={"video": (Path(path).name or "clip.mp4", path, "video/mp4")},
                          chat=cid, timeout=180)

    def _clip_replies(self, e: Event, ref: dict, ctx: SendContext, res: SendResult) -> None:
        roots = {}
        for r in ref.get("root") or ref.get("messages") or []:
            roots.setdefault(self._chat_id(r.get("chat")), r.get("id"))
        for cid, mid in roots.items():
            if not self._allowed(cid):
                continue
            try:
                self._send_clip(cid, e.clip_path, mid, silent=True)
            except ChannelError as err:
                res.note = (res.note + "; " if res.note else "") + f"The clip was not sent: {self._scrub(err)}"

    def _edit_all(self, e: Event, ref: dict, ctx: SendContext | None) -> SendResult:
        """Update every message of this incident (caption + buttons), photo swapped on escalation."""
        web_base = ctx.web_base if ctx else self._web_base()
        markup = self.keyboard(e, web_base)
        swap = (e.kind == "incident_update" and getattr(e, "alert_id", "") and ref.get("alert_id")
                and e.alert_id != ref.get("alert_id")
                and SEVERITY_RANK.get(e.severity, 0) > SEVERITY_RANK.get(str(ref.get("severity")), 0))
        pics = self._pictures(e, ctx.pictures if ctx else "crop") if swap else []
        notes, retry_err, perm_err, edited = [], None, None, 0
        for m in ref.get("messages") or []:
            if m.get("kind") == "group":
                continue
            cid = self._chat_id(m.get("chat"))
            if not self._allowed(cid) or not m.get("id"):
                continue
            try:
                self._edit_one(cid, int(m["id"]), m.get("kind", "text"), e, markup, pics[0] if pics else None)
                edited += 1
            except ChatMigrated as mig:
                self._migrated(cid, mig.new_id)
                notes.append("The group has a new id; the old message was not updated")
            except ChannelError as err:
                desc = str(getattr(err, "description", "")).lower()
                if "message to edit not found" in desc or "can't be edited" in desc or "message_id_invalid" in desc:
                    notes.append("A message was deleted in Telegram and could not be updated")
                elif getattr(err, "retry", False):
                    retry_err = err
                elif getattr(err, "permanent", False):
                    perm_err = err
                else:
                    notes.append(self._scrub(err))
        if retry_err is not None:
            return SendResult(ok=False, error=self._scrub(retry_err), retry=True,
                              retry_after=getattr(retry_err, "retry_after", None))
        if perm_err is not None and not edited:
            return SendResult(ok=False, error=self._scrub(perm_err), permanent=True)
        new_ref = dict(ref)
        if e.kind != "incident_status":
            new_ref["severity"] = e.severity if SEVERITY_RANK.get(e.severity, 0) >= SEVERITY_RANK.get(
                str(ref.get("severity")), 0) else ref.get("severity")
            if swap and pics:
                new_ref["alert_id"] = e.alert_id
        new_ref["status"] = getattr(e, "status", "") or ref.get("status") or "open"
        self._remember(getattr(e, "incident_id", ""), new_ref)
        return SendResult(ok=True, ref=new_ref, note="; ".join(dict.fromkeys(notes)))

    def _edit_one(self, cid: int, mid: int, kind: str, e: Event, markup: dict | None,
                  photo: tuple[str, bytes] | None = None) -> None:
        empty = {"inline_keyboard": []}
        if kind == "photo" and photo is not None:
            item = {"type": "photo", "media": "attach://p0", "caption": caption_html(e), "parse_mode": "HTML"}
            self._call("editMessageMedia", {"chat_id": cid, "message_id": mid, "media": item,
                                            "reply_markup": markup or empty},
                       files={"p0": (photo[0], photo[1], "image/jpeg")}, chat=cid, timeout=60)
        elif kind == "photo":
            self._call("editMessageCaption", {"chat_id": cid, "message_id": mid, "caption": caption_html(e),
                                              "parse_mode": "HTML", "reply_markup": markup or empty}, chat=cid)
        else:
            self._call("editMessageText", {"chat_id": cid, "message_id": mid, "text": caption_html(e, TEXT_LIMIT),
                                           "parse_mode": "HTML", "reply_markup": markup or empty,
                                           "link_preview_options": {"is_disabled": True}}, chat=cid)

    def _migrated(self, old: int, new: int) -> int:
        """A group became a supergroup: use the new id now and let the hub save it."""
        old, new = int(old), int(new)
        self._alias[old] = new
        log.info("Telegram: a connected group was upgraded by Telegram; using its new id")
        try:
            self.hub.request(Action("update_chat", {"dest_id": self.conf.id, "old_id": old, "new_id": new},
                                    "Telegram"))
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram: could not save the new group id: %s", type(e).__name__)
        return new

    @classmethod
    def check(cls, params: dict, secrets: dict[str, str]) -> CheckResult:
        """Is the token well formed and accepted by Telegram (getMe)? Sends nothing."""
        token = normalize_token((secrets or {}).get("token", ""))
        if not token:
            return CheckResult(False, "Paste the bot token from BotFather.", field="token")
        if not TOKEN_RE.match(token):
            return CheckResult(False, "This does not look like a bot token. It looks like 123456789:AAH... - "
                                      "copy the whole token from BotFather.", field="token")
        api = TelegramApi(token, (params or {}).get("api_base") or API_BASE, timeout=15)
        try:
            me = api.call("getMe") or {}
        except TelegramError as e:
            if e.code in (401, 404):
                return CheckResult(False, "Telegram did not accept this token. Copy it again from BotFather "
                                          "(or send /token there to get a new one).", field="token")
            return CheckResult(False, scrub(str(e), token))
        except Exception as e:  # noqa: BLE001
            return CheckResult(False, scrub(f"Could not check the token ({type(e).__name__}).", token))
        finally:
            api.close()
        username = str(me.get("username") or "")
        if not me.get("is_bot", True) or not username:
            return CheckResult(False, "This token does not belong to a bot.", field="token")
        return CheckResult(True, f"Found @{username}",
                           data={"bot_username": username, "bot_name": str(me.get("first_name") or username),
                                 "bot_id": me.get("id")})

    # -- poller --------------------------------------------------------------------------------
    def _kv(self, key: str, value: str | None = None) -> str:
        log_ = getattr(self.hub, "log", None)
        try:
            if value is None:
                return log_.kv_get(key, "") if log_ is not None else ""
            if log_ is not None:
                log_.kv_set(key, value)
        except Exception:  # noqa: BLE001 - log being reopened
            pass
        return ""

    def _run(self) -> None:
        stop = self._stop
        key = f"tg_offset_{self.conf.id}"
        try:
            offset = int(self._kv(key) or 0)
        except ValueError:
            offset = 0
        ready, backlog, backoff = False, True, 5.0
        api = None
        try:
            while not stop.is_set():
                if self._paused.is_set():
                    stop.wait(0.3)
                    continue
                if api is not None and api is not self._api:
                    api.drop_poll()
                api = self.api()
                offset, ready, backlog, backoff = self._poll_once(api, stop, key, offset, ready, backlog, backoff)
        finally:
            if api is not None:
                api.drop_poll()

    def _poll_once(self, api: TelegramApi, stop: threading.Event, key: str, offset: int, ready: bool,
                   backlog: bool, backoff: float) -> tuple[int, bool, bool, float]:
        """One getUpdates round and the handling of what it returned."""
        try:
            if not ready:
                self._setup(api)
                ready = True
            updates = api.poll(offset, 0 if backlog else POLL_TIMEOUT, ALLOWED_UPDATES,
                               should_stop=lambda: stop.is_set() or self._paused.is_set())
        except (_Aborted, _Again):
            return offset, ready, backlog, backoff
        except TelegramError as e:
            if e.conflict:
                self._set_status("problem", str(e))
                stop.wait(30)
            elif e.permanent:
                self._set_status("problem", str(e))
                ready = False
                stop.wait(60)
            elif e.code == 429:
                stop.wait(e.retry_after or 5)
            else:
                self._set_status("connecting", "Cannot reach Telegram right now. Trying again.")
                stop.wait(backoff)
                backoff = min(60.0, backoff * 2)
            return offset, ready, backlog, backoff
        except Exception as e:  # noqa: BLE001 - never let the poller die
            log.warning("Telegram poller: %s", self._scrub(type(e).__name__))
            stop.wait(backoff)
            return offset, ready, backlog, min(60.0, backoff * 2)
        if self.status != "ok" and not self._sticky:
            self._set_status("ok", "")
        for u in updates:
            try:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
            except (TypeError, ValueError):
                continue
            if stop.is_set():
                break
            try:
                self.handle_update(u, backlog=backlog)
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram: could not handle an update: %s", self._scrub(f"{type(e).__name__}: {e}"))
        if updates:
            self._kv(key, str(offset))
        return offset, ready, False, 5.0

    def _setup(self, api: TelegramApi) -> None:
        """Once per start: no webhook (we long-poll), the command menu, and the bot's @name."""
        me = api.call("getMe") or {}
        if me.get("username"):
            self._bot_username = str(me["username"])
        api.call("deleteWebhook", {"drop_pending_updates": False})
        try:
            api.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        except TelegramError as e:
            if e.retry or e.permanent:
                raise
            log.info("Telegram: the command menu was not set: %s", self._scrub(e))

    # -- updates ---------------------------------------------------------------------------------
    def handle_update(self, u: dict, backlog: bool = False) -> None:
        """One update from getUpdates (public for tests)."""
        if "callback_query" in u:
            self._on_callback(u["callback_query"], backlog)
        elif "my_chat_member" in u:
            self._on_member(u["my_chat_member"])
        elif "message" in u:
            self._on_message(u["message"])

    def _on_member(self, upd: dict) -> None:
        chat = upd.get("chat") or {}
        cid = chat.get("id")
        status = (upd.get("new_chat_member") or {}).get("status", "")
        if not self._allowed(cid) or status not in ("kicked", "left"):
            return
        title = self._chat_title(int(cid))
        for c in self.params.get("chats") or []:
            if isinstance(c, dict) and self._chat_id(c.get("id", 0)) == int(cid):
                c["enabled"] = False
        self._sticky = True     # the chat stays off in memory; it is saved with the next config save
        self._set_status("problem", f"The bot was removed from {title} or blocked there. "
                                    "Open Telegram and press Start again, then connect the chat again.")

    def _on_message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if not self._allowed(cid):
            log.info("Telegram: ignored a message from an unknown chat")
            return
        cid = int(cid)
        if msg.get("migrate_to_chat_id"):
            self._migrated(cid, int(msg["migrate_to_chat_id"]))
            return
        try:
            if float(msg.get("date") or 0) < self._started_at - OLD_UPDATE_S:
                return    # sent long before YardGuard started: do not act on it now
        except (TypeError, ValueError):
            return
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            if chat.get("type") == "private" and text:
                self._reply(cid, "Send /help to see what I can do.")
            return
        head, _, rest = text.partition(" ")
        if "\n" in head:
            head, _, more = head.partition("\n")
            rest = (more + " " + rest).strip()
        name, _, at = head[1:].partition("@")
        if at and self._bot_username and at.lower() != self._bot_username.lower():
            return    # a command for another bot in the same group
        name, arg = name.lower(), rest.strip()
        user = str((msg.get("from") or {}).get("first_name") or "")
        handler = {"start": self._cmd_start, "help": self._cmd_help, "status": self._cmd_status,
                   "snapshot": self._cmd_snapshot, "arm": self._cmd_arm, "disarm": self._cmd_disarm,
                   "incidents": self._cmd_incidents, "mute": self._cmd_mute}.get(name)
        if handler is None:
            self._reply(cid, "I do not know that command. Send /help to see what I can do.")
            return
        handler(cid, arg, user, msg)

    # -- hub actions -----------------------------------------------------------------------------
    def _ask(self, kind: str, args: dict, user: str = "", wait: float = ACTION_WAIT_S) -> dict:
        """Run an action on the GUI thread through the hub and wait for its result."""
        try:
            fut: Future = self.hub.request(Action(kind, args, "Telegram", user=user))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": self._scrub(f"YardGuard could not do that ({type(e).__name__}).")}
        end = time.monotonic() + wait
        while not fut.done():
            if self._stop.is_set() or time.monotonic() > end:
                return {"ok": False, "message": "YardGuard did not answer in time. Try again."}
            self._stop.wait(0.05)
        try:
            res = fut.result(0)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": self._scrub(f"YardGuard could not do that ({type(e).__name__}).")}
        return res if isinstance(res, dict) else {"ok": bool(res)}

    def _web_base(self) -> str:
        try:
            return str(self.hub.web_base() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _store(self):
        ctx = getattr(self.hub, "ctx", None)
        return getattr(ctx, "alert_store", None) if ctx is not None else None

    def incident_event(self, incident_id: str, kind: str = "incident_new") -> Event | None:
        """An Event for an incident read from the alert store (for /incidents and button edits).
        Uses the hub's own builder when there is one, so every channel words it the same way."""
        store = self._store()
        if store is None:
            return None
        inc = store.get_incident(incident_id)
        if inc is None:
            return None
        build = getattr(self.hub, "_incident_event", None)
        if callable(build):
            try:
                ev = build(inc, kind)
                if ev.clip_path and not os.path.isfile(ev.clip_path):
                    ev.clip_path = ""
                return ev
            except Exception as e:  # noqa: BLE001 - fall back to our own reading below
                log.info("Telegram: incident text fallback (%s)", type(e).__name__)
        cover = store.get(inc.cover_alert_id) if inc.cover_alert_id else None
        if cover is None:
            alerts = store.incident_alerts(inc.id)
            cover = alerts[-1] if alerts else None
        labels: list[str] = []
        if cover is not None:
            boxes = sorted(cover.boxes or [], key=lambda b: 0 if b.get("role") == "subject" else 1)
            for b in boxes:
                lab = str(b.get("label") or "")
                if lab and lab not in labels:
                    labels.append(lab)
        clip = cover.clip_path if cover is not None and cover.clip_path and os.path.isfile(cover.clip_path) else ""
        return Event(
            kind=kind, ts=float(inc.start_ts), title=inc.title or (cover.title if cover else "Incident"),
            severity=inc.severity, site_id=inc.site_id, channel=(inc.channels[0] if inc.channels else None),
            camera=", ".join(inc.cameras), incident_id=inc.id, alert_id=cover.id if cover else "",
            alert_count=int(inc.alert_count or 0), verdict=inc.verdict,
            ai_note=(cover.brain_findings[0] if cover is not None and inc.verdict == "confirmed"
                     and cover.brain_findings else ""),
            status=inc.status, labels=labels,
            snapshot_path=cover.snapshot_path if cover else "", crop_path=cover.crop_path if cover else "",
            clip_path=clip, clip_pending=bool(cover is not None and not clip and (cover.clip_offset_s or -1) >= 0),
            web_path=f"#/incident/{inc.id}")

    def _refs_for(self, incident_id: str) -> list[dict]:
        """Messages of this incident in our chats: from our own sends, else from the hub's log."""
        with self._mem_lock:
            ref = self._threads.get(incident_id)
        if ref is None:
            try:
                row = self.hub.log.thread(self.conf.id, incident_id)
            except Exception:  # noqa: BLE001
                row = None
            raw = (row or {}).get("ref") if isinstance(row, dict) else None
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = None
            ref = raw if isinstance(raw, dict) else None
        return list((ref or {}).get("messages") or [])

    # -- callbacks -------------------------------------------------------------------------------
    def _answer(self, cq_id: str, text: str = "", alert: bool = False) -> None:
        try:
            self.api().call("answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:200] or None,
                                                    "show_alert": alert or None}, timeout=10)
        except ChannelError as e:
            log.info("Telegram: could not answer a button press: %s", self._scrub(e))

    def _on_callback(self, cq: dict, backlog: bool) -> None:
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if not self._allowed(cid):
            log.info("Telegram: ignored a button press from an unknown chat")
            return
        cid = int(cid)
        cq_id = str(cq.get("id") or "")
        data = str(cq.get("data") or "")
        user = str((cq.get("from") or {}).get("first_name") or "")
        mid = msg.get("message_id")
        if backlog:
            self._answer(cq_id, EXPIRED)
            return
        prefix, _, arg = data.partition(":")
        if prefix in ("h", "f", "o", "u") or (prefix == "m" and arg == "off"):
            if not self._allow_control():
                self._answer(cq_id, CONTROL_OFF, alert=True)
                return
        if prefix in ("h", "f", "o"):
            self._answer(cq_id)
            self._mark(cid, mid, _photo_kind(msg), arg, {"h": "handled", "f": "false_alarm", "o": "open"}[prefix], user)
        elif prefix == "c":
            self._open_clip(cid, cq_id, arg, mid)
        elif prefix == "s":
            try:
                ch = int(arg)
            except ValueError:
                self._answer(cq_id)
                return
            self._answer(cq_id, "Getting a picture...")
            self._snapshot(cid, ch, user, reply_to=None)
        elif prefix == "i":
            self._answer(cq_id)
            self._send_incident_card(cid, arg)
        elif prefix == "m":
            self._answer(cq_id)
            if arg == "off":
                self._edit_plain(cid, mid, "Turning monitoring off...")
                res = self._ask("set_monitoring", {"on": False}, user)
                text = ("Monitoring is turning off. Send /arm to turn it on again." if res.get("ok")
                        else str(res.get("message") or "Monitoring could not be turned off."))
                self._edit_plain(cid, mid, text)
            else:
                self._edit_plain(cid, mid, "Cancelled. Monitoring stays as it is.")
        elif prefix == "u":
            self._answer(cq_id)
            self._mute(cid, arg, user, edit_mid=mid)
        else:
            self._answer(cq_id, EXPIRED)

    def _edit_plain(self, cid: int, mid, text: str) -> None:
        if not mid:
            self._reply(cid, text)
            return
        try:
            self._call("editMessageText", {"chat_id": cid, "message_id": int(mid), "text": text,
                                           "reply_markup": {"inline_keyboard": []}}, chat=cid)
        except ChannelError as e:
            log.info("Telegram: could not update a message: %s", self._scrub(e))

    def _mark(self, cid: int, mid, kind: str, incident_id: str, status: str, user: str) -> None:
        res = self._ask("mark_incident", {"incident_id": incident_id, "status": status,
                                          "dest_id": self.conf.id}, user)
        if not res.get("ok"):
            self._reply(cid, str(res.get("message") or "That incident could not be updated (was it deleted?)."),
                        reply_to=mid)
            return
        event = self.incident_event(incident_id, kind="incident_status")
        if event is None:
            return
        event = replace(event, status=str(res.get("status") or status),
                        status_by=f"{user} (Telegram)" if user else "Telegram",
                        extra={**(event.extra or {}), "status_ts": time.time()})
        markup = self.keyboard(event, self._web_base())
        targets = {(self._chat_id(m.get("chat")), int(m.get("id"))): m.get("kind", "text")
                   for m in self._refs_for(incident_id) if m.get("kind") != "group" and m.get("id")}
        if mid:
            targets[(cid, int(mid))] = kind
        for (c, m), k in targets.items():
            if not self._allowed(c):
                continue
            try:
                self._edit_one(c, m, k, event, markup)
            except ChannelError as e:
                log.info("Telegram: could not update an alert message: %s", self._scrub(e))

    def _open_clip(self, cid: int, cq_id: str, alert_id: str, reply_to) -> None:
        store = self._store()
        a = store.get(alert_id) if store is not None else None
        if a is None:
            self._answer(cq_id, "This alert is no longer saved in YardGuard.")
            return
        if a.clip_path and os.path.isfile(a.clip_path):
            self._answer(cq_id, "Sending the clip...")
            try:
                self._call("sendChatAction", {"chat_id": cid, "action": "upload_video"})
            except ChannelError:
                pass
            try:
                self._send_clip(cid, a.clip_path, reply_to, caption=f"{a.camera} · {media.clock(a.ts)}")
            except ChannelError as e:
                self._reply(cid, f"The clip could not be sent: {self._scrub(e)}", reply_to=reply_to)
        elif (a.clip_offset_s or -1) >= 0 and not a.clip_path:
            self._answer(cq_id, "The clip is still being saved, try again in a minute.", alert=True)
        else:
            self._answer(cq_id, "There is no clip for this alert.", alert=True)

    def _send_incident_card(self, cid: int, incident_id: str) -> None:
        event = self.incident_event(incident_id)
        if event is None:
            self._reply(cid, "That incident is no longer saved in YardGuard.")
            return
        ctx = SendContext(delivery_id=f"card-{secrets.token_hex(4)}", pictures="crop", silent=True, priority=3,
                          thread_ref=None, edit=False, send_clip=False, web_base=self._web_base(), attempt=1)
        try:
            pics = self._pictures(event, "crop")
            self._send_to(cid, event, ctx, pics, None, [])
        except ChannelError as e:
            self._reply(cid, f"Could not show the incident: {self._scrub(e)}")

    # -- commands --------------------------------------------------------------------------------
    def _cmd_start(self, cid: int, arg: str, user: str, msg: dict) -> None:
        self._reply(cid, "Hi! This chat gets YardGuard alerts.\n\n" + HELP_TEXT)

    def _cmd_help(self, cid: int, arg: str, user: str, msg: dict) -> None:
        self._reply(cid, HELP_TEXT)

    def _cmd_status(self, cid: int, arg: str, user: str, msg: dict) -> None:
        res = self._ask("status", {}, user)
        if "running" not in res:
            self._reply(cid, str(res.get("message") or "YardGuard did not answer."))
            return
        self._reply(cid, status_text(res))

    def _cameras(self, user: str = "") -> list[tuple[int, str]]:
        frames = getattr(self.hub, "frames", None)
        try:
            cams = frames.cameras() if frames is not None else None
        except Exception:  # noqa: BLE001
            cams = None
        if cams:
            return [(int(ch), str(name)) for ch, name in cams]
        res = self._ask("status", {}, user)
        return [(int(c.get("channel")), str(c.get("name") or f"Camera {c.get('channel')}"))
                for c in res.get("cameras") or [] if c.get("channel") is not None]

    def _cmd_snapshot(self, cid: int, arg: str, user: str, msg: dict) -> None:
        cams = self._cameras(user)
        if not cams:
            self._reply(cid, "There are no cameras set up in YardGuard.")
            return
        if not arg:
            rows, row = [], []
            for ch, name in cams:
                row.append(_btn(name[:40], f"s:{ch}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            rows.append(row)
            self._reply(cid, "Which camera?", _markup(rows))
            return
        ch = match_camera(cams, arg)
        if ch is None:
            self._reply(cid, f"No camera called \"{arg[:40]}\". Cameras: " + ", ".join(n for _, n in cams) + ".")
            return
        self._snapshot(cid, ch, user, reply_to=msg.get("message_id"))

    def _snapshot(self, cid: int, ch: int, user: str, reply_to=None) -> None:
        names = dict(self._cameras(user))
        name = names.get(ch, f"Camera {ch}")
        try:
            self._call("sendChatAction", {"chat_id": cid, "action": "upload_photo"})
        except ChannelError:
            pass
        res = self._ask("snapshot", {"channel": ch, "max_side": 1280}, user, wait=SNAPSHOT_WAIT_S)
        jpeg = res.get("jpeg") if res.get("ok") else None
        name = str(res.get("camera") or name)
        if not jpeg:
            self._reply(cid, f"No picture from {name} right now.", reply_to=reply_to)
            return
        p = {"chat_id": cid, "caption": f"{name} · now",
             "protect_content": bool(self.params.get("protect_content")) or None}
        if reply_to:
            p["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        try:
            self._call("sendPhoto", p, files={"photo": ("snapshot.jpg", bytes(jpeg), "image/jpeg")}, chat=cid,
                       timeout=60)
        except ChannelError as e:
            self._reply(cid, f"The picture could not be sent: {self._scrub(e)}")

    def _cmd_arm(self, cid: int, arg: str, user: str, msg: dict) -> None:
        if not self._allow_control():
            self._reply(cid, CONTROL_OFF)
            return
        res = self._ask("set_monitoring", {"on": True}, user)
        if res.get("ok"):
            already = "already" in str(res.get("message") or "")
            self._reply(cid, "Monitoring is already on." if already else "Monitoring is on.")
        else:
            self._reply(cid, str(res.get("message") or "Monitoring could not be turned on."))

    def _cmd_disarm(self, cid: int, arg: str, user: str, msg: dict) -> None:
        if not self._allow_control():
            self._reply(cid, CONTROL_OFF)
            return
        self._reply(cid, "Turn monitoring off? Nothing will be watched until you turn it on again.",
                    _markup([[_btn("Yes, turn off", "m:off"), _btn("Cancel", "m:x")]]))

    def _cmd_incidents(self, cid: int, arg: str, user: str, msg: dict) -> None:
        res = self._ask("list_incidents", {}, user)
        items = list(res.get("items") or [])[:5]
        if not items:
            self._reply(cid, "No open incidents." if "items" in res else
                        str(res.get("message") or "YardGuard did not answer."))
            return
        lines, rows = ["Open incidents:"], []
        for n, it in enumerate(items, 1):
            cams = it.get("cameras")
            cams = ", ".join(cams) if isinstance(cams, list) else str(cams or "")
            sev = str(it.get("severity") or "").capitalize()
            when = media.clock(float(it.get("start_ts") or 0))
            lines.append(f"{n}. {it.get('title') or 'Incident'} · {cams} · {when}" + (f" · {sev}" if sev else ""))
            title = str(it.get("title") or "Incident")
            rows.append([_btn(f"{n}. {title[:36]}", f"i:{it.get('id')}")])
        self._reply(cid, "\n".join(lines), _markup(rows))

    def _cmd_mute(self, cid: int, arg: str, user: str, msg: dict) -> None:
        # pausing messages affects every connection, so it is control like /arm and /disarm
        if not self._allow_control():
            self._reply(cid, CONTROL_OFF)
            return
        if not arg:
            self._reply(cid, "Pause messages for how long? Critical alerts still come through.",
                        _markup([[_btn("1 hour", "u:1h"), _btn("8 hours", "u:8h"), _btn("Resume", "u:off")]]))
            return
        self._mute(cid, arg, user)

    def _mute(self, cid: int, arg: str, user: str, edit_mid=None) -> None:
        arg = arg.strip().lower()
        if arg in ("off", "0", "resume", "on", "stop"):
            until = 0.0
        else:
            secs = parse_duration(arg)
            if secs is None:
                self._reply(cid, "Send /mute 1h, /mute 8h or /mute off.")
                return
            until = time.time() + secs
        res = self._ask("snooze", {"until": until}, user)
        if "until" not in res:
            text = str(res.get("message") or "Messages could not be paused.")
        elif float(res.get("until") or 0) > time.time():
            text = (f"Messages paused until {_hhmm(float(res['until']))}. "
                    "Critical alerts still come through.")
        else:
            text = "Messages are on again."
        if edit_mid:
            self._edit_plain(cid, edit_mid, text)
        else:
            self._reply(cid, text)


def match_camera(cams: list[tuple[int, str]], arg: str) -> int | None:
    """ "/snapshot 2", "/snapshot front", "/snapshot Front gate" -> channel."""
    a = arg.strip().lower()
    if not a:
        return None
    if a.isdigit():
        for ch, _ in cams:
            if ch == int(a):
                return ch
    for test in (lambda n: n == a, lambda n: n.startswith(a), lambda n: a in n):
        hits = [ch for ch, name in cams if test(name.lower())]
        if hits:
            return hits[0]
    return None


def status_text(res: dict) -> str:
    """The /status answer: one summary line and one line per camera."""
    cams = list(res.get("cameras") or [])
    running = bool(res.get("running"))
    parts = ["Monitoring is on" if running else "Monitoring is off"]
    if running and cams:
        watching = sum(c.get("state") in ("watching", "idle") for c in cams)
        if watching == len(cams):
            parts.append(f"{watching} camera{'s' if watching != 1 else ''} watching")
        else:
            parts.append(f"{watching} of {len(cams)} cameras watching")
    n_open = int(res.get("open") or 0)
    parts.append(_plural(n_open, "open incident") if n_open else "No open incidents")
    until = float(res.get("snoozed_until") or 0)
    if until > time.time():
        parts.append(f"Messages paused until {_hhmm(until)}")
    lines = [" · ".join(parts)]
    if res.get("site"):
        lines.append(str(res["site"]))
    msg = str(res.get("message") or "")
    if msg and not msg.startswith("Monitoring is o"):
        lines.append(msg)      # "Preparing the AI..." and similar
    for c in cams:
        word = STATE_WORDS.get(str(c.get("state") or ""), str(c.get("state") or "Unknown").capitalize())
        objs = _objects_text(c.get("objects") or {}) if c.get("state") != "offline" else ""
        lines.append(f"{c.get('name') or 'Camera ' + str(c.get('channel'))}: {word}" + (f" ({objs})" if objs else ""))
    return "\n".join(lines)


# --- setup support ------------------------------------------------------------------------------

class PairingListener:
    """Used by the setup dialog: polls getUpdates (timeout 2 s per call) for up to 3 min and reports
    chats that sent /start (with or without the code) or any message, or added the bot to a group.
    Runs on its own thread; the dialog reads found() on a QTimer. Stopped when the dialog closes.
    Must not run while the destination's own poller runs for the same token: the hub pauses that
    poller (hub.channel(id).pause_polling() / resume_polling())."""

    def __init__(self, token: str, code: str, api_base: str = API_BASE, duration: float = PAIR_SECONDS) -> None:
        self.token = normalize_token(token)
        self.code = str(code or "")
        self.api_base = api_base or API_BASE
        self.duration = duration
        self.error = ""
        self._found: OrderedDict[int, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._api: TelegramApi | None = None
        self._started = 0.0
        self._replied: set[int] = set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def remaining(self) -> float:
        """Seconds left to wait for /start (0 when finished)."""
        if not self._started:
            return self.duration
        return max(0.0, self._started + self.duration - time.time()) if self.running else 0.0

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._started = time.time()
        self._api = TelegramApi(self.token, self.api_base, timeout=10)
        self._thread = threading.Thread(target=self._run, name="yg-telegram-pair", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._api is not None:
            self._api.abort()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.5)

    def found(self) -> list[dict]:
        """[{"id", "title", "type", "with_code", "user"}], newest first."""
        with self._lock:
            items = sorted(self._found.values(), key=lambda c: -c["_ts"])
            return [{k: v for k, v in c.items() if not k.startswith("_")} for c in items]

    def _run(self) -> None:
        api = self._api
        offset = 0
        try:
            while not self._stop.is_set() and time.time() < self._started + self.duration:
                try:
                    updates = api.poll(offset, 2, ALLOWED_UPDATES, should_stop=self._stop.is_set)
                except _Again:
                    continue
                except _Aborted:
                    break
                except TelegramError as e:
                    # right after the app's own poller was paused Telegram may still answer 409 for a moment
                    if not (e.conflict and time.time() - self._started < 15):
                        self.error = str(e)
                    if e.permanent:
                        break
                    self._stop.wait(e.retry_after or 2)
                    continue
                except Exception as e:  # noqa: BLE001
                    self.error = scrub(f"Could not listen for /start ({type(e).__name__}).", self.token)
                    self._stop.wait(2)
                    continue
                self.error = ""
                for u in updates:
                    try:
                        offset = max(offset, int(u.get("update_id", 0)) + 1)
                        self._take(u, api)
                    except Exception as e:  # noqa: BLE001
                        log.info("Telegram pairing: skipped an update (%s)", type(e).__name__)
        finally:
            api.drop_poll()
            api.close()

    def _take(self, u: dict, api: TelegramApi) -> None:
        msg = u.get("message")
        joined = False
        if msg is None and u.get("my_chat_member"):
            m = u["my_chat_member"]
            if (m.get("new_chat_member") or {}).get("status") not in ("member", "administrator"):
                return
            msg, joined = {"chat": m.get("chat"), "from": m.get("from"), "date": m.get("date"), "text": ""}, True
        if not isinstance(msg, dict) or not isinstance(msg.get("chat"), dict):
            return
        if float(msg.get("date") or 0) < self._started - 10:
            return     # sent before Connect was pressed
        chat, sender = msg["chat"], msg.get("from") or {}
        cid = int(chat["id"])
        ctype = str(chat.get("type") or "private")
        if ctype == "channel":
            return
        text = str(msg.get("text") or "")
        with_code = bool(self.code) and re.search(rf"(^|\D){re.escape(self.code)}(\D|$)", text) is not None
        if ctype == "private":
            title = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p) or (
                "@" + chat["username"] if chat.get("username") else "Private chat")
        else:
            title = str(chat.get("title") or "Group")
        with self._lock:
            old = self._found.get(cid) or {}
            self._found[cid] = {"id": cid, "title": title, "type": ctype,
                                "with_code": bool(old.get("with_code")) or with_code,
                                "user": str(sender.get("first_name") or old.get("user") or ""),
                                "joined": joined or bool(old.get("joined")), "_ts": time.time()}
        if with_code and cid not in self._replied:
            self._replied.add(cid)
            try:
                api.call("sendMessage", {"chat_id": cid, "text": "Found this chat. Go back to YardGuard on your PC "
                                                                 "to finish the setup."})
            except ChannelError:
                pass
