"""Offscreen behaviour checks and screenshots for yardguard/areas_ui.py and areas_canvas.py.

Run:  run.cmd tests\\test_areas_ui.py
PNGs go to .cache\\tests\\areas-ui\\. Uses a synthetic config (never the real config.json), a fake
area-event store with the AlertStore API and a fake Monitor.area_live, so no camera, GPU or real
alerts database is touched. Real camera frames from .cache\\tests\\frames are used as pictures.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "areas-ui"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal  # noqa: E402
from PySide6.QtGui import QFont, QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from yardguard import config  # noqa: E402
from yardguard.config import Camera, Config, Site  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"

FRAMES = ROOT / ".cache" / "tests" / "frames"
from local_data import need  # noqa: E402

need(FRAMES / "ch1.png", what="camera frames in .cache/tests/frames (made by tests/grab_frames.py)")
PICS = OUT / "pics"
FAILS: list[str] = []


def check(cond, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


# --- fakes -------------------------------------------------------------------------------------

@dataclass
class AreaEvent:
    id: str
    site_id: str
    channel: int
    area_id: str
    area_name: str
    task: str
    ts: float
    kind: str
    label: str = ""
    track_id: int = 0
    value: str = ""
    confidence: float = 1.0
    snapshot_path: str = ""
    crop_path: str = ""
    clip_path: str = ""
    extra: dict = field(default_factory=dict)


class FakeStore:
    """The area-event part of AlertStore, in memory."""

    def __init__(self):
        self.events: list[AreaEvent] = []
        self.deleted: list[str] = []
        self.exported: list[tuple] = []

    def add_area_event(self, e) -> str:
        self.events.append(e)
        return e.id

    def list_area_events(self, area_id=None, channel=None, since=None, until=None, kind=None, query=None,
                         limit=500):
        out = []
        for e in sorted(self.events, key=lambda x: x.ts, reverse=True):
            if area_id is not None and e.area_id != area_id:
                continue
            if channel is not None and e.channel != channel:
                continue
            if since is not None and e.ts < since:
                continue
            if until is not None and e.ts >= until:
                continue
            if kind is not None and e.kind != kind:
                continue
            if query and query.lower() not in f"{e.value} {e.label} {e.area_name}".lower():
                continue
            out.append(e)
        return out[:limit]

    def export_area_events_csv(self, path, **filters):
        self.exported.append((path, filters))

    def delete_area_events(self, area_id):
        self.deleted.append(area_id)
        self.events = [e for e in self.events if e.area_id != area_id]

    def list(self, limit=200, status=None, verdict=None, channel=None):
        return []


class FakeMonitor:
    def __init__(self):
        self.running = True
        self.live: dict[int, dict] = {}
        self.resets: list = []

    def area_live(self, channel):
        return self.live.get(channel, {})

    def reset_area_reference(self, channel, area_id):
        self.resets.append((channel, area_id))


class FakeBrain:
    state = "ready"

    def status(self):
        return self.state

    def ask(self, image, phrases, mode="fast", upscale=False, **kw):
        return [{"label": phrases[0], "x1": 1, "y1": 1, "x2": 5, "y2": 5}]


class Bridge(QObject):
    area_event_added = Signal(object)


class Ctx(QObject):
    config_changed = Signal()

    def __init__(self, app, cfg):
        super().__init__()
        from yardguard.theme import PALETTES
        self.app, self.cfg = app, cfg
        self.palette = PALETTES["light"]
        self.alert_store = FakeStore()
        self.monitor = FakeMonitor()
        self.brain = FakeBrain()
        self.alert_bridge = Bridge()
        self._bindings = []
        self.saves = 0

    def apply_theme(self, name: str) -> None:
        from yardguard.alerts_ui import EXTRA_QSS as A
        from yardguard.areas_ui import EXTRA_QSS as AR
        from yardguard.incidents_ui import EXTRA_QSS as I
        from yardguard.settings_ui import EXTRA_QSS as S
        from yardguard.theme import PALETTES, build_qss, qt_palette, write_assets
        self.palette = PALETTES[name]
        self.app.setPalette(qt_palette(self.palette))
        assets = write_assets(self.palette, OUT / "ui")
        self.app.setStyleSheet(build_qss(self.palette, assets, A + I + AR) + S)
        for w, icon, role in list(self._bindings):
            try:
                w.setIcon(self.icon(icon, role))
            except RuntimeError:
                pass

    def icon(self, name, role="text", size=18):
        from yardguard.theme import svg_icon
        p = self.palette
        if role == "nav":
            return svg_icon(name, p["muted"], active=p["nav_active_text"], size=size)
        return svg_icon(name, p[role], size=size)

    def bind_icon(self, widget, name, role="text"):
        from PySide6.QtWidgets import QPushButton
        if isinstance(widget, QPushButton) and widget.text() and not widget.text().startswith(" "):
            widget.setText(" " + widget.text())
        widget.setIcon(self.icon(name, role))
        self._bindings.append((widget, name, role))

    def save(self):
        self.saves += 1
        self.cfg.save()
        self.config_changed.emit()


class Win(QWidget):
    def __init__(self):
        super().__init__()
        self.notes: list[tuple[str, bool]] = []
        self.settings = QWidget()

    def notify(self, text, error=False):
        self.notes.append((text, error))

    def go_page(self, page):
        pass

    def set_monitoring(self, on):
        pass


# --- helpers -------------------------------------------------------------------------------------

def frame(ch: int) -> np.ndarray:
    img = cv2.imread(str(FRAMES / f"ch{ch}.png"))
    if img is None:
        img = np.full((1080, 1920, 3), 60, np.uint8)
        cv2.rectangle(img, (300, 500), (900, 900), (90, 110, 130), -1)
    return img


def qimg(ch: int):
    from yardguard.qt_util import to_qimage
    return to_qimage(frame(ch))


def mouse(w, kind, pos, button=Qt.LeftButton, buttons=None, mods=Qt.NoModifier):
    types = {"press": QEvent.MouseButtonPress, "release": QEvent.MouseButtonRelease, "move": QEvent.MouseMove,
             "dbl": QEvent.MouseButtonDblClick}
    p = QPointF(*pos) if not isinstance(pos, QPointF) else pos
    if buttons is None:
        buttons = Qt.NoButton if kind in ("release",) else button
    if kind == "move" and buttons is None:
        buttons = Qt.NoButton
    ev = QMouseEvent(types[kind], p, w.mapToGlobal(p), button if kind != "move" else Qt.NoButton, buttons, mods)
    QApplication.sendEvent(w, ev)


def click(w, pos, button=Qt.LeftButton, mods=Qt.NoModifier):
    mouse(w, "move", pos, buttons=Qt.NoButton, mods=mods)
    mouse(w, "press", pos, button, mods=mods)
    mouse(w, "release", pos, button, buttons=Qt.NoButton, mods=mods)


def drag(w, a, b, steps=8, mods=Qt.NoModifier):
    mouse(w, "move", a, buttons=Qt.NoButton)
    mouse(w, "press", a, mods=mods)
    for i in range(1, steps + 1):
        t = i / steps
        mouse(w, "move", (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), buttons=Qt.LeftButton, mods=mods)
    mouse(w, "release", b, buttons=Qt.NoButton, mods=mods)


def dbl(w, pos):
    mouse(w, "press", pos)
    mouse(w, "release", pos, buttons=Qt.NoButton)
    mouse(w, "dbl", pos)
    mouse(w, "release", pos, buttons=Qt.NoButton)


def key(w, k, mods=Qt.NoModifier):
    QApplication.sendEvent(w, QKeyEvent(QEvent.KeyPress, k, mods))
    QApplication.sendEvent(w, QKeyEvent(QEvent.KeyRelease, k, mods))


def pump(n=5):
    app = QApplication.instance()
    for _ in range(n):
        app.processEvents()
        time.sleep(0.01)


def px(canvas, x, y):
    p = canvas.to_px([x, y])
    return p.x(), p.y()


def make_config() -> Config:
    cams = [Camera(1, "Yard / street side", "Yard"), Camera(5, "Parking", "Parking"), Camera(3, "Back door", "Yard")]
    site = Site(name="Home", host="192.0.2.10", cameras=cams, id="site1")
    cfg = Config(sites=[site], active_site_id="site1")
    for c in cams:
        if not hasattr(c, "areas"):
            c.areas = []
    return cfg


def area(name, task, shape, points, **kw) -> dict:
    a = {"id": uuid.uuid4().hex[:8], "name": name, "shape": shape, "points": points, "direction": "both",
         "task": task, "objects": [], "schedule": {"mode": "always", "start": "22:00", "end": "06:00"},
         "params": {}, "alert": False, "enabled": True, "color": 0}
    a.update(kw)
    return a


def crop_file(ch: int, box, name: str, size=None) -> str:
    PICS.mkdir(parents=True, exist_ok=True)
    img = frame(ch)
    h, w = img.shape[:2]
    x1, y1, x2, y2 = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
    c = img[y1:y2, x1:x2]
    if size:
        c = cv2.resize(c, size)
    path = PICS / name
    ok, data = cv2.imencode(".jpg", c)
    path.write_bytes(data.tobytes())
    return str(path)


def populate(ctx) -> dict:
    """Areas on two cameras plus a week of synthetic events."""
    site = ctx.cfg.sites[0]
    c1, c5 = site.camera(1), site.camera(5)
    gate = area("Gate line", "count", "line", [[0.22, 0.62], [0.68, 0.70]], objects=["car"],
                params={"in_name": "In", "out_name": "Out"}, color=0)
    barrier = area("Barrier", "log_plates", "line", [[0.30, 0.80], [0.80, 0.86]], direction="both",
                   objects=["car", "truck"], params={"min_plate_px": 80, "known": ["AA-123-BB", "GE-777-TT"],
                                                     "in_name": "In", "out_name": "Out"}, color=2)
    spot = area("Parking spot 1", "parking_spot", "polygon", [[0.06, 0.40], [0.22, 0.38], [0.25, 0.55], [0.07, 0.58]],
                objects=["car", "truck"], params={"occupied_after_s": 20}, color=1)
    c1.areas = [gate, barrier, spot]
    arm = area("Barrier arm", "ask_ai", "polygon", [[0.55, 0.20], [0.80, 0.20], [0.80, 0.42], [0.55, 0.42]],
               params={"question": "the barrier arm is raised", "interval_s": 30}, color=3)
    door = area("Front door", "loitering", "polygon", [[0.10, 0.55], [0.35, 0.50], [0.40, 0.85], [0.12, 0.90]],
                objects=["person"], params={"seconds": 30}, alert=True, color=4)
    off = area("Old zone", "watch_zone", "polygon", [[0.6, 0.6], [0.9, 0.6], [0.9, 0.95], [0.6, 0.95]],
               enabled=False, color=5)
    c5.areas = [arm, door, off]
    store = ctx.alert_store
    now = time.time()
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rng = np.random.default_rng(7)
    car = crop_file(1, (0.3, 0.45, 0.6, 0.85), "car.jpg")
    snap = crop_file(1, (0, 0, 1, 1), "snap1.jpg", (960, 540))
    plates = ["AA-123-BB", "GE-777-TT", "TB-405-KK", "LL-902-AA", "MK-310-OP", "AB-001-CD"]
    plate_crops = {p: crop_file(1, (0.42 + 0.01 * i, 0.70, 0.52 + 0.01 * i, 0.74), f"plate{i}.jpg", (180, 60))
                   for i, p in enumerate(plates)}

    def ev(a, cam, ts, kind, **kw):
        store.add_area_event(AreaEvent(uuid.uuid4().hex[:12], "site1", cam.channel, a["id"], a["name"], a["task"],
                                       ts, kind, **kw))

    for day in range(7):
        d0 = start - 86400 * day
        for h in range(24):
            busy = 4 if 7 <= h <= 9 or 17 <= h <= 19 else 1 if 6 <= h <= 22 else 0
            for _ in range(int(rng.poisson(busy * 1.5))):
                ts = d0 + h * 3600 + rng.uniform(0, 3500)
                if ts < now:
                    ev(gate, c1, ts, "cross_in" if rng.random() < 0.55 else "cross_out", label="car",
                       crop_path=car, snapshot_path=snap)
        t = d0 + 7 * 3600
        while t < d0 + 86400 and t < now:   # parking spot: taken / free
            stay = rng.uniform(0.5, 4) * 3600
            ev(spot, c1, t, "occupied", label="car", crop_path=car, snapshot_path=snap,
               extra={"plate": plates[int(rng.integers(0, 3))]})
            if t + stay < now:
                ev(spot, c1, t + stay, "freed", label="car", extra={"duration_s": stay}, crop_path=car)
            t += stay + rng.uniform(0.3, 2) * 3600
    for i in range(22):  # plates today
        ts = start + 6 * 3600 + i * 2400 + rng.uniform(0, 600)
        if ts > now:
            ts = now - (22 - i) * 600
        p = plates[i % len(plates)]
        d = "in" if i % 2 == 0 else "out"
        extra = {"direction": d}
        if d == "out":
            extra["stayed_s"] = float(rng.uniform(600, 9000))
        ev(barrier, c1, ts, "plate", label="car", value=p if i != 5 else "TB-4O5-K", confidence=0.9 if i != 5 else 0.4,
           crop_path=plate_crops[p], snapshot_path=snap, extra=extra)
    for i in range(10):  # ask the AI
        ts = now - 9 * 3600 + i * 3000
        ev(arm, c5, ts, "answer_yes" if i % 2 == 0 else "answer_no",
           crop_path=crop_file(5, (0.55, 0.2, 0.8, 0.42), "arm.jpg"))
    for i in range(5):
        ev(door, c5, now - i * 5000 - 600, "loitering", label="person", extra={"duration_s": 40 + i * 25},
           crop_path=crop_file(5, (0.1, 0.5, 0.4, 0.9), "door.jpg"))
    ctx.monitor.live = {
        1: {gate["id"]: {"state": "idle", "count_in": 12, "count_out": 9},
            barrier["id"]: {"state": "idle", "plate": "AA-123-BB", "since": now - 130},
            spot["id"]: {"state": "occupied", "occupied_by": "car", "since": now - 14 * 60}},
        5: {arm["id"]: {"state": "yes", "since": now - 30}, door["id"]: {"state": "idle"}},
    }
    return {"gate": gate, "barrier": barrier, "spot": spot, "arm": arm, "door": door, "off": off}


# --- unit checks -----------------------------------------------------------------------------------

def test_helpers():
    from yardguard import areas_canvas as ac
    from yardguard import areas_ui as au
    # direction words: 8 rotations, and the arrow agrees with side_of
    for k in range(8):
        ang = k * math.pi / 4
        a = (500.0, 300.0)
        b = (500 + 200 * math.cos(ang), 300 + 200 * math.sin(ang))
        n = ac.crossing_normal(a, b)
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        check(ac.side_of(a, b, (mid[0] + n[0] * 5, mid[1] + n[1] * 5)) > 0, f"arrow points to the a_to_b side ({k})")
        p0 = (mid[0] - n[0] * 10, mid[1] - n[1] * 10)
        p1 = (mid[0] + n[0] * 10, mid[1] + n[1] * 10)
        check(ac.crossed(a, b, p0, p1) == 1 and ac.crossed(a, b, p1, p0) == -1, f"crossing sign matches ({k})")
        labels = ac.line_direction_labels(a, b)
        check(labels["a_to_b"][1] != labels["b_to_a"][1], f"two directions get different words ({k})")
    try:
        from yardguard import areas as engine_areas
        check(ac.crossing_normal is engine_areas.arrow_normal and ac.side_of is engine_areas.side_of,
              "the drawn arrow uses the engine's own geometry")
    except ImportError:
        print("skip engine geometry check (yardguard.areas missing)")
    # horizontal line A left, B right: a_to_b goes down = towards the camera
    check(ac.line_direction_labels((0, 0), (10, 0))["a_to_b"][1] == "towards the camera", "a_to_b on A->B east is down")
    check(ac.line_direction_labels((0, 0), (0, 10))["a_to_b"][1] == "right to left", "a_to_b on A->B south is left")
    # crossing outside the segment extent does not count
    check(ac.crossed((0, 0), (10, 0), (20, -5), (20, 5)) == 0, "crossing beyond the line's end is ignored")
    # polygon checks
    bow = [[0, 0], [1, 1], [1, 0], [0, 1]]
    check(bool(ac.crossing_edges(bow)), "bow-tie polygon detected as self-crossing")
    check(not ac.crossing_edges([[0, 0], [1, 0], [1, 1], [0, 1]]), "square is fine")
    check(abs(ac.poly_area([[0, 0], [1, 0], [1, 1], [0, 1]]) - 1) < 1e-9, "polygon area")
    check(ac.point_in_poly((0.5, 0.5), [[0, 0], [1, 0], [1, 1], [0, 1]]), "point in polygon")
    # templates
    check(ac.fits_shape("log_plates", "line") and not ac.fits_shape("log_plates", "polygon"), "plates need a line")
    check(ac.fits_shape("count", "line") and ac.fits_shape("count", "polygon"), "count takes both shapes")
    check(ac.shape_badge("line_cross") == "Area or line", "shape badge")
    check(au.recommended_template(ac.Draft("line", [[0, 0], [1, 1]], True)) == "count", "line recommends count")
    check(au.recommended_template(ac.Draft("polygon", [[0, 0], [0.1, 0], [0.1, 0.1], [0, 0.1]], True))
          == "parking_spot", "small rectangle recommends parking spot")
    for key in ac.TEMPLATE_UI:
        check(isinstance(ac.template_params(key), dict), f"template params {key}")
    # wording for every kind
    gate = {"params": {"in_name": "Arriving", "out_name": "Leaving", "question": "the gate is open"}}
    cases = {
        "enter": ("person", "", {}, "Person entered"), "exit": ("car", "", {}, "Car left"),
        "cross_in": ("car", "", {}, "Car crossed · Arriving"), "cross_out": ("car", "", {}, "Car crossed · Leaving"),
        "count": ("car", "3", {}, "3 cars counted"), "occupied": ("car", "", {"plate": "AA-123-BB"},
                                                                  "Taken by a car · AA-123-BB"),
        "freed": ("car", "", {"duration_s": 3600}, "Free again after 1 h"),
        "blocked": ("truck", "", {"duration_s": 360}, "Truck parked for 6 min"),
        "loitering": ("person", "", {"duration_s": 45}, "Person stayed 45 s"),
        "presence": ("person", "", {}, "Person entered"),
        "plate": ("car", "AA-123-BB", {"direction": "out", "stayed_s": 8040}, "AA-123-BB went out · stayed 2 h 14 min"),
        "appeared": ("", "", {}, "Something new appeared"), "removed": ("", "", {}, "Something was taken away"),
        "changed": ("", "", {"duration_s": 180}, "Looks different for 3 min"),
        "answer_yes": ("", "", {}, "Yes: the gate is open"), "answer_no": ("", "", {}, "No longer: the gate is open"),
    }
    for kind, (lab, value, extra, want) in cases.items():
        e = AreaEvent("x", "s", 1, "a", "Gate", "count", time.time(), kind, label=lab, value=value, extra=extra)
        got = ac.event_text(e, gate)
        check(got == want, f"event_text {kind}: {got!r}")
    # stats
    now = time.time()
    ds = au.day_start(now)
    evs = [AreaEvent("1", "s", 1, "a", "A", "count", ds + 3600 * 8 + 5, "cross_in"),
           AreaEvent("2", "s", 1, "a", "A", "count", ds + 3600 * 8 + 9, "cross_out"),
           AreaEvent("3", "s", 1, "a", "A", "count", ds + 3600 * 9 + 9, "count", value="4", extra={"direction": "in"})]
    b = au.bucket_counts(evs, ds, 24)
    check(b[8] == (1, 1) and b[9] == (4, 0), f"hour buckets {b[8]} {b[9]}")
    check(au.busiest(b) == 9, "busiest hour")
    segs = au.state_segments([AreaEvent("1", "s", 1, "a", "A", "p", ds - 100, "occupied"),
                              AreaEvent("2", "s", 1, "a", "A", "p", ds + 100, "freed"),
                              AreaEvent("3", "s", 1, "a", "A", "p", ds + 500, "occupied")], ("occupied",), ("freed",),
                             ds, ds + 86400, now=ds + 1000)
    check([(round(s - ds), round(t - ds)) for s, t, _ in segs] == [(0, 100), (500, 1000)], f"segments {segs}")
    check(au.normalise_plate("aa 123 bb") == "AA-123-BB" and au.normalise_plate("aa123bb") == "AA-123-BB",
          "plate normalising")
    pe = [AreaEvent("1", "s", 1, "a", "A", "log_plates", now, "plate", value="AA-123-BB", extra={"direction": "in"}),
          AreaEvent("2", "s", 1, "a", "A", "log_plates", now, "plate", value="ZZ-999-ZZ", extra={"direction": "in"}),
          AreaEvent("3", "s", 1, "a", "A", "log_plates", now, "plate", value="Q", confidence=0.2)]
    s = au.plate_summary(pe, ["aa123bb"])
    check(s == {"vehicles": 3, "inside": 2, "visitors": 1, "unsure": 1}, f"plate summary {s}")
    check(au.default_name("parking_spot", "polygon", ["Parking spot 1"]) == "Parking spot 2", "numbered default name")
    check(au.default_name("count", "line", ["Gate line"]) == "Gate line 2", "unique default name")
    check(ac.stat_text({"task": "count", "shape": "line", "direction": "both", "params": {}},
                       {"count_in": 12, "count_out": 9}, True) == "In 12 · Out 9", "count stat line")
    check(ac.stat_text({"task": "count"}, {"count_in": 3}, False) == "Not watching", "no stale numbers")
    check(ac.stat_text({"task": "parking_spot"}, {"state": "occupied", "since": now - 840}, True, now)
          == "Taken · 14 min", "parking stat line")


def test_canvas(ctx):
    from yardguard.areas_canvas import AreaCanvas, Draft
    c = AreaCanvas(ctx)
    c.resize(960, 540)
    c.set_image(qimg(1))
    c.show()
    pump()
    # geometry round trip and clamping
    p = c.to_px([0.25, 0.75])
    check(c.to_norm(p) == [0.25, 0.75], "to_norm(to_px(p)) == p")
    check(c.to_norm(QPointF(-50, 9999)) == [0.0, 1.0], "points are clamped to the picture")
    # polygon by clicks + double-click (double-click must not add a duplicate)
    c.begin_draft("polygon", 0)
    for x, y in ((0.2, 0.2), (0.5, 0.2), (0.5, 0.5)):
        click(c, px(c, x, y))
    dbl(c, px(c, 0.2, 0.5))
    check(c.draft.closed and len(c.draft.points) == 4, f"polygon by clicks + double-click ({len(c.draft.points)})")
    check(c.mode == "edit", "closed polygon goes to edit mode")
    # undo / redo
    c.undo()
    check(not c.draft.closed and len(c.draft.points) == 4, "undo reopens the polygon")
    c.undo()
    check(len(c.draft.points) == 3, "undo removes the last corner")
    c.redo()
    c.redo()
    check(c.draft.closed and len(c.draft.points) == 4, "redo restores")
    # vertex drag (one undo step)
    before = c.undo_stack.count()
    drag(c, px(c, 0.5, 0.5), px(c, 0.6, 0.6))
    check(abs(c.draft.points[2][0] - 0.6) < 0.01 and c.undo_stack.count() == before + 1, "vertex drag is one step")
    # midpoint insert
    mid = ((0.2 + 0.5) / 2, 0.2)
    drag(c, px(c, *mid), px(c, 0.35, 0.1))
    check(len(c.draft.points) == 5 and abs(c.draft.points[1][1] - 0.1) < 0.01, "dragging a middle dot adds a corner")
    # right-click delete
    click(c, px(c, *c.draft.points[1]), Qt.RightButton)
    check(len(c.draft.points) == 4, "right-click removes a corner")
    # move whole shape, clamped to the picture
    pts0 = [list(p) for p in c.draft.points]
    drag(c, px(c, 0.35, 0.35), px(c, 0.35 - 0.9, 0.35))
    check(min(p[0] for p in c.draft.points) == 0.0 and c.draft.points != pts0, "moving the shape stays on the picture")
    # self-crossing stops finishing
    c.begin_draft("polygon", 0)
    for x, y in ((0.1, 0.1), (0.4, 0.4), (0.4, 0.1), (0.1, 0.4)):
        click(c, px(c, x, y))
    key(c, Qt.Key_Return)
    check(not c.draft.closed and "cross" in c.hint_text(), "twisted shape is refused with a hint")
    key(c, Qt.Key_Backspace)
    check(len(c.draft.points) == 3, "Backspace removes the last point")
    # 4 px dedupe
    n = len(c.draft.points)
    a = px(c, *c.draft.points[-1])
    click(c, (a[0] + 2, a[1] + 1))
    check(len(c.draft.points) == n, "a click within 4 px of the last corner adds nothing")
    # rectangle by drag
    c.begin_draft("polygon", 0)
    drag(c, px(c, 0.6, 0.6), px(c, 0.8, 0.8))
    check(c.draft.closed and len(c.draft.points) == 4, "press-drag draws a rectangle")
    # line by drag, arrow click cycles direction
    c.begin_draft("line", 1)
    drag(c, px(c, 0.2, 0.5), px(c, 0.8, 0.55))
    check(c.draft.closed and len(c.draft.points) == 2, "line by drag")
    mid = (c.to_px(c.draft.points[0]) + c.to_px(c.draft.points[1])) / 2
    seen = []
    for _ in range(3):
        click(c, (mid.x(), mid.y()))
        seen.append(c.draft.direction)
    check(seen == ["a_to_b", "b_to_a", "both"], f"arrow click cycles direction {seen}")
    lab = c.direction_labels()
    check(lab["a_to_b"][1] == "towards the camera", f"a_to_b words for this line: {lab['a_to_b']}")
    # click-click line (trackpads)
    c.begin_draft("line", 1)
    click(c, px(c, 0.3, 0.3))
    check(len(c.draft.points) == 1 and not c.draft.closed, "first click places A")
    click(c, px(c, 0.6, 0.3))
    check(c.draft.closed, "second click places B")
    # L / P keys, short line warning
    c.begin_draft("polygon", 0)
    key(c, Qt.Key_L)
    check(c.draft.shape == "line", "L switches to line")
    drag(c, px(c, 0.5, 0.5), px(c, 0.53, 0.5))
    check("Short lines" in c.validity()[1], "short line warning")
    # Esc ladder on the canvas: drop points
    c.begin_draft("polygon", 0)
    click(c, px(c, 0.2, 0.2))
    click(c, px(c, 0.3, 0.2))
    check(c.drop_in_progress() and not c.draft.points, "Esc drops unfinished points")
    # freeze frame keeps the picture while new frames arrive
    c.set_frozen(True)
    old = c.image()
    c.set_image(qimg(5))
    check(c.image() is old and c.latest_image() is not old, "freeze keeps the frame")
    c.set_frozen(False)
    check(c.image() is c.latest_image(), "thaw shows the newest frame")
    # camera offline while drawing keeps the picture
    c.begin_draft("polygon", 0)
    c.set_image(None, "Connection lost")
    check(c.image() is not None and c.frozen and "offline" in c.offline_note, "offline while drawing keeps picture")
    c.end_draft()
    # view mode hit test
    c.set_image(qimg(1))
    c.set_areas([area("Spot", "parking_spot", "polygon", [[0.1, 0.1], [0.3, 0.1], [0.3, 0.3], [0.1, 0.3]])])
    got = []
    c.area_clicked.connect(lambda aid, _p: got.append(aid))
    click(c, px(c, 0.2, 0.2))
    check(len(got) == 1, "click on an area opens it")
    # preview from live tracks
    c.begin_draft("polygon", 0, draft=Draft("polygon", [[0.1, 0.1], [0.5, 0.1], [0.5, 0.6], [0.1, 0.6]], True))
    texts = []
    c.preview_changed.connect(texts.append)
    c.set_tracks([(1, "car", 0.2, 0.3, 0.3, 0.5)])
    check(texts and texts[-1] == "Right now: 1 car inside", f"live preview {texts}")
    c.end_draft()
    c.close()


def test_panel_flow(ctx, win):
    from yardguard import areas_ui as au
    answers = []
    au.confirm = lambda *a, **k: answers.pop(0) if answers else True
    ctl = au.LiveAreas(ctx, win, raw_frame=lambda: frame(1))
    host = QWidget()
    row = QHBoxLayout(host)
    row.addWidget(ctl.canvas, 1)
    row.addWidget(ctl.panel)
    host.resize(1100, 600)
    host.show()
    ctl.canvas.set_image(qimg(1))
    site = ctx.cfg.sites[0]
    cam = site.camera(3)
    ctl.set_camera(site, cam)
    pump()
    c = ctl.canvas
    ctl.start_add()
    check(ctl.panel.isVisible() and ctl.panel.step == 0 and ctl.bar.isVisible(), "Add area opens the panel and bar")
    check(not ctl.panel.next_btn.isEnabled(), "Next is disabled until a shape exists")
    drag(c, px(c, 0.6, 0.6), px(c, 0.75, 0.8))
    check(ctl.panel.step == 1, "closing a shape moves to step 2")
    check(ctl.panel.cards["parking_spot"].property("selected"), "small rectangle preselects Parking spot")
    check(not ctl.panel.cards["log_plates"].fits, "plate logging is dimmed for an area")
    ctl.panel.pick("log_plates")
    check(ctl.panel.needs["log_plates"].isVisibleTo(ctl.panel), "incompatible card asks to draw a line")
    ctl.panel.pick("no_parking")
    check(ctl.panel.step == 2 and ctl.panel.f_alert.isChecked(), "No parking form, alert on by default")
    check(ctl.panel.f_objects["car"].isChecked() and ctl.panel.f_objects["truck"].isChecked(), "default objects")
    ctl.panel.f_objects["car"].setChecked(False)
    ctl.panel.f_objects["truck"].setChecked(False)
    check(ctl.panel.f_objects["truck"].isChecked(), "at least one object stays on")
    ctl.panel.f_spin.setValue(7)
    ctl.panel.f_name.setText("Gate blocker")
    ctl.panel.go(1)      # back and forth keeps what was typed
    ctl.panel.pick("no_parking")
    check(ctl.panel.f_name.text() == "Gate blocker" and ctl.panel.f_spin.value() == 7, "edits survive Back")
    saves = ctx.saves
    ctl.panel.save()
    a = cam.areas[-1] if getattr(cam, "areas", None) else {}
    check(ctx.saves == saves + 1 and a.get("task") == "no_parking" and a["params"]["max_minutes"] == 7,
          f"saved area {a.get('name')} {a.get('params')}")
    check(a.get("name") == "Gate blocker" and a.get("alert") is True and a.get("objects") == ["truck"],
          "saved fields")
    check(not ctl.panel.isVisible() and not c.drafting, "panel closes after Save")
    check(any("saved" in t for t, _e in win.notes), "save toast")
    # edit the area: title and button change; changing the task asks
    ctl.edit(a["id"])
    check(ctl.panel.title.text() == "Edit area" and ctl.panel.next_btn.text() == "Save changes", "edit mode labels")
    answers[:] = [False]
    ctl.panel.go(1)
    ctl.panel.pick("loitering")
    check(ctl.panel.task == "no_parking", "declining the task change keeps the task")
    # Esc ladder: panel open -> confirm discard (answer keep), then discard
    answers[:] = [False]
    drag(c, px(c, *a["points"][0]), px(c, a["points"][0][0] * 1.0 - 0.05, a["points"][0][1]))
    check(ctl.escape() and ctl.panel.isVisible(), "Esc asks before discarding; Keep editing stays")
    answers[:] = [True]
    check(ctl.escape() and not ctl.panel.isVisible(), "Discard closes the panel")
    check(not ctl.escape(), "Esc with nothing open is left to the page (full screen)")
    # line flow with direction labels in the form
    ctl.start_add()
    key(c, Qt.Key_L)
    drag(c, px(c, 0.2, 0.4), px(c, 0.8, 0.45))
    check(ctl.panel.step == 1 and ctl.panel.cards["count"].property("selected"), "line preselects Count")
    ctl.panel.pick("count")
    texts = [ctl.panel.f_dir.group.button(i).text() for i in range(3)]
    check("Towards the camera" in texts[1] and "Away from the camera" in texts[2], f"direction words {texts}")
    ctl.panel.f_dir.set_current(1)
    ctl.panel.f_dir.changed.emit(1)
    check(c.draft.direction == "a_to_b", "direction choice drives the arrow")
    ctl.panel.save()
    check(cam.areas[-1]["task"] == "count" and cam.areas[-1]["direction"] == "a_to_b", "count line saved")
    # ask the AI: test now
    ctl.start_add()
    drag(c, px(c, 0.1, 0.1), px(c, 0.3, 0.3))
    ctl.panel.pick("ask_ai")
    ctl.panel.f_question.setText("the gate is open")
    ctl.panel._test_now()
    for _ in range(100):
        pump(1)
        if "found" in ctl.panel.f_test_res.text():
            break
    check("Yes, found" in ctl.panel.f_test_res.text(), f"Test now answer: {ctl.panel.f_test_res.text()}")
    answers[:] = [True]
    ctl.panel.cancel()
    # popover and actions
    ctl.set_camera(site, cam)
    gate = cam.areas[-1]
    pos = c.to_px(gate["points"][0]) / 2 + c.to_px(gate["points"][1]) / 2
    click(c, (pos.x(), pos.y()))
    check(ctl.popover.isVisible() and ctl.popover.area_id == gate["id"], "clicking an area shows its popover")
    check(ctl.escape() and not ctl.popover.isVisible(), "Esc closes the popover first")
    ctl.duplicate(gate["id"])
    check(cam.areas[-1]["name"] == "Gate line copy", "duplicate")
    answers[:] = [True]
    n = len(cam.areas)
    ctl.delete(cam.areas[-1]["id"])
    check(len(cam.areas) == n - 1, "delete after confirm")
    host.close()
    return ctl


def test_hub(ctx, win, ids):
    from yardguard import areas_ui as au
    page = au.AreasPage(ctx, win)
    page.resize(1048, 760)
    page.show()
    pump(10)
    check(page.body.currentIndex() == 0 and len(page.lookup) == 6, f"hub lists all areas ({len(page.lookup)})")
    page.set_filter("off")
    items = [page.list.item(i).data(au.ID_ROLE) for i in range(page.list.count())]
    check(items == [None, ids["off"]["id"]], f"Off filter {items}")
    page.set_filter("cam:5")
    check(page.list.count() == 4, "camera filter")
    page.set_filter("all")
    page.search.setText("barrier")
    pump()
    names = [page.lookup[page.list.item(i).data(au.ID_ROLE)][2]["name"] for i in range(page.list.count())
             if page.list.item(i).data(au.ID_ROLE)]
    check(names == ["Barrier", "Barrier arm"], f"search {names}")
    page.search.setText("")
    page.show_area(ids["barrier"]["id"])
    pump(10)
    d = page.detail
    check(d.table.rowCount() > 10 and d.columns()[1] == "Plate", "plate log rows")
    d._cell_clicked(0, 1)
    plate = d._events[0].value
    check(all(e.value == plate for e in d._events) and d.plate_chip.isVisible(), "All sightings of a plate")
    d._clear_plate()
    rows = d.table.rowCount()
    e = AreaEvent("new1", "site1", 1, ids["barrier"]["id"], "Barrier", "log_plates", time.time(), "plate",
                  label="car", value="NE-555-WW", extra={"direction": "in"})
    ctx.alert_store.add_area_event(e)
    page.add_event(e)
    check(d.table.rowCount() == rows + 1 and d.table.item(0, 1).text() == "NE-555-WW", "live event row at the top")
    d._set_known("NE-555-WW", True)
    check("NE-555-WW" in ids["barrier"]["params"]["known"], "add to known plates")
    d.page.export_csv = lambda f, n: ctx.alert_store.exported.append((n, f))
    d._export()
    check(ctx.alert_store.exported and ctx.alert_store.exported[-1][1]["area_id"] == ids["barrier"]["id"],
          "CSV export gets the filters")
    page.show_area(ids["gate"]["id"])
    pump(5)
    check(page.detail.chart.isVisible() and page.detail.chart.mirrored, "count shows the mirrored chart")
    page.detail.dates.set_current(1)
    page.detail._dates_changed(1)
    check(len(page.detail.chart.buckets) == 7, "7 days per day")
    page.detail.dates.set_current(0)
    page.detail._dates_changed(0)
    page.show_area(ids["spot"]["id"])
    pump(5)
    check(page.detail.strip.isVisible() and page.detail.strip.rows, "parking shows the timeline strip")
    page.detail._kind = "freed"
    page.detail.load_events()
    check(all(e.kind == "freed" for e in page.detail._events), "kind filter")
    page.detail._kind = ""
    page.detail.query.setText("zzzz")
    page.detail.load_events()
    check(page.detail.log_empty.isVisibleTo(page.detail) and "match" in page.detail.log_empty_text.text(),
          "no-match message")
    page.detail._empty_action()
    check(not page.detail.query.text() and page.detail._events, "Clear filters")
    page.close()


def test_real_store(ctx, win, ids):
    """The hub against the engine's real AlertStore (temporary database under OUT)."""
    try:
        from yardguard.areas import new_event
        from yardguard.engine import AlertStore
    except ImportError as exc:
        print(f"skip real store test ({exc})")
        return
    from yardguard import areas_ui as au
    folder = OUT / "store"
    shutil.rmtree(folder, ignore_errors=True)
    store = AlertStore(str(folder))
    barrier, gate = ids["barrier"], ids["gate"]
    now = time.time()
    for i, (plate, d) in enumerate((("AA-123-BB", "in"), ("ZZ-404-QQ", "in"), ("AA-123-BB", "out"))):
        extra = {"direction": d, "known": plate == "AA-123-BB"}
        if d == "out":
            extra["stay_s"] = 3600.0
        store.add_area_event(new_event(barrier, 1, now - 600 + i * 60, "plate", site_id="site1", label="car",
                                       value=plate, confidence=0.9, extra=extra))
    for i in range(4):
        store.add_area_event(new_event(gate, 1, now - 300 + i, "cross_in" if i % 2 == 0 else "cross_out",
                                       site_id="site1", label="car", value=str(i), extra={"direction": "in"}))
    old_store, ctx.alert_store = ctx.alert_store, store
    page = au.AreasPage(ctx, win)
    page.resize(1048, 760)
    page.show()
    pump(5)
    page.show_area(barrier["id"])
    d = page.detail
    check(d.table.rowCount() == 3, f"real store: plate rows {d.table.rowCount()}")
    check(d.table.item(0, 3).text() == "1 h" and d.table.item(0, 4).text() == "Resident",
          f"real store: stay and resident ({d.table.item(0, 3).text()}, {d.table.item(0, 4).text()})")
    check(d.tiles[2].value.text() == "1", f"real store: visitors {d.tiles[2].value.text()}")
    d._cell_clicked(1, 1)
    check(d.table.rowCount() == 1 and d._events[0].value == "ZZ-404-QQ", "real store: all sightings of a plate")
    d._clear_plate()
    path = OUT / "export.csv"
    n = store.export_area_events_csv(str(path), **d.filters())
    check(n == 3 and path.exists(), "real store: CSV export with the page's filters")
    page.show_area(gate["id"])
    check(page.detail.tiles[0].value.text() == "2" and page.detail.tiles[1].value.text() == "2",
          "real store: count tiles")
    page.detail._kind = "cross_out"
    page.detail.load_events()
    check(len(page.detail._events) == 2, "real store: kind filter")
    page.close()
    store.close()
    ctx.alert_store = old_store


# --- screenshots ------------------------------------------------------------------------------------

class Shell(QWidget):
    """Main window stand-in: 232 px sidebar and a Live page laid out like gui.LivePage."""

    def __init__(self, ctx, win, page: QWidget | None = None):
        super().__init__()
        self.setObjectName("Root")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(232)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 20, 16, 16)
        from yardguard.widgets import label
        sl.addWidget(label("YardGuard", "AppTitle"))
        sl.addSpacing(30)
        from PySide6.QtWidgets import QPushButton
        for i, (text, icon) in enumerate((("Dashboard", "grid"), ("Incidents", "shield"), ("Areas", "pentagon"),
                                          ("Live view", "video"), ("Sites & cameras", "server"),
                                          ("Settings", "sliders"))):
            b = QPushButton("  " + text)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setChecked(text == ("Areas" if page is not None else "Live view"))
            ctx.bind_icon(b, icon, "nav")
            sl.addWidget(b)
        sl.addStretch(1)
        row.addWidget(side)
        self.page = page or self._live(ctx, win)
        row.addWidget(self.page, 1)

    def _live(self, ctx, win) -> QWidget:
        from yardguard import areas_ui as au
        from yardguard.widgets import PageHeader, button, muted
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(16)
        site = ctx.cfg.sites[0]
        self.header = PageHeader("Live view", f"{site.name} · Yard / street side")
        lay.addWidget(self.header)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        combo = QComboBox()
        combo.addItem("Yard / street side  (channel 1)")
        combo.setMinimumWidth(280)
        ov = QComboBox()
        ov.addItem("AI: Boxes")
        self.ctl = au.LiveAreas(ctx, win, raw_frame=lambda: frame(1))
        bar.addWidget(combo)
        bar.addWidget(ov)
        bar.addSpacing(8)
        bar.addWidget(self.ctl.show_cb)
        bar.addStretch(1)
        bar.addWidget(self.ctl.add_btn)
        snap = button("Snapshot")
        ctx.bind_icon(snap, "camera")
        full = button("Full screen")
        ctx.bind_icon(full, "maximize")
        bar.addWidget(snap)
        bar.addWidget(full)
        lay.addLayout(bar)
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self.ctl.canvas, 1)
        body.addWidget(self.ctl.panel)
        lay.addLayout(body, 1)
        info = QHBoxLayout()
        info.addWidget(muted("1920×1080 · 25 fps · 2 cars · AI on GPU (fast mode)", wrap=False), 1)
        st = QLabel("● Live")
        st.setProperty("status", "online")
        info.addWidget(st)
        lay.addLayout(info)
        return page


def shoot(w: QWidget, name: str) -> None:
    pump(8)
    w.grab().save(str(OUT / f"{name}.png"))


def screenshots(app, ctx, win, ids):
    from yardguard import areas_ui as au
    site = ctx.cfg.sites[0]
    cam1, cam5 = site.camera(1), site.camera(5)
    tracks = [(1, "car", 0.36, 0.48, 0.58, 0.80), (2, "person", 0.70, 0.40, 0.74, 0.62)]
    for theme in ("light", "dark"):
        ctx.apply_theme(theme)
        for size in ((1280, 800), (1600, 1000)):
            tag = f"{theme}_{size[0]}"
            # 1. polygon mid-way
            sh = Shell(ctx, win)
            sh.resize(*size)
            sh.show()
            ctl = sh.ctl
            ctl.canvas.set_image(qimg(1))
            ctl.set_camera(site, cam1)
            ctl.set_tracks(tracks)
            pump(5)
            c = ctl.canvas
            ctl.start_add()
            for x, y in ((0.05, 0.62), (0.30, 0.56), (0.42, 0.88)):
                click(c, px(c, x, y))
            mouse(c, "move", px(c, 0.12, 0.93), buttons=Qt.NoButton)
            shoot(sh, f"01_polygon_midway_{tag}")
            mouse(c, "move", px(c, 0.06, 0.625), buttons=Qt.NoButton)
            shoot(sh, f"01b_polygon_close_{tag}")
            # 2. step 2 template picker for the closed polygon
            click(c, px(c, 0.12, 0.93))
            dbl(c, px(c, 0.12, 0.93))
            shoot(sh, f"03_template_picker_{tag}")
            # 3. line with arrow + count form
            au.confirm = lambda *a, **k: True
            ctl.panel.cancel()
            ctl.start_add(shape="line")
            drag(c, px(c, 0.18, 0.66), px(c, 0.74, 0.74))
            ctl.panel.pick("count")
            c.set_direction("a_to_b")
            mouse(c, "move", px(c, 0.46, 0.70), buttons=Qt.NoButton)
            shoot(sh, f"02_line_arrow_{tag}")
            # 4. plate form
            ctl.panel.go(1)
            ctl.panel.pick("log_plates")
            ctl.panel._known = ["AA-123-BB", "GE-777-TT"]
            ctl.panel._fill_plates()
            shoot(sh, f"04_plate_form_{tag}")
            # ask the AI form
            ctl.panel.cancel()
            ctl.start_add()
            drag(c, px(c, 0.55, 0.20), px(c, 0.80, 0.42))
            ctl.panel.pick("ask_ai")
            ctl.panel.f_question.setText("the barrier arm is raised")
            shoot(sh, f"05_ask_ai_form_{tag}")
            ctl.panel.cancel()
            # vertex drag with the magnifier
            ctl.edit(ids["spot"]["id"])
            p0 = c.to_px(ids["spot"]["points"][2])
            mouse(c, "move", (p0.x(), p0.y()), buttons=Qt.NoButton)
            mouse(c, "press", (p0.x(), p0.y()))
            mouse(c, "move", (p0.x() + 14, p0.y() + 10), buttons=Qt.LeftButton)
            shoot(sh, f"06_edit_loupe_{tag}")
            mouse(c, "release", (p0.x() + 14, p0.y() + 10), buttons=Qt.NoButton)
            ctl.panel.close_panel()
            # 5. popover over live video
            ctl.set_camera(site, cam1)
            ctl.poll()
            sp = ids["spot"]["points"]
            centre = (sum(p[0] for p in sp) / 4, sum(p[1] for p in sp) / 4)
            click(c, px(c, *centre))
            shoot(sh, f"07_popover_{tag}")
            ctl.popover.hide()
            c.selected_id = None
            shoot(sh, f"08_live_areas_{tag}")
            sh.close()
            sh.deleteLater()
            # hub
            page = au.AreasPage(ctx, win)
            for ch, f in ((1, 1), (5, 5), (3, 2)):
                page.thumbs.put("site1", ch, qimg(f).scaledToWidth(320))
            hub = Shell(ctx, win, page)
            hub.resize(*size)
            hub.show()
            pump(5)
            page.show_area(ids["barrier"]["id"])
            pump(30)
            shoot(hub, f"10_hub_plates_{tag}")
            page.show_area(ids["gate"]["id"])
            shoot(hub, f"11_hub_count_{tag}")
            page.show_area(ids["spot"]["id"])
            shoot(hub, f"12_hub_parking_{tag}")
            page.detail.dates.set_current(1)
            page.detail._dates_changed(1)
            shoot(hub, f"12b_hub_parking_week_{tag}")
            page.detail.dates.set_current(0)
            page.detail._dates_changed(0)
            page.show_area(ids["arm"]["id"])
            shoot(hub, f"13_hub_ask_ai_{tag}")
            page.show_area(ids["barrier"]["id"])
            pump(5)
            page.detail.table.selectRow(1)
            pump(10)
            page.detail.verticalScrollBar().setValue(page.detail.verticalScrollBar().maximum())
            shoot(hub, f"14_hub_preview_{tag}")
            hub.close()
            hub.deleteLater()
            # empty hub
            saved = {c.channel: list(c.areas) for c in site.cameras}
            for c_ in site.cameras:
                c_.areas = []
            page2 = au.AreasPage(ctx, win)
            empty = Shell(ctx, win, page2)
            empty.resize(*size)
            empty.show()
            shoot(empty, f"15_hub_empty_{tag}")
            empty.close()
            empty.deleteLater()
            for c_ in site.cameras:
                c_.areas = saved[c_.channel]
            pump(3)


def mojibake_check():
    bad = []
    for p in list((ROOT / "yardguard").glob("*.py")) + list((ROOT / "tests").glob("test_areas_ui.py")):
        text = p.read_text(encoding="utf-8")
        for m in ("\u00c2", "\u00e2\u20ac", "\u00c3"):
            if m in text:
                bad.append(f"{p.name}: {m!r}")
    check(not bad, f"no mojibake in project files {bad}")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    from yardguard import areas_ui
    areas_ui.CACHE = OUT / "thumbs"   # camera thumbnails of the fake site stay in the test folder
    cfg = make_config()
    ctx = Ctx(app, cfg)
    ctx.apply_theme("light")
    win = Win()
    test_helpers()
    test_canvas(ctx)
    test_panel_flow(ctx, win)
    for c in cfg.sites[0].cameras:
        c.areas = []
    ctx.alert_store = FakeStore()
    ids = populate(ctx)
    test_hub(ctx, win, ids)
    test_real_store(ctx, win, ids)
    if "--no-shots" not in sys.argv:
        screenshots(app, ctx, win, ids)
    mojibake_check()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
