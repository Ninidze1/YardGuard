"""End-to-end check of the Areas feature in the real main window (offscreen).

Areas are drawn through the real UI code paths (Add area button, drawing bar, mouse drags and
clicks on the live canvas, template cards, the details form, Save). Monitoring then runs on two
recorded clips replayed as fake cameras:
  channel 5 "Driveway"        day clip with cars driving to the barrier (.cache/tests/plates/ch5.mp4)
  channel 2 "Driveway night"  night clip of parked cars (.cache/tests/night/long/ch5.mp4)
and the script checks that area events reach the Areas hub live, Live view shows the counters,
CSV export works and area alerts reach Incidents with their clips. Screenshots of every new
screen in light and dark go to .cache/tests/areas_e2e/shots.

    run.cmd tests\\e2e_areas.py [seconds of replay, default 120]

Needs the GPU for YOLO and the plate reader (checks that at most 3 GB is in use first).
Writes only under .cache/tests/areas_e2e (its own config file and events folder).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "areas_e2e"
SHOTS = OUT / "shots"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ["TEMP"] = os.environ["TMP"] = str(ROOT / ".cache" / "tmp")
sys.path.insert(0, str(ROOT))

DAY = ROOT / ".cache" / "tests" / "plates" / "ch5.mp4"
NIGHT = ROOT / ".cache" / "tests" / "night" / "long" / "ch5.mp4"
CLIPS = {5: (DAY, 915.0), 2: (NIGHT, 5.0)}   # channel -> (file, start second)
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0


def gpu_used_mb() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


used = gpu_used_mb()
print(f"GPU memory in use: {used} MB")
if used > 3000:
    print("GPU busy (more than 3 GB in use): not running")
    sys.exit(2)

for p in (OUT / "events", SHOTS, OUT / "config.json", OUT / "ui.ini"):
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
SHOTS.mkdir(parents=True, exist_ok=True)
(ROOT / ".cache" / "tmp").mkdir(parents=True, exist_ok=True)
cfg = {"sites": [{"id": "e2e", "name": "E2E yard", "host": "192.0.2.20", "brand": "dahua",
                  "cameras": [{"channel": 2, "name": "Driveway night", "role": "Parking"},
                              {"channel": 5, "name": "Driveway", "role": "Parking"}]}],
       "active_site_id": "e2e",
       "settings": {"events_dir": str(OUT / "events"), "brain_mode": "off", "monitoring_on": False,
                    "clips_enabled": True, "theme": "light"}}
(OUT / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from yardguard import areas_ui, config, gui  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json"
failures: list[str] = []


def check(name: str, ok: bool, detail="") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail != "" else ""))
    if not ok:
        failures.append(name)


# --- fake cameras ----------------------------------------------------------------------------------

def channel_of(url: str) -> int:
    return int(url.split("channel=")[1].split("&")[0])


class ReplaySource:
    """FrameSource stand-in: plays a clip in real time from a start second (loops at the end)."""

    def __init__(self, url: str, fps: float, long_side: int = 1280, gpu_decode: bool = False) -> None:
        self.path, self.start_s = CLIPS[channel_of(url)]
        self.fps, self.long_side = fps, long_side
        self._item = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.size = None
        self.backend = "replay"
        self.error = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def latest(self):
        return self._item

    def _run(self) -> None:
        cap = cv2.VideoCapture(str(self.path))
        cap.set(cv2.CAP_PROP_POS_MSEC, self.start_s * 1000)
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 15
        t0, n, last = time.time(), 0, 0.0
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_MSEC, self.start_s * 1000)
                continue
            n += 1
            wait = t0 + n / src_fps - time.time()
            if wait > 0:
                self._stop.wait(wait)
            now = time.time()
            if now - last < 1 / max(self.fps, 0.5) * 0.9:
                continue
            last = now
            h, w = frame.shape[:2]
            k = self.long_side / max(h, w)
            if k < 1:
                frame = cv2.resize(frame, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
            self.size = (frame.shape[1], frame.shape[0])
            self._item = (frame, now)
        cap.release()


class PacedCapture:
    """cv2.VideoCapture of a clip that reads at the clip's own speed (for Live view's worker)."""

    def __init__(self, url: str) -> None:
        path, start = CLIPS[channel_of(url)]
        self.cap = cv2.VideoCapture(str(path))
        self.cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 15
        self.t0, self.n = time.time(), 0

    def isOpened(self) -> bool:
        return self.cap.isOpened()

    def read(self):
        self.n += 1
        wait = self.t0 + self.n / self.fps - time.time()
        if wait > 0:
            time.sleep(wait)
        return self.cap.read()

    def release(self) -> None:
        self.cap.release()


gui.open_capture = PacedCapture  # Live view plays the clips too


class StubBrain:
    """No AI brain in this run (brain_mode off); never loads a model."""

    user_stopped = False
    last_error = None
    install_step = None

    def status(self):
        return "stopped"

    def start(self, user=False):
        pass

    def stop(self, user=False):
        pass

    def stats(self):
        return {}

    def ask(self, *a, **k):
        return []

    def cancel_install(self):
        pass


# --- UI helpers ------------------------------------------------------------------------------------

app = QApplication(sys.argv)
app.setStyle("Fusion")


def pump(seconds: float = 0.2) -> None:
    end = time.time() + seconds
    while True:
        app.processEvents()
        if time.time() >= end:
            break
        time.sleep(0.01)


def wait(cond, seconds: float = 20.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        pump(0.1)
    return cond()


def mouse(w, kind, pos, buttons=None):
    types = {"press": QEvent.MouseButtonPress, "release": QEvent.MouseButtonRelease, "move": QEvent.MouseMove,
             "dbl": QEvent.MouseButtonDblClick}
    p = QPointF(*pos)
    button = Qt.NoButton if kind == "move" else Qt.LeftButton
    if buttons is None:
        buttons = Qt.NoButton if kind == "release" else Qt.LeftButton
    QApplication.sendEvent(w, QMouseEvent(types[kind], p, w.mapToGlobal(p), button, buttons, Qt.NoModifier))


def at(canvas, x, y):
    p = canvas.to_px([x, y])
    return p.x(), p.y()


def drag(canvas, a, b, steps=12):
    a, b = at(canvas, *a), at(canvas, *b)
    mouse(canvas, "move", a, buttons=Qt.NoButton)
    mouse(canvas, "press", a)
    for i in range(1, steps + 1):
        t = i / steps
        mouse(canvas, "move", (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), buttons=Qt.LeftButton)
        pump(0.01)
    mouse(canvas, "release", b)
    pump(0.1)


def click(canvas, x, y):
    p = at(canvas, x, y)
    mouse(canvas, "move", p, buttons=Qt.NoButton)
    mouse(canvas, "press", p)
    mouse(canvas, "release", p)
    pump(0.15)  # longer than the double-click guard, so each click is its own corner


def dbl(canvas, x, y):
    p = at(canvas, x, y)
    mouse(canvas, "press", p)
    mouse(canvas, "release", p)
    mouse(canvas, "dbl", p)
    mouse(canvas, "release", p)
    pump(0.1)


def card_click(card) -> None:
    QApplication.sendEvent(card, QMouseEvent(QEvent.MouseButtonRelease, QPointF(10, 10), card.mapToGlobal(QPointF(10, 10)),
                                             Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump(0.1)


def shot(name: str, widget=None) -> None:
    pump(0.3)
    (widget or win).grab().save(str(SHOTS / f"{name}_{ctx.cfg.settings.theme}.png"))


def set_theme(theme: str) -> None:
    ctx.cfg.settings.theme = theme
    ctx.apply_theme()
    pump(0.3)


# --- the app -----------------------------------------------------------------------------------------

areas_ui.confirm = lambda *a, **k: True   # "Discard?" boxes would block offscreen
ctx = gui.AppContext(app)
ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)
ctx.brain = ctx.monitor.brain = StubBrain()
ctx.monitor._source_factory = ReplaySource
ctx.apply_theme()
win = gui.MainWindow(ctx)
win.resize(1280, 800)
win.show()
pump(0.5)
events: list = []
ctx.alert_bridge.area_event_added.connect(events.append)
live = win.live
la = live.areas
canvas = la.canvas
panel = la.panel


def open_camera(ch: int) -> None:
    win.open_live(ch)
    check(f"Live view shows channel {ch}", wait(lambda: canvas.image() is not None, 60) and
          live.combo.currentData() == ch)


def new_area(shape: str, draw, task: str, form=None, name: str = "") -> dict:
    """Add area -> choose the shape on the drawing bar -> draw -> pick the template card -> form -> Save."""
    la.add_btn.click()
    pump(0.1)
    check(f"[{name}] Add area opens the panel and the drawing bar", panel.isVisible() and la.bar.isVisible())
    (la.bar.line_btn if shape == "line" else la.bar.area_btn).click()
    pump(0.05)
    draw()
    if panel.step == 0:
        panel.next_btn.click()
        pump(0.1)
    check(f"[{name}] shape closed, template step", panel.step == 1, panel.step)
    card_click(panel.cards[task])
    check(f"[{name}] {task} form", panel.step == 2 and panel.task == task, (panel.step, panel.task))
    panel.f_name.setText(name)
    if form:
        form()
    before = len(la.areas())
    panel.next_btn.click()   # "Save area"
    pump(0.3)
    a = la.areas()[-1] if len(la.areas()) > before else {}
    check(f"[{name}] saved", a.get("task") == task and a.get("name") == name and not panel.isVisible(),
          a.get("name"))
    return a


try:
    # ---------------------------------------------------------------- draw areas on channel 5 (day)
    open_camera(5)
    pump(3)   # a few frames so the live detector shows tracks
    shot("live_before")

    def plates_form():
        panel.f_alert.setChecked(True)

    lane = new_area("line", lambda: drag(canvas, (0.02, 0.62), (0.55, 0.62)), "log_plates", plates_form,
                    "Barrier lane")
    count = new_area("line", lambda: drag(canvas, (0.03, 0.50), (0.50, 0.50)), "count", None, "Driveway count")

    def draw_poly():
        for x, y in ((0.03, 0.40), (0.55, 0.40), (0.62, 0.95)):
            click(canvas, x, y)
        dbl(canvas, 0.02, 0.95)

    drive = new_area("polygon", draw_poly, "presence", lambda: panel.f_objects["car"].setChecked(True),
                     "Driveway")

    def arm_form():
        panel.f_spin.setValue(1)   # minutes (the barrier arm moves often)

    arm = new_area("polygon", lambda: drag(canvas, (0.20, 0.10), (0.58, 0.32)), "state_watch", arm_form,
                   "Barrier arm")
    check("line direction is both ways by default", lane.get("direction") == "both")
    check("plate area raises alerts", lane.get("alert") is True)
    check("areas saved to the config file", len(json.loads((OUT / "config.json").read_text(encoding="utf-8"))
                                                ["sites"][0]["cameras"][1]["areas"]) == 4)

    # screenshots of the drawing flow (light and dark): polygon midway, line + arrow, picker, form
    for theme in ("light", "dark"):
        set_theme(theme)
        la.add_btn.click()
        pump(0.1)
        for x, y in ((0.62, 0.45), (0.95, 0.42), (0.97, 0.80)):
            click(canvas, x, y)
        mouse(canvas, "move", at(canvas, 0.66, 0.85), buttons=Qt.NoButton)
        shot("draw_polygon")
        panel.cancel()
        pump(0.1)
        la.add_btn.click()
        la.bar.line_btn.click()
        drag(canvas, (0.10, 0.75), (0.60, 0.70))
        shot("template_picker")
        card_click(panel.cards["count"])
        shot("count_form")
        panel.cancel()
        pump(0.1)
        la._area_clicked(lane["id"], canvas.to_px(lane["points"][1]))
        shot("popover")
        la.popover.hide()
    set_theme("light")

    # ---------------------------------------------------------------- channel 2 (night): parked cars
    live.combo.setCurrentIndex(live.combo.findData(2))
    check("camera switch shows channel 2", wait(lambda: canvas.image() is not None and la.cam.channel == 2, 60))

    def no_parking_form():
        panel.f_spin.setValue(1)
        panel.f_alert.setChecked(True)

    white = new_area("polygon", lambda: drag(canvas, (0.33, 0.03), (0.47, 0.14)), "no_parking", no_parking_form,
                     "White car")
    silver = new_area("polygon", lambda: drag(canvas, (0.47, 0.06), (0.64, 0.19)), "parking_spot", None,
                      "Silver car")
    check("small rectangle preselects Parking spot", silver.get("task") == "parking_spot")

    # ---------------------------------------------------------------- monitoring on the replays
    win.set_monitoring(True)
    check("monitoring started", wait(lambda: ctx.monitor.running and not win._mon_busy, 30))
    t0 = time.time()
    live_seen: dict[str, str] = {}
    while time.time() - t0 < SECONDS:
        pump(1.0)
        for aid, info in (ctx.monitor.area_live(2) | ctx.monitor.area_live(5)).items():
            live_seen[aid] = info.get("text", "")
    print("events:", [(e.area_name, e.kind, e.value) for e in events])
    print("live:", live_seen)
    store = ctx.alert_store
    logged = store.list_area_events(limit=1000)
    check("area events were logged", len(logged) > 0, len(logged))
    check("every logged event reached the GUI live", {e.id for e in logged} <= {e.id for e in events},
          f"{len(logged)} logged, {len(events)} live")
    by_area = {}
    for e in logged:
        by_area.setdefault(e.area_name, []).append(e)
    check("parked white car blocks the no-parking area", any(e.kind == "blocked" for e in by_area.get("White car", [])),
          [e.kind for e in by_area.get("White car", [])])
    check("silver car occupies its parking spot", any(e.kind == "occupied" for e in by_area.get("Silver car", [])),
          [e.kind for e in by_area.get("Silver car", [])])
    crossings = by_area.get("Barrier lane", []) + by_area.get("Driveway count", [])
    check("cars crossing the driveway lines are logged", len(crossings) > 0, [(e.area_name, e.kind) for e in crossings])
    plates = [e for e in by_area.get("Barrier lane", []) if e.kind == "plate"]
    print("plates:", [(e.value, e.confidence, e.extra.get("direction"), e.extra.get("note")) for e in plates])
    check("live counters available for Live view", any(t for t in live_seen.values()), live_seen)

    # Live view tags: the canvas polls area_live once a second
    open_camera(5)
    pump(2.5)
    check("Live view canvas has live stats", bool(canvas.live) and canvas.watching, list(canvas.live.values())[:2])
    for theme in ("light", "dark"):
        set_theme(theme)
        shot("live_areas")
    set_theme("light")

    # ---------------------------------------------------------------- Areas hub
    hub = win.areas_page
    win.go_page(hub)
    pump(0.5)
    check("hub lists all 6 areas", len(hub.lookup) == 6, len(hub.lookup))

    def saturation(ch: int) -> float:
        img = cv2.imread(str(areas_ui.thumb_path("e2e", ch)))
        return float(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 1].mean()) if img is not None else -1.0

    sat5, sat2 = saturation(5), saturation(2)
    check("hub thumbnails show each camera's own picture (day colour vs night grey)", sat5 > 25 and 0 <= sat2 < 15,
          (round(sat5, 1), round(sat2, 1)))
    for name, a in (("lane", lane), ("count", count), ("white", white), ("silver", silver)):
        hub.show_area(a["id"])
        pump(0.8)
        rows = hub.detail.table.rowCount()
        n = len(store.list_area_events(area_id=a["id"]))
        check(f"hub log of {a['name']} shows its events", rows == min(n, 500) or (n == 0 and rows <= 1), (rows, n))
        for theme in ("light", "dark"):
            set_theme(theme)
            shot(f"hub_{name}")
        set_theme("light")
    # a new event appears at the top of the open log without a reload
    hub.show_area(silver["id"])
    pump(0.5)
    rows = hub.detail.table.rowCount()
    from yardguard.areas import new_event  # noqa: E402
    ev = new_event(silver, 2, time.time(), "freed", label="car")
    ev.site_id = "e2e"
    store.add_area_event(ev)
    ctx.alert_bridge.area_event_added.emit(ev)
    pump(0.5)
    check("a live event goes to the top of the open log", hub.detail.table.rowCount() == rows + 1, rows)
    # CSV export through the page (the save dialog answers with a path under the test folder)
    csv_path = OUT / "lane.csv"
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(csv_path), "CSV files (*.csv)"))
    hub.show_area(lane["id"])
    pump(0.5)
    hub.detail.export_btn.click()
    pump(0.5)
    text = csv_path.read_text(encoding="utf-8-sig") if csv_path.exists() else ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    want = len(store.list_area_events(**hub.detail.filters(), limit=100000))
    check("CSV export writes the lane's log", csv_path.exists() and len(lines) == 1 + want, (len(lines), want))

    # ---------------------------------------------------------------- alerts -> Incidents with clips
    alerts = [a for a in store.list(limit=200) if a.rule.startswith("area_")]
    print("area alerts:", [(a.rule, a.title, a.verdict) for a in alerts])
    check("area alerts were raised", len(alerts) > 0)
    check("area alerts belong to incidents", all(a.incident_id for a in alerts))
    check("area alert titles are plain language", all(a.title and "area_" not in a.title for a in alerts),
          [a.title for a in alerts])
    wait(lambda: all(store.get(a.id).clip_path for a in alerts), 40)
    clipped = [store.get(a.id) for a in alerts]
    check("area alerts got their clips", all(a.clip_path and Path(a.clip_path).exists() for a in clipped),
          [a.clip_path for a in clipped])
    win.go_page(win.incidents)
    win.incidents.reload()
    pump(0.8)
    if alerts:
        win.open_incident(alerts[0].incident_id, alerts[0].id)
        pump(1.0)
    for theme in ("light", "dark"):
        set_theme(theme)
        shot("incidents_area_alert")
    set_theme("light")

    # ---------------------------------------------------------------- other new screens
    win.go_page(win.sites)
    pump(0.5)
    btn = win.sites.table.cellWidget(win.sites._row_for(5), 6)
    check("Sites Areas column counts the areas", btn is not None and btn.text().strip() == "4 areas",
          btn.text() if btn else None)
    for theme in ("light", "dark"):
        set_theme(theme)
        win.sites.findChild(gui.QScrollArea).ensureWidgetVisible(win.sites.table)
        shot("sites_areas_column")
        win.go_page(win.settings)
        pump(0.4)
        sc = win.settings.findChild(gui.QScrollArea)
        sc.ensureWidgetVisible(win.settings.models_card.addon_rows["plates"])
        shot("settings_models_addon")
        sc.ensureWidgetVisible(win.settings.clips.area_days)
        shot("settings_area_log")
        win.go_page(win.sites)
        pump(0.3)
    set_theme("light")
finally:
    win.set_monitoring(False)
    wait(lambda: not ctx.monitor.running and not win._mon_busy, 30)
    win.close()
    pump(0.5)
    win.live.stop_all(10000)

print(f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
