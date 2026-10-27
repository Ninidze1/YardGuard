"""Phone push through ntfy (https://ntfy.sh or the user's own ntfy server).

A message with a picture is one PUT of the JPEG to <server>/<topic> with the texts in headers
(RFC 2047 encoded when they are not plain ASCII). Without a picture it is one JSON publish to the
server root. The topic is long and random: anyone who knows it can read the messages, so the UI
shows it only shortened. An optional access token (Bearer) is stored encrypted.
"""
from __future__ import annotations

import base64
import logging
import re
import secrets as _secrets
import string
import time
from urllib.parse import urlsplit

from . import media
from .base import Channel, ChannelError, http
from .base import scrub as _scrub
from .model import CheckResult, Event, SendContext, SendResult

log = logging.getLogger("yardguard")
logging.getLogger("urllib3").setLevel(logging.WARNING)


def scrub(text: str, *secrets: str) -> str:
    """base.scrub without the empty strings (an unset token must not mask every character)."""
    return _scrub(text, *[s for s in secrets if s])

DEFAULT_SERVER = "https://ntfy.sh"
MAX_UPLOAD = 15_000_000          # ntfy.sh attachment limit
PICTURE_BYTES = 4_000_000        # what we upload at most (prepare_jpeg lowers the quality until it fits)
TIMEOUT_S = 20
TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
TAGS = {"critical": "rotating_light", "warning": "warning", "info": "information_source"}
_ALPHABET = string.ascii_lowercase + string.digits


def random_topic() -> str:
    """A new hard-to-guess topic name: "yardguard-" + 20 random letters/digits."""
    return "yardguard-" + "".join(_secrets.choice(_ALPHABET) for _ in range(20))


def normalize_server(server: str) -> str:
    """"ntfy.example.com/" -> "https://ntfy.example.com"; "" -> ntfy.sh."""
    s = (server or "").strip().rstrip("/")
    if not s:
        return DEFAULT_SERVER
    if "://" not in s:
        s = "https://" + s
    return s


def valid_server(server: str) -> bool:
    try:
        parts = urlsplit(server)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username


def subscribe_url(server: str, topic: str) -> str:
    """The web address of the topic; the ntfy app's "Subscribe to topic" accepts it (also as QR)."""
    return f"{normalize_server(server)}/{topic}"


def short_topic(topic: str) -> str:
    return topic if len(topic) <= 16 else topic[:13] + "..."


def header_value(text: str) -> str:
    """A header-safe value: ASCII as is, anything else as RFC 2047 encoded words (UTF-8, base64),
    split so each word stays under 75 characters. Newlines are sent as the literal \\n ntfy expands."""
    text = text.replace("\r", "").replace("\n", "\\n")
    if text.isascii():
        return text
    words, chunk = [], ""
    for ch in text:
        if len((chunk + ch).encode("utf-8")) > 45:
            words.append(chunk)
            chunk = ""
        chunk += ch
    if chunk:
        words.append(chunk)
    return " ".join("=?UTF-8?B?" + base64.b64encode(w.encode("utf-8")).decode("ascii") + "?=" for w in words)


def _texts(event: Event) -> tuple[str, str]:
    """(title, message) for the notification: the headline, then the remaining lines."""
    lines = [ln for ln in media.lines(event) if ln]
    title = lines[0] if lines else (event.title or "YardGuard")
    message = "\n".join(lines[1:]) or media.subline(event) or title
    return title[:250], message[:3500]


def _error_result(exc: Exception, started: float, *secrets: str) -> SendResult:
    ms = int((time.monotonic() - started) * 1000)
    if isinstance(exc, ChannelError):
        return SendResult(ok=False, error=scrub(str(exc), *secrets), retry=bool(getattr(exc, "retry", False)),
                          retry_after=getattr(exc, "retry_after", None),
                          permanent=bool(getattr(exc, "permanent", False)), ms=ms)
    log.warning("ntfy: unexpected error (%s)", type(exc).__name__)
    return SendResult(ok=False, error=scrub(f"Unexpected problem: {exc}", *secrets), retry=True, ms=ms)


def _raise_for(resp, has_token: bool, *secrets: str) -> None:
    """Turns a 4xx answer of ntfy into a ChannelError with plain words (2xx returns)."""
    code = resp.status_code
    if 200 <= code < 300:
        return
    if code in (401, 403):
        if has_token:
            raise ChannelError("The access token was not accepted by the ntfy server.", permanent=True,
                               field="token")
        raise ChannelError("This topic is protected on the ntfy server. Add an access token.", permanent=True,
                           field="token")
    if code == 408:
        raise ChannelError("The ntfy server did not answer in time.", retry=True)
    detail = ""
    try:
        detail = str(resp.json().get("error", ""))[:120]
    except Exception:
        pass
    text = f"The ntfy server did not accept the message (HTTP {code})"
    raise ChannelError(scrub(f"{text}: {detail}" if detail else text + ".", *secrets), permanent=True)


class NtfyChannel(Channel):
    KIND = "ntfy"
    CAPS = {"pictures": True, "clip": False, "edit": False, "buttons": False, "two_way": False,
            "max_upload": MAX_UPLOAD, "updates": "drop"}
    SECRET_KEYS = ("token",)

    # --- setup ---------------------------------------------------------------

    @classmethod
    def check(cls, params: dict, secrets: dict[str, str]) -> CheckResult:
        """Is the server reachable, is the topic name usable, is the token (if any) accepted?
        Publishes nothing."""
        server = normalize_server(params.get("server", ""))
        topic = str(params.get("topic", "")).strip()
        token = (secrets.get("token") or "").strip()
        if not valid_server(server):
            return CheckResult(False, "Enter the server address, for example https://ntfy.sh", field="server")
        if not TOPIC_RE.match(topic):
            return CheckResult(False, "The topic may only use letters, digits, - and _ (up to 64).", field="topic")
        host = urlsplit(server).hostname or server
        try:
            r = http("GET", f"{server}/v1/health", secrets=(token,), timeout=10)
            if r.status_code == 404:
                r = http("HEAD", server + "/", secrets=(token,), timeout=10)
            if r.status_code >= 400 and r.status_code not in (401, 403):
                return CheckResult(False, f"{host} does not look like an ntfy server (HTTP {r.status_code}).",
                                   field="server")
            data: dict = {"server": server, "topic": topic}
            if token:
                r = http("GET", f"{server}/v1/account", secrets=(token,), timeout=10,
                         headers={"Authorization": f"Bearer {token}"})
                if r.status_code in (401, 403):
                    return CheckResult(False, "The access token was not accepted.", field="token")
                if r.status_code >= 400:
                    return CheckResult(False, f"Could not check the access token (HTTP {r.status_code}).",
                                       field="token")
                try:
                    user = str(r.json().get("username", ""))
                except Exception:
                    user = ""
                if user and user != "*":
                    data["user"] = user
                    return CheckResult(True, f"Connected to {host} as {user}", data=data)
            return CheckResult(True, f"{host} is reachable", data=data)
        except ChannelError as e:
            return CheckResult(False, scrub(str(e), token), field=getattr(e, "field", "") or "server")
        except Exception as e:
            return CheckResult(False, scrub(f"Could not check the server: {e}", token), field="server")

    # --- delivery ------------------------------------------------------------

    def _target(self) -> tuple[str, str]:
        server = normalize_server(self.conf.params.get("server", ""))
        topic = str(self.conf.params.get("topic", "")).strip()
        if not valid_server(server):
            raise ChannelError("The ntfy server address is not valid.", permanent=True, field="server")
        if not TOPIC_RE.match(topic):
            raise ChannelError("The ntfy topic name is not valid.", permanent=True, field="topic")
        return server, topic

    def send(self, event: Event, ctx: SendContext) -> SendResult:
        started = time.monotonic()
        token = ""
        try:
            token = self.conf.secret("token")
            if ctx.edit:   # ntfy cannot change a sent notification
                return SendResult(ok=True, note="ntfy cannot update a sent message")
            server, topic = self._target()
            title, message = _texts(event)
            priority = max(1, min(5, int(ctx.priority or 3)))
            tag = TAGS.get(event.severity, "information_source")
            click = ""
            if self.conf.params.get("click_url", True) and ctx.web_base and event.web_path:
                click = ctx.web_base.rstrip("/") + "/" + event.web_path.lstrip("/")
            auth = {"Authorization": f"Bearer {token}"} if token else {}
            jpeg = None
            if ctx.pictures != "none":
                path = event.image_path(ctx.pictures)
                if path:
                    jpeg = media.prepare_jpeg(path, 1280, 80, PICTURE_BYTES)
            note = ""
            if jpeg:
                headers = {"Filename": "yardguard.jpg", "Title": header_value(title),
                           "Message": header_value(message), "Priority": str(priority), "Tags": tag,
                           "Content-Type": "image/jpeg", **auth}
                if click:
                    headers["Click"] = click
                r = http("PUT", f"{server}/{topic}", secrets=(token, topic), timeout=TIMEOUT_S, data=jpeg,
                         headers=headers)
                if r.status_code == 413:
                    note, jpeg = "The picture was too large for the ntfy server; sent text only.", None
                else:
                    _raise_for(r, bool(token), token, topic)
            if not jpeg:
                body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": [tag]}
                if click:
                    body["click"] = click
                r = http("POST", server + "/", secrets=(token, topic), timeout=TIMEOUT_S, json=body, headers=auth)
                _raise_for(r, bool(token), token, topic)
            ref = None
            try:
                mid = r.json().get("id")
                ref = {"message_id": str(mid)} if mid else None
            except Exception:
                pass
            return SendResult(ok=True, ref=ref, ms=int((time.monotonic() - started) * 1000), note=note)
        except Exception as e:  # send never raises
            return _error_result(e, started, token)

    # --- status --------------------------------------------------------------

    def describe(self) -> str:
        server = normalize_server(self.conf.params.get("server", ""))
        host = urlsplit(server).hostname or server
        topic = str(self.conf.params.get("topic", ""))
        return f"Topic {short_topic(topic)} on {host}" if topic else f"ntfy on {host}"
