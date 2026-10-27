"""Checks for yardguard/integrations/mqtt_ha.py (Home Assistant over MQTT) against a local fake broker.

Run:  run.cmd tests\\test_integrations_mqtt.py
Uses a synthetic config (never the real config.json), a minimal MQTT 3.1.1 broker on 127.0.0.1 (port
chosen by the OS) and a small hub stand-in. Nothing reaches a real broker or Home Assistant. When the
core builder's integrations.model / integrations.base are not there yet, SPEC-shaped stand-ins are
installed for this test only. Skips (exit 0) when paho-mqtt is not installed.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import io
import json
import logging
import os
import socket
import socketserver
import struct
import sys
import threading
import time
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "integrations" / "mqtt"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
sys.path.insert(0, str(ROOT))

from yardguard import config  # noqa: E402
from yardguard.config import Camera, Site, new_area  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"

PASSED: list[str] = []
PASSWORD = "s3cret-Pa55word-xyz"


def ok(name: str) -> None:
    PASSED.append(name)
    print("PASS", name, flush=True)


def wait_for(pred, timeout: float = 5.0, step: float = 0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


# --- stand-ins for the core builder's contracts (SPEC 2.3 / 2.4), only when missing ----------------

def _install_core_standins() -> bool:
    try:
        import yardguard.integrations.base  # noqa: F401
        import yardguard.integrations.model  # noqa: F401
        return False
    except Exception as e:   # not published yet, or mid-edit by the core builder
        print("core model/base not importable:", type(e).__name__, e)
        for name in [m for m in sys.modules if m.startswith("yardguard.integrations.")]:
            sys.modules.pop(name, None)
    model = types.ModuleType("yardguard.integrations.model")

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
            order = [self.snapshot_path, self.crop_path] if pictures == "snapshot" else [self.crop_path,
                                                                                         self.snapshot_path]
            return next((p for p in order if p and Path(p).is_file()), "")

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
        data: dict = dataclasses.field(default_factory=dict)   # "field" is shadowed by the attribute above

    @dataclass
    class Action:
        kind: str
        args: dict
        source: str
        user: str = ""

    for c in (Event, SendContext, SendResult, CheckResult, Action):
        c.__module__ = model.__name__
        setattr(model, c.__name__, c)

    base = types.ModuleType("yardguard.integrations.base")

    def scrub(text: str, *secrets: str) -> str:
        text = str(text)
        for s in secrets:
            if s:
                text = text.replace(s, "***")
        return text[:300]

    class IntegrationConf:
        def __init__(self, d: dict) -> None:
            self.d = d

        id = property(lambda self: self.d["id"])
        kind = property(lambda self: self.d["kind"])
        name = property(lambda self: self.d.get("name", ""))
        enabled = property(lambda self: self.d.get("enabled", True))
        params = property(lambda self: self.d.setdefault("params", {}))
        routes = property(lambda self: self.d.setdefault("routes", {}))

        def secret(self, key: str) -> str:
            v = self.d.get("secrets", {}).get(key, "")
            return config.decrypt(v) if v else ""

        def set_secret(self, key: str, value: str) -> None:
            sec = self.d.setdefault("secrets", {})
            if value:
                sec[key] = config.encrypt(value)
            else:
                sec.pop(key, None)

        def has_secret(self, key: str) -> bool:
            return bool(self.d.get("secrets", {}).get(key))

        def secret_unreadable(self, key: str) -> bool:
            return self.has_secret(key) and not self.secret(key)

    class Channel:
        KIND = ""
        CAPS: dict = {}
        SECRET_KEYS: tuple = ()

        def __init__(self, conf, hub) -> None:
            self.conf, self.hub = conf, hub
            self.status, self.status_text = "off", ""

    base.scrub, base.IntegrationConf, base.Channel = scrub, IntegrationConf, Channel
    sys.modules[model.__name__] = model
    sys.modules[base.__name__] = base
    return True


STANDINS = _install_core_standins()

try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    print("SKIP paho missing (install with run.cmd -m pip install paho-mqtt)")
    sys.exit(0)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yardguard.integrations import mqtt_ha  # noqa: E402
from yardguard.integrations.base import IntegrationConf  # noqa: E402
from yardguard.integrations.model import Event, SendContext  # noqa: E402


# --- a minimal MQTT 3.1.1 broker -----------------------------------------------------------------------

def _enc_str(s: str | bytes) -> bytes:
    b = s.encode("utf-8") if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def _packet(first: int, body: bytes) -> bytes:
    n, out = len(body), bytearray([first])
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + body


def topic_matches(flt: str, topic: str) -> bool:
    f, t = flt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


class FakeMqttBroker:
    """CONNECT/CONNACK (user/password check), PUBLISH QoS 0/1 + PUBACK, SUBSCRIBE/UNSUBSCRIBE, PING,
    DISCONNECT, retained messages, last will on abrupt drops. Records every client publish."""

    def __init__(self, username: str = "", password: str = "") -> None:
        self.username, self.password = username, password
        self.lock = threading.RLock()
        self.retained: dict[str, bytes] = {}
        self.published: list[dict] = []
        self.sessions: dict[int, "socketserver.BaseRequestHandler"] = {}
        self.connects: list[dict] = []
        broker = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                broker._serve(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="fake-mqtt")
        self.thread.start()

    # -- helpers for the tests
    def pubs(self, topic: str | None = None) -> list[dict]:
        with self.lock:
            return [p for p in self.published if topic is None or p["topic"] == topic]

    def last(self, topic: str) -> dict | None:
        p = self.pubs(topic)
        return p[-1] if p else None

    def inject(self, topic: str, payload: bytes | str, retain: bool = False) -> None:
        """A message from another client (Home Assistant)."""
        data = payload.encode() if isinstance(payload, str) else payload
        self._route(topic, data, retain)

    def kill_all(self) -> None:
        """Drop every client connection without a DISCONNECT (wills are published)."""
        with self.lock:
            socks = [s["sock"] for s in self.sessions.values()]
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        self.kill_all()
        self.server.shutdown()
        self.server.server_close()

    # -- protocol
    def _route(self, topic: str, payload: bytes, retain: bool) -> None:
        with self.lock:
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            targets = [s for s in self.sessions.values() if any(topic_matches(f, topic) for f in s["subs"])]
        for s in targets:
            self._send(s, _packet(0x30, _enc_str(topic) + payload))

    @staticmethod
    def _send(s: dict, data: bytes) -> None:
        try:
            with s["wlock"]:
                s["sock"].sendall(data)
        except OSError:
            pass

    @staticmethod
    def _read(sock, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _read_packet(self, sock) -> tuple[int, bytes]:
        first = self._read(sock, 1)[0]
        mult, length = 1, 0
        while True:
            b = self._read(sock, 1)[0]
            length += (b & 0x7F) * mult
            mult *= 128
            if not b & 0x80:
                break
        return first, self._read(sock, length) if length else b""

    def _serve(self, sock) -> None:
        sess = {"sock": sock, "wlock": threading.Lock(), "subs": [], "will": None, "client_id": ""}
        key = id(sess)
        clean_exit = False
        try:
            first, body = self._read_packet(sock)
            if first >> 4 != 1:
                return
            i = 2 + struct.unpack("!H", body[:2])[0]
            flags = body[i + 1]
            i += 4
            def take():
                nonlocal i
                n = struct.unpack("!H", body[i:i + 2])[0]
                v = body[i + 2:i + 2 + n]
                i += 2 + n
                return v
            client_id = take().decode()
            will = None
            if flags & 0x04:
                will = {"topic": take().decode(), "payload": take(), "retain": bool(flags & 0x20)}
            user = take().decode() if flags & 0x80 else ""
            pw = take().decode() if flags & 0x40 else ""
            with self.lock:
                self.connects.append({"client_id": client_id, "user": user, "password": pw})
            if self.username and (user != self.username or pw != self.password):
                sock.sendall(bytes([0x20, 2, 0, 4]))     # bad user name or password
                return
            sock.sendall(bytes([0x20, 2, 0, 0]))
            sess.update(will=will, client_id=client_id)
            with self.lock:
                self.sessions[key] = sess
            while True:
                first, body = self._read_packet(sock)
                ptype = first >> 4
                if ptype == 3:
                    qos, retain = (first >> 1) & 3, bool(first & 1)
                    n = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + n].decode()
                    j, pid = 2 + n, b""
                    if qos:
                        pid = body[j:j + 2]
                        j += 2
                    payload = body[j:]
                    with self.lock:
                        self.published.append({"topic": topic, "payload": payload, "retain": retain, "qos": qos,
                                               "client_id": client_id, "ts": time.time()})
                    self._route(topic, payload, retain)
                    if qos:      # acknowledged once stored, like a real broker
                        self._send(sess, _packet(0x40, pid))
                elif ptype == 8:
                    pid, j, granted, new = body[:2], 2, [], []
                    while j < len(body):
                        n = struct.unpack("!H", body[j:j + 2])[0]
                        new.append(body[j + 2:j + 2 + n].decode())
                        granted.append(min(1, body[j + 2 + n]))
                        j += 3 + n
                    with self.lock:
                        sess["subs"].extend(new)
                        ret = [(t, p) for t, p in self.retained.items() if any(topic_matches(f, t) for f in new)]
                    self._send(sess, _packet(0x90, pid + bytes(granted)))
                    for t, p in ret:
                        self._send(sess, _packet(0x31, _enc_str(t) + p))
                elif ptype == 10:
                    pid, j = body[:2], 2
                    while j < len(body):
                        n = struct.unpack("!H", body[j:j + 2])[0]
                        t = body[j + 2:j + 2 + n].decode()
                        with self.lock:
                            if t in sess["subs"]:
                                sess["subs"].remove(t)
                        j += 2 + n
                    self._send(sess, _packet(0xB0, pid))
                elif ptype == 12:
                    self._send(sess, bytes([0xD0, 0]))
                elif ptype == 14:
                    clean_exit = True
                    return
        except (ConnectionError, OSError, IndexError, struct.error):
            pass
        finally:
            with self.lock:
                self.sessions.pop(key, None)
            if not clean_exit and sess.get("will"):
                w = sess["will"]
                with self.lock:
                    self.published.append({"topic": w["topic"], "payload": w["payload"], "retain": w["retain"],
                                           "qos": 1, "client_id": "(will of " + client_id_or(sess) + ")",
                                           "ts": time.time()})
                self._route(w["topic"], w["payload"], w["retain"])
            try:
                sock.close()
            except OSError:
                pass


def client_id_or(sess: dict) -> str:
    return sess.get("client_id") or "?"


# --- a hub stand-in with the SPEC 2.6 surface the channel uses -----------------------------------------

class FakeSignal:
    def __init__(self):
        self.calls: list[tuple] = []

    def emit(self, *a):
        self.calls.append(a)


class FakeKV:
    def __init__(self):
        self.kv: dict[str, str] = {}

    def kv_get(self, key, default=""):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


class FakeFrames:
    def __init__(self, site: Site):
        self.site = site

    def cameras(self):
        return [(c.channel, c.label) for c in self.site.enabled_cameras()]


class FakeHub:
    def __init__(self, site: Site):
        self.site = site
        settings = types.SimpleNamespace(install_id="ab12cd34", events_dir=str(OUT / "events"))
        cfg = types.SimpleNamespace(settings=settings, active_site=lambda: self.site)
        self.ctx = types.SimpleNamespace(cfg=cfg, monitor=types.SimpleNamespace(running=True))
        self.frames = FakeFrames(site)
        self.log = FakeKV()
        self.destination_status = FakeSignal()
        self.status_notes: list[str] = []
        self.listeners: list = []
        self.actions: list = []
        self.sample_picture = ""

    def notify_status(self, dest_id):     # what the real hub offers to Channel.set_status (any thread)
        self.status_notes.append(dest_id)

    def add_listener(self, fn):
        self.listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    def feed(self, kind, payload):
        for fn in list(self.listeners):
            fn(kind, payload)

    def request(self, action):
        self.actions.append(action)
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run():   # the real hub runs actions on the GUI thread; any other thread will do here
            time.sleep(0.02)
            if action.kind == "set_monitoring":
                self.ctx.monitor.running = bool(action.args["on"])
                fut.set_result({"ok": True, "running": self.ctx.monitor.running, "message": "ok"})
                self.feed("monitoring", self.ctx.monitor.running)
            elif action.kind == "snapshot":
                ok_, buf = cv2.imencode(".jpg", np.full((90, 160, 3), 120, np.uint8))
                fut.set_result({"ok": True, "jpeg": buf.tobytes(), "camera": "Front gate"})
            elif action.kind == "mark_incident":
                fut.set_result({"ok": True, "title": "x", "status": action.args["status"]})
            else:
                fut.set_result({"ok": False})
        threading.Thread(target=run, daemon=True).start()
        return fut

    def sample_event(self):
        return Event(kind="test", ts=time.time(), title="YardGuard test", crop_path=self.sample_picture,
                     camera="Front gate", channel=1)

    def web_base(self):
        return ""


def make_site() -> Site:
    gate = Camera(channel=1, name="Front gate")
    drive = Camera(channel=2, name="Driveway Ünïcode")
    gate.areas.append(new_area("log_plates", "line", [(0.1, 0.5), (0.9, 0.5)], "Gate barrier", id="pl8a1234"))
    return Site(name="Home", cameras=[gate, drive])


def make_conf(port: int, **params) -> IntegrationConf:
    d = {"id": "mq-" + uuid.uuid4().hex[:8], "kind": "mqtt", "name": "Home Assistant", "enabled": True,
         "created": time.time(), "secrets": {},
         "params": {"host": "127.0.0.1", "port": port, "tls": False, "username": "ha", "discovery": True,
                    "discovery_prefix": "homeassistant", "base_topic": "yardguard", "retain_images": True,
                    "allow_control": True, **params},
         "routes": {"pictures": "crop"}}
    conf = IntegrationConf(d)
    conf.set_secret("password", PASSWORD)
    return conf


def ctx(pictures: str = "crop", edit: bool = False) -> SendContext:
    return SendContext(delivery_id="d1", pictures=pictures, silent=False, priority=3, thread_ref=None, edit=edit,
                       send_clip=False, web_base="", attempt=1)


def yg_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("yg-") and t.is_alive()]


# --- tests ----------------------------------------------------------------------------------------------

def main() -> int:
    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setLevel(logging.DEBUG)
    ylog = logging.getLogger("yardguard")
    ylog.addHandler(handler)
    ylog.setLevel(logging.DEBUG)
    print("core stand-ins:", "yes (core not published yet)" if STANDINS else "no (real model/base)")

    pic = OUT / "events" / "crop ü.jpg"
    pic.parent.mkdir(parents=True, exist_ok=True)
    ok_, buf = cv2.imencode(".jpg", np.random.default_rng(1).integers(0, 255, (240, 320, 3), np.uint8))
    pic.write_bytes(buf.tobytes())

    # 1. pure helpers
    assert mqtt_ha.parse_host("mqtt://user:pw@homeassistant.local:1884/") == ("homeassistant.local", 1884)
    assert mqtt_ha.parse_host("192.168.1.5") == ("192.168.1.5", None)
    assert mqtt_ha.parse_host("[fe80::1]:1883") == ("fe80::1", 1883)
    assert mqtt_ha.clean_topic(" my/+base#/ ", "x") == "my/_base_"
    t = mqtt_ha.Topics("yardguard", "ab12cd34")
    assert t.discovery == "homeassistant/device/yardguard_ab12cd34/config"
    assert t.cam(3, "person") == "yardguard/ab12cd34/cam/3/person"
    site = make_site()
    cams = [(c.channel, c.label) for c in site.enabled_cameras()]
    cm = mqtt_ha.components("ab12cd34", t, cams, [("pl8a1234", "Gate barrier")])
    uids = [c["unique_id"] for c in cm.values()]
    assert len(uids) == len(set(uids)) == 2 + 7 * 2 + 1, len(uids)
    assert all(u.startswith("yardguard_ab12cd34_") for u in uids)
    assert all("p" in c for c in cm.values())
    assert cm["cam1_alert"]["event_types"] == ["critical", "warning", "info"]
    assert cm["cam1_image"]["content_type"] == "image/jpeg"
    assert cm["cam2_online"]["device_class"] == "connectivity"
    assert "monitoring_running" in mqtt_ha.components("n", t, cams, [], control=False)
    assert mqtt_ha.entity_list(cams, [("a", "Gate")])[0] == "Monitoring switch"
    assert "yardguard/ab12cd34/monitoring/set" in mqtt_ha.topic_list({}, "ab12cd34")
    ok("helpers: topics, host parsing, components with unique ids")

    broker = FakeMqttBroker("ha", PASSWORD)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()     # nothing listens there any more

    # 2. check(): ok / wrong password / unreachable / bad input / no secret leaks
    base_params = make_conf(broker.port).params
    r = mqtt_ha.MqttChannel.check(base_params, {"password": PASSWORD})
    assert r.ok, r
    assert broker.connects[-1]["user"] == "ha"
    r = mqtt_ha.MqttChannel.check(base_params, {"password": "wrong"})
    assert not r.ok and r.message == "Wrong user name or password." and r.field == "password", r
    t0 = time.monotonic()
    r = mqtt_ha.MqttChannel.check({**base_params, "port": closed_port}, {"password": PASSWORD})
    assert not r.ok and "Could not reach the broker" in r.message and r.field == "host", r
    assert time.monotonic() - t0 < 6
    r = mqtt_ha.MqttChannel.check({**base_params, "host": ""}, {})
    assert not r.ok and r.field == "host"
    r = mqtt_ha.MqttChannel.check({**base_params, "port": 70000}, {})
    assert not r.ok and r.field == "port"
    r = mqtt_ha.MqttChannel.check({**base_params, "host": "no-such-host.invalid"}, {"password": PASSWORD})
    assert not r.ok and r.field == "host", r
    assert PASSWORD not in r.message
    ok("check(): connect, wrong password, unreachable, bad host/port")

    # 3. test() before start: short connection, discovery + test event
    hub = FakeHub(site)
    hub.sample_picture = str(pic)
    conf = make_conf(broker.port)
    ch = mqtt_ha.MqttChannel(conf, hub)
    res = ch.test()
    assert res.ok, res
    ev = json.loads(broker.last("yardguard/ab12cd34/events")["payload"])
    assert ev["type"] == "test" and ev["event"]["title"] == "YardGuard test"
    assert broker.retained.get("homeassistant/device/yardguard_ab12cd34/config")
    bad = mqtt_ha.MqttChannel(make_conf(broker.port), hub)
    bad.conf.set_secret("password", "nope")
    res = bad.test()
    assert not res.ok and res.permanent and "Wrong user name" in res.error
    assert not yg_threads(), yg_threads()
    ok("test(): works before start, wrong password is permanent")

    # 4. start: availability, discovery, subscriptions, monitoring state
    t0 = time.monotonic()
    ch.start()
    assert time.monotonic() - t0 < 0.5, "start must not block the GUI thread"
    assert wait_for(lambda: ch.status == "ok"), (ch.status, ch.status_text)
    assert hub.destination_status.calls or hub.status_notes, "status changes are announced"
    assert any(n.startswith("yg-mqtt-") for n in yg_threads())
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/availability") == b"online")
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/monitoring/state") == b"ON")
    disc = json.loads(broker.retained["homeassistant/device/yardguard_ab12cd34/config"])
    assert disc["dev"]["ids"] == ["yardguard_ab12cd34"] and disc["dev"]["name"] == "YardGuard"
    assert disc["o"]["name"] == "YardGuard" and disc["avty_t"] == "yardguard/ab12cd34/availability"
    assert set(disc["cmps"]) >= {"monitoring", "last_incident", "cam1_online", "cam1_person", "cam1_vehicle",
                                 "cam1_person_count", "cam1_image", "cam1_alert", "cam1_snapshot", "cam2_alert",
                                 "areapl8a1234_last_plate"}, sorted(disc["cmps"])
    assert disc["cmps"]["cam2_online"]["name"] == "Driveway Ünïcode online"
    assert broker.last("homeassistant/device/yardguard_ab12cd34/config")["retain"]
    assert "Driveway" in ch.describe() or "127.0.0.1:" in ch.describe()
    assert PASSWORD not in ch.describe()
    ok("start(): availability online, retained discovery with all components, monitoring state")

    # 5. status feed -> online / person / vehicle / count, deduplicated
    hub.feed("status", {1: {"state": "watching", "objects": {"person": 2, "car": 1}},
                        2: {"state": "offline", "objects": {}}})
    def retained_is(topic: str, value: bytes) -> bool:
        return bool(wait_for(lambda: broker.retained.get(topic) == value))
    assert retained_is("yardguard/ab12cd34/cam/1/person_count", b"2")
    assert retained_is("yardguard/ab12cd34/cam/1/person", b"ON")
    assert retained_is("yardguard/ab12cd34/cam/1/vehicle", b"ON")
    assert retained_is("yardguard/ab12cd34/cam/1/online", b"ON")
    assert retained_is("yardguard/ab12cd34/cam/2/online", b"OFF")
    assert retained_is("yardguard/ab12cd34/cam/2/vehicle", b"OFF")
    time.sleep(0.1)
    n = len(broker.pubs())
    hub.feed("status", {1: {"state": "watching", "objects": {"person": 2, "car": 1}},
                        2: {"state": "offline", "objects": {}}})
    time.sleep(0.2)
    assert len(broker.pubs()) == n, "unchanged status must not publish again"
    hub.feed("status", {1: {"state": "watching", "objects": {"truck": 1}}})
    assert retained_is("yardguard/ab12cd34/cam/1/person", b"OFF")
    assert retained_is("yardguard/ab12cd34/cam/1/vehicle", b"ON")
    ok("status feed: online/person/vehicle/person_count, no repeats")

    # 6. Home Assistant switch -> set_monitoring action, state follows
    broker.inject("yardguard/ab12cd34/monitoring/set", "OFF")
    assert wait_for(lambda: any(a.kind == "set_monitoring" for a in hub.actions))
    a = [a for a in hub.actions if a.kind == "set_monitoring"][-1]
    assert a.args == {"on": False} and a.source == "Home Assistant"
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/monitoring/state") == b"OFF")
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/cam/1/vehicle") == b"OFF")
    broker.inject("yardguard/ab12cd34/monitoring/set", "on")
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/monitoring/state") == b"ON")
    k = len(hub.actions)
    broker.inject("yardguard/ab12cd34/monitoring/set", "maybe")
    broker.inject("yardguard/ab12cd34/monitoring/set", "x" * 5000)
    time.sleep(0.3)
    assert len(hub.actions) == k, "junk commands are ignored"
    ok("monitoring/set ON/OFF -> set_monitoring action; junk ignored")

    # 7. incident/set -> mark action
    broker.inject("yardguard/ab12cd34/incident/set", json.dumps({"id": "inc-0123456789ab", "status": "handled"}))
    assert wait_for(lambda: any(a.kind == "mark_incident" for a in hub.actions))
    a = [a for a in hub.actions if a.kind == "mark_incident"][-1]
    assert a.args == {"incident_id": "inc-0123456789ab", "status": "handled"}
    k = len(hub.actions)
    broker.inject("yardguard/ab12cd34/incident/set", json.dumps({"id": "../../x", "status": "handled"}))
    broker.inject("yardguard/ab12cd34/incident/set", json.dumps({"id": "inc-1", "status": "deleted"}))
    broker.inject("yardguard/ab12cd34/incident/set", "not json")
    time.sleep(0.3)
    assert len(hub.actions) == k
    ok("incident/set -> mark_incident; bad ids/statuses ignored")

    # 8. snapshot button -> picture on the image topic, presses throttled
    before = len(broker.pubs("yardguard/ab12cd34/cam/2/image"))
    broker.inject("yardguard/ab12cd34/cam/2/snapshot/press", "PRESS")
    assert wait_for(lambda: len(broker.pubs("yardguard/ab12cd34/cam/2/image")) == before + 1)
    img = broker.last("yardguard/ab12cd34/cam/2/image")
    assert img["payload"][:2] == b"\xff\xd8" and img["retain"]
    snaps = sum(a.kind == "snapshot" for a in hub.actions)
    broker.inject("yardguard/ab12cd34/cam/2/snapshot/press", "PRESS")
    broker.inject("yardguard/ab12cd34/cam/9/snapshot/press", "PRESS")   # unknown camera
    time.sleep(0.3)
    assert sum(a.kind == "snapshot" for a in hub.actions) == snaps, "throttled / unknown camera ignored"
    ok("snapshot/press -> image published; throttled; unknown camera ignored")

    # 9. send(): incident new / update / escalation / status / other kinds
    inc = Event(kind="incident_new", ts=time.time(), title="Person near the car at night", severity="critical",
                channel=1, camera="Front gate", incident_id="inc-aaaaaaaaaaaa", alert_id="a1", alert_count=1,
                verdict="confirmed", crop_path=str(pic), labels=["person"])
    res = ch.send(inc, ctx())
    assert res.ok, res
    evs = json.loads(broker.last("yardguard/ab12cd34/events")["payload"])
    assert evs["type"] == "new" and evs["incident"]["incident"]["id"] == "inc-aaaaaaaaaaaa"
    assert evs["incident"]["schema"] == "yardguard.v1" and evs["incident"]["has_crop"] is True
    assert json.loads(broker.retained["yardguard/ab12cd34/incident"])["title"] == "Person near the car at night"
    alert = broker.last("yardguard/ab12cd34/cam/1/alert")
    assert json.loads(alert["payload"])["event_type"] == "critical" and not alert["retain"]
    img = broker.last("yardguard/ab12cd34/cam/1/image")
    assert img["payload"][:2] == b"\xff\xd8" and img["retain"]
    n_alert = len(broker.pubs("yardguard/ab12cd34/cam/1/alert"))
    upd = Event(**{**inc.__dict__, "kind": "incident_update", "alert_count": 2, "id": "e2"})
    assert ch.send(upd, ctx()).ok
    assert json.loads(broker.last("yardguard/ab12cd34/events")["payload"])["type"] == "update"
    assert len(broker.pubs("yardguard/ab12cd34/cam/1/alert")) == n_alert, "plain updates fire no new HA event"
    esc = Event(**{**upd.__dict__, "id": "e3", "escalated": True})
    assert ch.send(esc, ctx()).ok
    assert len(broker.pubs("yardguard/ab12cd34/cam/1/alert")) == n_alert + 1
    n_img = len(broker.pubs("yardguard/ab12cd34/cam/1/image"))
    st = Event(**{**inc.__dict__, "kind": "incident_status", "status": "handled", "status_by": "Alex", "id": "e4"})
    assert ch.send(st, ctx()).ok
    assert json.loads(broker.last("yardguard/ab12cd34/events")["payload"])["type"] == "status"
    assert json.loads(broker.retained["yardguard/ab12cd34/incident"])["incident"]["status"] == "handled"
    assert len(broker.pubs("yardguard/ab12cd34/cam/1/image")) == n_img
    other = Event(kind="incident_status", ts=time.time(), title="Older one", incident_id="inc-bbbbbbbbbbbb",
                  channel=2, status="handled")
    assert ch.send(other, ctx()).ok
    assert json.loads(broker.retained["yardguard/ab12cd34/incident"])["incident"]["id"] == "inc-aaaaaaaaaaaa", \
        "an older incident must not replace the retained last incident"
    off = Event(kind="camera_offline", ts=time.time(), title="Camera Driveway stopped sending video", channel=2)
    assert ch.send(off, ctx()).ok
    assert json.loads(broker.last("yardguard/ab12cd34/events")["payload"])["type"] == "camera_offline"
    n_img = len(broker.pubs("yardguard/ab12cd34/cam/1/image"))
    assert ch.send(Event(**{**inc.__dict__, "id": "e5"}), ctx("none")).ok
    assert len(broker.pubs("yardguard/ab12cd34/cam/1/image")) == n_img, "pictures none -> no image"
    assert ch.send(inc, ctx(edit=True)).ok
    ok("send(): events + retained incident + HA alert event + image; updates/status/other kinds")

    # 10. area events via the hub feed
    area_ev = types.SimpleNamespace(id="ae1", site_id=site.id, channel=1, area_id="pl8a1234", area_name="Gate barrier",
                                    task="log_plates", ts=time.time(), kind="plate", label="car", value="AB-123-CD",
                                    snapshot_path="", crop_path=str(pic), clip_path="")
    hub.feed("area_event", area_ev)
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/area/pl8a1234/plate") == b"AB-123-CD")
    body = json.loads(wait_for(lambda: broker.last("yardguard/ab12cd34/area/pl8a1234/event"))["payload"])
    assert body["kind"] == "plate" and body["value"] == "AB-123-CD" and body["camera"]["name"] == "Front gate"
    assert body["kind_text"] == "Number plate"
    ok("area events: per-area event JSON and retained last plate")

    # 11. Home Assistant restarted -> discovery re-published after a short random delay
    mqtt_ha.random = types.SimpleNamespace(uniform=lambda a, b: 0.1)
    nd = len(broker.pubs("homeassistant/device/yardguard_ab12cd34/config"))
    broker.inject("homeassistant/status", "online")
    assert wait_for(lambda: len(broker.pubs("homeassistant/device/yardguard_ab12cd34/config")) == nd + 1, 3)
    broker.inject("homeassistant/status", "offline")
    time.sleep(0.4)
    assert len(broker.pubs("homeassistant/device/yardguard_ab12cd34/config")) == nd + 1
    ok("homeassistant/status online -> discovery re-published")

    # 12. camera removed -> tombstones so HA removes its entities; config feed only publishes on change
    site.cameras = [c for c in site.cameras if c.channel != 2]
    hub.feed("config", None)
    assert wait_for(lambda: json.loads(broker.retained["homeassistant/device/yardguard_ab12cd34/config"])
                    ["cmps"].get("cam2_alert") == {"p": "event"})
    disc = json.loads(broker.retained["homeassistant/device/yardguard_ab12cd34/config"])
    assert disc["cmps"]["cam2_alert"] == {"p": "event"} and disc["cmps"]["cam2_online"] == {"p": "binary_sensor"}
    assert "unique_id" in disc["cmps"]["cam1_alert"]
    nd = len(broker.pubs("homeassistant/device/yardguard_ab12cd34/config"))
    hub.feed("config", None)
    time.sleep(0.2)
    assert len(broker.pubs("homeassistant/device/yardguard_ab12cd34/config")) == nd
    site.cameras.append(Camera(channel=2, name="Driveway Ünïcode"))
    hub.feed("config", None)
    assert wait_for(lambda: "unique_id" in json.loads(
        broker.retained["homeassistant/device/yardguard_ab12cd34/config"])["cmps"]["cam2_alert"])
    ok("removed camera -> platform-only components (HA removes them); re-added camera restored")

    # 13. allow_control off -> commands ignored, switch becomes a read-only sensor
    conf.params["allow_control"] = False
    ch.reconfigure(conf)
    assert wait_for(lambda: "monitoring_running" in json.loads(
        broker.retained["homeassistant/device/yardguard_ab12cd34/config"])["cmps"])
    disc = json.loads(broker.retained["homeassistant/device/yardguard_ab12cd34/config"])
    assert disc["cmps"]["monitoring"] == {"p": "switch"} and disc["cmps"]["monitoring_running"]["p"] == "binary_sensor"
    def subscribed(topic: str) -> bool:
        with broker.lock:
            return any(topic in s["subs"] for s in broker.sessions.values())
    assert wait_for(lambda: not subscribed("yardguard/ab12cd34/monitoring/set")), "command topics unsubscribed"
    k = len(hub.actions)
    broker.inject("yardguard/ab12cd34/monitoring/set", "OFF")
    time.sleep(0.4)
    assert len(hub.actions) == k, "control from Home Assistant is off"
    conf.params["allow_control"] = True
    ch.reconfigure(conf)
    assert wait_for(lambda: json.loads(broker.retained["homeassistant/device/yardguard_ab12cd34/config"])
                    ["cmps"]["monitoring"].get("command_topic"))
    assert wait_for(lambda: subscribed("yardguard/ab12cd34/monitoring/set")), "command topics subscribed again"
    broker.inject("yardguard/ab12cd34/monitoring/set", "ON")
    assert wait_for(lambda: len(hub.actions) == k + 1)
    ok("allow_control off: commands ignored, switch -> read-only sensor, and back")

    # 14. abrupt connection loss -> LWT offline; automatic reconnect -> online + states replayed
    broker.kill_all()
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/availability") == b"offline", 3)
    assert wait_for(lambda: ch.status in ("connecting", "problem") or not ch._connected.is_set(), 2)
    res = ch.send(inc, ctx()) if not ch._connected.is_set() else None
    if res is not None:
        assert not res.ok and res.retry, res
    broker.retained.pop("yardguard/ab12cd34/cam/1/vehicle", None)     # broker "forgot" a state
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/availability") == b"online", 8)
    assert wait_for(lambda: ch.status == "ok")
    assert wait_for(lambda: broker.retained.get("yardguard/ab12cd34/cam/1/vehicle") is not None, 3)
    ok("LWT offline on abrupt close; reconnect -> online and retained states replayed")

    # 15. connection settings change -> old discovery removed, new topics used
    old_disc = "homeassistant/device/yardguard_ab12cd34/config"
    new_disc = "ha2/device/yardguard_ab12cd34/config"
    conf.params["base_topic"] = "home/yg"
    conf.params["discovery_prefix"] = "ha2"
    ch.reconfigure(conf)
    assert old_disc not in broker.retained, "old discovery cleared"
    assert broker.retained.get("yardguard/ab12cd34/availability") == b"offline", "old availability says offline"
    assert wait_for(lambda: broker.retained.get("home/yg/ab12cd34/availability") == b"online")
    disc = json.loads(wait_for(lambda: broker.retained.get(new_disc), 3))
    assert disc["cmps"]["cam1_person"]["state_topic"] == "home/yg/ab12cd34/cam/1/person"
    nd = len(broker.pubs(new_disc))
    time.sleep(0.2)
    broker.inject("ha2/status", "online")
    assert wait_for(lambda: len(broker.pubs(new_disc)) == nd + 1, 3), "subscribed to the new HA status topic"
    conf.params["discovery"] = False
    ch.reconfigure(conf)
    assert new_disc not in broker.retained, "discovery off removes the device from HA"
    conf.params["discovery"] = True
    ch.reconfigure(conf)
    assert wait_for(lambda: broker.retained.get(new_disc), 3)
    ok("reconfigure: new prefix/base topic moves the device; discovery off clears it")

    # 16. stop(): offline published, < 3 s, no threads; listener removed
    t0 = time.monotonic()
    ch.stop()
    took = time.monotonic() - t0
    assert took < 3, took
    assert broker.retained.get("home/yg/ab12cd34/availability") == b"offline"
    assert ch.status == "off" and not hub.listeners
    assert wait_for(lambda: not yg_threads(), 3), yg_threads()
    res = ch.send(inc, ctx())
    assert not res.ok and res.retry
    ok(f"stop(): offline + disconnect in {took:.2f} s, no yg- threads left")

    # 17. broker unreachable: problem status in plain words, stop still quick
    ch2 = mqtt_ha.MqttChannel(make_conf(closed_port), FakeHub(make_site()))
    ch2.start()
    assert wait_for(lambda: ch2.status == "problem", 8), (ch2.status, ch2.status_text)
    assert "Could not reach the broker" in ch2.status_text
    t0 = time.monotonic()
    ch2.stop()
    assert time.monotonic() - t0 < 3
    ch3 = mqtt_ha.MqttChannel(make_conf(broker.port), FakeHub(make_site()))
    ch3.conf.set_secret("password", "wrong-one")
    ch3.start()
    assert wait_for(lambda: ch3.status == "problem", 5) and "Wrong user name or password" in ch3.status_text
    ch3.stop()
    assert wait_for(lambda: not yg_threads(), 3), yg_threads()
    ok("unreachable broker / wrong password -> plain problem text; stop < 3 s")

    # 18. add-on missing: degrade gracefully
    real = mqtt_ha._mqtt
    mqtt_ha._mqtt = lambda: None
    try:
        r = mqtt_ha.MqttChannel.check(base_params, {"password": PASSWORD})
        assert not r.ok and r.message == mqtt_ha.ADDON_MISSING
        ch4 = mqtt_ha.MqttChannel(make_conf(broker.port), FakeHub(make_site()))
        ch4.start()
        assert ch4.status == "problem" and "add-on" in ch4.status_text
        res = ch4.send(inc, ctx())
        assert not res.ok and res.permanent
        ch4.stop()
    finally:
        mqtt_ha._mqtt = real
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    real_run = mqtt_ha.subprocess.run
    mqtt_ha.subprocess.run = fake_run
    try:
        good, msg = mqtt_ha.install_addon()
    finally:
        mqtt_ha.subprocess.run = real_run
    assert good, msg
    assert calls[0][0][0].lower().endswith("run.cmd") and "paho-mqtt>=2,<3" in calls[0][0]
    assert calls[0][1].get("creationflags") == getattr(mqtt_ha.subprocess, "CREATE_NO_WINDOW", 0)
    ok("paho missing: check/start/send degrade; install_addon runs pip through run.cmd (faked)")

    # 19. secrets never leak
    handler.flush()
    logs = log_buf.getvalue()
    assert PASSWORD not in logs
    for p in broker.pubs():
        assert PASSWORD.encode() not in p["payload"], p["topic"]
    for c in (ch, ch2, ch3):
        assert PASSWORD not in c.status_text and PASSWORD not in c.describe()
    raw = json.dumps(conf.d)
    assert PASSWORD not in raw, "password stored encrypted only"
    ok("no password in logs, publishes, status texts or the stored destination")

    broker.close()
    assert not yg_threads(), yg_threads()
    print(f"\nALL {len(PASSED)} MQTT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    t_start = time.monotonic()
    rc = main()
    print(f"({time.monotonic() - t_start:.1f} s)")
    sys.exit(rc)
