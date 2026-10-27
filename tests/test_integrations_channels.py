"""Checks for the generic channels: ntfy, Discord, email and webhook (yardguard/integrations/).

Run:  run.cmd tests\\test_integrations_channels.py
Everything talks to local fakes on 127.0.0.1 (an HTTP sink and a small SMTP server defined here);
nothing reaches ntfy.sh, Discord or a real mail server. Uses a synthetic config under
.cache\\tests\\integrations\\channels\\ (never the real config.json).

When the core builder's base.py / media.py are not there yet, small stand-ins written from the
SPEC are installed for this process only (they are not shipped).
"""
from __future__ import annotations

import base64
import email
import email.header
import email.policy
import hashlib
import hmac
import importlib
import io
import json
import logging
import os
import re
import socket
import socketserver
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "integrations" / "channels"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yardguard import config  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"

# Capture every log line (root + urllib3) to check that no secret is ever logged.
LOG_BUF = io.StringIO()
_handler = logging.StreamHandler(LOG_BUF)
_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.DEBUG)


# --- Stand-ins for core modules that may not exist yet (test process only) -------------------

def _install_standins() -> list[str]:
    """Uses the real core modules when they import; otherwise (core not finished, or
    YG_TEST_STANDINS=1) stand-ins for the package, model, base and media."""
    if os.environ.get("YG_TEST_STANDINS") != "1":
        try:
            for name in ("model", "base", "media"):
                importlib.import_module("yardguard.integrations." + name)
            return []
        except Exception as e:
            print(f"NOTE core modules not usable yet ({type(e).__name__}: {e})", flush=True)
    for name in [n for n in sys.modules if n.startswith("yardguard.integrations")]:
        del sys.modules[name]
    pkg = types.ModuleType("yardguard.integrations")
    pkg.__path__ = [str(ROOT / "yardguard" / "integrations")]
    sys.modules["yardguard.integrations"] = pkg
    sys.modules["yardguard.integrations.model"] = _standin_model()
    sys.modules["yardguard.integrations.base"] = _standin_base()
    sys.modules["yardguard.integrations.media"] = _standin_media()
    return ["package", "model", "base", "media"]


def _standin_model() -> types.ModuleType:
    import dataclasses
    import uuid

    m = types.ModuleType("yardguard.integrations.model")

    @dataclasses.dataclass
    class Event:
        kind: str
        ts: float
        title: str
        text: str = ""
        severity: str = "info"
        id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:12])
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
        labels: list = dataclasses.field(default_factory=list)
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
        extra: dict = dataclasses.field(default_factory=dict)
        escalated: bool = False

        def image_path(self, pictures):
            if pictures == "none":
                return ""
            if pictures == "snapshot":
                return self.snapshot_path or self.crop_path
            return self.crop_path or self.snapshot_path

    @dataclasses.dataclass
    class SendContext:
        delivery_id: str = ""
        pictures: str = "crop"
        silent: bool = False
        priority: int = 3
        thread_ref: dict | None = None
        edit: bool = False
        send_clip: bool = False
        web_base: str = ""
        attempt: int = 1

    @dataclasses.dataclass
    class SendResult:
        ok: bool
        ref: dict | None = None
        error: str = ""
        retry: bool = False
        retry_after: float | None = None
        permanent: bool = False
        ms: int = 0
        note: str = ""

    @dataclasses.dataclass
    class CheckResult:
        ok: bool
        message: str
        field: str = ""
        data: dict = dataclasses.field(default_factory=dict)

    for obj in (Event, SendContext, SendResult, CheckResult):
        obj.__module__ = m.__name__
        setattr(m, obj.__name__, obj)
    return m


def _standin_base() -> types.ModuleType:
    import requests
    from urllib.parse import urlsplit

    m = types.ModuleType("yardguard.integrations.base")

    def scrub(text, *secrets):
        text = str(text)
        for s in secrets:
            if s:
                text = text.replace(s, "***")
        text = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)
        text = re.sub(r"(https?://[^\s?]+)\?\S+", r"\1?***", text)
        return text[:300]

    def mask(secret, keep=3):
        return "****" + secret[-keep:] if secret else ""

    class ChannelError(Exception):
        def __init__(self, message, *, retry=False, permanent=False, retry_after=None, field=""):
            super().__init__(message)
            self.message, self.retry, self.permanent = message, retry, permanent
            self.retry_after, self.field = retry_after, field

    def http(method, url, *, secrets=(), timeout=15, session=None, **kw):
        try:
            r = (session or requests).request(method, url, timeout=timeout, **kw)
        except requests.Timeout:
            raise ChannelError(f"The server did not answer in {timeout:.0f} s", retry=True) from None
        except requests.RequestException:
            host = urlsplit(url).hostname or "the server"
            raise ChannelError(f"Could not connect to {host}. No internet connection?", retry=True) from None
        if r.status_code == 429:
            after = None
            try:
                after = float(r.headers.get("Retry-After") or r.json().get("retry_after"))
            except Exception:
                pass
            raise ChannelError("Too many messages; waiting", retry=True, retry_after=after)
        if r.status_code >= 500:
            raise ChannelError(f"The server had a problem (HTTP {r.status_code})", retry=True)
        return r

    class IntegrationConf:
        def __init__(self, d):
            self.d = d
            d.setdefault("secrets", {})
            d.setdefault("params", {})
            d.setdefault("routes", {})

        id = property(lambda s: s.d["id"])
        kind = property(lambda s: s.d["kind"])
        name = property(lambda s: s.d.get("name", ""))
        enabled = property(lambda s: bool(s.d.get("enabled", True)))
        params = property(lambda s: s.d["params"])
        routes = property(lambda s: s.d["routes"])

        def secret(self, key):
            v = self.d["secrets"].get(key)
            return config.decrypt(v) if v else ""

        def set_secret(self, key, value):
            if value:
                self.d["secrets"][key] = config.encrypt(value)
            else:
                self.d["secrets"].pop(key, None)

        def has_secret(self, key):
            return bool(self.d["secrets"].get(key))

        def secret_unreadable(self, key):
            return self.has_secret(key) and not self.secret(key)

    class Channel:
        KIND = ""
        CAPS = {}
        SECRET_KEYS = ()

        def __init__(self, conf, hub):
            self.conf, self.hub = conf, hub
            self.status, self.status_text = "ok", ""

        def start(self):
            pass

        def stop(self):
            pass

        def test(self):
            from yardguard.integrations.model import SendContext
            return self.send(self.hub.sample_event(), SendContext(delivery_id="test", pictures="crop"))

        def describe(self):
            return ""

    for name, obj in dict(scrub=scrub, mask=mask, ChannelError=ChannelError, http=http,
                          IntegrationConf=IntegrationConf, Channel=Channel).items():
        setattr(m, name, obj)
    return m


def _standin_media() -> types.ModuleType:
    from datetime import datetime

    m = types.ModuleType("yardguard.integrations.media")

    def prepare_jpeg(path, max_side=1280, quality=80, max_bytes=5_000_000):
        try:
            img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
        except (OSError, ValueError):
            return None
        if img is None:
            return None
        h, w = img.shape[:2]
        s = min(1.0, max_side / max(h, w))
        if s < 1:
            img = cv2.resize(img, (int(w * s), int(h * s)))
        q = quality
        while True:
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
            if len(buf) <= max_bytes or q <= 30:
                return buf.tobytes()
            q -= 10

    def clock(ts):
        return datetime.fromtimestamp(ts).strftime("%H:%M")

    def headline(e):
        return e.title

    def subline(e):
        word = {"critical": "Critical", "warning": "Warning", "info": "Info"}.get(e.severity, "Info")
        return " · ".join(x for x in (e.camera, clock(e.ts), word) if x)

    def ai_line(e):
        if e.verdict == "confirmed":
            return "AI: confirmed" + (f" ({e.ai_note})" if e.ai_note else "")
        return e.ai_note if e.ai_note == "not checked by AI" else ""

    def lines(e):
        out = [headline(e), subline(e)]
        if ai_line(e):
            out.append(ai_line(e))
        if e.text:
            out.extend(e.text.splitlines()[:3])
        return out

    def plain(e):
        return "\n".join(lines(e))

    for name, obj in dict(prepare_jpeg=prepare_jpeg, clock=clock, headline=headline, subline=subline,
                          ai_line=ai_line, lines=lines, plain=plain).items():
        setattr(m, name, obj)
    return m


STANDINS = _install_standins()

from yardguard.integrations import discord, emailer, ntfy, webhook  # noqa: E402
from yardguard.integrations.base import ChannelError, IntegrationConf  # noqa: E402
from yardguard.integrations.model import Event, SendContext  # noqa: E402

PASSED: list[str] = []
SECRETS_SEEN: set[str] = set()       # every secret used anywhere: none may show up in results or logs
TEXTS: list[str] = []                # every error/message string a channel returned


def ok(name: str) -> None:
    PASSED.append(name)
    print("PASS", name, flush=True)


def note_texts(*texts: str) -> None:
    TEXTS.extend(t for t in texts if t)


# --- Local fakes ---------------------------------------------------------------------------

class FakeHttpSink:
    """Records every request; answers with programmable responses (a queue, then a responder)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.queue: list[tuple[int, dict, bytes]] = []
        self.responder = lambda req: (200, {"Content-Type": "application/json"}, b'{"id": "m1"}')
        self.delay = 0.0
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = {"method": self.command, "path": self.path, "headers": dict(self.headers.items()),
                       "raw_headers": self.headers, "body": body}
                sink.requests.append(req)
                if sink.delay:
                    time.sleep(sink.delay)
                status, headers, data = sink.queue.pop(0) if sink.queue else sink.responder(req)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_HEAD = do_PATCH = _any

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-http", daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def reply(self, status: int, body: dict | bytes | None = None, headers: dict | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        self.queue.append((status, {"Content-Type": "application/json", **(headers or {})}, data))

    def reset(self) -> None:
        self.requests.clear()
        self.queue.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeSmtp:
    """A minimal SMTP server: EHLO/HELO, AUTH PLAIN/LOGIN, MAIL, RCPT, DATA, RSET, NOOP, QUIT.
    No STARTTLS (tests use security "none"). Stores (mail_from, rcpts, raw bytes) per message."""

    def __init__(self, user: str = "me@example.com", password: str = "app-pass") -> None:
        self.user, self.password = user, password
        self.messages: list[tuple[str, list[str], bytes]] = []
        self.logins: list[str] = []
        self.reject_rcpt: set[str] = set()
        self.auth_fail = False
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def send(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                try:
                    self.session()
                except OSError:     # the client hung up (e.g. it tried TLS on this plain server)
                    pass

            def session(self):
                self.send("220 fake ESMTP")
                mail_from, rcpts = "", []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    cmd = line.split(" ", 1)[0].upper()
                    arg = line[len(cmd):].strip()
                    if cmd in ("EHLO", "HELO"):
                        self.send("250-fake")
                        self.send("250-SIZE 20000000")
                        self.send("250 AUTH PLAIN LOGIN")
                    elif cmd == "AUTH":
                        parts = arg.split()
                        mech = parts[0].upper()
                        if mech == "PLAIN":
                            blob = parts[1] if len(parts) > 1 else None
                            if blob is None:
                                self.send("334 ")
                                blob = self.rfile.readline().decode().strip()
                            _z, u, p = base64.b64decode(blob).decode().split("\0")
                        else:
                            if len(parts) > 1:
                                u = base64.b64decode(parts[1]).decode()
                            else:
                                self.send("334 VXNlcm5hbWU6")
                                u = base64.b64decode(self.rfile.readline().strip()).decode()
                            self.send("334 UGFzc3dvcmQ6")
                            p = base64.b64decode(self.rfile.readline().strip()).decode()
                        if fake.auth_fail or u != fake.user or p != fake.password:
                            self.send("535 5.7.8 Username and Password not accepted. " + p)
                        else:
                            fake.logins.append(u)
                            self.send("235 2.7.0 Accepted")
                    elif cmd == "MAIL":
                        mail_from, rcpts = re.sub(r"(?i)^FROM:", "", arg).split()[0].strip("<>"), []
                        self.send("250 OK")
                    elif cmd == "RCPT":
                        addr = re.sub(r"(?i)^TO:", "", arg).split()[0].strip("<>")
                        if addr in fake.reject_rcpt:
                            self.send("550 5.1.1 No such user")
                        else:
                            rcpts.append(addr)
                            self.send("250 OK")
                    elif cmd == "DATA":
                        self.send("354 End data with <CR><LF>.<CR><LF>")
                        buf = []
                        while True:
                            ln = self.rfile.readline()
                            if ln in (b".\r\n", b".\n", b""):
                                break
                            buf.append(ln[1:] if ln.startswith(b"..") else ln)
                        fake.messages.append((mail_from, list(rcpts), b"".join(buf)))
                        self.send("250 OK queued")
                    elif cmd in ("RSET", "NOOP"):
                        self.send("250 OK")
                    elif cmd == "QUIT":
                        self.send("221 Bye")
                        return
                    else:
                        self.send("502 Not implemented")

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-smtp", daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- Test data -------------------------------------------------------------------------------

MEDIA = OUT / "media"
MEDIA.mkdir(exist_ok=True)


def make_jpeg(name: str, w: int, h: int, noise: bool = False) -> str:
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (40, 70, 50)
    if noise:
        img = np.random.default_rng(1).integers(0, 255, (h, w, 3), dtype=np.uint8)
    cv2.putText(img, "YardGuard", (20, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    path = MEDIA / name
    ok_, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    buf.tofile(str(path))
    return str(path)


CROP = make_jpeg("crop.jpg", 400, 300)
SNAP = make_jpeg("snapshot_ქართ.jpg", 2560, 1440, noise=True)    # big and with a non-ASCII name
CLIP = str(MEDIA / "clip.mp4")
Path(CLIP).write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(200_000))
BIG_CLIP = str(MEDIA / "big.mp4")
with open(BIG_CLIP, "wb") as f:
    f.truncate(9_000_000)


def incident(**kw) -> Event:
    d = dict(kind="incident_new", ts=time.time(), title="Person near the car at night", severity="critical",
             site_id="home", site_name="Home", channel=1, camera="Front gate", incident_id="inc-0123456789ab",
             alert_id="a1", alert_count=2, verdict="confirmed", ai_note="hand on door handle", status="open",
             labels=["person"], snapshot_path=SNAP, crop_path=CROP, web_path="#/incident/inc-0123456789ab")
    d.update(kw)
    return Event(**d)


class StubHub:
    def sample_event(self) -> Event:
        return Event(kind="test", ts=time.time(), title="YardGuard test", camera="Front gate", channel=1,
                     text="If you can read this, alerts will arrive here.", crop_path=CROP, snapshot_path=SNAP)

    def web_base(self) -> str:
        return ""


HUB = StubHub()


def make_conf(kind: str, params: dict, secrets: dict[str, str]) -> IntegrationConf:
    d = {"id": f"{kind[:2]}-{os.urandom(4).hex()}", "kind": kind, "name": kind.title(), "enabled": True,
         "created": time.time(), "secrets": {}, "params": dict(params), "routes": {}}
    conf = IntegrationConf(d)
    for k, v in secrets.items():
        conf.set_secret(k, v)
        SECRETS_SEEN.add(v)
    for v in d["secrets"].values():
        for s in secrets.values():
            assert s not in v, "secrets must be stored encrypted"
    return conf


def ctx(**kw) -> SendContext:
    d = dict(delivery_id="dl-test", pictures="crop", silent=False, priority=5, thread_ref=None, edit=False,
             send_clip=False, web_base="", attempt=1)
    d.update(kw)
    return SendContext(**d)


def no_secret(*texts: str) -> None:
    for t in texts:
        for s in SECRETS_SEEN:
            assert s not in (t or ""), f"secret leaked into: {t!r}"


# --- ntfy ------------------------------------------------------------------------------------

def test_ntfy(sink: FakeHttpSink) -> None:
    topic = ntfy.random_topic()
    assert re.fullmatch(r"yardguard-[a-z0-9]{20}", topic) and ntfy.random_topic() != topic
    assert ntfy.subscribe_url("ntfy.sh/", topic) == f"https://ntfy.sh/{topic}"
    assert ntfy.normalize_server("") == "https://ntfy.sh"
    ok("ntfy: random topic, subscribe URL, server normalisation")

    token = "tk_" + "s3cr3tT0ken" * 3
    conf = make_conf("ntfy", {"server": sink.base, "topic": topic, "click_url": True}, {"token": token})
    ch = ntfy.NtfyChannel(conf, HUB)

    # Picture: one PUT with the JPEG body and the texts in headers.
    sink.reset()
    r = ch.send(incident(), ctx(web_base="https://pc.tail1234.ts.net", priority=5))
    assert r.ok, r.error
    req = sink.requests[-1]
    assert req["method"] == "PUT" and req["path"] == f"/{topic}"
    h = req["headers"]
    assert req["body"][:2] == b"\xff\xd8" and len(req["body"]) < 4_000_000
    img = cv2.imdecode(np.frombuffer(req["body"], np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and max(img.shape[:2]) <= 1280
    assert h["Filename"] == "yardguard.jpg" and h["Priority"] == "5" and h["Tags"] == "rotating_light"
    assert h["Authorization"] == f"Bearer {token}"
    assert h["Click"] == "https://pc.tail1234.ts.net/#/incident/inc-0123456789ab"
    assert "Person near the car at night" in decode_2047(h["Title"])
    msg = decode_2047(h["Message"]).replace("\\n", "\n")
    assert "Front gate" in msg and "\r" not in h["Message"] and "\n" not in h["Message"]
    ok("ntfy: PUT with picture, priority, tags, bearer token, click URL")

    # Non-ASCII title -> RFC 2047 encoded words that decode back exactly.
    sink.reset()
    title = "Persona vicino all'auto · ღამით ქართული ტექსტი ძალიან გრძელი სათაური"
    r = ch.send(incident(title=title, severity="warning", camera="Ворота"), ctx(priority=4))
    assert r.ok, r.error
    raw = sink.requests[-1]["headers"]["Title"]
    assert raw.isascii() and raw.startswith("=?UTF-8?B?")
    assert all(len(w) <= 75 for w in raw.split(" "))
    assert decode_2047(raw) == title, decode_2047(raw)
    assert sink.requests[-1]["headers"]["Tags"] == "warning"
    assert "Ворота" in decode_2047(sink.requests[-1]["headers"]["Message"])
    ok("ntfy: non-ASCII title/message as RFC 2047 encoded words (<= 75 chars each)")

    # No picture: JSON publish to the server root; silent -> priority 2; no click without web base.
    sink.reset()
    r = ch.send(incident(severity="info"), ctx(pictures="none", silent=True, priority=2))
    assert r.ok, r.error
    req = sink.requests[-1]
    body = json.loads(req["body"])
    assert req["method"] == "POST" and req["path"] == "/"
    assert body["topic"] == topic and body["priority"] == 2 and body["tags"] == ["information_source"]
    assert body["title"].startswith("Person near") and "click" not in body
    assert req["headers"]["Authorization"] == f"Bearer {token}"
    sink.reset()
    r = ch.send(incident(crop_path="", snapshot_path=""), ctx(priority=4))      # no picture files at all
    assert r.ok and sink.requests[-1]["method"] == "POST"
    ok("ntfy: JSON publish without picture, silent priority mapping")

    # Without token: no Authorization header.
    conf2 = make_conf("ntfy", {"server": sink.base, "topic": topic, "click_url": False}, {})
    ch2 = ntfy.NtfyChannel(conf2, HUB)
    sink.reset()
    assert ch2.send(incident(), ctx(web_base="https://x.ts.net")).ok
    assert "Authorization" not in sink.requests[-1]["headers"] and "Click" not in sink.requests[-1]["headers"]
    ok("ntfy: no token -> no Authorization; click_url off -> no Click")

    # Errors: 401 permanent with plain words, 413 picture -> text only, 500 -> retry, 429 -> retry_after.
    sink.reset()
    sink.reply(401, {"error": f"unauthorized {token}"})
    r = ch.send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.permanent and not r.retry and "token" in r.error.lower(), r
    sink.reply(413, {"error": "too large"})
    r = ch.send(incident(), ctx())
    assert r.ok and "text only" in r.note and sink.requests[-1]["method"] == "POST"
    sink.reply(500, b"oops")
    r = ch.send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.retry and not r.permanent
    sink.reply(429, {"error": "slow down"}, {"Retry-After": "17"})
    r = ch.send(incident(), ctx())
    assert not r.ok and r.retry and r.retry_after == 17, r
    r = ch.send(incident(), ctx(edit=True))
    assert r.ok
    ok("ntfy: 401 permanent, 413 -> text only, 500 retry, 429 retry_after, edit is a no-op")

    # Network down -> retry with plain words; send never raises.
    conf3 = make_conf("ntfy", {"server": f"http://127.0.0.1:{free_port()}", "topic": topic}, {"token": token})
    r = ntfy.NtfyChannel(conf3, HUB).send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.retry and r.error
    conf4 = make_conf("ntfy", {"server": sink.base, "topic": "bad topic!"}, {})
    r = ntfy.NtfyChannel(conf4, HUB).send(incident(), ctx())
    assert not r.ok and r.permanent
    ok("ntfy: unreachable server -> retry; bad topic -> permanent")

    # An unexpected exception that carries the token: scrubbed result, log names only the type.
    orig = ntfy.media.prepare_jpeg

    def boom(*a, **k):
        raise RuntimeError(f"boom while using {token}")

    ntfy.media.prepare_jpeg = boom
    try:
        r = ch.send(incident(), ctx())
    finally:
        ntfy.media.prepare_jpeg = orig
    note_texts(r.error)
    assert not r.ok and r.retry and "boom" in r.error and token not in r.error, r
    assert "ntfy: unexpected error (RuntimeError)" in LOG_BUF.getvalue()
    assert logging.getLogger("urllib3").getEffectiveLevel() >= logging.WARNING
    ok("ntfy: unexpected exception -> scrubbed result, only the type is logged; urllib3 quiet")

    # check(): health, token accepted / refused, fallback HEAD, bad input.
    def responder(req):
        p = req["path"]
        if p == "/v1/health":
            return 200, {"Content-Type": "application/json"}, b'{"healthy":true}'
        if p == "/v1/account":
            good = req["headers"].get("Authorization") == f"Bearer {token}"
            return (200, {}, b'{"username":"alex"}') if good else (401, {}, b'{"code":40101}')
        return 200, {}, b"{}"

    sink.reset()
    sink.responder = responder
    c = ntfy.NtfyChannel.check({"server": sink.base, "topic": topic}, {"token": token})
    assert c.ok and "alex" in c.message, c
    c = ntfy.NtfyChannel.check({"server": sink.base, "topic": topic}, {"token": "wrong-" + token[::-1]})
    SECRETS_SEEN.add("wrong-" + token[::-1])
    note_texts(c.message)
    assert not c.ok and c.field == "token" and "not accepted" in c.message
    c = ntfy.NtfyChannel.check({"server": sink.base, "topic": topic}, {})
    assert c.ok and not any(r["path"] == "/v1/account" for r in sink.requests[-1:])
    sink.responder = lambda req: (404, {}, b"") if req["path"] == "/v1/health" else (200, {}, b"")
    c = ntfy.NtfyChannel.check({"server": sink.base, "topic": topic}, {})
    assert c.ok and sink.requests[-1]["method"] == "HEAD", c
    c = ntfy.NtfyChannel.check({"server": sink.base, "topic": "no spaces allowed"}, {})
    assert not c.ok and c.field == "topic"
    c = ntfy.NtfyChannel.check({"server": "ftp://x", "topic": topic}, {})
    assert not c.ok and c.field == "server"
    c = ntfy.NtfyChannel.check({"server": f"http://127.0.0.1:{free_port()}", "topic": topic}, {"token": token})
    note_texts(c.message)
    assert not c.ok and c.field == "server"
    sink.responder = lambda req: (200, {"Content-Type": "application/json"}, b'{"id": "m1"}')
    ok("ntfy: check() health / token accepted / token refused (field token) / HEAD fallback / bad input")

    d = ch.describe()
    assert topic not in d and topic[:13] in d and "127.0.0.1" in d and token not in d
    r = ch.test()
    assert r.ok, r.error
    ok("ntfy: describe() shortens the topic; test() sends the hub's sample")


def decode_2047(value: str) -> str:
    return str(email.header.make_header(email.header.decode_header(value)))


# --- Discord -----------------------------------------------------------------------------------

def test_discord(sink: FakeHttpSink) -> None:
    good = "https://discord.com/api/webhooks/123456789012345678/AbCdEf-gh_IJKLmnop1234567890"
    assert discord.valid_url(good)
    assert discord.valid_url(good.replace("discord.com", "discordapp.com"))
    assert discord.valid_url(" <" + good + "> ")
    assert discord.valid_url(good + "?thread_id=42")
    assert discord.valid_url(good.replace("https://", "https://canary."))
    for bad in ("http://discord.com/api/webhooks/1/abc", "https://evil.com/api/webhooks/1/abc",
                "https://discord.com/api/webhooks/abc/def", "https://discord.com.evil.com/api/webhooks/1/x",
                "", "discord.com/api/webhooks/1/abc"):
        assert not discord.valid_url(bad), bad
    c = discord.DiscordChannel.check({}, {"url": "https://example.com/hook"})
    assert not c.ok and c.field == "url"
    ok("discord: webhook URL validation")

    wh_token = "WebhookT0ken-" + "x" * 40
    url = f"{sink.base}/api/webhooks/987654321/{wh_token}"
    conf = make_conf("discord", {"username": "YardGuard"}, {"url": url})
    ch = discord.DiscordChannel(conf, HUB)

    sink.reset()
    sink.responder = lambda req: (200, {"Content-Type": "application/json"}, b'{"id": "111222333"}')
    r = ch.send(incident(), ctx(web_base="https://pc.tail1234.ts.net"))
    assert r.ok and r.ref == {"message_id": "111222333"}, r
    req = sink.requests[-1]
    assert req["method"] == "POST" and "wait=true" in req["path"]
    parts = multipart(req)
    payload = json.loads(parts["payload_json"][1])
    emb = payload["embeds"][0]
    assert payload["username"] == "YardGuard" and payload["allowed_mentions"] == {"parse": []}
    assert emb["title"].startswith("Person near the car") and emb["color"] == discord.COLORS["critical"]
    assert emb["image"]["url"] == "attachment://yardguard.jpg" and emb["timestamp"].endswith("+00:00")
    assert emb["url"].startswith("https://pc.tail1234.ts.net/")
    assert parts["files[0]"][0] == "yardguard.jpg" and parts["files[0]"][1][:2] == b"\xff\xd8"
    ok("discord: multipart payload_json + files[0] picture, embed colour/time/link")

    # Clip: second post when small enough; too large -> note only.
    sink.reset()
    r = ch.send(incident(clip_path=CLIP), ctx(send_clip=True))
    assert r.ok and len(sink.requests) == 2, (r, len(sink.requests))
    p2 = multipart(sink.requests[1])
    assert p2["files[0]"][0] == "yardguard.mp4" and len(p2["files[0]"][1]) == os.path.getsize(CLIP)
    sink.reset()
    r = ch.send(incident(clip_path=BIG_CLIP), ctx(send_clip=True))
    assert r.ok and len(sink.requests) == 1 and "too large" in r.note, r
    sink.reset()
    r = ch.send(incident(clip_path=CLIP), ctx(send_clip=False, pictures="none"))
    assert r.ok and len(sink.requests) == 1 and "files[0]" not in multipart(sink.requests[0])
    ok("discord: clip as a second post, 8 MB cap, text-only when pictures none")

    sink.reset()
    sink.reply(429, {"message": "You are being rate limited.", "retry_after": 2.5, "global": False})
    r = ch.send(incident(), ctx())
    assert not r.ok and r.retry and r.retry_after == 2.5, r
    sink.reply(404, {"message": "Unknown Webhook", "code": 10015})
    r = ch.send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.permanent and "deleted" in r.error, r
    sink.reply(401, {"message": "Invalid Webhook Token"})
    r = ch.send(incident(), ctx())
    assert not r.ok and r.permanent
    sink.reply(413, {"message": "Request entity too large"})
    r = ch.send(incident(), ctx())
    assert r.ok and "text only" in r.note
    sink.reply(400, {"message": f"Invalid Form Body {wh_token} {url}"})
    r = ch.send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.permanent and wh_token not in r.error and "***" in r.error, r.error
    sink.reply(502, b"bad gateway")
    r = ch.send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.retry
    ok("discord: 429 retry_after, 404/401 permanent, 413 text only, 400 scrubbed, 5xx retry")

    # Network error text never shows the URL or token.
    conf2 = make_conf("discord", {}, {"url": f"http://127.0.0.1:{free_port()}/api/webhooks/1/{wh_token}"})
    r = discord.DiscordChannel(conf2, HUB).send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.retry and wh_token not in r.error
    ok("discord: unreachable -> retry, no token in the error")

    # check(): GET the webhook; name in data. (Test hosts allowed by patching the validator.)
    orig = discord.valid_url
    discord.valid_url = lambda u: True
    try:
        sink.reset()
        sink.responder = lambda req: (200, {"Content-Type": "application/json"},
                                      json.dumps({"name": "Yard alerts", "channel_id": "5",
                                                  "token": wh_token}).encode())
        c = discord.DiscordChannel.check({}, {"url": url})
        assert c.ok and c.data == {"webhook_name": "Yard alerts"} and sink.requests[-1]["method"] == "GET", c
        assert wh_token not in c.message
        sink.reply(404, {"message": "Unknown Webhook"})
        c = discord.DiscordChannel.check({}, {"url": url})
        note_texts(c.message)
        assert not c.ok and c.field == "url" and "deleted" in c.message
    finally:
        discord.valid_url = orig
        sink.responder = lambda req: (200, {"Content-Type": "application/json"}, b'{"id": "m1"}')
    conf.params["webhook_name"] = "Yard alerts"
    assert ch.describe() == 'Webhook "Yard alerts"' and wh_token not in ch.describe()
    assert ch.send(incident(), ctx(edit=True)).ok
    ok("discord: check() reads the webhook name; 404 -> deleted; describe() has no secret")


def multipart(req: dict) -> dict[str, tuple[str, bytes]]:
    """{field: (filename, data)} of a multipart/form-data request."""
    ctype = req["headers"]["Content-Type"]
    msg = email.message_from_bytes(f"Content-Type: {ctype}\r\n\r\n".encode() + req["body"],
                                   policy=email.policy.HTTP)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        out[name] = (part.get_filename() or "", part.get_payload(decode=True))
    return out


# --- Email -------------------------------------------------------------------------------------

def test_email() -> None:
    password = "app-pass-" + "Zq9" * 5
    smtp = FakeSmtp(user="me@example.com", password=password)
    try:
        base = {"preset": "other", "host": "127.0.0.1", "port": smtp.port, "security": "none",
                "username": "me@example.com", "sender": "me@example.com",
                "to": ["me@example.com", "wife@example.org"], "subject_prefix": "YardGuard"}

        # Presets and helpers.
        p = emailer.apply_preset({}, "gmail")
        assert (p["host"], p["port"], p["security"]) == ("smtp.gmail.com", 587, "starttls")
        assert emailer.apply_preset({}, "yahoo")["security"] == "ssl"
        assert emailer.guess_preset("x@googlemail.com") == "gmail" and emailer.guess_preset("a@hotmail.com") == "outlook"
        assert emailer.parse_recipients("a@x.com; b@y.org, a@x.com ") == ["a@x.com", "b@y.org"]
        ok("email: presets, provider guess, recipient parsing")

        # check(): login works, sends nothing.
        c = emailer.EmailChannel.check(base, {"password": password})
        assert c.ok and smtp.logins == ["me@example.com"] and not smtp.messages, c
        c = emailer.EmailChannel.check(base, {"password": "wrong-" + password})
        SECRETS_SEEN.add("wrong-" + password)
        note_texts(c.message)
        assert not c.ok and c.field == "password" and "app password" in c.message, c
        c = emailer.EmailChannel.check({**base, "to": ["not-an-address"]}, {"password": password})
        assert not c.ok and c.field == "to"
        c = emailer.EmailChannel.check({**base, "sender": "me at home"}, {"password": password})
        assert not c.ok and c.field == "sender"
        c = emailer.EmailChannel.check({**base, "sender": "", "username": ""}, {"password": password})
        assert not c.ok and c.field == "sender"
        c = emailer.EmailChannel.check({**base, "username": "me@example.com"}, {"password": ""})
        assert not c.ok and c.field == "password"
        ok("email: check() logs in without sending; 535 -> 'Password not accepted' on field password")

        # Unreachable / wrong security / unknown host messages.
        dead = free_port()
        t0 = time.monotonic()
        c = emailer.EmailChannel.check({**base, "port": dead}, {"password": password})
        note_texts(c.message)
        assert not c.ok and f"127.0.0.1:{dead}" in c.message and c.field == "port", c
        assert time.monotonic() - t0 < 20
        c = emailer.EmailChannel.check({**base, "security": "ssl"}, {"password": password})
        note_texts(c.message)
        assert not c.ok and "Secure connection failed" in c.message and c.field == "security", c
        c = emailer.EmailChannel.check({**base, "security": "starttls"}, {"password": password})
        assert not c.ok and c.field == "security", c
        c = emailer.EmailChannel.check({**base, "host": "no-such-host.invalid"}, {"password": password})
        note_texts(c.message)
        assert not c.ok and c.field == "host" and "no-such-host.invalid" in c.message, c
        ok("email: unreachable port / SSL mismatch / no STARTTLS / unknown host -> plain words + field")

        conf = make_conf("email", base, {"password": password})
        ch = emailer.EmailChannel(conf, HUB)

        # Incident mail: text + HTML with inline CID picture; snapshot attached with "both".
        smtp.messages.clear()
        ev = incident(title="Person near the car at night", camera="Front gate")
        r = ch.send(ev, ctx(pictures="both", web_base="https://pc.tail1234.ts.net"))
        assert r.ok, r.error
        assert r.ref and r.ref["message_id"].startswith("<") and r.ref["subject"]
        mail_from, rcpts, raw = smtp.messages[-1]
        assert mail_from == "me@example.com" and rcpts == ["me@example.com", "wife@example.org"]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        subj = str(msg["Subject"])
        assert re.fullmatch(r"\[YardGuard\] Critical: Person near the car at night - Front gate \d\d:\d\d", subj), subj
        assert msg["X-Priority"] == "1"
        text = msg.get_body(("plain",)).get_content()
        htmlp = msg.get_body(("html",))
        html = htmlp.get_content()
        assert "Person near the car at night" in text and "Open in YardGuard: https://pc.tail1234.ts.net/" in text
        m = re.search(r'src="cid:([^"]+)"', html)
        assert m, html
        related = [p for p in msg.walk() if p.get("Content-ID") == f"<{m.group(1)}>"]
        assert related and related[0].get_content_type() == "image/jpeg"
        assert related[0].get_payload(decode=True)[:2] == b"\xff\xd8"
        atts = [p for p in msg.iter_attachments() if p.get_content_type() == "image/jpeg"]
        assert len(atts) == 1 and atts[0].get_filename() == "picture.jpg"
        assert len(raw) < emailer.MAX_UPLOAD
        ok("email: subject format, text + HTML parts, inline CID close-up, snapshot attached (both)")

        # crop only -> no attachment; none -> no image at all; HTML escaping.
        smtp.messages.clear()
        r = ch.send(incident(title="<b>Car</b> & co", severity="warning"), ctx(pictures="crop"))
        assert r.ok
        msg = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
        assert not list(msg.iter_attachments()) or all(
            p.get("Content-ID") for p in msg.walk() if p.get_content_type() == "image/jpeg")
        assert "&lt;b&gt;Car&lt;/b&gt; &amp; co" in msg.get_body(("html",)).get_content()
        assert str(msg["Subject"]).startswith("[YardGuard] Warning: <b>Car</b> & co")
        r = ch.send(incident(), ctx(pictures="none"))
        msg = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
        assert not [p for p in msg.walk() if p.get_content_type() == "image/jpeg"]
        ok("email: crop inline only, none without pictures, HTML escaped")

        # Escalation: reply in the same conversation.
        smtp.messages.clear()
        r = ch.send(incident(kind="incident_update", escalated=True),
                    ctx(thread_ref={"message_id": "<first@example.com>", "subject": "[YardGuard] Warning: X"}))
        msg = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
        assert msg["In-Reply-To"] == "<first@example.com>" and str(msg["Subject"]) == "Re: [YardGuard] Warning: X"
        assert ch.send(incident(), ctx(edit=True)).ok and len(smtp.messages) == 1
        ok("email: escalation is a 'Re:' reply with In-Reply-To; edit is a no-op")

        # Summary as an HTML table; test event; refused recipient; describe.
        smtp.messages.clear()
        summ = Event(kind="summary", ts=time.time(), title="Daily summary",
                     text="Incidents: 3 (1 critical)\nFalse alarms: 2\nAll cameras online.", crop_path=CROP)
        assert ch.send(summ, ctx(pictures="crop")).ok
        msg = email.message_from_bytes(smtp.messages[-1][2], policy=email.policy.default)
        html = msg.get_body(("html",)).get_content()
        assert "<table" in html and ">Incidents</td>" in html and "3 (1 critical)" in html
        assert str(msg["Subject"]) == "[YardGuard] Daily summary"
        assert ch.test().ok
        smtp.reject_rcpt = {"me@example.com", "wife@example.org"}
        r = ch.send(incident(), ctx())
        note_texts(r.error)
        assert not r.ok and r.permanent and ("wife@example.org" in r.error or "me@example.com" in r.error), r
        smtp.reject_rcpt = {"wife@example.org"}
        r = ch.send(incident(), ctx())
        assert r.ok and "wife@example.org" in r.note
        smtp.reject_rcpt = set()
        smtp.auth_fail = True
        r = ch.send(incident(), ctx())
        note_texts(r.error)
        assert not r.ok and r.permanent and "Password not accepted" in r.error and password not in r.error
        smtp.auth_fail = False
        d = ch.describe()
        assert d == "To me@example.com (+1)" and password not in d, d
        ok("email: summary HTML table, test mail, refused recipients, auth failure, describe()")

        # Server gone -> retry; bad stored config -> permanent.
        conf2 = make_conf("email", {**base, "port": free_port()}, {"password": password})
        r = emailer.EmailChannel(conf2, HUB).send(incident(), ctx())
        note_texts(r.error)
        assert not r.ok and r.retry and not r.permanent
        conf3 = make_conf("email", {**base, "to": ["bad"]}, {"password": password})
        r = emailer.EmailChannel(conf3, HUB).send(incident(), ctx())
        assert not r.ok and r.permanent
        ok("email: server down -> retry; invalid recipients -> permanent")
    finally:
        smtp.close()


# --- Webhook -----------------------------------------------------------------------------------

def test_webhook(sink: FakeHttpSink) -> None:
    key = webhook.new_secret()
    assert len(key) >= 40 and webhook.new_secret() != key
    url = f"{sink.base}/hooks/yardguard?apikey=Sup3rS3cretQuery"
    SECRETS_SEEN.add("Sup3rS3cretQuery")
    conf = make_conf("webhook", {"method": "POST", "include_image": "none", "timeout_s": 10},
                     {"url": url, "hmac_secret": key})
    ch = webhook.WebhookChannel(conf, HUB)

    sink.reset()
    sink.responder = lambda req: (204, {}, b"")
    ev = incident(plate="AB-123-CD", plate_known=False, area_id="ar-1", area_name="Gate", area_task="Plates",
                  area_kind="plate")
    r = ch.send(ev, ctx(web_base="https://pc.tail1234.ts.net"))
    assert r.ok, r.error
    req = sink.requests[-1]
    body = json.loads(req["body"])
    for k in ("schema", "event", "id", "ts", "time", "site", "camera", "title", "text", "severity", "verdict",
              "ai_note", "incident", "labels", "area", "plate", "image", "web_url"):
        assert k in body, k
    assert body["schema"] == "yardguard.v1" and body["event"] == "incident_new" and body["id"] == ev.id
    assert body["site"] == {"id": "home", "name": "Home"} and body["camera"] == {"channel": 1, "name": "Front gate"}
    assert body["incident"]["id"] == "inc-0123456789ab" and body["incident"]["alert_count"] == 2
    assert body["plate"] == {"text": "AB-123-CD", "known": False} and body["area"]["kind"] == "plate"
    assert body["image"] is None and body["web_url"] == "https://pc.tail1234.ts.net/#/incident/inc-0123456789ab"
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", body["time"])
    assert "snapshot_path" not in req["body"].decode() and SNAP not in req["body"].decode()
    h = req["headers"]
    assert h["Content-Type"] == "application/json" and h["User-Agent"].startswith("YardGuard/")
    assert h["X-YardGuard-Event"] == "incident_new"
    stamp = h["X-YardGuard-Timestamp"]
    assert abs(int(stamp) - time.time()) < 10
    expect = "sha256=" + hmac.new(key.encode(), stamp.encode() + b"." + req["body"], hashlib.sha256).hexdigest()
    assert h["X-YardGuard-Signature"] == expect and webhook.verify(key, stamp, req["body"], expect)
    assert not webhook.verify("other", stamp, req["body"], expect)
    assert req["path"] == "/hooks/yardguard?apikey=Sup3rS3cretQuery"
    ok("webhook: v1 body keys, no local paths, headers, HMAC-SHA256 over '<ts>.<body>' verifies")

    # base64 picture; no signature without secret; PUT method.
    conf.params["include_image"] = "base64"
    sink.reset()
    assert ch.send(incident(), ctx(pictures="snapshot")).ok
    img = json.loads(sink.requests[-1]["body"])["image"]
    data = base64.b64decode(img["base64"])
    assert img["content_type"] == "image/jpeg" and data[:2] == b"\xff\xd8" and len(data) <= 2_000_000
    pic = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert max(pic.shape[:2]) <= 1280
    assert ch.send(incident(), ctx(pictures="none")).ok
    assert json.loads(sink.requests[-1]["body"])["image"] is None
    conf2 = make_conf("webhook", {"method": "PUT", "include_image": "none"}, {"url": f"{sink.base}/plain"})
    assert webhook.WebhookChannel(conf2, HUB).send(incident(), ctx()).ok
    assert sink.requests[-1]["method"] == "PUT" and "X-YardGuard-Signature" not in sink.requests[-1]["headers"]
    ok("webhook: base64 picture option (resized), PUT, unsigned without secret")

    # Status mapping.
    sink.responder = lambda req: (200, {}, b"ok")
    sink.reply(500, b"boom")
    r = ch.send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.retry and not r.permanent, r
    sink.reply(400, b"bad")
    r = ch.send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.permanent and not r.retry, r
    sink.reply(408, b"")
    assert ch.send(incident(), ctx()).retry
    sink.reply(429, b"", {"Retry-After": "9"})
    r = ch.send(incident(), ctx())
    assert r.retry and r.retry_after == 9
    sink.reply(401, b"")
    r = ch.send(incident(), ctx())
    assert r.permanent and "signing secret" in r.error
    sink.reply(302, b"", {"Location": "http://elsewhere/"})
    r = ch.send(incident(), ctx())
    assert r.permanent and "redirect" in r.error
    conf3 = make_conf("webhook", {}, {"url": f"http://127.0.0.1:{free_port()}/h?apikey=Sup3rS3cretQuery"})
    r = webhook.WebhookChannel(conf3, HUB).send(incident(), ctx())
    note_texts(r.error)
    assert not r.ok and r.retry
    assert ch.send(incident(), ctx(edit=True)).ok
    ok("webhook: 500/408/429 retry, 400/401 permanent, redirect refused, network retry")

    # describe() / display_url mask the URL; check() posts a test event.
    d = ch.describe()
    assert d == f"http://127.0.0.1:{sink.server.server_address[1]}/*** · signed", d
    assert webhook.display_url("https://user:pw@hooks.example.com:8443/abc?key=1") == "https://hooks.example.com:8443/***"
    assert webhook.display_url("not a url") == "***"
    sink.reset()
    c = webhook.WebhookChannel.check({"method": "POST"}, {"url": url, "hmac_secret": key})
    assert c.ok and json.loads(sink.requests[-1]["body"])["event"] == "test" and "apikey" not in c.message, c
    sink.reply(403, b"")
    c = webhook.WebhookChannel.check({}, {"url": url})
    note_texts(c.message)
    assert not c.ok and c.field == "url"
    c = webhook.WebhookChannel.check({}, {"url": "ftp://example.com/x"})
    assert not c.ok and c.field == "url"
    preview = json.loads(webhook.sample_payload(include_image=True))
    assert preview["schema"] == "yardguard.v1" and preview["image"]["content_type"] == "image/jpeg"
    assert ch.test().ok
    ok("webhook: URL masked in describe(); check() posts event 'test'; payload preview for the UI")


# --- Main --------------------------------------------------------------------------------------

def main() -> int:
    t0 = time.monotonic()
    if STANDINS:
        print("NOTE using test stand-ins for:", ", ".join(STANDINS), flush=True)
    if not STANDINS:
        from yardguard.integrations import base
        for kind, cls in (("ntfy", ntfy.NtfyChannel), ("discord", discord.DiscordChannel),
                          ("email", emailer.EmailChannel), ("webhook", webhook.WebhookChannel)):
            assert base.load_channel_class(kind) is cls, kind
            assert set(cls.SECRET_KEYS) == set(base.SECRET_KEYS[kind]), kind
            d = base.new_integration(kind)
            assert base.IntegrationConf(d).kind == kind
        ok("registry: load_channel_class finds the four channels; secret keys match base")
    sink = FakeHttpSink()
    try:
        test_ntfy(sink)
        test_discord(sink)
        test_email()
        test_webhook(sink)
    finally:
        sink.close()

    no_secret(*TEXTS)
    logs = LOG_BUF.getvalue()
    no_secret(logs)
    assert "Sup3rS3cretQuery" not in logs
    ok(f"no secret in {len(TEXTS)} error/check messages or in {len(logs.splitlines())} log lines")

    # Every source file is clean UTF-8 without mojibake.
    for name in ("ntfy.py", "discord.py", "emailer.py", "webhook.py"):
        text = (ROOT / "yardguard" / "integrations" / name).read_text(encoding="utf-8")
        assert not any(bad in text for bad in ("\u00c2", "\u00e2\u20ac", "\u00c3")), name
    ok("channel sources have no mojibake")

    left = [t.name for t in threading.enumerate() if t.name.startswith("yg-")]
    assert not left, left
    elapsed = time.monotonic() - t0
    assert elapsed < 60, elapsed
    print(f"\nALL {len(PASSED)} CHECKS PASSED in {elapsed:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
