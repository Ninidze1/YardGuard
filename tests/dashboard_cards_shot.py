"""Render the dashboard camera cards in several states (light and dark) and check that a status
change can never change a card's height.

Usage:  run.cmd tests\\dashboard_cards_shot.py      -> .cache/tests/dashboard/*.png
"""
import json
import os
import shutil
import sys
from pathlib import Path

from local_data import need_config

need_config()
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "dashboard"
OUT.mkdir(parents=True, exist_ok=True)
cfg_copy = OUT / "config.json"
shutil.copy(ROOT / "config.json", cfg_copy)
data = json.loads(cfg_copy.read_text(encoding="utf-8"))
data["settings"]["monitoring_on"] = False
data["settings"]["events_dir"] = str(OUT / "events")
cfg_copy.write_text(json.dumps(data), encoding="utf-8")
os.environ["YARDGUARD_CONFIG"] = str(cfg_copy)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
from PySide6.QtCore import QEventLoop, QSettings, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from yardguard import gui  # noqa: E402
from yardguard.qt_util import to_qimage  # noqa: E402

failures = 0


def check(ok: bool, name: str) -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name)


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
frames = {int(p.stem[2:]): cv2.imread(str(p)) for p in (ROOT / ".cache" / "tests" / "frames").glob("ch*.png")}
STATES = [("online", {"state": "watching", "objects": {"car": 11, "person": 1}, "fps": 4.0}),
          ("online", {"state": "idle", "objects": {"car": 3}}),
          ("offline", None),
          ("online", None)]

for theme in ("light", "dark"):
    ctx = gui.AppContext(app)
    ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)
    ctx.cfg.settings.theme = theme
    ctx.apply_theme()
    win = gui.MainWindow(ctx)
    win.resize(1280, 800)
    win.show()
    dash = win.dashboard
    dash.timer.stop()
    fake = {}  # pinned monitoring states: the app's own 1.5 s status timer must not overwrite them
    win.monitor_status = lambda: fake
    orig_set = gui.CameraCard.set_monitor
    pump(300)
    cards = list(dash.cards.values())
    for card, (conn, mon) in zip(cards, STATES):
        img = frames.get(card.cam.channel)
        card.thumb.set_image(to_qimage(img) if img is not None and conn != "offline" else None,
                             "No connection" if conn == "offline" else "")
        card.set_connection(conn)
        fake[card.cam.channel] = mon
        card.set_monitor(mon)
    pump(600)
    win.grab().save(str(OUT / f"dashboard_{theme}_1280.png"))
    heights = [c.height() for c in cards]
    # the case that used to reflow the whole row: a long status on one card
    fake[cards[0].cam.channel] = {"state": "watching", "fps": 4.0,
                                  "objects": {"car": 14, "person": 3, "truck": 2, "motorcycle": 1}}
    fake[cards[1].cam.channel] = {"state": "offline", "error": "Camera not reachable. The recorder did not "
                                                                "answer for 30 seconds, retrying every 10 seconds."}
    cards[0].set_monitor(fake[cards[0].cam.channel])
    cards[1].set_monitor(fake[cards[1].cam.channel])
    pump(600)
    check([c.height() for c in cards] == heights, f"{theme}: card heights unchanged by long statuses {heights}")
    check(all(c.detail.text().endswith("…") or c.detail.fontMetrics().horizontalAdvance(c.detail.text())
              <= c.detail.width() for c in cards), f"{theme}: detail lines fit on one line")
    win.grab().save(str(OUT / f"dashboard_{theme}_1280_long.png"))
    win.resize(1600, 1000)
    pump(400)
    win.grab().save(str(OUT / f"dashboard_{theme}_1600.png"))
    win.close()
    pump(200)

print(f"{failures} failure(s)")
sys.stdout.flush()
os._exit(1 if failures else 0)  # skip Qt teardown (the offscreen platform can crash while exiting)
