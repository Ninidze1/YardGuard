"""Phone access (yardguard/webremote + yardguard/web) checks with local fakes only.

Run:  run.cmd tests\\test_integrations_webapp.py            (all checks, exit code 0 = pass)
      run.cmd tests\\test_integrations_webapp.py --serve    (demo server for screenshots, port 8799)

Uses a synthetic config and events folder under .cache\\tests\\integrations\\webapp\\ (never the real
config.json or alerts.db), a monitor stub with generated frames, and the real IntegrationHub for
actions (or a small stand-in when the hub cannot be built). Nothing outside 127.0.0.1 is contacted.
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "integrations" / "webapp"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from yardguard import config  # noqa: E402
from yardguard.areas import AreaEvent  # noqa: E402
from yardguard.config import Camera, Config, Site, new_area  # noqa: E402
from yardguard.engine import Alert, AlertStore  # noqa: E402
from yardguard.gui import AlertBridge  # noqa: E402
from yardguard.webremote import WebRemote, normalize_remote, server as srv, tailscale  # noqa: E402
from yardguard.webremote.auth import PairingCodes, PairLimiter  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"

app = QApplication.instance() or QApplication(sys.argv)
LOG = io.StringIO()
_h = logging.StreamHandler(LOG)
_h.setLevel(logging.DEBUG)
logging.getLogger("yardguard").addHandler(_h)
logging.getLogger("yardguard").setLevel(logging.DEBUG)


# -- fakes ------------------------------------------------------------------------------------------

class FakeSource:
    def __init__(self, channel: int) -> None:
        self.channel = channel
        self.n = 0

    def latest(self):
        self.n += 1
        img = np.zeros((720, 1280, 3), np.uint8)
        img[:] = (40 + self.channel * 30, 60, 50)
        cv2.putText(img, f"Camera {self.channel} frame {self.n}", (60, 360), cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                    (230, 230, 230), 3)
        x = (self.n * 40) % 1200
        cv2.rectangle(img, (x, 450), (x + 80, 640), (60, 140, 230), -1)
        return img, time.time()


class FakeCam:
    def __init__(self, channel: int) -> None:
        self.channel = channel
        self.source = FakeSource(channel)


class FakeMonitor:
    def __init__(self) -> None:
        self.running = True
        self.message = "Watching 2 cameras"
        self._cams = [FakeCam(1), FakeCam(2)]

    def camera_status(self) -> dict:
        return {1: {"state": "watching", "objects": {"person": 1, "car": 2}, "name": "Front gate"},
                2: {"state": "offline", "objects": {}, "name": "Driveway"}}


class FakeCtx(QObject):
    config_changed = Signal()

    def __init__(self, cfg: Config, store: AlertStore) -> None:
        super().__init__()
        self.cfg = cfg
        self.alert_bridge = AlertBridge()
        self.alert_store = store
        self.monitor = FakeMonitor()
        self.brain = None
        self.saves = 0

    def save(self) -> None:
        self.saves += 1
        self.cfg.save()


class StubHub(QObject):
    """Stand-in for IntegrationHub when the real one cannot be built (core not finished)."""
    incidents_changed = Signal()

    def __init__(self, ctx) -> None:
        super().__init__()
        import concurrent.futures
        self._cf = concurrent.futures
        self.ctx = ctx
        from yardguard.integrations.frames import FrameProvider
        self.frames = FrameProvider(ctx)
        self.handled: list = []

    def set_handlers(self, set_monitoring=None, open_incident=None):
        self._set_mon = set_monitoring

    def request(self, action):
        fut = self._cf.Future()
        if action.kind == "mark_incident":
            self.ctx.alert_store.mark_incident(action.args["incident_id"], action.args["status"])
            fut.set_result({"ok": True, "status": action.args["status"], "title": ""})
        elif action.kind == "set_monitoring":
            self._set_mon(action.args["on"])
            fut.set_result({"ok": True, "running": True, "message": "Monitoring is starting"})
        else:
            fut.set_result({"ok": False, "message": "Not available"})
        return fut

    def shutdown(self):
        self.frames.close()


# -- fixture ------------------------------------------------------------------------------------------

def _jpeg(path: Path, w: int, h: int, color) -> str:
    """A small night-yard scene (sky, ground, a car, a person) so screenshots look plausible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = np.array([min(255, int(v)) for v in color], np.float32)
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    img = (c * 0.6 * (1 - t) + np.array([30, 38, 32], np.float32) * t).repeat(w, axis=1).astype(np.uint8)
    gy = int(h * 0.62)
    cv2.rectangle(img, (0, gy), (w, h), (38, 46, 40), -1)
    cx = int(w * 0.32)
    cv2.rectangle(img, (cx, int(h * 0.48)), (cx + int(w * 0.3), int(h * 0.78)), (95, 92, 88), -1)
    cv2.rectangle(img, (cx + int(w * 0.05), int(h * 0.38)), (cx + int(w * 0.24), int(h * 0.5)), (80, 78, 76), -1)
    for wx in (cx + int(w * 0.06), cx + int(w * 0.24)):
        cv2.circle(img, (wx, int(h * 0.79)), max(4, int(h * 0.06)), (20, 20, 20), -1)
    px = int(w * 0.7)
    cv2.circle(img, (px, int(h * 0.4)), max(3, int(h * 0.04)), (150, 160, 175), -1)
    cv2.rectangle(img, (px - int(w * 0.025), int(h * 0.45)), (px + int(w * 0.025), int(h * 0.75)), (120, 130, 150), -1)
    cv2.rectangle(img, (px - int(w * 0.05), int(h * 0.33)), (px + int(w * 0.05), int(h * 0.8)), (40, 40, 230), 2)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    path.write_bytes(buf.tobytes())
    return str(path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class World:
    """Synthetic config, events folder with incidents / area events, context and hub."""

    def __init__(self, folder: Path, port: int | None = None, demo: bool = False) -> None:
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        self.events = folder / "events"
        self.events.mkdir(parents=True, exist_ok=True)
        cams = [Camera(1, "Front gate", role="Entrance"),
                Camera(2, "Driveway", role="Parking", areas=[
                    new_area("count", "line", [[0.1, 0.5], [0.9, 0.5]], name="Gate line"),
                    new_area("log_plates", "line", [[0.2, 0.6], [0.8, 0.6]], name="Barrier")]),
                Camera(3, "Backyard", enabled=False)]
        site = Site(name="Home", host="", cameras=cams)
        self.cfg = Config(sites=[site], active_site_id=site.id)
        s = self.cfg.settings
        s.events_dir = str(self.events)
        s.install_id = "abcd1234"
        s.remote = {"enabled": True, "port": port or free_port(), "lan": False, "allow_actions": True}
        self.store = AlertStore(str(self.events))
        self.site = site
        self.ctx = FakeCtx(self.cfg, self.store)
        self.incidents = self._fill(demo)
        try:
            from yardguard.integrations import IntegrationHub
            self.hub = IntegrationHub(self.ctx)
            self.real_hub = True
        except Exception:
            traceback.print_exc()
            self.hub = StubHub(self.ctx)
            self.real_hub = False
        self.monitoring_calls: list[bool] = []
        self.hub.set_handlers(set_monitoring=self.monitoring_calls.append, open_incident=lambda _id: None)
        self.web = WebRemote(self.ctx, self.hub)

    def _fill(self, demo: bool) -> list[str]:
        now = time.time()
        pics = self.events / "alerts"
        spec = [  # (minutes ago, channel, camera, title, severity, verdict, findings, with clip)
            (3, 1, "Front gate", "Person near the car at night", "critical", "confirmed", ["hand on door handle"], True),
            (48, 2, "Driveway", "Someone lingering by the cars", "warning", "unverified", [], False),
            (190, 1, "Front gate", "Person in the yard at night", "warning", "rejected", [], False),
            (60 * 26, 2, "Driveway", "Parked car moved", "info", "confirmed", [], False),
        ]
        if demo:
            spec += [(60 * 30 + i * 37, 1 + i % 2, "Front gate" if i % 2 == 0 else "Driveway",
                      "Person near the car at night", "warning", "unverified", [], False) for i in range(6)]
        clip = self.events / "clips" / "clip1.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(bytes(range(256)) * 800)   # 204800 bytes, content known for range checks
        ids = []
        for n, (mins, ch, cam, title, sev, verdict, findings, with_clip) in enumerate(spec):
            for k in range(2 if n == 0 else 1):
                aid = f"a{n}x{k}"
                a = Alert(id=aid, site_id=self.site.id, channel=ch, camera=cam, ts=now - mins * 60 + k * 20,
                          rule="night_presence", title=title, detail="Seen for 12 s near the silver car",
                          severity=sev,
                          snapshot_path=_jpeg(pics / f"{aid}_snap.jpg", 1280, 720, (60 + 20 * n, 70, 40)),
                          crop_path=_jpeg(pics / f"{aid}_crop.jpg", 480, 360, (30, 50 + 25 * n, 90)),
                          verdict=verdict, brain_findings=list(findings),
                          clip_path=str(clip) if with_clip else "", clip_offset_s=4.0 if with_clip else -1.0)
                self.store.add(a)
            ids.append(self.store.get(f"a{n}x0").incident_id)
        if not demo:
            # an alert whose picture path points outside the events folder must never be served
            outside = _jpeg(self.events.parent / "outside.jpg", 64, 64, (0, 0, 255))
            self.store.add(Alert(id="evil", site_id=self.site.id, channel=1, camera="Front gate",
                                 ts=now - 86400 * 5, rule="night_presence", title="Outside", detail="",
                                 severity="info", snapshot_path=outside, crop_path=str(self.events / ".." / "outside.jpg"),
                                 verdict="unverified"))
        cam2 = self.site.camera(2)
        gate, barrier = cam2.areas[0], cam2.areas[1]
        apics = self.events / "areas"
        for i in range(8 if demo else 3):
            ev = AreaEvent(id=f"ae{i}", site_id=self.site.id, channel=2, area_id=barrier["id"], area_name="Barrier",
                           task="log_plates", ts=now - 600 * (i + 1), kind="plate", label="car",
                           value=["AB123CD", "GE-404-XY", "TBS 777"][i % 3],
                           snapshot_path=_jpeg(apics / f"ae{i}_snap.jpg", 1280, 720, (40, 40, 40)),
                           crop_path=_jpeg(apics / f"ae{i}_crop.jpg", 320, 120, (200, 200, 200)),
                           extra={"direction": "in" if i % 2 == 0 else "out"})
            self.store.add_area_event(ev)
        for i in range(5):
            self.store.add_area_event(AreaEvent(id=f"gc{i}", site_id=self.site.id, channel=2, area_id=gate["id"],
                                                area_name="Gate line", task="count", ts=now - 300 * (i + 1),
                                                kind="cross_in" if i % 3 else "cross_out", label="car"))
        return ids

    def close(self) -> None:
        self.web.stop()
        try:
            self.hub.shutdown()
        except Exception:
            traceback.print_exc()
        self.store.close()


# -- HTTP helpers ---------------------------------------------------------------------------------------

def pump_until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


def in_thread(fn, timeout: float = 30.0):
    """Run fn on a thread while the GUI thread keeps processing events (actions need it)."""
    box: dict = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # reported to the caller
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    pump_until(lambda: not t.is_alive(), timeout)
    if "error" in box:
        raise box["error"]
    return box.get("result")


class Resp:
    def __init__(self, status: int, headers, body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def header(self, name: str) -> str:
        return self.headers.get(name, "") or ""

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def req(port: int, method: str, path: str, body=None, headers: dict | None = None, cookie: str = "",
        host: str | None = None, timeout: float = 15) -> Resp:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    hdrs = {"Host": host or f"127.0.0.1:{port}"}
    if cookie:
        hdrs["Cookie"] = cookie
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.update(headers or {})
    try:
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        out = Resp(r.status, r.headers, r.read())
    finally:
        conn.close()
    return out


def pair(w: World, name: str = "Test phone", headers: dict | None = None) -> tuple[str, Resp]:
    code, url = w.web.new_pairing()
    assert "#pair=" in url and code.replace("-", "") in url
    r = req(w.web.remote["port"], "POST", "/api/pair", {"code": code, "name": name}, headers=headers)
    assert r.status == 200, (r.status, r.body)
    cookie = r.header("Set-Cookie").split(";")[0]
    return cookie, r


# -- tests ----------------------------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        def run(*a):
            t0 = time.monotonic()
            try:
                fn(*a)
                RESULTS.append((name, True, f"{time.monotonic() - t0:.2f} s"))
                print(f"PASS {name}")
            except Exception:
                RESULTS.append((name, False, traceback.format_exc()))
                print(f"FAIL {name}\n{traceback.format_exc()}")
        run.__name__ = fn.__name__
        return run
    return wrap


@check("normalize_remote fixes wrong values")
def t_normalize():
    d = normalize_remote({"enabled": "yes", "port": 80, "lan": 1, "live_fps": "fast", "public_url": "http://x",
                          "devices": [{"name": "x" * 99, "token_sha256": "a" * 64}, {"token_sha256": "bad"}, 3]})
    assert d["enabled"] is False and d["lan"] is False and d["port"] == 8765 and d["live_fps"] == 2
    assert d["public_url"] == "" and len(d["devices"]) == 1 and len(d["devices"][0]["name"]) == 40
    assert normalize_remote(None)["enabled"] is False


@check("off by default: nothing listens")
def t_off_by_default():
    w = World(OUT / "w_off")
    try:
        w.cfg.settings.remote = {}
        web = WebRemote(w.ctx, w.hub)
        assert web.remote["enabled"] is False
        web.start()
        assert not web.running and web._server is None
        try:
            socket.create_connection(("127.0.0.1", web.remote["port"]), timeout=0.5).close()
            # something else may use 8765 on this PC: only fail when it is our server
            assert web._server is None
        except OSError:
            pass
        web.stop()
    finally:
        w.close()


@check("binds 127.0.0.1 by default and 0.0.0.0 with lan")
def t_bind():
    w = World(OUT / "w_bind")
    seen = []
    real = srv._Server

    class Spy(real):
        def __init__(self, addr, handler, remote):
            seen.append(addr[0])
            super().__init__(("127.0.0.1", addr[1]), handler, remote)   # never really open the LAN in tests

    srv._Server = Spy
    try:
        w.web.start()
        assert w.web.running and seen[-1] == "127.0.0.1"
        assert w.web._server.server_address[0] == "127.0.0.1"
        w.cfg.settings.remote["lan"] = True
        w.web.apply_settings()
        assert w.web.running and seen[-1] == "0.0.0.0", seen
        urls = w.web.urls()
        assert urls[-1].startswith("http://127.0.0.1:")
        w.cfg.settings.remote["enabled"] = False
        w.web.apply_settings()
        assert not w.web.running
    finally:
        srv._Server = real
        w.close()


@check("port in use -> plain error")
def t_port_busy():
    w = World(OUT / "w_busy")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        w.cfg.settings.remote["port"] = blocker.getsockname()[1]
        w.web.start()
        assert not w.web.running and "used by another program" in w.web.error, w.web.error
    finally:
        blocker.close()
        w.close()


def started_world(name: str) -> World:
    w = World(OUT / name)
    w.web.start()
    assert w.web.running, w.web.error
    return w


@check("static files, 404, traversal and security headers")
def t_static():
    w = started_world("w_static")
    p = w.web.remote["port"]
    try:
        r = req(p, "GET", "/")
        assert r.status == 200 and b"/app.js" in r.body and b"/app.css" in r.body
        for k, v in srv.SECURITY_HEADERS:
            assert r.header(k) == v, (k, r.header(k))
        assert req(p, "GET", "/app.js").header("Content-Type").startswith("text/javascript")
        assert req(p, "GET", "/manifest.webmanifest").json()["display"] == "standalone"
        assert req(p, "GET", "/icon-192.png").body[:4] == b"\x89PNG"
        assert req(p, "HEAD", "/app.css").body == b""
        cookie, _ = pair(w)
        for path in ("/nope", "/../config.json", "/web/app.js", "/app.js/../../config.json", "/%2e%2e/config.json",
                     "/media/alert/../../config.json/snapshot", "/media/alert/..%2F..%2Fx/snapshot",
                     "/media/alert/evil/snapshot", "/media/alert/evil/crop", "/media/alert/missing/snapshot",
                     "/media/alert/a0x0/raw", "/live/99.mjpg", "/live/3.jpg", "/api/incidents/..",
                     "/api/area_events?area=../x"):
            r = req(p, "GET", path, cookie=cookie)
            assert r.status == 404, (path, r.status)
            assert r.header("X-Frame-Options") == "DENY" and r.json()["error"] == "not_found"
            assert b"config" not in r.body
        r = req(p, "GET", "/", host="evil.example.com")
        assert r.status == 403 and r.json()["error"] == "host"
        assert req(p, "GET", "/", host=f"localhost:{p}").status == 200
        assert req(p, "GET", "/", host="pc.tail1234.ts.net").status == 200
    finally:
        w.close()


@check("pairing: single use, expiry, cookie flags, rate limit and lockout")
def t_pairing():
    w = started_world("w_pair")
    p = w.web.remote["port"]
    try:
        code, _ = w.web.new_pairing()
        r = req(p, "POST", "/api/pair", {"code": code.lower(), "name": "Alex's iPhone\n<b>"})
        assert r.status == 200, r.body
        sc = r.header("Set-Cookie")
        assert sc.startswith("yg_abcd1234=") and "HttpOnly" in sc and "SameSite=Strict" in sc
        assert "Path=/" in sc and "Max-Age=31536000" in sc and "Secure" not in sc
        assert r.json()["device"]["name"] == "Alex's iPhone <b>"
        r2 = req(p, "POST", "/api/pair", {"code": code, "name": "again"})
        assert r2.status == 403 and r2.json()["error"] == "code", "codes are single use"
        # Secure only for https forwarded by Tailscale serve on this PC
        _c, r3 = pair(w, headers={"X-Forwarded-Proto": "https"})
        assert "Secure" in r3.header("Set-Cookie")
        # expiry
        clock = [1000.0]
        w.web.codes = PairingCodes(clock=lambda: clock[0])
        code, _ = w.web.new_pairing(ttl=300)
        clock[0] += 301
        assert req(p, "POST", "/api/pair", {"code": code, "name": "late"}).status == 403
        # wrong codes: 5 tries a minute, then "slow"; 10 failures lock pairing for 15 min
        lclock = [5000.0]
        w.web.limiter = PairLimiter(clock=lambda: lclock[0])
        w.web.codes = PairingCodes()
        statuses = [req(p, "POST", "/api/pair", {"code": "AAAA-AAAA", "name": "x"}).status for _ in range(6)]
        assert statuses == [403] * 5 + [429], statuses
        lclock[0] += 61
        for _ in range(5):
            assert req(p, "POST", "/api/pair", {"code": "AAAA-AAAA", "name": "x"}).status == 403
        lclock[0] += 61
        good, _ = w.web.new_pairing()
        r = req(p, "POST", "/api/pair", {"code": good, "name": "x"})
        assert r.status == 429 and r.json()["error"] == "locked", (r.status, r.body)
        lclock[0] += 15 * 60 + 1
        assert req(p, "POST", "/api/pair", {"code": good, "name": "x"}).status == 200
        # bad bodies
        assert req(p, "POST", "/api/pair", b"code=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status == 400
        assert req(p, "POST", "/api/pair", b"x" * 10000).status == 413
        assert req(p, "POST", "/api/pair", {"code": "x"}, headers={"Origin": "https://evil.example"}).status == 403
        # the config keeps only hashes; saves happen on the GUI thread
        pump_until(lambda: (OUT / "config.json").exists() and "yg_" not in (OUT / "config.json").read_text())
        pump_until(lambda: len(json.loads((OUT / "config.json").read_text())["settings"]["remote"]["devices"]) >= 3)
        saved = json.loads((OUT / "config.json").read_text())["settings"]["remote"]["devices"]
        assert len(saved) >= 3 and all(len(d["token_sha256"]) == 64 for d in saved)
        token = sc.split(";")[0].split("=", 1)[1]
        assert token not in (OUT / "config.json").read_text() and token not in LOG.getvalue()
        assert code.replace("-", "") not in LOG.getvalue()
    finally:
        w.close()


@check("every API needs a paired device; removed device -> 401")
def t_auth():
    w = started_world("w_auth")
    p = w.web.remote["port"]
    try:
        paths = ["/api/status", "/api/incidents", f"/api/incidents/{w.incidents[0]}", "/api/areas",
                 "/api/area_events", "/api/changes?since=0&wait=0", "/media/alert/a0x0/snapshot",
                 "/media/area/ae0/crop", "/live/1.jpg", "/live/1.mjpg"]
        for path in paths:
            r = req(p, "GET", path)
            assert r.status == 401 and r.json() == {"error": "pair"}, (path, r.status)
            r = req(p, "GET", path, cookie="yg_abcd1234=forged-token-value")
            assert r.status == 401, path
        for path in (f"/api/incidents/{w.incidents[0]}/mark", "/api/monitoring", "/api/logout"):
            assert req(p, "POST", path, {"status": "handled", "on": True}).status == 401, path
        cookie, r = pair(w)
        dev_id = r.json()["device"]["id"]
        assert req(p, "GET", "/api/status", cookie=cookie).status == 200
        assert w.web.connected_count() == 1
        w.web.remove_device(dev_id)
        assert req(p, "GET", "/api/status", cookie=cookie).status == 401
        # logout from the phone removes it too
        cookie, r = pair(w)
        lo = req(p, "POST", "/api/logout", {}, cookie=cookie, headers={"Origin": f"http://127.0.0.1:{p}"})
        assert lo.status == 200 and "Max-Age=0" in lo.header("Set-Cookie")
        assert req(p, "GET", "/api/status", cookie=cookie).status == 401
        assert w.web.devices() == [d for d in w.web.devices() if "token_sha256" not in d]
    finally:
        w.close()


@check("JSON shapes: status, incidents, detail, areas, area events")
def t_json():
    w = started_world("w_json")
    p = w.web.remote["port"]
    try:
        cookie, _ = pair(w, "Alex")
        st = req(p, "GET", "/api/status", cookie=cookie)
        assert st.header("Cache-Control") == "no-store"
        st = st.json()
        for k in ("site", "monitoring", "cameras", "counts", "unread", "allow_actions", "csrf", "server_time", "version",
                  "newest_ts", "device"):
            assert k in st, k
        assert st["site"] == "Home" and st["monitoring"]["running"] is True and st["device"]["name"] == "Alex"
        assert [c["channel"] for c in st["cameras"]] == [1, 2], "disabled cameras are not listed"
        c1 = st["cameras"][0]
        assert c1["state"] == "watching" and c1["people"] == 1 and c1["vehicles"] == 2 and c1["name"] == "Front gate"
        assert st["cameras"][1]["state_text"] == "No video"
        assert st["allow_actions"] is True and len(st["csrf"]) == 64
        inc = req(p, "GET", "/api/incidents?limit=2", cookie=cookie).json()
        assert len(inc["items"]) == 2 and inc["more"] is True
        first = inc["items"][0]
        for k in ("id", "title", "severity", "verdict", "status", "start_ts", "end_ts", "cameras", "alert_count", "cover",
                  "has_crop", "has_snapshot", "has_clip"):
            assert k in first, k
        assert first["title"].startswith("Person") and first["alert_count"] == 2 and first["has_clip"], first
        assert first["cameras"] == ["Front gate"] and first["cover"] == "a0x0" and first["severity"] == "critical"
        assert "path" not in json.dumps(inc), "no file paths leave the PC"
        older = req(p, "GET", f"/api/incidents?limit=30&before={inc['items'][1]['start_ts']}", cookie=cookie).json()
        assert all(i["start_ts"] < inc["items"][1]["start_ts"] for i in older["items"]) and older["items"]
        ids = [i["id"] for i in inc["items"]] + [i["id"] for i in older["items"]]
        assert len(ids) == len(set(ids))
        open_ids = [i["id"] for i in req(p, "GET", "/api/incidents?status=open", cookie=cookie).json()["items"]]
        assert w.incidents[2] not in open_ids and w.incidents[0] in open_ids, "AI-dismissed are not 'open' here"
        det = req(p, "GET", f"/api/incidents/{first['id']}", cookie=cookie).json()
        assert len(det["alerts"]) == 2 and det["alerts"][0]["brain_findings"] == ["hand on door handle"]
        assert det["alerts"][0]["clip_offset_s"] == 4.0 and det["alerts"][0]["has_crop"]
        ar = req(p, "GET", "/api/areas", cookie=cookie).json()
        names = {a["name"]: a for a in ar["items"]}
        assert set(names) == {"Gate line", "Barrier"}
        assert names["Barrier"]["headline"] == "3 plates today" or names["Barrier"]["total"] >= 1, names["Barrier"]
        assert names["Gate line"]["task_title"] == "Count" and names["Gate line"]["camera"] == "Driveway"
        aid = names["Barrier"]["id"]
        ev = req(p, "GET", f"/api/area_events?area={aid}&limit=2", cookie=cookie).json()
        assert len(ev["items"]) == 2 and ev["more"] is True
        e0 = ev["items"][0]
        assert e0["kind"] == "plate" and e0["value"] == "AB123CD" and e0["kind_text"] == "Number plate"
        assert e0["camera"] == "Driveway" and e0["has_crop"] and e0["direction"] == "in"
        ev2 = req(p, "GET", f"/api/area_events?area={aid}&before={ev['items'][1]['ts']}", cookie=cookie).json()
        assert len(ev2["items"]) == 1 and ev2["more"] is False
    finally:
        w.close()


@check("changes long poll returns within 1 s of a new incident")
def t_changes():
    w = started_world("w_changes")
    p = w.web.remote["port"]
    try:
        cookie, _ = pair(w)
        first = req(p, "GET", f"/api/changes?since={2**53}&wait=25", cookie=cookie).json()
        assert first["reset"] is True and first["items"] == []
        seq = first["seq"]
        box = {}

        def poll():
            t0 = time.monotonic()
            box["r"] = req(p, "GET", f"/api/changes?since={seq}&wait=25", cookie=cookie, timeout=30).json()
            box["t"] = time.monotonic() - t0

        th = threading.Thread(target=poll, daemon=True)
        th.start()
        time.sleep(0.3)
        t_emit = time.monotonic()
        inc = w.store.get_incident(w.incidents[1])
        w.ctx.alert_bridge.incident_added.emit(inc)
        pump_until(lambda: not th.is_alive(), 5)
        assert "r" in box and time.monotonic() - t_emit < 1.5, box
        items = box["r"]["items"]
        assert items and items[0]["type"] == "incident" and items[0]["id"] == inc.id
        # status changes are throttled; nothing new -> empty answer after the wait
        r = req(p, "GET", f"/api/changes?since={box['r']['seq']}&wait=0.2", cookie=cookie).json()
        assert r["items"] == [] and r["reset"] is False
    finally:
        w.close()


@check("media: JPEG, ?w= thumbnail, clip ranges")
def t_media():
    w = started_world("w_media")
    p = w.web.remote["port"]
    try:
        cookie, _ = pair(w)
        r = req(p, "GET", "/media/alert/a0x0/snapshot", cookie=cookie)
        assert r.status == 200 and r.header("Content-Type") == "image/jpeg" and r.body[:2] == b"\xff\xd8"
        img = cv2.imdecode(np.frombuffer(r.body, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[:2] == (720, 1280)
        r = req(p, "GET", "/media/alert/a0x0/snapshot?w=240", cookie=cookie)
        img = cv2.imdecode(np.frombuffer(r.body, np.uint8), cv2.IMREAD_COLOR)
        assert max(img.shape[:2]) <= 240, img.shape
        r2 = req(p, "GET", "/media/alert/a0x0/snapshot?w=240", cookie=cookie)
        assert r2.body == r.body and len(w.web._thumbs) >= 1
        assert req(p, "GET", "/media/area/ae1/crop?w=100", cookie=cookie).status == 200
        clip = w.events / "clips" / "clip1.mp4"
        data = clip.read_bytes()
        full = req(p, "GET", "/media/alert/a0x0/clip", cookie=cookie)
        assert full.status == 200 and full.body == data and full.header("Accept-Ranges") == "bytes"
        assert full.header("Content-Type") == "video/mp4"
        r = req(p, "GET", "/media/alert/a0x0/clip", cookie=cookie, headers={"Range": "bytes=100-199"})
        assert r.status == 206 and r.body == data[100:200] and r.header("Content-Range") == f"bytes 100-199/{len(data)}"
        r = req(p, "GET", "/media/alert/a0x0/clip", cookie=cookie, headers={"Range": "bytes=204700-"})
        assert r.status == 206 and r.body == data[204700:]
        r = req(p, "GET", "/media/alert/a0x0/clip", cookie=cookie, headers={"Range": "bytes=-50"})
        assert r.status == 206 and r.body == data[-50:] and r.header("Content-Range") == f"bytes {len(data) - 50}-{len(data) - 1}/{len(data)}"
        r = req(p, "GET", "/media/alert/a0x0/clip", cookie=cookie, headers={"Range": "bytes=999999-"})
        assert r.status == 416 and r.header("Content-Range") == f"bytes */{len(data)}"
        assert req(p, "GET", "/media/alert/a1x0/clip", cookie=cookie).status == 404   # no clip
        # keep-alive: two requests on one connection
        conn = http.client.HTTPConnection("127.0.0.1", p, timeout=10)
        for _ in range(2):
            conn.request("GET", "/media/alert/a0x0/crop", headers={"Cookie": cookie, "Host": f"127.0.0.1:{p}"})
            resp = conn.getresponse()
            assert resp.status == 200 and resp.read()[:2] == b"\xff\xd8"
        conn.close()
    finally:
        w.close()


def read_parts(sock_file, n: int) -> list[bytes]:
    parts = []
    while len(parts) < n:
        line = sock_file.readline()
        if not line:
            break
        if line.strip() == b"--ygframe":
            headers = {}
            while True:
                hl = sock_file.readline().strip()
                if not hl:
                    break
                k, _, v = hl.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            assert headers["content-type"] == "image/jpeg"
            parts.append(sock_file.read(int(headers["content-length"])))
    return parts


def open_stream(port: int, cookie: str, channel: int = 1):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(f"GET /live/{channel}.mjpg HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\n\r\n".encode())
    f = s.makefile("rb")
    status = f.readline()
    headers = {}
    while True:
        line = f.readline().strip()
        if not line:
            break
        k, _, v = line.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    return s, f, status, headers


@check("live: MJPEG frames, still picture, viewer limits, stop < 2 s with a viewer")
def t_live():
    w = started_world("w_live")
    p = w.web.remote["port"]
    try:
        w.cfg.settings.remote["live_fps"] = 8
        cookie, _ = pair(w)
        s, f, status, headers = open_stream(p, cookie)
        assert b"200" in status and headers["content-type"] == "multipart/x-mixed-replace; boundary=ygframe", headers
        parts = read_parts(f, 3)
        assert len(parts) == 3 and all(x[:2] == b"\xff\xd8" for x in parts)
        img = cv2.imdecode(np.frombuffer(parts[-1], np.uint8), cv2.IMREAD_COLOR)
        assert max(img.shape[:2]) <= 960, "live frames are shrunk to live_max_side"
        assert len(set(parts)) == 3, "frames change"
        r = req(p, "GET", "/live/1.jpg", cookie=cookie)
        assert r.status == 200 and r.body[:2] == b"\xff\xd8"
        # limits: one viewer per camera here
        w.web.live.max_per_camera = 1
        r = req(p, "GET", "/live/1.mjpg", cookie=cookie)
        assert r.status == 503 and r.json()["error"] == "busy"
        w.web.live.max_total = 1
        assert req(p, "GET", "/live/2.mjpg", cookie=cookie).status == 503
        assert req(p, "GET", "/live/2.jpg", cookie=cookie).status in (200, 503)   # stills never take a slot
        # removing the device ends its stream within ~5 s
        assert w.web.live.viewers() == 1
        t0 = time.monotonic()
        w.web.stop()
        took = time.monotonic() - t0
        assert took < 2.0, took
        rest = f.read()
        assert rest is not None
        s.close()
        assert not w.web.running
        pump_until(lambda: not [t for t in threading.enumerate() if t.name.startswith(("yg-web", "yg-live"))], 3)
        left = [t.name for t in threading.enumerate() if t.name.startswith(("yg-web", "yg-live"))]
        assert not left, left
    finally:
        w.close()


@check("live stream ends after the time limit and when the device is removed")
def t_live_end():
    w = started_world("w_live_end")
    p = w.web.remote["port"]
    try:
        w.cfg.settings.remote["live_fps"] = 10
        w.web.live_max_s = 1.0
        cookie, r = pair(w)
        s, f, status, _ = open_stream(p, cookie)
        t0 = time.monotonic()
        data = f.read()   # until the server closes
        assert time.monotonic() - t0 < 4 and data.rstrip().endswith(b"--ygframe--"), data[-40:]
        s.close()
        w.web.live_max_s = 60
        s, f, status, _ = open_stream(p, cookie)
        read_parts(f, 1)
        w.web.remove_device(r.json()["device"]["id"])
        t0 = time.monotonic()
        f.read()
        assert time.monotonic() - t0 < 7
        s.close()
        pump_until(lambda: w.web.live.viewers() == 0, 3)
        assert w.web.live.viewers() == 0
    finally:
        w.close()


@check("actions: allow switch, CSRF, Origin; accepted through the hub")
def t_actions():
    w = started_world("w_actions")
    p = w.web.remote["port"]
    origin = {"Origin": f"http://127.0.0.1:{p}"}
    try:
        cookie, _ = pair(w, "Alex")
        st = in_thread(lambda: req(p, "GET", "/api/status", cookie=cookie).json())
        csrf = st["csrf"]
        inc_id = w.incidents[1]
        path = f"/api/incidents/{inc_id}/mark"
        ok_headers = {**origin, "X-YG-CSRF": csrf}
        w.cfg.settings.remote["allow_actions"] = False
        r = req(p, "POST", path, {"status": "handled"}, cookie=cookie, headers=ok_headers)
        assert r.status == 403 and r.json()["error"] == "actions_off"
        assert req(p, "GET", "/api/status", cookie=cookie).json()["csrf"] == "", "no token while actions are off"
        w.cfg.settings.remote["allow_actions"] = True
        r = req(p, "POST", path, {"status": "handled"}, cookie=cookie, headers=origin)
        assert r.status == 403 and r.json()["error"] == "csrf"
        r = req(p, "POST", path, {"status": "handled"}, cookie=cookie, headers={**ok_headers, "X-YG-CSRF": "0" * 64})
        assert r.status == 403 and r.json()["error"] == "csrf"
        r = req(p, "POST", path, {"status": "handled"}, cookie=cookie, headers={"X-YG-CSRF": csrf})
        assert r.status == 403 and r.json()["error"] == "origin", "Origin is required for actions"
        r = req(p, "POST", path, {"status": "handled"}, cookie=cookie,
                headers={"X-YG-CSRF": csrf, "Origin": "https://evil.example"})
        assert r.status == 403 and r.json()["error"] == "origin"
        r = req(p, "POST", path, {"status": "nonsense"}, cookie=cookie, headers=ok_headers)
        assert r.status == 400
        r = req(p, "POST", "/api/incidents/nope/mark", {"status": "handled"}, cookie=cookie, headers=ok_headers)
        assert r.status == 404
        assert w.store.get_incident(inc_id).status == "open"
        changed = []
        if hasattr(w.hub, "incidents_changed"):
            w.hub.incidents_changed.connect(lambda: changed.append(1))
        r = in_thread(lambda: req(p, "POST", path, {"status": "handled"}, cookie=cookie, headers=ok_headers))
        assert r.status == 200 and r.json()["ok"] is True, (r.status, r.body)
        assert w.store.get_incident(inc_id).status == "handled"
        if w.real_hub:
            assert changed, "the desktop is told (incidents_changed)"
        r = in_thread(lambda: req(p, "POST", "/api/monitoring", {"on": False}, cookie=cookie, headers=ok_headers))
        assert r.status == 200 and w.monitoring_calls == [False], (r.status, r.body, w.monitoring_calls)
        r = req(p, "POST", "/api/monitoring", {"on": "yes"}, cookie=cookie, headers=ok_headers)
        assert r.status == 400
    finally:
        w.close()


@check("tailscale helpers with a fake CLI")
def t_tailscale():
    folder = OUT / "fake_tailscale"
    folder.mkdir(parents=True, exist_ok=True)
    status_json = {"BackendState": "Running", "Self": {"DNSName": "pc.tail1234.ts.net.", "Online": True}}
    (folder / "status.json").write_text(json.dumps(status_json), encoding="utf-8")
    cli = folder / "tailscale.cmd"
    cli.write_text("@echo off\r\n"
                   "if \"%1\"==\"status\" type \"%~dp0status.json\" & exit /b 0\r\n"
                   "if \"%1\"==\"serve\" echo %* > \"%~dp0serve_args.txt\" & exit /b 0\r\n"
                   "exit /b 1\r\n", encoding="ascii")
    st = tailscale.status(str(cli))
    assert st["installed"] and st["running"] and st["dns_name"] == "pc.tail1234.ts.net" and not st["error"], st
    ok, url = tailscale.serve_on(8765, str(cli))
    assert ok and url == "https://pc.tail1234.ts.net", (ok, url)
    args = (folder / "serve_args.txt").read_text()
    assert "--bg" in args and "--https=443" in args and "http://127.0.0.1:8765" in args, args
    assert tailscale.serve_off(str(cli)) == (True, "")
    # other states through a patched subprocess.run
    real = subprocess.run

    def fake_run(answer, code=0):
        def run(*a, **k):
            return subprocess.CompletedProcess(a[0], code, stdout=answer, stderr="")
        return run

    try:
        subprocess.run = fake_run(json.dumps({"BackendState": "NeedsLogin", "Self": {}}))
        st = tailscale.status("tailscale.exe")
        assert not st["running"] and "sign in" in st["error"]
        assert tailscale.serve_on(8765, "tailscale.exe")[0] is False
        subprocess.run = fake_run("", 1)
        assert "not running" in tailscale.status("tailscale.exe")["error"]

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("tailscale", 5)
        subprocess.run = boom
        assert "did not answer" in tailscale.status("tailscale.exe")["error"]
    finally:
        subprocess.run = real
    assert tailscale.status(str(folder / "missing.exe"))["error"]


@check("set_public_url and pairing link")
def t_public_url():
    w = World(OUT / "w_url")
    try:
        w.web.set_public_url("https://pc.tail1234.ts.net/")
        assert w.web.remote["public_url"] == "https://pc.tail1234.ts.net"
        code, url = w.web.new_pairing()
        assert url == f"https://pc.tail1234.ts.net/#pair={code.replace('-', '')}"
        assert w.web.urls()[0] == "https://pc.tail1234.ts.net"
        w.web.set_public_url("http://plain")
        assert w.web.remote["public_url"] == ""
    finally:
        w.close()


@check("static app sanity: no external URLs, small, routes present")
def t_static_app():
    web = ROOT / "yardguard" / "web"
    total = 0
    for name in ("index.html", "app.js", "app.css", "manifest.webmanifest"):
        text = (web / name).read_text(encoding="utf-8")
        total += len(text.encode("utf-8"))
        cleaned = text.replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in cleaned and "https://" not in cleaned and "//cdn" not in cleaned, name
        for bad in (chr(0xC2), chr(0xE2) + chr(0x20AC), chr(0xC3)):   # mojibake markers
            assert bad not in text, (name, bad)
    assert total < 60_000, total
    js = (web / "app.js").read_text(encoding="utf-8")
    for route in ("#/cameras", "#/incidents", "#/incident/", "#/areas", "#/area/", "#/pair", "/api/changes",
                  "X-YG-CSRF", "visibilitychange"):
        assert route in js, route
    assert "innerHTML = ICONS" in js and js.count("innerHTML") == 1, "only constant icon markup uses innerHTML"
    for f in ("icon-192.png", "icon-512.png", "apple-touch-icon.png", "sample.jpg"):
        assert (web / f).stat().st_size > 1000, f
    assert set(n for n, *_ in srv.STATIC.values()) <= {p.name for p in web.iterdir()}


@check("no yg- threads and no secrets in the log")
def t_cleanup():
    left = [t.name for t in threading.enumerate() if t.name.startswith(("yg-web", "yg-live"))]
    assert not left, left
    text = LOG.getvalue()
    assert "token_sha256" not in text and "Cookie" not in text


def run_tests() -> int:
    t_normalize()
    t_off_by_default()
    t_bind()
    t_port_busy()
    t_static()
    t_pairing()
    t_auth()
    t_json()
    t_changes()
    t_media()
    t_live()
    t_live_end()
    t_actions()
    t_tailscale()
    t_public_url()
    t_static_app()
    t_cleanup()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


def serve(port: int = 8799) -> None:
    """Demo server for screenshots: prints the pairing link, runs until the process is stopped."""
    w = World(OUT / "demo", port=port, demo=True)
    w.web.start()
    if not w.web.running:
        print("could not start:", w.web.error)
        return
    code, url = w.web.new_pairing(ttl=3600)
    (OUT / "demo_pair.txt").write_text(url, encoding="utf-8")
    print("pair link written to", OUT / "demo_pair.txt", flush=True)
    end = time.monotonic() + float(os.environ.get("YG_DEMO_SECONDS", "1800"))
    while time.monotonic() < end and not (OUT / "demo_stop").exists():
        app.processEvents()
        time.sleep(0.02)
    w.close()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
        sys.exit(0)
    sys.exit(run_tests())
