"""Areas: what happens inside the polygons and on the lines the user drew on a camera.

AreaEvaluator (one per camera) is fed after every tracker update with the camera's tracks
(smoothed boxes, stable ids, labels) and the RAW frame, and turns them into AreaEvents according
to each area's task (config.TEMPLATES). Pure logic driven by timestamps, so tests can use a fake
clock and synthetic tracks; the Monitor (engine.py) saves the events, their pictures and clips.

How the tasks see the picture:
- Polygons: a person is where their feet are (bottom centre of the box), a vehicle is in when most
  of its footprint (the lower part of its box) is, with spatial hysteresis at the edge. An object
  enters after ENTER_CHECKS checks inside and leaves after EXIT_CHECKS checks outside lasting EXIT_S; a track that
  vanishes is held for a grace time, and a new track id appearing where it was takes its place, so
  an id switch is not a leave + enter (night noise does that to parked cars).
- Lines: a crossing is the foot point changing side of the line. Positions within a band around the
  line are "on it" and change nothing, the new side must hold for LINE_CONFIRM checks, the crossing
  point must lie on the drawn segment and the object must really move: jitter at the line never
  counts twice. a_to_b = from the left of A->B to its right (screen coordinates, y down).
- Number plates (log_plates lines): a vehicle's pictures are collected while it is within PLATE_APPROACH
  box heights of the line on either side, at every detection and, through between() when PLATE_BETWEEN
  is on, on the frames between detections (the box moved on by the track's speed and a small template
  search). A cheap
  whole-vehicle score keeps the best PLATE_CANDIDATES moments; the crossing is read once the vehicle
  has left the band (or is gone), so the moments after the line count too.
- left_object / state_watch compare a small grey picture of the area with a reference that slowly
  follows the light. Pictures are normalised (mean / contrast) so a global brightness change does not
  count, a sudden jump of the whole frame (IR switch) starts a new reference, and tracked people and
  vehicles are masked out.
- ask_ai asks the brain about the area crop at most every interval_s, and only when the crop changed
  since the last question; the Monitor runs those asks only while no alert waits for the brain.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np

from .config import area_valid

log = logging.getLogger("yardguard")

VEHICLES = {"car", "motorcycle", "bus", "truck"}
LOOKALIKE = {"car": {"car", "truck"}, "truck": {"car", "truck"}}  # YOLO flips between them at night

# Polygons
ENTER_CHECKS = 2
EXIT_CHECKS = 3
EXIT_S = 1.5
# A track not seen in a frame is "lost" at once; it counts as gone after this long (longer than the
# tracker keeps a person, 3 s)...
LOST_GRACE_S = {"person": 3.5, "vehicle": 30.0}
INHERIT_IOU = 0.3            # ...unless a new track turns up where it was (box overlap)...
INHERIT_REACH = 0.6          # ...or its feet this many box heights away...
INHERIT_SPEED = 0.8          # ...plus this many per second unseen (walking pace)
EDGE_MARGIN = 0.006          # of the frame diagonal: "in" = this far inside the polygon, "out" = this far out...
EDGE_MARGIN_BOX = 0.05       # ...or this share of the object's height, whichever is bigger
VEHICLE_IN = 0.5             # share of a vehicle's footprint inside the polygon to come in...
VEHICLE_OUT = 0.25           # ...and below which it has left
STILL_IOU = 0.6              # no_parking: the vehicle box this close to where it stopped = still there
# Lines
LINE_BAND = 0.012            # of the frame diagonal: this close to the line = on it
LINE_BAND_BOX = 0.12         # ...or this share of the object's height, whichever is bigger
LINE_CONFIRM = 2             # checks on the new side before a crossing counts...
LINE_CONFIRM_S = 0.6         # ...over at least this long (someone dithering on the line flips sooner)
LINE_EXTENT = 0.08           # the crossing may be this much (of the line length) beyond its ends
LINE_MIN_MOVE = 0.015        # of the frame diagonal between the last point before and the first after
LINE_REVERSE_S = 1.0         # the same object crossing again sooner than this is jitter
LINE_INHERIT_S = {"person": 3.5, "vehicle": 5.0}
# Plates: a vehicle is followed from PLATE_APPROACH box heights before the line until as far past it
# (or gone); its best moments are searched for the plate when it leaves (plates.PlateReader.read).
PLATE_APPROACH = 5.0         # vehicle pictures are collected this many box heights either side of the line
PLATE_NEAR = 1.5             # ...and of a vehicle standing still only this close (a car waiting at the barrier)
PLATE_CANDIDATES = 12        # pictures kept per vehicle, by a cheap whole-vehicle score (size x sharpness;
                             # 20 read no more plates than 12 on the replayed clips)
PLATE_GAP_S = 0.35           # pictures closer in time than this compete for one place
PLATE_EDGE = 0.6             # prefilter weight of a vehicle cut by the edge of the picture...
PLATE_FAR = 0.4              # ...and how much less a moment at the edge of the band counts than one at the line
PLATE_STILL = 0.15           # box heights per second: slower is standing still
PLATE_WAIT_S = 20.0          # a car standing near the line counts this long (waiting, not parked)
PLATE_BETWEEN = False        # also use the frames between detections (between()). Off: on the replayed day
                             # clips it read 6 vs 8 plates at 6 detections/s and 6 vs 5 at 4/s (no gain)
PLATE_PREDICT_S = 0.6        # frames between detections are used this long after the last detection
PLATE_MATCH = 0.5            # template match score that moves the predicted box onto the vehicle
PLATE_TMPL = 64              # px: width of that template
PLATE_FULL_PX = 640          # vehicle width that scores full size in the prefilter
PLATE_HOLD_S = 8.0           # a crossing is read when the vehicle leaves the band, at the latest this long
PLATE_LOST_S = 1.0           # after it, or once the vehicle has not been seen for this long
PLATE_MAX_AGE_S = 20.0       # older pictures are dropped (a car that turned back before the line)
PLATE_SURE = 0.6             # plate readings below this get a "?" (the hub's "Unsure read": a wrong
                             # 6-character read scored 0.4 on the barrier replay)
# Pictures of an area (left_object, state_watch, ask_ai)
PIXEL_SIDE = 96              # long side of the small grey picture
PIXEL_EVERY_S = 1.0
PIXEL_DIFF = 0.9             # normalised difference that makes a pixel "changed"
PIXEL_AVG = 0.35             # running average of the current picture (night noise)
PIXEL_FOLLOW = 0.03          # the reference follows slow light changes this fast while nothing changed
MASK_GROW = 0.2              # tracked objects are masked out with this much (of their size) around them
MIN_VISIBLE = 0.4          # less of the area visible (the rest hidden by people / cars) = no evidence
LIGHT_JUMP = 0.25            # the whole frame this much brighter / darker at once = new reference
# ask_ai
AI_CHANGE = 0.04             # share of changed pixels since the last question that is worth asking again
AI_REFRESH_S = 1800.0        # ...or ask anyway after this long, so one wrong answer does not stick
AI_CROP_MAX = 896

KINDS = ("enter", "exit", "cross_in", "cross_out", "count", "occupied", "freed", "blocked", "loitering",
         "presence", "plate", "appeared", "removed", "changed", "answer_yes", "answer_no")
# kinds that raise an alert when the area has "Alert me" on
ALERT_KINDS = {"enter", "cross_in", "cross_out", "occupied", "blocked", "loitering", "presence", "plate",
               "appeared", "removed", "changed", "answer_yes"}
KIND_TEXT = {"enter": "Came in", "exit": "Went out", "cross_in": "In", "cross_out": "Out", "count": "Count",
             "occupied": "Occupied", "freed": "Free again", "blocked": "Blocking", "loitering": "Lingering",
             "presence": "Seen", "plate": "Number plate", "appeared": "Left behind", "removed": "Taken away",
             "changed": "Changed", "answer_yes": "Yes", "answer_no": "No"}


@dataclass
class AreaEvent:
    id: str
    site_id: str
    channel: int
    area_id: str
    area_name: str
    task: str
    ts: float
    kind: str                     # see KINDS
    label: str = ""               # what it is about: "car", "person", ...
    track_id: int | None = None
    value: str = ""               # plate text / count / duration, as shown
    confidence: float = 0.0
    snapshot_path: str = ""
    crop_path: str = ""
    clip_path: str = ""
    clip_offset_s: float = -1.0   # where the event happens in the clip; -1 = no clip
    extra: dict = field(default_factory=dict)  # direction, duration_s, plate details, alert_id...


@dataclass
class PlateCandidate:
    """One moment of a vehicle near a log_plates line."""
    ts: float
    score: float                  # cheap whole-vehicle score (plates.plate_quality)
    crop: np.ndarray              # the vehicle with a little margin, frame pixels
    box: tuple                    # the vehicle in the frame
    frame: np.ndarray             # the whole frame (shared with the camera source, never written)
    detected: bool = True         # False: placed by the track's motion between two detections


@dataclass
class AreaHit:
    """An event as the evaluator found it, with what the Monitor needs to finish it."""
    event: AreaEvent
    area: dict
    box: tuple | None = None                        # the subject in frame pixels
    plate_crops: list = field(default_factory=list)  # log_plates: vehicle pictures, best first...
    plate_cands: list = field(default_factory=list)  # ...and their moments (PlateCandidate, same order)
    frame: np.ndarray | None = None                 # ask_ai: the frame the question was about
    night: bool = False


def new_event(area: dict, channel: int, ts: float, kind: str, **kw) -> AreaEvent:
    return AreaEvent(id=f"ae-{uuid.uuid4().hex[:12]}", site_id=kw.pop("site_id", ""), channel=channel,
                     area_id=area["id"], area_name=area["name"], task=area["task"], ts=ts, kind=kind, **kw)


# --- Geometry and time -----------------------------------------------------------------

def foot(box) -> tuple[float, float]:
    return (float(box[0]) + float(box[2])) / 2, float(box[3])


def _clip_box(box, w: int, h: int) -> tuple[int, int, int, int]:
    """A box rounded to whole pixels inside a w x h frame."""
    x1, y1, x2, y2 = (int(round(float(v))) for v in box[:4])
    return max(0, x1), max(0, y1), min(w, x2), min(h, y2)


def iou(a, b) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return float(inter / union) if union > 0 else 0.0


def side_of(a, b, p) -> float:
    """Signed distance of p from the line A->B: negative on its left, positive on its right
    (screen coordinates, y down). a_to_b crosses from negative to positive."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    return (dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / length


def arrow_normal(a, b) -> tuple[float, float]:
    """Unit vector across the line A->B pointing to its right: the a_to_b direction arrow. Use
    pixel coordinates (normalised ones distort the angle when the frame is not square)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    return -dy / length, dx / length


def _minutes(text: str, default: int) -> int:
    try:
        h, m = str(text).strip().split(":")
        return (int(h) % 24) * 60 + int(m) % 60
    except ValueError:
        return default


def in_window(ts: float, start: str, end: str) -> bool:
    """Local time of ts inside [start, end), also across midnight; start == end = all day."""
    t = datetime.fromtimestamp(ts)
    m = t.hour * 60 + t.minute
    s, e = _minutes(start, 0), _minutes(end, 360)
    if s == e:
        return True
    return s <= m < e if s < e else (m >= s or m < e)


def scheduled(area: dict, ts: float, night: tuple[str, str] = ("00:00", "06:00")) -> bool:
    sched = area.get("schedule") or {}
    mode = sched.get("mode", "always")
    if mode == "night":
        return in_window(ts, *night)
    if mode == "custom":
        return in_window(ts, sched.get("start", "22:00"), sched.get("end", "06:00"))
    return True


def wants(objects, label: str) -> bool:
    """The area watches this kind of object. No objects = everything."""
    if not objects:
        return True
    return label in objects or bool(LOOKALIKE.get(label, set()) & set(objects)) or \
        ("vehicle" in objects and label in VEHICLES)


def group_of(label: str) -> str:
    return "vehicle" if label in VEHICLES else "person"


def reads_plates(areas) -> bool:
    """The camera has a switched-on number plate line: it is watched at full resolution and every
    frame between detections is searched for plates too."""
    return any(a.get("task") == "log_plates" and a.get("enabled", True) and area_valid(a) for a in areas or [])


def duration_text(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def describe(ev: AreaEvent) -> str:
    """The event in plain words, e.g. "Car blocking Gate for 6 min" (alert titles, the log)."""
    what = (ev.label or "Something").capitalize()
    area, x = ev.area_name, ev.extra or {}
    k = ev.kind
    if k == "blocked":
        return f"{what} blocking {area} for {ev.value}"
    if k == "loitering":
        return f"{what} lingering at {area} for {ev.value}"
    if k == "presence":
        return f"{what} in {area}"
    if k in ("cross_in", "cross_out"):
        if ev.task == "count":
            return f"{what} {'in' if k == 'cross_in' else 'out'} at {area}"
        return f"{what} crossed {area}" + ("" if x.get("direction_filter", "both") == "both" else
                                          " in the watched direction")
    if k == "enter":
        return f"{what} came into {area}"
    if k == "exit":
        return f"{what} left {area}"
    if k == "occupied":
        return f"{area} taken by a {ev.label or 'vehicle'}"
    if k == "freed":
        return f"{area} free again" + (f" after {ev.value}" if ev.value else "")
    if k == "plate":
        way = {"in": " coming in", "out": " going out"}.get(x.get("direction", ""), "")
        if not ev.value:
            return f"{what}{way} at {area}, plate unreadable"
        unsure = " ?" if 0 < (ev.confidence or 0) < PLATE_SURE else ""  # a guess: shown as one
        return f"{what} {ev.value}{unsure}{way} at {area}"
    if k == "appeared":
        return f"Something left in {area}"
    if k == "removed":
        return f"Something taken from {area}"
    if k == "changed":
        return f"{area} back to normal" if ev.value == "restored" else f"{area} changed and stayed so"
    if k in ("answer_yes", "answer_no"):
        q = x.get("question", "")
        return f"{area}: {'yes' if k == 'answer_yes' else 'no'}, {q}" if q else f"{area}: {KIND_TEXT[k].lower()}"
    return f"{KIND_TEXT.get(k, k)} at {area}"


def _poly_px(area: dict, w: int, h: int) -> np.ndarray:
    return np.array([(float(x) * w, float(y) * h) for x, y in area["points"]], np.float32)


def area_rect(area: dict, w: int, h: int, pad: float = 0.0) -> tuple[int, int, int, int]:
    """Bounding box of the area in pixels, grown by pad (share of its size), inside the frame."""
    pts = _poly_px(area, w, h)
    x1, y1 = pts.min(axis=0)
    x2, y2 = pts.max(axis=0)
    px, py = pad * (x2 - x1) + 2, pad * (y2 - y1) + 2
    return (int(max(0, x1 - px)), int(max(0, y1 - py)), int(min(w, math.ceil(x2 + px))),
            int(min(h, math.ceil(y2 + py))))


def draw_area(img: np.ndarray, area: dict, color_bgr: tuple | None = None, fill: float = 0.18,
              label: bool = True) -> np.ndarray:
    """Draw the area in place: a translucent polygon, or a line with its direction arrow(s), and
    its name. For event pictures and thumbnails."""
    from .config import zone_color_bgr
    h, w = img.shape[:2]
    if not area_valid(area):
        return img
    color = tuple(int(c) for c in (color_bgr or zone_color_bgr(int(area.get("color") or 0))))
    pts = _poly_px(area, w, h).round().astype(np.int32)
    thick = max(2, round(max(w, h) / 640))
    if area.get("shape") == "line":
        a, b = pts[0], pts[1]
        cv2.line(img, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), color, thick, cv2.LINE_AA)
        nx, ny = arrow_normal(a, b)
        mx, my = (a + b) / 2
        size = max(18.0, 0.035 * math.hypot(w, h))
        direction = area.get("direction", "both")
        for sign, way in ((1, "a_to_b"), (-1, "b_to_a")):
            if direction in ("both", way):
                tip = (int(mx + sign * nx * size), int(my + sign * ny * size))
                cv2.arrowedLine(img, (int(mx), int(my)), tip, color, thick, cv2.LINE_AA, tipLength=0.4)
        anchor = (int(min(a[0], b[0])), int(min(a[1], b[1])))
    else:
        x1, y1 = np.maximum(pts.min(axis=0), 0)
        x2, y2 = np.minimum(pts.max(axis=0) + 1, (w, h))
        if fill > 0 and x2 > x1 and y2 > y1:  # blend only the area's box: cheap on big frames
            roi = img[y1:y2, x1:x2]
            tinted = roi.copy()
            cv2.fillPoly(tinted, [pts - (x1, y1)], color, cv2.LINE_AA)
            cv2.addWeighted(tinted, fill, roi, 1 - fill, 0, dst=roi)
        cv2.polylines(img, [pts], True, color, thick, cv2.LINE_AA)
        anchor = (int(pts[:, 0].min()), int(pts[:, 1].min()))
    text = str(area.get("name", ""))[:40].encode("ascii", "replace").decode()
    if label and text:
        fs = 0.5 * max(1.0, max(w, h) / 1280)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        x, y = min(max(0, anchor[0]), max(0, w - tw - 8)), max(th + 8, anchor[1])
        cv2.rectangle(img, (x, y - th - 8), (x + tw + 8, y), color, -1)
        ink = (30, 30, 30) if sum(color) > 450 else (255, 255, 255)
        cv2.putText(img, text, (x + 4, y - 5), cv2.FONT_HERSHEY_SIMPLEX, fs, ink, 1, cv2.LINE_AA)
    return img


_GRID = [(gx, gy) for gy in (0.6, 0.73, 0.86, 0.97) for gx in (0.15, 0.32, 0.5, 0.68, 0.85)]


def footprint_share(poly: np.ndarray, box) -> float:
    """Share of a vehicle's footprint inside the polygon: a grid of points over the lower part of
    its box (the wheels and the ground under the car, without mirrors and roof)."""
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    bw, bh = x2 - x1, y2 - y1
    inside = sum(cv2.pointPolygonTest(poly, (x1 + gx * bw, y1 + gy * bh), False) >= 0 for gx, gy in _GRID)
    return inside / len(_GRID)


def _signature(area: dict) -> str:
    keys = ("shape", "points", "task", "objects", "direction", "params", "schedule")
    return json.dumps({k: area.get(k) for k in keys}, sort_keys=True)


def sharpness(img: np.ndarray) -> float:
    """Variance of the Laplacian: higher = sharper (motion blur and night noise score low)."""
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(grey, cv2.CV_32F).var())


# --- Per-object state --------------------------------------------------------------------

@dataclass
class _Member:
    """One object and a polygon: the visit, held across short detection gaps and id switches."""
    track_id: int
    label: str
    group: str
    box: np.ndarray
    hits: int = 0
    misses: int = 0
    inside: bool = False
    first_in: float | None = None
    since: float = 0.0              # in the area since (the first check of this visit)
    last_in: float = 0.0
    lost_ts: float | None = None    # the track vanished at
    anchor: np.ndarray | None = None
    still_since: float = 0.0
    fired: set = field(default_factory=set)


@dataclass
class _LineTrack:
    track_id: int
    label: str
    group: str
    box: np.ndarray
    side: int = 0                   # committed side: -1 left, +1 right, 0 not known yet
    point: tuple = (0.0, 0.0)       # last foot point on the committed side
    cand: int = 0                   # the other side it is seen on now...
    cand_n: int = 0                 # ...in this many checks...
    cand_since: float = 0.0         # ...since
    last_cross: float = -1e18       # the last time it changed sides over the segment (counted or not)
    last_dir: int = 0               # the direction of the last counted crossing
    lost_ts: float | None = None
    # log_plates
    cands: list = field(default_factory=list)  # PlateCandidate near the line, best first
    seen_ts: float = 0.0            # the last detection...
    seen_box: np.ndarray | None = None
    still_since: float | None = None  # standing still since (log_plates)
    vel: tuple = (0.0, 0.0)         # ...the box's motion then (px / s)...
    tmpl: np.ndarray | None = None  # ...and a small grey picture of the vehicle in it
    tmpl_k: float = 1.0             # that picture's scale
    pending: AreaHit | None = None  # crossed: read once it leaves the band


class _PixelWatch:
    """The area's look: a reference small grey picture and the current one."""

    def __init__(self) -> None:
        self.ref: np.ndarray | None = None
        self.before: np.ndarray | None = None   # the reference before the last change
        self.cur: np.ndarray | None = None
        self.changed_since: float | None = None
        self.back_since: float | None = None
        self.state = "normal"                   # normal | changed (state_watch) / object | taken (left_object)
        self.state_since: float | None = None
        self.last_kind = ""
        self.last_update = -1e18
        self.level = 0.0                        # share of changed pixels, for diagnosis

    def reset(self) -> None:
        self.ref = self.before = self.cur = None
        self.changed_since = self.back_since = None
        # The current look becomes the new normal, so the shown state must not linger.
        self.state, self.state_since, self.last_kind = "normal", None, ""


def _norm(img: np.ndarray, valid: np.ndarray) -> np.ndarray:
    vals = img[valid]
    if vals.size < 8:
        return img - img.mean()
    return (img - vals.mean()) / (vals.std() + 4.0)  # +4: flat dark pictures do not blow up noise


def _changed(a: np.ndarray, b: np.ndarray, valid: np.ndarray) -> tuple[float, np.ndarray]:
    diff = np.abs(_norm(a, valid) - _norm(b, valid)) > PIXEL_DIFF
    diff &= valid
    return float(diff.sum()) / max(1, int(valid.sum())), diff


def _edges(img: np.ndarray, where: np.ndarray) -> float:
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    return float(mag[where].mean()) if where.any() else 0.0


class _AreaState:
    def __init__(self, area: dict) -> None:
        self.sig = _signature(area)
        self.members: dict[int, _Member] = {}
        self.lines: dict[int, _LineTrack] = {}
        self.count_in = self.count_out = 0
        self.day = ""
        self.occupied = False
        self.occupied_by: str | None = None
        self.occ_since: float | None = None
        self.empty_since: float | None = None
        self.last_event: str | None = None
        self.last_event_ts: float | None = None
        self.pix = _PixelWatch()
        self.mask_key: tuple | None = None
        self.mask: np.ndarray | None = None
        # ask_ai
        self.answer: bool | None = None
        self.answer_since: float | None = None
        self.ask_pending = False
        self.last_ask = -1e18
        self.last_asked: np.ndarray | None = None
        self.active = True


# --- Evaluator ---------------------------------------------------------------------------

class AreaEvaluator:
    """All areas of one camera. update() on the inference thread; live() from any thread."""

    def __init__(self, channel: int, camera: str = "", site_id: str = "", seed=None) -> None:
        self.channel = channel
        self.camera = camera or f"Camera {channel}"
        self.site_id = site_id
        self._seed = seed            # seed(area_id) -> (count_in, count_out) today, e.g. from the store
        self._states: dict[str, _AreaState] = {}
        self._areas: dict[str, dict] = {}
        self._asks: list[dict] = []
        self._lock = threading.RLock()
        self._light: float | None = None
        self._vel: dict[int, tuple] = {}   # track id -> box motion (px / s) at the last update
        self.size = (0, 0)

    # -- public ------------------------------------------------------------------------
    def update(self, areas: list[dict], tracks, frame: np.ndarray | None, ts: float,
               night: tuple[str, str] = ("00:00", "06:00")) -> list[AreaHit]:
        """tracks: objects with .id, .label and .box (x1, y1, x2, y2 frame pixels), the tracker's
        current ones. frame: the RAW frame (needed by left_object / state_watch / ask_ai / plates)."""
        with self._lock:
            return self._update(areas, tracks, frame, ts, night)

    def live(self, now: float | None = None) -> dict[str, dict]:
        """{area_id: {"state", "count_in", "count_out", "occupied_by", "since", "last_event",
        "last_event_ts", "inside", "text"}}: what the Live view shows next to each area."""
        with self._lock:
            out = {}
            for aid, st in self._states.items():
                area = self._areas.get(aid)
                if area is not None:
                    out[aid] = self._live(area, st, now)
            return out

    def between(self, areas: list[dict], frame: np.ndarray | None, ts: float) -> None:
        """A frame between two detections (cameras with a log_plates line): vehicles moving near the
        line are placed by their motion since the last detection (refined by a small template match)
        and the picture is kept as one more plate candidate. Cheap: nothing runs without a vehicle there.
        Only while PLATE_BETWEEN is on."""
        if frame is None or not PLATE_BETWEEN:
            return
        with self._lock:
            if (frame.shape[1], frame.shape[0]) != self.size:
                return
            w, h = self.size
            for area in areas:
                st = self._states.get(area.get("id"))
                if area.get("task") != "log_plates" or st is None or not st.lines or st.sig != _signature(area):
                    continue
                a, b = _poly_px(area, w, h)[:2]
                for lt in st.lines.values():
                    dt = ts - lt.seen_ts
                    if lt.lost_ts is not None or not 0 < dt <= PLATE_PREDICT_S or lt.tmpl is None:
                        continue
                    box_h = max(float(lt.box[3] - lt.box[1]), 1.0)
                    if math.hypot(*lt.vel) < PLATE_STILL * box_h:
                        continue
                    box = self._place(lt, frame, dt)
                    away = abs(side_of(a, b, foot(box))) / box_h
                    if away <= PLATE_APPROACH:
                        self._keep_crop(lt, frame, box, area, ts, away, detected=False)

    def take_ask(self) -> dict | None:
        """The oldest ask_ai question due: {"area", "crop", "frame", "ts", "rect", "question"}."""
        with self._lock:
            return self._asks.pop(0) if self._asks else None

    def pending_asks(self) -> int:
        with self._lock:
            return len(self._asks)

    def current_answer(self, area_id: str) -> bool | None:
        with self._lock:
            st = self._states.get(area_id)
            return st.answer if st else None

    def ai_answer(self, ask: dict, yes: bool | None, confidence: float = 1.0) -> list[AreaHit]:
        """The brain's answer to a take_ask() question (None: could not ask). An event only when
        the answer differs from the previous one (the first answer is logged too)."""
        with self._lock:
            area = ask["area"]
            st = self._states.get(area["id"])
            if st is None:
                return []
            st.ask_pending = False
            if yes is None:
                st.last_ask = ask["ts"] - float(area["params"].get("interval_s", 30)) / 2  # retry sooner
                return []
            st.last_asked = ask["small"]
            if st.answer is not None and st.answer == yes:
                return []
            st.answer, st.answer_since = yes, ask["ts"]
            ev = self._event(area, ask["ts"], "answer_yes" if yes else "answer_no", confidence=round(confidence, 2),
                             extra={"question": area["params"].get("question", "")})
            return [self._note(st, AreaHit(ev, area, None, frame=ask["frame"]))]

    def reset(self) -> None:
        with self._lock:
            self._states.clear()
            self._asks.clear()

    def reset_reference(self, area_id: str) -> bool:
        """"This is how it should look": the area's current picture becomes its normal look
        (left_object / state_watch). False when the area has no state yet."""
        with self._lock:
            st = self._states.get(area_id)
            if st is None:
                return False
            px = st.pix
            px.ref = px.cur.copy() if px.cur is not None else None
            px.before = px.changed_since = px.back_since = None
            px.state, px.state_since, px.last_kind = "normal", None, ""
            return True

    # -- update ------------------------------------------------------------------------
    def _update(self, areas, tracks, frame, ts, night) -> list[AreaHit]:
        if frame is not None:
            self.size = (frame.shape[1], frame.shape[0])
        w, h = self.size
        if not w or not h:
            return []
        objs = [(int(t.id), str(t.label), np.asarray(t.box[:4], np.float32).copy()) for t in tracks]
        vel = [getattr(t, "vel", None) for t in tracks]
        self._vel = {o[0]: (float(v[0]), float(v[1])) for o, v in zip(objs, vel) if v is not None and len(v) >= 2}
        jump = self._light_jump(frame)
        live_ids = set()
        hits: list[AreaHit] = []
        for area in areas:
            if not area.get("enabled", True) or area.get("task") == "watch_zone" or not area_valid(area):
                continue
            live_ids.add(area["id"])
            self._areas[area["id"]] = area
            st = self._states.get(area["id"])
            if st is None or st.sig != _signature(area):
                st = self._states[area["id"]] = _AreaState(area)
            self._new_day(area, st, ts)
            st.active = scheduled(area, ts, night)
            mine = [o for o in objs if wants(area.get("objects"), o[1])]
            if jump:
                st.pix.reset()
            try:
                hits += [self._note(st, hit) for hit in self._run(area, st, mine, objs, frame, ts, w, h)]
            except Exception:  # one broken area must not stop the others (or detection)
                log.exception("Area %s failed", area.get("name"))
        for aid in [a for a in self._states if a not in live_ids]:  # deleted / disabled areas
            del self._states[aid]
            self._areas.pop(aid, None)
        for hit in hits:
            hit.night = in_window(ts, *night)
        return hits

    def _run(self, area, st, objs, all_objs, frame, ts, w, h) -> list[AreaHit]:
        task = area["task"]
        if task in ("left_object", "state_watch"):
            # Everything tracked is masked out, whatever the area's objects: people and cars are the
            # other tasks' business, and a person standing there is not a "left object".
            return self._pixels(area, st, all_objs, frame, ts, w, h)
        if task == "ask_ai":
            self._schedule_ask(area, st, objs, frame, ts, w, h)
            return []
        if area["shape"] == "line":
            return self._line(area, st, objs, frame, ts, w, h)
        return self._polygon(area, st, objs, ts, w, h)

    def _new_day(self, area, st, ts) -> None:
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if st.day == day:
            return
        first = not st.day
        st.day = day
        st.count_in = st.count_out = 0
        if first and self._seed is not None and area["task"] in ("count", "line_cross", "log_plates"):
            try:
                st.count_in, st.count_out = self._seed(area["id"])
            except Exception:
                pass

    def _light_jump(self, frame) -> bool:
        if frame is None:
            return False
        small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
        level = float(small.mean()) + 1.0
        prev, self._light = self._light, level if self._light is None else 0.7 * self._light + 0.3 * level
        return prev is not None and abs(level - prev) / prev > LIGHT_JUMP

    def _event(self, area, ts, kind, **kw) -> AreaEvent:
        return new_event(area, self.channel, ts, kind, site_id=self.site_id, **kw)

    def _note(self, st: _AreaState, hit: AreaHit) -> AreaHit:
        st.last_event, st.last_event_ts = describe(hit.event), hit.event.ts
        return hit

    # -- polygons ------------------------------------------------------------------------
    def _members(self, st: _AreaState, poly, objs, ts) -> list[tuple[str, _Member]]:
        """Visit bookkeeping (see the module docstring): returns [("enter" | "exit", member)]."""
        diag = math.hypot(*self.size)
        changes = []
        seen = set()
        for tid, label, box in objs:
            m = st.members.get(tid) or self._inherit(st, tid, label, box, ts) or \
                _Member(tid, label, group_of(label), box)
            st.members[tid] = m
            seen.add(tid)
            m.track_id, m.label, m.box, m.lost_ts = tid, label, box, None
            if m.anchor is None or iou(box, m.anchor) < STILL_IOU:
                m.anchor, m.still_since = box.copy(), ts
            if self._is_in(poly, box, m.group, m.inside, diag):
                m.hits, m.misses, m.last_in = m.hits + 1, 0, ts
                m.first_in = ts if m.first_in is None else m.first_in
                if not m.inside and m.hits >= ENTER_CHECKS:
                    m.inside, m.since, m.fired = True, m.first_in, set()
                    changes.append(("enter", m))
            else:
                m.misses += 1
                if not m.inside:
                    m.hits, m.first_in = 0, None
                elif m.misses >= EXIT_CHECKS and ts - m.last_in >= EXIT_S:
                    m.inside, m.hits, m.first_in = False, 0, None
                    changes.append(("exit", m))
        for tid, m in list(st.members.items()):
            if tid in seen:
                continue
            m.lost_ts = m.lost_ts if m.lost_ts is not None else ts
            if ts - m.lost_ts >= LOST_GRACE_S[m.group] and ts - m.last_in >= EXIT_S:
                del st.members[tid]
                if m.inside:
                    m.inside = False
                    changes.append(("exit", m))
        return changes

    def _is_in(self, poly, box, group: str, was_in: bool, diag: float) -> bool:
        """With spatial hysteresis, so an object on the edge does not flicker in and out.
        People: where their feet are, a little inside to come in and a little outside to leave.
        Vehicles: how much of their footprint (the lower part of the box) lies inside; a parking
        spot drawn tightly around a car, or a car cut by the bottom of the picture, is still in,
        and a parked car whose bumper just reaches into the area is not."""
        if group == "vehicle":
            share = footprint_share(poly, box)
            return share >= (VEHICLE_OUT if was_in else VEHICLE_IN)
        fx, fy = foot(box)
        w, h = self.size
        margin = max(EDGE_MARGIN * diag, EDGE_MARGIN_BOX * float(box[3] - box[1]))
        if not was_in:  # feet at the bottom of the picture cannot be deep inside an area drawn to it
            margin = min(margin, max(0.0, min(fx, fy, w - fx, h - fy) - 1.0))
        return cv2.pointPolygonTest(poly, (fx, fy), True) >= (-margin if was_in else margin)

    @staticmethod
    def _inherit(st: _AreaState, tid, label, box, ts) -> _Member | None:
        """A new track where a vanished one of the same kind was: the same object (id switch)."""
        group = group_of(label)
        best, best_key = None, None
        box_h = max(float(box[3] - box[1]), 1.0)
        for old_id, m in st.members.items():
            if m.lost_ts is None or m.group != group or ts - m.lost_ts > LOST_GRACE_S[group]:
                continue
            score = iou(box, m.box)
            (fx, fy), (ox, oy) = foot(box), foot(m.box)
            dist = math.hypot(fx - ox, fy - oy)
            # as far as it could have walked while unseen (a detection gap of a couple of seconds)
            reach = (INHERIT_REACH + INHERIT_SPEED * (ts - m.lost_ts)) * box_h
            if score < INHERIT_IOU and dist >= reach:
                continue
            key = (score, -dist / box_h)
            if best_key is None or key > best_key:
                best, best_key = old_id, key
        return st.members.pop(best) if best is not None else None

    def _polygon(self, area, st, objs, ts, w, h) -> list[AreaHit]:
        poly = _poly_px(area, w, h)
        changes = self._members(st, poly, objs, ts)
        task, p = area["task"], area["params"]
        direction = area.get("direction", "both")
        hits = []

        def hit(kind, m: _Member | None, **kw) -> None:
            ev = self._event(area, ts, kind, label=m.label if m else "", track_id=m.track_id if m else None, **kw)
            hits.append(AreaHit(ev, area, tuple(float(v) for v in m.box) if m is not None else None))

        for change, m in changes:
            stay = ts - m.since
            if task in ("count", "line_cross") and st.active:
                way = "a_to_b" if change == "enter" else "b_to_a"
                if direction in ("both", way):
                    if change == "enter":
                        st.count_in += 1
                    else:
                        st.count_out += 1
                    kind = change if task == "count" else ("cross_in" if change == "enter" else "cross_out")
                    hit(kind, m, value=str(st.count_in if change == "enter" else st.count_out),
                        extra={"direction": "in" if change == "enter" else "out", "direction_filter": direction})
            if change == "exit" and task == "no_parking" and "blocked" in m.fired:
                hit("freed", m, value=duration_text(stay), extra={"duration_s": round(stay, 1)})
        inside = [m for m in st.members.values() if m.inside]
        for m in inside:
            stay = ts - m.since
            if task == "presence" and st.active and "presence" not in m.fired:
                m.fired.add("presence")
                hit("presence", m)
            elif task == "loitering" and st.active and "loitering" not in m.fired \
                    and stay >= float(p.get("seconds", 30)):
                m.fired.add("loitering")
                hit("loitering", m, value=duration_text(stay), extra={"duration_s": round(stay, 1)})
            elif task == "no_parking" and m.group == "vehicle" and "blocked" not in m.fired and st.active \
                    and ts - max(m.since, m.still_since) >= 60.0 * float(p.get("max_minutes", 5)):
                m.fired.add("blocked")  # standing still INSIDE that long (not still outside, then in)
                stay = ts - max(m.since, m.still_since)
                hit("blocked", m, value=duration_text(stay), extra={"duration_s": round(stay, 1)})
        if task == "parking_spot":
            hits += self._parking(area, st, inside, ts)
        return hits

    def _parking(self, area, st, inside: list[_Member], ts) -> list[AreaHit]:
        p = area["params"]
        vehicles = inside  # already only the area's objects (vehicles by default)
        if vehicles:
            st.empty_since = None
            m = min(vehicles, key=lambda v: v.since)
            if not st.occupied and ts - m.since >= float(p.get("occupied_after_s", 20)):
                st.occupied, st.occupied_by, st.occ_since = True, m.label, m.since
                ev = self._event(area, ts, "occupied", label=m.label, track_id=m.track_id,
                                 extra={"since": round(m.since, 2)})
                return [AreaHit(ev, area, tuple(float(v) for v in m.box))]
            return []
        if not st.occupied:
            return []
        st.empty_since = st.empty_since or ts
        if ts - st.empty_since < float(p.get("free_after_s", 45)):
            return []
        stay = st.empty_since - (st.occ_since or st.empty_since)
        ev = self._event(area, ts, "freed", label=st.occupied_by or "", value=duration_text(stay),
                         extra={"duration_s": round(stay, 1)})
        st.occupied, st.occupied_by, st.occ_since, st.empty_since = False, None, None, None
        return [AreaHit(ev, area, None)]

    # -- lines -----------------------------------------------------------------------------
    def _line(self, area, st, objs, frame, ts, w, h) -> list[AreaHit]:
        a, b = _poly_px(area, w, h)[:2]
        a, b = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length < 4:
            return []
        diag = math.hypot(w, h)
        task, direction = area["task"], area.get("direction", "both")
        hits = []
        seen = set()
        for tid, label, box in objs:
            lt = st.lines.get(tid) or self._inherit_line(st, tid, label, box, ts, side_of(a, b, foot(box))) or \
                _LineTrack(tid, label, group_of(label), box)
            st.lines[tid] = lt
            seen.add(tid)
            lt.track_id, lt.label, lt.box, lt.lost_ts = tid, label, box, None
            pt = foot(box)
            box_h = max(float(box[3] - box[1]), 1.0)
            d = side_of(a, b, pt)
            if task == "log_plates":
                hits += self._plate_step(area, st, lt, frame, box, d, box_h, ts)
            band = max(LINE_BAND * diag, LINE_BAND_BOX * box_h)
            s = 0 if abs(d) < band else (1 if d > 0 else -1)
            if s == 0:
                continue
            if lt.side == 0 or s == lt.side:
                lt.side, lt.point, lt.cand_n = s, pt, 0
                continue
            if lt.cand != s or lt.cand_n == 0:
                lt.cand, lt.cand_n, lt.cand_since = s, 0, ts
            lt.cand_n += 1
            if lt.cand_n < LINE_CONFIRM or ts - lt.cand_since < LINE_CONFIRM_S:
                continue
            crossed = self._crossing(a, b, lt.point, pt, length, diag)
            lt.side, lt.point, lt.cand_n = s, pt, 0
            if not crossed:
                # It went round the end of the segment: its next pass over the line is a new crossing.
                lt.last_dir = 0
                continue
            # Someone dithering on the line flips sides moments apart: only the first flip counts,
            # and never twice the same way without a counted crossing back in between.
            quick = ts - lt.last_cross < LINE_REVERSE_S
            lt.last_cross = ts
            if quick or s == lt.last_dir:
                continue
            lt.last_dir = s
            way = "a_to_b" if s > 0 else "b_to_a"
            if not st.active or direction not in ("both", way):
                continue
            hit = self._line_hit(area, st, lt, way, ts, direction)
            if task != "log_plates":
                hits.append(hit)
                continue
            if lt.pending is not None:  # back over the line before the last crossing was read
                hits.append(self._plate_hit(lt))
            lt.pending = hit
        for tid, lt in list(st.lines.items()):
            if tid not in seen:
                lt.lost_ts = lt.lost_ts if lt.lost_ts is not None else ts
                if lt.pending is not None and (ts - lt.lost_ts >= PLATE_LOST_S
                                               or ts - lt.pending.event.ts >= PLATE_HOLD_S):
                    hits.append(self._plate_hit(lt))
                if ts - lt.lost_ts > LINE_INHERIT_S[lt.group]:
                    del st.lines[tid]
        return hits

    @staticmethod
    def _crossing(a, b, p0, p1, length, diag) -> bool:
        """The move p0 -> p1 crosses the drawn segment (not its extension) and is a real move."""
        if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < LINE_MIN_MOVE * diag:
            return False
        d0, d1 = side_of(a, b, p0), side_of(a, b, p1)
        if d0 == d1:
            return False
        k = d0 / (d0 - d1)
        x, y = p0[0] + k * (p1[0] - p0[0]), p0[1] + k * (p1[1] - p0[1])
        t = ((x - a[0]) * (b[0] - a[0]) + (y - a[1]) * (b[1] - a[1])) / (length * length)
        return -LINE_EXTENT <= t <= 1 + LINE_EXTENT

    @staticmethod
    def _inherit_line(st: _AreaState, tid, label, box, ts, d: float) -> _LineTrack | None:
        """A new track id where a vanished one was, on the same side of the line: the same object.
        (The next vehicle in a queue is on the other side of the one that just crossed.)"""
        group = group_of(label)
        (fx, fy) = foot(box)
        box_h = max(float(box[3] - box[1]), 1.0)
        best, best_d = None, None
        for old_id, lt in st.lines.items():
            if lt.lost_ts is None or lt.group != group or ts - lt.lost_ts > LINE_INHERIT_S[group]:
                continue
            reach = (0.8 + INHERIT_SPEED * (ts - lt.lost_ts)) * box_h
            if best_d is not None:
                reach = min(reach, best_d)
            if lt.side and d * lt.side < 0 and abs(d) > LINE_BAND_BOX * float(box[3] - box[1]):
                continue
            ox, oy = foot(lt.box)
            dist = math.hypot(fx - ox, fy - oy)
            if dist < reach or iou(box, lt.box) >= INHERIT_IOU:
                best, best_d = old_id, dist if best_d is None else min(dist, best_d)
        return st.lines.pop(best) if best is not None else None

    def _line_hit(self, area, st, lt: _LineTrack, way: str, ts, direction) -> AreaHit:
        task = area["task"]
        inward = way == "a_to_b"
        if inward:
            st.count_in += 1
        else:
            st.count_out += 1
        extra = {"direction": "in" if inward else "out", "direction_filter": direction}
        box = tuple(float(v) for v in lt.box)
        if task == "log_plates":  # pictures are added when it is read (_plate_hit)
            ev = self._event(area, ts, "plate", label=lt.label, track_id=lt.track_id, extra=extra)
            return AreaHit(ev, area, box)
        kind = "cross_in" if inward else "cross_out"
        value = str(st.count_in if inward else st.count_out)
        ev = self._event(area, ts, kind, label=lt.label, track_id=lt.track_id, value=value, extra=extra)
        return AreaHit(ev, area, box)

    # -- plates ------------------------------------------------------------------------------
    def _plate_step(self, area, st, lt: _LineTrack, frame, box, d, box_h, ts) -> list[AreaHit]:
        """log_plates, at a detection: keep the vehicle's picture while it is in the band around the
        line, remember how it moves (for the frames until the next detection), and read a crossing
        once the vehicle has left the band."""
        vel = self._vel.get(lt.track_id)
        if vel is None:  # tracks without a speed (tests): from the last two detections
            dt = ts - lt.seen_ts
            old = lt.seen_box if lt.seen_box is not None else box
            vel = ((float(box[0] + box[2] - old[0] - old[2]) / 2 / dt, float(box[1] + box[3] - old[1] - old[3]) / 2 / dt)
                   if 0 < dt < 2 else (0.0, 0.0))
        far = abs(d) > PLATE_APPROACH * box_h
        moving = math.hypot(*vel) >= PLATE_STILL * box_h
        lt.still_since = None if moving else (lt.still_since if lt.still_since is not None else ts)
        lt.seen_ts, lt.vel, lt.seen_box, lt.tmpl = ts, vel, box.copy(), None
        # pictures of a car parked at the line would push out the moments of its next drive (and hold
        # whole frames): a standing car counts only while it waits briefly, e.g. at the barrier
        waiting = abs(d) <= PLATE_NEAR * box_h and ts - (lt.still_since or ts) <= PLATE_WAIT_S
        lt.cands = [c for c in lt.cands if ts - c.ts <= PLATE_MAX_AGE_S]
        if frame is not None and not far and (moving or waiting):
            self._keep_crop(lt, frame, box, area, ts, abs(d) / box_h)
            if moving:  # a small grey picture to find it again in the frames until the next detection
                x1, y1, x2, y2 = _clip_box(box, frame.shape[1], frame.shape[0])
                if x2 - x1 >= 8 and y2 - y1 >= 8:
                    lt.tmpl_k = min(1.0, PLATE_TMPL / (x2 - x1))
                    small = cv2.resize(frame[y1:y2, x1:x2], None, fx=lt.tmpl_k, fy=lt.tmpl_k,
                                       interpolation=cv2.INTER_AREA)
                    lt.tmpl = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if lt.pending is not None and (far or ts - lt.pending.event.ts >= PLATE_HOLD_S):
            return [self._plate_hit(lt)]
        return []

    @staticmethod
    def _plate_hit(lt: _LineTrack) -> AreaHit:
        """The pending crossing with every moment collected for it, best first."""
        hit, lt.pending = lt.pending, None
        hit.plate_cands, lt.cands = lt.cands, []
        hit.plate_crops = [c.crop for c in hit.plate_cands]
        return hit

    @staticmethod
    def _place(lt: _LineTrack, frame, dt: float) -> np.ndarray:
        """Where the vehicle is dt after its last detection: moved on at its speed, then the small
        template searched around that spot (a car braking at the barrier does not keep its speed)."""
        dx, dy = lt.vel[0] * dt, lt.vel[1] * dt
        box = lt.box[:4] + np.array([dx, dy, dx, dy], np.float32)
        bw, bh = float(box[2] - box[0]), float(box[3] - box[1])
        reach_x, reach_y = 0.15 * bw + abs(dx), 0.15 * bh + abs(dy)  # as far as from a dead stop
        x1, y1, x2, y2 = _clip_box((box[0] - reach_x, box[1] - reach_y, box[2] + reach_x, box[3] + reach_y),
                                   frame.shape[1], frame.shape[0])
        k = lt.tmpl_k
        th, tw = lt.tmpl.shape[:2]
        if (x2 - x1) * k < tw + 2 or (y2 - y1) * k < th + 2:
            return box  # at the edge of the picture: the motion alone
        small = cv2.cvtColor(cv2.resize(frame[y1:y2, x1:x2], None, fx=k, fy=k, interpolation=cv2.INTER_AREA),
                             cv2.COLOR_BGR2GRAY)
        if small.shape[0] < th or small.shape[1] < tw:
            return box
        _, score, _, (mx, my) = cv2.minMaxLoc(cv2.matchTemplate(small, lt.tmpl, cv2.TM_CCOEFF_NORMED))
        if score < PLATE_MATCH:
            return box
        nx, ny = x1 + mx / k, y1 + my / k
        return np.array([nx, ny, nx + bw, ny + bh], np.float32)

    @staticmethod
    def _keep_crop(lt: _LineTrack, frame, box, area, ts: float, away: float = 0.0, detected: bool = True) -> None:
        """Keep the vehicle's picture if it is among its best moments. away: box heights from the line
        (the user drew it where plates face the camera, so nearer moments rank a little higher)."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = _clip_box(box, w, h)
        pad_x, pad_y = (x2 - x1) // 20, (y2 - y1) // 20
        x1, y1, x2, y2 = max(0, x1 - pad_x), max(0, y1 - pad_y), min(w, x2 + pad_x), min(h, y2 + pad_y)
        if x2 - x1 < max(16, int(area["params"].get("min_plate_px", 80))) or y2 - y1 < 16:
            return  # narrower than a readable plate: nothing to read there
        crop = frame[y1:y2, x1:x2]  # a view: camera frames are replaced, never written
        try:
            from .plates import plate_quality
            quality = float(plate_quality(crop, full_px=PLATE_FULL_PX))
        except Exception:  # plates.py missing or failing: sharp and big is best
            quality = sharpness(crop) * math.sqrt((x2 - x1) * (y2 - y1))
        bx1, by1, bx2, by2 = (float(v) for v in box[:4])
        if min(bx1, by1, w - bx2, h - by2) < 3:  # cut by the edge of the picture: the plate is often what is cut
            quality *= PLATE_EDGE
        quality *= 1.0 - PLATE_FAR * min(1.0, away / PLATE_APPROACH)
        # a better picture replaces those less than PLATE_GAP_S away. While the score keeps rising this
        # walks along with the car and keeps its moments near the best one dense: spreading them evenly
        # over the approach (fixed time slots) read 2 plates instead of 8 on the replayed clips
        cands = [c for c in lt.cands if ts - c.ts <= PLATE_MAX_AGE_S]
        if any(abs(c.ts - ts) < PLATE_GAP_S and c.score >= quality for c in cands):
            lt.cands = cands
            return
        cands = [c for c in cands if abs(c.ts - ts) >= PLATE_GAP_S]
        cands.append(PlateCandidate(ts, quality, crop, tuple(float(v) for v in box[:4]), frame, detected))
        cands.sort(key=lambda c: -c.score)
        lt.cands = cands[:PLATE_CANDIDATES]

    # -- pictures of the area -----------------------------------------------------------------
    def _small(self, area, st, frame, objs, w, h):
        """(small grey picture, valid mask) of the area; tracked objects are masked out."""
        x1, y1, x2, y2 = area_rect(area, w, h)
        if x2 - x1 < 4 or y2 - y1 < 4:
            return None
        k = PIXEL_SIDE / max(x2 - x1, y2 - y1)
        sw, sh = max(4, round((x2 - x1) * k)), max(4, round((y2 - y1) * k))
        key = (x1, y1, x2, y2, sw, sh)
        if st.mask_key != key:
            pts = ((_poly_px(area, w, h) - (x1, y1)) * k).astype(np.int32)
            mask = np.zeros((sh, sw), np.uint8)
            cv2.fillPoly(mask, [pts], 1)
            st.mask, st.mask_key = mask.astype(bool), key
        grey = cv2.cvtColor(cv2.resize(frame[y1:y2, x1:x2], (sw, sh), interpolation=cv2.INTER_AREA),
                            cv2.COLOR_BGR2GRAY)
        grey = cv2.GaussianBlur(grey, (5, 5), 0).astype(np.float32)
        valid = st.mask.copy()
        for _, _, box in objs:  # grown: detection boxes rarely cover arms, shadows and mirrors
            gx, gy = MASK_GROW * (box[2] - box[0]), MASK_GROW * (box[3] - box[1])
            bx1, by1 = int((box[0] - gx - x1) * k) - 1, int((box[1] - gy - y1) * k) - 1
            bx2 = int(math.ceil((box[2] + gx - x1) * k)) + 1
            by2 = int(math.ceil((box[3] + gy - y1) * k)) + 1
            if bx2 > 0 and by2 > 0 and bx1 < sw and by1 < sh:
                valid[max(0, by1):by2, max(0, bx1):bx2] = False
        return grey, valid

    def _pixels(self, area, st, objs, frame, ts, w, h) -> list[AreaHit]:
        px = st.pix
        if frame is None or ts - px.last_update < PIXEL_EVERY_S:
            return []
        px.last_update = ts
        got = self._small(area, st, frame, objs, w, h)
        if got is None:
            return []
        grey, valid = got
        # averaged against night noise, except where something is in front: there the newest picture,
        # so the area is clean again as soon as a person walks off
        px.cur = grey if px.cur is None or px.cur.shape != grey.shape else \
            np.where(valid, (1 - PIXEL_AVG) * px.cur + PIXEL_AVG * grey, grey)
        if px.ref is None or px.ref.shape != grey.shape:
            px.ref, px.before, px.changed_since = px.cur.copy(), None, None
            return []
        if valid.sum() < MIN_VISIBLE * max(1, int(st.mask.sum())):
            return []  # mostly hidden: no evidence either way
        p = area["params"]
        seconds = float(p.get("seconds", 60))
        need = float(p.get("min_change", 0.06))
        level, diff = _changed(px.cur, px.ref, valid)
        px.level = level
        if px.before is not None:  # changed earlier: back to how it was?
            back, _ = _changed(px.cur, px.before, valid)
            if back < need / 2:
                px.back_since = px.back_since or ts
                if ts - px.back_since >= max(PIXEL_EVERY_S * 2, seconds / 3):
                    return self._restored(area, st, ts)
            else:
                px.back_since = None
        if level < need:
            px.changed_since = None
            if level < need / 2:  # follow the light, but not into what is hidden behind people / cars
                px.ref = np.where(valid, (1 - PIXEL_FOLLOW) * px.ref + PIXEL_FOLLOW * px.cur, px.ref)
            return []
        px.changed_since = px.changed_since or ts
        if ts - px.changed_since < seconds or not st.active:
            return []
        if area["task"] == "state_watch":
            kind, value, state = "changed", "changed", "changed"
        else:
            more = _edges(px.cur, diff) > 1.1 * _edges(px.ref, diff)
            kind, value, state = ("appeared", "", "object") if more else ("removed", "", "taken")
        px.state, px.state_since, px.last_kind = state, px.changed_since, kind
        px.before, px.ref, px.changed_since, px.back_since = px.ref, px.cur.copy(), None, None
        ev = self._event(area, ts, kind, value=value, confidence=round(min(1.0, level / max(need, 1e-3) / 2), 2),
                         extra={"changed_share": round(level, 3)})
        return [AreaHit(ev, area, None)]

    def _restored(self, area, st, ts) -> list[AreaHit]:
        px = st.pix
        px.ref, px.before, px.back_since, px.changed_since = px.cur.copy(), None, None, None
        last, px.state, px.state_since = px.last_kind, "normal", ts
        if not st.active:
            return []
        if area["task"] == "state_watch":
            ev = self._event(area, ts, "changed", value="restored")
        else:  # left_object: what was left is gone again / what was taken is back
            ev = self._event(area, ts, "removed" if last == "appeared" else "appeared", value="back to before")
        return [AreaHit(ev, area, None)]

    # -- ask_ai ------------------------------------------------------------------------------
    def _schedule_ask(self, area, st, objs, frame, ts, w, h) -> None:
        interval = max(5.0, float(area["params"].get("interval_s", 30)))
        if frame is None or st.ask_pending or not st.active or ts - st.last_ask < interval:
            return
        got = self._small(area, st, frame, [], w, h)
        if got is None:
            return
        grey, valid = got
        if st.last_asked is not None and st.last_asked.shape == grey.shape and ts - st.last_ask < AI_REFRESH_S:
            level, _ = _changed(grey, st.last_asked, valid)
            if level < AI_CHANGE:
                return  # same picture as last time: same answer
        st.last_ask, st.ask_pending = ts, True
        x1, y1, x2, y2 = area_rect(area, w, h, pad=0.1)
        self._asks.append({"area": area, "crop": frame[y1:y2, x1:x2].copy(), "frame": frame.copy(), "ts": ts,
                           "rect": (x1, y1, x2, y2), "question": str(area["params"].get("question", "")).strip(),
                           "small": grey})

    # -- live --------------------------------------------------------------------------------
    def _live(self, area, st: _AreaState, now) -> dict:
        task = area["task"]
        now = now if now is not None else (st.last_event_ts or 0.0)
        inside = [m for m in st.members.values() if m.inside]
        since, occupied_by = None, None
        if task in ("count", "line_cross", "log_plates"):
            state = "watching"
        elif task == "parking_spot":
            state = "occupied" if st.occupied else "free"
            since, occupied_by = (st.occ_since if st.occupied else None), st.occupied_by
        elif task == "no_parking":
            blocked = [m for m in inside if "blocked" in m.fired]
            state = "blocked" if blocked else ("vehicle" if inside else "clear")
            if inside:
                since = min(max(v.since, v.still_since) for v in inside)
                occupied_by = inside[0].label
        elif task in ("loitering", "presence"):
            state = "present" if inside else "clear"
            since = min((m.since for m in inside), default=None)
            occupied_by = inside[0].label if inside else None
        elif task in ("left_object", "state_watch"):
            state, since = st.pix.state, st.pix.state_since
        elif task == "ask_ai":
            state = "unknown" if st.answer is None else ("yes" if st.answer else "no")
            since = st.answer_since
        else:
            state = "watching"
        if not st.active:
            state = "paused"
        info = {"state": state, "count_in": st.count_in, "count_out": st.count_out, "occupied_by": occupied_by,
                "since": since, "last_event": st.last_event, "last_event_ts": st.last_event_ts,
                "inside": len(inside)}
        info["text"] = live_text(area, info, now)
        return info


def live_text(area: dict, info: dict, now: float) -> str:
    """The mini-stat next to an area on the Live view: "In 12 · Out 9", "Occupied · 14 min"."""
    task, state = area.get("task"), info.get("state")
    if state == "paused":
        return "Paused (outside its hours)"
    since = info.get("since")
    dur = f" · {duration_text(now - since)}" if since and now >= since else ""
    if task in ("count", "line_cross", "log_plates"):
        if area.get("shape") == "line" or task == "count":
            return f"In {info.get('count_in', 0)} · Out {info.get('count_out', 0)}"
        return f"Crossed {info.get('count_in', 0) + info.get('count_out', 0)}"
    if task == "parking_spot":
        return f"Occupied{dur}" if state == "occupied" else "Free"
    if task == "no_parking":
        who = (info.get("occupied_by") or "vehicle").capitalize()
        return {"blocked": f"{who} blocking{dur}", "vehicle": f"{who} standing{dur}"}.get(state, "Clear")
    if task in ("loitering", "presence"):
        n = info.get("inside", 0)
        return f"{n} inside{dur}" if n else "Clear"
    if task == "state_watch":
        return f"Changed{dur}" if state == "changed" else "As usual"
    if task == "left_object":
        return {"object": f"Something left{dur}", "taken": f"Something taken{dur}"}.get(state, "As usual")
    if task == "ask_ai":
        return {"yes": f"Yes{dur}", "no": f"No{dur}"}.get(state, "Not asked yet")
    return "Watching"
