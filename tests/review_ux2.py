"""UX review renders: every page of the real MainWindow offscreen, both themes, two sizes, fake brain/monitor states.

    .venv\\Scripts\\python.exe tests\\review_ux.py
Screenshots go to .cache/tests/review/shots. Uses a copy of config.json, never the real one; no models are loaded.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "review"
CACHE = ROOT / ".cache"
for key, sub in (("YOLO_CONFIG_DIR", "ultralytics"), ("TORCH_HOME", "torch"), ("HF_HOME", "hf"), ("CUDA_CACHE_PATH", "cuda")):
    os.environ[key] = str(CACHE / sub)
os.environ["TEMP"] = os.environ["TMP"] = str(CACHE / "tmp")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
sys.path.insert(0, str(ROOT))

(OUT / "shots").mkdir(parents=True, exist_ok=True)
from local_data import SNAPS, need_config, need_snapshots  # noqa: E402

need_config()
need_snapshots()
real = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
real["settings"]["events_dir"] = str(OUT / "events")
real["settings"]["monitoring_on"] = False
real["settings"]["theme"] = "light"
real["settings"]["brain_mode"] = "verify"
for site in real["sites"]:
    if site["id"] == real.get("active_site_id"):
        for cam in site["cameras"]:
            if cam["channel"] == 1:
                cam["zones"] = [{"name": "Parking row", "points": [[0.05, 0.45], [0.95, 0.45], [0.98, 0.98], [0.02, 0.98]]},
                                {"name": "Gate", "points": [[0.6, 0.1], [0.8, 0.1], [0.8, 0.3], [0.6, 0.3]]}]
(OUT / "config.json").write_text(json.dumps(real, indent=2), encoding="utf-8")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QSettings, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QMessageBox  # noqa: E402

from yardguard import config  # noqa: E402
from yardguard.engine import Alert  # noqa: E402
from yardguard import gui  # noqa: E402
from yardguard.gui import AppContext, MainWindow  # noqa: E402
from yardguard.settings_ui import ZoneEditorDialog  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json"


class FakeBrain:
    def __init__(self):
        self.state = "stopped"
        self.last_error = None
        self._stats = {"last_latency_s": None, "vram_mb": None, "avg_latency_s": None}

    def status(self):
        return self.state

    def stats(self):
        return dict(self._stats)

    def install(self, progress=None):
        for i in range(1000):
            time.sleep(0.05)
            if progress:
                progress("Downloading model weights (model-00002-of-00004.safetensors)", i, 1000)

    def cancel_install(self):
        pass

    def start(self):
        self.state = "starting"

    def stop(self):
        self.state = "stopped"

    def ask(self, *a, **k):
        return []


class FakeMonitor:
    def __init__(self):
        self.running = False
        self._detector = None
        self._cams = []

    def summary(self):
        return {"cameras": 4 if self.running else 0, "watching": 4 if self.running else 0}

    def camera_status(self):
        return {1: {"state": "watching", "fps": 5.0, "objects": {"person": 1, "car": 3}, "backend": "cpu", "error": None},
                5: {"state": "idle", "fps": 0.3, "objects": {"car": 2}, "backend": "cpu", "error": None},
                2: {"state": "offline", "fps": 0, "objects": {}, "backend": "cpu", "error": "Connection refused by the NVR"},
                7: {"state": "starting", "fps": 0, "objects": {}, "backend": "cpu", "error": None}}

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def imread(p):
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def make_alerts(folder: Path, site_id: str) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    specs = [
        ("check_ch1_20261023_010006.jpg", 1, "Yard parking", 40, "prowling", "Person checking several cars",
         "critical", "confirmed", "new", [("person", 1060, 520, 1180, 820), ("car", 615, 125, 1040, 355)],
         ["hand on a car door handle", "person looking into a car window"], "Visited 3 cars in 2 minutes."),
        ("check_ch5_20261023_010046.jpg", 5, "Yard / street side", 300, "night_presence",
         "Person near parked cars at night", "warning", "pending", "new",
         [("person", 1320, 30, 1370, 110), ("car", 642, 36, 895, 148)], [], "Seen in zone “Parking row”."),
        ("check_ch1_20261023_010006.jpg", 1, "Yard parking", 1500, "loitering", "Person waiting next to a car",
         "warning", "unverified", "new", [("person", 300, 560, 380, 760)], [], "Stayed 48 seconds in “Parking row”."),
        ("check_ch5_20261023_010046.jpg", 5, "Yard / street side", 3 * 3600, "car_moved", "Parked car moved",
         "warning", "rejected", "seen", [("car", 913, 88, 1258, 230)], ["no person near the car"],
         "The car shifted by about 4% of its width."),
        ("check_ch2_20261023_010028.jpg", 2, "Stairwell", 7 * 3600, "night_presence", "Person on the stairs at night",
         "info", "unverified", "false_alarm", [("person", 820, 300, 1000, 760)], [], "Seen in the whole picture."),
        ("check_ch7_20261023_010056.jpg", 7, "Hallway", 26 * 3600, "brain_detect",
         "AI found: person crouching next to a car near the gate with a long description",
         "critical", "confirmed", "seen", [("person crouching next to a car", 300, 200, 520, 480)],
         ["person crouching next to a car"], ""),
    ]
    out = []
    for i, (fname, ch, cam, age, rule, title, sev, verdict, status, boxes, findings, detail) in enumerate(specs):
        src = SNAPS / fname
        img = imread(src)
        snap = folder / f"a{i}_snap.jpg"
        shutil.copyfile(src, snap)
        x1 = min(b[1] for b in boxes); y1 = min(b[2] for b in boxes)
        x2 = max(b[3] for b in boxes); y2 = max(b[4] for b in boxes)
        crop = img[max(0, y1 - 60):y2 + 60, max(0, x1 - 60):x2 + 60]
        cp = folder / f"a{i}_crop.jpg"
        cp.write_bytes(cv2.imencode(".jpg", crop)[1].tobytes())
        out.append(Alert(id=uuid.uuid4().hex[:12], site_id=site_id, channel=ch, camera=cam, ts=now - age, rule=rule,
                         title=title, detail=detail, severity=sev, snapshot_path=str(snap), crop_path=str(cp),
                         boxes=[{"label": b[0], "x1": b[1], "y1": b[2], "x2": b[3], "y2": b[4]} for b in boxes],
                         verdict=verdict, brain_findings=findings, status=status))
    return out


app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
ctx = AppContext(app)
ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)
brain = FakeBrain()
ctx.brain = brain
ctx.monitor = FakeMonitor()
site = ctx.cfg.active_site()
alerts = make_alerts(OUT / "events" / "alerts" / "2026-10-23", site.id)
for a in alerts:
    ctx.alert_store.add(a)
ctx.apply_theme()
win = MainWindow(ctx)
win.settings.brain_card.brain = brain
win.live.overlay_combo.setCurrentIndex(win.live.overlay_combo.findData("off"))  # no YOLO load during the review
win.show()

# stop the dashboard / sites from probing the NVR with real ffmpeg grabs during the review
theme = "light"
size_tag = "1280"


def pump(seconds: float = 0.4) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def shot(name: str, widget=None) -> None:
    pump(0.3)
    w = widget or win
    p = OUT / "shots" / f"{size_tag}_{theme}_{name}.png"
    w.grab().save(str(p))
    print("shot", p.name, flush=True)


def settings_scroll(target=None, frac=None):
    sa = win.settings.findChild(QScrollArea)
    bar = sa.verticalScrollBar()
    if frac is not None:
        bar.setValue(int(bar.maximum() * frac))
    else:
        bar.setValue(target.mapTo(sa.widget(), target.rect().topLeft()).y() - 8)



from PySide6.QtCore import QPointF, Qt, QEvent
from PySide6.QtGui import QMouseEvent, QKeyEvent
from PySide6.QtWidgets import QWidget
def click(w, x, y, dbl=False):
    pos = QPointF(x, y)
    t = QEvent.MouseButtonDblClick if dbl else QEvent.MouseButtonPress
    app.sendEvent(w, QMouseEvent(t, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    app.sendEvent(w, QMouseEvent(QEvent.MouseButtonRelease, pos, pos, Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    app.sendEvent(w, QMouseEvent(QEvent.MouseMove, QPointF(x+80, y+40), QPointF(x+80,y+40), Qt.NoButton, Qt.NoButton, Qt.NoModifier))
win.resize(1280, 800)
for theme in ("light", "dark"):
    ctx.cfg.settings.theme = theme; ctx.apply_theme(); pump(0.5)
    frame = imread(SNAPS / "check_ch5_20261023_010046.jpg")
    cam5 = site.camera(5)
    dlg = ZoneEditorDialog(ctx, site, cam5, parent=win, frame=frame)
    dlg.show(); pump(0.8)
    cv = dlg.canvas
    r = cv.image_rect()
    for fx, fy in ((0.1, 0.5), (0.5, 0.45)):
        click(cv, r.x() + r.width()*fx, r.y() + r.height()*fy)
    pump(0.3); shot("20_zone_drawing_2pts", dlg)
    click(cv, r.x() + r.width()*0.55, r.y() + r.height()*0.9)
    pump(0.3); shot("21_zone_drawing_3pts", dlg)
    app.sendEvent(cv, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    pump(0.3); shot("22_zone_closed", dlg)
    dlg.done(0); dlg.deleteLater()
    # alerts filter with no match
    win.go_page(win.alerts); pump(1)
    win.alerts.findChild(QWidget)  # noop
    chips = [c for c in win.alerts.findChildren(QWidget) if c.objectName()=="Chip" or "Chip" in c.objectName()]
    # pill states
    ctx.monitor.running = False; win._refresh_monitoring(); pump(0.2)
    shot("23_alerts_monitor_off")
    win.pill._show("busy", "Starting…", "AI brain starting…"); shot("24_pill_starting")
    win.pill._show("error", "The AI could not start", ""); shot("25_pill_error")
# empty alerts store
for a in ctx.alert_store.list(1000):
    ctx.alert_store.delete(a.id)
win.alerts.reload(); pump(0.5)
win.go_page(win.alerts); pump(0.5)
shot("26_alerts_empty")
win.close(); pump(0.5)
os._exit(0)
