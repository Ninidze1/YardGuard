"""Areas: draw an area or a line on any camera in Live view and choose what YardGuard does there
(count, log number plates, watch a parking spot, ask the AI...), plus the Areas hub page.

LiveAreas   controller the Live page hosts: the drawing canvas (areas_canvas.AreaCanvas, a drop-in
            for the video ImageView), the floating drawing bar, the "What should happen here?"
            side panel and the popover of an area clicked on the video.
AreasPage   nav page: every area of the site, and per area its log, chart or timeline.

Areas live in config.Camera.areas (dicts, see areas_canvas); their events come from the engine's
AlertStore (list_area_events / export_area_events_csv / delete_area_events) and the live state from
Monitor.area_live(channel). Like alerts_ui, this module only reads attributes of those objects.
"""
from __future__ import annotations

import copy
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QDate, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, QStandardPaths, Qt, QTime, QTimer, Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QFileDialog, QFrame, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QStyle, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QTimeEdit, QToolTip, QVBoxLayout, QWidget,
)

from .alerts_ui import Switch, _tone_colors, load_image, rel_time, show_in_explorer
from .areas_canvas import (
    BAR_OUTSIDE, GROUPS, KIND_NAMES, OBJECTS, TASK_KINDS, TEMPLATE_UI, AreaCanvas, Draft, _extra, area_color,
    event_direction, event_duration, event_text, fits_shape, is_active, obj_word, plate_stay, poly_area,
    short_duration, shape_badge, stat_text, template, template_params,
)
from .clip_player import ClipPlayer
from .config import ROOT, ZONE_COLORS
from .incidents_ui import GLOW_S, Segmented, clock_text, day_label, duration_text
from .qt_util import run_async, set_prop
from .widgets import EmptyState, ImageView, PageHeader, button, label, muted, shortcut, spin_box

CACHE = ROOT / ".cache" / "areas"
PANEL_W = 340
LOAD_LIMIT = 500
LOG_ROWS = 10             # log rows shown before the table scrolls
STATS_LIMIT = 20000
UNSURE = 0.6              # plate reads below this confidence are "Unsure read"
MANY_AREAS = 12
THUMB = QSize(120, 68)
ROW_H = 88
HEAD_H = 36
ID_ROLE = Qt.UserRole + 1
KIND_ROLE = Qt.UserRole + 2

EXTRA_QSS = """
#AreaPanel { background: $surface; border: 1px solid $border; border-radius: 10px; }
#AreaPanelBody { background: transparent; }
QPushButton#Step { background: transparent; border: none; border-radius: 12px; padding: 4px 10px; min-height: 18px;
    color: $muted; font-weight: 500; }
QPushButton#Step[state="current"] { background: $nav_active_bg; color: $nav_active_text; font-weight: 600; }
QPushButton#Step[state="done"] { color: $success; }
QPushButton#Step[state="done"]:hover { background: $surface2; }
#TemplateCard { background: $surface; border: 1px solid $border; border-radius: 8px; }
#TemplateCard:hover { border-color: $border_strong; background: $surface2; }
#TemplateCard:focus { border-color: $primary; }
#TemplateCard[selected="true"] { border: 2px solid $primary; }
#TemplateName { font-weight: 600; }
#IconTile { background: $surface2; border-radius: 8px; }
#Chip[tone="warning"] { color: $warning; }
#NeedsShape { background: $surface2; border-radius: 8px; }
#DrawBar { background: rgba(15, 17, 21, 0.84); border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 10px; }
#DrawBar QPushButton { background: transparent; border: 1px solid transparent; color: #e6e8ec; padding: 4px 10px;
    border-radius: 6px; min-height: 20px; font-weight: 500; }
#DrawBar QPushButton:hover { background: rgba(255, 255, 255, 0.12); }
#DrawBar QPushButton:checked { background: rgba(255, 255, 255, 0.22); color: #ffffff; font-weight: 600; }
#DrawBar QPushButton:disabled { color: rgba(230, 232, 236, 0.38); }
#DrawBar QPushButton:focus { border-color: rgba(255, 255, 255, 0.45); }
#DrawBarSep { background: rgba(255, 255, 255, 0.18); min-width: 1px; max-width: 1px; min-height: 18px; }
#AreaPopover { background: $surface; border: 1px solid $border_strong; border-radius: 10px; }
#PopEvent { background: transparent; border: 1px solid transparent; border-radius: 6px; }
#PopEvent:hover { background: $surface2; }
#PopEvent:focus { border-color: $primary; }
#Tile { background: $surface; border: 1px solid $border; border-radius: 10px; }
#TileValue { font-size: 15pt; font-weight: 600; }
#TileCaption { color: $muted; font-size: 8.5pt; }
QPushButton#Swatch { border-radius: 12px; min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;
    padding: 0; border: 2px solid transparent; }
QPushButton#Swatch:checked { border: 2px solid $text; }
QPushButton#Swatch:focus { border: 2px solid $primary; }
#WarnText { color: $warning; }
#OkText { color: $success; }
#ErrText { color: $danger; }
#PlateText { font-family: "Cascadia Mono", Consolas; font-weight: 700; }
QListWidget#AreaList { background: transparent; border: none; padding: 0 6px 0 0; }
#AreaList::item { padding: 0; margin: 0; border: none; background: transparent; }
QTableWidget#AreaLog { border: none; background: transparent; }
#AreaLog::item { border-bottom: 1px solid $border; padding: 0 6px; }
#AreaLog::item:selected { background: $nav_active_bg; color: $text; }
QDateEdit { background: $surface; border: 1px solid $border_strong; border-radius: 6px; padding: 5px 8px; min-height: 22px; }
QDateEdit:focus { border: 1px solid $primary; }
#Choice { background: $surface2; border: 1px solid $border; border-radius: 8px; }
QPushButton#ChoiceButton { background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 5px 10px;
    min-height: 18px; color: $muted; font-weight: 600; text-align: left; }
QPushButton#ChoiceButton:hover { color: $text; }
QPushButton#ChoiceButton:checked { background: $surface; border-color: $border_strong; color: $text; }
QPushButton#ChoiceButton:focus { border-color: $primary; }
QPushButton#ChoiceButton:disabled { color: $border_strong; font-weight: 400; }
QPushButton#ChoiceButton:checked:disabled { background: transparent; border-color: $border; color: $muted; }
#PlateChip { background: $surface2; border: 1px solid $border; border-radius: 12px; }
#PlateChip QPushButton { background: transparent; border: none; padding: 0; min-height: 14px; }
"""

NAME_BASE = {
    "watch_zone": "Watch zone", "loitering": "Lingering spot", "presence": "Entrance", "line_cross": "Crossing line",
    "log_plates": "Barrier", "count": "Gate line", "parking_spot": "Parking spot", "no_parking": "Keep clear",
    "left_object": "Front door", "state_watch": "Gate", "ask_ai": "AI check",
}
SENTENCES = {  # task -> (sentence, unit, param, factor shown -> stored, low, high)
    "loitering": ("Tell me when someone stays longer than", "seconds", "seconds", 1, 5, 3600),
    "no_parking": ("Tell me when a vehicle stays longer than", "minutes", "max_minutes", 1, 1, 240),
    "parking_spot": ("Count the spot as taken after", "seconds", "occupied_after_s", 1, 5, 600),
    "left_object": ("Tell me when something new stays longer than", "seconds (also when something is taken away)",
                    "seconds", 1, 10, 3600),
    "state_watch": ("Tell me when it looks different for longer than", "minutes", "seconds", 60, 1, 240),
}
ASK_EXAMPLES = ("the gate is open", "the barrier arm is raised", "a car is parked here", "the garage door is open")


def confirm(parent: QWidget, title: str, text: str, yes: str, no: str, danger: bool = False) -> bool:
    """Two-button question; tests replace this function."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    ok = box.addButton(yes, QMessageBox.AcceptRole)
    box.addButton(no, QMessageBox.RejectRole)
    box.setDefaultButton(ok)
    box.exec()
    return box.clickedButton() is ok


def new_area_id() -> str:
    return uuid.uuid4().hex[:8]


def cam_areas(cam) -> list[dict]:
    return [a for a in (getattr(cam, "areas", None) or []) if isinstance(a, dict)]


def default_name(task: str, shape: str, taken: list[str]) -> str:
    base = NAME_BASE.get(task, "Area")
    if task == "count" and shape == "polygon":
        base = "Counting area"
    if task == "line_cross" and shape == "polygon":
        base = "Fence"
    numbered = task in ("parking_spot", "watch_zone")
    names = {n.lower() for n in taken}
    if not numbered and base.lower() not in names:
        return base
    i = 1 if numbered else 2
    while f"{base} {i}".lower() in names:
        i += 1
    return f"{base} {i}"


def next_color(areas: list[dict]) -> int:
    used = [int(a.get("color", 0) or 0) % len(ZONE_COLORS) for a in areas]
    for i in range(len(ZONE_COLORS)):
        if i not in used:
            return i
    return len(areas) % len(ZONE_COLORS)


def recommended_template(draft: Draft) -> str | None:
    if draft.shape == "line":
        return "count"
    if len(draft.points) == 4 and poly_area(draft.points) < 0.04:
        return "parking_spot"
    return None


def normalise_plate(text: str) -> str:
    """'aa 123 bb' -> 'AA-123-BB' (upper case, groups joined by hyphens)."""
    parts = [p for p in "".join(c if c.isalnum() else " " for c in text.upper()).split() if p]
    if len(parts) == 1 and len(parts[0]) == 7 and parts[0][:2].isalpha() and parts[0][2:5].isdigit() \
            and parts[0][5:].isalpha():
        s = parts[0]
        parts = [s[:2], s[2:5], s[5:]]
    return "-".join(parts)


def schedule_text(schedule: dict | None) -> str:
    s = schedule or {}
    mode = s.get("mode", "always")
    if mode == "night":
        return "At night"
    if mode == "custom":
        return f"{s.get('start', '22:00')}–{s.get('end', '06:00')}"
    return "Always"


# --- Stats (pure, unit-tested) -----------------------------------------------------------------

def day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts)
    return datetime(d.year, d.month, d.day).timestamp()


def _count_of(e) -> int:
    if getattr(e, "kind", "") == "count":
        try:
            return max(1, int(float(getattr(e, "value", "") or 1)))
        except ValueError:
            return 1
    return 1


def bucket_counts(events, start: float, n: int, step: str = "hour", split: bool = True) -> list[tuple[int, int]]:
    """Per hour (n hours from start) or per day (n days): (in, out). split=False puts all in 'in'."""
    out = [[0, 0] for _ in range(n)]
    start_day = date.fromtimestamp(start)
    for e in events:
        if step == "hour":
            i = int((e.ts - start) // 3600)
        else:
            i = (date.fromtimestamp(e.ts) - start_day).days
        if 0 <= i < n:
            side = 1 if split and event_direction(e) == "out" else 0
            out[i][side] += _count_of(e)
    return [tuple(x) for x in out]


def busiest(buckets: list[tuple[int, int]]) -> int:
    totals = [a + b for a, b in buckets]
    return max(range(len(totals)), key=lambda i: totals[i]) if totals and max(totals) > 0 else -1


def state_segments(events, on_kinds: tuple, off_kinds: tuple, start: float, end: float,
                   now: float | None = None) -> list[tuple[float, float, object]]:
    """Periods [start, end) the area was 'on' (taken, yes): (from, to, the event that started it).
    events in any order; the state before `start` follows the last event before it."""
    now = time.time() if now is None else now
    end = min(end, now)
    segs, cur, cur_e = [], None, None
    for e in sorted(events, key=lambda x: x.ts):
        if e.kind in on_kinds and cur is None:
            cur, cur_e = e.ts, e
        elif e.kind in off_kinds and cur is not None:
            segs.append((cur, e.ts, cur_e))
            cur = None
    if cur is not None:
        segs.append((cur, max(cur, end), cur_e))
    clipped = []
    for s, t, e in segs:
        s2, t2 = max(s, start), min(t, end)
        if t2 > s2:
            clipped.append((s2, t2, e))
    return clipped


def plate_summary(events, known: list[str], now: float | None = None) -> dict:
    """Vehicles, inside now, visitors (plates not in known), unsure reads."""
    known_set = {normalise_plate(k) for k in known or []}
    plates = [e for e in events if e.kind == "plate"]
    last: dict[str, str] = {}
    for e in sorted(plates, key=lambda x: x.ts):
        if e.value:
            last[normalise_plate(e.value)] = event_direction(e)
    sure = [e for e in plates if (e.confidence or 0) >= UNSURE and e.value and not _extra(e).get("known")]
    return {
        "vehicles": len(plates),
        "inside": sum(1 for d in last.values() if d == "in"),
        "visitors": len({normalise_plate(e.value) for e in sure} - known_set),
        "unsure": sum(1 for e in plates if (e.confidence or 0) < UNSURE or not e.value),
    }


def kinds_for(area: dict) -> tuple:
    """Event kinds an area can log (the hub's kind filter)."""
    kinds = TASK_KINDS.get(area.get("task", ""), ())
    if area.get("task") == "count":
        line = area.get("shape") == "line"
        kinds = tuple(k for k in kinds if (k in ("cross_in", "cross_out")) == line)
    return kinds


def who_badge(e, known: list[str]) -> tuple[str, str]:
    if (e.confidence or 0) < UNSURE or not e.value:
        return "Unsure read", "muted"
    # the engine matches known plates with one character of tolerance (extra["known"])
    if _extra(e).get("known") or normalise_plate(e.value) in {normalise_plate(k) for k in known or []}:
        return "Resident", "primary"
    return "Visitor", "neutral"


# --- Small widgets -----------------------------------------------------------------------------

class Choice(QFrame):
    """Exclusive options stacked vertically (labels too long for a Segmented row)."""

    changed = Signal(int)

    def __init__(self, items: list[str]):
        super().__init__()
        self.setObjectName("Choice")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        for i, text in enumerate(items):
            b = QPushButton(text)
            b.setObjectName("ChoiceButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(i == 0)
            self.group.addButton(b, i)
            lay.addWidget(b)
        self.group.idClicked.connect(self.changed.emit)

    def current(self) -> int:
        return self.group.checkedId()

    def set_current(self, i: int) -> None:
        b = self.group.button(i)
        if b:
            b.setChecked(True)

    def set_text(self, i: int, text: str) -> None:
        b = self.group.button(i)
        if b and b.text() != text:
            b.setText(text)


class _Tile(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(0)
        self.value = label("", "TileValue")
        self.caption = label("", "TileCaption")
        self.caption.setWordWrap(True)
        lay.addWidget(self.value)
        lay.addWidget(self.caption)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set(self, value: str, caption: str) -> None:
        self.value.setText(value)
        self.caption.setText(caption)


def _hline() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    return line


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    widget.setObjectName("AreaPanelBody")
    area.setWidget(widget)
    area.viewport().setAutoFillBackground(False)
    return area


def _two_lines(fm: QFontMetrics, text: str, width: int) -> str:
    """text wrapped at word boundaries onto at most two lines of width px; the second is elided."""
    if fm.horizontalAdvance(text) <= width:
        return text
    words, first = text.split(" "), ""
    while words and fm.horizontalAdvance((first + " " + words[0]).strip()) <= width:
        first = (first + " " + words.pop(0)).strip()
    if not first:   # one long word
        return fm.elidedText(text, Qt.ElideRight, width)
    return first + "\n" + fm.elidedText(" ".join(words), Qt.ElideRight, width)


def _text_label(name: str, text: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


# --- Floating drawing bar ------------------------------------------------------------------------

class DrawBar(QFrame):
    """Translucent bar at the top of the video while drawing: Area | Line, Freeze, Undo, Cancel."""

    def __init__(self, ctx, canvas: AreaCanvas, controller: "LiveAreas"):
        super().__init__(canvas)
        self.setObjectName("DrawBar")
        self.canvas, self.ctl = canvas, controller
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 5, 6, 5)
        row.setSpacing(2)
        self.shape = QButtonGroup(self)
        self.area_btn = self._btn("Area", "Draw an area: click the corners (P)", True)
        self.line_btn = self._btn("Line", "Draw a line: drag across the path (L)", True)
        ctx.bind_icon(self.area_btn, "pentagon", "on_primary")
        ctx.bind_icon(self.line_btn, "line", "on_primary")
        self.shape.addButton(self.area_btn, 0)
        self.shape.addButton(self.line_btn, 1)
        self.shape.idClicked.connect(lambda i: controller.panel.switch_shape("polygon" if i == 0 else "line"))
        self.freeze_btn = self._btn("Freeze", "Freeze frame to draw precisely (F)", True)
        ctx.bind_icon(self.freeze_btn, "freeze", "on_primary")
        self.freeze_btn.toggled.connect(canvas.set_frozen)
        self.undo_btn = self._btn("", "Undo (Ctrl+Z). Hold Alt while dragging to place freely.")
        ctx.bind_icon(self.undo_btn, "undo", "on_primary")
        self.undo_btn.clicked.connect(canvas.undo)
        self.redo_btn = self._btn("", "Redo (Ctrl+Shift+Z)")
        ctx.bind_icon(self.redo_btn, "redo", "on_primary")
        self.redo_btn.clicked.connect(canvas.redo)
        self.cancel_btn = self._btn("Cancel", "Cancel (Esc)")
        self.cancel_btn.clicked.connect(lambda: controller.panel.cancel())
        for w in (self.area_btn, self.line_btn):
            row.addWidget(w)
        row.addWidget(self._sep())
        row.addWidget(self.freeze_btn)
        row.addWidget(self._sep())
        row.addWidget(self.undo_btn)
        row.addWidget(self.redo_btn)
        row.addWidget(self._sep())
        row.addWidget(self.cancel_btn)
        canvas.freeze_changed.connect(self._frozen)
        canvas.undo_stack.canUndoChanged.connect(self.undo_btn.setEnabled)
        canvas.undo_stack.canRedoChanged.connect(self.redo_btn.setEnabled)
        canvas.draft_changed.connect(self.sync)
        canvas.installEventFilter(self)
        self.hide()

    def _btn(self, text: str, tip: str, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.NoFocus)  # keys stay with the canvas
        b.setIconSize(QSize(16, 16))
        return b

    @staticmethod
    def _sep() -> QFrame:
        s = QFrame()
        s.setObjectName("DrawBarSep")
        return s

    def _frozen(self, on: bool) -> None:
        self.freeze_btn.blockSignals(True)
        self.freeze_btn.setChecked(on)
        self.freeze_btn.blockSignals(False)

    def sync(self) -> None:
        d = self.canvas.draft
        (self.line_btn if d.shape == "line" else self.area_btn).setChecked(True)
        self.undo_btn.setEnabled(self.canvas.undo_stack.canUndo())
        self.redo_btn.setEnabled(self.canvas.undo_stack.canRedo())
        self.place()

    def place(self) -> None:
        self.adjustSize()
        r = self.canvas.image_rect() if self.canvas.image() is not None else QRectF(self.canvas.rect())
        x = int(r.center().x() - self.width() / 2)
        y = int(r.top() - self.height() - 8) if r.top() >= BAR_OUTSIDE else int(r.top()) + 10
        self.move(max(4, x), max(4, y))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize and self.isVisible():
            self.place()
        return False


# --- Template cards (step 2) ----------------------------------------------------------------------

class _TemplateCard(QFrame):
    picked = Signal(str)
    nav = Signal(int)

    def __init__(self, ctx, key: str):
        super().__init__()
        self.key = key
        info = template(key)
        self.setObjectName("TemplateCard")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(f"{info['name']}: {info['example']}")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 9, 10, 9)
        row.setSpacing(10)
        tile = QLabel()
        tile.setObjectName("IconTile")
        tile.setFixedSize(36, 36)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(ctx.icon(info["icon"], "primary", 18).pixmap(18, 18))
        self.tile = tile
        col = QVBoxLayout()
        col.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = _text_label("TemplateName", info["name"], wrap=False)
        self.badge = QLabel(shape_badge(key))
        self.badge.setObjectName("Chip")
        top.addWidget(self.name, 1)
        top.addWidget(self.badge, 0, Qt.AlignTop)
        self.example = muted(info["example"])
        self.example.setProperty("muted", True)
        col.addLayout(top)
        col.addWidget(self.example)
        row.addWidget(tile, 0, Qt.AlignTop)
        row.addLayout(col, 1)
        self._fade = QGraphicsOpacityEffect(self)
        self._fade.setOpacity(1.0)
        self.setGraphicsEffect(self._fade)
        self.fits = True

    def set_fits(self, fits: bool) -> None:
        self.fits = fits
        self._fade.setOpacity(1.0 if fits else 0.55)
        need = shape_badge(self.key)
        self.badge.setText(need if fits else ("Needs a line" if need == "Line" else "Needs an area"))
        set_prop(self.badge, "tone", "" if fits else "warning")

    def set_selected(self, on: bool) -> None:
        set_prop(self, "selected", on)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.picked.emit(self.key)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.picked.emit(self.key)
        elif key in (Qt.Key_Up, Qt.Key_Down):
            self.nav.emit(-1 if key == Qt.Key_Up else 1)
        else:
            super().keyPressEvent(event)


# --- Side panel --------------------------------------------------------------------------------

class AreaPanel(QFrame):
    """Docked panel beside the video: 1 Draw · 2 Choose · 3 Details, then Save."""

    saved = Signal(dict)
    closed = Signal()

    def __init__(self, ctx, win, controller: "LiveAreas"):
        super().__init__()
        self.ctx, self.win, self.ctl = ctx, win, controller
        self.canvas: AreaCanvas = controller.canvas
        self.setObjectName("AreaPanel")
        self.setFixedWidth(PANEL_W)
        self.site = self.cam = None
        self.area: dict = {}             # being created / edited
        self.original: dict | None = None
        self.task: str | None = None
        self.wanted_task: str | None = None   # chosen in step 2 before a matching shape existed
        self.step = 0
        self._name_touched = False
        self._known: list[str] = []
        self._loading = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QVBoxLayout()
        head.setContentsMargins(18, 16, 18, 10)
        head.setSpacing(10)
        self.title = label("New area", "SectionTitle")
        head.addWidget(self.title)
        steps = QHBoxLayout()
        steps.setSpacing(2)
        self.step_btns = []
        for i, text in enumerate(("1 Draw", "2 Choose", "3 Details")):
            b = QPushButton(text)
            b.setObjectName("Step")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, i=i: self._step_clicked(i))
            self.step_btns.append(b)
            steps.addWidget(b)
        steps.addStretch(1)
        head.addLayout(steps)
        lay.addLayout(head)
        lay.addWidget(_hline())

        self.stack = QStackedWidget()
        self.stack.addWidget(_scroll(self._build_draw()))
        self.stack.addWidget(_scroll(self._build_choose()))
        self.form_host = QWidget()
        self.form_lay = QVBoxLayout(self.form_host)
        self.form_lay.setContentsMargins(18, 14, 18, 18)
        self.form_lay.setSpacing(10)
        self.stack.addWidget(_scroll(self.form_host))
        lay.addWidget(self.stack, 1)

        foot = QFrame()
        foot.setObjectName("WizardFooter")
        frow = QHBoxLayout(foot)
        frow.setContentsMargins(14, 10, 14, 10)
        frow.setSpacing(8)
        self.cancel_btn = button("Cancel", "ghost", "Close without saving (Esc)")
        self.cancel_btn.clicked.connect(self.cancel)
        self.back_btn = button("Back")
        self.back_btn.clicked.connect(lambda: self.go(max(0, self.step - 1)))
        self.next_btn = button("Next", "primary")
        self.next_btn.clicked.connect(self.primary)
        frow.addWidget(self.cancel_btn)
        frow.addStretch(1)
        frow.addWidget(self.back_btn)
        frow.addWidget(self.next_btn)
        lay.addWidget(foot)

        self.canvas.draft_changed.connect(self._draft_changed)
        self.canvas.draft_closed.connect(self._draft_closed)
        self.canvas.enter_pressed.connect(self.primary)
        self.canvas.preview_changed.connect(self._preview)
        self._meter = QTimer(self)
        self._meter.setInterval(1000)
        self._meter.timeout.connect(self._plate_meter)
        self.hide()

    # --- step 1 ---
    def _build_draw(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 14, 18, 18)
        lay.setSpacing(10)
        lay.addWidget(label("Draw an area or a line", "CardTitle"))
        self.shape_seg = Segmented([("Area", "Click the corners of an area (P)"),
                                    ("Line", "Drag a line across a path (L)")])
        self.shape_seg.setAccessibleName("Shape")
        self.shape_seg.changed.connect(lambda i: self.switch_shape("polygon" if i == 0 else "line"))
        lay.addWidget(self.shape_seg, 0, Qt.AlignLeft)
        lay.addWidget(muted("Area: click the corners, double-click to finish. "
                            "Line: drag from one side to the other."))
        lay.addWidget(muted("Draw on the ground. Something counts as inside when its feet or wheels are inside."))
        self.freeze_cb = QCheckBox("Freeze frame to draw precisely (F)")
        self.freeze_cb.toggled.connect(self.canvas.set_frozen)
        self.canvas.freeze_changed.connect(lambda on: (self.freeze_cb.blockSignals(True),
                                                       self.freeze_cb.setChecked(on),
                                                       self.freeze_cb.blockSignals(False)))
        lay.addWidget(self.freeze_cb)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.undo_btn = button("Undo", tooltip="Undo (Ctrl+Z)")
        self.ctx.bind_icon(self.undo_btn, "undo")
        self.undo_btn.clicked.connect(self.canvas.undo)
        self.redo_btn = button("Redo", tooltip="Redo (Ctrl+Shift+Z)")
        self.ctx.bind_icon(self.redo_btn, "redo")
        self.redo_btn.clicked.connect(self.canvas.redo)
        row.addWidget(self.undo_btn)
        row.addWidget(self.redo_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.draw_msg = _text_label("WarnText")
        lay.addWidget(self.draw_msg)
        self.many_msg = _text_label("WarnText", "Many areas on one camera can slow down monitoring.")
        lay.addWidget(self.many_msg)
        lay.addSpacing(6)
        lay.addWidget(muted("Tips: drag to draw a rectangle · hold Shift for a magnifier · "
                            "Alt places corners freely · Esc cancels"))
        lay.addStretch(1)
        return w

    # --- step 2 ---
    def _build_choose(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 14, 18, 18)
        lay.setSpacing(6)
        lay.addWidget(label("What should happen here?", "CardTitle"))
        self.cards: dict[str, _TemplateCard] = {}
        self.needs: dict[str, QFrame] = {}
        for group in GROUPS:
            lay.addSpacing(6)
            lay.addWidget(_text_label("StripCaption", group, wrap=False))
            for key, info in TEMPLATE_UI.items():
                if info["group"] != group:
                    continue
                c = _TemplateCard(self.ctx, key)
                c.picked.connect(self.pick)
                c.nav.connect(lambda step, key=key: self._card_nav(key, step))
                self.cards[key] = c
                lay.addWidget(c)
                lay.addWidget(self._needs_frame(key))
        lay.addStretch(1)
        return w

    def _needs_frame(self, key: str) -> QFrame:
        f = QFrame()
        f.setObjectName("NeedsShape")
        fl = QVBoxLayout(f)
        fl.setContentsMargins(12, 10, 12, 10)
        fl.setSpacing(8)
        need_line = template(key)["shapes"] == ("line",)
        fl.addWidget(QLabel("This needs a line. Draw a line instead?" if need_line else
                            "This needs an area. Draw an area instead?"))
        row = QHBoxLayout()
        go = button("Draw a line" if need_line else "Draw an area", "primary")
        go.clicked.connect(lambda: self._redraw_for(key))
        back = button("Back", "ghost")
        back.clicked.connect(lambda: f.hide())
        row.addWidget(go)
        row.addWidget(back)
        row.addStretch(1)
        fl.addLayout(row)
        f.hide()
        self.needs[key] = f
        return f

    def _card_nav(self, key: str, step: int) -> None:
        keys = list(self.cards)
        i = (keys.index(key) + step) % len(keys)
        self.cards[keys[i]].setFocus()
        self.stack.currentWidget().ensureWidgetVisible(self.cards[keys[i]])

    # --- lifecycle -------------------------------------------------------------------------

    def start_new(self, site, cam, shape: str = "polygon", task: str | None = None,
                  from_area: dict | None = None) -> None:
        self.site, self.cam = site, cam
        self.original = None
        self.task, self.wanted_task = None, task
        self._name_touched = False
        areas = cam_areas(cam)
        color = next_color(areas)
        self.area = {"id": new_area_id(), "enabled": True, "color": color}
        self.title.setText("New area")
        if from_area is not None:   # "Add another task here": same shape, new task
            d = Draft(from_area.get("shape", "polygon"), [list(p) for p in from_area.get("points", [])], True,
                      from_area.get("direction", "both"))
            self.canvas.begin_draft(color=color, draft=d)
        else:
            self.canvas.begin_draft(shape, color)
        self.many_msg.setVisible(len(areas) >= MANY_AREAS)
        self.show()
        self.go(1 if from_area is not None else 0)

    def edit(self, site, cam, area: dict) -> None:
        self.site, self.cam = site, cam
        self.original = copy.deepcopy(area)
        self.area = copy.deepcopy(area)
        self.task, self.wanted_task = area.get("task", "watch_zone"), None
        self._name_touched = True
        self.title.setText("Edit area")
        d = Draft(area.get("shape", "polygon"), [list(p) for p in area.get("points", [])], True,
                  area.get("direction", "both"))
        self.canvas.begin_draft(color=int(area.get("color", 0) or 0), draft=d, editing_id=area.get("id"))
        self.many_msg.hide()
        self.show()
        self.go(2)

    def close_panel(self) -> None:
        self._meter.stop()
        self.canvas.end_draft()
        self.hide()
        self.closed.emit()

    def dirty(self) -> bool:
        d = self.canvas.draft
        if self.original is None:
            return bool(d.points)
        o = self.original
        if [list(p) for p in o.get("points", [])] != d.points or o.get("direction", "both") != d.direction:
            return True
        if self.step == 2 and self.task:
            mine = self._collect()
            keys = ("name", "task", "objects", "schedule", "params", "alert", "color")
            return any(mine.get(k) != o.get(k) for k in keys)
        return False

    def cancel(self) -> None:
        if self.dirty() and not confirm(self, "Discard area", "Discard this area?", "Discard", "Keep editing"):
            return
        self.close_panel()

    # --- steps -----------------------------------------------------------------------------

    def go(self, step: int) -> None:
        if step >= 1 and not self.canvas.draft.closed:
            step = 0
        if step == 2 and not self.task:
            step = 1
        if self.step == 2 and step != 2 and self.task and getattr(self, "f_name", None) is not None:
            try:
                self.area = self._collect()   # keep what was typed when going back and forth
            except RuntimeError:
                pass
        self.step = step
        if step == 2:
            self._build_form(self.task)
            self._meter.start()
        else:
            self._meter.stop()
        if step == 1:
            self._refresh_cards()
        self.stack.setCurrentIndex(step)
        for i, b in enumerate(self.step_btns):
            set_prop(b, "state", "current" if i == step else "done" if i < step else "")
            b.setEnabled(i <= step or (i == 2 and bool(self.task) and self.canvas.draft.closed))
        self._footer()
        if step == 1:   # opens at the top, the selected / suggested card in view
            scroll = self.stack.widget(1)
            scroll.verticalScrollBar().setValue(0)
            first = self.cards.get(self._chosen() or "")
            if first:
                QTimer.singleShot(0, first, lambda: (scroll.ensureWidgetVisible(first, 0, 24), first.setFocus()))

    def _step_clicked(self, i: int) -> None:
        if i != self.step:
            self.go(i)

    def _footer(self) -> None:
        self.back_btn.setVisible(self.step > 0)
        if self.step == 2:
            self.next_btn.setText("Save changes" if self.original is not None else "Save area")
            self.next_btn.setEnabled(self.canvas.validity()[0])
        else:
            self.next_btn.setText("Next")
            ok = self.canvas.validity()[0]
            d = self.canvas.draft
            closable = self.step == 0 and self.canvas.mode == "draw" and d.shape == "polygon" and len(d.points) >= 3
            self.next_btn.setEnabled((ok or closable) and (self.step == 0 or bool(self._chosen())))
        self.next_btn.setToolTip("Enter")

    def primary(self) -> None:
        if not self.isVisible():
            return
        if self.step == 0:
            if self.canvas.mode == "draw":
                self.canvas.finish()
                return
            if self.canvas.validity()[0]:
                self.go(2 if self.task else 1)
        elif self.step == 1:
            key = self._chosen()
            if key:   # the highlighted card: the chosen task, else the suggestion
                self.pick(key)
        else:
            self.save()

    def _chosen(self) -> str | None:
        """The task card shown as selected on step 2: the chosen task, else the suggested one."""
        d = self.canvas.draft
        key = self.task or recommended_template(d)
        return key if key and fits_shape(key, d.shape) else None

    def _draft_changed(self) -> None:
        if not self.isVisible():
            return
        d = self.canvas.draft
        self.shape_seg.set_current(1 if d.shape == "line" else 0)
        ok, msg = self.canvas.validity()
        self.draw_msg.setText(msg if d.closed else "")
        self.draw_msg.setVisible(bool(msg) and d.closed)
        self.undo_btn.setEnabled(self.canvas.undo_stack.canUndo())
        self.redo_btn.setEnabled(self.canvas.undo_stack.canRedo())
        if not d.closed and self.step > 0:
            self.go(0)   # undo reopened the shape: back to drawing
            return
        if self.step == 2:
            self._update_direction_labels()
            self._form_warning()
        self._footer()

    def _draft_closed(self) -> None:
        if not self.isVisible():
            return
        wanted = self.wanted_task
        if wanted and fits_shape(wanted, self.canvas.draft.shape):
            self.task, self.wanted_task = wanted, None
            self.go(2)
        elif self.task and fits_shape(self.task, self.canvas.draft.shape):
            self.go(2)
        else:
            self.task = None if self.task and not fits_shape(self.task, self.canvas.draft.shape) else self.task
            self.go(1)

    def _refresh_cards(self) -> None:
        shape = self.canvas.draft.shape
        pick = self.task or recommended_template(self.canvas.draft)
        for key, c in self.cards.items():
            c.set_fits(fits_shape(key, shape))
            c.set_selected(key == pick)
            self.needs[key].hide()

    def pick(self, key: str) -> None:
        if not fits_shape(key, self.canvas.draft.shape):
            for k, f in self.needs.items():
                f.setVisible(k == key)
            return
        if self.original is not None and key != self.original.get("task") and self.task != key:
            if not confirm(self, "Change task", "Changing the task keeps the old log but starts a new one. Continue?",
                           "Change task", "Keep task"):
                return
        if self.task != key and not self._name_touched:
            self.area.pop("name", None)
        self.task = key
        self.go(2)

    def switch_shape(self, shape: str) -> bool:
        """Area <-> Line from the bar or step 1. A finished shape is only replaced after asking."""
        d = self.canvas.draft
        if shape != d.shape and d.closed and d.points:
            what = "a line" if shape == "line" else "an area"
            drawn = "line" if d.shape == "line" else "area"
            if not confirm(self, "Replace shape", f"Replace the drawn {drawn} with {what}?",
                           "Replace", f"Keep the {drawn}"):
                self._draft_changed()   # puts the Area / Line buttons back
                self.ctl.bar.sync()
                return False
        self.canvas.set_shape(shape)
        return True

    def _redraw_for(self, key: str) -> None:
        self.wanted_task, self.task = key, None
        drawn = self.canvas.draft.closed and bool(self.canvas.draft.points)
        self.canvas.set_shape(template(key)["shapes"][0])
        self.go(0)
        if drawn:
            self.win.notify("The old shape was removed. Ctrl+Z brings it back.")

    # --- step 3: details form --------------------------------------------------------------

    def _clear_form(self) -> None:
        while self.form_lay.count():
            item = self.form_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
            elif item.layout() is not None:
                _delete_layout(item.layout())

    def _build_form(self, key: str) -> None:
        self._loading = True
        self._clear_form()
        info = template(key)
        a = self.area
        shape = self.canvas.draft.shape
        lay = self.form_lay
        own = (a.get("params") or {}) if a.get("task") == key else {}
        params = {**template_params(key), **own}
        self._params = params

        head = QHBoxLayout()
        head.setSpacing(10)
        tile = QLabel()
        tile.setObjectName("IconTile")
        tile.setFixedSize(32, 32)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(self.ctx.icon(info["icon"], "primary", 16).pixmap(16, 16))
        head.addWidget(tile)
        head.addWidget(label(info["name"], "CardTitle"), 1)
        change = QPushButton("Change")
        change.setObjectName("LinkButton")
        change.setCursor(Qt.PointingHandCursor)
        change.setToolTip("Choose another task")
        change.clicked.connect(lambda: self.go(1))
        head.addWidget(change)
        lay.addLayout(head)

        lay.addWidget(self._caption("Name"))
        self.f_name = QLineEdit()
        taken = [x.get("name", "") for x in cam_areas(self.cam) if x.get("id") != a.get("id")]
        name = a.get("name") or default_name(key, shape, taken)   # pick() drops an untouched default
        self.f_name.setText(name)
        self.f_name.setAccessibleName("Name")
        self.f_name.textEdited.connect(lambda _t: setattr(self, "_name_touched", True))
        self.f_name.returnPressed.connect(self.primary)
        lay.addWidget(self.f_name)

        self.f_objects: dict[str, QPushButton] = {}
        if info["objects"]:
            lay.addWidget(self._caption("Watch for"))
            chips = _FlowRow()
            chosen = a.get("objects") if a.get("task") == key and a.get("objects") else list(info["objects"])
            for obj, text in OBJECTS:
                b = QPushButton(text)
                b.setObjectName("FilterChip")
                b.setCheckable(True)
                b.setCursor(Qt.PointingHandCursor)
                b.setChecked(obj in chosen)
                b.toggled.connect(lambda on, obj=obj: self._objects_changed(obj, on))
                self.f_objects[obj] = b
                chips.add(b)
            lay.addWidget(chips)
            self.f_obj_err = _text_label("ErrText", "Choose at least one")
            self.f_obj_err.hide()
            lay.addWidget(self.f_obj_err)

        self.f_dir = None
        if shape == "line" or key == "line_cross":
            lay.addWidget(self._caption("Direction"))
            if shape == "line":
                self.f_dir = Choice(["Both ways", "", ""])
            else:
                self.f_dir = Choice(["Both ways", "Going in", "Going out"])
            self.f_dir.set_current({"both": 0, "a_to_b": 1, "b_to_a": 2}.get(self.canvas.draft.direction, 0))
            self.f_dir.changed.connect(lambda i: self.canvas.set_direction(("both", "a_to_b", "b_to_a")[i]))
            lay.addWidget(self.f_dir)
            if shape == "line":
                lay.addWidget(muted("Tip: click the arrow on the video to change the direction."))
            self._update_direction_labels()

        self.f_spin = self.f_sentence = None
        if key in SENTENCES:
            text, unit, param, factor, low, high = SENTENCES[key]
            self.f_sentence = QLabel(text)
            self.f_sentence.setWordWrap(True)
            lay.addWidget(self.f_sentence)
            row = QHBoxLayout()
            row.setSpacing(8)
            self.f_spin = spin_box(low, high, max(low, int(round(float(params.get(param, low * factor)) / factor))))
            self.f_spin.setFixedWidth(96)
            self.f_spin.setAccessibleName(text)
            row.addWidget(self.f_spin)
            unit_lbl = QLabel(unit)
            unit_lbl.setWordWrap(True)
            row.addWidget(unit_lbl, 1)
            lay.addLayout(row)
            if key == "state_watch":
                lay.addWidget(muted("Light changes, headlights and the night (IR) switch are ignored."))
        if key == "count":
            days = getattr(self.ctx.cfg.settings, "area_log_days", 30)
            lay.addWidget(muted(f"Counts reset at midnight; history is kept for {days} days."))
        if key == "watch_zone":
            lay.addWidget(muted("The night, loitering, prowling and car-moved alerts (Settings → Monitoring) only "
                                "count people and cars inside this area."))
        if key == "log_plates":
            self._plates_section(lay, params)
        if key == "ask_ai":
            self._ask_section(lay, params)

        self._more_options(lay, key, shape, params)

        self.f_when = None
        if key != "watch_zone":
            lay.addWidget(self._caption("When"))
            self.f_when = Segmented([("Always", "Watch day and night"),
                                     ("At night", "Only during the night hours set in Settings → Monitoring"),
                                     ("Custom", "Choose the hours")])
            sched = a.get("schedule") or {"mode": info.get("schedule", "always")}
            self.f_when.set_current({"always": 0, "night": 1, "custom": 2}.get(sched.get("mode"), 0))
            lay.addWidget(self.f_when, 0, Qt.AlignLeft)
            times = QWidget()
            tl = QHBoxLayout(times)
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(6)
            self.f_from, self.f_to = QTimeEdit(), QTimeEdit()
            for edit, value in ((self.f_from, sched.get("start", "22:00")), (self.f_to, sched.get("end", "06:00"))):
                edit.setDisplayFormat("HH:mm")
                edit.setTime(QTime.fromString(value, "HH:mm"))
                edit.setFixedWidth(76)
                edit.timeChanged.connect(self._times_changed)
            self.f_over = muted("(overnight)", wrap=False)
            tl.addWidget(QLabel("from"))
            tl.addWidget(self.f_from)
            tl.addWidget(QLabel("to"))
            tl.addWidget(self.f_to)
            tl.addWidget(self.f_over)
            tl.addStretch(1)
            self.f_times = times
            lay.addWidget(times)
            self.f_when.changed.connect(lambda i: self.f_times.setVisible(i == 2))
            times.setVisible(self.f_when.current() == 2)
            self._times_changed()

        self.f_alert = None
        if key != "watch_zone":
            row = QHBoxLayout()
            row.setSpacing(10)
            self.f_alert = Switch(self.ctx)
            self.f_alert.setAccessibleName("Also raise an alert")
            on = a.get("alert") if a.get("task") == key and "alert" in a else info["alert"]
            self.f_alert.set_checked_quietly(bool(on))
            row.addWidget(self.f_alert)
            row.addWidget(label("Also raise an alert", "CardTitle"), 1)
            lay.addLayout(row)
            lay.addWidget(muted("Shows up in Incidents with a short video clip, like other alerts. "
                                "Every event is still logged on the Areas page."))

        lay.addWidget(self._caption("Colour"))
        sw = QHBoxLayout()
        sw.setSpacing(8)
        self.f_color = QButtonGroup(self)
        for i, c in enumerate(ZONE_COLORS):
            b = QPushButton()
            b.setObjectName("Swatch")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setAccessibleName(f"Colour {i + 1}")
            b.setStyleSheet(f"QPushButton#Swatch {{ background: {c}; }}")
            self.f_color.addButton(b, i)
            sw.addWidget(b)
        sw.addStretch(1)
        self.f_color.button(int(a.get("color", 0) or 0) % len(ZONE_COLORS)).setChecked(True)
        self.f_color.idClicked.connect(self._color_changed)
        lay.addLayout(sw)

        self.f_preview = _text_label("OkText")
        self.f_preview.hide()
        lay.addWidget(self.f_preview)
        self.f_warn = _text_label("WarnText")
        lay.addWidget(self.f_warn)
        self.f_error = _text_label("ErrText")
        self.f_error.hide()
        lay.addWidget(self.f_error)
        lay.addStretch(1)
        self._form_warning()
        self._objects_changed(None, True)
        self._loading = False

    def _more_options(self, lay: QVBoxLayout, key: str, shape: str, params: dict) -> None:
        """Names of the two directions (lines only) and the minimum plate size, folded away."""
        self.f_more = self.f_in_name = self.f_out_name = self.f_min_plate = None
        if not (key == "log_plates" or (key == "count" and shape == "line")):
            return
        more_btn = QPushButton("More options")
        more_btn.setObjectName("LinkButton")
        more_btn.setCursor(Qt.PointingHandCursor)
        more = QWidget()
        ml = QVBoxLayout(more)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        ml.addWidget(QLabel("Names of the two directions"))
        nrow = QHBoxLayout()
        self.f_in_name, self.f_out_name = QLineEdit(params.get("in_name", "In")), QLineEdit(params.get("out_name", "Out"))
        self.f_in_name.setAccessibleName("Name of the first direction")
        self.f_out_name.setAccessibleName("Name of the other direction")
        nrow.addWidget(self.f_in_name)
        nrow.addWidget(self.f_out_name)
        ml.addLayout(nrow)
        if key == "log_plates":
            prow = QHBoxLayout()
            prow.addWidget(QLabel("Minimum plate size"))
            self.f_min_plate = spin_box(20, 400, int(params.get("min_plate_px", 80)))
            self.f_min_plate.setFixedWidth(90)
            prow.addWidget(self.f_min_plate)
            prow.addWidget(QLabel("px"))
            prow.addStretch(1)
            ml.addLayout(prow)
        more.hide()
        more_btn.clicked.connect(lambda: (more.setVisible(not more.isVisible()),
                                          more_btn.setText("Fewer options" if more.isVisible() else "More options")))
        lay.addWidget(more_btn, 0, Qt.AlignLeft)
        lay.addWidget(more)
        self.f_more = more

    @staticmethod
    def _caption(text: str) -> QLabel:
        return label(text, "CardTitle")

    def _form_warning(self) -> None:
        if not hasattr(self, "f_warn"):
            return
        try:
            ok, msg = self.canvas.validity()
            self.f_warn.setText(msg)
            self.f_warn.setVisible(bool(msg))
        except RuntimeError:
            pass

    def _objects_changed(self, obj, on: bool) -> None:
        if not self.f_objects:
            self.canvas.preview_objects = set()
            return
        chosen = [o for o, b in self.f_objects.items() if b.isChecked()]
        if not chosen and obj is not None:
            self.f_objects[obj].blockSignals(True)
            self.f_objects[obj].setChecked(True)   # at least one stays on
            self.f_objects[obj].blockSignals(False)
            self.f_obj_err.show()
            QTimer.singleShot(2500, self.f_obj_err, self.f_obj_err.hide)
            chosen = [obj]
        self.canvas.preview_objects = set(chosen)
        if self.task == "loitering" and getattr(self, "f_sentence", None) is not None:
            who = "someone" if chosen == ["person"] else "a " + " or ".join(obj_word(o, 1) for o in chosen)
            self.f_sentence.setText(f"Tell me when {who} stays longer than")

    def _times_changed(self) -> None:
        if getattr(self, "f_over", None) is not None:
            self.f_over.setVisible(self.f_to.time() < self.f_from.time())

    def _color_changed(self, i: int) -> None:
        self.area["color"] = i
        self.canvas.draft_color = i
        self.canvas.update()

    def _update_direction_labels(self) -> None:
        if self.f_dir is None or self.canvas.draft.shape != "line":
            return
        labels = self.canvas.direction_labels()
        for i, key in ((1, "a_to_b"), (2, "b_to_a")):
            arrow, words = labels[key]
            self.f_dir.set_text(i, f"{arrow}  {words[0].upper() + words[1:]}")
        self.f_dir.set_current({"both": 0, "a_to_b": 1, "b_to_a": 2}.get(self.canvas.draft.direction, 0))

    def _preview(self, text: str) -> None:
        if getattr(self, "f_preview", None) is None:
            return
        try:
            self.f_preview.setText(text)
            self.f_preview.setVisible(bool(text))
        except RuntimeError:
            pass

    # --- plates ---
    def _plates_section(self, lay: QVBoxLayout, params: dict) -> None:
        lay.addWidget(self._caption("Known plates"))
        self._known = [normalise_plate(p) for p in params.get("known", []) if p]
        self.f_plate_in = QLineEdit()
        self.f_plate_in.setPlaceholderText("Type a plate, then Enter")
        self.f_plate_in.setAccessibleName("Add a known plate")
        self.f_plate_in.returnPressed.connect(self._add_plate)
        lay.addWidget(self.f_plate_in)
        self.f_plates = _FlowRow()
        lay.addWidget(self.f_plates)
        self._fill_plates()
        lay.addWidget(muted("Plates from residents are marked ‘Resident’ in the log."))
        self.f_meter = QLabel("")
        self.f_meter.setWordWrap(True)
        lay.addWidget(self.f_meter)
        lay.addWidget(muted("Works best where cars slow down and the plate faces the camera."))
        note = plate_model_note()
        if note:
            lay.addWidget(muted(note))
        self._plate_meter()

    def _fill_plates(self) -> None:
        self.f_plates.clear()
        for plate in self._known:
            chip = QFrame()
            chip.setObjectName("PlateChip")
            row = QHBoxLayout(chip)
            row.setContentsMargins(10, 2, 6, 2)
            row.setSpacing(4)
            text = QLabel(plate)
            text.setObjectName("PlateText")
            row.addWidget(text)
            x = QPushButton()
            x.setToolTip(f"Remove {plate}")
            x.setCursor(Qt.PointingHandCursor)
            x.setIcon(self.ctx.icon("x", "muted", 12))
            x.setIconSize(QSize(12, 12))
            x.clicked.connect(lambda _c=False, plate=plate: self._remove_plate(plate))
            row.addWidget(x)
            self.f_plates.add(chip)

    def _add_plate(self) -> None:
        plate = normalise_plate(self.f_plate_in.text())
        if plate and plate not in self._known:
            self._known.append(plate)
            self._fill_plates()
        self.f_plate_in.clear()

    def _remove_plate(self, plate: str) -> None:
        self._known = [p for p in self._known if p != plate]
        self._fill_plates()

    def _plate_meter(self) -> None:
        if self.task != "log_plates" or self.step != 2 or getattr(self, "f_meter", None) is None:
            return
        try:
            self.f_meter.setText("Plate readability now: " + self.ctl.plate_readability())
        except RuntimeError:
            self._meter.stop()

    # --- ask the AI ---
    def _ask_section(self, lay: QVBoxLayout, params: dict) -> None:
        lay.addWidget(self._caption("What should be visible?"))
        self.f_question = QLineEdit(params.get("question", ""))
        self.f_question.setPlaceholderText("the barrier arm is raised")
        self.f_question.setAccessibleName("What should be visible")
        lay.addWidget(self.f_question)
        ex = _FlowRow()
        for text in ASK_EXAMPLES:
            b = QPushButton(text)
            b.setObjectName("FilterChip")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, text=text: self.f_question.setText(text))
            ex.add(b)
        lay.addWidget(ex)
        lay.addWidget(muted("Describe what you want to see, not a question. YardGuard tells you when the answer "
                            "changes."))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Check every"))
        self.f_interval = spin_box(5, 3600, int(params.get("interval_s", 30)))
        self.f_interval.setFixedWidth(90)
        row.addWidget(self.f_interval)
        row.addWidget(muted("s (only when the picture changed)"), 1)
        lay.addLayout(row)
        trow = QHBoxLayout()
        self.f_test = button("Test now", tooltip="Ask the AI once about this area right now")
        self.f_test.clicked.connect(self._test_now)
        trow.addWidget(self.f_test)
        trow.addStretch(1)
        lay.addLayout(trow)
        self.f_test_img = QLabel()
        self.f_test_img.hide()
        lay.addWidget(self.f_test_img)
        self.f_test_res = QLabel("")
        self.f_test_res.setWordWrap(True)
        lay.addWidget(self.f_test_res)
        ready = self.ctl.brain_ready()
        self.f_test.setEnabled(ready)
        if not ready:
            self.f_test.setToolTip("The AI brain is not running. Start it in Settings → AI brain.")
            self.f_test_res.setText("The AI brain is not running, so this question is not checked yet.")
            set_prop(self.f_test_res, "muted", True)
            if hasattr(self.win, "settings") and hasattr(self.win, "go_page"):
                link = QPushButton("Open AI brain settings")
                link.setObjectName("LinkButton")
                link.setCursor(Qt.PointingHandCursor)
                link.clicked.connect(lambda: self.win.go_page(self.win.settings))
                lay.addWidget(link, 0, Qt.AlignLeft)

    def _test_now(self) -> None:
        question = self.f_question.text().strip()
        if not question:
            self.f_test_res.setText("Type what should be visible first.")
            return
        crop = self.ctl.area_crop(self.canvas.draft.points)
        if crop is None:
            self.f_test_res.setText("No picture yet. Wait until the video is shown.")
            return
        self.f_test.setEnabled(False)
        self.f_test_res.setText("Asking the AI…")
        from .qt_util import to_qimage
        pm = QPixmap.fromImage(to_qimage(crop)).scaled(160, 90, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.f_test_img.setPixmap(pm)
        self.f_test_img.show()
        started = time.monotonic()
        brain, mode = self.ctx.brain, getattr(self.ctx.cfg.settings, "brain_generation", "fast")

        def ask():
            return brain.ask(crop, [question], mode=mode, upscale=True)

        def done(result):
            try:
                self.f_test.setEnabled(True)
                took = f" ({time.monotonic() - started:.1f} s)"
                if isinstance(result, Exception):
                    self.f_test_res.setText(f"The AI could not answer: {result}")
                else:
                    self.f_test_res.setText(("Yes, found" if result else "No, not found") + took)
            except RuntimeError:
                pass

        run_async(ask, on_done=done)

    # --- save ------------------------------------------------------------------------------

    def _collect(self) -> dict:
        key = self.task
        d = self.canvas.draft
        a = dict(self.area)
        params = dict(getattr(self, "_params", {}) or template_params(key))
        a.update(name=self.f_name.text().strip() or default_name(key, d.shape, []),
                 shape=d.shape, points=[list(p) for p in d.points], direction=d.direction, task=key)
        a["objects"] = [o for o, b in self.f_objects.items() if b.isChecked()] if self.f_objects else []
        if key in SENTENCES and self.f_spin is not None:
            _t, _u, param, factor, _lo, _hi = SENTENCES[key]
            params[param] = self.f_spin.value() * factor
        if key == "log_plates":
            params["known"] = list(self._known)
            if getattr(self, "f_min_plate", None) is not None:
                params["min_plate_px"] = self.f_min_plate.value()
        if key in ("count", "log_plates") and getattr(self, "f_in_name", None) is not None:
            params["in_name"] = self.f_in_name.text().strip() or "In"
            params["out_name"] = self.f_out_name.text().strip() or "Out"
        if key == "ask_ai":
            params["question"] = self.f_question.text().strip()
            params["interval_s"] = self.f_interval.value()
        a["params"] = params
        if self.f_when is not None:
            mode = ("always", "night", "custom")[max(0, self.f_when.current())]
            a["schedule"] = {"mode": mode, "start": self.f_from.time().toString("HH:mm"),
                             "end": self.f_to.time().toString("HH:mm")}
        else:
            a["schedule"] = {"mode": "always", "start": "00:00", "end": "00:00"}
        a["alert"] = bool(self.f_alert.isChecked()) if self.f_alert is not None else False
        a["color"] = max(0, self.f_color.checkedId())
        a.setdefault("enabled", True)
        return a

    def save(self) -> bool:
        ok, msg = self.canvas.validity()
        if not ok:
            self._error(msg or "Finish drawing the shape first.")
            return False
        if self.task == "ask_ai" and not self.f_question.text().strip():
            self._error("Type what should be visible, for example ‘the gate is open’.")
            self.f_question.setFocus()
            return False
        area = self._collect()
        cam = self.cam
        areas = cam_areas(cam)
        idx = next((i for i, x in enumerate(areas) if x.get("id") == area["id"]), None)
        old = copy.deepcopy(areas)
        if idx is None:
            areas.append(area)
        else:
            areas[idx] = area
        cam.areas = areas
        try:
            self.ctl.save_config()
        except Exception as exc:
            cam.areas = old
            self._error(f"Could not save the area: {exc}")
            self.win.notify(f"Could not save the area: {exc}", error=True)
            return False
        watching = self.ctl.monitoring()
        name = area["name"]
        self.win.notify(f"“{name}” saved. YardGuard is watching it now." if watching else
                        f"“{name}” saved. It starts working when monitoring is on.")
        self.close_panel()
        self.saved.emit(area)
        return True

    def _error(self, text: str) -> None:
        if getattr(self, "f_error", None) is not None and self.step == 2:
            self.f_error.setText(text)
            self.f_error.show()
        else:
            self.draw_msg.setText(text)
            self.draw_msg.show()

    # --- keys: drawing keys work while the panel has the focus ---
    def event(self, event):
        if event.type() == QEvent.ShortcutOverride and self.isVisible():
            k, mods = event.key(), event.modifiers()
            if k == Qt.Key_Escape or (mods & Qt.ControlModifier and k in (Qt.Key_Z, Qt.Key_Y)):
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        k, mods = event.key(), event.modifiers()
        if k == Qt.Key_Escape:
            self.ctl.escape()
            return
        if mods & Qt.ControlModifier and k in (Qt.Key_Z, Qt.Key_Y):
            self.canvas.handle_key(k, mods)
            return
        if self.step == 1 and Qt.Key_1 <= k <= Qt.Key_9:
            keys = list(self.cards)
            i = k - Qt.Key_1
            if i < len(keys):
                self.pick(keys[i])
            return
        if k in (Qt.Key_Return, Qt.Key_Enter) and self.step != 1 and not self._typing_field():
            self.primary()
            return
        super().keyPressEvent(event)

    # Tab stays inside the panel while it is open: the step's fields, then Save / Back / Cancel, the
    # step buttons and the video. (Qt asks the parents of the focused widget, so this sees every Tab.)
    def focusNextPrevChild(self, next_: bool) -> bool:
        chain = self._focus_chain()
        if not self.isVisible() or not chain:
            return super().focusNextPrevChild(next_)
        cur = QApplication.focusWidget()
        while cur is not None and cur not in chain and cur is not self:
            cur = cur.parentWidget()   # a spin box's inner line edit is its spin box
        i = chain.index(cur) if cur in chain else (-1 if next_ else 0)
        target = chain[(i + (1 if next_ else -1)) % len(chain)]
        target.setFocus(Qt.TabFocusReason if next_ else Qt.BacktabFocusReason)
        if isinstance(self.stack.currentWidget(), QScrollArea) and self.stack.currentWidget().isAncestorOf(target):
            self.stack.currentWidget().ensureWidgetVisible(target, 0, 24)
        return True

    def _focus_chain(self) -> list[QWidget]:
        page = self.stack.currentWidget()

        def tabbable(w: QWidget) -> bool:
            return bool(w.focusPolicy() & Qt.TabFocus) and w.isVisibleTo(self) and w.isEnabled() \
                and not isinstance(w.parentWidget(), QAbstractSpinBox)

        fields = [w for w in page.findChildren(QWidget) if tabbable(w)] if page is not None else []
        tail = [self.next_btn, self.back_btn, self.cancel_btn, *self.step_btns]
        chain = fields + [w for w in tail if tabbable(w)]
        if self.canvas.isVisible() and self.canvas.focusPolicy() & Qt.TabFocus:
            chain.append(self.canvas)
        return chain

    def _typing_field(self) -> bool:
        """Enter in a form field (a known plate, the question, a number) belongs to that field: it
        must not save the half-finished area. The Name field saves on Enter itself."""
        w = QApplication.focusWidget()
        return isinstance(w, (QLineEdit, QAbstractSpinBox)) and w is not getattr(self, "f_name", None)


def _delete_layout(lay) -> None:
    while lay.count():
        item = lay.takeAt(0)
        if item.widget() is not None:
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout() is not None:
            _delete_layout(item.layout())
    lay.deleteLater()


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto more lines (chips in a fixed-width panel)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect: QRect, apply: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._gap, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line = max(line, hint.height())
        return y + line - rect.y()


class _FlowRow(QWidget):
    """Chips that wrap onto more lines."""

    def __init__(self):
        super().__init__()
        self._lay = FlowLayout(self)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def add(self, w: QWidget) -> None:
        self._lay.addWidget(w)

    def clear(self) -> None:
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self._lay.invalidate()


def plate_model_note() -> str:
    """'The number-plate reader downloads once (about N MB) when you save.' while it is missing."""
    try:
        from . import plates
    except Exception:
        return ""
    ready = getattr(plates, "model_ready", None) or getattr(plates, "models_ready", None)
    try:
        if ready is None or ready():
            return ""
    except Exception:
        return ""
    mb = getattr(plates, "DOWNLOAD_MB", None)
    size = f" (about {mb:.0f} MB)" if isinstance(mb, (int, float)) else ""
    return f"The number-plate reader downloads once{size} when it is first needed."


# --- Popover ---------------------------------------------------------------------------------

class AreaPopover(QFrame):
    """Card shown beside an area clicked on the video: live state, last events, actions."""

    def __init__(self, ctx, controller: "LiveAreas"):
        super().__init__(controller.canvas)
        self.ctx, self.ctl = ctx, controller
        self.setObjectName("AreaPopover")
        self.setFixedWidth(300)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)
        self.area_id = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.name = label("", "CardTitle")
        self.chip = QLabel("")
        self.chip.setObjectName("Chip")
        close = QPushButton()
        close.setProperty("kind", "ghost")
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Close (Esc)")
        ctx.bind_icon(close, "x", "muted")
        close.clicked.connect(self.hide)
        top.addWidget(self.name, 1)
        top.addWidget(self.chip, 0, Qt.AlignVCenter)
        top.addWidget(close, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        self.state = QLabel("")
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.events_box = QVBoxLayout()
        self.events_box.setSpacing(2)
        lay.addLayout(self.events_box)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.edit_btn = button("Edit", "primary")
        ctx.bind_icon(self.edit_btn, "pencil", "on_primary")
        self.edit_btn.clicked.connect(lambda: (self.hide(), controller.edit(self.area_id)))
        self.log_btn = button("Open log")
        ctx.bind_icon(self.log_btn, "list")
        self.log_btn.clicked.connect(lambda: (self.hide(), controller.open_log.emit(self.area_id)))
        self.more_btn = button("", "ghost", "More actions")
        ctx.bind_icon(self.more_btn, "more")
        self.more_btn.clicked.connect(self._menu)
        btns.addWidget(self.edit_btn)
        btns.addWidget(self.log_btn)
        btns.addStretch(1)
        btns.addWidget(self.more_btn)
        lay.addLayout(btns)
        self.hide()

    def show_for(self, area: dict, at: QPointF) -> None:
        self.area_id = area.get("id", "")
        info = template(area.get("task", "watch_zone"))
        self.name.setText(QFontMetrics(self.name.font()).elidedText(area.get("name", "Area"), Qt.ElideRight, 170))
        self.chip.setText(info["name"])
        self.refresh(area)
        _clear_box(self.events_box)
        events = self.ctl.events(area_id=self.area_id, limit=3)
        if not events and area.get("task") != "watch_zone":
            self.events_box.addWidget(muted("Nothing logged yet."))
        for e in events[:3]:
            self.events_box.addWidget(self._event_row(e, area))
        self.adjustSize()
        self._place(at)
        self.show()
        self.raise_()

    def refresh(self, area: dict) -> None:
        live = self.ctl.live.get(area.get("id"))
        text = stat_text(area, live, self.ctl.monitoring())
        sched = schedule_text(area.get("schedule"))
        bits = [text] + ([sched] if sched != "Always" else []) + (["Alerts on"] if area.get("alert") else [])
        self.state.setText(" · ".join(b for b in bits if b))

    def _event_row(self, e, area: dict) -> QWidget:
        row = _ClickRow()
        row.setObjectName("PopEvent")
        row.setToolTip("Open the log")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(10)
        pic = QLabel()
        pic.setFixedSize(48, 27)
        pic.setStyleSheet("background: #0b0d10; border-radius: 4px;")
        pic.setAttribute(Qt.WA_TransparentForMouseEvents)
        path = getattr(e, "crop_path", "") or getattr(e, "snapshot_path", "")
        if path:
            run_async(load_image, path, QSize(96, 54), True,
                      on_done=lambda img, pic=pic: isinstance(img, QImage) and _set_pic(pic, img, QSize(48, 27)))
        lay.addWidget(pic)
        col = QVBoxLayout()
        col.setSpacing(0)
        text = QLabel()
        fm = QFontMetrics(text.font())
        full = event_text(e, area)
        shown = _two_lines(fm, full, 210)
        text.setText(shown)
        if shown.replace("\n", " ") != full:
            row.setToolTip(full)
        when = muted(rel_time(e.ts), wrap=False)
        for w in (text, when):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
            col.addWidget(w)
        lay.addLayout(col, 1)
        row.clicked.connect(lambda: (self.hide(), self.ctl.open_log.emit(self.area_id)))
        return row

    def _place(self, at: QPointF) -> None:
        canvas = self.ctl.canvas
        rect = canvas.area_rect(self.area_id)
        w, h = self.width(), self.height()
        x = rect.right() + 12 if rect.right() + 12 + w < canvas.width() else rect.left() - 12 - w
        if x < 4:
            x = min(max(4.0, at.x() + 12), canvas.width() - w - 4)
        y = min(max(4.0, at.y() - h / 3), canvas.height() - h - 4)
        self.move(int(x), int(y))

    def _menu(self) -> None:
        area = self.ctl.area(self.area_id)
        if area is None:
            return
        m = QMenu(self)
        m.addAction("Switch off" if area.get("enabled", True) else "Switch on",
                    lambda: self.ctl.set_enabled(self.area_id, not area.get("enabled", True)))
        m.addAction("Duplicate", lambda: self.ctl.duplicate(self.area_id))
        m.addAction("Add another task here", lambda: (self.hide(), self.ctl.start_add(from_area=area)))
        if area.get("task") in ("left_object", "state_watch"):
            m.addAction("This is how it should look", lambda: self.ctl.reset_reference(self.area_id))
        m.addSeparator()
        m.addAction("Delete…", lambda: (self.hide(), self.ctl.delete(self.area_id)))
        m.exec(QCursor.pos())


class _ClickRow(QFrame):
    """A focusable row that acts like a button (mouse, Enter, Space)."""

    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)


def _set_pic(lbl: QLabel, img: QImage, size: QSize) -> None:
    try:
        pm = QPixmap.fromImage(img).scaled(size * 2, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(2.0)
        lbl.setPixmap(pm)
    except RuntimeError:
        pass


def _clear_box(box) -> None:
    while box.count():
        item = box.takeAt(0)
        if item.widget() is not None:
            item.widget().hide()
            item.widget().deleteLater()


# --- Controller ------------------------------------------------------------------------------

class LiveAreas(QObject):
    """Everything about areas on the Live page. The page hosts self.canvas (instead of its
    ImageView), self.panel (right of the video), self.add_btn and self.show_cb (toolbar)."""

    open_log = Signal(str)        # area id: show it in the Areas hub
    changed = Signal()            # areas were added, edited or deleted

    def __init__(self, ctx, win, raw_frame=None):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.raw_frame = raw_frame or (lambda: None)   # BGR ndarray of the current camera picture (no drawings)
        self.save_config = ctx.save
        self.site = self.cam = None
        self.live: dict = {}
        self._pending = None
        self.canvas = AreaCanvas(ctx)
        self.panel = AreaPanel(ctx, win, self)
        self.bar = DrawBar(ctx, self.canvas, self)
        self.popover = AreaPopover(ctx, self)
        self.add_btn = button("Add area", "primary", "Draw an area or a line on this camera (A)")
        ctx.bind_icon(self.add_btn, "plus", "on_primary")
        self.add_btn.clicked.connect(lambda: self.start_add())
        self.show_cb = QCheckBox("Show areas")
        self.show_cb.setChecked(True)
        self.show_cb.toggled.connect(self.canvas.set_show_areas)
        self.canvas.area_clicked.connect(self._area_clicked)
        self.canvas.clicked.connect(self.popover.hide)
        self.canvas.escape_pressed.connect(self.escape)
        self.canvas.draft_changed.connect(self._sync_bar)
        self.panel.closed.connect(self._panel_closed)
        self.panel.saved.connect(lambda _a: self._after_change())
        self._poll = QTimer(self.canvas)   # goes with the canvas when the page is torn down
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self.poll)
        self._thumb_at: dict = {}
        bridge = getattr(ctx, "alert_bridge", None)
        if bridge is not None and hasattr(bridge, "area_event_added"):
            bridge.area_event_added.connect(self.area_event)

    # --- data ---
    def store(self):
        return getattr(self.ctx, "alert_store", None)

    def monitoring(self) -> bool:
        mon = getattr(self.ctx, "monitor", None)
        return bool(mon is not None and getattr(mon, "running", False))

    def brain_ready(self) -> bool:
        brain = getattr(self.ctx, "brain", None)
        try:
            return brain is not None and brain.status() in ("ready", "busy")
        except Exception:
            return False

    def areas(self) -> list[dict]:
        return cam_areas(self.cam) if self.cam is not None else []

    def area(self, area_id: str) -> dict | None:
        return next((a for a in self.areas() if a.get("id") == area_id), None)

    def events(self, **filters) -> list:
        store = self.store()
        fn = getattr(store, "list_area_events", None)
        if fn is None:
            return []
        try:
            return list(fn(**filters))
        except Exception:
            return []

    # --- camera ---
    def set_camera(self, site, cam) -> bool:
        """The Live page shows this camera now (call from LivePage.start). Returns False when the user
        kept a drawing in progress on the previous camera."""
        same = (cam is not None and self.cam is not None and site is not None and self.site is not None
                and site.id == self.site.id and cam.channel == self.cam.channel)
        if self.canvas.drafting and not same:
            if not confirm(self.win, "Discard area", "Discard the area you are drawing?", "Discard", "Keep drawing"):
                return False
            self.panel.close_panel()
        self.site, self.cam = site, cam
        if same and self.canvas.drafting:  # the config was saved elsewhere: save into its current objects
            self.panel.site, self.panel.cam = site, cam
        self.popover.hide()
        self.canvas.set_areas(self.areas())
        n = len(self.areas())
        self.show_cb.setToolTip(f"{n} area{'s' if n != 1 else ''} on this camera" if n else
                                "No areas on this camera yet. Use Add area to draw one.")
        self.add_btn.setEnabled(cam is not None)
        self.add_btn.setToolTip("Draw an area or a line on this camera (A)" if cam is not None else
                                "Add a camera in Sites & cameras first")
        self.poll()
        self._poll.start()
        if self._pending and cam is not None and self._pending[0] == cam.channel:
            _ch, fn = self._pending
            self._pending = None
            QTimer.singleShot(0, fn)
        return True

    def confirm_discard(self) -> bool:
        if not self.canvas.drafting or not self.panel.dirty():
            return True
        if confirm(self.win, "Discard area", "Discard the area you are drawing?", "Discard", "Keep drawing"):
            self.panel.close_panel()
            return True
        return False

    def request(self, channel: int, action: str, area_id: str = "") -> None:
        """Run 'add' / 'edit' once this channel is shown (the hub opens Live view first)."""
        fn = (lambda: self.start_add()) if action == "add" else (lambda: self.edit(area_id))
        if self.cam is not None and self.cam.channel == channel:
            QTimer.singleShot(0, fn)
        else:
            self._pending = (channel, fn)

    def stop(self) -> None:
        """The Live page was hidden."""
        self._poll.stop()
        self.popover.hide()

    # --- live ---
    def poll(self) -> None:
        mon = getattr(self.ctx, "monitor", None)
        fn = getattr(mon, "area_live", None)
        live = {}
        if fn is not None and self.cam is not None and self.monitoring():
            try:
                live = fn(self.cam.channel) or {}
            except Exception:
                live = {}
        self.live = live
        self.canvas.set_live(live, self.monitoring())
        if self.popover.isVisible():
            a = self.area(self.popover.area_id)
            if a is not None:
                self.popover.refresh(a)
        self._save_thumb()

    def area_event(self, e) -> None:
        if self.cam is not None and getattr(e, "channel", None) == self.cam.channel:
            self.canvas.pulse(getattr(e, "area_id", ""))

    def set_tracks(self, tracks) -> None:
        self.canvas.set_tracks(tracks)

    def _save_thumb(self) -> None:
        img = self.canvas.latest_image()
        if img is None or self.cam is None or self.site is None:
            return
        key = (self.site.id, self.cam.channel)
        if time.monotonic() - self._thumb_at.get(key, -1e9) < 60:
            return
        self._thumb_at[key] = time.monotonic()
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            img.scaledToWidth(320, Qt.SmoothTransformation).save(str(thumb_path(*key)), "JPG", 82)
        except Exception:
            pass

    # --- drawing ---
    def start_add(self, shape: str | None = None, task: str | None = None, from_area: dict | None = None) -> None:
        if self.cam is None:
            return
        if self.canvas.drafting and not self.confirm_discard():
            return
        self.popover.hide()
        self.show_cb.setChecked(True)
        self.panel.start_new(self.site, self.cam, shape or "polygon", task, from_area)
        self._sync_bar()

    def edit(self, area_id: str) -> None:
        a = self.area(area_id)
        if a is None or self.cam is None:
            return
        if self.canvas.drafting and not self.confirm_discard():
            return
        self.popover.hide()
        self.panel.edit(self.site, self.cam, a)
        self._sync_bar()

    def escape(self) -> bool:
        """Esc ladder: close the popover, drop unfinished points, ask to discard the draft.
        False when nothing here used it (the page may leave full screen then)."""
        if self.popover.isVisible():
            self.popover.hide()
            return True
        if not self.canvas.drafting:
            return False
        if self.canvas.drop_in_progress():
            return True
        self.panel.cancel()
        return True

    def _sync_bar(self) -> None:
        drafting = self.canvas.drafting
        self.bar.setVisible(drafting)
        self.add_btn.setEnabled(not drafting and self.cam is not None)
        if drafting:
            self.bar.sync()
            self.bar.raise_()

    def _panel_closed(self) -> None:
        self._sync_bar()
        self.canvas.set_areas(self.areas())

    def _area_clicked(self, area_id: str, pos: QPointF) -> None:
        a = self.area(area_id)
        if a is None:
            return
        self.canvas.selected_id = area_id
        self.canvas.update()
        self.popover.show_for(a, pos)

    # --- actions ---
    def _after_change(self) -> None:
        self.canvas.set_areas(self.areas())
        self.changed.emit()

    def _save(self) -> bool:
        try:
            self.save_config()
            return True
        except Exception as exc:
            self.win.notify(f"Could not save the areas: {exc}", error=True)
            return False

    def set_enabled(self, area_id: str, on: bool) -> None:
        a = self.area(area_id)
        if a is None:
            return
        a["enabled"] = on
        if self._save():
            self.win.notify(f"“{a.get('name', 'Area')}” is {'on' if on else 'off'}.")
        self._after_change()

    def duplicate(self, area_id: str) -> None:
        a = self.area(area_id)
        if a is None:
            return
        dup = copy.deepcopy(a)
        dup["id"] = new_area_id()
        dup["name"] = default_name(a.get("task", ""), a.get("shape", "polygon"),
                                   [x.get("name", "") for x in self.areas()] + [a.get("name", "")]) \
            if a.get("task") == "parking_spot" else f"{a.get('name', 'Area')} copy"
        dup["color"] = next_color(self.areas())
        self.cam.areas = self.areas() + [dup]
        self._save()
        self._after_change()

    def delete(self, area_id: str) -> bool:
        a = self.area(area_id)
        if a is None:
            return False
        if not delete_area(self.ctx, self.win, self.cam, a, self.save_config):
            return False
        self._after_change()
        return True

    def reset_reference(self, area_id: str) -> None:
        mon = getattr(self.ctx, "monitor", None)
        fn = getattr(mon, "reset_area_reference", None)
        if fn is None or self.cam is None or not self.monitoring():
            self.win.notify("Monitoring is off. The picture is learned again when it starts.")
            return
        try:
            if fn(self.cam.channel, area_id) is False:
                self.win.notify("Monitoring has not looked at this area yet. Try again in a few seconds.")
                return
            self.win.notify("Got it. The area looks like this when everything is normal.")
        except Exception as exc:
            self.win.notify(f"Could not reset the area: {exc}", error=True)

    # --- pictures for the panel ---
    def area_crop(self, points: list):
        """Raw camera picture cropped around the area (with a margin), BGR, or None."""
        frame = self.raw_frame()
        if frame is None or not points:
            return None
        h, w = frame.shape[:2]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        mx, my = (max(xs) - min(xs)) * 0.1 + 0.01, (max(ys) - min(ys)) * 0.1 + 0.01
        x1, x2 = int(max(0.0, min(xs) - mx) * w), int(min(1.0, max(xs) + mx) * w)
        y1, y2 = int(max(0.0, min(ys) - my) * h), int(min(1.0, max(ys) + my) * h)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None
        return frame[y1:y2, x1:x2].copy()

    def plate_readability(self) -> str:
        """'Good', 'Too small' or 'No vehicle' for the vehicle nearest the drawn line."""
        tracks = [t for t in self.canvas.tracks if t[1] in ("car", "truck", "bus", "motorcycle")]
        if not tracks:
            return "No vehicle"
        frame = self.raw_frame()
        if frame is None:
            return "No vehicle"
        try:
            from .plates import plate_quality
        except Exception:
            plate_quality = None
        h, w = frame.shape[:2]
        _tid, _label, x1, y1, x2, y2 = max(tracks, key=lambda t: (t[4] - t[2]) * (t[5] - t[3]))
        crop = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
        if crop.size == 0:
            return "No vehicle"
        if plate_quality is None:
            return "Good" if (x2 - x1) * w >= 240 else "Too small"
        try:
            q = float(plate_quality(crop))
        except Exception:
            return "No vehicle"
        return "Good" if q >= 0.5 else "Too small or blurry"


def thumb_path(site_id: str, channel: int) -> Path:
    return CACHE / f"thumb_{site_id}_{channel}.jpg"


def delete_area(ctx, parent, cam, area: dict, save_fn) -> bool:
    """Confirm, remove the area from its camera, save, then delete its log and pictures."""
    store = getattr(ctx, "alert_store", None)
    n = 0
    lister = getattr(store, "list_area_events", None)
    if lister is not None:
        try:
            n = len(lister(area_id=area.get("id"), limit=100000))
        except Exception:
            n = 0
    name = area.get("name", "Area")
    log = f"Its log ({n} event{'s' if n != 1 else ''}) and pictures are deleted too." if n else \
        "Its log and pictures are deleted too."
    if area.get("alert"):
        log += " Incidents it already raised stay in Incidents."
    if not confirm(parent, "Delete area", f"Delete “{name}”? {log}", "Delete", "Cancel", danger=True):
        return False
    old = cam_areas(cam)
    cam.areas = [a for a in old if a.get("id") != area.get("id")]
    try:
        save_fn()
    except Exception as exc:
        cam.areas = old
        if hasattr(parent, "notify"):
            parent.notify(f"Could not delete the area: {exc}", error=True)
        return False
    deleter = getattr(store, "delete_area_events", None)
    if deleter is not None:
        run_async(deleter, area.get("id"))
    return True


# --- Settings / Sites helpers -----------------------------------------------------------------

def area_log_row(ctx, on_changed=None) -> QWidget:
    """'Area log: keep [30] days' for the clip storage section (saves settings.area_log_days)."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(QLabel("Area log: keep"))
    box = spin_box(1, 365, int(getattr(ctx.cfg.settings, "area_log_days", 30)))
    box.setFixedWidth(90)
    box.setAccessibleName("Days to keep the area log")
    row.addWidget(box)
    row.addWidget(QLabel("days"))
    row.addStretch(1)
    lay.addLayout(row)
    lay.addWidget(muted("Area events and their pictures older than this are deleted automatically, together with "
                        "old clips."))

    def changed(v: int) -> None:
        ctx.cfg.settings.area_log_days = int(v)
        ctx.cfg.save()
        if on_changed:
            on_changed("area_log_days")

    box.valueChanged.connect(changed)
    w.spin = box
    return w


def areas_button(cam) -> QPushButton:
    """Sites & cameras 'Areas' cell: '3 areas', or 'Add area' when there are none."""
    n = len(cam_areas(cam))
    b = button(f"{n} area{'s' if n != 1 else ''}" if n else "Add area", "ghost",
               "Show this camera's areas in Live view" if n else "Draw an area on this camera in Live view")
    return b


# --- Hub: charts ------------------------------------------------------------------------------

class BarChart(QWidget):
    """Per-hour or per-day bars. With two series, 'in' points up and 'out' points down from one
    axis, so they never overlap and need no legend box."""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.buckets: list[tuple[int, int]] = []
        self.labels: list[str] = []
        self.tips: list[str] = []
        self.mirrored = False
        self.highlight = -1
        self.names = ("In", "Out")
        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, buckets, labels, tips, mirrored: bool, highlight: int = -1, names=("In", "Out")) -> None:
        self.buckets, self.labels, self.tips = list(buckets), list(labels), list(tips)
        self.mirrored, self.highlight, self.names = mirrored, highlight, names
        self.setFixedHeight(190 if mirrored else 160)
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(36, 22, self.width() - 44, self.height() - 22 - 22)

    def _slot(self, i: int) -> QRectF:
        r = self._plot()
        n = max(1, len(self.buckets))
        w = r.width() / n
        return QRectF(r.left() + i * w, r.top(), w, r.height())

    def paintEvent(self, event):
        pal = self.ctx.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._plot()
        n = len(self.buckets)
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        fm = QFontMetrics(f)
        muted_c, border = QColor(pal["muted"]), QColor(pal["border"])
        if not n:
            p.end()
            return
        top = max([a for a, _ in self.buckets] + [1])
        bot = max([b for _, b in self.buckets] + [1]) if self.mirrored else 0
        peak = max(top, bot)
        axis = r.top() + r.height() / 2 if self.mirrored else r.bottom()
        up_h = (r.height() / 2 if self.mirrored else r.height()) - 4
        p.setPen(QPen(border, 1))
        p.drawLine(QPointF(r.left(), axis), QPointF(r.right(), axis))
        p.setPen(muted_c)
        p.drawText(QRectF(0, r.top() - 8, 30, 16), Qt.AlignRight | Qt.AlignVCenter, str(peak))
        p.drawText(QRectF(0, axis - 8, 30, 16), Qt.AlignRight | Qt.AlignVCenter, "0")
        # "out" in the muted text colour: a real data colour with enough contrast in both themes
        primary, second = QColor(pal["primary"]), QColor(pal["muted"])
        if self.mirrored:
            p.drawText(QRectF(0, r.bottom() - 8, 30, 16), Qt.AlignRight | Qt.AlignVCenter, str(peak))
            x = r.left()
            for name, color, arrow in ((self.names[0], primary, "↑"), (self.names[1], second, "↓")):
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawRoundedRect(QRectF(x, 5, 10, 10), 2, 2)
                p.setPen(QColor(pal["text"]))
                text = f"{name} {arrow}"
                p.drawText(QRectF(x + 14, 2, fm.horizontalAdvance(text) + 4, 16), Qt.AlignLeft | Qt.AlignVCenter, text)
                x += 14 + fm.horizontalAdvance(text) + 16
        for i, (a, b) in enumerate(self.buckets):
            s = self._slot(i)
            bw = max(2.0, s.width() * 0.62)
            x = s.center().x() - bw / 2
            if i == self.highlight:   # the current hour, faint behind its bars
                hl = QColor(pal["nav_active_bg"])
                hl.setAlpha(90)
                p.setPen(Qt.NoPen)
                p.setBrush(hl)
                p.drawRoundedRect(QRectF(s.left() + 1, r.top(), s.width() - 2, r.height()), 3, 3)
            p.setPen(Qt.NoPen)
            if a:
                h = max(2.0, up_h * a / peak)
                p.setBrush(primary)
                p.drawRoundedRect(QRectF(x, axis - h, bw, h), 2, 2)
            if b and self.mirrored:
                h = max(2.0, up_h * b / peak)
                p.setBrush(second)
                p.drawRoundedRect(QRectF(x, axis + 1, bw, h), 2, 2)
        p.setPen(muted_c)
        step = 3 if n > 12 else 1
        for i in range(0, n, step):
            text = self.labels[i] if i < len(self.labels) else ""
            s = self._slot(i)
            tw = fm.horizontalAdvance(text) + 4
            p.drawText(QRectF(s.center().x() - tw / 2, r.bottom() + 4, tw, 16), Qt.AlignCenter, text)
        p.end()

    def mouseMoveEvent(self, event):
        x = event.position().x()
        for i in range(len(self.buckets)):
            if self._slot(i).left() <= x < self._slot(i).right():
                if i < len(self.tips):
                    QToolTip.showText(event.globalPosition().toPoint(), self.tips[i], self)
                return
        QToolTip.hideText()


class StateStrip(QWidget):
    """One 24 h strip per day: 'on' segments (taken, yes) in a colour, the rest neutral; hour ticks."""

    ROW = 28

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.rows: list[tuple[str, float, list[tuple[float, float, str]]]] = []   # label, day start, segs
        self.color = QColor("#f59e0b")
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_rows(self, rows, color: QColor) -> None:
        self.rows, self.color = rows, color
        self.setFixedHeight(len(rows) * (self.ROW + 6) + 22)
        self.update()

    def _label_w(self) -> int:
        return 70 if len(self.rows) > 1 else 0

    def _bar(self, i: int) -> QRectF:
        lw = self._label_w()
        return QRectF(lw, i * (self.ROW + 6), self.width() - lw - 2, self.ROW)

    def paintEvent(self, event):
        pal = self.ctx.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        now = time.time()
        for i, (text, start, segs) in enumerate(self.rows):
            bar = self._bar(i)
            if self._label_w():
                p.setPen(QColor(pal["muted"]))
                p.drawText(QRectF(0, bar.top(), self._label_w() - 8, bar.height()), Qt.AlignRight | Qt.AlignVCenter,
                           text)
            path = QPainterPath()
            path.addRoundedRect(bar, 5, 5)
            p.save()
            p.setClipPath(path)
            p.fillRect(bar, QColor(pal["surface2"]))
            if start + 86400 > now:   # the future part of today
                fx = bar.left() + bar.width() * max(0.0, (now - start) / 86400)
                fut = QRectF(fx, bar.top(), bar.right() - fx, bar.height())
                p.fillRect(fut, QColor(pal["bg"]))
            for s, t, _tip in segs:
                x1 = bar.left() + bar.width() * (s - start) / 86400
                x2 = bar.left() + bar.width() * (t - start) / 86400
                p.fillRect(QRectF(x1, bar.top(), max(2.0, x2 - x1), bar.height()), self.color)
            p.restore()
            p.setPen(QPen(QColor(pal["border"]), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(bar, 5, 5)
        if self.rows:
            bar = self._bar(len(self.rows) - 1)
            p.setPen(QColor(pal["muted"]))
            for h in range(0, 25, 3):
                x = bar.left() + bar.width() * h / 24
                box = QRectF(x - 16, bar.bottom() + 4, 32, 14)
                align = Qt.AlignCenter
                if h == 0:
                    box, align = QRectF(x, bar.bottom() + 4, 32, 14), Qt.AlignLeft | Qt.AlignVCenter
                elif h == 24:
                    box, align = QRectF(x - 32, bar.bottom() + 4, 32, 14), Qt.AlignRight | Qt.AlignVCenter
                p.drawText(box, align, f"{h:02d}")
        p.end()

    def mouseMoveEvent(self, event):
        pos = event.position()
        for i, (_text, start, segs) in enumerate(self.rows):
            bar = self._bar(i)
            if bar.contains(pos):
                ts = start + 86400 * (pos.x() - bar.left()) / bar.width()
                tip = next((tip for s, t, tip in segs if s <= ts <= t), "")
                if tip:
                    QToolTip.showText(event.globalPosition().toPoint(), tip, self)
                else:
                    QToolTip.hideText()
                return
        QToolTip.hideText()


# --- Hub: list ----------------------------------------------------------------------------------

class AreaThumbs(QObject):
    """Camera pictures for the hub rows (saved by LiveAreas every minute while Live view runs)."""

    loaded = Signal()

    def __init__(self, parent=None, fallback=None):
        super().__init__(parent)
        self._cache: dict[tuple, QImage | None] = {}
        self._loading: set = set()
        self.fallback = fallback   # (site_id, channel) -> picture path when Live view never saved one

    def get(self, site_id: str, channel: int) -> QImage | None:
        key = (site_id, channel)
        if key in self._cache:
            return self._cache[key]
        if key not in self._loading:
            self._loading.add(key)
            run_async(self._load, site_id, channel, on_done=lambda img, key=key: self._done(key, img))
        return None

    def _load(self, site_id: str, channel: int):
        img = load_image(str(thumb_path(site_id, channel)), QSize(320, 180))
        if img is None and self.fallback is not None:
            path = self.fallback(site_id, channel)
            img = load_image(path, QSize(320, 180)) if path else None
        return img

    def put(self, site_id: str, channel: int, img: QImage) -> None:
        self._cache[(site_id, channel)] = img
        self.loaded.emit()

    def _done(self, key, img) -> None:
        self._loading.discard(key)
        self._cache[key] = img if isinstance(img, QImage) else None
        self.loaded.emit()

    def forget(self) -> None:
        self._cache.clear()


def paint_area_thumb(p: QPainter, pal: dict, rect: QRectF, img: QImage | None, area: dict) -> None:
    """Camera picture with the area drawn on it (the hub row picture)."""
    path = QPainterPath()
    path.addRoundedRect(rect, 6, 6)
    p.save()
    p.setClipPath(path)
    p.fillRect(rect, QColor(pal["video_bg"]))
    if img is not None:
        p.drawImage(rect, img)
    color = area_color(area)
    pts = [QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height()) for x, y in area.get("points", [])]
    if area.get("shape") == "line" and len(pts) == 2:
        p.setPen(QPen(color, 2.5, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(pts[0], pts[1])
    elif len(pts) >= 3:
        fill = QColor(color)
        fill.setAlpha(80)
        p.setPen(QPen(color, 1.5))
        p.setBrush(fill)
        p.drawPolygon(QPolygonF(pts))
    if not area.get("enabled", True):
        p.fillRect(rect, QColor(0, 0, 0, 110))
    p.restore()


class _AreaDelegate(QStyledItemDelegate):
    """Hub row: picture with the area, name, task chip, live state, on/off switch."""

    def __init__(self, page: "AreasPage"):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), HEAD_H if index.data(KIND_ROLE) == "head" else ROW_H)

    @staticmethod
    def switch_rect(rect: QRect) -> QRectF:
        return QRectF(rect.right() - 48, rect.center().y() - 11, 40, 22)

    def paint(self, painter, option, index):
        pal = self.page.ctx.palette
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if index.data(KIND_ROLE) == "head":
            f = QFont(option.font)
            f.setPointSizeF(8)
            f.setWeight(QFont.DemiBold)
            painter.setFont(f)
            painter.setPen(QColor(pal["muted"]))
            painter.drawText(QRectF(rect).adjusted(6, 10, -6, 0), Qt.AlignLeft | Qt.AlignVCenter,
                             index.data(Qt.DisplayRole).upper())
            painter.restore()
            return
        area_id = index.data(ID_ROLE)
        info = self.page.lookup.get(area_id)
        if info is None:
            painter.restore()
            return
        site_id, cam, area = info
        card = QRectF(rect).adjusted(0, 3, -2, -3)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        bg = QColor(pal["nav_active_bg"] if selected else pal["surface2"] if hover else pal["surface"])
        painter.setPen(QPen(QColor(pal["primary"] if selected else pal["border"]), 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        pic = QRectF(card.left() + 8, card.center().y() - THUMB.height() / 2, THUMB.width(), THUMB.height())
        paint_area_thumb(painter, pal, pic, self.page.thumbs.get(site_id, cam.channel), area)
        x = pic.right() + 12
        right = self.switch_rect(rect).left() - 10
        f = QFont(option.font)
        f.setWeight(QFont.DemiBold)
        painter.setFont(f)
        fm = QFontMetrics(f)
        painter.setPen(QColor(pal["text"]))
        painter.drawText(QRectF(x, card.top() + 9, right - x, fm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(area.get("name", "Area"), Qt.ElideRight, int(right - x)))
        # task chip
        t = template(area.get("task", "watch_zone"))
        f2 = QFont(option.font)
        f2.setPointSizeF(8.5)
        painter.setFont(f2)
        fm2 = QFontMetrics(f2)
        chip_text = fm2.elidedText(t["name"], Qt.ElideRight, int(right - x - 30))
        cw = fm2.horizontalAdvance(chip_text) + 32
        chip = QRectF(x, card.top() + 13 + fm.height(), cw, fm2.height() + 6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(pal["surface2"] if not selected else pal["surface"]))
        painter.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
        icon = self.page.icon_pm(t["icon"])
        painter.drawPixmap(QPointF(chip.left() + 8, chip.center().y() - 6), icon)
        painter.setPen(QColor(pal["muted"]))
        painter.drawText(chip.adjusted(24, 0, -6, 0), Qt.AlignLeft | Qt.AlignVCenter, chip_text)
        # live state
        live = self.page.live.get(cam.channel, {}).get(area.get("id"))
        state = stat_text(area, live, self.page.watching())
        tone = pal["warning"] if is_active(live) and area.get("alert") else \
            pal["success"] if state in ("Free", "Clear", "Looks normal") else pal["muted"]
        painter.setFont(f2)
        painter.setPen(QColor(tone))
        painter.drawText(QRectF(x, chip.bottom() + 4, right - x, fm2.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         fm2.elidedText(f"{cam.label} · {state}", Qt.ElideRight, int(right - x)))
        self._paint_switch(painter, pal, self.switch_rect(rect), area.get("enabled", True))
        painter.restore()

    @staticmethod
    def _paint_switch(p: QPainter, pal: dict, track: QRectF, on: bool) -> None:
        radius = track.height() / 2
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal["primary"]))
        else:
            p.setPen(QPen(QColor(pal["muted"]), 1))
            p.setBrush(QColor(pal["surface"]))
        p.drawRoundedRect(track, radius, radius)
        knob = track.height() - (6 if on else 8)
        x = track.right() - 4 - knob if on else track.left() + 4
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["on_primary"] if on else pal["muted"]))
        p.drawEllipse(QRectF(x, track.center().y() - knob / 2, knob, knob))


FAINT_ROLE = Qt.UserRole + 6   # log cell shown muted and italic ("Unreadable")
TONE_ROLE = Qt.UserRole + 7    # log cell painted as a badge of this tone


class _BadgeDelegate(QStyledItemDelegate):
    """A log cell painted as a #Badge pill (Resident / Visitor / Unsure read)."""

    def __init__(self, page: "AreasPage"):
        super().__init__(page)
        self.page = page

    def paint(self, painter, option, index):
        pal = self.page.ctx.palette
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(pal["nav_active_bg"]))
        text = index.data(Qt.DisplayRole) or ""
        if text:
            f = QFont(option.font)
            f.setPointSizeF(8.5)
            f.setWeight(QFont.DemiBold)
            fm = QFontMetrics(f)
            fill, fg, outline = _tone_colors(pal, index.data(TONE_ROLE) or "neutral")
            r = QRectF(option.rect.left() + 6, option.rect.center().y() - 10, fm.horizontalAdvance(text) + 18, 20)
            painter.setPen(QPen(outline, 1) if outline is not None else Qt.NoPen)
            painter.setBrush(fill if fill is not None else Qt.NoBrush)
            painter.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
            painter.setFont(f)
            painter.setPen(fg)
            painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()


class _PicDelegate(QStyledItemDelegate):
    """Log table pictures (and plate crops) painted from the page's picture cache."""

    def __init__(self, page: "AreasPage", size: QSize, plate: bool = False):
        super().__init__(page)
        self.page, self.size, self.plate = page, size, plate

    def paint(self, painter, option, index):
        pal = self.page.ctx.palette
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(pal["nav_active_bg"]))
        paths = index.data(Qt.UserRole) or []
        x = option.rect.left() + 6
        y = option.rect.center().y() - self.size.height() / 2
        for path in paths[:2]:
            r = QRectF(x, y, self.size.width(), self.size.height())
            if not path:   # no "before" picture yet: an outlined placeholder
                painter.setPen(QPen(QColor(pal["border_strong"]), 1, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
                f = QFont(option.font)
                f.setPointSizeF(7.5)
                painter.setFont(f)
                painter.setPen(QColor(pal["muted"]))
                painter.drawText(r, Qt.AlignCenter, "no before")
                x += self.size.width() + 4
                continue
            pm = self.page.pic(path, self.size)
            clip = QPainterPath()
            clip.addRoundedRect(r, 4, 4)
            painter.setClipPath(clip)
            painter.fillRect(r, QColor(pal["video_bg"]))
            if pm is not None:
                painter.drawPixmap(r.toRect(), pm)
            painter.setClipping(False)
            x += self.size.width() + 4
        text = index.data(Qt.DisplayRole) or ""
        if text:
            f = QFont(option.font)
            faint = bool(index.data(FAINT_ROLE))
            if self.plate and not faint:
                f.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
                f.setStyleHint(QFont.Monospace)
                f.setWeight(QFont.Bold)
            f.setItalic(faint)
            painter.setFont(f)
            painter.setPen(QColor(pal["muted" if faint else "text"]))
            painter.drawText(QRectF(x + 4, option.rect.top(), option.rect.right() - x - 6, option.rect.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()


# --- Hub page -------------------------------------------------------------------------------------

DATE_PRESETS = (("Today", 1), ("7 days", 7), ("30 days", 30), ("Custom…", 0))


class AreasPage(QWidget):
    """Every area of the active site; the selected one's live numbers, chart or timeline and log."""

    edit_requested = Signal(int, str)      # channel, area id: open Live view and edit
    add_requested = Signal(int)            # channel (-1: the last used or first camera)
    open_incidents = Signal(int)           # channel

    def __init__(self, ctx, win):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.lookup: dict[str, tuple] = {}     # area id -> (site id, camera, area)
        self.live: dict[int, dict] = {}        # channel -> area_live
        self.thumbs = AreaThumbs(self, fallback=self._latest_snapshot)
        self.thumbs.loaded.connect(lambda: self.list.viewport().update())
        self._pics: dict[tuple, QPixmap | None] = {}
        self._pic_loading: set = set()
        self._filter = "all"
        self._fresh: dict[str, float] = {}
        self.player = ClipPlayer(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(16)
        self.header = PageHeader("Areas", "What you asked YardGuard to watch, count and log.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search areas")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        self.search.setAccessibleName("Search areas")
        self.search.textChanged.connect(lambda _t: self.reload())
        self.add_btn = button("Add area", "primary", "Draw an area or a line in Live view")
        ctx.bind_icon(self.add_btn, "plus", "on_primary")
        self.add_btn.clicked.connect(lambda: self.add_requested.emit(-1))
        self.header.actions.addWidget(self.search, 0, Qt.AlignTop)
        self.header.actions.addWidget(self.add_btn, 0, Qt.AlignTop)
        lay.addWidget(self.header)

        self.body = QStackedWidget()
        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        left = QWidget()
        self.left = left
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(6)
        ll.addLayout(self.chips_row)
        self.cam_combo = QComboBox()
        self.cam_combo.setAccessibleName("Camera")
        self.cam_combo.currentIndexChanged.connect(lambda _i: self._combo_filter())
        self.cam_combo.hide()
        ll.addWidget(self.cam_combo)
        self.list = QListWidget()
        self.list.setObjectName("AreaList")
        self.list.setItemDelegate(_AreaDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setAccessibleName("Areas")
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.currentItemChanged.connect(lambda *_: self._selected())
        self.list.itemActivated.connect(lambda item: self._edit(item.data(ID_ROLE)))
        self.list.viewport().installEventFilter(self)
        self.list.installEventFilter(self)
        ll.addWidget(self.list, 1)
        self.no_match = muted("No areas match.")
        self.no_match.setAlignment(Qt.AlignCenter)
        self.no_match.hide()
        ll.addWidget(self.no_match)
        row.addWidget(left)
        self.detail = _AreaDetail(self)
        row.addWidget(self.detail, 1)
        self.body.addWidget(content)
        self.empty = EmptyState(ctx)
        self.body.addWidget(self.empty)
        lay.addWidget(self.body, 1)

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self._glow = QTimer(self)
        self._glow.setInterval(250)
        self._glow.timeout.connect(self._tick_glow)
        self._icons: dict[tuple, QPixmap] = {}
        bridge = getattr(ctx, "alert_bridge", None)
        if bridge is not None and hasattr(bridge, "area_event_added"):
            bridge.area_event_added.connect(self.add_event)
        shortcut(self, "Ctrl+F", lambda: self.isVisible() and (self.search.setFocus(), self.search.selectAll()))

    # --- data -------------------------------------------------------------------------------

    @property
    def store(self):
        return getattr(self.ctx, "alert_store", None)

    def watching(self) -> bool:
        mon = getattr(self.ctx, "monitor", None)
        return bool(mon is not None and getattr(mon, "running", False))

    def cameras(self) -> list:
        site = self.ctx.cfg.active_site()
        return sorted(site.cameras, key=lambda c: c.channel) if site else []

    def area_info(self, area_id: str):
        return self.lookup.get(area_id)

    def _latest_snapshot(self, site_id: str, channel: int) -> str:
        """Thread pool: the newest area event picture of the camera (hub thumbnail fallback)."""
        fn = getattr(self.store, "list_area_events", None)
        try:
            for e in fn(channel=channel, limit=20) if fn else []:
                if getattr(e, "site_id", site_id) in (site_id, "") and e.snapshot_path:
                    return e.snapshot_path
        except Exception:
            pass
        return ""

    def reload(self, select: str | None = None) -> None:
        site = self.ctx.cfg.active_site()
        current = select or self._current_id()
        self.lookup = {}
        cams = [c for c in self.cameras() if cam_areas(c)]
        for c in cams:
            for a in cam_areas(c):
                self.lookup[a.get("id")] = (site.id, c, a)
        self._fill_chips(cams)
        self._refill(current)
        has_cams = bool(self.cameras())
        self.add_btn.setEnabled(has_cams)
        self.add_btn.setToolTip("Draw an area or a line in Live view" if has_cams else
                                "Add a camera in Sites & cameras first")
        if not self.lookup:
            self.empty.configure("scan", "No areas yet",
                                 "Draw an area or a line on a camera picture, then choose what YardGuard should do "
                                 "there: count cars, log number plates, watch a parking spot and more.",
                                 "Draw your first area", lambda: self.add_requested.emit(-1))
            self.empty.action.setEnabled(has_cams)
            self.body.setCurrentIndex(1)
        else:
            self.body.setCurrentIndex(0)
        self._poll_live()

    def _fill_chips(self, cams) -> None:
        while self.chips_row.count():
            item = self.chips_row.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.chip_group = QButtonGroup(self)
        keys = [("all", "All")]
        many = len(cams) > 6
        if not many:
            keys += [(f"cam:{c.channel}", c.label) for c in cams]
        keys.append(("off", "Off"))
        if self._filter not in [k for k, _ in keys] and not self._filter.startswith("cam:"):
            self._filter = "all"
        for key, text in keys:
            chip = QPushButton(text)
            chip.setObjectName("FilterChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setProperty("key", key)
            chip.setChecked(key == self._filter)
            self.chip_group.addButton(chip)
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch(1)
        self.chip_group.buttonClicked.connect(lambda chip: self.set_filter(chip.property("key")))
        self.cam_combo.setVisible(many)
        if many:
            self.cam_combo.blockSignals(True)
            self.cam_combo.clear()
            self.cam_combo.addItem("All cameras", "all")
            for c in cams:
                self.cam_combo.addItem(c.label, f"cam:{c.channel}")
            self.cam_combo.setCurrentIndex(max(0, self.cam_combo.findData(self._filter)))
            self.cam_combo.blockSignals(False)
        width = self.window().width() if self.window() else 1320
        self.left.setFixedWidth(340 if width < 1180 else 400)

    def _combo_filter(self) -> None:
        self._filter = self.cam_combo.currentData() or "all"
        self._refill(self._current_id())

    def set_filter(self, key: str) -> None:
        self._filter = key
        for chip in self.chip_group.buttons():
            chip.setChecked(chip.property("key") == key)
        self._refill(self._current_id())

    def _matches(self, cam, area) -> bool:
        f = self._filter
        if f == "off" and area.get("enabled", True):
            return False
        if f.startswith("cam:") and cam.channel != int(f[4:]):
            return False
        q = self.search.text().strip().lower()
        if q:
            hay = " ".join((area.get("name", ""), template(area.get("task", ""))["name"], cam.label)).lower()
            return q in hay
        return True

    def _refill(self, select: str | None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        first = None
        groups: dict[int, list] = {}
        for _aid, (_sid, cam, area) in self.lookup.items():
            if self._matches(cam, area):
                groups.setdefault(cam.channel, []).append((cam, area))
        for ch in sorted(groups):
            cam = groups[ch][0][0]
            n = len(groups[ch])
            head = QListWidgetItem(f"{cam.label} · {n} area{'s' if n != 1 else ''}")
            head.setData(KIND_ROLE, "head")
            head.setFlags(Qt.NoItemFlags)
            self.list.addItem(head)
            for cam, area in groups[ch]:
                item = QListWidgetItem()
                item.setData(ID_ROLE, area.get("id"))
                item.setData(KIND_ROLE, "area")
                item.setData(Qt.AccessibleTextRole, f"{area.get('name')}, {template(area.get('task'))['name']}")
                self.list.addItem(item)
                if first is None:
                    first = item
                if area.get("id") == select:
                    first = item
                    select = None
        self.list.blockSignals(False)
        self.no_match.setVisible(self.list.count() == 0 and bool(self.lookup))
        if first is not None:
            self.list.setCurrentItem(first)
        self._selected()

    def _current_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(ID_ROLE) if item is not None else None

    def show_area(self, area_id: str) -> None:
        """Select an area (popover 'Open log')."""
        self._filter = "all"
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.reload(select=area_id)

    def _selected(self) -> None:
        aid = self._current_id()
        info = self.lookup.get(aid) if aid else None
        self.player.close()
        self.detail._plate_filter = ""
        self.detail.plate_chip.hide()
        self.detail.show_area(info)

    # --- live ---
    def _poll_live(self) -> None:
        mon = getattr(self.ctx, "monitor", None)
        fn = getattr(mon, "area_live", None)
        self.live = {}
        if fn is not None and self.watching():
            for ch in {cam.channel for _s, cam, _a in self.lookup.values()}:
                try:
                    self.live[ch] = fn(ch) or {}
                except Exception:
                    self.live[ch] = {}
        self.list.viewport().update()

    def _tick(self) -> None:
        if self.isVisible():
            self._poll_live()
            self.detail.tick()

    def add_event(self, e) -> None:
        """area_event_added from the engine: the open log and the tiles follow without a reload."""
        if getattr(e, "area_id", None) not in self.lookup:
            return
        self._fresh[getattr(e, "id", "")] = time.monotonic()
        self._glow.start()
        self.detail.add_event(e)
        self.list.viewport().update()

    def _tick_glow(self) -> None:
        now = time.monotonic()
        self._fresh = {k: t for k, t in self._fresh.items() if now - t < GLOW_S}
        if not self._fresh:
            self._glow.stop()
        self.detail.update_glow()

    # --- pictures ---
    def icon_pm(self, name: str, role: str = "muted", size: int = 12) -> QPixmap:
        key = (name, role, size, id(self.ctx.palette))
        if key not in self._icons:
            self._icons[key] = self.ctx.icon(name, role, size).pixmap(size, size)
        return self._icons[key]

    def pic(self, path: str, size: QSize) -> QPixmap | None:
        key = (path, size.width(), size.height())
        if key in self._pics:
            return self._pics[key]
        if key not in self._pic_loading and path:
            self._pic_loading.add(key)
            run_async(load_image, path, size * 2, True, on_done=lambda img, key=key: self._pic_done(key, img))
        return None

    def _pic_done(self, key, img) -> None:
        self._pic_loading.discard(key)
        pm = None
        if isinstance(img, QImage):
            pm = QPixmap.fromImage(img)
            pm.setDevicePixelRatio(2.0)
        self._pics[key] = pm
        self.detail.table.viewport().update()

    # --- actions ---
    def _edit(self, area_id: str | None) -> None:
        info = self.lookup.get(area_id) if area_id else None
        if info is not None:
            self.edit_requested.emit(info[1].channel, area_id)

    def _save(self) -> bool:
        try:
            self.ctx.save()
            return True
        except Exception as exc:
            self.win.notify(f"Could not save the areas: {exc}", error=True)
            return False

    def toggle(self, area_id: str) -> None:
        info = self.lookup.get(area_id)
        if info is None:
            return
        area = info[2]
        area["enabled"] = not area.get("enabled", True)
        self._save()
        self.list.viewport().update()
        self.detail.show_area(self.lookup.get(self._current_id()) if self._current_id() else None)

    def duplicate(self, area_id: str) -> None:
        info = self.lookup.get(area_id)
        if info is None:
            return
        _sid, cam, area = info
        dup = copy.deepcopy(area)
        dup["id"] = new_area_id()
        dup["name"] = f"{area.get('name', 'Area')} copy"
        dup["color"] = next_color(cam_areas(cam))
        cam.areas = cam_areas(cam) + [dup]
        if self._save():
            self.reload(select=dup["id"])

    def delete(self, area_id: str) -> None:
        info = self.lookup.get(area_id)
        if info is None:
            return
        if delete_area(self.ctx, self.win, info[1], info[2], self.ctx.save):
            self.reload()

    def _list_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None or item.data(KIND_ROLE) != "area":
            return
        aid = item.data(ID_ROLE)
        area = self.lookup[aid][2]
        m = QMenu(self)
        m.addAction("Edit in Live view", lambda: self._edit(aid))
        m.addAction("Duplicate", lambda: self.duplicate(aid))
        m.addAction("Switch off" if area.get("enabled", True) else "Switch on", lambda: self.toggle(aid))
        m.addSeparator()
        m.addAction("Delete…", lambda: self.delete(aid))
        m.exec(self.list.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, event):
        if obj is self.list.viewport() and event.type() == QEvent.MouseButtonRelease:
            item = self.list.itemAt(event.position().toPoint())
            if item is not None and item.data(KIND_ROLE) == "area":
                rect = self.list.visualItemRect(item)
                if _AreaDelegate.switch_rect(rect).contains(event.position()):
                    self.toggle(item.data(ID_ROLE))
                    return True
        if obj is self.list and event.type() == QEvent.KeyPress:
            aid = self._current_id()
            if aid and event.key() == Qt.Key_Space:
                self.toggle(aid)
                return True
            if aid and event.key() == Qt.Key_Delete:
                self.delete(aid)
                return True
        return super().eventFilter(obj, event)

    def export_csv(self, filters: dict, name: str) -> None:
        fn = getattr(self.store, "export_area_events_csv", None)
        if fn is None:
            self.win.notify("Exporting is not available.", error=True)
            return
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip() or "area"
        path, _ = QFileDialog.getSaveFileName(self, "Export area log", str(Path(docs) / f"{safe}.csv"),
                                              "CSV files (*.csv)")
        if not path:
            return
        try:
            fn(path, **filters)
            self.win.notify(f"Area log exported: {path}")
        except Exception as exc:
            self.win.notify(f"Could not export the log: {exc}", error=True)

    def showEvent(self, event):
        super().showEvent(event)
        self.thumbs.forget()
        self.reload()
        self._clock.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._clock.stop()
        self.player.pause()


class _AreaDetail(QScrollArea):
    """Right side of the hub, adapted to the area's task."""

    def __init__(self, page: AreasPage):
        super().__init__()
        self.page, self.ctx = page, page.ctx
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.info = None
        self.area: dict = {}
        self.cam = None
        self._loaded = 0
        host = QWidget()
        host.setObjectName("Transparent")
        self.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label("", "DetailTitle", wrap=True)
        self.sub = muted("")
        # nothing here may push the page sideways (long titles wrap; the log table scrolls itself)
        self.horizontalScrollBar().rangeChanged.connect(lambda *_: self.horizontalScrollBar().setValue(0))
        col.addWidget(self.title)
        col.addWidget(self.sub)
        head.addLayout(col, 1)
        self.edit_btn = button("Edit in Live view")
        self.ctx.bind_icon(self.edit_btn, "pencil")
        self.edit_btn.clicked.connect(lambda: self.page._edit(self.area.get("id")))
        self.more_btn = button("", "ghost", "More actions")
        self.ctx.bind_icon(self.more_btn, "more")
        self.more_btn.clicked.connect(self._menu)
        head.addWidget(self.edit_btn, 0, Qt.AlignTop)
        head.addWidget(self.more_btn, 0, Qt.AlignTop)
        lay.addLayout(head)

        self.banner = QFrame()
        self.banner.setObjectName("Card")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 10, 14, 10)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        self.banner_btn = button("")
        self.banner_btn.clicked.connect(self._banner_action)
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.banner_btn)
        self.banner.hide()
        lay.addWidget(self.banner)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tiles = [_Tile() for _ in range(4)]
        for t in self.tiles:
            tiles.addWidget(t)
        lay.addLayout(tiles)

        self.viz = QFrame()
        self.viz.setObjectName("Card")
        vl = QVBoxLayout(self.viz)
        vl.setContentsMargins(16, 12, 16, 14)
        vl.setSpacing(8)
        self.viz_title = label("", "CardTitle")
        vl.addWidget(self.viz_title)
        self.chart = BarChart(self.ctx)
        self.strip = StateStrip(self.ctx)
        self.viz_note = muted("")
        self.viz_btn = button("Open Incidents")
        self.viz_btn.clicked.connect(lambda: self.cam is not None and self.page.open_incidents.emit(self.cam.channel))
        vl.addWidget(self.chart)
        vl.addWidget(self.strip)
        vl.addWidget(self.viz_note)
        vl.addWidget(self.viz_btn, 0, Qt.AlignLeft)
        lay.addWidget(self.viz)

        self.log = QFrame()
        self.log.setObjectName("Card")
        gl = QVBoxLayout(self.log)
        gl.setContentsMargins(16, 12, 16, 12)
        gl.setSpacing(10)
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        r1.addWidget(label("Log", "CardTitle"))
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search plate or text")
        self.query.setClearButtonEnabled(True)
        self.query.setAccessibleName("Search the log")
        self.query.textChanged.connect(lambda _t: self._query_timer.start())
        self._query_timer = QTimer(self)
        self._query_timer.setSingleShot(True)
        self._query_timer.setInterval(300)
        self._query_timer.timeout.connect(self.load_events)
        r1.addWidget(self.query, 1)
        self.export_btn = button("Export CSV", tooltip="Save this log with the current filters as a CSV file")
        self.ctx.bind_icon(self.export_btn, "download")
        self.export_btn.clicked.connect(self._export)
        r1.addWidget(self.export_btn)
        gl.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.dates = Segmented([(t, "") for t, _ in DATE_PRESETS])
        for b in self.dates.group.buttons():
            b.setProperty("compact", True)
        self.dates.setAccessibleName("Period")
        self.dates.changed.connect(self._dates_changed)
        r2.addWidget(self.dates)
        self.d_from, self.d_to = QDateEdit(), QDateEdit()
        for d in (self.d_from, self.d_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("d MMM yyyy")
            d.setDate(QDate.currentDate())
            d.dateChanged.connect(lambda _d: self.load_all())
            d.hide()
            r2.addWidget(d)
        r2.addStretch(1)
        gl.addLayout(r2)
        self.kind_row = QHBoxLayout()
        self.kind_row.setSpacing(6)
        gl.addLayout(self.kind_row)
        self.plate_chip = QPushButton(" ")
        self.plate_chip.setObjectName("FilterChip")
        self.plate_chip.setCheckable(True)
        self.plate_chip.setChecked(True)
        self.ctx.bind_icon(self.plate_chip, "x", "muted")
        self.plate_chip.setLayoutDirection(Qt.RightToLeft)
        self.plate_chip.clicked.connect(self._clear_plate)
        self.plate_chip.hide()
        gl.addWidget(self.plate_chip, 0, Qt.AlignLeft)

        self.table = QTableWidget()
        self.table.setObjectName("AreaLog")
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.table.itemSelectionChanged.connect(self._row_selected)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._open_file(r))
        gl.addWidget(self.table)
        self.log_empty = QWidget()
        el = QVBoxLayout(self.log_empty)
        el.setContentsMargins(0, 12, 0, 12)
        self.log_empty_text = muted("")
        self.log_empty_text.setAlignment(Qt.AlignCenter)
        self.log_empty_btn = button("")
        self.log_empty_btn.clicked.connect(self._empty_action)
        el.addWidget(self.log_empty_text)
        el.addWidget(self.log_empty_btn, 0, Qt.AlignCenter)
        gl.addWidget(self.log_empty)
        self.older_btn = button("Show older", "ghost")
        self.older_btn.clicked.connect(lambda: self.load_events(more=True))
        gl.addWidget(self.older_btn, 0, Qt.AlignCenter)
        lay.addWidget(self.log)

        self.preview = QFrame()
        self.preview.setObjectName("Card")
        pl = QVBoxLayout(self.preview)
        pl.setContentsMargins(16, 12, 16, 14)
        pl.setSpacing(8)
        ph = QHBoxLayout()
        self.preview_title = label("", "CardTitle", wrap=True)
        ph.addWidget(self.preview_title, 1)
        self.pic_toggle = Segmented([("Close-up", "The area or the vehicle"), ("Whole picture", "The camera picture")])
        for b in self.pic_toggle.group.buttons():
            b.setProperty("compact", True)
        self.pic_toggle.changed.connect(lambda _i: self._show_picture())
        ph.addWidget(self.pic_toggle)
        close = QPushButton()
        close.setProperty("kind", "ghost")
        close.setToolTip("Close")
        close.setCursor(Qt.PointingHandCursor)
        self.ctx.bind_icon(close, "x", "muted")
        close.clicked.connect(self._close_preview)
        ph.addWidget(close)
        pl.addLayout(ph)
        self.image = ImageView("", "Video")
        pl.addWidget(self.image)
        mb = QHBoxLayout()
        self.play_btn = QPushButton()
        self.play_btn.setObjectName("PlayButton")
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setToolTip("Play or pause the clip")
        self.ctx.bind_icon(self.play_btn, "play")
        self.play_btn.clicked.connect(self._toggle_play)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(lambda v: self.page.player.seek_frame(v))
        self.clip_time = _text_label("ClipTime", "", wrap=False)
        self.folder_btn = QPushButton("Show in folder")
        self.folder_btn.setObjectName("LinkButton")
        self.folder_btn.setCursor(Qt.PointingHandCursor)
        self.folder_btn.clicked.connect(lambda: self._open_file(self.table.currentRow()))
        mb.addWidget(self.play_btn)
        mb.addWidget(self.slider, 1)
        mb.addWidget(self.clip_time)
        mb.addWidget(self.folder_btn)
        pl.addLayout(mb)
        self.clip_widgets = (self.play_btn, self.slider, self.clip_time)
        self.preview.hide()
        lay.addWidget(self.preview)
        lay.addStretch(1)
        player = page.player
        player.frame_ready.connect(lambda img: self.image.set_image(img))
        player.position_changed.connect(self._clip_pos)
        player.playing_changed.connect(lambda on: self.ctx.bind_icon(self.play_btn, "pause" if on else "play"))
        self.kind_group = QButtonGroup(self)
        self._kind = ""
        self._plate_filter = ""
        self._events: list = []
        self._stats: list = []
        self._preview_event = None
        self._plain = QStyledItemDelegate(self)
        self._pic_d = _PicDelegate(page, QSize(64, 36))
        self._plate_d = _PicDelegate(page, QSize(72, 24), plate=True)
        self._badge_d = _BadgeDelegate(page)

    # --- filters -----------------------------------------------------------------------------

    def period(self) -> tuple[float, float, int]:
        """(since, until, days) of the chosen period."""
        now = time.time()
        i = self.dates.current()
        if i < 0 or DATE_PRESETS[i][1] == 0:
            a, b = self.d_from.date().toPython(), self.d_to.date().toPython()
            if b < a:
                a, b = b, a
            since = datetime(a.year, a.month, a.day).timestamp()
            until = datetime(b.year, b.month, b.day).timestamp() + 86400
            return since, until, (b - a).days + 1
        days = DATE_PRESETS[i][1]
        since = day_start(now) - 86400 * (days - 1)
        return since, now + 1, days

    def filters(self) -> dict:
        since, until, _days = self.period()
        f = {"area_id": self.area.get("id"), "since": since, "until": until}
        if self._kind:
            f["kind"] = self._kind
        q = self._plate_filter or self.query.text().strip()
        if q:
            f["query"] = q
        return f

    def _dates_changed(self, i: int) -> None:
        custom = DATE_PRESETS[i][1] == 0
        for d in (self.d_from, self.d_to):
            d.setVisible(custom)
        if custom:
            self.d_from.blockSignals(True)
            self.d_from.setDate(QDate.currentDate().addDays(-6))
            self.d_from.blockSignals(False)
        self.load_all()

    def _fill_kinds(self, task: str) -> None:
        while self.kind_row.count():
            item = self.kind_row.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.kind_group = QButtonGroup(self)
        kinds = kinds_for(self.area)
        if len(kinds) <= 1:
            return
        for key in ("",) + tuple(kinds):
            chip = QPushButton(KIND_NAMES.get(key, "All") if key else "All")
            chip.setObjectName("FilterChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setProperty("key", key)
            chip.setChecked(key == self._kind)
            self.kind_group.addButton(chip)
            self.kind_row.addWidget(chip)
        self.kind_row.addStretch(1)
        self.kind_group.buttonClicked.connect(self._kind_clicked)

    def _kind_clicked(self, chip) -> None:
        self._kind = chip.property("key") or ""
        self.load_events()

    # --- showing an area ---------------------------------------------------------------------

    def show_area(self, info) -> None:
        self.info = info
        self._close_preview()
        if info is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        _sid, self.cam, self.area = info
        a, task = self.area, self.area.get("task", "watch_zone")
        t = template(task)
        self.title.setText(a.get("name", "Area"))
        bits = [self.cam.label, t["name"], schedule_text(a.get("schedule"))]
        if a.get("alert"):
            bits.append("Alerts on")
        self.sub.setText(" · ".join(b for b in bits if b and b != "Always"))
        self._kind = ""
        self._fill_kinds(task)
        self.load_all()

    def load_all(self) -> None:
        if not self.area:
            return
        self.load_stats()
        self.load_events()

    def _store_call(self, **f) -> list:
        fn = getattr(self.page.store, "list_area_events", None)
        if fn is None:
            return []
        try:
            return list(fn(**f))
        except Exception as exc:
            self.page.win.notify(f"Could not read the area log: {exc}", error=True)
            return []

    def load_stats(self) -> None:
        since, until, days = self.period()
        task = self.area.get("task")
        # parking / yes-no strips need the state before the period starts
        back = 86400 * 2 if task in ("parking_spot", "ask_ai") else 0
        self._stats = self._store_call(area_id=self.area.get("id"), since=since - back, until=until,
                                       limit=STATS_LIMIT)
        self._render_stats()

    def _render_stats(self) -> None:
        a, task = self.area, self.area.get("task", "watch_zone")
        since, until, days = self.period()
        now = time.time()
        events = [e for e in self._stats if e.ts >= since]
        span = "today" if days == 1 and since == day_start(now) else f"in {days} days" if days > 1 else "that day"
        for t in self.tiles:
            t.show()
        self.chart.hide()
        self.strip.hide()
        self.viz_btn.hide()
        self.viz_note.hide()
        self.viz.show()
        self.log.show()
        live = self.page.live.get(self.cam.channel, {}).get(a.get("id"))
        hourly = days == 1
        n = 24 if hourly else days
        start = since
        labels = [f"{h:02d}" for h in range(24)] if hourly else \
            [(date.fromtimestamp(since) + timedelta(days=i)).strftime("%a %d") for i in range(days)]
        if task == "count":
            both = a.get("shape") == "line" and a.get("direction", "both") == "both"
            buckets = bucket_counts(events, start, n, "hour" if hourly else "day", split=True)
            tin, tout = sum(b[0] for b in buckets), sum(b[1] for b in buckets)
            names = (dir_label(a, "in"), dir_label(a, "out"))
            self.tiles[0].set(str(tin), f"{names[0]} {span}")
            self.tiles[1].set(str(tout), f"{names[1]} {span}")
            if both:
                self.tiles[2].set(str(max(0, tin - tout)), "Now inside (estimate)")
            else:
                self.tiles[2].hide()
            bi = busiest(buckets)
            self.tiles[3].set(labels[bi] + (":00" if hourly else "") if bi >= 0 else "–",
                              "Busiest hour" if hourly else "Busiest day")
            tips = [f"{labels[i]}{':00' if hourly else ''} · {names[0]} {b[0]} · {names[1]} {b[1]}"
                    for i, b in enumerate(buckets)]
            hl = int((now - start) // 3600) if hourly and since == day_start(now) else -1
            self.chart.set_data(buckets, labels, tips, mirrored=True, highlight=hl, names=names)
            self.chart.show()
            self.viz_title.setText("Per hour" if hourly else "Per day")
        elif task == "log_plates":
            s = plate_summary(events, (a.get("params") or {}).get("known", []), now)
            self.tiles[0].set(str(s["vehicles"]), f"Vehicles {span}")
            self.tiles[1].set(str(s["inside"]), "Inside now")
            self.tiles[2].set(str(s["visitors"]), "Visitors")
            self.tiles[3].set(str(s["unsure"]), "Unsure reads")
            self.viz.hide()
        elif task == "parking_spot":
            segs = state_segments(self._stats, ("occupied",), ("freed",), since, until, now)
            taken = sum(t - s for s, t, _e in segs)
            total = max(1.0, min(until, now) - since)
            active = is_active(live) if self.page.watching() else bool(segs and segs[-1][1] >= now - 1)
            if active and segs:
                self.tiles[0].set("Taken", f"for {duration_text(now - segs[-1][0])}")
            else:
                last_free = segs[-1][1] if segs else None
                self.tiles[0].set("Free", f"since {datetime.fromtimestamp(last_free):%H:%M}" if last_free else "Now")
            self.tiles[1].set(f"{round(100 * taken / total)} %", f"Taken {span}")
            longest = max((t - s for s, t, _e in segs), default=0)
            self.tiles[2].set(duration_text(longest) if longest else "–", "Longest stay")
            self.tiles[3].hide()
            self._strip(segs, since, days, area_color(a), lambda s, t, e: occupied_tip(e, s, t))
            self.viz_title.setText("Taken and free")
        elif task == "ask_ai":
            segs = state_segments(self._stats, ("answer_yes",), ("answer_no",), since, until, now)
            q = (a.get("params") or {}).get("question", "")
            state = str((live or {}).get("state", "")).lower()
            last = max(self._stats, key=lambda e: e.ts) if self._stats else None
            now_yes = state == "yes" if state in ("yes", "no") else (last is not None and last.kind == "answer_yes")
            self.tiles[0].set("Yes" if now_yes else "No", f"Now: {q}" if q else "Now")
            checked = (live or {}).get("checked") or (last.ts if last else None)
            self.tiles[1].set(rel_time(checked) if checked else "–", "Last change" if not (live or {}).get("checked")
                              else "Last checked")
            self.tiles[2].set(str(sum(1 for e in events)), f"Changes {span}")
            self.tiles[3].hide()
            self._strip(segs, since, days, QColor(self.ctx.palette["warning"]),
                        lambda s, t, e: f"Yes · {datetime.fromtimestamp(s):%H:%M}–{datetime.fromtimestamp(t):%H:%M}"
                                        f" ({duration_text(t - s)})")
            self.viz_title.setText("Yes and no")
        elif task == "watch_zone":
            for t in self.tiles:
                t.hide()
            self.viz_title.setText("Alerts from this area")
            self.viz_note.setText("Alerts from this area appear in Incidents, together with the clip.")
            self.viz_note.show()
            self.viz_btn.show()
            self.log.hide()
        else:
            durations = [d for d in (event_duration(e) for e in events) if d]
            self.tiles[0].set(str(len(events)), f"Events {span}")
            self.tiles[1].set(stayed_or_dash(max(durations) if durations else None), "Longest")
            last = max(events, key=lambda e: e.ts) if events else None
            self.tiles[2].set(rel_time(last.ts) if last else "–", "Last")
            self.tiles[3].hide()
            buckets = bucket_counts(events, start, n, "hour" if hourly else "day", split=False)
            tips = [f"{labels[i]}{':00' if hourly else ''} · {b[0]} event{'s' if b[0] != 1 else ''}"
                    for i, b in enumerate(buckets)]
            hl = int((now - start) // 3600) if hourly and since == day_start(now) else -1
            self.chart.set_data(buckets, labels, tips, mirrored=False, highlight=hl)
            self.chart.show()
            self.viz_title.setText("Per hour" if hourly else "Per day")
        self._banner()

    def _strip(self, segs, since: float, days: int, color: QColor, tip) -> None:
        rows = []
        for i in range(min(days, 7)):
            d0 = since + 86400 * i if days <= 7 else day_start(time.time()) - 86400 * (6 - i)
            day_segs = [(max(s, d0), min(t, d0 + 86400), tip(s, t, e)) for s, t, e in segs
                        if t > d0 and s < d0 + 86400]
            text = "Today" if d0 == day_start(time.time()) else datetime.fromtimestamp(d0).strftime("%a %d")
            rows.append((text if days > 1 else "", d0, day_segs))
        self.strip.set_rows(rows, color)
        self.strip.show()

    def _banner(self) -> None:
        a = self.area
        if not a.get("enabled", True):
            self._show_banner("This area is switched off.", "Switch on", "enable")
        elif not self.page.watching():
            self._show_banner("Monitoring is off, so this area is not being watched.", "Turn on monitoring", "monitor")
        elif a.get("task") == "ask_ai" and not _brain_ready(self.ctx):
            self._show_banner("The AI brain is not running, so this question is not being checked.",
                              "Open AI brain settings", "brain")
        else:
            self.banner.hide()

    def _show_banner(self, text: str, action: str, key: str) -> None:
        self.banner_text.setText(text)
        self.banner_btn.setText(action)
        self.banner_btn.setProperty("key", key)
        self.banner.show()

    def _banner_action(self) -> None:
        key = self.banner_btn.property("key")
        win = self.page.win
        if key == "enable":
            self.page.toggle(self.area.get("id"))
        elif key == "monitor" and hasattr(win, "set_monitoring"):
            win.set_monitoring(True)
        elif key == "brain" and hasattr(win, "settings"):
            win.go_page(win.settings)

    # --- log -----------------------------------------------------------------------------------

    def columns(self) -> list[str]:
        task = self.area.get("task")
        if task == "log_plates":
            return ["Time", "Plate", "Direction", "Stayed", "Who"]
        if task in ("left_object", "state_watch"):
            return ["Time", "What", "Before / after"]
        return ["Time", "What", "Picture"]   # the sentence already carries plate, direction and duration

    def load_events(self, more: bool = False) -> None:
        if not self.area:
            return
        limit = (self._loaded if more else 0) + LOAD_LIMIT
        events = self._store_call(**self.filters(), limit=limit + 1)
        self.older_btn.setVisible(len(events) > limit)
        self._events = events[:limit]
        self._loaded = len(self._events)
        self._fill_table()

    def _fill_table(self) -> None:
        cols = self.columns()
        t = self.table
        t.clear()
        t.setColumnCount(len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setRowCount(0)
        hh = t.horizontalHeader()
        for i in range(len(cols)):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        task = self.area.get("task")
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in range(5):
            t.setItemDelegateForColumn(c, self._plain)
        if task == "log_plates":
            t.setItemDelegateForColumn(1, self._plate_d)
            t.setItemDelegateForColumn(4, self._badge_d)
            hh.setSectionResizeMode(4, QHeaderView.Fixed)
            hh.resizeSection(4, 120)
        else:
            pic = len(cols) - 1
            t.setItemDelegateForColumn(pic, self._pic_d)
            hh.setSectionResizeMode(pic, QHeaderView.Fixed)
            hh.resizeSection(pic, 150 if task in ("left_object", "state_watch") else 80)
        # plates: "still inside" only on a car's latest sighting
        self._latest = {}
        for e in self._events:
            if e.value:
                self._latest.setdefault(normalise_plate(e.value), getattr(e, "id", ""))
        t.setUpdatesEnabled(False)
        for e in self._events:
            self._insert_row(t.rowCount(), e)
        t.setUpdatesEnabled(True)
        self._fit_table()
        t.setVisible(bool(self._events))
        self._log_empty()

    def _fit_table(self) -> None:
        """At most LOG_ROWS rows tall; the table scrolls inside the page beyond that."""
        t = self.table
        rows = min(t.rowCount(), LOG_ROWS)
        t.setFixedHeight(t.horizontalHeader().sizeHint().height() + rows * 44 + 4)
        t.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if t.rowCount() > LOG_ROWS else Qt.ScrollBarAlwaysOff)

    def _insert_row(self, row: int, e) -> None:
        t = self.table
        t.insertRow(row)
        a = self.area
        task = a.get("task")
        cells: list[tuple[str, list | None]] = [(log_time(e.ts), None)]
        if task == "log_plates":
            known = (a.get("params") or {}).get("known", [])
            who, tone = who_badge(e, known)
            plate = e.value or "Unreadable"
            if (e.confidence or 0) < UNSURE and e.value:
                plate = f"{e.value} ?"   # the same form as the alert titles
            d = event_direction(e)
            arrow = {"in": "↓", "out": "↑"}.get(d, "")
            stayed = plate_stay(e)
            latest = e.value and getattr(self, "_latest", {}).get(normalise_plate(e.value)) == getattr(e, "id", "")
            inside = "still inside" if d == "in" and latest else "–"
            cells += [(plate, [e.crop_path] if getattr(e, "crop_path", "") else []),
                      (f"{dir_label(a, d)} {arrow}".strip() if d else "–", None),
                      (stayed_or_dash(stayed) if stayed is not None else inside, None),
                      (who, None)]
        elif task in ("left_object", "state_watch"):
            before = _extra_get(e, "reference_path") or ""
            after = e.crop_path or e.snapshot_path
            cells += [(event_text(e, a), None), ("", [before, after] if after else [])]
        else:
            cells.append((event_text(e, a), None))
            cells.append(("", [e.crop_path or e.snapshot_path] if (e.crop_path or e.snapshot_path) else []))
        for c, (text, pics) in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setData(Qt.UserRole + 5, getattr(e, "id", ""))
            if pics is not None:
                item.setData(Qt.UserRole, pics)
                if c == 1 and task == "log_plates":
                    item.setToolTip(f"Show all sightings of {e.value}" if e.value else "")
            if task == "log_plates" and c == 1 and not e.value:
                item.setData(FAINT_ROLE, True)
            if task == "log_plates" and c == 4:
                item.setData(TONE_ROLE, tone)
            t.setItem(row, c, item)

    def _log_empty(self) -> None:
        if self._events:
            self.log_empty.hide()
            return
        a = self.area
        filtered = bool(self._kind or self.query.text().strip() or self._plate_filter)
        if filtered:
            text, action, key = "No events match these filters.", "Clear filters", "clear"
        elif not a.get("enabled", True):
            text, action, key = "This area is switched off.", "Switch on", "enable"
        elif not self.page.watching():
            text, action, key = "Monitoring is off, so this area is not being watched.", "Turn on monitoring", "monitor"
        else:
            text, action, key = "Nothing logged yet. Events show up here as they happen.", "", ""
        self.log_empty_text.setText(text)
        self.log_empty_btn.setText(action)
        self.log_empty_btn.setProperty("key", key)
        self.log_empty_btn.setVisible(bool(action))
        self.log_empty.show()

    def _empty_action(self) -> None:
        key = self.log_empty_btn.property("key")
        if key == "clear":
            self._kind = ""
            self._plate_filter = ""
            self.plate_chip.hide()
            self.query.blockSignals(True)
            self.query.clear()
            self.query.blockSignals(False)
            for chip in self.kind_group.buttons():
                chip.setChecked(chip.property("key") == "")
            self.load_events()
        else:
            self.banner_btn.setProperty("key", key)
            self._banner_action()

    def add_event(self, e) -> None:
        if not self.area or getattr(e, "area_id", None) != self.area.get("id"):
            return
        since, until, _d = self.period()
        self._stats.append(e)
        self._render_stats()
        if not (since <= e.ts <= until + 60) or (self._kind and e.kind != self._kind) \
                or self._plate_filter or self.query.text().strip():
            return
        self._events.insert(0, e)
        if e.value:
            self._latest = getattr(self, "_latest", {})
            self._latest[normalise_plate(e.value)] = getattr(e, "id", "")
        self.table.setVisible(True)
        self._insert_row(0, e)
        self._fit_table()
        self.update_glow()
        self._log_empty()

    def update_glow(self) -> None:
        now = time.monotonic()
        fresh = self.page._fresh
        glow = QColor(self.ctx.palette["nav_active_bg"])
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is None:
                continue
            on = item.data(Qt.UserRole + 5) in fresh and now - fresh[item.data(Qt.UserRole + 5)] < GLOW_S
            for c in range(self.table.columnCount()):
                it = self.table.item(r, c)
                if it is not None:
                    it.setBackground(glow if on else QColor(0, 0, 0, 0))

    def tick(self) -> None:
        if self.area and self.area.get("task") in ("parking_spot", "ask_ai"):
            self._render_stats()

    def _event_at(self, row: int):
        return self._events[row] if 0 <= row < len(self._events) else None

    def _cell_clicked(self, row: int, col: int) -> None:
        e = self._event_at(row)
        if e is not None and self.area.get("task") == "log_plates" and col == 1 and e.value:
            self._plate_filter = e.value
            self.plate_chip.setText(f" All sightings of {e.value}")
            self.plate_chip.setToolTip("Clear this filter")
            self.plate_chip.show()
            self.load_events()

    def _clear_plate(self) -> None:
        self._plate_filter = ""
        self.plate_chip.hide()
        self.load_events()

    def _row_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        e = self._event_at(row)
        if e is None:
            return
        m = QMenu(self)
        if self.area.get("task") == "log_plates" and e.value:
            known = (self.area.get("params") or {}).get("known", [])
            plate = normalise_plate(e.value)
            if plate in {normalise_plate(k) for k in known}:
                m.addAction("Remove from known plates", lambda: self._set_known(plate, False))
            else:
                m.addAction("Add to known plates", lambda: self._set_known(plate, True))
            m.addAction(f"All sightings of {e.value}", lambda: self._cell_clicked(row, 1))
        m.addAction("Show in folder", lambda: self._open_file(row))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _set_known(self, plate: str, on: bool) -> None:
        params = self.area.setdefault("params", {})
        known = [normalise_plate(k) for k in params.get("known", [])]
        known = (known + [plate]) if on and plate not in known else [k for k in known if k != plate or on]
        params["known"] = known
        if self.page._save():
            self.page.win.notify(f"{plate} {'added to' if on else 'removed from'} the known plates.")
        self._render_stats()
        self._fill_table()

    def _row_selected(self) -> None:
        e = self._event_at(self.table.currentRow())
        if e is None:
            return
        self._preview_event = e
        self.preview_title.setText(f"{event_text(e, self.area)} · {day_label(e.ts)} "
                                   f"{datetime.fromtimestamp(e.ts):%H:%M}")
        has_clip = bool(getattr(e, "clip_path", "") and Path(e.clip_path).exists())
        for w in self.clip_widgets:
            w.setVisible(has_clip)
        self.pic_toggle.setVisible(bool(e.crop_path and e.snapshot_path))
        self.preview.show()
        self.page.player.close()
        if has_clip:
            offset = getattr(e, "clip_offset_s", None)
            offset = float(offset if isinstance(offset, (int, float)) and offset >= 0 else
                           _extra_get(e, "clip_offset_s") or 0.0)
            best = _extra_get(e, "best_offset_s")  # plates: where the plate is best seen
            offset = float(best) if isinstance(best, (int, float)) and best >= 0 else offset
            self.page.player.open(e.clip_path, offset, play=False)
        else:
            self._show_picture()
        QTimer.singleShot(0, self._scroll_to_preview)

    def _scroll_to_preview(self) -> None:
        bar = self.verticalScrollBar()
        bottom = self.preview.geometry().bottom() + 12 - self.viewport().height()
        if bottom > bar.value():
            bar.setValue(min(bar.maximum(), max(bottom, 0)))

    def _show_picture(self) -> None:
        e = self._preview_event
        if e is None:
            return
        close = self.pic_toggle.current() == 0
        path = (e.crop_path or e.snapshot_path) if close else (e.snapshot_path or e.crop_path)
        img = load_image(path, QSize(1280, 720)) if path else None
        self.image.set_image(img, "No picture was saved")
        self.page.player.pause()

    def _close_preview(self) -> None:
        self.preview.hide()
        self._preview_event = None
        self.page.player.close()

    def _toggle_play(self) -> None:
        self.page.player.toggle()

    def _clip_pos(self, index: int, count: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, count - 1))
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        fps = self.page.player.fps or 8.0
        self.clip_time.setText(f"{clock_text(index / fps)} / {clock_text(count / fps)}")

    def _open_file(self, row: int) -> None:
        e = self._event_at(row)
        if e is None:
            return
        for path in (getattr(e, "clip_path", ""), e.snapshot_path, e.crop_path):
            if path and show_in_explorer(path):
                return
        self.page.win.notify("The picture of this event was deleted.", error=True)

    def _export(self) -> None:
        f = self.filters()
        self.page.export_csv(f, self.area.get("name", "area"))

    def _menu(self) -> None:
        aid = self.area.get("id")
        if not aid:
            return
        m = QMenu(self)
        m.addAction("Duplicate", lambda: self.page.duplicate(aid))
        m.addAction("Switch off" if self.area.get("enabled", True) else "Switch on", lambda: self.page.toggle(aid))
        if self.area.get("task") in ("left_object", "state_watch"):
            m.addAction("This is how it should look", self._reset_reference)
        m.addSeparator()
        m.addAction("Delete…", lambda: self.page.delete(aid))
        m.exec(QCursor.pos())

    def _reset_reference(self) -> None:
        fn = getattr(getattr(self.ctx, "monitor", None), "reset_area_reference", None)
        if fn is None or not self.page.watching():
            self.page.win.notify("Monitoring is off. The picture is learned again when it starts.")
            return
        if fn(self.cam.channel, self.area.get("id")) is False:
            self.page.win.notify("Monitoring has not looked at this area yet. Try again in a few seconds.")
            return
        self.page.win.notify("Got it. The area looks like this when everything is normal.")


def _brain_ready(ctx) -> bool:
    brain = getattr(ctx, "brain", None)
    try:
        return brain is not None and brain.status() in ("ready", "busy")
    except Exception:
        return False


def _extra_get(e, key: str):
    return _extra(e).get(key)


def dir_label(area: dict, direction: str) -> str:
    params = area.get("params") or {}
    return (params.get("in_name") or "In") if direction == "in" else (params.get("out_name") or "Out") \
        if direction == "out" else ""


def log_time(ts: float, now: float | None = None) -> str:
    """'Today 14:02', 'Yesterday 09:10', 'Wed 21 Oct 18:30'."""
    label = day_label(ts, now)
    dt = datetime.fromtimestamp(ts)
    if label in ("Today", "Yesterday"):
        return f"{label} {dt:%H:%M}"
    return f"{dt:%a} {dt.day} {dt:%b %H:%M}"


def stayed_or_dash(seconds) -> str:
    return short_duration(float(seconds)) if isinstance(seconds, (int, float)) and seconds > 0 else "–"


def occupied_tip(e, s: float, t: float) -> str:
    who = obj_word(getattr(e, "label", "") or "vehicle").capitalize()
    plate = _extra_get(e, "plate") or ""
    return f"{who}{' ' + plate if plate else ''} · {datetime.fromtimestamp(s):%H:%M}–" \
           f"{datetime.fromtimestamp(t):%H:%M} ({duration_text(t - s)})"
