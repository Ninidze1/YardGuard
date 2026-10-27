"""End-to-end check of the integrations wired into the real main window (offscreen, local fakes only).

Builds AppContext + MainWindow on a synthetic, older-version config with five destinations (Telegram,
ntfy, email, webhook, Home Assistant/MQTT) and phone access on 127.0.0.1, then checks:
- nav order (Integrations after Areas), Ctrl+1..7 shortcuts, About text, the page, config migration;
- incidents from the monitor callbacks reach the fakes according to each destination's routing
  (minimum level, AI-confirmed only, quiet hours sent silently, per-area rules);
- a Telegram "False alarm" button marks the incident in the store and on the Incidents page, and the
  webhook hears the status change; marking on the desktop edits the Telegram message;
- /arm and /disarm (with its confirm button) switch monitoring like the sidebar switch;
- Home Assistant discovery, availability and monitoring state on the MQTT stub;
- phone access: auth rejection, pairing cookie, cameras list, incidents JSON, MJPEG frames, picture,
  marking from the phone, brute-force slow-down and lockout;
- the Telegram setup dialog end to end against a second fake bot (token check, pairing, test photo,
  Finish -> a new running destination);
- screenshots of the Integrations page (3 tabs) and every setup dialog in light and dark, with no
  clipped text and no horizontal scrollbar;
- closing the window stops the phone server and the hub, with no "yg-" thread left and no secret
  in the log.

    run.cmd tests\\test_integrations_e2e.py
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "integrations" / "integrator"
SHOTS = OUT / "shots"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ["TEMP"] = os.environ["TMP"] = str(ROOT / ".cache" / "tmp")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
if OUT.exists():
    shutil.rmtree(OUT)
SHOTS.mkdir(parents=True)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QSettings, Qt, QThreadPool  # noqa: E402
from PySide6.QtGui import QFont, QShortcut  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QApplication, QCheckBox, QLabel, QPushButton, QRadioButton, QScrollArea, QWidget,
)

from yardguard import config  # noqa: E402
from yardguard.config import Camera, Config, Site  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never use the real config.json"
import fakes_integrations as fi  # noqa: E402
from yardguard.integrations import base  # noqa: E402

LOG = io.StringIO()
_handler = logging.StreamHandler(LOG)
_handler.setLevel(logging.DEBUG)
logging.getLogger("yardguard").addHandler(_handler)
logging.getLogger("yardguard").setLevel(logging.DEBUG)

failures: list[str] = []


def check(name: str, ok, detail="") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(name)


app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))


def pump(seconds: float = 0.2) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, timeout: float = 8.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        value = cond()
        if value:
            return value
        time.sleep(0.02)
    return cond()


def in_thread(fn):
    """Run a blocking call off the GUI thread while the GUI keeps working (remote actions need it)."""
    box: list = []
    t = threading.Thread(target=lambda: box.append(fn()), name="test-client", daemon=True)
    t.start()
    while t.is_alive():
        app.processEvents()
        time.sleep(0.01)
    return box[0] if box else None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- fakes and the synthetic config -----------------------------------------------------------------

tg = fi.FakeTelegram()
TOKEN = tg.token
tg2 = fi.FakeTelegram(token="7000000002:BBHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ")   # for the setup dialog
ntfy = fi.FakeHttpSink()
hook = fi.FakeHttpSink()
smtp = fi.FakeSmtp(users={"cam@example.com": "app-pass-123"})
broker = fi.FakeMqttBroker(users={"ha": "mqtt-pass-456"})
HOOK_URL = hook.url + "/hook/abc123secretpath"
SECRETS = [TOKEN, tg2.token, "app-pass-123", "mqtt-pass-456", "abc123secretpath", "s3cr3t-hmac"]
WEB_PORT = free_port()
CHAT = 1001

cfg = Config()
cam1, cam2 = Camera(1, "Front gate"), Camera(2, "Driveway")
area_a = config.new_area("presence", "polygon", [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]], "Front path", alert=False)
area_b = config.new_area("presence", "polygon", [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9]], "Side path", alert=False)
cam1.areas = [area_a, area_b]
site = Site(name="Home", host="192.0.2.10", username="admin", cameras=[cam1, cam2])
cfg.sites.append(site)
cfg.active_site_id = site.id
s = cfg.settings
s.events_dir = str(OUT / "events")
s.brain_mode = "verify"
s.monitoring_on = False
s.theme = "light"
now_t = datetime.now()
quiet = {"enabled": True, "start": (now_t - timedelta(hours=1)).strftime("%H:%M"),
         "end": (now_t + timedelta(hours=1)).strftime("%H:%M"), "days": list(range(7)), "mode": "silent",
         "critical_through": True}


def dest(kind: str, name: str, params: dict, secrets: dict, routes: dict) -> dict:
    d = base.new_integration(kind, name)
    d["params"].update(params)
    for key, section in routes.items():
        if isinstance(section, dict) and isinstance(d["routes"].get(key), dict):
            d["routes"][key].update(section)
        else:
            d["routes"][key] = section
    conf = base.IntegrationConf(d)
    for k, v in secrets.items():
        conf.set_secret(k, v)
    return d


fast = {"cooldown_s": 0, "max_per_10min": 100}
d_tg = dest("telegram", "Telegram", {"api_base": tg.api_base, "bot_username": "MyYardBot", "allow_control": True,
                                     "chats": [{"id": CHAT, "title": "Alex", "type": "private", "enabled": True}]},
            {"token": TOKEN}, {"rate": fast, "incidents": {"min_severity": "warning", "confirmed_only": True}})
d_ntfy = dest("ntfy", "ntfy", {"server": ntfy.url, "topic": "yardguard-e2e-topic-4f7a9c2b81d3"}, {},
              {"rate": fast, "quiet": quiet,
               "incidents": {"min_severity": "warning", "confirmed_only": False}})
d_mail = dest("email", "Email", {"preset": "other", "host": "127.0.0.1", "port": smtp.port, "security": "none",
                                 "username": "cam@example.com", "sender": "cam@example.com", "to": ["me@example.com"]},
              {"password": "app-pass-123"},
              {"rate": fast, "summary": {"enabled": False},
               "incidents": {"min_severity": "critical", "confirmed_only": False},
               "areas": {area_a["id"]: {"kinds": [], "mode": "instant"}}})
d_hook = dest("webhook", "Webhook", {}, {"url": HOOK_URL, "hmac_secret": "s3cr3t-hmac"},
              {"rate": fast, "incidents": {"min_severity": "info", "confirmed_only": True}})
d_mqtt = dest("mqtt", "Home Assistant", {"host": "127.0.0.1", "port": broker.port, "username": "ha"},
              {"password": "mqtt-pass-456"}, {"rate": fast})
s.integrations = [d_tg, d_ntfy, d_mail, d_hook, d_mqtt]
s.remote = {"enabled": True, "lan": False, "port": WEB_PORT, "allow_actions": True}
cfg.save()
raw = json.loads((OUT / "config.json").read_text(encoding="utf-8"))
raw["settings"].pop("install_id", None)      # an older file: the migration must make one
raw["settings"]["config_version"] = 3
(OUT / "config.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
check("no secret in the saved config", not any(x in (OUT / "config.json").read_text(encoding="utf-8")
                                                 for x in SECRETS))

# --- the app -------------------------------------------------------------------------------------------------

from yardguard import integrations_setup as isu  # noqa: E402
from yardguard.gui import AppContext, MainWindow  # noqa: E402
from yardguard.integrations import IntegrationHub  # noqa: E402
from yardguard.webremote import WebRemote  # noqa: E402
from yardguard.webremote.auth import PairLimiter  # noqa: E402

isu.confirm = lambda *a, **k: True    # never a modal message box in this test


class StubBrain:
    """Never loads a model."""

    def __init__(self):
        self.state, self.user_stopped, self.last_error, self.install_step = "stopped", False, None, None

    def status(self):
        return self.state

    def start(self, user=False):
        self.state = "starting"

    def stop(self, user=False):
        self.state = "stopped"

    def stats(self):
        return {}

    def ask(self, *a, **k):
        return []

    def cancel_install(self):
        pass


class CountingSource(fi.FakeSource):
    """A camera whose every picture is different (the live encoder skips repeated frames)."""

    n = 0

    def latest(self):
        got = super().latest()
        if got is None:
            return None
        CountingSource.n += 1
        frame = got[0].copy()
        cv2.putText(frame, str(CountingSource.n), (40, 400), cv2.FONT_HERSHEY_SIMPLEX, 6, (255, 255, 0), 8)
        return frame, time.time()


ctx = AppContext(app)
ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)
ctx.brain = ctx.monitor.brain = StubBrain()
mon = ctx.monitor
mon.start = lambda: setattr(mon, "_running", True)      # no cameras, no AI models: just the switch
mon.stop = lambda: setattr(mon, "_running", False)
hub = ctx.integrations
check("AppContext made the hub and the phone server", isinstance(hub, IntegrationHub) and isinstance(ctx.web, WebRemote))
hub.frames.source_factory = lambda url, fps, side, gpu: CountingSource(url, fps, side, gpu)
store = ctx.alert_store

st = ctx.cfg.settings
check("migration made an install id", len(st.install_id) == 8 and st.config_version == config.CONFIG_VERSION,
      (st.install_id, st.config_version))
check("destinations survive the migration", [d["kind"] for d in st.integrations] ==
      ["telegram", "ntfy", "email", "webhook", "mqtt"])
check("secrets decrypt after loading", hub.destinations()[0].secret("token") == TOKEN
      and hub.destinations()[2].secret("password") == "app-pass-123")
check("nothing listens before start", not ctx.web.running)

ctx.apply_theme()
win = MainWindow(ctx)
win.resize(1280, 800)
win.show()
check("hub and phone access started after the window", wait(lambda: hub._started and ctx.web.running, 6))
check("phone access binds 127.0.0.1", ctx.web._bound == ("127.0.0.1", WEB_PORT), ctx.web._bound)

# --- wiring --------------------------------------------------------------------------------------------------
names = [win.nav.button(i).text().strip().replace("&&", "&") for i in range(len(win.NAV))]
check("nav: Integrations right after Areas", names == ["Dashboard", "Incidents", "Areas", "Integrations",
                                                        "Live view", "Sites & cameras", "Settings"], names)
check("stack follows the nav", [win.stack.widget(i) for i in range(7)] ==
      [win.dashboard, win.incidents, win.areas_page, win.integrations_page, win.live, win.sites, win.settings])
check("tooltips follow the nav", all(f"Ctrl+{i + 1}" in win.nav.button(i).toolTip() for i in range(7)))
keys = {sc.key().toString(): sc for sc in win.findChildren(QShortcut)}
check("Ctrl+1..Ctrl+7 exist", all(f"Ctrl+{i}" in keys for i in range(1, 8)) and "Ctrl+8" not in keys)
keys["Ctrl+4"].activated.emit()
pump(0.1)
check("Ctrl+4 opens Integrations", win.stack.currentWidget() is win.integrations_page)
keys["Ctrl+7"].activated.emit()
pump(0.1)
check("Ctrl+7 opens Settings", win.stack.currentWidget() is win.settings)
check("About lists Ctrl+1–7", "Ctrl+1–7" in win.settings.about.text())
keys["Ctrl+4"].activated.emit()
pump(0.2)
page = win.integrations_page
check("Integrations page shows the five connections", page.connections is not None
      and set(page.connections.cards) == {d["id"] for d in st.integrations}, list(page.connections.cards))
check("integrations style sheet applied", "SnoozeBar" in app.styleSheet())
check("monitoring-off banner shown", page.connections.mon_banner.isVisible())
keys["Ctrl+1"].activated.emit()

# --- Home Assistant discovery ------------------------------------------------------------------------------------
iid = st.install_id
disc = broker.wait_for(f"homeassistant/device/yardguard_{iid}/config", timeout=10)
payload = json.loads(disc[-1]["payload"]) if disc else {}
check("HA discovery published (retained) with both cameras and the monitoring switch", bool(disc) and disc[-1]["retain"]
      and {"monitoring", "cam1_online", "cam2_online", "cam1_person"} <= set(payload.get("cmps", {})),
      list(payload.get("cmps", {})))
check("HA availability online", bool(broker.wait_for(f"yardguard/{iid}/availability", payload=b"online")))
check("HA monitoring state OFF", bool(broker.wait_for(f"yardguard/{iid}/monitoring/state", payload=b"OFF")))
check("Telegram commands registered", bool(tg.wait_for("setMyCommands", timeout=10)))

# --- incidents through the monitor callbacks ---------------------------------------------------------------------


def raise_incident(severity: str, verdict: str, channel: int = 1, title: str = "Person near cars at night"):
    a = fi.make_alert(store, severity, verdict, channel=channel, camera="Front gate" if channel == 1 else "Driveway",
                      site_id=site.id, title=title)
    store.add(a, gap_s=-1)          # what Monitor does before calling on_alert (on its own thread)
    t = threading.Thread(target=mon.on_alert, args=(a,), name="monitor-sim")
    t.start()
    t.join()
    return a


def hook_events() -> list[dict]:
    out = []
    for r in list(hook.requests):
        try:
            out.append(json.loads(r["body"]))
        except ValueError:
            pass
    return out


def tg_photo_for(inc_id: str):
    for c in tg.calls_of("sendPhoto"):
        if f"f:{inc_id}" in json.dumps(c["params"].get("reply_markup")):
            return c
    return None


def log_reason(dest_id: str, inc_id: str) -> str:
    rows = [r for r in hub.log.list(500, dest_id=dest_id) if r.get("incident_id") == inc_id]
    return " | ".join(f"{r.get('status')}: {r.get('reason')}" for r in rows)


crit = raise_incident("critical", "confirmed", 1, "Person at the car door")
c1 = crit.incident_id
photo = wait(lambda: tg_photo_for(c1), 10)
check("critical incident -> Telegram photo with buttons", photo is not None and "photo" in photo["files"]
      and "h:" + c1 in json.dumps(photo["params"].get("reply_markup")))
check("... caption names the camera", photo is not None and "Front gate" in photo["params"].get("caption", ""),
      photo and photo["params"].get("caption"))
check("critical -> ntfy at full priority (critical gets through quiet hours)",
      wait(lambda: any(r["headers"].get("Priority") == "5" for r in ntfy.requests), 8),
      [r["headers"].get("Priority") for r in ntfy.requests])
check("critical -> email", wait(lambda: len(smtp.messages) >= 1, 8))
check("critical -> webhook", wait(lambda: any(e.get("event") == "incident_new" and e["incident"]["id"] == c1
                                              for e in hook_events()), 8))
check("critical -> Home Assistant event", bool(broker.wait_for(f"yardguard/{iid}/events", timeout=8)))
n_mail = len(smtp.messages)

warn = raise_incident("warning", "confirmed", 2, "Person near the car")
w1 = warn.incident_id
check("warning -> Telegram", wait(lambda: tg_photo_for(w1), 8) is not None)
check("warning in quiet hours -> ntfy silently (priority 2)",
      wait(lambda: any(r["headers"].get("Priority") == "2" for r in ntfy.requests), 8))
check("warning -> webhook", wait(lambda: any(e.get("incident", {}).get("id") == w1 for e in hook_events()), 8))
pump(0.8)
check("warning not emailed (email minimum is critical)", len(smtp.messages) == n_mail)
check("... and the log says why", "minimum" in wait(lambda: log_reason(d_mail["id"], w1), 3), log_reason(d_mail["id"], w1))

n_ntfy = len(ntfy.requests)
info = raise_incident("info", "confirmed", 1, "Car parked")
pump(1.0)
check("info incident not sent to ntfy (minimum warning)", len(ntfy.requests) == n_ntfy)
check("info incident -> webhook (minimum info)",
      wait(lambda: any(e.get("incident", {}).get("id") == info.incident_id for e in hook_events()), 8))

pend = raise_incident("warning", "pending", 1, "Person by the gate")
p1 = pend.incident_id
check("pending -> ntfy at once (AI-confirmed only is off there)", wait(lambda: len(ntfy.requests) > n_ntfy, 8))
pump(1.5)
check("pending -> Telegram waits for the AI", tg_photo_for(p1) is None)
check("pending -> webhook waits for the AI", not any(e.get("incident", {}).get("id") == p1 for e in hook_events()))
pend.verdict = "confirmed"
store.update(pend)
t = threading.Thread(target=mon.on_alert_update, args=(pend,), name="monitor-sim")
t.start()
t.join()
check("AI confirmed -> Telegram sends it", wait(lambda: tg_photo_for(p1), 8) is not None)
check("AI confirmed -> webhook sends it", wait(lambda: any(e.get("incident", {}).get("id") == p1
                                                            for e in hook_events()), 8))

rej = raise_incident("warning", "rejected", 2, "Shadow")
pump(1.5)
check("AI-dismissed incident sent nowhere", tg_photo_for(rej.incident_id) is None
      and not any(e.get("incident", {}).get("id") == rej.incident_id for e in hook_events()))

# --- area events: per-area routing (email: Front path only) -------------------------------------------------------
from yardguard.areas import AreaEvent  # noqa: E402

for area in (area_a, area_b):
    ev = AreaEvent(id=os.urandom(6).hex(), site_id=site.id, channel=1, area_id=area["id"], area_name=area["name"],
                   task=area["task"], ts=time.time(), kind="enter", label="person")
    store.add_area_event(ev)
    t = threading.Thread(target=mon.on_area_event, args=(ev,), name="monitor-sim")
    t.start()
    t.join()
check("area event on Front path -> email", wait(lambda: len(smtp.messages) == n_mail + 1, 8), len(smtp.messages))
pump(1.0)
subjects = [m["data"].split(b"Subject:", 1)[-1].split(b"\n", 1)[0] for m in smtp.messages]
check("... and not for Side path (not chosen)", len(smtp.messages) == n_mail + 1
      and b"Front path" in subjects[-1], subjects)

# --- Telegram "False alarm" button -> store and Incidents page ----------------------------------------------------
win.go_page(win.incidents)
pump(0.3)
win.incidents.reload()
thread = wait(lambda: hub.log.thread(d_tg["id"], c1), 5)
mid = next((m["id"] for m in (thread or {}).get("ref", {}).get("messages", []) if m.get("chat") == CHAT), None)
check("Telegram message saved in the delivery log", mid is not None, thread)
n_edit = len(tg.calls_of("editMessageCaption"))
tg.push_callback(CHAT, f"f:{c1}", int(mid or 1))
check("False alarm -> incident marked in the store", wait(lambda: store.get_incident(c1).status == "false_alarm", 10))
check("... the button press is answered", bool(tg.wait_for("answerCallbackQuery")))
check("... the Incidents page shows it at once",
      wait(lambda: win.incidents.incidents.get(c1) is not None and win.incidents.incidents[c1].status == "false_alarm"))
check("... and it left the Open list", win.incidents._filter != "open" or win.incidents._row_of(c1) is None)
check("... the Telegram caption is edited", wait(lambda: len(tg.calls_of("editMessageCaption")) > n_edit, 8))
check("... the webhook hears the status change",
      wait(lambda: any(e.get("event") == "incident_status" and e["incident"]["id"] == c1
                       and e["incident"]["status"] == "false_alarm" for e in hook_events()), 8))

# --- marked on the desktop -> the Telegram message follows (P2 sync) -----------------------------------------------
n_edit = len(tg.calls_of("editMessageCaption"))
inc_w1 = store.get_incident(w1)
win.incidents.toggle_handled(inc_w1)
check("desktop Mark handled -> store", store.get_incident(w1).status == "handled")
check("... Telegram message edited to show it",
      wait(lambda: any("Handled" in c["params"].get("caption", "") for c in tg.calls_of("editMessageCaption")[n_edit:]),
           8), [c["params"].get("caption", "")[-60:] for c in tg.calls_of("editMessageCaption")[n_edit:]])

# --- /arm and /disarm switch monitoring like the sidebar switch -----------------------------------------------------
hub._started_at -= 120       # past the start-up quiet time, so the change is announced
tg.push_message(CHAT, "/arm")
check("/arm turns monitoring on", wait(lambda: mon.running and not win._mon_busy, 10))
check("... the sidebar switch follows", wait(lambda: win.pill.switch.isChecked(), 3))
check("... and the choice is saved", json.loads((OUT / "config.json").read_text(encoding="utf-8"))["settings"]
      ["monitoring_on"] is True)
check("... Telegram is told who did it", wait(lambda: any("Telegram" in c["params"].get("text", "") and "on" in
                                                          c["params"].get("text", "")
                                                          for c in tg.calls_of("sendMessage")), 8),
      [c["params"].get("text", "")[:80] for c in tg.calls_of("sendMessage")])
check("... Home Assistant monitoring state ON",
      bool(broker.wait_for(f"yardguard/{iid}/monitoring/state", payload=b"ON", timeout=8)))
n_msg = len(tg.calls_of("sendMessage"))
tg.push_message(CHAT, "/disarm")
ask = wait(lambda: [c for c in tg.calls_of("sendMessage")[n_msg:] if "m:off" in json.dumps(c["params"])], 8)
check("/disarm asks first", bool(ask) and mon.running)
tg.push_callback(CHAT, "m:off", 1)
check("confirm -> monitoring off", wait(lambda: not mon.running and not win._mon_busy, 10))
check("... the sidebar switch follows", wait(lambda: not win.pill.switch.isChecked(), 3))

# --- phone access ------------------------------------------------------------------------------------------------


def web(method: str, path: str, body=None, cookie: str = "", headers: dict | None = None, raw: bool = False):
    conn = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=10)
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    if cookie:
        h["Cookie"] = cookie
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    if raw:
        return resp, conn
    out = resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    conn.close()
    return out


check("no cookie -> 401", web("GET", "/api/status")[0] == 401)
check("no cookie on live -> 401", web("GET", "/live/1.mjpg")[0] == 401)
code, link = ctx.web.new_pairing()
raw_code = link.split("#pair=", 1)[1]
status, hdrs, body = in_thread(lambda: web("POST", "/api/pair", {"code": raw_code, "name": "Test phone"}))
set_cookie = hdrs.get("set-cookie", "")
check("pairing code -> cookie", status == 200 and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie,
      (status, body[:120]))
cookie = set_cookie.split(";", 1)[0]
check("pairing code works only once",
      in_thread(lambda: web("POST", "/api/pair", {"code": raw_code, "name": "Again"}))[0] == 403)
check("forged cookie -> 401", web("GET", "/api/status", cookie=f"{ctx.web.cookie_name}=" + "x" * 43)[0] == 401)
status, _, body = web("GET", "/api/status", cookie=cookie)
info_json = json.loads(body) if status == 200 else {}
check("status lists the cameras", [c.get("name") for c in info_json.get("cameras", [])] == ["Front gate", "Driveway"],
      info_json.get("cameras"))
status, _, body = web("GET", "/api/incidents", cookie=cookie)
items = json.loads(body).get("items", []) if status == 200 else []
check("incidents JSON has the incidents", {c1, w1, p1} <= {i.get("id") for i in items}, [i.get("id") for i in items])
check("incidents JSON has no file paths", "test_pics" not in body.decode("utf-8", "replace"))
status, hdrs, body = web("GET", f"/media/alert/{crit.id}/crop", cookie=cookie)
check("alert picture served", status == 200 and hdrs.get("content-type") == "image/jpeg" and body[:2] == b"\xff\xd8")

resp, conn = web("GET", "/live/1.mjpg", cookie=cookie, raw=True)
frames = []
if resp.status == 200 and "multipart/x-mixed-replace" in resp.getheader("Content-Type", ""):
    end = time.time() + 10
    while len(frames) < 3 and time.time() < end:
        line = resp.fp.readline()
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":")[1])
            resp.fp.readline()
            frames.append(resp.fp.read(n))
conn.close()
decoded = [cv2.imdecode(np.frombuffer(f, np.uint8), cv2.IMREAD_COLOR) for f in frames]
check("MJPEG stream sends 3 different JPEG frames", len(frames) == 3 and all(d is not None for d in decoded)
      and len({f for f in frames}) == 3, len(frames))
status, hdrs, body = web("GET", "/live/2.jpg", cookie=cookie)
check("single live picture", status == 200 and body[:2] == b"\xff\xd8")

csrf = info_json.get("csrf", "")
origin = f"http://127.0.0.1:{WEB_PORT}"
check("phone mark without CSRF refused", in_thread(lambda: web("POST", f"/api/incidents/{c1}/mark", {"status": "open"},
                                                               cookie=cookie, headers={"Origin": origin}))[0] == 403)
n_edit = len(tg.calls_of("editMessageCaption"))
status, _, body = in_thread(lambda: web("POST", f"/api/incidents/{c1}/mark", {"status": "open"}, cookie=cookie,
                                        headers={"Origin": origin, "X-YG-CSRF": csrf}))
check("phone marks the incident open again", status == 200 and store.get_incident(c1).status == "open", (status, body))
check("... the Incidents page follows", wait(lambda: win.incidents.incidents[c1].status == "open"))
check("... and the Telegram message too", wait(lambda: len(tg.calls_of("editMessageCaption")) > n_edit, 8))

clock = [time.time()]
ctx.web.limiter = PairLimiter(clock=lambda: clock[0])
answers = [in_thread(lambda: web("POST", "/api/pair", {"code": "ZZZZZZZZ"}))[0] for _ in range(6)]
check("5 wrong codes, then slowed down", answers == [403] * 5 + [429], answers)
clock[0] += 61
answers = [in_thread(lambda: web("POST", "/api/pair", {"code": "ZZZZZZZZ"}))[0] for _ in range(5)]
code2, link2 = ctx.web.new_pairing()
good = link2.split("#pair=", 1)[1]
status, _, body = in_thread(lambda: web("POST", "/api/pair", {"code": good, "name": "Thief"}))
check("10 wrong codes lock pairing, even with a right code", answers == [403] * 5 and status == 429
      and b"15 minutes" in body, (answers, status, body[:100]))
clock[0] += 16 * 60
check("the lock ends after 15 minutes",
      in_thread(lambda: web("POST", "/api/pair", {"code": good, "name": "Second phone"}))[0] == 200)
ctx.web.limiter = PairLimiter()

# --- Telegram setup dialog end to end (second fake bot) -------------------------------------------------------------
dlg = isu.TelegramSetup(ctx, hub, parent=win)
dlg.params["api_base"] = tg2.api_base
dlg.show()
pump(0.2)
dlg.steps[0].token.setText(" bot" + tg2.token + " ")
dlg.next()
check("setup: token checked with getMe", wait(lambda: dlg.index == 1, 10) and dlg.params.get("bot_username") == "MyYardBot")
step = dlg.steps[1]
step.connect_btn.click()
wait(lambda: step.code, 3)
time.sleep(1.1)              # the listener ignores messages from before Connect was pressed
tg2.push_message(4242, f"/start {step.code}", first_name="Sam")
check("setup: the chat that sent the code is found and ticked",
      wait(lambda: 4242 in step.rows and step.rows[4242].box.isChecked(), 10), list(step.rows))
dlg.next()
check("setup: -> test step", wait(lambda: dlg.index == 2, 5))
dlg.test_step.send_btn.click()
check("setup: test photo sent to the new chat", wait(lambda: any(str(c["params"].get("chat_id")) == "4242"
                                                                  for c in tg2.calls_of("sendPhoto")), 10))
check("setup: Did it arrive?", wait(lambda: dlg.test_step.ask.isVisible(), 5))
before = len(st.integrations)
dlg.test_step.yes_btn.click()
pump(0.3)
new = st.integrations[-1] if len(st.integrations) > before else {}
check("setup: Finish adds a running destination", bool(new) and new.get("kind") == "telegram"
      and hub.channel(new.get("id", "")) is not None)
check("setup: token stored encrypted", tg2.token not in (OUT / "config.json").read_text(encoding="utf-8")
      and base.IntegrationConf(new).secret("token") == tg2.token if new else False)
check("setup: the new bot polls", wait(lambda: len(tg2.calls_of("getUpdates")) > 0 and
                                       tg2.calls_of("setMyCommands"), 10))
dlg.deleteLater()

# --- screenshots, light and dark ----------------------------------------------------------------------------------


def clipped(root: QWidget) -> list[str]:
    bad = []
    for w in root.findChildren(QLabel):
        if not w.isVisibleTo(root) or w.wordWrap() or not w.text().strip() or w.width() <= 0:
            continue
        if type(w).__name__ == "ElidedLabel" or w.textFormat() == Qt.RichText:   # elided on purpose / rich text
            continue
        if w.pixmap() is not None and not w.pixmap().isNull():
            continue
        if w.objectName() == "Step":
            continue
        need = max(w.fontMetrics().horizontalAdvance(w.text()), w.sizeHint().width())
        if need > w.width() + 2:
            bad.append(f"label {w.text()[:30]!r} {need}>{w.width()}")
    for w in root.findChildren(QAbstractButton):
        if not w.isVisibleTo(root) or not w.text().strip() or w.width() <= 0:
            continue
        if isinstance(w, (QPushButton, QCheckBox, QRadioButton)) and w.sizeHint().width() > w.width() + 2:
            if w.objectName() == "NavButton":
                continue
            if isinstance(w, QCheckBox) and w.width() >= w.fontMetrics().horizontalAdvance(w.text()) + 22:
                continue
            bad.append(f"button {w.text()[:30]!r} {w.sizeHint().width()}>{w.width()}")
    return bad


def no_hscroll(root: QWidget) -> bool:
    return all(not sa.horizontalScrollBar().isVisible() for sa in root.findChildren(QScrollArea))


def shot(widget: QWidget, name: str) -> None:
    pump(0.25)
    widget.grab().save(str(SHOTS / f"{name}.png"))
    bad = clipped(widget)
    check(f"{name}: no clipped text", not bad, bad[:3])
    check(f"{name}: no horizontal scrollbar", no_hscroll(widget))


win.go_page(page)
page.connections.reload()       # what ConnectionsView.open_setup does after a dialog closes
tg3 = fi.FakeTelegram(token="7000000003:CCHdqTcvCH1vGWJxfSeofSAs0K5PALDsawR")   # clean bot for the shots
SECRETS.append(tg3.token)
for theme in ("light", "dark"):
    st.theme = theme
    ctx.apply_theme()
    pump(0.3)
    for i, tab in enumerate(("connections", "routes", "activity")):
        page.select_tab(i, remember=False)
        if tab == "routes":
            page.routes.reload()
            page.routes.select(d_tg["id"])
        shot(win, f"page_{tab}_{theme}")
    page.select_tab(0, remember=False)
    dialogs = [("ntfy", isu.NtfySetup(ctx, hub, parent=win)), ("email", isu.EmailSetup(ctx, hub, parent=win)),
               ("discord", isu.DiscordSetup(ctx, hub, parent=win)), ("webhook", isu.WebhookSetup(ctx, hub, parent=win)),
               ("mqtt", isu.MqttSetup(ctx, hub, parent=win))]
    for name, d in dialogs:
        d.show()
        shot(d, f"setup_{name}_{theme}")
        if name == "email":
            d.steps[0].options["other"].radio.click()
            d.next()
            wait(lambda d=d: d.index == 1, 3)
            shot(d, f"setup_email_account_{theme}")
        if name == "mqtt":
            b = d.steps[d.index]
            if hasattr(b, "host"):
                b.host.setText("127.0.0.1")
                b.port.setValue(broker.port)
                b.user.setText("ha")
                b.password.setText("mqtt-pass-456")
                b.password.textEdited.emit("mqtt-pass-456")
                d.next()
                if wait(lambda d=d: d.index == 2, 8):
                    shot(d, f"setup_mqtt_discovery_{theme}")
        d.dirty = False
        d.reject()
        d.deleteLater()
    t = isu.TelegramSetup(ctx, hub, parent=win)
    t.show()
    shot(t, f"setup_telegram_1_{theme}")
    t.params["api_base"] = tg3.api_base
    t.steps[0].token.setText(tg3.token)
    t.next()
    if wait(lambda: t.index == 1, 8):
        tg3.updates.clear()          # the light run's message must not show up in the dark run
        t.steps[1].connect_btn.click()
        wait(lambda: t.steps[1].code, 3)
        time.sleep(1.1)
        tg3.push_message(5151, f"/start {t.steps[1].code}", first_name="Alex")
        wait(lambda: 5151 in t.steps[1].rows, 8)
        shot(t, f"setup_telegram_2_{theme}")
        t.next()
        if wait(lambda: t.index == 2, 5):
            shot(t, f"setup_telegram_3_{theme}")
    t.dirty = False
    t.reject()
    t.deleteLater()
    p = isu.PhoneAccessDialog(ctx, hub, parent=win)
    p.show()
    shot(p, f"phone_1_{theme}")
    p.next()
    if wait(lambda: p.index == 2, 5):
        check(f"[{theme}] phone pair step shows a code", bool(p.pair.code.text()))
        shot(p, f"phone_pair_{theme}")
    p.reject()
    p.deleteLater()
    m = isu.ManagePhonesDialog(ctx, win)
    m.show()
    shot(m, f"phones_{theme}")
    m.close()
    m.deleteLater()
st.theme = "light"
ctx.apply_theme()
check("phone access still on after the dialogs", ctx.web.running)

# --- problem surfacing ------------------------------------------------------------------------------------------
win.go_page(win.dashboard)
hub.problem.emit(d_ntfy["id"], "ntfy: the server refused the token.")
pump(0.1)
check("hub problem -> dot on the Integrations nav button", win.integrations_badge.isVisible()
      and "problem" in win.nav.button(3).toolTip())
check("... and a status bar notice", "refused" in win.statusBar().currentMessage())
win.go_page(page)
check("opening Integrations clears the dot", not win.integrations_badge.isVisible())

# --- close -----------------------------------------------------------------------------------------------------------
t0 = time.time()
win.close()
pump(0.3)
check("closing stops phone access", not ctx.web.running)
check("closing shuts the hub down", hub._shut)
check("closing stays quick", time.time() - t0 < 12, round(time.time() - t0, 1))
check("no yg- threads left", wait(lambda: not fi.yg_threads(), 6), fi.yg_threads())
try:
    socket.create_connection(("127.0.0.1", WEB_PORT), timeout=1).close()
    closed = False
except OSError:
    closed = True
check("nothing listens after closing", closed)
logged = LOG.getvalue()
check("no secret in the log", not any(x in logged for x in SECRETS), [x[:6] for x in SECRETS if x in logged])
check("availability offline on Home Assistant", bool(broker.wait_for(f"yardguard/{iid}/availability",
                                                                    payload=b"offline", timeout=5)))
QThreadPool.globalInstance().waitForDone(5000)
pump(0.3)
for f in (tg, tg2, tg3, ntfy, hook, smtp, broker):
    f.close()
print(f"{len(failures)} failure(s)", flush=True)
sys.exit(1 if failures else 0)
