"""Offscreen checks of the main window wiring (no cameras or AI needed).

Nav order and shortcuts, unread badge, toasts, the Sites page zone column (and that saving a
site keeps zones), settings -> monitor restart decisions, monitoring on/off state handling.

    .venv\\Scripts\\python.exe tests\\test_gui_wiring.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "gui_wiring"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ["TEMP"] = os.environ["TMP"] = str(ROOT / ".cache" / "tmp")
sys.path.insert(0, str(ROOT))
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
cfg = {"sites": [{"id": "s1", "name": "Test yard", "host": "192.0.2.1", "brand": "dahua",
                  "cameras": [{"channel": 1, "name": "Parking", "role": "Parking",
                               "zones": [{"name": "Row", "points": [[0, 0], [1, 0], [1, 1]]}]},
                              {"channel": 2, "name": "Gate"}]}],
       "active_site_id": "s1",
       "settings": {"events_dir": str(OUT / "events"), "brain_mode": "off", "monitoring_on": False}}
(OUT / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from yardguard import config  # noqa: E402
from yardguard.engine import Alert  # noqa: E402
from yardguard.gui import AppContext, MainWindow, object_text, track_tuples  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json"
failures = []


def check(name: str, ok: bool, detail="") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def pump(seconds: float = 0.3) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


class StubBrain:
    """Brain stand-in: never loads a model (the real one would take 9 GB of GPU memory)."""

    def __init__(self, state="stopped"):
        self.state, self.starts, self.stops = state, 0, 0
        self.user_stopped = False
        self.last_error = None
        self.install_step = None

    def status(self):
        return self.state

    def start(self, user=False):
        self.starts += 1
        if user:
            self.user_stopped = False
        self.state = "starting"

    def stop(self, user=False):
        self.stops += 1
        if user:
            self.user_stopped = True
        self.state = "stopped"

    def stats(self):
        return {"avg_latency_s": 0.9, "last_latency_s": 0.9, "vram_mb": 9100} if self.state == "ready" else {}

    def ask(self, image, phrases, mode="fast", timeout=60, max_side=896, upscale=False, seed=0):
        return []

    def cancel_install(self):
        pass


app = QApplication(sys.argv)
app.setStyle("Fusion")
ctx = AppContext(app)
ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)
ctx.brain = ctx.monitor.brain = StubBrain()
ctx.apply_theme()
win = MainWindow(ctx)
win.resize(1280, 800)
win.show()
pump()

check("nav order", [win.nav.button(i).text().strip().replace("&&", "&") for i in range(7)] ==
      ["Dashboard", "Incidents", "Areas", "Integrations", "Live view", "Sites & cameras", "Settings"])
check("stack order", [win.stack.widget(i) for i in range(7)] ==
      [win.dashboard, win.incidents, win.areas_page, win.integrations_page, win.live, win.sites, win.settings])
check("Settings at Ctrl+7", "Ctrl+7" in win.nav.button(6).toolTip())
check("Areas at Ctrl+3", "Ctrl+3" in win.nav.button(2).toolTip())
check("Integrations at Ctrl+4", "Ctrl+4" in win.nav.button(3).toolTip())
check("Live view at Ctrl+5", "Ctrl+5" in win.nav.button(4).toolTip())
check("Incidents at Ctrl+2", "Ctrl+2" in win.nav.button(1).toolTip())
check("bridge has area_event_added", hasattr(ctx.alert_bridge, "area_event_added"))
check("monitor reports area events to the bridge", ctx.monitor.on_area_event is not None)
check("live page hosts the areas canvas and panel", win.live.video is win.live.areas.canvas
      and win.live.areas.panel.parent() is not None)
check("toolbar: Show areas and Add area", win.live.areas.show_cb.text() == "Show areas"
      and win.live.areas.add_btn.parent() is win.live.toolbar)
check("All alerts lives inside the Incidents page", win.alerts.parent() is win.incidents.views)
check("badge hidden with no alerts", win.incident_badge.isHidden())

a = Alert(id="t1", site_id="s1", channel=1, camera="Parking", ts=time.time(), rule="night_presence",
          title="Person near the cars at night", detail="test", severity="warning", verdict="unverified")
ctx.alert_store.add(a)
ctx.alert_bridge.alert_added.emit(a)
pump()
check("badge shows 1 after a new alert", not win.incident_badge.isHidden() and win.incident_badge.text() == "1",
      win.incident_badge.text())
check("toast shown", len(win.toasts.toasts) == 1)
win.grab().save(str(OUT / "badge_toast.png"))
rej = Alert(id="t2", site_id="s1", channel=2, camera="Gate", ts=time.time(), rule="loitering",
            title="x", detail="", severity="info", verdict="rejected")
ctx.alert_store.add(rej)
ctx.alert_bridge.alert_added.emit(rej)
pump()
check("no toast for an alert the AI already dismissed", len(win.toasts.toasts) == 1)
check("both alerts in one incident", ctx.alert_store.get("t2").incident_id == a.incident_id)
win._open_alert("t1")
pump(0.5)
check("toast click opens the Incidents page", win.stack.currentWidget() is win.incidents)
check("toast click selects the alert's incident", win.incidents._current_id() == a.incident_id)
check("badge 0 once the incident is seen", win.incident_badge.isHidden(), win.incident_badge.text())

import threading  # noqa: E402

got = []
ctx.alert_bridge.incident_added.connect(lambda inc: got.append(("added", inc.id, threading.current_thread().name)))
ctx.alert_bridge.incident_updated.connect(lambda inc: got.append(("updated", inc.id, inc.verdict)))
late = Alert(id="t3", site_id="s1", channel=2, camera="Gate", ts=time.time() + 3600, rule="night_presence",
             title="Person seen at night", detail="", severity="warning", verdict="pending")


def monitor_thread() -> None:  # what Monitor._raise / _finish do on their own threads
    ctx.alert_store.add(late)
    ctx.monitor.on_alert(late)
    late.verdict = "confirmed"
    ctx.alert_store.update(late)
    ctx.monitor.on_alert_update(late)


worker = threading.Thread(target=monitor_thread, name="yg-test")
worker.start()
worker.join()
pump()
check("monitor callbacks emit incident_added then incident_updated on the GUI thread",
      got == [("added", late.incident_id, "MainThread"), ("updated", late.incident_id, "confirmed")], got)
check("new incident shows up on the page", late.incident_id in win.incidents.incidents)
check("dashboard strip lists it", any(i.incident_id == late.incident_id for i in win.dashboard.strip.items))

win.go_page(win.sites)
pump()
zbtn = win.sites.table.cellWidget(0, 6)
check("areas column button", zbtn is not None and "1 area" in zbtn.text(), zbtn.text() if zbtn else None)
check("areas column header", win.sites.table.horizontalHeaderItem(6).text() == "Areas")
abtn = win.sites.table.cellWidget(1, 6)
check("camera without areas offers Add area", abtn is not None and "Add area" in abtn.text())
watch = win.sites.table.item(0, 5)
check("alerts (monitor) column checked", watch is not None and watch.checkState() == Qt.Checked)
watch.setCheckState(Qt.Unchecked)
check("unticking alerts clears cam.monitor", not win.sites._cameras()[0].monitor)
watch.setCheckState(Qt.Checked)
win.sites.name_edit.setText("Test yard 2")
win.sites.save()
pump()
check("saving a site keeps zones", len(ctx.cfg.active_site().camera(1).zones) == 1)
zone_id = ctx.cfg.active_site().camera(1).areas[0]["id"]
ctx.cfg.active_site().camera(1).areas.append(config.new_area("count", "line", [[0.1, 0.5], [0.9, 0.5]], "Gate count"))
ctx.save()
win.sites.refresh()
win.sites.name_edit.setText("Test yard 3")
win.sites.save()
pump()
kept = ctx.cfg.active_site().camera(1).areas
check("saving a site keeps every area with its id", [a["task"] for a in kept] == ["watch_zone", "count"]
      and kept[0]["id"] == zone_id, [a["task"] for a in kept])
saved = json.loads((OUT / "config.json").read_text(encoding="utf-8"))
check("zones in saved config (as watch_zone areas)",
      [a["task"] for a in saved["sites"][0]["cameras"][0]["areas"]] == ["watch_zone", "count"])
check("no zones key stored", "zones" not in saved["sites"][0]["cameras"][0])

mon = ctx.monitor
check("monitor off at start (monitoring_on False)", not mon.running)
win.set_monitoring(True)
check("monitoring_on saved", json.loads((OUT / "config.json").read_text())["settings"]["monitoring_on"] is True)
t0 = time.time()
while not mon.running and time.time() - t0 < 10:
    pump(0.1)
check("monitor started", mon.running)
ctx.cfg.settings.detect_fps = 3.0
check("fps change needs restart", mon.needs_restart())
win.setting_changed("detect_fps")
t0 = time.time()
while (win._mon_busy or mon.needs_restart()) and time.time() - t0 < 20:
    pump(0.1)
check("restarted with new settings", mon.running and not mon.needs_restart())
win.set_monitoring(False)
t0 = time.time()
while (mon.running or win._mon_busy) and time.time() - t0 < 20:
    pump(0.1)
check("monitor stopped", not mon.running)
check("monitoring_on False saved", json.loads((OUT / "config.json").read_text())["settings"]["monitoring_on"] is False)

def wait(cond, seconds=20.0) -> None:
    t0 = time.time()
    while not cond() and time.time() - t0 < seconds:
        pump(0.1)


# --- "Stop AI brain" stays stopped while monitoring runs (it used to come back) ---------------
stub = ctx.brain
s = ctx.cfg.settings
win.settings.brain_card.mode_options["verify"].radio.click()
check("verify chosen in the brain card", s.brain_mode == "verify")
win.set_monitoring(True)
wait(lambda: mon.running and not win._mon_busy)
check("monitoring with verify starts the brain", stub.starts == 1, stub.starts)
stub.state = "ready"
card = win.settings.brain_card
card.refresh(force=True)
check("Stop enabled with a YOLO main model", card.buttons["stop"].isEnabled())
card._action("stop")
wait(lambda: stub.state == "stopped", 5)
check("Stop stops it and marks it user-stopped", stub.state == "stopped" and stub.user_stopped)
starts = stub.starts
s.detect_fps = 4.0
win.setting_changed("detect_fps")  # a setting that restarts monitoring (Monitor.start used to restart the brain)
wait(lambda: not win._mon_busy and not mon.needs_restart())
check("monitoring restarted", mon.running and not mon.needs_restart())
win.setting_changed("brain_mode")
pump(0.3)
check("restart and mode change keep the brain stopped", stub.starts == starts and stub.state == "stopped",
      (stub.starts, stub.state))
check("alerts are unverified meanwhile", not mon.verifying())
card.refresh(force=True)
check("brain card says it stays off", "stays off" in card.st_text.text(), card.st_text.text())
win.set_monitoring(False)
wait(lambda: not mon.running and not win._mon_busy)
check("monitoring off does not touch a user-stopped brain", stub.stops == 1, stub.stops)
win.set_monitoring(True)  # the user turns monitoring on again: that lifts the Stop
wait(lambda: mon.running and not win._mon_busy)
check("turning monitoring on again starts it", stub.starts == starts + 1 and not stub.user_stopped)
stub.state = "ready"
card._action("stop")
wait(lambda: stub.state == "stopped", 5)
card._action("start")
wait(lambda: stub.state == "starting", 5)
check("Start by the user lifts the Stop", stub.starts == starts + 2 and not stub.user_stopped)
win.set_monitoring(False)
wait(lambda: not mon.running and not win._mon_busy)
check("brain the user started is not stopped with monitoring", stub.state == "starting", stub.state)

# --- AI models: the four YOLO models only; LocateAnything is only the AI brain ----------------------
models = win.settings.models_card
win.go_page(win.settings)
pump(0.3)
check("models card lists exactly the 4 YOLO models", list(models.rows) == ["yolo11n", "yolo11s", "yolo11m", "yolo11l"]
      and [r.findChild(QLabel, "CardTitle").text() for r in models.rows.values()] ==
      ["Fast", "Balanced", "Accurate", "Most accurate"], list(models.rows))
check("no LocateAnything in the models card",
      not any("LocateAnything" in w.text() for w in models.findChildren(QLabel)))
stub.state = "ready"
card.refresh(force=True)
check("brain card: Off / Verify both selectable, Stop enabled",
      all(o.isEnabled() for o in card.mode_options.values()) and card.buttons["stop"].isEnabled())
check("brain card: no LA-main parts", not hasattr(card, "builtin") and not hasattr(card, "objects_box"))
check("brain card: generation row and prompts shown", card.generation.isVisibleTo(card)
      and card.prompts.isVisibleTo(card))
s.model = "yolo11m"
win.setting_changed("model")
pump(0.3)
check("switching YOLO models leaves the brain alone", stub.state == "ready", stub.state)
s.model = "yolo11s"
win.setting_changed("model")
pump(0.2)

import numpy as np  # noqa: E402

class _T:
    id, label, box = 3, "car", np.array([16.0, 9.0, 80.0, 90.0])


check("tracks normalised for the canvas", track_tuples([_T()], 160, 90) == [(3, "car", 0.1, 0.1, 0.5, 1.0)])

# --- Areas: Sites column opens Live view in add mode; hub add / edit open Live view --------------
win.go_page(win.sites)
pump()
win.sites._open_areas(2)  # camera 2 has no areas: Live view in add mode
pump(0.5)
check("Sites Areas button opens Live view on that camera", win.stack.currentWidget() is win.live
      and win.live.combo.currentData() == 2, win.live.combo.currentData())
check("... in add mode", win.live.areas.canvas.drafting and win.live.areas.panel.isVisible())
win.live.areas.panel.close_panel()
pump()
win.areas_page.add_requested.emit(-1)
pump(0.5)
check("hub Add area: Live view in add mode on the current camera", win.stack.currentWidget() is win.live
      and win.live.areas.canvas.drafting)
win.live.areas.panel.close_panel()
win.areas_page.edit_requested.emit(1, kept[1]["id"])
pump(0.5)
check("hub Edit: Live view on camera 1, editing that area", win.live.combo.currentData() == 1
      and win.live.areas.canvas.editing_id == kept[1]["id"])
win.live.areas.panel.close_panel()
win.live.areas.open_log.emit(kept[1]["id"])
pump(0.3)
check("Open log shows the hub", win.stack.currentWidget() is win.areas_page)
win.go_page(win.settings)
pump(0.3)
clips = win.settings.clips
clips.area_days.setValue(12)
check("area log days saved", json.loads((OUT / "config.json").read_text())["settings"]["area_log_days"] == 12)
check("plates add-on row in AI models (not one of the 4 models)",
      list(models.addon_rows) == ["plates"] and "plates" not in models.rows)
check("object text", object_text({"car": 3, "person": 1}) == "1 person, 3 cars")
win.close()
pump(0.5)
win.live.stop_all(15000)  # the live view's camera (an unreachable test address) times out after ~6 s
print(f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
