"""Tests for yardguard/integrations/telegram.py against a local fake of the Telegram Bot API.

Run:  run.cmd tests\\test_integrations_telegram.py
Nothing reaches the real Telegram: the fake binds 127.0.0.1 on an OS-chosen port and the channel's
api_base points at it. Config and events live under .cache\\tests\\integrations\\telegram\\.
When the core builder's integrations modules (model, base, media) are not there yet, small
test-only stand-ins written from the SPEC are used (printed as NOTE); they are never shipped.
"""
from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import types
from concurrent.futures import Future
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "integrations" / "telegram"
if OUT.exists():
    shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True, exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yardguard import config  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"

TOKEN = "123456789:AAH" + "x7Kq_P0-zZ9mN3vB8cD4eF5gH6iJ7kL8"
BOT = "YardTestBot"
STUBS: list[str] = []


# --- test-only stand-ins for core modules that are not published yet -------------------------------

def _stub_model() -> types.ModuleType:
    m = types.ModuleType("yardguard.integrations.model")
    import uuid

    @dataclass
    class Event:
        kind: str
        ts: float
        title: str
        text: str = ""
        severity: str = "info"
        id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
        site_id: str = ""
        site_name: str = ""
        channel: int | None = None
        camera: str = ""
        incident_id: str = ""
        alert_id: str = ""
        alert_count: int = 0
        verdict: str = ""
        ai_note: str = ""
        status: str = ""
        status_by: str = ""
        labels: list = field(default_factory=list)
        area_id: str = ""
        area_name: str = ""
        area_task: str = ""
        area_kind: str = ""
        plate: str = ""
        plate_known: bool | None = None
        snapshot_path: str = ""
        crop_path: str = ""
        clip_path: str = ""
        clip_pending: bool = False
        web_path: str = ""
        extra: dict = field(default_factory=dict)
        escalated: bool = False

        def image_path(self, pictures: str) -> str:
            if pictures == "none":
                return ""
            if pictures == "snapshot":
                return self.snapshot_path or self.crop_path
            return self.crop_path or self.snapshot_path

    @dataclass
    class SendContext:
        delivery_id: str
        pictures: str
        silent: bool
        priority: int
        thread_ref: dict | None
        edit: bool
        send_clip: bool
        web_base: str
        attempt: int

    @dataclass
    class SendResult:
        ok: bool
        ref: dict | None = None
        error: str = ""
        retry: bool = False
        retry_after: float | None = None
        permanent: bool = False
        ms: int = 0
        note: str = ""

    @dataclass
    class CheckResult:
        ok: bool
        message: str
        field: str = ""
        data: dict = field(default_factory=dict)

    @dataclass
    class Action:
        kind: str
        args: dict
        source: str
        user: str = ""

    for c in (Event, SendContext, SendResult, CheckResult, Action):
        c.__module__ = m.__name__
        setattr(m, c.__name__, c)
    return m


def _stub_base() -> types.ModuleType:
    m = types.ModuleType("yardguard.integrations.base")

    def scrub(text: str, *secrets: str) -> str:
        out = str(text)
        for s in secrets:
            if s:
                out = out.replace(s, "***")
        out = re.sub(r"bot\d{5,12}:[A-Za-z0-9_-]{30,}", "bot***", out)
        return out[:300]

    class ChannelError(Exception):
        def __init__(self, message: str, *, retry: bool = False, permanent: bool = False,
                     retry_after: float | None = None, field: str = "") -> None:
            super().__init__(message)
            self.retry, self.permanent, self.retry_after, self.field = retry, permanent, retry_after, field

    class IntegrationConf:
        def __init__(self, d: dict) -> None:
            self.d = d

        id = property(lambda self: self.d["id"])
        kind = property(lambda self: self.d["kind"])
        name = property(lambda self: self.d.get("name", ""))
        enabled = property(lambda self: bool(self.d.get("enabled", True)))
        params = property(lambda self: self.d.setdefault("params", {}))
        routes = property(lambda self: self.d.setdefault("routes", {}))

        def secret(self, key: str) -> str:
            v = self.d.get("secrets", {}).get(key, "")
            return config.decrypt(v) if v else ""

        def set_secret(self, key: str, value: str) -> None:
            if value:
                self.d.setdefault("secrets", {})[key] = config.encrypt(value)
            else:
                self.d.setdefault("secrets", {}).pop(key, None)

        def has_secret(self, key: str) -> bool:
            return bool(self.d.get("secrets", {}).get(key))

    class Channel:
        KIND = ""
        CAPS: dict = {}
        SECRET_KEYS: tuple = ()

        def __init__(self, conf, hub) -> None:
            self.conf, self.hub = conf, hub
            self.status, self.status_text = "off", ""

    m.scrub, m.ChannelError, m.IntegrationConf, m.Channel = scrub, ChannelError, IntegrationConf, Channel
    return m


def _stub_media() -> types.ModuleType:
    m = types.ModuleType("yardguard.integrations.media")
    from datetime import datetime

    def prepare_jpeg(path, max_side=1280, quality=80, max_bytes=5_000_000):
        try:
            img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        except (OSError, ValueError):
            return None
        if img is None:
            return None
        h, w = img.shape[:2]
        if max(h, w) > max_side:
            f = max_side / max(h, w)
            img = cv2.resize(img, (int(w * f), int(h * f)))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    def clock(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M")

    def headline(e) -> str:
        return e.title

    def html_caption(e, limit: int = 1024) -> str:
        lines = ["<b>" + html.escape(e.title) + "</b>",
                 html.escape(" · ".join(p for p in (e.camera, clock(e.ts), e.severity.capitalize()) if p))]
        if e.status and e.status != "open":
            lines.append("Status " + e.status)
        return "\n".join(lines)[:limit]

    m.prepare_jpeg, m.clock, m.headline, m.html_caption = prepare_jpeg, clock, headline, html_caption
    return m


def _install_stubs() -> None:
    import importlib
    for name, make in (("model", _stub_model), ("base", _stub_base), ("media", _stub_media)):
        full = f"yardguard.integrations.{name}"
        try:
            importlib.import_module(full)
        except ImportError:
            sys.modules[full] = make()
            STUBS.append(name)


_install_stubs()

from yardguard.engine import Alert, AlertStore  # noqa: E402
from yardguard.integrations import telegram as tg  # noqa: E402
from yardguard.integrations.base import IntegrationConf  # noqa: E402
from yardguard.integrations.model import Action, Event, SendContext  # noqa: E402

tg._RateLimiter.MIN_GAP = 0.02       # keep the suite fast; one test below checks the real limits
tg._RateLimiter.GROUP_PER_MIN = 1000

LOG = io.StringIO()
_h = logging.StreamHandler(LOG)
_h.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_h)
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger("yardguard").setLevel(logging.DEBUG)


# --- fake Telegram Bot API ----------------------------------------------------------------------

class FakeTelegram:
    """Local stand-in for api.telegram.org: records calls, long-polls updates, injects errors."""

    def __init__(self, token: str = TOKEN, username: str = BOT) -> None:
        self.token, self.username = token, username
        self.calls: list[tuple[str, dict, dict]] = []
        self.updates: list[dict] = []
        self.next_update = 1
        self.next_msg = 100
        self.cond = threading.Condition()
        self.fail: dict[str, list[tuple[int, dict]]] = {}
        self.poll_cap = 30.0
        self.getupdates_count = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # keep test output clean
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                m = re.fullmatch(r"/bot([^/]+)/(\w+)", self.path)
                if not m:
                    return self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                token, method = m.groups()
                params, files = fake._parse(self.headers.get("Content-Type", ""), body)
                status, data = fake.handle(token, method, params, files)
                self._send(status, data)

            def _send(self, status, data):
                raw = json.dumps(data).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):   # aborted long polls are expected
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-telegram", daemon=True)
        self.thread.start()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        with self.cond:
            self.cond.notify_all()
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _parse(ctype: str, body: bytes) -> tuple[dict, dict]:
        if ctype.startswith("application/json"):
            return (json.loads(body) if body else {}), {}
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
            params, files = {}, {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    files[name] = part.get_payload(decode=True)
                else:
                    v = part.get_payload(decode=True).decode()
                    try:
                        params[name] = json.loads(v)    # numbers, booleans, JSON objects
                    except ValueError:
                        params[name] = v
            return params, files
        return {}, {}

    def push(self, update: dict) -> int:
        with self.cond:
            update = {"update_id": self.next_update, **update}
            self.next_update += 1
            self.updates.append(update)
            self.cond.notify_all()
            return update["update_id"]

    def msg(self, chat: dict, text: str, user: str = "Alex", age: float = 0) -> int:
        self.next_msg += 1
        return self.push({"message": {"message_id": self.next_msg, "date": int(time.time() - age), "chat": chat,
                                      "from": {"id": 7, "first_name": user, "is_bot": False}, "text": text}})

    def press(self, chat: dict, data: str, message_id: int = 555, photo: bool = True, user: str = "Alex") -> int:
        message = {"message_id": message_id, "date": int(time.time()), "chat": chat}
        if photo:
            message["photo"] = [{"file_id": "x"}]
        else:
            message["text"] = "x"
        return self.push({"callback_query": {"id": f"cq{self.next_update}", "data": data, "message": message,
                                             "from": {"id": 7, "first_name": user}}})

    def handle(self, token: str, method: str, params: dict, files: dict) -> tuple[int, dict]:
        if token != self.token:
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        if method == "getUpdates":
            return self._get_updates(params)
        with self.cond:
            self.calls.append((method, params, files))
            queue_ = self.fail.get(method)
            if queue_:
                status, data = queue_.pop(0)
                return status, data
        return 200, {"ok": True, "result": self._result(method, params, files)}

    def _get_updates(self, params: dict) -> tuple[int, dict]:
        offset = int(params.get("offset") or 0)
        timeout = min(float(params.get("timeout") or 0), self.poll_cap)
        end = time.time() + timeout
        with self.cond:
            self.getupdates_count += 1
            queue_ = self.fail.get("getUpdates")
            if queue_:
                return queue_.pop(0)
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
            while not self.updates and time.time() < end:
                self.cond.wait(min(0.2, max(0.01, end - time.time())))
            return 200, {"ok": True, "result": list(self.updates)}

    def _result(self, method: str, params: dict, files: dict):
        if method == "getMe":
            return {"id": 42, "is_bot": True, "first_name": "Yard Test", "username": self.username}
        if method in ("deleteWebhook", "setMyCommands", "answerCallbackQuery", "sendChatAction"):
            return True
        if method == "sendMediaGroup":
            out = []
            for _ in params.get("media") or []:
                self.next_msg += 1
                out.append({"message_id": self.next_msg, "chat": {"id": params.get("chat_id")}})
            return out
        self.next_msg += 1
        return {"message_id": int(params.get("message_id") or self.next_msg), "chat": {"id": params.get("chat_id")}}

    # helpers for assertions
    def called(self, method: str) -> list[tuple[dict, dict]]:
        with self.cond:
            return [(p, f) for m, p, f in self.calls if m == method]

    def outgoing(self) -> int:
        with self.cond:
            return sum(1 for m, _, _ in self.calls if m not in ("getMe", "deleteWebhook", "setMyCommands"))

    def wait_for(self, method: str, count: int = 1, timeout: float = 5.0, pred=None) -> list[tuple[dict, dict]]:
        end = time.time() + timeout
        while time.time() < end:
            hits = [c for c in self.called(method) if pred is None or pred(c[0])]
            if len(hits) >= count:
                return hits
            time.sleep(0.02)
        raise AssertionError(f"{method} not called {count}x in {timeout}s; calls: {[c[0] for c in self.calls]}")

    def wait_polls(self, n: int, timeout: float = 5.0) -> None:
        end = time.time() + timeout
        start = self.getupdates_count
        while self.getupdates_count < start + n and time.time() < end:
            time.sleep(0.02)
        assert self.getupdates_count >= start + n, "poller did not poll"


# --- fake hub (only what telegram.py uses: request, log, frames, ctx, sample_event, web_base) -----

class FakeLog:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.threads: dict[tuple[str, str], dict] = {}

    def kv_get(self, key: str, default: str = "") -> str:
        return self.kv.get(key, default)

    def kv_set(self, key: str, value: str) -> None:
        self.kv[key] = value

    def thread(self, dest_id: str, incident_id: str):
        return self.threads.get((dest_id, incident_id))


class FakeFrames:
    def cameras(self):
        return [(1, "Front gate"), (2, "Driveway")]


class FakeSignal:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, *a) -> None:
        self.emitted.append(a)


class FakeHub:
    """Runs actions on its own 'GUI' thread like IntegrationHub.request() does."""

    def __init__(self, store: AlertStore, conf: IntegrationConf) -> None:
        self.ctx = types.SimpleNamespace(alert_store=store)
        self.log = FakeLog()
        self.frames = FakeFrames()
        self.destination_status = FakeSignal()
        self.status_notes: list[str] = []
        self.actions: list[Action] = []
        self.conf = conf
        self.running = True
        self.snooze_until = 0.0
        self.snapshot_ok = True

    def web_base(self) -> str:
        return ""

    def notify_status(self, dest_id: str) -> None:
        self.status_notes.append(dest_id)

    def sample_event(self) -> Event:
        return Event(kind="test", ts=time.time(), title="YardGuard test", camera="Front gate",
                     snapshot_path=str(SNAP), crop_path=str(CROP), channel=1)

    def request(self, action: Action) -> Future:
        fut: Future = Future()
        self.actions.append(action)

        def run():
            try:
                fut.set_result(self._do(action))
            except Exception as e:  # noqa: BLE001
                fut.set_exception(e)
        threading.Thread(target=run, name="fake-gui", daemon=True).start()
        return fut

    def _do(self, a: Action) -> dict:
        store = self.ctx.alert_store
        if a.kind == "mark_incident":
            store.mark_incident(a.args["incident_id"], a.args["status"])
            inc = store.get_incident(a.args["incident_id"])
            return {"ok": True, "title": inc.title, "status": inc.status}
        if a.kind == "set_monitoring":
            self.running = bool(a.args["on"])
            return {"ok": True, "running": self.running, "message": "Monitoring is starting"}
        if a.kind == "snapshot":
            if not self.snapshot_ok:
                return {"ok": False, "jpeg": b"", "camera": "Front gate"}
            ok, buf = cv2.imencode(".jpg", np.full((90, 160, 3), 128, np.uint8))
            return {"ok": True, "jpeg": buf.tobytes(), "camera": dict(self.frames.cameras())[a.args["channel"]]}
        if a.kind == "status":
            return {"running": self.running, "message": "", "open": store.open_incident_count(),
                    "snoozed_until": self.snooze_until, "site": "Home",
                    "cameras": [{"channel": 1, "name": "Front gate", "state": "watching", "objects": {"person": 1}},
                                {"channel": 2, "name": "Driveway", "state": "offline", "objects": {}}]}
        if a.kind == "list_incidents":
            return {"items": [{"id": i.id, "title": i.title, "severity": i.severity, "start_ts": i.start_ts,
                               "cameras": i.cameras, "status": i.status}
                              for i in store.list_incidents(limit=5, status="open")]}
        if a.kind == "snooze":
            self.snooze_until = float(a.args["until"])
            return {"until": self.snooze_until}
        if a.kind == "update_chat":
            for c in self.conf.params["chats"]:
                if c["id"] == a.args["old_id"]:
                    c["id"] = a.args["new_id"]
            return {"ok": True}
        return {"ok": False, "message": "Not available"}


# --- fixtures -----------------------------------------------------------------------------------

EVENTS = OUT / "events"
EVENTS.mkdir(parents=True, exist_ok=True)
SNAP = EVENTS / "snap.jpg"
CROP = EVENTS / "crop.jpg"
CLIP = EVENTS / "clip.mp4"
img = np.zeros((720, 1280, 3), np.uint8)
cv2.rectangle(img, (400, 200), (700, 600), (0, 200, 255), -1)
cv2.imwrite(str(SNAP), img)
cv2.imwrite(str(CROP), img[200:600, 400:700])
CLIP.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2048))

PRIVATE = {"id": 1001, "type": "private", "first_name": "Alex"}
GROUP = {"id": -2002, "type": "group", "title": "Family"}
STRANGER = {"id": 9999, "type": "private", "first_name": "Eve"}


def make_conf(api_base: str, chats=None, **params) -> IntegrationConf:
    d = {"id": "tg-3f9a1c2e", "kind": "telegram", "name": "Telegram", "enabled": True, "created": time.time(),
         "secrets": {"token": config.encrypt(TOKEN)},
         "params": {"api_base": api_base, "bot_username": BOT, "bot_name": "Yard Test",
                    "chats": chats if chats is not None else [
                        {"id": PRIVATE["id"], "title": "Alex", "type": "private", "enabled": True},
                        {"id": GROUP["id"], "title": "Family", "type": "group", "enabled": True}],
                    "allow_control": True, "protect_content": False, **params},
         "routes": {"pictures": "crop"}}
    return IntegrationConf(d)


def make_incident(store: AlertStore, severity: str = "warning", clip: str = "", clip_offset: float = -1.0,
                  title: str = "Person near the car at night", channel: int = 1) -> tuple[str, str]:
    a = Alert(id=f"20261023-021405-{channel}-{os.urandom(3).hex()}", site_id="s1", channel=channel,
              camera="Front gate", ts=time.time(), rule="night_presence", title=title, detail="near the car",
              severity=severity, snapshot_path=str(SNAP), crop_path=str(CROP),
              boxes=[{"label": "person", "x1": 1, "y1": 1, "x2": 5, "y2": 5, "role": "subject"}],
              verdict="confirmed", brain_findings=["hand on door handle"], clip_path=clip,
              clip_offset_s=clip_offset)
    store.add(a, gap_s=0)
    return a.incident_id, a.id


def incident_event(ch: tg.TelegramChannel, inc_id: str, kind: str = "incident_new", **kw) -> Event:
    e = ch.incident_event(inc_id, kind=kind)
    for k, v in kw.items():
        setattr(e, k, v)
    return e


def ctx_for(**kw) -> SendContext:
    base = dict(delivery_id=os.urandom(4).hex(), pictures="crop", silent=False, priority=3, thread_ref=None,
                edit=False, send_clip=False, web_base="", attempt=1)
    base.update(kw)
    return SendContext(**base)


PASSED: list[str] = []


def check(name: str):
    def deco(fn):
        t0 = time.time()
        fn()
        PASSED.append(f"{name} ({time.time() - t0:.1f}s)")
        print(f"PASS {name} ({time.time() - t0:.1f} s)")
        return fn
    return deco


def all_callback_data(markup: dict | None) -> list[str]:
    return [b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row
            if "callback_data" in b]


# --- tests --------------------------------------------------------------------------------------

def main() -> int:
    if STUBS:
        print("NOTE using test-only stand-ins for core modules:", ", ".join(STUBS))
    fake = FakeTelegram()
    store = AlertStore(str(EVENTS))
    conf = make_conf(fake.api_base)
    hub = FakeHub(store, conf)

    @check("token helpers")
    def _():
        assert tg.normalize_token("  bot" + TOKEN + "\n") == TOKEN
        assert tg.normalize_token(f'"{TOKEN}"') == TOKEN
        assert re.fullmatch(r"\d{6}", tg.new_pair_code())
        assert tg.deep_link("@MyYardBot", "123456") == "https://t.me/MyYardBot?start=123456"
        assert tg.parse_duration("1h") == 3600 and tg.parse_duration("30m") == 1800
        assert tg.parse_duration("8 hours") == 8 * 3600 and tg.parse_duration("soon") is None
        assert tg.match_camera([(1, "Front gate"), (2, "Driveway")], "front") == 1
        assert tg.match_camera([(1, "Front gate"), (2, "Driveway")], "2") == 2
        assert tg.match_camera([(1, "Front gate")], "garden") is None

    @check("check(): format, getMe, wrong token, no network")
    def _():
        r = tg.TelegramChannel.check({"api_base": fake.api_base}, {"token": "hello"})
        assert not r.ok and r.field == "token", r
        r = tg.TelegramChannel.check({"api_base": fake.api_base}, {"token": ""})
        assert not r.ok and r.field == "token"
        r = tg.TelegramChannel.check({"api_base": fake.api_base}, {"token": " bot" + TOKEN})
        assert r.ok and r.message == f"Found @{BOT}" and r.data["bot_username"] == BOT and r.data["bot_id"] == 42, r
        wrong = "987654321:" + "Z" * 35
        r = tg.TelegramChannel.check({"api_base": fake.api_base}, {"token": wrong})
        assert not r.ok and r.field == "token" and wrong not in r.message, r
        r = tg.TelegramChannel.check({"api_base": "http://127.0.0.1:9"}, {"token": TOKEN})
        assert not r.ok and "reach" in r.message.lower() and TOKEN not in r.message, r

    @check("pairing listener: code, group, old updates ignored")
    def _():
        fake.msg({"id": 3003, "type": "private", "first_name": "Old"}, "/start", age=600)
        lst = tg.PairingListener(TOKEN, "482913", api_base=fake.api_base, duration=20)
        lst.start()
        time.sleep(0.3)
        fake.msg(PRIVATE, "/start 482913")
        fake.msg(GROUP, "/start@" + BOT, user="Sam")
        fake.push({"my_chat_member": {"chat": {"id": -4004, "type": "supergroup", "title": "Neighbours"},
                                      "from": {"first_name": "Alex"}, "date": int(time.time()),
                                      "new_chat_member": {"status": "member"}}})
        end = time.time() + 5
        while len(lst.found()) < 3 and time.time() < end:
            time.sleep(0.05)
        found = lst.found()
        ids = {c["id"]: c for c in found}
        assert set(ids) == {1001, -2002, -4004}, found
        assert ids[1001]["with_code"] and ids[1001]["title"] == "Alex" and ids[1001]["type"] == "private"
        assert not ids[-2002]["with_code"] and ids[-2002]["title"] == "Family" and ids[-2002]["user"] == "Sam"
        assert 3003 not in ids
        assert 0 < lst.remaining() <= 20
        fake.wait_for("sendMessage", pred=lambda p: p.get("chat_id") == 1001 and "Found this chat" in p["text"])
        t0 = time.time()
        lst.stop()
        assert time.time() - t0 < 3 and not lst.running
        assert lst.remaining() == 0

    ch = tg.TelegramChannel(conf, hub)

    @check("incident_new -> sendPhoto with caption and keyboard to every chat")
    def _():
        inc, alert = make_incident(store, "warning", clip_offset=2.0)
        n0 = len(fake.called("sendPhoto"))
        res = ch.send(incident_event(ch, inc), ctx_for())
        assert res.ok, res
        calls = fake.called("sendPhoto")[n0:]
        assert [p["chat_id"] for p, _ in calls] == [1001, -2002], [p for p, _ in calls]
        p, files = calls[0]
        assert p["parse_mode"] == "HTML" and p["caption"].startswith("<b>Person near"), p["caption"]
        assert len(p["caption"]) <= 1024
        assert files["photo"][:2] == b"\xff\xd8" and len(files["photo"]) > 1000
        data = all_callback_data(p["reply_markup"])
        assert f"h:{inc}" in data and f"f:{inc}" in data and f"c:{alert}" in data and "s:1" in data, data
        assert all(len(d.encode()) <= 64 for d in data)
        assert [m["kind"] for m in res.ref["messages"]] == ["photo", "photo"]
        assert {r["chat"] for r in res.ref["root"]} == {1001, -2002}
        assert res.ms >= 0

    @check("pictures both -> media group + keyboard message; none -> text")
    def _():
        inc, _ = make_incident(store, "critical")
        res = ch.send(incident_event(ch, inc), ctx_for(pictures="both"))
        assert res.ok, res
        groups = fake.called("sendMediaGroup")[-2:]
        p, files = groups[0]
        assert len(p["media"]) == 2 and set(files) == {"p0", "p1"} and p["media"][0]["parse_mode"] == "HTML"
        kinds = [m["kind"] for m in res.ref["messages"]]
        assert kinds.count("group") == 4 and kinds.count("text") == 2, kinds
        texts = [c for c in fake.called("sendMessage") if c[0].get("reply_markup")][-2:]
        assert all(f"h:{inc}" in all_callback_data(c[0]["reply_markup"]) for c in texts)
        inc2, _ = make_incident(store, "warning")
        res = ch.send(incident_event(ch, inc2), ctx_for(pictures="none"))
        assert res.ok and [m["kind"] for m in res.ref["messages"]] == ["text", "text"], res

    @check("update with edit -> editMessageCaption ('not modified' tolerated)")
    def _():
        inc, _ = make_incident(store, "warning")
        first = ch.send(incident_event(ch, inc), ctx_for())
        fake.fail["editMessageCaption"] = [(400, {"ok": False, "error_code": 400, "description":
                                                  "Bad Request: message is not modified: specified new message "
                                                  "content and reply markup are exactly the same"})]
        n0 = len(fake.called("editMessageCaption"))
        ev = incident_event(ch, inc, kind="incident_update", alert_count=3)
        res = ch.send(ev, ctx_for(edit=True, thread_ref=first.ref))
        assert res.ok, res
        edits = fake.called("editMessageCaption")[n0:]
        assert len(edits) == 2 and {e[0]["chat_id"] for e in edits} == {1001, -2002}
        assert all(e[0]["parse_mode"] == "HTML" for e in edits)
        # escalation: new photo as a reply to the first message
        n1 = len(fake.called("sendPhoto"))
        ev = incident_event(ch, inc, kind="incident_update", severity="critical", escalated=True)
        res2 = ch.send(ev, ctx_for(thread_ref=res.ref))
        assert res2.ok
        replies = fake.called("sendPhoto")[n1:]
        roots = {r["chat"]: r["id"] for r in first.ref["root"]}
        assert [p["reply_parameters"]["message_id"] for p, _ in replies] == [roots[1001], roots[-2002]]
        assert res2.ref["root"] == first.ref["root"]
        # a bigger alert of the same incident swaps the photo
        n2 = len(fake.called("editMessageMedia"))
        ev = incident_event(ch, inc, kind="incident_update", severity="critical", alert_id="other-alert")
        ref = dict(first.ref, severity="warning")
        res3 = ch.send(ev, ctx_for(edit=True, thread_ref=ref))
        assert res3.ok and len(fake.called("editMessageMedia")) - n2 == 2 and res3.ref["alert_id"] == "other-alert"

    @check("incident_status event -> caption 'Handled by', keyboard Reopen")
    def _():
        inc, _ = make_incident(store, "warning")
        first = ch.send(incident_event(ch, inc), ctx_for())
        n0 = len(fake.called("editMessageCaption"))
        ev = incident_event(ch, inc, kind="incident_status", status="handled", status_by="Sam (Telegram)")
        res = ch.send(ev, ctx_for(edit=True, thread_ref=first.ref))
        assert res.ok, res
        p = fake.called("editMessageCaption")[n0][0]
        assert "Handled by Sam at" in p["caption"], p["caption"]
        assert all_callback_data(p["reply_markup"]) == [f"o:{inc}"], p["reply_markup"]
        res = ch.send(ev, ctx_for(thread_ref=None))
        assert res.ok and "No earlier message" in res.note

    @check("clip: sendVideo reply when ready; too large -> text")
    def _():
        inc, _ = make_incident(store, "warning", clip=str(CLIP), clip_offset=1.0)
        first = ch.send(incident_event(ch, inc), ctx_for())
        n0 = len(fake.called("sendVideo"))
        ev = incident_event(ch, inc, kind="incident_update")
        res = ch.send(ev, ctx_for(send_clip=True, thread_ref=first.ref))
        assert res.ok, res
        vids = fake.called("sendVideo")[n0:]
        assert len(vids) == 2 and vids[0][0]["supports_streaming"] is True and vids[0][1]["video"][:4] == b"\x00\x00\x00\x18"
        old = tg.VIDEO_LIMIT
        tg.VIDEO_LIMIT = 100
        try:
            ch.send(ev, ctx_for(send_clip=True, thread_ref=first.ref))
            fake.wait_for("sendMessage", pred=lambda p: "too large for Telegram" in p.get("text", ""))
        finally:
            tg.VIDEO_LIMIT = old

    @check("non-incident events (test, plate) and describe()")
    def _():
        res = ch.test()
        assert res.ok, res
        p = fake.called("sendPhoto")[-1][0]
        assert "YardGuard test" in p["caption"] and all_callback_data(p["reply_markup"]) == ["s:1"]
        ev = Event(kind="plate", ts=time.time(), title="Unknown plate AB-123-CD at Gate barrier", severity="warning",
                   plate="AB-123-CD", plate_known=False)
        res = ch.send(ev, ctx_for(pictures="none"))
        assert res.ok and fake.called("sendMessage")[-1][0].get("reply_markup") is None
        assert ch.describe() == f"@{BOT} to Alex, Family", ch.describe()

    @check("429: short retry_after waited out, long one returned to the hub")
    def _():
        inc, _ = make_incident(store, "warning")
        one = make_conf(fake.api_base, chats=[{"id": 1001, "title": "Alex", "type": "private", "enabled": True}])
        c1 = tg.TelegramChannel(one, hub)
        fake.fail["sendPhoto"] = [(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry "
                                         "after 1", "parameters": {"retry_after": 1}})]
        t0 = time.time()
        res = c1.send(incident_event(c1, inc), ctx_for())
        assert res.ok and time.time() - t0 >= 0.95, (res, time.time() - t0)
        fake.fail["sendPhoto"] = [(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                         "parameters": {"retry_after": 30}})]
        res = c1.send(incident_event(c1, inc), ctx_for())
        assert not res.ok and res.retry and res.retry_after == 30, res

    @check("401 permanent, 403 permanent, 5xx retry, partial delivery resumes per chat")
    def _():
        inc, _ = make_incident(store, "warning")
        fake.fail["sendPhoto"] = [(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})] * 2
        res = ch.send(incident_event(ch, inc), ctx_for())
        assert not res.ok and res.permanent and "no longer valid" in res.error and TOKEN not in res.error, res
        fake.fail["sendPhoto"] = [(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was "
                                                                                       "blocked by the user"})]
        res = ch.send(incident_event(ch, inc), ctx_for())   # private blocked, group fine
        assert res.ok and "Not sent to Alex" in res.note and ch.status == "problem", (res, ch.status)
        ch.status, ch.status_text = "ok", ""
        # 1st chat ok, 2nd 502 -> retry; the retry only sends to the 2nd chat
        fake.fail["sendPhoto"] = [(200, {"ok": True, "result": {"message_id": 7001}}),
                                  (502, {"ok": False, "error_code": 502, "description": "Bad Gateway"})]
        c = ctx_for()
        res = ch.send(incident_event(ch, inc), c)
        assert not res.ok and res.retry, res
        n0 = len(fake.called("sendPhoto"))
        c.attempt = 2
        res = ch.send(incident_event(ch, inc), c)
        assert res.ok and [p["chat_id"] for p, _ in fake.called("sendPhoto")[n0:]] == [-2002], res
        assert {m["chat"] for m in res.ref["messages"]} == {1001, -2002} and 7001 in [m["id"] for m in res.ref["messages"]]

    @check("group upgraded to supergroup -> update_chat action and resend")
    def _():
        inc, _ = make_incident(store, "warning")
        mconf = make_conf(fake.api_base, chats=[{"id": -5005, "title": "Old group", "type": "group", "enabled": True}])
        mhub = FakeHub(store, mconf)
        c = tg.TelegramChannel(mconf, mhub)
        fake.fail["sendPhoto"] = [(400, {"ok": False, "error_code": 400, "description": "Bad Request: group chat was "
                                         "upgraded to a supergroup chat", "parameters": {"migrate_to_chat_id": -1005005}})]
        res = c.send(incident_event(c, inc), ctx_for())
        assert res.ok and fake.called("sendPhoto")[-1][0]["chat_id"] == -1005005, res
        act = [a for a in mhub.actions if a.kind == "update_chat"]
        assert act and act[0].args == {"dest_id": mconf.id, "old_id": -5005, "new_id": -1005005}
        time.sleep(0.1)
        assert mconf.params["chats"][0]["id"] == -1005005

    @check("rate limit: 1 message per second per chat")
    def _():
        lim = tg._RateLimiter()
        lim.MIN_GAP = 1.0
        stop = threading.Event()
        t0 = time.time()
        for _ in range(3):
            lim.wait(1001, stop)
        assert time.time() - t0 >= 1.9
        lim.GROUP_PER_MIN, lim.MIN_GAP = 3, 0.0
        for _ in range(3):
            lim.wait(-2002, stop)
        assert lim.delay(-2002, time.time()) > 50

    # --- the poller: commands and buttons ----------------------------------------------------------
    @check("poller: start, setMyCommands, backlog button expired, old messages skipped")
    def _():
        fake.msg(PRIVATE, "/status", age=600)        # before the start: skipped
        fake.press(PRIVATE, "m:off")                  # pressed while YardGuard was not running: expired
        n0 = fake.outgoing()
        polls0 = fake.getupdates_count
        ch.start()
        fake.wait_for("setMyCommands")
        fake.wait_for("answerCallbackQuery", pred=lambda p: p.get("text") == tg.EXPIRED)
        end = time.time() + 5
        while fake.getupdates_count < polls0 + 2 and time.time() < end:   # backlog poll, then a long poll
            time.sleep(0.02)
        assert fake.getupdates_count >= polls0 + 2
        time.sleep(0.2)
        assert not any(a.kind == "set_monitoring" for a in hub.actions)
        assert not [p for p, _ in fake.called("sendMessage") if "Monitoring is on" in p.get("text", "")]
        assert ch.status == "ok", (ch.status, ch.status_text)
        assert int(hub.log.kv.get(f"tg_offset_{conf.id}", "0")) > 0
        assert fake.outgoing() == n0 + 1
        cmds = fake.called("setMyCommands")[-1][0]["commands"]
        assert [c["command"] for c in cmds] == ["status", "snapshot", "incidents", "arm", "disarm", "mute", "help"]

    @check("buttons: Mark handled / False alarm / Reopen")
    def _():
        inc, _ = make_incident(store, "warning")
        first = ch.send(incident_event(ch, inc), ctx_for())
        mid = [m["id"] for m in first.ref["messages"] if m["chat"] == 1001][0]
        n0 = len(fake.called("editMessageCaption"))
        fake.press(PRIVATE, f"h:{inc}", message_id=mid, user="Alex")
        fake.wait_for("answerCallbackQuery", pred=lambda p: p.get("callback_query_id", "").startswith("cq"))
        edits = fake.wait_for("editMessageCaption", count=n0 + 2)[n0:]
        assert store.get_incident(inc).status == "handled"
        assert {e[0]["chat_id"] for e in edits} == {1001, -2002}, edits   # both chats of this incident updated
        assert all("Handled by Alex at" in e[0]["caption"] for e in edits)
        assert all(all_callback_data(e[0]["reply_markup"]) == [f"o:{inc}"] for e in edits)
        act = [a for a in hub.actions if a.kind == "mark_incident"][-1]
        assert act.source == "Telegram" and act.user == "Alex" and act.args["status"] == "handled"
        n1 = len(fake.called("editMessageCaption"))
        fake.press(GROUP, f"f:{inc}", message_id=mid, user="Sam")
        fake.wait_for("editMessageCaption", count=n1 + 2)
        assert store.get_incident(inc).status == "false_alarm"
        assert "false alarm by Sam" in fake.called("editMessageCaption")[-1][0]["caption"]
        n2 = len(fake.called("editMessageCaption"))
        fake.press(PRIVATE, f"o:{inc}", message_id=mid)
        fake.wait_for("editMessageCaption", count=n2 + 2)
        assert store.get_incident(inc).status == "open"
        last = fake.called("editMessageCaption")[-1][0]
        assert "Reopened by Alex" in last["caption"] and f"h:{inc}" in all_callback_data(last["reply_markup"])

    @check("buttons: Open clip (ready / still saving), Snapshot now")
    def _():
        inc, alert = make_incident(store, "warning", clip=str(CLIP), clip_offset=1.0)
        n0 = len(fake.called("sendVideo"))
        fake.press(PRIVATE, f"c:{alert}", message_id=321)
        vid = fake.wait_for("sendVideo", count=n0 + 1)[-1][0]
        assert vid["chat_id"] == 1001 and vid["reply_parameters"]["message_id"] == 321
        inc2, alert2 = make_incident(store, "warning", clip="", clip_offset=3.0)
        fake.press(PRIVATE, f"c:{alert2}")
        fake.wait_for("answerCallbackQuery", pred=lambda p: "still being saved" in (p.get("text") or ""))
        n1 = len(fake.called("sendPhoto"))
        fake.press(GROUP, "s:2")
        photo = fake.wait_for("sendPhoto", count=n1 + 1)[-1]
        assert photo[0]["chat_id"] == -2002 and photo[0]["caption"] == "Driveway · now" and photo[1]["photo"][:2] == b"\xff\xd8"
        hub.snapshot_ok = False
        fake.press(GROUP, "s:1")
        fake.wait_for("sendMessage", pred=lambda p: p.get("text") == "No picture from Front gate right now.")
        hub.snapshot_ok = True

    @check("commands: /status /help /start /snapshot (with and without camera)")
    def _():
        fake.msg(PRIVATE, "/status")
        p = fake.wait_for("sendMessage", pred=lambda p: p.get("text", "").startswith("Monitoring is on"))[-1][0]
        assert "1 of 2 cameras watching" in p["text"] and "Front gate: Watching (1 person)" in p["text"], p["text"]
        assert "Driveway: No video" in p["text"] and "open incident" in p["text"]
        fake.msg(PRIVATE, "/help")
        fake.wait_for("sendMessage", pred=lambda p: "/disarm - Turn monitoring off" in p.get("text", ""))
        fake.msg(PRIVATE, "/start")
        fake.wait_for("sendMessage", pred=lambda p: p.get("text", "").startswith("Hi! This chat gets"))
        fake.msg(PRIVATE, "/snapshot")
        p = fake.wait_for("sendMessage", pred=lambda p: p.get("text") == "Which camera?")[-1][0]
        assert all_callback_data(p["reply_markup"]) == ["s:1", "s:2"]
        n0 = len(fake.called("sendPhoto"))
        fake.msg(PRIVATE, "/snapshot drive")
        p = fake.wait_for("sendPhoto", count=n0 + 1)[-1][0]
        assert p["caption"] == "Driveway · now" and p["chat_id"] == 1001
        fake.msg(PRIVATE, "/snapshot garden")
        fake.wait_for("sendMessage", pred=lambda p: p.get("text", "").startswith('No camera called "garden"'))

    @check("commands: /arm /disarm (+ confirm and cancel)")
    def _():
        fake.msg(PRIVATE, "/arm")
        fake.wait_for("sendMessage", pred=lambda p: p.get("text") == "Monitoring is on.")
        end = time.time() + 3
        while not any(a.kind == "set_monitoring" and a.args == {"on": True} for a in hub.actions) and time.time() < end:
            time.sleep(0.02)
        assert any(a.kind == "set_monitoring" and a.args == {"on": True} for a in hub.actions)
        fake.msg(GROUP, "/disarm")
        p = fake.wait_for("sendMessage", pred=lambda p: p.get("text", "").startswith("Turn monitoring off?"))[-1][0]
        assert all_callback_data(p["reply_markup"]) == ["m:off", "m:x"]
        assert hub.running
        fake.press(GROUP, "m:x", message_id=777, photo=False)
        fake.wait_for("editMessageText", pred=lambda p: p.get("text", "").startswith("Cancelled"))
        assert hub.running
        fake.press(GROUP, "m:off", message_id=778, photo=False)
        fake.wait_for("editMessageText", pred=lambda p: p.get("message_id") == 778 and "turning off" in p.get("text", ""))
        assert not hub.running
        a = [a for a in hub.actions if a.kind == "set_monitoring"][-1]
        assert a.args == {"on": False} and a.source == "Telegram"

    @check("commands: /incidents + incident button, /mute 1h, /mute off, /mute buttons")
    def _():
        fake.msg(PRIVATE, "/incidents")
        p = fake.wait_for("sendMessage", pred=lambda p: p.get("text", "").startswith("Open incidents:"))[-1][0]
        data = all_callback_data(p["reply_markup"])
        assert 1 <= len(data) <= 5 and all(d.startswith("i:inc-") for d in data), data
        n0 = len(fake.called("sendPhoto"))
        fake.press(PRIVATE, data[0])
        photo = fake.wait_for("sendPhoto", count=n0 + 1)[-1][0]
        assert f"h:{data[0][2:]}" in all_callback_data(photo["reply_markup"])
        fake.msg(PRIVATE, "/mute 1h")
        p = fake.wait_for("sendMessage", pred=lambda p: p.get("text", "").startswith("Messages paused until"))[-1][0]
        assert "Critical alerts still come through" in p["text"]
        assert 3500 < hub.snooze_until - time.time() <= 3600
        fake.msg(PRIVATE, "/mute off")
        fake.wait_for("sendMessage", pred=lambda p: p.get("text") == "Messages are on again.")
        assert hub.snooze_until == 0
        fake.msg(PRIVATE, "/mute")
        p = fake.wait_for("sendMessage", pred=lambda p: p.get("text", "").startswith("Pause messages for how long"))[-1][0]
        assert all_callback_data(p["reply_markup"]) == ["u:1h", "u:8h", "u:off"]
        fake.press(PRIVATE, "u:8h", message_id=901, photo=False)
        fake.wait_for("editMessageText", pred=lambda p: p.get("message_id") == 901 and "paused until" in p.get("text", ""))
        assert 8 * 3600 - 100 < hub.snooze_until - time.time() <= 8 * 3600

    @check("strangers ignored, @BotName suffix in groups")
    def _():
        time.sleep(0.3)
        n0 = fake.outgoing()
        acts = len(hub.actions)
        fake.msg(STRANGER, "/status")
        fake.msg(STRANGER, "/disarm")
        fake.press(STRANGER, "m:off")
        fake.msg(GROUP, "/status@OtherBot")
        fake.msg(GROUP, f"/status@{BOT}")       # handled in order: its answer means the ones above were seen
        fake.wait_for("sendMessage", pred=lambda p: p.get("chat_id") == -2002 and p.get("text", "").startswith("Monitoring"))
        time.sleep(0.2)
        assert fake.outgoing() == n0 + 1, [c[0] for c in fake.calls[-4:]]
        assert len(hub.actions) == acts + 1 and hub.actions[-1].kind == "status"
        assert "unknown chat" in LOG.getvalue()

    @check("allow_control off: /arm and buttons refused")
    def _():
        conf.params["allow_control"] = False
        try:
            acts = len(hub.actions)
            fake.msg(PRIVATE, "/arm")
            fake.wait_for("sendMessage", pred=lambda p: p.get("text") == tg.CONTROL_OFF)
            fake.press(PRIVATE, "h:inc-000000000000")
            fake.wait_for("answerCallbackQuery", pred=lambda p: p.get("text") == tg.CONTROL_OFF)
            n_off = len([p for p, _ in fake.called("sendMessage") if p.get("text") == tg.CONTROL_OFF])
            fake.msg(PRIVATE, "/mute 8h")
            fake.wait_for("sendMessage", count=n_off + 1, pred=lambda p: p.get("text") == tg.CONTROL_OFF)
            n_ans = len([p for p, _ in fake.called("answerCallbackQuery") if p.get("text") == tg.CONTROL_OFF])
            fake.press(PRIVATE, "u:8h")
            fake.wait_for("answerCallbackQuery", count=n_ans + 1, pred=lambda p: p.get("text") == tg.CONTROL_OFF)
            assert len(hub.actions) == acts
            assert not any(a.kind == "snooze" for a in hub.actions[acts:])
        finally:
            conf.params["allow_control"] = True

    @check("bot removed from a group -> chat disabled, status problem")
    def _():
        fake.push({"my_chat_member": {"chat": GROUP, "from": {"first_name": "Sam"}, "date": int(time.time()),
                                      "new_chat_member": {"status": "left"}}})
        end = time.time() + 3
        while ch.status != "problem" and time.time() < end:
            time.sleep(0.02)
        assert ch.status == "problem" and "Family" in ch.status_text, (ch.status, ch.status_text)
        assert [c for c in conf.params["chats"] if c["id"] == -2002][0]["enabled"] is False
        assert ch.describe() == f"@{BOT} to Alex"
        assert hub.status_notes or hub.destination_status.emitted
        conf.params["chats"][1]["enabled"] = True

    @check("pause/resume polling, stop() < 3 s during a long poll")
    def _():
        time.sleep(0.3)
        ch.pause_polling()
        time.sleep(0.4)
        n = fake.getupdates_count
        time.sleep(0.5)
        assert fake.getupdates_count == n, "paused poller still polls"
        ch.resume_polling()
        fake.wait_polls(1)
        time.sleep(0.5)            # now inside a 25 s long poll
        t0 = time.time()
        ch.stop()
        took = time.time() - t0
        assert took < 3, took
        assert not [t for t in threading.enumerate() if t.name.startswith("yg-telegram")], threading.enumerate()

    @check("409 conflict -> status problem; 401 at start -> problem")
    def _():
        c2conf = make_conf(fake.api_base)
        c2 = tg.TelegramChannel(c2conf, FakeHub(store, c2conf))
        fake.fail["getUpdates"] = [(409, {"ok": False, "error_code": 409, "description": "Conflict: terminated by "
                                          "other getUpdates request"})]
        c2.start()
        end = time.time() + 5
        while c2.status != "problem" and time.time() < end:
            time.sleep(0.02)
        assert c2.status == "problem" and "Another copy" in c2.status_text, (c2.status, c2.status_text)
        t0 = time.time()
        c2.stop()
        assert time.time() - t0 < 3
        bad = make_conf(fake.api_base)
        bad.set_secret("token", "987654321:" + "Q" * 35)
        c3 = tg.TelegramChannel(bad, FakeHub(store, bad))
        c3.start()
        end = time.time() + 5
        while c3.status != "problem" and time.time() < end:
            time.sleep(0.02)
        assert "no longer valid" in c3.status_text, c3.status_text
        c3.stop()
        empty = make_conf(fake.api_base)
        empty.set_secret("token", "")
        c4 = tg.TelegramChannel(empty, FakeHub(store, empty))
        c4.start()
        assert c4.status == "problem" and "missing" in c4.status_text
        res = c4.send(Event(kind="test", ts=time.time(), title="x"), ctx_for())
        assert not res.ok and res.permanent
        c4.stop()

    @check("no chats -> permanent error; unreachable Telegram -> retry")
    def _():
        nc = make_conf(fake.api_base, chats=[])
        res = tg.TelegramChannel(nc, hub).send(Event(kind="test", ts=time.time(), title="x"), ctx_for())
        assert not res.ok and res.permanent and "No Telegram chat" in res.error
        down = make_conf("http://127.0.0.1:9")
        res = tg.TelegramChannel(down, hub).send(Event(kind="test", ts=time.time(), title="x"), ctx_for())
        assert not res.ok and res.retry and TOKEN not in res.error, res

    @check("the token never appears in the log")
    def _():
        text = LOG.getvalue()
        assert TOKEN not in text and TOKEN.split(":")[1] not in text, "token leaked into the log"
        assert not [t for t in threading.enumerate() if t.name.startswith("yg-")], threading.enumerate()

    store.close()
    fake.close()
    hub_tests()
    print(f"\n{len(PASSED)} checks passed")
    return 0


def hub_tests() -> None:
    """The channel inside core's real IntegrationHub (Qt offscreen, shared FakeCtx)."""
    try:
        from yardguard.integrations import hub as hub_mod  # noqa: F401
        sys.path.insert(0, str(ROOT / "tests"))
        import fakes_integrations as fi
    except Exception as e:  # noqa: BLE001
        print(f"SKIP real-hub checks ({type(e).__name__}: {e})")
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    from yardguard.integrations import IntegrationHub
    from yardguard.integrations.base import new_integration
    app = QApplication.instance() or QApplication([])

    def pump(pred, timeout: float = 8.0, what: str = ""):
        end = time.time() + timeout
        while time.time() < end:
            app.processEvents()
            value = pred()
            if value:
                return value
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}")

    fake = FakeTelegram()
    ctx = fi.FakeCtx(str(OUT / "hub"))
    d = new_integration("telegram", "Telegram")
    d["params"].update({"api_base": fake.api_base, "bot_username": BOT, "chats": [
        {"id": PRIVATE["id"], "title": "Alex", "type": "private", "enabled": True},
        {"id": GROUP["id"], "title": "Family", "type": "group", "enabled": True}]})
    IntegrationConf(d).set_secret("token", TOKEN)
    ctx.cfg.settings.integrations = [d]
    hub = IntegrationHub(ctx)
    monitoring: list[bool] = []
    hub.set_handlers(set_monitoring=monitoring.append, open_incident=lambda _id: None)
    changed: list[int] = []
    hub.incidents_changed.connect(lambda: changed.append(1))

    @check("real hub: start, incident -> photo to both chats")
    def _():
        hub.start()
        pump(lambda: fake.called("setMyCommands"), what="setMyCommands")
        a = fi.make_alert(ctx.alert_store, "critical", "confirmed")
        ctx.raise_alert(a)
        calls = pump(lambda: len(fake.called("sendPhoto")) >= 2 and fake.called("sendPhoto"), what="sendPhoto")
        assert {p["chat_id"] for p, _ in calls} == {1001, -2002}
        p = calls[0][0]
        assert f"h:{a.incident_id}" in all_callback_data(p["reply_markup"]), p["reply_markup"]
        pump(lambda: hub.log.thread(d["id"], a.incident_id), what="thread saved")
        hub_tests.inc = a.incident_id
        hub_tests.mid = [m["id"] for m in hub.log.thread(d["id"], a.incident_id)["ref"]["messages"]
                         if m["chat"] == 1001][0]

    @check("real hub: Mark handled button -> store, incidents_changed, both chats edited")
    def _():
        inc, mid = hub_tests.inc, hub_tests.mid
        time.sleep(0.3)
        n0 = len(fake.called("editMessageCaption"))
        fake.press(PRIVATE, f"h:{inc}", message_id=mid)
        edits = pump(lambda: len(fake.called("editMessageCaption")) >= n0 + 2 and fake.called("editMessageCaption"),
                     what="edits")[n0:]
        assert ctx.alert_store.get_incident(inc).status == "handled" and changed
        assert {e[0]["chat_id"] for e in edits} == {1001, -2002}
        assert all("Handled by Alex at" in e[0]["caption"] for e in edits), edits[0][0]["caption"]

    @check("real hub: /status and /arm through hub.request()")
    def _():
        fake.msg(PRIVATE, "/status")
        p = pump(lambda: [p for p, _ in fake.called("sendMessage") if p.get("text", "").startswith("Monitoring is")],
                 what="/status answer")[-1]
        assert "Front gate" in p["text"] and "Driveway" in p["text"], p["text"]
        fake.msg(PRIVATE, "/arm")
        pump(lambda: monitoring == [True], what="set_monitoring handler")

    @check("real hub: shutdown leaves no yg- threads")
    def _():
        hub.shutdown()
        pump(lambda: not fi.yg_threads(), timeout=5, what="threads to end")
        ctx.close()
        fake.close()
        assert TOKEN not in LOG.getvalue(), "token leaked into the log"


if __name__ == "__main__":
    sys.exit(main())
