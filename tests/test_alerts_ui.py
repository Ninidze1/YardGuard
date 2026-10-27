"""Offscreen render + behaviour test for yardguard/alerts_ui.py.

Run:  set QT_QPA_PLATFORM=offscreen & set QT_QPA_FONTDIR=C:\\Windows\\Fonts
      .venv\\Scripts\\python.exe tests\\test_alerts_ui.py
Writes screenshots to .cache/tests/alerts_ui/. Uses copies of real night snapshots, never the originals.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "alerts_ui"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")  # never touch the user's config
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QHBoxLayout, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)

from local_data import SNAPS, need_snapshots  # noqa: E402

need_snapshots()


# --- engine (real one when available, else a test-only stand-in matching contract 4) ---------

def _engine():
    try:
        from yardguard.engine import Alert, AlertStore
        print("using yardguard.engine")
        return Alert, AlertStore
    except Exception as exc:  # engine is being written in parallel
        print(f"engine not importable ({exc.__class__.__name__}); using test stand-in")

    @dataclass
    class Alert:
        id: str
        site_id: str
        channel: int
        camera: str
        ts: float
        rule: str
        title: str
        detail: str
        severity: str
        snapshot_path: str
        crop_path: str
        boxes: list = field(default_factory=list)
        verdict: str = "pending"
        brain_findings: list = field(default_factory=list)
        status: str = "new"

    class AlertStore:
        def __init__(self, events_dir: str):
            Path(events_dir).mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(Path(events_dir) / "alerts.db"), check_same_thread=False)
            self._lock = threading.Lock()
            self._db.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, ts REAL, data TEXT)")

        def add(self, a):
            import json
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?)", (a.id, a.ts, json.dumps(asdict(a))))
                self._db.commit()

        update = add

        def list(self, limit=200, status=None, verdict=None, channel=None):
            import json
            with self._lock:
                rows = self._db.execute("SELECT data FROM alerts ORDER BY ts DESC").fetchall()
            out = [Alert(**json.loads(r[0])) for r in rows]
            out = [a for a in out if (status is None or a.status == status) and (verdict is None or a.verdict == verdict)
                   and (channel is None or a.channel == channel)]
            return out[:limit]

        def get(self, alert_id):
            return next((a for a in self.list(10_000) if a.id == alert_id), None)

        def mark(self, alert_id, status):
            a = self.get(alert_id)
            if a:
                a.status = status
                self.add(a)

        def delete(self, alert_id):
            with self._lock:
                self._db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                self._db.commit()

        def unread_count(self):
            return sum(1 for a in self.list(10_000) if a.status == "new")

    return Alert, AlertStore


Alert, AlertStore = _engine()

from yardguard import alerts_ui, theme  # noqa: E402
from yardguard.config import Camera, Site  # noqa: E402
from yardguard.gui import AppContext  # noqa: E402


class Bridge(QObject):
    alert_added = Signal(object)
    alert_updated = Signal(object)


class FakeWindow(QMainWindow):
    """Just enough of MainWindow for the alerts UI: sidebar with the pill, stack with the page."""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.notes: list[str] = []
        self.live_opened: list[int] = []
        root = QWidget()
        root.setObjectName("Root")
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(232)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 20, 16, 16)
        sl.addStretch(1)
        self.pill = alerts_ui.MonitorPill(ctx)
        sl.addWidget(self.pill)
        row.addWidget(side)
        self.stack = QStackedWidget()
        self.live = QWidget()
        self.page = alerts_ui.AlertsPage(ctx, self)
        self.stack.addWidget(self.page)
        self.stack.addWidget(self.live)
        row.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.resize(1280, 800)

    def notify(self, text, error=False):
        self.notes.append(("ERROR " if error else "") + text)

    def open_live(self, channel):
        self.live_opened.append(channel)

    def go(self, index):
        self.stack.setCurrentIndex(index)

    def set_monitoring(self, on):
        self.notes.append(f"monitoring {on}")


def apply_theme(ctx, mode: str) -> None:
    ctx.cfg.settings.theme = mode
    ctx.apply_theme()
    pal = ctx.palette
    assets = theme.write_assets(pal, ROOT / ".cache" / "ui")
    ctx.app.setStyleSheet(Template(theme.QSS.template + alerts_ui.EXTRA_QSS).substitute({**pal, **assets}))


def imread(path: Path):
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)


def make_alerts(folder: Path, site_id: str) -> list:
    """Realistic alerts built from copies of real night frames."""
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    specs = [
        # file, ch, camera, age_s, rule, title, severity, verdict, status, boxes, findings
        ("check_ch1_20261023_010006.jpg", 1, "Yard parking", 40, "prowling", "Person checking several cars",
         "critical", "confirmed", "new",
         [("person", 1060, 520, 1180, 820), ("car", 615, 125, 1040, 355), ("car", 1320, 450, 1885, 745)],
         ["hand on a car door handle", "person looking into a car window"]),
        ("check_ch5_20261023_010046.jpg", 5, "Yard / street side", 300, "night_presence",
         "Person near parked cars at night", "warning", "pending", "new",
         [("person", 1320, 30, 1370, 110), ("car", 642, 36, 895, 148), ("car", 913, 88, 1258, 230)], []),
        ("check_ch1_20261023_010006.jpg", 1, "Yard parking", 1500, "loitering", "Person waiting next to a car",
         "warning", "unverified", "new", [("person", 300, 560, 380, 760), ("car", 85, 177, 532, 507)], []),
        ("check_ch5_20261023_010046.jpg", 5, "Yard / street side", 3 * 3600, "car_moved", "Parked car moved",
         "warning", "rejected", "seen", [("car", 913, 88, 1258, 230)],
         ["no person near the car", "car is parked normally"]),
        ("check_ch2_20261023_010028.jpg", 2, "Stairwell", 7 * 3600, "night_presence", "Person on the stairs at night",
         "info", "unverified", "false_alarm", [("person", 820, 300, 1000, 760)], []),
        ("check_ch7_20261023_010056.jpg", 7, "Hallway", 26 * 3600, "brain_detect", "AI found: person crouching",
         "critical", "confirmed", "seen", [("person crouching next to a car", 300, 200, 520, 480)],
         ["person crouching next to a car"]),
        ("check_ch1_20261023_010006.jpg", 1, "Yard parking", 3 * 86400, "night_presence",
         "Person near parked cars at night", "warning", "confirmed", "seen",
         [("person", 1500, 300, 1580, 520)], ["person looking into a car window"]),
        ("check_ch5_20261023_010046.jpg", 5, "Yard / street side", 9 * 86400, "loitering",
         "Person waiting next to a car with a very long title that must be shortened nicely",
         "info", "rejected", "seen", [("person", 1700, 880, 1780, 1060)], []),
    ]
    alerts = []
    for i, (fname, ch, cam, age, rule, title, sev, verdict, status, boxes, findings) in enumerate(specs):
        src = SNAPS / fname
        img = imread(src)
        snap = folder / f"alert_{i}_snap.jpg"
        shutil.copyfile(src, snap)
        x1 = min(b[1] for b in boxes)
        y1 = min(b[2] for b in boxes)
        x2 = max(b[3] for b in boxes)
        y2 = max(b[4] for b in boxes)
        pad = 60
        crop = img[max(0, y1 - pad):min(img.shape[0], y2 + pad), max(0, x1 - pad):min(img.shape[1], x2 + pad)]
        crop_path = folder / f"alert_{i}_crop.jpg"
        crop_path.write_bytes(cv2.imencode(".jpg", crop)[1].tobytes())
        detail = {
            "prowling": "Visited 3 cars in 2 minutes.",
            "loitering": "Stayed 48 seconds in “Parking spots”.",
            "night_presence": "Seen in zone “Parking spots”.",
            "car_moved": "The car shifted by about 4% of its width.",
            "brain_detect": "",
        }[rule]
        alerts.append(Alert(
            id=uuid.uuid4().hex[:12], site_id=site_id, channel=ch, camera=cam, ts=now - age, rule=rule, title=title,
            detail=detail, severity=sev, snapshot_path=str(snap), crop_path=str(crop_path),
            boxes=[{"label": b[0], "x1": b[1], "y1": b[2], "x2": b[3], "y2": b[4]} for b in boxes],
            verdict=verdict, brain_findings=findings, status=status))
    return alerts


def shot(widget, name: str) -> None:
    QApplication.processEvents()
    path = OUT / f"{name}.png"
    widget.grab().save(str(path))
    print("saved", path.name)


def pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    ctx = AppContext(app)
    site = Site(name="Home yard", host="192.0.2.10",
                cameras=[Camera(1, "Yard parking", "Parking"), Camera(5, "Yard / street side", "Parking")])
    ctx.cfg.sites = [site]
    ctx.cfg.active_site_id = site.id
    ctx.cfg.settings.events_dir = str(OUT / "events")
    ctx.alert_store = AlertStore(str(OUT / "events"))
    ctx.alert_bridge = Bridge()
    failures: list[str] = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    alerts = make_alerts(OUT / "events" / "alerts" / "2026-10-23", site.id)
    for a in alerts:
        ctx.alert_store.add(a)

    for mode in ("light", "dark"):
        apply_theme(ctx, mode)
        win = FakeWindow(ctx)
        page = win.page
        toasts = alerts_ui.ToastManager(win, ctx)
        toasts.open_alert.connect(page.show_alert)
        win.show()
        page.reload()
        pump(0.8)  # thumbnails + picture load on the thread pool
        win.pill.set_state(True, 4, 4, "ready")
        shot(win, f"{mode}_01_page")

        if mode == "light":
            first = alerts[0]
            check(ctx.alert_store.get(first.id).status == "seen", "selecting an alert marks it seen")
            check(page.list.count() == len(alerts), f"list shows all {len(alerts)} alerts")
            unread = []
            page.unread_changed.connect(unread.append)
            page.reload()
            check(unread and unread[-1] == ctx.alert_store.unread_count(),
                  f"unread count matches the store ({unread[-1:]} vs {ctx.alert_store.unread_count()})")
            check("6 alerts" not in page.header.subtitle.text(), f"subtitle: {page.header.subtitle.text()}")

        # Live alert arrives + toasts
        live = make_alerts(OUT / "events" / "alerts" / "live", site.id)[1]
        live.ts = time.time()
        live.title = "Person near parked cars at night"
        ctx.alert_store.add(live)
        ctx.alert_bridge.alert_added.emit(live)
        toasts.show_alert(alerts[0])
        toasts.show_alert(alerts[2])
        toasts.show_alert(live)
        pump(0.5)
        shot(win, f"{mode}_02_live_toasts")
        if mode == "light":
            check(page.list.item(0).data(alerts_ui.ID_ROLE) == live.id, "live alert inserted at the top")
            check(len(toasts.toasts) == 3, "three toasts stacked")
            toasts.show_alert(alerts[3])
            check(len(toasts.toasts) == 3, "never more than three toasts")
            # AI verdict arrives
            live.verdict, live.brain_findings, live.severity = "confirmed", ["hand on a car door handle"], "critical"
            ctx.alert_bridge.alert_updated.emit(live)
            toasts.update_alert(live)
            pump(0.1)
            check(page.alerts[live.id].verdict == "confirmed", "verdict updated in place")
            # click on toast opens alert
            toasts.toasts[0].clicked.emit(toasts.toasts[0].alert_id)
            pump(0.1)
            check(page._current_id() == alerts[3].id, "toast click selects that alert")

        toasts.clear()
        # Filters
        for chip in page.chips.buttons():
            if chip.property("key") == "review":
                chip.click()
        pump(0.5)
        shot(win, f"{mode}_03_filter_review")
        if mode == "light":
            ids = {page.list.item(i).data(alerts_ui.ID_ROLE) for i in range(page.list.count())}
            check(all(page.alerts[i].verdict in ("pending", "unverified") for i in ids), "review filter")
            idx = page.camera_combo.findText("Hallway")
            page.camera_combo.setCurrentIndex(idx)
            pump(0.2)
            check(page.left.currentIndex() == 1, "no-match state for camera with nothing to review")
            shot(win, f"{mode}_04_no_match")
            page.clear_filters()
            pump(0.2)

            # False alarm / undo
            a = page.alerts[page._current_id()]
            page.toggle_false_alarm(a)
            check(ctx.alert_store.get(a.id).status == "false_alarm", "mark false alarm saved")
            page.toggle_false_alarm(a)
            check(ctx.alert_store.get(a.id).status == "seen", "undo false alarm")

            # Delete (confirm dialog auto-answered)
            orig_exec = QMessageBox.exec
            QMessageBox.exec = lambda box: next(b for b in box.buttons() if b.text() == "Delete").click()
            target = page.alerts[page._current_id()]
            page.delete_alert(target)
            QMessageBox.exec = orig_exec
            check(ctx.alert_store.get(target.id) is None, "delete removes from store")
            pump(1.5)  # file removal is retried while a picture load still has it open
            check(not Path(target.snapshot_path).exists(), f"delete removes the picture ({win.notes[-1:]})")
            check(page._current_id() is not None, "another alert selected after delete")

            # Enter opens live view
            page.list.setFocus()
            page._activate(page.list.currentItem())
            check(bool(win.live_opened), "Enter/double-click opens live view")

            page.mark_all_seen()
            check(ctx.alert_store.unread_count() == 0, "mark all as seen")
        else:
            page.clear_filters()
            pump(0.3)
            # Scroll detail to show findings
            page.show_alert(alerts[5].id)
            pump(0.6)
            shot(win, f"{mode}_04_detail_brain")

        # Pill states
        pill_shots = []
        for state in ((False, 4, 0, "off"), (True, 4, 0, "starting"), (True, 4, 3, "busy"),
                      (True, 4, 4, "ready"), (True, 0, 0, "not_installed")):
            win.pill.set_state(*state)
            QApplication.processEvents()
            pill_shots.append(win.pill.grab())
        h = sum(p.height() + 8 for p in pill_shots)
        from PySide6.QtGui import QPainter, QPixmap
        canvas = QPixmap(win.pill.width() + 16, h + 8)
        canvas.fill(Qt.transparent)
        canvas.fill(win.palette().window().color())
        painter = QPainter(canvas)
        y = 8
        for p in pill_shots:
            painter.drawPixmap(8, y, p)
            y += p.height() + 8
        painter.end()
        canvas.save(str(OUT / f"{mode}_05_pill_states.png"))

        # Empty state
        empty_store = AlertStore(str(OUT / f"empty_{mode}"))
        ctx.alert_store = empty_store
        page.reload()
        toasts.clear()
        shot(win, f"{mode}_06_empty")
        ctx.alert_store = AlertStore(str(OUT / "events"))
        win.close()
        win.deleteLater()
        pump(0.2)

    print("notes sample:", win.notes[:6])
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
