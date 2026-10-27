"""Areas on the live video: the template catalogue, plain-language wording, shared geometry and
AreaCanvas, the widget that shows the camera picture with every area drawn as vectors and lets
the user draw or edit an area (polygon) or a line right on the playing video.

AreaCanvas is a drop-in for widgets.ImageView on the Live page (same set_image / clicked API).
The frame is scaled once per new frame (or resize) into a cached pixmap; mouse moves only repaint,
so handles, hover and the magnifier stay smooth while the video keeps playing.

Areas are plain dicts (config.Camera.areas): {"id", "name", "shape": "polygon"|"line", "points":
[[x, y], ...] normalised 0..1, "direction": "both"|"a_to_b"|"b_to_a", "task", "objects", "schedule",
"params", "alert", "enabled", "color"}.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from .config import ZONE_COLORS
from .incidents_ui import duration_text
from .theme import ICONS

# --- Icons (Lucide, ISC licence; "line" is drawn in the same style) ---------------------------

AREA_ICONS = {
    "pentagon": '<path d="M10.83 2.38a2 2 0 0 1 2.34 0l8 5.74a2 2 0 0 1 .73 2.25l-3.04 9.26a2 2 0 0 1-1.9 '
                '1.37H7.04a2 2 0 0 1-1.9-1.37L2.1 10.37a2 2 0 0 1 .73-2.25z"/>',
    "line": '<circle cx="5" cy="19" r="2"/><circle cx="19" cy="5" r="2"/><path d="M6.5 17.5 17.5 6.5"/>',
    "crossing": '<path d="m16 3 4 4-4 4"/><path d="M20 7H4"/><path d="m8 21-4-4 4-4"/><path d="M4 17h16"/>',
    "hash": '<line x1="4" x2="20" y1="9" y2="9"/><line x1="4" x2="20" y1="15" y2="15"/>'
            '<line x1="10" x2="8" y1="3" y2="21"/><line x1="16" x2="14" y1="3" y2="21"/>',
    "plate": '<rect width="20" height="12" x="2" y="6" rx="2"/><path d="M7 12h10"/>',
    "parking": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 17V7h4a3 3 0 0 1 0 6H9"/>',
    "ban": '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
    "package": '<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 '
               '4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"/><path d="M12 22V12"/>'
               '<path d="m3.3 7 7.703 4.734a2 2 0 0 0 1.994 0L20.7 7"/><path d="m7.5 4.27 9 5.15"/>',
    "door": '<path d="M13 4h3a2 2 0 0 1 2 2v14"/><path d="M2 20h3"/><path d="M13 20h9"/><path d="M10 12v.01"/>'
            '<path d="M13 4.562v16.157a1 1 0 0 1-1.242.97L5 20V5.562a2 2 0 0 1 1.515-1.94l4-1A2 2 0 0 1 '
            '13 4.561Z"/>',
    "ask": '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
           '<path d="M12 17h.01"/>',
    "bell": '<path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 '
            '.74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
    "redo": '<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5A5.5 5.5 0 0 0 4 14.5A5.5 5.5 0 0 0 9.5 20H13"/>',
    "freeze": '<path d="m10 20-1.25-2.5L6 18"/><path d="M10 4 8.75 6.5 6 6"/><path d="m14 20 1.25-2.5L18 18"/>'
              '<path d="m14 4 1.25 2.5L18 6"/><path d="m17 21-3-6h-4"/><path d="m17 3-3 6 1.5 3"/>'
              '<path d="M2 12h6.5L10 9"/><path d="m20 10-1.5 2 1.5 2"/><path d="M22 12h-6.5L14 15"/>'
              '<path d="m4 10 1.5 2L4 14"/><path d="m7 21 3-6-1.5-3"/><path d="m7 3 3 6h4"/>',
    "pencil": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 '
              '.623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2"/>'
            '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "list": '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/>'
            '<path d="M8 18h13"/><path d="M8 6h13"/>',
}
for _name, _shape in AREA_ICONS.items():  # one icon set for the whole app (theme.svg_icon reads ICONS)
    ICONS.setdefault(_name, _shape)

# --- Template catalogue ------------------------------------------------------------------------

GROUPS = ("SECURITY", "VEHICLES", "THINGS", "ASK THE AI")
OBJECTS = (("person", "People"), ("car", "Cars"), ("truck", "Trucks"), ("bus", "Buses"), ("motorcycle", "Motorbikes"))
OBJECT_WORD = {"person": ("person", "people"), "car": ("car", "cars"), "truck": ("truck", "trucks"),
               "bus": ("bus", "buses"), "motorcycle": ("motorbike", "motorbikes"), "vehicle": ("vehicle", "vehicles")}

# key -> UI facts. params are the UI defaults; the engine's own defaults (config.TEMPLATES) are merged in.
TEMPLATE_UI = {
    "watch_zone": dict(name="Watch zone", icon="scan", group="SECURITY", shapes=("polygon",), objects=(),
                       alert=False, example="Use the night, loitering and prowling alerts only inside this area.",
                       params={}),
    "loitering": dict(name="Someone lingers", icon="clock", group="SECURITY", shapes=("polygon",),
                      objects=("person",), alert=True, example="Person stays near the gate for more than 30 s.",
                      params={"seconds": 30}),
    "presence": dict(name="Anyone enters", icon="eye", group="SECURITY", shapes=("polygon",), objects=("person",),
                     alert=True, example="Tell me when anyone walks in at night.", params={}, schedule="night"),
    "line_cross": dict(name="Crossing a line", icon="crossing", group="SECURITY", shapes=("line", "polygon"),
                       objects=("person", "car"), alert=True,
                       example="Someone climbs over the fence, or a car drives the wrong way.", params={}),
    "log_plates": dict(name="Log number plates", icon="plate", group="VEHICLES", shapes=("line",),
                       objects=("car", "truck", "bus", "motorcycle"), alert=False,
                       example="Every car through the barrier: plate, in or out, how long it stayed.",
                       params={"min_plate_px": 80, "known": [], "in_name": "In", "out_name": "Out"}),
    "count": dict(name="Count", icon="hash", group="VEHICLES", shapes=("line", "polygon"), objects=("car",),
                  alert=False, example="How many cars came in and went out today.",
                  params={"in_name": "In", "out_name": "Out"}),
    "parking_spot": dict(name="Parking spot", icon="parking", group="VEHICLES", shapes=("polygon",),
                         objects=("car", "truck"), alert=False, example="Is my spot free? Who parked, and for how long?",
                         params={"occupied_after_s": 20}),
    "no_parking": dict(name="No parking", icon="ban", group="VEHICLES", shapes=("polygon",), objects=("car", "truck"),
                       alert=True, example="A car blocks the gate for more than 5 min.", params={"max_minutes": 5}),
    "left_object": dict(name="Left or removed object", icon="package", group="THINGS", shapes=("polygon",),
                        objects=(), alert=True, example="A parcel is left at the door, or something disappears.",
                        params={"seconds": 60}),
    "state_watch": dict(name="Gate or door left open", icon="door", group="THINGS", shapes=("polygon",), objects=(),
                        alert=False, example="The gate stays open for more than 2 min.", params={"seconds": 120}),
    "ask_ai": dict(name="Ask the AI", icon="ask", group="ASK THE AI", shapes=("polygon",), objects=(), alert=False,
                   example="Check a custom thing, e.g. ‘the barrier arm is raised’.",
                   params={"question": "", "interval_s": 30}),
}
try:  # the engine's template defaults (params it reads), when that part is installed
    from .config import TEMPLATES as _ENGINE_TEMPLATES
except ImportError:
    _ENGINE_TEMPLATES = {}

# kinds each task logs (the hub's kind filter chips)
TASK_KINDS = {
    "count": ("cross_in", "cross_out", "enter", "exit"),   # lines cross, areas are entered / left
    "log_plates": ("plate",),
    "parking_spot": ("occupied", "freed"),
    "no_parking": ("blocked", "freed"),
    "loitering": ("loitering",),
    "presence": ("presence",),
    "line_cross": ("cross_in", "cross_out"),
    "left_object": ("appeared", "removed"),
    "state_watch": ("changed",),
    "ask_ai": ("answer_yes", "answer_no"),
    "watch_zone": (),
}
KIND_NAMES = {
    "enter": "Came in", "exit": "Went out", "cross_in": "Crossed in", "cross_out": "Crossed out", "count": "Counted",
    "occupied": "Taken", "freed": "Free again", "blocked": "Blocked", "loitering": "Stayed", "presence": "Entered",
    "plate": "Plates", "appeared": "Appeared", "removed": "Taken away", "changed": "Changed",
    "answer_yes": "Yes", "answer_no": "No",
}


def template(key: str) -> dict:
    return TEMPLATE_UI.get(key, TEMPLATE_UI["watch_zone"])


def template_params(key: str) -> dict:
    """Default params of a template: the UI's, completed by the engine's own defaults."""
    engine = _ENGINE_TEMPLATES.get(key, {})
    engine = engine.get("params", engine) if isinstance(engine, dict) else {}
    params = {k: (list(v) if isinstance(v, list) else v) for k, v in template(key)["params"].items()}
    for k, v in engine.items():
        params.setdefault(k, list(v) if isinstance(v, list) else v)
    return params


def shape_badge(key: str) -> str:
    shapes = template(key)["shapes"]
    return "Area or line" if len(shapes) > 1 else "Line" if shapes == ("line",) else "Area"


def fits_shape(key: str, shape: str) -> bool:
    return shape in template(key)["shapes"]


def area_color(area: dict) -> QColor:
    return QColor(ZONE_COLORS[int(area.get("color", 0) or 0) % len(ZONE_COLORS)])


# --- Wording ----------------------------------------------------------------------------------

def obj_word(label: str, n: int = 1) -> str:
    one, many = OBJECT_WORD.get(label or "", (label or "object", (label or "object") + "s"))
    return one if n == 1 else many


def objects_text(objects: list[str], n: int = 2) -> str:
    """'cars', 'people and cars', 'cars, trucks and buses'."""
    words = [obj_word(o, n) for o in objects] or ["objects"]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def short_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{max(1, int(seconds))} s"
    return duration_text(seconds)


def stayed_text(seconds: float) -> str:
    """Duration for logs: '45 s', '14 min', '2 h 14 min'."""
    return short_duration(seconds)


def _extra(e) -> dict:
    x = getattr(e, "extra", None)
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and x:
        try:
            import json
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}


def event_duration(e) -> float | None:
    """Seconds an event lasted (stay, block, change), from extra or a numeric value."""
    x = _extra(e)
    for key in ("duration_s", "stayed_s", "dwell_s", "seconds"):
        if isinstance(x.get(key), (int, float)):
            return float(x[key])
    if getattr(e, "kind", "") in ("freed", "blocked", "loitering", "changed", "removed", "appeared"):
        try:
            return float(getattr(e, "value", "") or "")
        except ValueError:
            return None
    return None


def plate_stay(e) -> float | None:
    """Seconds a vehicle stayed (its 'out' plate event matched to the last 'in')."""
    x = _extra(e)
    for key in ("stay_s", "stayed_s"):
        if isinstance(x.get(key), (int, float)):
            return float(x[key])
    return None


def event_direction(e) -> str:
    """'in', 'out' or '' for a crossing / plate event."""
    kind = getattr(e, "kind", "")
    if kind in ("cross_in", "enter"):
        return "in"
    if kind in ("cross_out", "exit"):
        return "out"
    d = str(_extra(e).get("direction", "")).lower()
    return {"in": "in", "cross_in": "in", "a_to_b": "in", "enter": "in",
            "out": "out", "cross_out": "out", "b_to_a": "out", "exit": "out"}.get(d, "")


def dir_name(area: dict | None, direction: str) -> str:
    params = (area or {}).get("params") or {}
    if direction == "in":
        return params.get("in_name") or "In"
    if direction == "out":
        return params.get("out_name") or "Out"
    return ""


def event_text(e, area: dict | None = None) -> str:
    """One area event as a plain sentence for logs, popovers and tooltips."""
    kind = getattr(e, "kind", "")
    label = getattr(e, "label", "") or ""
    obj = obj_word(label).capitalize() if label else "Something"
    dur = event_duration(e)
    dur_s = stayed_text(dur) if dur is not None else ""
    value = str(getattr(e, "value", "") or "")
    question = ((area or {}).get("params") or {}).get("question") or _extra(e).get("question") \
        or getattr(e, "area_name", "") or "the question"
    if kind == "enter" or kind == "presence":
        return f"{obj} entered"
    if kind == "exit":
        return f"{obj} left"
    if kind == "cross_in":
        return f"{obj} crossed · {dir_name(area, 'in')}"
    if kind == "cross_out":
        return f"{obj} crossed · {dir_name(area, 'out')}"
    if kind == "count":
        try:
            n = int(float(value))
        except ValueError:
            n = 1
        return f"{n} {obj_word(label or 'object', n)} counted"
    if kind == "occupied":
        plate = _extra(e).get("plate") or ""
        return f"Taken by a {obj_word(label or 'vehicle')}" + (f" · {plate}" if plate else "")
    if kind == "freed":
        return f"Free again after {dur_s}" if dur_s else "Free again"
    if kind == "blocked":
        return f"{obj} parked for {dur_s}" if dur_s else f"{obj} parked too long"
    if kind == "loitering":
        return f"{obj} stayed {dur_s}" if dur_s else f"{obj} stayed too long"
    if kind == "plate":
        plate = value or "Unreadable plate"
        d = event_direction(e)
        moved = {"in": "came in", "out": "went out"}.get(d, "passed")
        stayed = plate_stay(e)
        return f"{plate} {moved}" + (f" · stayed {stayed_text(stayed)}" if stayed is not None else "")
    back = value == "back to before"     # left_object: the area looks like before again
    if kind == "appeared":
        return "What was taken is back" if back else "Something new appeared"
    if kind == "removed":
        return "The left object is gone again" if back else "Something was taken away"
    if kind == "changed":
        if value == "restored":
            return "Back to normal"
        return f"Looks different for {dur_s}" if dur_s else "Looks different"
    if kind == "answer_yes":
        return f"Yes: {question}"
    if kind == "answer_no":
        return f"No longer: {question}"
    return KIND_NAMES.get(kind, kind.replace("_", " ").capitalize() or "Event")


# Monitor.area_live states that mean "something is going on" (fill drawn stronger, hub line coloured)
ACTIVE_STATES = {"occupied", "taken", "inside", "active", "yes", "changed", "blocked", "appeared", "present",
                 "loitering", "removed", "object", "vehicle"}


def is_active(live: dict | None) -> bool:
    return bool(live) and str(live.get("state", "")).lower() in ACTIVE_STATES


def stat_text(area: dict, live: dict | None, watching: bool, now: float | None = None) -> str:
    """The short live line under an area's name ('In 12 · Out 9', 'Taken · 14 min', 'Not watching')."""
    if not area.get("enabled", True):
        return "Off"
    if not watching:
        return "Not watching"
    live = live or {}
    now = time.time() if now is None else now
    task = area.get("task", "watch_zone")
    state = str(live.get("state", "")).lower()
    since = live.get("since")
    age = short_duration(now - since) if isinstance(since, (int, float)) and since > 0 else ""
    active = state in ACTIVE_STATES
    objects = area.get("objects") or list(template(task)["objects"])
    if state == "paused":
        return "Paused (outside its hours)"
    if task == "count":
        cin, cout = int(live.get("count_in", 0) or 0), int(live.get("count_out", 0) or 0)
        d = area.get("direction", "both")
        parts = []
        if d in ("both", "a_to_b") or area.get("shape") == "polygon":
            parts.append(f"{dir_name(area, 'in')} {cin}")
        if d in ("both", "b_to_a") and area.get("shape") != "polygon":
            parts.append(f"{dir_name(area, 'out')} {cout}")
        return " · ".join(parts)
    if task == "parking_spot":
        return f"Taken · {age}" if active and age else "Taken" if active else "Free"
    if task == "log_plates":
        plate = live.get("plate") or ""
        if plate:
            from .alerts_ui import rel_time
            return f"{plate} · {rel_time(since, now)}" if isinstance(since, (int, float)) and since > 0 else plate
        cin, cout = int(live.get("count_in", 0) or 0), int(live.get("count_out", 0) or 0)
        return f"{dir_name(area, 'in')} {cin} · {dir_name(area, 'out')} {cout}" if cin or cout else "No vehicles yet"
    if task == "line_cross":
        n = int(live.get("count_in", 0) or 0) + int(live.get("count_out", 0) or 0)
        return f"Crossed {n} today" if n else "Watching"
    if task in ("loitering", "presence", "watch_zone"):
        n = int(live.get("inside", 1 if active else 0) or 0)
        label = live.get("occupied_by") or (objects[0] if objects else "person")
        return f"{n} {obj_word(label, n)} inside" if n else "Clear"
    if task == "no_parking":
        label = obj_word(live.get("occupied_by") or "car").capitalize()
        if state == "blocked":
            return f"{label} blocking · {age}" if age else f"{label} blocking"
        return f"{label} · {age}" if active and age else "Clear"
    if task == "ask_ai":
        if state == "yes":
            return f"Yes · {age} ago" if age else "Yes"
        if state == "no":
            return "No"
        return "Not checked yet"
    if task == "state_watch":
        return f"Changed · {age}" if active and age else "Changed" if active else "Looks normal"
    if task == "left_object":
        if state in ("removed", "taken"):
            return f"Something taken · {age}" if age else "Something taken away"
        return f"New object · {age}" if active and age else "New object" if active else "Clear"
    return ""


# --- Geometry (normalised or pixel points; same maths either way) ----------------------------

def side_of(a, b, p) -> float:
    """Which side of the line A->B point p is on: > 0 right of A->B as seen on the picture
    (y grows downwards), < 0 left, 0 on the line."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def crossing_normal(a, b) -> tuple[float, float]:
    """Unit vector of an "a_to_b" crossing: from the left side of A->B to the right side."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1.0
    return -dy / n, dx / n


try:  # the engine's own helpers, so the drawn arrow is always the direction that is counted
    from .areas import arrow_normal as crossing_normal, side_of  # noqa: F811
except Exception:  # engine part not installed: the definitions above follow the same contract
    pass


def direction_words(vec) -> str:
    """Plain words for a direction on the picture: 'towards the camera', 'left to right'..."""
    vx, vy = vec
    if abs(vy) >= abs(vx):
        return "towards the camera" if vy > 0 else "away from the camera"
    return "left to right" if vx > 0 else "right to left"


def direction_arrow(vec) -> str:
    vx, vy = vec
    if abs(vy) >= abs(vx):
        return "↓" if vy > 0 else "↑"
    return "→" if vx > 0 else "←"


def line_direction_labels(a, b) -> dict:
    """{"a_to_b": (arrow, words), "b_to_a": (arrow, words)} for a line in picture pixels."""
    n = crossing_normal(a, b)
    back = (-n[0], -n[1])
    return {"a_to_b": (direction_arrow(n), direction_words(n)), "b_to_a": (direction_arrow(back), direction_words(back))}


def poly_area(pts) -> float:
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1, p2, p3, p4) -> bool:
    """Proper crossing of two segments (touching ends do not count)."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and 0 not in (d1, d2, d3, d4)


def crossing_edges(pts, closed: bool = True) -> set[int]:
    """Indices of polygon edges (edge i = pts[i] -> pts[i+1]) that cross another edge."""
    n = len(pts)
    edges = [(i, pts[i], pts[(i + 1) % n]) for i in range(n if closed else n - 1)]
    bad: set[int] = set()
    for x in range(len(edges)):
        for y in range(x + 1, len(edges)):
            i, a, b = edges[x]
            j, c, d = edges[y]
            if abs(i - j) == 1 or (closed and {i, j} == {0, n - 1}):
                continue  # neighbours share a corner
            if segments_cross(a, b, c, d):
                bad.update((i, j))
    return bad


def point_in_poly(p, pts) -> bool:
    inside = False
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > p[1]) != (y2 > p[1]):
            x = x1 + (p[1] - y1) * (x2 - x1) / (y2 - y1)
            if p[0] < x:
                inside = not inside
    return inside


def dist_to_segment(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == dy == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def foot_point(box) -> tuple[float, float]:
    """Where an object stands: bottom centre of its box (x1, y1, x2, y2)."""
    return (box[0] + box[2]) / 2, box[3]


def crossed(a, b, p0, p1) -> int:
    """+1 when the move p0 -> p1 crosses segment A-B in the a_to_b direction, -1 the other way, 0 not."""
    s0, s1 = side_of(a, b, p0), side_of(a, b, p1)
    if (s0 > 0) == (s1 > 0) or s0 == 0 or not segments_cross(a, b, p0, p1):
        return 0
    return 1 if s1 > 0 else -1


# --- Draft -------------------------------------------------------------------------------------

MAX_CORNERS = 24
MIN_AREA = 0.001          # of the picture
SHORT_LINE = 0.06         # of the picture diagonal
EDGE_MARGIN = 0.04        # of the picture size
HANDLE_HIT = 10
CLOSE_RADIUS = 12
SNAP_PX = 8
DEDUPE_PX = 4
DRAG_START = 6
ARROW_R = 11
LOUPE = 132
LOUPE_ZOOM = 3.0
TAG_MAX_W = 220
BAR_OUTSIDE = 56          # px of letterbox above the picture that fit the drawing bar
HINT_BG = QColor(15, 17, 21, 210)
TAG_TEXT = QColor(20, 20, 20)


@dataclass
class Draft:
    shape: str = "polygon"
    points: list = field(default_factory=list)   # [[x, y]] normalised
    closed: bool = False
    direction: str = "both"

    def copy(self) -> "Draft":
        return Draft(self.shape, [list(p) for p in self.points], self.closed, self.direction)

    def same(self, other: "Draft") -> bool:
        return (self.shape, self.points, self.closed, self.direction) == \
            (other.shape, other.points, other.closed, other.direction)


class _DraftCommand(QUndoCommand):
    """One undo step: the draft before and after a change (applied already when pushed)."""

    def __init__(self, canvas: "AreaCanvas", before: Draft, after: Draft, text: str):
        super().__init__(text)
        self.canvas, self.before, self.after = canvas, before, after
        self._done = True

    def redo(self):
        if self._done:
            self._done = False
            return
        self.canvas._apply(self.after)

    def undo(self):
        self.canvas._apply(self.before)


# --- Canvas ------------------------------------------------------------------------------------

class AreaCanvas(QWidget):
    """The camera picture with areas painted on top; draws and edits a draft area."""

    clicked = Signal()                    # ImageView compatibility: click on the picture outside any area
    area_clicked = Signal(str, QPointF)   # area id, position in this widget
    draft_changed = Signal()              # points / shape / direction changed (panel re-validates)
    draft_closed = Signal()               # a polygon was closed or a line finished
    escape_pressed = Signal()             # the controller decides what Esc does (see LiveAreas.escape)
    enter_pressed = Signal()              # Enter while editing a finished shape (panel: Next / Save)
    preview_changed = Signal(str)         # "Right now: 1 car inside" for the draft
    freeze_changed = Signal(bool)

    def __init__(self, ctx, placeholder: str = "Select a camera"):
        super().__init__()
        self.ctx = ctx
        self.setObjectName("AreaCanvas")
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setAccessibleName("Live camera picture")
        self._img: QImage | None = None        # what is shown (the frozen frame while frozen)
        self._latest: QImage | None = None     # newest frame from the camera
        self._pm: QPixmap | None = None
        self._pm_key: tuple | None = None
        self._text = placeholder
        self.frozen = False
        self.offline_note = ""
        self.areas: list[dict] = []
        self.live: dict = {}
        self.watching = False
        self.show_areas = True
        self.compact_tags = False
        self.hover_id: str | None = None
        self.selected_id: str | None = None
        # draft
        self.mode = "view"                     # view | draw | edit
        self.draft = Draft()
        self.draft_color = 0
        self.editing_id: str | None = None
        self.sel_vertex: int | None = None
        self.preview_objects: set[str] = set()
        self.undo_stack = QUndoStack(self)
        self._warning = ""
        self._bad_edges: set[int] = set()
        self._bad_rubber = False        # the edge being drawn to the cursor crosses a placed one
        self._mouse: QPointF | None = None
        self._press: QPointF | None = None
        self._drag: str | None = None
        self._drag_index = -1
        self._drag_before: Draft | None = None
        self._drag_origin: list | None = None
        self._snap: QPointF | None = None
        self._loupe: QPointF | None = None
        self._shift = False
        # live tracks (foot points while drawing) and the draft preview
        self.tracks: list[tuple] = []          # (track_id, label, x1, y1, x2, y2) normalised
        self._feet: dict = {}                  # track id -> last foot point (normalised)
        self._crossings: list[tuple] = []      # (monotonic time, label, +1 / -1)
        self._preview = ""
        # event pulse
        self._pulse: dict[str, float] = {}
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(30)
        self._pulse_timer.timeout.connect(self._tick_pulse)

    # --- ImageView-compatible picture ------------------------------------------------------------

    def set_image(self, img: QImage | None, text: str = "") -> None:
        if img is None:
            if self.drafting and self._img is not None:
                # drawing goes on over the last picture while the camera is away
                self.offline_note = f"Camera offline · drawing on the picture from {time.strftime('%H:%M')}"
                if not self.frozen:
                    self.set_frozen(True)
                self.update()
                return
            self._img = self._latest = None
            self._pm = None
            self._text = text
            self.update()
            return
        self._latest = img
        self.offline_note = ""
        if self.frozen and self._img is not None:
            return
        self._img = img
        self._pm_key = None
        self.update()

    def image(self) -> QImage | None:
        return self._img

    def latest_image(self) -> QImage | None:
        return self._latest

    def set_frozen(self, on: bool) -> None:
        if on == self.frozen:
            return
        self.frozen = on
        if not on:
            self.offline_note = ""
            if self._latest is not None:
                self._img = self._latest
                self._pm_key = None
        self.freeze_changed.emit(on)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(640, 360)

    def minimumSizeHint(self) -> QSize:
        return QSize(160, 90)

    # --- geometry --------------------------------------------------------------------------

    def image_rect(self) -> QRectF:
        if self._img is None or self._img.width() < 1:
            return QRectF(self.rect())
        w, h = self._img.width(), self._img.height()
        scale = min(self.width() / w, self.height() / h)
        dw, dh = w * scale, h * scale
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)

    def to_norm(self, pos: QPointF) -> list[float]:
        r = self.image_rect()
        x = min(max((pos.x() - r.x()) / max(r.width(), 1.0), 0.0), 1.0)
        y = min(max((pos.y() - r.y()) / max(r.height(), 1.0), 0.0), 1.0)
        return [round(x, 4), round(y, 4)]

    def to_px(self, pt) -> QPointF:
        r = self.image_rect()
        return QPointF(r.x() + pt[0] * r.width(), r.y() + pt[1] * r.height())

    def _pts_px(self, pts) -> list[QPointF]:
        return [self.to_px(p) for p in pts]

    @staticmethod
    def _xy(p: QPointF) -> tuple[float, float]:
        return p.x(), p.y()

    @staticmethod
    def _dist(a: QPointF, b: QPointF) -> float:
        return math.hypot(a.x() - b.x(), a.y() - b.y())

    # --- areas (view) ----------------------------------------------------------------------

    def set_areas(self, areas: list[dict]) -> None:
        self.areas = [a for a in areas if isinstance(a, dict)]
        ids = {a.get("id") for a in self.areas}
        if self.hover_id not in ids:
            self.hover_id = None
        if self.selected_id not in ids:
            self.selected_id = None
        self.update()

    def set_live(self, live: dict, watching: bool) -> None:
        self.live, self.watching = live or {}, watching
        self.update()

    def set_show_areas(self, on: bool) -> None:
        self.show_areas = on
        self.update()

    def _shown_areas(self) -> list[dict]:
        return [a for a in self.areas if a.get("id") != self.editing_id]

    def area_at(self, pos: QPointF) -> str | None:
        if not self.show_areas:
            return None
        for a in reversed(self._shown_areas()):
            pts = self._pts_px(a.get("points", []))
            if a.get("shape") == "line" and len(pts) == 2:
                mid = (pts[0] + pts[1]) / 2
                if dist_to_segment(self._xy(pos), self._xy(pts[0]), self._xy(pts[1])) <= 8 or \
                        self._dist(pos, mid) <= ARROW_R + 3:
                    return a.get("id")
            elif len(pts) >= 3 and QPolygonF(pts).containsPoint(pos, Qt.OddEvenFill):
                return a.get("id")
        return None

    def area_rect(self, area_id: str) -> QRectF:
        a = next((x for x in self.areas if x.get("id") == area_id), None)
        if a is None:
            return QRectF()
        pts = self._pts_px(a.get("points", []))
        return QPolygonF(pts).boundingRect() if pts else QRectF()

    def pulse(self, area_id: str) -> None:
        """Short outline pulse when an area logs an event."""
        self._pulse[area_id] = time.monotonic()
        self._pulse_timer.start()

    def _tick_pulse(self) -> None:
        now = time.monotonic()
        self._pulse = {k: t for k, t in self._pulse.items() if now - t < 0.6}
        if not self._pulse:
            self._pulse_timer.stop()
        self.update()

    # --- draft API -------------------------------------------------------------------------

    @property
    def drafting(self) -> bool:
        return self.mode != "view"

    def begin_draft(self, shape: str = "polygon", color: int = 0, draft: Draft | None = None,
                    editing_id: str | None = None) -> None:
        self.draft = draft.copy() if draft is not None else Draft(shape)
        self.mode = "edit" if self.draft.closed else "draw"
        self.draft_color = color
        self.editing_id = editing_id
        self.sel_vertex = None
        self._warning = ""
        self._bad_edges = set()
        self._feet, self._crossings, self._preview = {}, [], ""
        self.undo_stack.clear()
        self.hover_id = None
        self._update_cursor()
        self.setFocus(Qt.OtherFocusReason)
        self.draft_changed.emit()
        self.update()

    def end_draft(self) -> None:
        self.mode = "view"
        self.draft = Draft()
        self.editing_id = None
        self.sel_vertex = None
        self._drag = None
        self._loupe = None
        self._warning = ""
        self.undo_stack.clear()
        self.set_frozen(False)
        self._update_cursor()
        self.update()

    def set_shape(self, shape: str) -> None:
        """Area or Line. A shape already drawn is replaced (one undo step)."""
        if shape == self.draft.shape and (self.draft.points or self.mode == "draw"):
            return
        before = self.draft.copy()
        self.draft = Draft(shape, direction=self.draft.direction)
        self.mode = "draw"
        self.sel_vertex = None
        self._push(before, "Change shape")

    def set_direction(self, direction: str) -> None:
        if direction == self.draft.direction:
            return
        before = self.draft.copy()
        self.draft.direction = direction
        self._push(before, "Change direction")

    def cycle_direction(self) -> None:
        order = ("both", "a_to_b", "b_to_a")
        i = order.index(self.draft.direction) if self.draft.direction in order else 0
        self.set_direction(order[(i + 1) % 3])

    def undo(self) -> None:
        if self._drag is None:
            self.undo_stack.undo()

    def redo(self) -> None:
        if self._drag is None:
            self.undo_stack.redo()

    def drop_in_progress(self) -> bool:
        """Esc while placing points: forget the unfinished shape. False when there is none."""
        if self.mode != "draw" or not self.draft.points:
            return False
        before = self.draft.copy()
        self.draft.points = []
        self._push(before, "Clear")
        return True

    def remove_last_point(self) -> bool:
        if self.mode != "draw" or not self.draft.points:
            return False
        before = self.draft.copy()
        self.draft.points.pop()
        self._push(before, "Remove corner")
        return True

    def finish(self) -> bool:
        """Close the polygon being drawn (Enter, double-click, click on the first corner)."""
        d = self.draft
        if self.mode != "draw":
            return False
        if d.shape == "line":
            if len(d.points) == 2:
                before = d.copy()
                d.closed = True
                self.mode = "edit"
                self._push(before, "Finish line")
                self.draft_closed.emit()
                return True
            self._warn("Drag from one side of the path to the other.")
            return False
        if len(d.points) < 3:
            self._warn("Add at least 3 corners")
            return False
        bad = crossing_edges(d.points)
        if bad:
            self._bad_edges = bad
            self._warn("Two edges cross. Move a corner so the shape does not twist.")
            return False
        if poly_area(d.points) < MIN_AREA:
            self._warn("This area is very small. Make it bigger so whole cars or people fit inside.")
            return False
        before = d.copy()
        d.closed = True
        self.mode = "edit"
        self._push(before, "Finish area")
        self.draft_closed.emit()
        return True

    def validity(self) -> tuple[bool, str]:
        """(can be saved, message). Messages for valid shapes are warnings only."""
        d = self.draft
        if not d.closed:
            return False, ""
        if d.shape == "polygon":
            if len(d.points) < 3:
                return False, "Add at least 3 corners"
            if crossing_edges(d.points):
                return False, "Two edges cross. Move a corner so the shape does not twist."
            if poly_area(d.points) < MIN_AREA:
                return False, "This area is very small. Make it bigger so whole cars or people fit inside."
            return True, ""
        if len(d.points) != 2:
            return False, "Drag from one side of the path to the other."
        return True, self.line_warning()

    def line_warning(self) -> str:
        if self.draft.shape != "line" or len(self.draft.points) != 2:
            return ""
        r = self.image_rect()
        a, b = self._pts_px(self.draft.points)
        if self._dist(a, b) < SHORT_LINE * math.hypot(r.width(), r.height()):
            return "Short lines are easy to miss. Make it cross the whole path."
        (ax, ay), (bx, by) = self.draft.points
        m = EDGE_MARGIN
        along_x = abs(bx - ax) >= abs(by - ay)
        near = (along_x and (max(ay, by) > 1 - m or min(ay, by) < m)) or \
               (not along_x and (max(ax, bx) > 1 - m or min(ax, bx) < m))
        if near:
            return "Objects at the edge of the picture are often not recognised yet. Move the line further in."
        return ""

    def direction_labels(self) -> dict:
        if self.draft.shape != "line" or len(self.draft.points) != 2:
            return {"a_to_b": ("↓", "towards the camera"), "b_to_a": ("↑", "away from the camera")}
        a, b = self._pts_px(self.draft.points)
        return line_direction_labels(self._xy(a), self._xy(b))

    def hint_text(self) -> str:
        if self._warning:
            return self._warning
        d = self.draft
        if self.mode == "draw":
            if d.shape == "polygon":
                if not d.points:
                    return "Click the corners of the area, or drag to draw a rectangle. L draws a line instead."
                if self._bad_rubber or self._bad_edges:
                    return "Two edges cross. Move the corner so the edges do not cross."
                if len(d.points) < 3:
                    return "Click to add corners. Right-click removes the last one."
                return "Click the first corner, double-click or press Enter to finish."
            if not d.points:
                return "Drag from one side of the path to the other. P draws an area instead."
            if self._drag == "line_new":   # the mouse button is still down
                return "Release where the line should end."
            return "Click where the line should end."
        if self.mode == "edit":
            warn = self.line_warning()
            if warn:
                return warn
            if d.shape == "line":
                return "Drag the ends to adjust. Click the arrow to choose the direction."
            return "Drag corners to adjust. Drag a middle dot to add a corner. Right-click a corner to remove it."
        return ""

    def _warn(self, text: str) -> None:
        self._warning = text
        self.update()
        self.draft_changed.emit()

    def _push(self, before: Draft, text: str) -> None:
        self._warning = ""
        if not before.same(self.draft):
            self.undo_stack.push(_DraftCommand(self, before, self.draft.copy(), text))
        self._after_change()

    def _apply(self, d: Draft) -> None:
        """Undo / redo: show that draft."""
        self.draft = d.copy()
        self.mode = "edit" if d.closed else "draw"
        if self.sel_vertex is not None and self.sel_vertex >= len(d.points):
            self.sel_vertex = None
        self._warning = ""
        self._after_change()

    def _after_change(self) -> None:
        d = self.draft
        self._bad_edges = crossing_edges(d.points, closed=d.closed) \
            if d.shape == "polygon" and len(d.points) > 3 else set()
        self._bad_rubber = self._bad_rubber and not d.closed and d.shape == "polygon"
        self._update_cursor()
        self.update()
        self.draft_changed.emit()

    # --- live tracks -----------------------------------------------------------------------

    def set_tracks(self, tracks) -> None:
        """Current AI tracks (normalised): (id, label, x1, y1, x2, y2) tuples or dicts with those keys.
        Shown as foot dots while drawing and used for the draft's live preview."""
        norm = []
        for t in tracks or []:
            if isinstance(t, dict):
                t = (t.get("id", t.get("track_id")), t.get("label", ""), t.get("x1"), t.get("y1"),
                     t.get("x2"), t.get("y2"))
            if len(t) == 6 and None not in t[2:]:
                norm.append((t[0], t[1], float(t[2]), float(t[3]), float(t[4]), float(t[5])))
        self.tracks = norm
        if self.drafting:
            self._update_preview()
            self.update()

    def _wanted(self, label: str) -> bool:
        return not self.preview_objects or label in self.preview_objects

    def _update_preview(self) -> None:
        d = self.draft
        text = ""
        now = time.monotonic()
        if d.closed and d.shape == "polygon" and len(d.points) >= 3:
            counts: dict[str, int] = {}
            for _tid, label, *box in self.tracks:
                if self._wanted(label) and point_in_poly(foot_point(box), d.points):
                    counts[label] = counts.get(label, 0) + 1
            if counts:
                text = "Right now: " + ", ".join(f"{n} {obj_word(k, n)}" for k, n in counts.items()) + " inside"
        elif d.closed and d.shape == "line" and len(d.points) == 2:
            a, b = d.points
            for tid, label, *box in self.tracks:
                foot = foot_point(box)
                prev = self._feet.get(tid)
                if prev is not None and self._wanted(label):
                    sign = crossed(a, b, prev, foot)
                    if sign and (d.direction == "both" or (sign > 0) == (d.direction == "a_to_b")):
                        self._crossings.append((now, label, sign))
            self._crossings = [c for c in self._crossings if now - c[0] < 2.0]
            if self._crossings:
                _t, label, sign = self._crossings[-1]
                arrow, words = self.direction_labels()["a_to_b" if sign > 0 else "b_to_a"]
                text = f"Just now: {obj_word(label)} crossed {arrow} {words}"
        self._feet = {t[0]: foot_point(t[2:]) for t in self.tracks}
        if text != self._preview:
            self._preview = text
            self.preview_changed.emit(text)

    def preview_active(self) -> bool:
        return bool(self._preview)

    # --- hit tests -------------------------------------------------------------------------

    def _vertex_at(self, pos: QPointF) -> int | None:
        best, best_d = None, HANDLE_HIT + 0.01
        for i, p in enumerate(self._pts_px(self.draft.points)):
            dd = self._dist(p, pos)
            if dd < best_d:
                best, best_d = i, dd
        return best

    def _midpoints(self) -> list[QPointF]:
        pts = self._pts_px(self.draft.points)
        if self.draft.shape != "polygon" or len(pts) < 3:
            return []
        return [(pts[i] + pts[(i + 1) % len(pts)]) / 2 for i in range(len(pts))]

    def _midpoint_at(self, pos: QPointF) -> int | None:
        for i, m in enumerate(self._midpoints()):
            if self._dist(m, pos) <= 8:
                return i
        return None

    def _edge_at(self, pos: QPointF, tol: float = 6) -> int | None:
        pts = self._pts_px(self.draft.points)
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            if dist_to_segment(self._xy(pos), self._xy(a), self._xy(b)) <= tol:
                return i
        return None

    def _arrow_center(self) -> QPointF | None:
        if self.draft.shape != "line" or len(self.draft.points) != 2:
            return None
        a, b = self._pts_px(self.draft.points)
        return (a + b) / 2

    def _arrow_hit(self, pos: QPointF) -> bool:
        c = self._arrow_center()
        return c is not None and self.draft.closed and self._dist(c, pos) <= ARROW_R + 4

    def _inside_draft(self, pos: QPointF) -> bool:
        pts = self._pts_px(self.draft.points)
        if self.draft.shape == "line" and len(pts) == 2:
            return dist_to_segment(self._xy(pos), self._xy(pts[0]), self._xy(pts[1])) <= 8
        return len(pts) >= 3 and QPolygonF(pts).containsPoint(pos, Qt.OddEvenFill)

    def _near_first(self, pos: QPointF | None) -> bool:
        d = self.draft
        if pos is None or self.mode != "draw" or d.shape != "polygon" or len(d.points) < 3:
            return False
        return self._dist(self.to_px(d.points[0]), pos) <= CLOSE_RADIUS

    # --- snapping --------------------------------------------------------------------------

    def _snapped(self, pos: QPointF, exclude: int | None = None, free: bool = False) -> QPointF:
        """Snap to corners of this camera's other areas and to the picture border (Alt: free)."""
        self._snap = None
        r = self.image_rect()
        x = min(max(pos.x(), r.left()), r.right())
        y = min(max(pos.y(), r.top()), r.bottom())
        p = QPointF(x, y)
        if free:
            return p
        best, best_d = None, SNAP_PX + 0.01
        for a in self._shown_areas():
            for q in self._pts_px(a.get("points", [])):
                dd = self._dist(q, p)
                if dd < best_d:
                    best, best_d = q, dd
        if best is not None:
            self._snap = best
            return QPointF(best)
        snapped = False
        for edge in (r.left(), r.right()):
            if abs(p.x() - edge) <= SNAP_PX:
                p.setX(edge)
                snapped = True
        for edge in (r.top(), r.bottom()):
            if abs(p.y() - edge) <= SNAP_PX:
                p.setY(edge)
                snapped = True
        if snapped:
            self._snap = QPointF(p)
        return p

    # --- mouse -----------------------------------------------------------------------------

    def mousePressEvent(self, event):
        pos = event.position()
        self._mouse = pos
        if not self.drafting:
            if event.button() == Qt.LeftButton:
                aid = self.area_at(pos)
                if aid:
                    self.area_clicked.emit(aid, pos)
                else:
                    self.clicked.emit()
            return
        self.setFocus(Qt.MouseFocusReason)
        if self._img is None:
            return
        free = bool(event.modifiers() & Qt.AltModifier)
        if self.mode == "draw":
            self._press_draw(event.button(), pos, free)
        else:
            self._press_edit(event.button(), pos, free)
        self.update()

    def _press_draw(self, button, pos: QPointF, free: bool) -> None:
        d = self.draft
        if button == Qt.RightButton:
            self.remove_last_point()
            return
        if button != Qt.LeftButton:
            return
        if d.shape == "polygon" and self._near_first(pos):
            self.finish()
            return
        if not self.image_rect().contains(pos):
            return
        self._press = pos
        self._drag_before = d.copy()
        if d.shape == "line":
            if len(d.points) == 1:                    # click-click: this click is B
                end = self.to_norm(self._snapped(pos, free=free))
                if self._dist(self.to_px(end), self.to_px(d.points[0])) < DRAG_START:
                    return
                d.points.append(end)
                self._press = None
                self._complete_line(self._drag_before)
                return
            self._drag = "line_new"
            d.points = [self.to_norm(self._snapped(pos, free=free))]
            self.update()
            return
        if not d.points:
            self._drag = "pending"                    # a drag from here draws a rectangle
            return
        self._add_point(pos, free)

    def _complete_line(self, before: Draft) -> None:
        self.draft.closed = True
        self.mode = "edit"
        self._push(before, "Draw line")
        self.draft_closed.emit()

    def _add_point(self, pos: QPointF, free: bool) -> None:
        d = self.draft
        if len(d.points) >= MAX_CORNERS:
            self._warn("That is the most corners an area can have.")
            return
        p = self._snapped(pos, free=free)
        if d.points and self._dist(self.to_px(d.points[-1]), p) < DEDUPE_PX:
            return  # first click of a double-click, or a twitch
        before = d.copy()
        d.points.append(self.to_norm(p))
        self._push(before, "Add corner")

    def _press_edit(self, button, pos: QPointF, free: bool) -> None:
        d = self.draft
        i = self._vertex_at(pos)
        if button == Qt.RightButton:
            if i is not None:
                self.delete_vertex(i)
            return
        if button != Qt.LeftButton:
            return
        if self._arrow_hit(pos):
            self.cycle_direction()
            return
        self._drag_before = d.copy()
        self._press = pos
        if i is not None:
            self.sel_vertex, self._drag, self._drag_index = i, "vertex", i
        elif (m := self._midpoint_at(pos)) is not None and len(d.points) < MAX_CORNERS:
            d.points.insert(m + 1, self.to_norm(self._snapped(pos, free=free)))
            self.sel_vertex, self._drag, self._drag_index = m + 1, "vertex", m + 1
        elif self._inside_draft(pos):
            self._drag = "move"
            self._drag_origin = [list(p) for p in d.points]
        else:
            self.sel_vertex = None
            self._drag_before = None
            self._press = None
        if self._drag == "vertex":
            self._loupe = pos
        self._update_cursor()

    def mouseMoveEvent(self, event):
        pos = event.position()
        self._mouse = pos
        free = bool(event.modifiers() & Qt.AltModifier)
        self._shift = bool(event.modifiers() & Qt.ShiftModifier)
        d = self.draft
        if not self.drafting:
            hid = self.area_at(pos)
            if hid != self.hover_id:
                self.hover_id = hid
                self.setCursor(Qt.PointingHandCursor if hid else Qt.ArrowCursor)
                self.update()
            return
        if self._drag == "pending" and self._press is not None and self._dist(pos, self._press) > DRAG_START:
            self._drag = "rect"
        if self._drag == "rect":
            a = self.to_norm(self._snapped(self._press, free=True))
            b = self.to_norm(self._snapped(pos, free=free))
            d.points = [a, [b[0], a[1]], b, [a[0], b[1]]]
            self._loupe = pos if self._shift else None
        elif self._drag == "line_new":
            end = self.to_norm(self._snapped(pos, free=free))
            d.points = [d.points[0], end] if self._dist(pos, self._press) > DRAG_START else d.points[:1]
            self._loupe = pos
        elif self._drag == "vertex":
            d.points[self._drag_index] = self.to_norm(self._snapped(pos, self._drag_index, free))
            self._loupe = pos
            if d.shape == "polygon":
                self._bad_edges = crossing_edges(d.points)
            self.draft_changed.emit()
        elif self._drag == "move" and self._drag_origin:
            self._move_shape(pos)
        else:
            self._loupe = pos if (self.mode == "draw" and self._shift) else None
            if self.mode == "draw":
                self._snapped(pos, free=free)   # shows the snap ring before the click
                self._check_rubber(pos)
            else:
                self._snap = None
            self._update_cursor(pos)
        self.update()

    def _check_rubber(self, pos: QPointF) -> None:
        """While clicking corners: do the placed edges, or the one to the cursor, cross another?
        Shown in red at once, not only when the user tries to finish."""
        d = self.draft
        if d.shape != "polygon" or d.closed or len(d.points) < 3:
            bad, rubber = set(), False
        else:
            chain = d.points + [self.to_norm(pos)]
            bad = crossing_edges(chain, closed=False)
            rubber = len(chain) - 2 in bad
            bad.discard(len(chain) - 2)
        if bad != self._bad_edges or rubber != self._bad_rubber:
            self._bad_edges, self._bad_rubber = bad, rubber
            self.draft_changed.emit()   # the hint changes

    def _move_shape(self, pos: QPointF) -> None:
        r = self.image_rect()
        dx = (pos.x() - self._press.x()) / max(r.width(), 1.0)
        dy = (pos.y() - self._press.y()) / max(r.height(), 1.0)
        xs = [p[0] for p in self._drag_origin]
        ys = [p[1] for p in self._drag_origin]
        dx = min(max(dx, -min(xs)), 1 - max(xs))   # the whole shape stays on the picture
        dy = min(max(dy, -min(ys)), 1 - max(ys))
        self.draft.points = [[round(x + dx, 4), round(y + dy, 4)] for x, y in self._drag_origin]

    def mouseReleaseEvent(self, event):
        if not self.drafting or event.button() != Qt.LeftButton:
            return
        d = self.draft
        drag, before = self._drag, self._drag_before
        self._drag, self._drag_before, self._drag_origin = None, None, None
        self._loupe = None
        self._snap = None
        if drag == "pending" and self._press is not None:
            self._add_point(self._press, bool(event.modifiers() & Qt.AltModifier))
        elif drag == "rect":
            if len(d.points) == 4 and poly_area(d.points) >= MIN_AREA:
                d.closed = True
                self.mode = "edit"
                self._push(before, "Draw rectangle")
                self.draft_closed.emit()
            else:
                d.points = []
                self._warn("Drag a bigger rectangle, or click the corners one by one.")
        elif drag == "line_new":
            if len(d.points) == 2:
                self._complete_line(before)
            else:
                self._push(before, "Start line")      # A placed: click B next (trackpads)
        elif drag in ("vertex", "move") and before is not None:
            self._push(before, "Move corner" if drag == "vertex" else "Move area")
        self._press = None
        self._update_cursor(event.position())
        self.update()

    def mouseDoubleClickEvent(self, event):
        if not self.drafting or event.button() != Qt.LeftButton:
            if not self.drafting:
                self.mousePressEvent(event)
            return
        pos = event.position()
        if self.mode == "draw" and self.draft.shape == "polygon":
            self.finish()
        elif self.mode == "edit" and self.draft.shape == "polygon" and self._vertex_at(pos) is None:
            i = self._edge_at(pos)
            if i is not None and len(self.draft.points) < MAX_CORNERS:
                before = self.draft.copy()
                self.draft.points.insert(i + 1, self.to_norm(pos))
                self.sel_vertex = i + 1
                self._push(before, "Add corner")

    def leaveEvent(self, event):
        self._mouse = None
        if not self.drafting and self.hover_id:
            self.hover_id = None
        self.update()
        super().leaveEvent(event)

    def delete_vertex(self, i: int) -> bool:
        d = self.draft
        if d.shape != "polygon" or len(d.points) <= 3 or not 0 <= i < len(d.points):
            if d.shape == "polygon" and len(d.points) <= 3:
                self._warn("An area needs at least 3 corners.")
            return False
        before = d.copy()
        d.points.pop(i)
        self.sel_vertex = None
        self._push(before, "Remove corner")
        return True

    def nudge(self, dx: int, dy: int) -> None:
        i = self.sel_vertex
        if i is None or not 0 <= i < len(self.draft.points):
            return
        before = self.draft.copy()
        p = self.to_px(self.draft.points[i]) + QPointF(dx, dy)
        self.draft.points[i] = self.to_norm(p)
        self._push(before, "Nudge corner")

    def _update_cursor(self, pos: QPointF | None = None) -> None:
        if not self.drafting:
            self.setCursor(Qt.ArrowCursor)
            return
        if self.mode == "draw":
            near = self._near_first(pos)
            self.setCursor(Qt.PointingHandCursor if near else Qt.CrossCursor)
            return
        if self._drag in ("vertex", "move"):
            self.setCursor(Qt.ClosedHandCursor if self._drag == "vertex" else Qt.SizeAllCursor)
        elif pos is not None and (self._vertex_at(pos) is not None or self._arrow_hit(pos)
                                  or self._midpoint_at(pos) is not None):
            self.setCursor(Qt.PointingHandCursor)
        elif pos is not None and self._inside_draft(pos):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    # --- keyboard --------------------------------------------------------------------------

    KEYS = {Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Backspace, Qt.Key_Delete, Qt.Key_F, Qt.Key_L,
            Qt.Key_P, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Z, Qt.Key_Y, Qt.Key_Tab,
            Qt.Key_Backtab}

    def event(self, event):
        if event.type() == QEvent.ShortcutOverride and self.drafting and event.key() in self.KEYS:
            event.accept()  # our keys win over window shortcuts (Esc leaves full screen, S...)
            return True
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Tab, Qt.Key_Backtab) and self.drafting:
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if not self.drafting:
            super().keyPressEvent(event)
            return
        if not self.handle_key(event.key(), event.modifiers()):
            super().keyPressEvent(event)

    def handle_key(self, key, mods) -> bool:
        """Drawing keys; also called by the side panel so they work wherever the focus is."""
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        d = self.draft
        if key == Qt.Key_Escape:
            self.escape_pressed.emit()
        elif ctrl and key == Qt.Key_Z:
            self.redo() if shift else self.undo()
        elif ctrl and key == Qt.Key_Y:
            self.redo()
        elif ctrl:
            return False
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            if self.mode == "draw":
                self.finish()
            else:
                self.enter_pressed.emit()
        elif key == Qt.Key_Backspace:
            if self.mode == "draw":
                self.remove_last_point()
            elif self.sel_vertex is not None:
                self.delete_vertex(self.sel_vertex)
        elif key == Qt.Key_Delete:
            if self.mode == "edit" and self.sel_vertex is not None:
                self.delete_vertex(self.sel_vertex)
        elif key == Qt.Key_F:
            self.set_frozen(not self.frozen)
        elif key in (Qt.Key_L, Qt.Key_P) and self.mode == "draw" and not d.points:
            self.set_shape("line" if key == Qt.Key_L else "polygon")
        elif key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) and self.mode == "edit":
            step = 10 if shift else 1
            dx = {Qt.Key_Left: -step, Qt.Key_Right: step}.get(key, 0)
            dy = {Qt.Key_Up: -step, Qt.Key_Down: step}.get(key, 0)
            if self.sel_vertex is None and d.points:
                self.sel_vertex = 0
            self.nudge(dx, dy)
        elif key in (Qt.Key_Tab, Qt.Key_Backtab) and self.mode == "edit" and d.points:
            back = key == Qt.Key_Backtab or shift
            cur = -1 if self.sel_vertex is None else self.sel_vertex
            self.sel_vertex = (cur + (-1 if back else 1)) % len(d.points)
            self.update()
        else:
            return False
        return True

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Shift and self._loupe is not None and self._drag is None:
            self._loupe = None
            self.update()
        super().keyReleaseEvent(event)

    # --- painting --------------------------------------------------------------------------

    def _palette(self) -> dict:
        return getattr(self.ctx, "palette", None) or {"bg": "#f5f6f8", "video_bg": "#0b0d10"}

    def _scaled(self, r: QRectF) -> QPixmap | None:
        if self._img is None:
            return None
        dpr = self.devicePixelRatioF()
        key = (id(self._img), int(r.width() * dpr), int(r.height() * dpr))
        if self._pm is None or self._pm_key != key:
            self._pm = QPixmap.fromImage(self._img).scaled(key[1], key[2], Qt.IgnoreAspectRatio,
                                                           Qt.SmoothTransformation)
            self._pm.setDevicePixelRatio(dpr)
            self._pm_key = key
        return self._pm

    def paintEvent(self, event):
        pal = self._palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(pal.get("bg", "#f5f6f8")))
        frame = QPainterPath()
        frame.addRoundedRect(QRectF(self.rect()), 8, 8)
        p.fillPath(frame, QColor(pal.get("video_bg", "#0b0d10")))
        if self._img is None:
            p.setPen(QColor("#9aa3b2"))
            p.drawText(self.rect().adjusted(24, 24, -24, -24), Qt.AlignCenter | Qt.TextWordWrap, self._text)
            p.end()
            return
        r = self.image_rect()
        pm = self._scaled(r)
        if pm is not None:
            p.drawPixmap(r.topLeft(), pm)
        if self.show_areas or self.drafting:
            self._paint_areas(p)
        if self.drafting:
            self._paint_feet(p)
            self._paint_draft(p)
            self._paint_hint(p, r)
        if self.frozen or self.offline_note:
            self._paint_chip(p, r, self.offline_note or "Frozen")
        if self._loupe is not None:
            self._paint_loupe(p, r, self._loupe)
        p.end()

    def _paint_areas(self, p: QPainter) -> None:
        shown = self._shown_areas() if (self.show_areas or self.drafting) else []
        now = time.monotonic()
        order = sorted(shown, key=lambda a: (a.get("id") == self.hover_id, a.get("id") == self.selected_id))
        for a in order:
            self._paint_area(p, a, now)
        if self.drafting:
            return  # context only while drawing: no tags
        small = self.image_rect().width() < 480
        hot = {self.hover_id, self.selected_id} - {None}
        # Laid out in config order, so tags do not jump when an area is hovered or selected; a tag
        # with no free place is hidden (the hovered / selected one is then drawn on top).
        tags = self._layout_tags(shown, small)
        for a, spec, box in tags:
            if box is not None and a.get("id") not in hot:
                self._paint_tag(p, a, spec, box)
        for a, spec, box in tags:
            if a.get("id") in hot:
                self._paint_tag(p, a, spec, box if box is not None else self._tag_spots(spec)[0])

    def _paint_area(self, p: QPainter, a: dict, now: float) -> None:
        pts = self._pts_px(a.get("points", []))
        color = area_color(a)
        enabled = a.get("enabled", True)
        aid = a.get("id")
        live = self.live.get(aid) if self.watching else None
        hot = aid in (self.hover_id, self.selected_id)
        width = 3 if hot else 2
        alpha = 95 if hot else (85 if is_active(live) else 45)
        if aid in self._pulse:
            width += 3 * max(0.0, 1 - (now - self._pulse[aid]) / 0.6)
        if self.drafting:
            alpha, width = 25, 1.5
            color.setAlpha(150)
        pen = QPen(color, width)
        if not enabled:
            pen = QPen(QColor(170, 175, 185, 200), 1.5, Qt.DashLine)
        if a.get("shape") == "line" and len(pts) == 2:
            pen.setWidthF(pen.widthF() + 1)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(pts[0], pts[1])
            if enabled and not self.drafting:
                self._paint_arrow(p, pts[0], pts[1], a.get("direction", "both"), color, hot)
            return
        if len(pts) < 3:
            return
        fill = QColor(color)
        fill.setAlpha(0 if not enabled else alpha)
        p.setPen(pen)
        p.setBrush(fill)
        p.drawPolygon(QPolygonF(pts))
        p.setBrush(Qt.NoBrush)

    def _paint_arrow(self, p: QPainter, a: QPointF, b: QPointF, direction: str, color: QColor,
                     hot: bool = False) -> None:
        """The crossing direction: a round badge on the line's middle with chevrons across the line."""
        c = (a + b) / 2
        nx, ny = crossing_normal(self._xy(a), self._xy(b))
        n = QPointF(nx, ny)
        t = QPointF(-ny, nx)   # along the line
        r = ARROW_R + (2 if hot else 0)
        p.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
        p.setBrush(color)
        p.drawEllipse(c, r, r)
        pen = QPen(TAG_TEXT, 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        heads = {"a_to_b": (1,), "b_to_a": (-1,)}.get(direction, (1, -1))
        shaft = 6.5
        p.drawLine(c - n * shaft, c + n * shaft)
        for s in heads:
            tip = c + n * (shaft * s)
            back = tip - n * (4 * s)
            p.drawLine(tip, back + t * 4)
            p.drawLine(tip, back - t * 4)

    def _tag_font(self, size: float, bold: bool) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(size)
        f.setWeight(QFont.DemiBold if bold else QFont.Normal)
        return f

    def _tag_spec(self, a: dict, small: bool) -> dict | None:
        """What a tag shows and how big it is: name (+ bell), and the live stat line."""
        pts = self._pts_px(a.get("points", []))
        if not pts:
            return None
        enabled = a.get("enabled", True)
        stat = "" if small else stat_text(a, self.live.get(a.get("id")), self.watching)
        if not enabled:
            stat = "Off"
        f1, f2 = self._tag_font(9, True), self._tag_font(8.2, False)
        fm1, fm2 = QFontMetrics(f1), QFontMetrics(f2)
        bell = bool(a.get("alert") and enabled)
        extra = 14 if bell else 0
        name_t = fm1.elidedText(a.get("name") or "Area", Qt.ElideRight, TAG_MAX_W - 14 - extra)
        stat_t = fm2.elidedText(stat, Qt.ElideRight, TAG_MAX_W - 14) if stat else ""
        w = max(fm1.horizontalAdvance(name_t) + extra, fm2.horizontalAdvance(stat_t) if stat_t else 0) + 14
        return {"pts": pts, "line": a.get("shape") == "line", "name": name_t, "stat": stat_t, "bell": bell,
                "extra": extra, "w": w, "h1": fm1.height(), "h2": fm2.height() if stat_t else 0,
                "f1": f1, "f2": f2}

    def _tag_spots(self, spec: dict, with_stat: bool = True) -> list[QRectF]:
        """Places to try, best first: on the shape's top-left corner (lines: just above it), a few
        steps down, above the shape, right of it, left of it. All kept on the picture."""
        pts, w, h1 = spec["pts"], spec["w"], spec["h1"]
        h = h1 + (spec["h2"] if with_stat else 0) + 6
        r = self.image_rect()
        xs, ys = [q.x() for q in pts], [q.y() for q in pts]
        min_y, span = min(ys), max(ys) - min(ys)
        # the left-most of the top corners, so the tag sits on the shape rather than beside it
        top = min((q for q in pts if q.y() <= min_y + span * 0.25 + 4), key=lambda q: q.x())
        x0 = top.x() - 6
        y0 = top.y() - h - 4 if spec["line"] else top.y() + 4
        spots = [(x0, y0 + i * (h + 3)) for i in range(4)]
        spots += [(x0, min_y - h - 4), (max(xs) + 6, min_y), (min(xs) - w - 6, min_y),
                  (max(xs) + 6, max(ys) - h), (x0, max(ys) + 4)]
        out = []
        for x, y in spots:
            x = min(max(r.left() + 2, x), r.right() - w - 2)
            y = min(max(r.top() + 2, y), r.bottom() - h - 2)
            out.append(QRectF(x, y, w, h))
        return out

    def _layout_tags(self, shown: list[dict], small: bool) -> list[tuple[dict, dict, QRectF | None]]:
        """Tags never cover each other: each takes the first free spot (without its stat line if
        needed); a tag with no free spot is hidden (None)."""
        placed: list[QRectF] = []
        out = []
        for a in shown:
            spec = self._tag_spec(a, small)
            if spec is None:
                continue
            box = None
            for with_stat in ((True, False) if spec["stat"] else (True,)):
                box = next((b for b in self._tag_spots(spec, with_stat)
                            if not any(b.adjusted(-2, -2, 2, 2).intersects(o) for o in placed)), None)
                if box is not None:
                    if not with_stat:
                        spec = {**spec, "stat": "", "h2": 0}
                    break
            if box is not None:
                placed.append(box)
            out.append((a, spec, box))
        return out

    def _paint_tag(self, p: QPainter, a: dict, spec: dict, box: QRectF) -> None:
        enabled = a.get("enabled", True)
        name_t, stat_t, bell, extra = spec["name"], spec["stat"], spec["bell"], spec["extra"]
        w, h1, h2, f1, f2 = spec["w"], spec["h1"], spec["h2"], spec["f1"], spec["f2"]
        if box.height() < h1 + h2 + 6:   # a spot that only fits the name
            stat_t = ""
        color = area_color(a) if enabled else QColor(170, 175, 185)
        if a.get("id") in (self.hover_id, self.selected_id):
            color = color.lighter(112)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(box, 5, 5)
        p.setFont(f1)
        p.setPen(TAG_TEXT)
        p.drawText(QRectF(box.x() + 7, box.y() + 3, w - 14 - extra, h1), Qt.AlignLeft | Qt.AlignVCenter, name_t)
        if bell:
            self._paint_bell(p, QPointF(box.right() - 12, box.y() + 3 + h1 / 2))
        if stat_t:
            p.setFont(f2)
            p.setPen(QColor(20, 20, 20, 190))
            p.drawText(QRectF(box.x() + 7, box.y() + 3 + h1, w - 14, h2), Qt.AlignLeft | Qt.AlignVCenter, stat_t)

    @staticmethod
    def _paint_bell(p: QPainter, c: QPointF) -> None:
        path = QPainterPath()
        path.moveTo(c.x() - 4, c.y() + 2.5)
        path.cubicTo(c.x() - 3.5, c.y() - 5, c.x() + 3.5, c.y() - 5, c.x() + 4, c.y() + 2.5)
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(TAG_TEXT)
        p.drawPath(path)
        p.drawEllipse(QPointF(c.x(), c.y() + 4), 1.3, 1.3)

    def _paint_feet(self, p: QPainter) -> None:
        if not self.tracks:
            return
        p.setBrush(QColor(255, 255, 255, 220))
        p.setPen(QPen(QColor(255, 255, 255, 128), 3))
        for _tid, _label, *box in self.tracks:
            p.drawEllipse(self.to_px(foot_point(box)), 2.5, 2.5)

    def _paint_draft(self, p: QPainter) -> None:
        d = self.draft
        color = QColor(ZONE_COLORS[self.draft_color % len(ZONE_COLORS)])
        pts = self._pts_px(d.points)
        if not pts:
            if self._snap is not None:
                self._paint_snap(p, self._snap)
            return
        danger = QColor(self._palette().get("danger", "#ef4444"))
        hot = self.preview_active()
        width = 4 if hot else 2.5
        if d.shape == "line":
            pen = QPen(color, width + 0.5)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            if len(pts) == 2:
                p.drawLine(pts[0], pts[1])
                if d.closed:
                    hover = self._mouse is not None and self._arrow_hit(self._mouse)
                    self._paint_arrow(p, pts[0], pts[1], d.direction, color, hover)
            elif self._mouse is not None and self.mode == "draw":
                p.setPen(QPen(color, 2, Qt.DashLine))
                p.drawLine(pts[0], self._mouse)
            for i, pt in enumerate(pts):
                self._paint_handle(p, pt, color, i == self.sel_vertex or (self._drag == "vertex" and
                                                                          i == self._drag_index), 6)
                self._paint_endpoint_label(p, pt, "AB"[i], color, pts)
        else:
            closing = self._near_first(self._mouse)
            preview = list(pts)
            if self.mode == "draw" and self._mouse is not None and not closing and self._drag != "rect":
                preview.append(self._mouse)
            if len(preview) >= 3:
                fill = QColor(color)
                fill.setAlpha(70 if hot else 45)
                path = QPainterPath()
                path.addPolygon(QPolygonF(preview))
                path.closeSubpath()
                p.fillPath(path, fill)
            n = len(pts)
            edges = range(n if d.closed else n - 1)
            for i in edges:
                bad = i in self._bad_edges
                p.setPen(QPen(danger if bad else color, width + (0.5 if bad else 0)))
                p.drawLine(pts[i], pts[(i + 1) % n])
            if self.mode == "draw" and self._mouse is not None and self._drag != "rect":
                p.setPen(QPen(danger if self._bad_rubber and not closing else color, 2, Qt.DashLine))
                p.drawLine(pts[-1], pts[0] if closing else self._mouse)
            if self.mode == "edit":
                p.setPen(Qt.NoPen)
                ghost = QColor(255, 255, 255, 150)
                for m in self._midpoints():
                    p.setBrush(ghost)
                    p.drawEllipse(m, 3.5, 3.5)
            for i, pt in enumerate(pts):
                if self.mode == "draw" and i == 0:
                    r = 8 if closing else 6
                    p.setPen(QPen(color, 2))
                    p.setBrush(color if closing else QColor("white"))
                    p.drawEllipse(pt, r, r)
                    if closing:
                        p.setPen(QPen(QColor("white"), 1.5))
                        p.setBrush(Qt.NoBrush)
                        p.drawEllipse(pt, 12, 12)
                        self._paint_pill(p, "Click to finish", pt + QPointF(0, -28))
                    continue
                active = i == self.sel_vertex or (self._drag == "vertex" and i == self._drag_index)
                self._paint_handle(p, pt, color, active, 5)
        if self._snap is not None:
            self._paint_snap(p, self._snap)

    def _paint_handle(self, p: QPainter, pt: QPointF, color: QColor, active: bool, base: float) -> None:
        hover = self._mouse is not None and self.mode == "edit" and self._dist(pt, self._mouse) <= HANDLE_HIT
        r = base + (2 if active or hover else 0)
        p.setPen(QPen(color, 2))
        p.setBrush(QColor("white"))
        p.drawEllipse(pt, r, r)
        if active:
            p.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(pt, r + 4, r + 4)

    def _paint_endpoint_label(self, p: QPainter, pt: QPointF, text: str, color: QColor, pts) -> None:
        if len(pts) == 2:
            other = pts[1] if text == "A" else pts[0]
            v = pt - other
            n = math.hypot(v.x(), v.y()) or 1.0
            at = pt + v / n * 18
        else:
            at = pt + QPointF(0, -18)
        f = self._tag_font(8, True)
        p.setFont(f)
        box = QRectF(at.x() - 9, at.y() - 9, 18, 18)
        p.setPen(Qt.NoPen)
        p.setBrush(HINT_BG)
        p.drawRoundedRect(box, 9, 9)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignCenter, text)

    @staticmethod
    def _paint_snap(p: QPainter, pt: QPointF) -> None:
        p.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(pt, 9, 9)

    def _paint_pill(self, p: QPainter, text: str, center: QPointF, max_w: float = 0) -> QRectF:
        f = self._tag_font(9, True)
        p.setFont(f)
        fm = QFontMetrics(f)
        if max_w:
            text = fm.elidedText(text, Qt.ElideRight, int(max_w - 24))
        w, h = fm.horizontalAdvance(text) + 24, fm.height() + 10
        box = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(HINT_BG)
        p.drawRoundedRect(box, h / 2, h / 2)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignCenter, text)
        return box

    def _paint_hint(self, p: QPainter, r: QRectF) -> None:
        text = self.hint_text()
        if not text:
            return
        f = self._tag_font(9, True)
        h = QFontMetrics(f).height() + 10
        below = self.height() - r.bottom() >= h + 16   # letterbox space: keep the picture uncovered
        y = r.bottom() + 8 + h / 2 if below else r.bottom() - 14 - h / 2
        self._paint_pill(p, text, QPointF(r.center().x(), y), max(r.width(), self.width() * 0.9) - 24)

    def _paint_chip(self, p: QPainter, r: QRectF, text: str) -> None:
        f = self._tag_font(8.5, True)
        p.setFont(f)
        fm = QFontMetrics(f)
        text = fm.elidedText(text, Qt.ElideRight, int(r.width() / 2))
        w, h = fm.horizontalAdvance(text) + 26, fm.height() + 8
        top = 60 if self.drafting and r.top() < BAR_OUTSIDE else 12   # below the floating drawing bar
        box = QRectF(r.left() + 12, r.top() + top, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(HINT_BG)
        p.drawRoundedRect(box, h / 2, h / 2)
        p.setBrush(QColor("#60a5fa"))
        p.drawEllipse(QPointF(box.left() + 11, box.center().y()), 3.5, 3.5)
        p.setPen(QColor("white"))
        p.drawText(box.adjusted(18, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, text)

    def _paint_loupe(self, p: QPainter, r: QRectF, at: QPointF) -> None:
        """Round 3x magnifier from the source picture, above-left of the cursor (flips to stay inside)."""
        if self._img is None or r.width() < 1:
            return
        size = LOUPE
        x, y = at.x() - 40 - size, at.y() - 40 - size
        if x < r.left():
            x = at.x() + 40
        if y < r.top():
            y = at.y() + 40
        target = QRectF(x, y, size, size)
        scale = self._img.width() / r.width()
        span = size / LOUPE_ZOOM * scale
        sx = (at.x() - r.left()) * scale - span / 2
        sy = (at.y() - r.top()) * scale - span / 2
        clip = QPainterPath()
        clip.addEllipse(target)
        p.save()
        p.setClipPath(clip)
        p.fillRect(target, QColor(0, 0, 0))
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(target, self._img, QRectF(sx, sy, span, span))
        c = target.center()
        pts = self._pts_px(self.draft.points)
        if pts:  # the shape's edges, magnified with the picture
            p.save()
            p.translate(c)
            p.scale(LOUPE_ZOOM, LOUPE_ZOOM)
            p.translate(-at)
            color = QColor(ZONE_COLORS[self.draft_color % len(ZONE_COLORS)])
            p.setPen(QPen(color, 1.2 / LOUPE_ZOOM * 2))
            p.setBrush(Qt.NoBrush)
            if self.draft.shape == "line" or not self.draft.closed:
                for a, b in zip(pts, pts[1:]):
                    p.drawLine(a, b)
            else:
                p.drawPolygon(QPolygonF(pts))
            p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 220), 1))
        p.drawLine(QPointF(c.x() - 12, c.y()), QPointF(c.x() + 12, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - 12), QPointF(c.x(), c.y() + 12))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 230), 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(target)
