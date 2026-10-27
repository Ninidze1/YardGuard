"""Incidents page: related alerts told as one story per event (timeline, pictures, AI verdict),
plus the dashboard's "Open incidents" strip.

Incidents and alerts come from the engine (yardguard.engine.Incident / Alert / AlertStore); like
alerts_ui, this module only reads their attributes. The old one-row-per-alert list stays
reachable through the "Incidents | All alerts" toggle at the top of the page.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout,
    QWidget,
)

from .alerts_ui import AlertsPage, _mix, _tone_colors, load_image, rel_time, show_in_explorer
from .clip_player import ClipPlayer
from .qt_util import run_async, set_prop
from .widgets import EmptyState, ImageView, PageHeader, button, label, muted

FILTERS = (
    ("open", "Open", "Incidents you have not dealt with yet"),
    ("handled", "Handled", "Incidents you marked as handled"),
    ("false_alarm", "False alarms", "Incidents you marked as false alarms"),
    ("dismissed", "Dismissed by AI", "The AI brain found nobody there: kept here instead of in Open"),
    ("all", "All", "Every incident"),
)
ID_ROLE = Qt.UserRole + 1
KIND_ROLE = Qt.UserRole + 2      # "incident" | "day"
CARD_H = 116
DAY_H = 36
COVER = QSize(116, 76)
FILM = QSize(104, 62)
LOAD_LIMIT = 500
GLOW_S = 4.0
RULE_ICONS = {"night_presence": "moon", "loitering": "clock", "prowling": "route", "car_moved": "car",
              "brain_detect": "scan"}


def rule_icon(rule: str) -> str:
    """Icon of an alert rule; area alerts ("area_<task>") use their task's icon."""
    if rule in RULE_ICONS:
        return RULE_ICONS[rule]
    if rule.startswith("area_"):
        from .areas_canvas import TEMPLATE_UI  # also registers the area icons
        return TEMPLATE_UI.get(rule[5:], {}).get("icon", "pentagon")
    return "shield"
SAVING_S = 300.0   # a clip still missing this long after its alert is not coming (app closed while recording)

EXTRA_QSS = """
#Segmented { background: $surface2; border: 1px solid $border; border-radius: 8px; }
QPushButton#SegButton {
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 4px 16px; min-height: 20px; color: $muted; font-weight: 600;
}
QPushButton#SegButton:hover { color: $text; }
QPushButton#SegButton:checked { background: $surface; border-color: $border_strong; color: $text; }
QPushButton#SegButton:focus { border-color: $primary; }
QListWidget#IncidentList { background: transparent; border: none; padding: 0 6px 0 0; }
#IncidentList::item { padding: 0; margin: 0; border: none; background: transparent; }
#TimelineEntry { background: transparent; border: 1px solid transparent; border-radius: 8px; }
#TimelineEntry:hover { background: $surface2; }
#TimelineEntry[selected="true"] { background: $nav_active_bg; }
#TimelineEntry:focus { border-color: $primary; }
#TimelineTitle { font-weight: 600; }
#TimelineTime { color: $muted; font-size: 9pt; }
#TimelineThumb, #FilmstripHost { background: transparent; }
#TimelineThumb { border-radius: 6px; }
#MomentCaption { color: $muted; }
#StripItem { background: $surface; border: 1px solid $border; border-radius: 10px; }
#StripItem:hover, #StripItem:focus { border-color: $primary; }
#SevBar { background: $primary; border-radius: 2px; }
#SevBar[severity="critical"] { background: $danger; }
#SevBar[severity="warning"] { background: $warning; }
#SevBar[severity="muted"] { background: $border_strong; }
#StripCaption { color: $muted; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }
QPushButton#LinkButton { background: transparent; border: none; color: $primary; padding: 2px 4px; font-weight: 600; }
QPushButton#LinkButton:hover { color: $primary_hover; text-decoration: underline; }
QPushButton#LinkButton:focus { border: 1px solid $primary; border-radius: 4px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $border_strong; border-radius: 4px; min-width: 30px; }
#MediaBar { background: transparent; }
QPushButton#PlayButton {
    background: $surface2; border: 1px solid $border; border-radius: 16px; padding: 0;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
}
QPushButton#PlayButton:hover { border-color: $border_strong; }
QPushButton#PlayButton:focus { border-color: $primary; }
#ClipTime { color: $muted; }
#ClipNote { color: $muted; }
QPushButton#SegButton[compact="true"] { padding: 2px 10px; }
"""


# --- Plain-language helpers ------------------------------------------------------

def incident_badge(inc) -> tuple[str, str]:
    """(text, tone) of the verdict badge; tones match #Badge[tone=...] in alerts_ui."""
    if inc.status == "false_alarm":
        return "False alarm", "neutral"
    return {  # red only when the AI also saw suspicious behaviour (see alerts_ui.verdict_badge)
        "confirmed": ("Confirmed by AI", "danger" if inc.severity == "critical" else "primary"),
        "pending": ("Checking…", "primary"),
        "rejected": ("Dismissed by AI", "muted"),
    }.get(inc.verdict, ("Not verified", "neutral"))


def is_quiet(inc) -> bool:
    """Nothing to worry about: handled, a false alarm or dismissed by the AI. Shown stepped back."""
    return inc.status != "open" or inc.verdict == "rejected"


def incident_bucket(inc) -> str:
    """The filter chip an incident belongs to: open ones the AI dismissed wait under "dismissed"."""
    if inc.status == "open" and inc.verdict == "rejected":
        return "dismissed"
    return inc.status


def severity_color(p: dict, severity: str) -> QColor:
    return QColor(p["danger"] if severity == "critical" else p["warning"] if severity == "warning" else p["primary"])


def duration_text(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "under 1 min"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h" + (f" {minutes % 60} min" if minutes % 60 else "")


def time_range(inc) -> str:
    """'02:14–02:19 · 5 min', or just '02:14' for a single moment."""
    start, end = datetime.fromtimestamp(inc.start_ts), datetime.fromtimestamp(inc.end_ts)
    if inc.alert_count <= 1 or inc.end_ts - inc.start_ts < 30:
        return f"{start:%H:%M}"
    return f"{start:%H:%M}–{end:%H:%M} · {duration_text(inc.end_ts - inc.start_ts)}"


def day_label(ts: float, now: float | None = None) -> str:
    d = date.fromtimestamp(ts)
    today = date.fromtimestamp(time.time() if now is None else now)
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    dt = datetime.fromtimestamp(ts)
    return f"{dt:%A} {dt.day} {dt:%B}" + (f" {dt.year}" if dt.year != today.year else "")


def moment_text(a) -> str:
    """One alert of an incident in plain words: 'Arrived at night', 'Visited 3 cars'..."""
    near_car = "car" in (a.title or "").lower()
    if a.rule == "night_presence":
        return "Arrived at night"
    if a.rule == "loitering":
        m = re.search(r"Stayed (.+?) in\b", a.detail or "")
        where = " next to a car" if near_car else ""
        return f"Stayed {m.group(1)}{where}" if m else f"Lingered{where}"
    if a.rule == "prowling":
        m = re.search(r"close to (\d+)", a.detail or "")
        return f"Visited {m.group(1)} cars" if m else "Went from car to car"
    if a.rule == "brain_detect":
        found = next((f for f in a.brain_findings or [] if f), "") or (a.title or "").replace("AI found: ", "")
        return f"AI spotted: {found}" if found else "AI spotted something"
    return a.title or a.rule.replace("_", " ").capitalize()


def ai_text(a) -> str:
    if a.status == "false_alarm":
        return "You marked it as a false alarm"
    found = [f for f in a.brain_findings or [] if f]
    return {
        "confirmed": "AI confirmed: " + ", ".join(found) if found else "AI confirmed it",
        "pending": "AI is checking…",
        "rejected": "AI found nothing there",
    }.get(a.verdict, "AI could not judge it: " + found[0] if found else "Not checked by the AI")


def clip_of(a) -> str:
    """The alert's clip file, or "" when it has none (or the file is gone)."""
    path = getattr(a, "clip_path", "") or ""
    return path if path and Path(path).exists() else ""


def clip_saving(a, now: float | None = None) -> bool:
    """A clip is planned for this alert (offset known) but not written yet."""
    now = time.time() if now is None else now
    return (not getattr(a, "clip_path", "") and getattr(a, "clip_offset_s", -1.0) >= 0
            and now - a.ts < SAVING_S)


def clock_text(seconds: float) -> str:
    s = max(0, int(seconds + 0.5))
    return f"{s // 60}:{s % 60:02d}"


def collage_alerts(alerts: list, cover_id: str, limit: int = 3) -> list:
    """Up to `limit` alerts for the card picture: the cover first, then other cameras, then the rest."""
    cover = next((a for a in alerts if a.id == cover_id), alerts[0] if alerts else None)
    if cover is None:
        return []
    picked, cams = [cover], {cover.channel}
    for a in alerts:
        if len(picked) < limit and a.channel not in cams:
            picked.append(a)
            cams.add(a.channel)
    for a in alerts:
        if len(picked) < limit and a not in picked:
            picked.append(a)
    return picked


def wrap_lines(fm: QFontMetrics, text: str, width: int, max_lines: int = 2) -> list[str]:
    """Word-wrap text into at most max_lines lines; only the last one is shortened with '…'."""
    lines, words = [], text.split(" ")
    while words and len(lines) < max_lines - 1:
        line = words[0]
        rest = words[1:]
        while rest and fm.horizontalAdvance(f"{line} {rest[0]}") <= width:
            line = f"{line} {rest.pop(0)}"
        if fm.horizontalAdvance(line) > width:
            break  # one very long word: let the last line elide it
        lines.append(line)
        words = rest
    if words:
        lines.append(fm.elidedText(" ".join(words), Qt.ElideRight, width))
    return lines


def _collage_slots(rect: QRect, n: int, gap: int = 2) -> list[QRect]:
    if n <= 1:
        return [rect]
    if n == 2:
        w = (rect.width() - gap) // 2
        return [QRect(rect.left(), rect.top(), w, rect.height()),
                QRect(rect.left() + w + gap, rect.top(), rect.width() - w - gap, rect.height())]
    w = int(rect.width() * 0.6)
    h = (rect.height() - gap) // 2
    rx = rect.left() + w + gap
    return [QRect(rect.left(), rect.top(), w, rect.height()),
            QRect(rx, rect.top(), rect.right() - rx + 1, h),
            QRect(rx, rect.top() + h + gap, rect.right() - rx + 1, rect.height() - h - gap)]


def _badge(painter: QPainter, p: dict, rect: QRectF, text: str, tone: str, font: QFont,
           avoid: QColor | None = None) -> None:
    bg, fg, outline = _tone_colors(p, tone)
    if avoid is not None and bg is not None and bg == avoid:
        bg = QColor(p["surface"])  # keep the badge visible on a fill of the same colour
    painter.setPen(QPen(outline, 1) if outline else Qt.NoPen)
    painter.setBrush(bg if bg else Qt.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), rect.height() / 2, rect.height() / 2)
    painter.setFont(font)
    painter.setPen(fg)
    painter.drawText(rect, Qt.AlignCenter, text)


# --- Pictures ----------------------------------------------------------------------

class Pictures(QObject):
    """Alert crops scaled to cover a size, loaded off the GUI thread and shared by every widget."""

    loaded = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cache: dict[tuple, QPixmap | None] = {}
        self._loading: set[tuple] = set()

    def get(self, alert, size: QSize) -> QPixmap | None:
        path = alert.crop_path or alert.snapshot_path
        key = (alert.id, path, size.width(), size.height())
        if key in self._cache:
            return self._cache[key]
        if key not in self._loading:
            self._loading.add(key)
            run_async(load_image, path, size * 2, True, on_done=lambda img, key=key: self._done(key, img))
        return None

    def _done(self, key: tuple, img) -> None:
        self._loading.discard(key)
        pm = None
        if isinstance(img, QImage):
            pm = QPixmap.fromImage(img)
            pm.setDevicePixelRatio(2.0)
        self._cache[key] = pm
        self.loaded.emit()

    def drop(self, alert_id: str) -> None:
        self._cache = {k: v for k, v in self._cache.items() if k[0] != alert_id}


def paint_picture(painter: QPainter, p: dict, rect: QRect, pm: QPixmap | None, radius: float = 6,
                  faded: bool = False) -> None:
    """A rounded picture, or a dark placeholder while it loads."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)
    painter.save()
    painter.setClipPath(path)
    painter.fillRect(rect, QColor(p["video_bg"]))
    if pm is not None:
        painter.setOpacity(0.5 if faded else 1.0)
        painter.drawPixmap(rect, pm)
    painter.restore()


def paint_play_badge(painter: QPainter, p: dict, rect: QRectF) -> None:
    """Small round "has a clip" mark on a picture."""
    painter.save()
    bg = QColor(p["video_bg"])
    bg.setAlphaF(0.72)
    painter.setPen(Qt.NoPen)
    painter.setBrush(bg)
    painter.drawEllipse(rect)
    c, s = rect.center(), rect.width() * 0.2
    tri = QPainterPath()
    tri.moveTo(c.x() - s * 0.8, c.y() - s)
    tri.lineTo(c.x() + s * 1.1, c.y())
    tri.lineTo(c.x() - s * 0.8, c.y() + s)
    tri.closeSubpath()
    painter.setBrush(QColor(p["on_primary"]))
    painter.drawPath(tri)
    painter.restore()


# --- Incident list -------------------------------------------------------------------

class _IncidentDelegate(QStyledItemDelegate):
    """Paints incident cards and the day headers between them."""

    def __init__(self, page: "IncidentsPage"):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), DAY_H if index.data(KIND_ROLE) == "day" else CARD_H)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if index.data(KIND_ROLE) == "day":
            self._paint_day(painter, option, index)
        else:
            inc = self.page.incidents.get(index.data(ID_ROLE))
            if inc is not None:
                self._paint_card(painter, option, inc)
        painter.restore()

    def _paint_day(self, painter: QPainter, option, index) -> None:
        p = self.page.ctx.palette
        font = QFont(option.font)
        font.setWeight(QFont.DemiBold)
        font.setPointSizeF(option.font.pointSizeF() * 0.92)
        painter.setFont(font)
        painter.setPen(QColor(p["muted"]))
        r = option.rect.adjusted(6, 10, -6, -4)
        painter.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, index.data(Qt.DisplayRole))
        count = index.data(Qt.UserRole + 3) or ""
        painter.drawText(r, Qt.AlignRight | Qt.AlignVCenter, count)

    def _paint_card(self, painter: QPainter, option, inc) -> None:
        page, p = self.page, self.page.ctx.palette
        r = option.rect.adjusted(1, 4, -1, -4)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        fill = QColor(p["nav_active_bg"] if selected else p["surface"])
        glow = page.glow(inc.id)
        if glow > 0 and not selected:  # just arrived: a soft tint that fades out
            fill = _mix(p["warning"], fill.name(), 0.14 * glow)
        border = QColor(p["primary"] if selected else p["border_strong"] if hover else p["border"])
        painter.setPen(QPen(border, 1.5 if selected else 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        if selected and page.list.hasFocus():
            painter.setPen(QPen(QColor(p["primary"]), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(r).adjusted(2.5, 2.5, -2.5, -2.5), 8, 8)

        quiet = is_quiet(inc)
        bar = QColor(p["border_strong"]) if quiet else severity_color(p, inc.severity)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bar)
        painter.drawRoundedRect(QRectF(r.left() + 8, r.top() + 12, 4, r.height() - 24), 2, 2)

        cover = QRect(r.left() + 20, r.top() + (r.height() - COVER.height()) // 2, COVER.width(), COVER.height())
        self._paint_cover(painter, p, cover, inc, quiet)

        x, right = cover.right() + 14, r.right() - 12
        base = QFont(option.font)
        title_font = QFont(base)
        title_font.setWeight(QFont.Bold if page.unread(inc.id) else QFont.DemiBold)
        meta_font = QFont(base)
        meta_font.setPointSizeF(base.pointSizeF() * 0.9)
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.82)
        badge_font = QFont(small)
        badge_font.setWeight(QFont.DemiBold)
        tf, mf, sf, bf = (QFontMetrics(f) for f in (title_font, meta_font, small, badge_font))
        chip_h = sf.height() + 4
        tx = x + (13 if page.unread(inc.id) else 0)
        lines = wrap_lines(tf, inc.title, right - tx)
        block = tf.height() * len(lines) + 4 + mf.height() + 8 + chip_h
        top = r.top() + (r.height() - block) // 2

        if page.unread(inc.id):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["primary"]))
            painter.drawEllipse(QRectF(x, top + tf.height() / 2 - 3.5, 7, 7))
        painter.setFont(title_font)
        painter.setPen(QColor(p["muted"] if quiet else p["text"]))
        for i, line in enumerate(lines):
            painter.drawText(QRect(tx, top + i * tf.height(), right - tx, tf.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, line)

        y2 = top + tf.height() * len(lines) + 4
        count = f"{inc.alert_count} alert{'s' if inc.alert_count != 1 else ''}"
        if inc.status == "handled":
            count = "Handled · " + count
        painter.setFont(meta_font)
        painter.setPen(QColor(p["muted"]))
        cw = mf.horizontalAdvance(count)
        painter.drawText(QRect(right - cw, y2, cw, mf.height()), Qt.AlignRight | Qt.AlignVCenter, count)
        painter.drawText(QRect(x, y2, right - cw - 10 - x, mf.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         mf.elidedText(time_range(inc), Qt.ElideRight, right - cw - 10 - x))

        # Third line: camera chips on the left, the verdict badge on the right.
        y3 = y2 + mf.height() + 8
        text, tone = incident_badge(inc)
        bw = bf.horizontalAdvance(text) + 16
        _badge(painter, p, QRectF(right - bw, y3, bw, chip_h), text, tone, badge_font,
               avoid=fill if selected else None)
        self._paint_chips(painter, p, QRect(x, y3, right - bw - 8 - x, chip_h), inc.cameras, small, sf, selected)

    def _paint_cover(self, painter: QPainter, p: dict, rect: QRect, inc, faded: bool) -> None:
        parts = collage_alerts(self.page.members.get(inc.id, []), inc.cover_alert_id)
        slots = _collage_slots(rect, len(parts))
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 7, 7)
        painter.save()
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(p["video_bg"]))
        painter.setOpacity(0.5 if faded else 1.0)  # quiet incidents stay listed, but step back
        for a, slot in zip(parts, slots):
            pm = self.page.pictures.get(a, slot.size())
            if pm is not None:
                painter.drawPixmap(slot, pm)
        painter.restore()

    @staticmethod
    def _paint_chips(painter: QPainter, p: dict, rect: QRect, names: list[str], font: QFont,
                     fm: QFontMetrics, selected: bool) -> None:
        painter.setFont(font)
        x = rect.left()
        for i, name in enumerate(names):
            later = len(names) - i - 1
            reserve = fm.horizontalAdvance(f"+{later}") + 8 if later else 0
            room = rect.right() - x - reserve
            if room < 56 and i > 0:  # no space for another chip: say how many cameras are left
                painter.setPen(QColor(p["muted"]))
                painter.drawText(QRect(x, rect.top(), rect.right() - x, rect.height()),
                                 Qt.AlignLeft | Qt.AlignVCenter, f"+{len(names) - i}")
                return
            text = fm.elidedText(name, Qt.ElideRight, max(24, min(room - 14, 150)))
            w = fm.horizontalAdvance(text) + 14
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["surface"] if selected else p["surface2"]))
            painter.drawRoundedRect(QRectF(x, rect.top(), w, rect.height()), rect.height() / 2, rect.height() / 2)
            painter.setPen(QColor(p["muted"]))
            painter.drawText(QRect(x, rect.top(), w, rect.height()), Qt.AlignCenter, text)
            x += w + 6


# --- Detail pane: filmstrip, timeline ----------------------------------------------

class _Filmstrip(QWidget):
    """Every alert's crop in a row; click or Left / Right to pick one."""

    picked = Signal(int)

    GAP = 8

    def __init__(self, pane: "_IncidentDetail"):
        super().__init__()
        self.pane = pane
        self.alerts: list = []
        self.current = 0
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Pictures of this incident")
        self.setObjectName("FilmstripHost")

    def set_alerts(self, alerts: list, current: int) -> None:
        self.alerts, self.current = alerts, current
        self.setFixedSize(self.sizeHint())
        self._describe()
        self.update()

    def set_current(self, i: int) -> None:
        self.current = i
        self._describe()
        self.update()

    def _describe(self) -> None:
        if self.alerts:
            a = self.alerts[self.current]
            self.setAccessibleDescription(f"Picture {self.current + 1} of {len(self.alerts)}: "
                                          f"{moment_text(a)}, {a.camera}, {datetime.fromtimestamp(a.ts):%H:%M:%S}")

    def sizeHint(self) -> QSize:
        n = len(self.alerts)
        fm = QFontMetrics(self.font())
        return QSize(max(1, n * (FILM.width() + self.GAP) - self.GAP + 4), FILM.height() + fm.height() + 10)

    def rect_of(self, i: int) -> QRect:
        return QRect(2 + i * (FILM.width() + self.GAP), 2, FILM.width(), FILM.height())

    def paintEvent(self, event):
        p = self.pane.ctx.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        small = QFont(self.font())
        small.setPointSizeF(self.font().pointSizeF() * 0.85)
        fm = QFontMetrics(small)
        for i, a in enumerate(self.alerts):
            r = self.rect_of(i)
            pm = self.pane.pictures.get(a, FILM)
            paint_picture(painter, p, r, pm, 6, faded=a.verdict == "rejected" or a.status == "false_alarm")
            painter.setPen(Qt.NoPen)
            painter.setBrush(severity_color(p, a.severity))
            painter.drawRoundedRect(QRectF(r.left() + 6, r.bottom() - 6, 18, 3), 1.5, 1.5)
            if getattr(a, "clip_path", ""):
                paint_play_badge(painter, p, QRectF(r.right() - 23, r.top() + 5, 18, 18))
            if i == self.current:
                painter.setPen(QPen(QColor(p["primary"]), 2.5))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 6, 6)
                if self.hasFocus():
                    painter.setPen(QPen(QColor(p["nav_active_text"]), 1, Qt.DashLine))
                    painter.drawRoundedRect(QRectF(r).adjusted(-1.5, -1.5, 1.5, 1.5), 7, 7)
            painter.setFont(small)
            painter.setPen(QColor(p["text"] if i == self.current else p["muted"]))
            painter.drawText(QRect(r.left(), r.bottom() + 4, r.width(), fm.height()), Qt.AlignCenter,
                             f"{datetime.fromtimestamp(a.ts):%H:%M:%S}")
        painter.end()

    def mousePressEvent(self, event):
        for i in range(len(self.alerts)):
            if self.rect_of(i).adjusted(0, 0, 0, 20).contains(event.position().toPoint()):
                self.setFocus(Qt.MouseFocusReason)
                self.picked.emit(i)
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        n = len(self.alerts)
        target = {Qt.Key_Home: 0, Qt.Key_End: n - 1}.get(event.key())
        if n and target is not None:
            self.picked.emit(target)
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()


class _Rail(QWidget):
    """The timeline's vertical line with this entry's dot in the severity colour."""

    def __init__(self, ctx, first: bool, last: bool, severity: str, faded: bool):
        super().__init__()
        self.ctx, self.first, self.last, self.severity, self.faded = ctx, first, last, severity, faded
        self.setFixedWidth(18)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def paintEvent(self, event):
        p = self.ctx.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, 20
        painter.setPen(QPen(QColor(p["border_strong"]), 2))
        if not self.first:
            painter.drawLine(int(cx), 0, int(cx), int(cy))
        if not self.last:
            painter.drawLine(int(cx), int(cy), int(cx), self.height())
        color = QColor(p["border_strong"]) if self.faded else severity_color(self.ctx.palette, self.severity)
        painter.setPen(QPen(QColor(p["surface"]), 2))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(cx - 6, cy - 6, 12, 12))
        painter.end()


class _TimelineEntry(QFrame):
    """One alert in the timeline: time, what happened, camera, a small picture and the AI's word."""

    clicked = Signal(int)

    def __init__(self, pane: "_IncidentDetail", index: int, alert, first: bool, last: bool):
        super().__init__()
        self.pane, self.index, self.alert = pane, index, alert
        ctx = pane.ctx
        self.setObjectName("TimelineEntry")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        faded = alert.verdict == "rejected" or alert.status == "false_alarm"
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 10, 0)  # no vertical margin: the rails of the entries join up
        row.setSpacing(10)
        when = label(f"{datetime.fromtimestamp(alert.ts):%H:%M:%S}", "TimelineTime")
        when.setFixedWidth(QFontMetrics(when.font()).horizontalAdvance("00:00:00") + 12)
        when.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        when.setContentsMargins(0, 11, 0, 0)
        row.addWidget(when, 0, Qt.AlignTop)
        row.addWidget(_Rail(ctx, first, last, alert.severity, faded))
        col = QVBoxLayout()
        col.setContentsMargins(0, 10, 0, 10)
        col.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(6)
        icon = QLabel()
        icon.setPixmap(ctx.icon(rule_icon(alert.rule), "muted", size=16).pixmap(16, 16))
        icon.setFixedSize(16, 18)
        head.addWidget(icon, 0, Qt.AlignTop)
        self.title = label(moment_text(alert), "TimelineTitle", wrap=True)
        head.addWidget(self.title, 1)
        col.addLayout(head)
        self.meta = muted(f"{alert.camera}  ·  {ai_text(alert)}")
        col.addWidget(self.meta)
        row.addLayout(col, 1)
        self.thumb = QLabel()
        self.thumb.setObjectName("TimelineThumb")
        self.thumb.setFixedSize(84, 50)
        row.addWidget(self.thumb, 0, Qt.AlignVCenter)
        self.setAccessibleName(f"{datetime.fromtimestamp(alert.ts):%H:%M:%S}, {moment_text(alert)}, "
                               f"{alert.camera}, {ai_text(alert)}")
        self.refresh_thumb()

    def refresh_thumb(self) -> None:
        size = self.thumb.size()
        pm = self.pane.pictures.get(self.alert, size)
        canvas = QPixmap(size * 2)
        canvas.fill(Qt.transparent)
        canvas.setDevicePixelRatio(2.0)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        paint_picture(painter, self.pane.ctx.palette, QRect(0, 0, size.width(), size.height()), pm, 6,
                      faded=self.alert.verdict == "rejected" or self.alert.status == "false_alarm")
        painter.end()
        self.thumb.setPixmap(canvas)

    def set_selected(self, on: bool) -> None:
        if bool(self.property("selected")) != on:
            set_prop(self, "selected", on)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            self.clicked.emit(self.index)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self.index)
            return
        if event.key() in (Qt.Key_Up, Qt.Key_Down):
            self.pane.focus_entry(self.index + (1 if event.key() == Qt.Key_Down else -1))
            return
        super().keyPressEvent(event)


class _IncidentDetail(QFrame):
    """Right side: the selected incident's header, actions, big picture, filmstrip and timeline."""

    def __init__(self, page: "IncidentsPage"):
        super().__init__()
        self.page, self.ctx, self.pictures = page, page.ctx, page.pictures
        self.setObjectName("Card")
        self.incident = None
        self.alerts: list = []
        self.index = 0
        self._gen = 0
        self.entries: list[_TimelineEntry] = []
        self.player = ClipPlayer(self)
        self.player.frame_ready.connect(self._clip_frame)
        self.player.position_changed.connect(self._clip_position)
        self.player.playing_changed.connect(self._clip_playing)
        self.player.failed.connect(self._clip_failed)
        self.prefer_clip = True        # the user's last Picture | Clip choice
        self._clip_alert = None        # alert whose clip is in the player
        self._suspended: tuple | None = None   # (path, seconds, playing) while the page is hidden
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        placeholder = QWidget()
        pl = QVBoxLayout(placeholder)
        pl.addStretch(1)
        hint = muted("Select an incident to see what happened, step by step.")
        hint.setAlignment(Qt.AlignCenter)
        pl.addWidget(hint)
        pl.addStretch(1)
        self.stack.addWidget(placeholder)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("Transparent")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)
        self.scroll.setWidget(body)
        self.stack.addWidget(self.scroll)

        self.title = label("", "DetailTitle", wrap=True)
        lay.addWidget(self.title)
        sub = QHBoxLayout()
        sub.setSpacing(10)
        self.badge = label("", "Badge")
        self.status_badge = label("", "Badge")
        set_prop(self.status_badge, "tone", "neutral")
        self.meta = muted()
        sub.addWidget(self.badge, 0, Qt.AlignVCenter)
        sub.addWidget(self.status_badge, 0, Qt.AlignVCenter)
        sub.addWidget(self.meta, 1)
        lay.addLayout(sub)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.handled_btn = button("Mark handled", "primary", "Done with this incident (H)")
        self.ctx.bind_icon(self.handled_btn, "check", "on_primary")
        self.handled_btn.clicked.connect(lambda: self.page.toggle_handled(self.incident))
        self.false_btn = button("False alarm", tooltip="Nothing wrong happened here (F)")
        self.false_btn.clicked.connect(lambda: self.page.toggle_false_alarm(self.incident))
        self.live_btn = button("Open live view", tooltip="Watch the first camera of this incident live (Enter)")
        self.ctx.bind_icon(self.live_btn, "video")
        self.live_btn.clicked.connect(lambda: self.page.open_live(self.incident))
        self.files_btn = button("", "ghost", "Show the picture file in Explorer")
        self.files_btn.setAccessibleName("Show files")
        self.ctx.bind_icon(self.files_btn, "folder", "muted")
        self.files_btn.clicked.connect(self._show_file)
        self.delete_btn = button("", "ghost", "Delete this incident and its pictures (Delete)")
        self.delete_btn.setAccessibleName("Delete incident")
        self.ctx.bind_icon(self.delete_btn, "trash", "danger")
        self.delete_btn.clicked.connect(lambda: self.page.delete_incident(self.incident))
        for w in (self.handled_btn, self.false_btn, self.live_btn):
            acts.addWidget(w)
        acts.addStretch(1)
        acts.addWidget(self.files_btn)
        acts.addWidget(self.delete_btn)
        lay.addLayout(acts)

        self.image = ImageView("Loading picture…")
        self.image.setCursor(Qt.PointingHandCursor)
        self.image.setToolTip("Open the full-size picture")
        self.image.setAccessibleName("Picture of the selected moment")
        self.image.clicked.connect(self._image_clicked)
        lay.addSpacing(2)
        lay.addWidget(self.image)
        lay.addWidget(self._build_media_bar())
        self.caption = label("", "MomentCaption", wrap=True)
        lay.addWidget(self.caption)
        self.film = _Filmstrip(self)
        self.film.picked.connect(self.select)
        self.film_scroll = QScrollArea()
        self.film_scroll.setObjectName("Transparent")
        self.film_scroll.setFrameShape(QFrame.NoFrame)
        self.film_scroll.setWidget(self.film)
        self.film_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.film_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.film_scroll.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self.film_scroll)

        lay.addSpacing(2)
        line = QFrame()
        line.setObjectName("Divider")
        lay.addWidget(line)
        head = QHBoxLayout()
        head.addWidget(label("Timeline", "CardTitle"))
        self.timeline_meta = muted(wrap=False)
        head.addStretch(1)
        head.addWidget(self.timeline_meta)
        lay.addLayout(head)
        self.timeline = QVBoxLayout()
        self.timeline.setSpacing(0)
        lay.addLayout(self.timeline)
        lay.addStretch(1)
        self.pictures.loaded.connect(self._pictures_loaded)

    # --- content ------------------------------------------------------------------

    def show_incident(self, inc, alerts: list, select_id: str | None = None) -> None:
        """Show an incident; keeps the selected moment when it is the same incident (live updates)."""
        same = self.incident is not None and inc is not None and self.incident.id == inc.id
        keep = select_id or (self.alerts[self.index].id if same and self.alerts else None)
        self.incident, self.alerts = inc, list(alerts)
        if inc is None or not alerts:
            self._stop_clip()
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        self._fill_header(inc)
        ids = [a.id for a in self.alerts]
        if keep in ids:
            index = ids.index(keep)
        else:
            index = ids.index(inc.cover_alert_id) if inc.cover_alert_id in ids else 0
        self.film.set_alerts(self.alerts, index)
        self.film_scroll.setFixedHeight(self.film.height() + 14)  # room for the scroll bar of a long strip
        self._fill_timeline()
        # Reload on updates too: the engine redraws the snapshot with the AI brain's boxes. The old
        # picture stays up until the new one is decoded, so this does not flicker.
        self.select(index, mark=False)
        if not same:
            self.scroll.verticalScrollBar().setValue(0)

    def _fill_header(self, inc) -> None:
        self.title.setText(inc.title)
        text, tone = incident_badge(inc)
        self.badge.setText(text)
        set_prop(self.badge, "tone", tone)
        self.status_badge.setText("Handled")
        self.status_badge.setVisible(inc.status == "handled")
        cams = len(inc.channels)
        self.meta.setText(f"{day_label(inc.start_ts)} {time_range(inc)} · {inc.alert_count} alert"
                          f"{'s' if inc.alert_count != 1 else ''}" + (f" on {cams} cameras" if cams > 1 else ""))
        handled = inc.status == "handled"
        self.handled_btn.setText(" Reopen" if handled else " Mark handled")
        self.handled_btn.setToolTip("Move this incident back to Open (H)" if handled else
                                    "Done with this incident (H)")
        false = inc.status == "false_alarm"
        self.false_btn.setText("Not a false alarm" if false else "False alarm")
        self.false_btn.setToolTip("Move this incident back to Open (F)" if false else
                                  "Nothing wrong happened here (F)")
        self.live_btn.setToolTip(f"Watch {inc.cameras[0] if inc.cameras else 'the camera'} live (Enter)")

    def _fill_timeline(self) -> None:
        for e in self.entries:
            self.timeline.removeWidget(e)
            e.setParent(None)  # gone from the layout now, not only once the event loop runs
            e.deleteLater()
        self.entries = []
        n = len(self.alerts)
        for i, a in enumerate(self.alerts):
            e = _TimelineEntry(self, i, a, i == 0, i == n - 1)
            e.clicked.connect(self.select)
            e.installEventFilter(self.page)
            self.timeline.addWidget(e)
            self.entries.append(e)
        first, last = self.alerts[0], self.alerts[-1]
        span = f"{datetime.fromtimestamp(first.ts):%H:%M} to {datetime.fromtimestamp(last.ts):%H:%M}"
        self.timeline_meta.setText(f"{n} moment{'s' if n != 1 else ''}" + (f" · {span}" if n > 1 else ""))

    def select(self, index: int, mark: bool = True) -> None:
        if not self.alerts:
            return
        index = max(0, min(index, len(self.alerts) - 1))
        self.index = index
        a = self.alerts[index]
        self.film.set_current(index)
        r = self.film.rect_of(index)
        self.film_scroll.ensureVisible(r.center().x(), r.center().y(), r.width() // 2 + 12, 0)
        for e in self.entries:
            e.set_selected(e.index == index)
        n = len(self.alerts)
        self.caption.setText(f"{index + 1} of {n}  ·  {datetime.fromtimestamp(a.ts):%H:%M:%S}  ·  {a.camera}  ·  "
                             f"{moment_text(a)}")
        self._show_media(a)
        if mark:
            self.page.mark_alert_seen(a)

    def step(self, delta: int) -> None:
        if self.alerts:
            self.select(self.index + delta)

    def focus_entry(self, index: int) -> None:
        if 0 <= index < len(self.entries):
            self.entries[index].setFocus(Qt.TabFocusReason)
            self.scroll.ensureWidgetVisible(self.entries[index], 0, 24)

    def current_alert(self):
        return self.alerts[self.index] if self.alerts else None

    # --- clip player --------------------------------------------------------------

    def _build_media_bar(self) -> QWidget:
        self.media = QWidget()
        self.media.setObjectName("MediaBar")
        row = QHBoxLayout(self.media)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.play_btn = QPushButton()
        self.play_btn.setObjectName("PlayButton")
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setIconSize(QSize(14, 14))
        self.play_btn.clicked.connect(self.toggle_play)
        self.clip_time = label("0:00 / 0:00", "ClipTime")
        self.clip_time.setFixedWidth(QFontMetrics(self.clip_time.font()).horizontalAdvance("00:00 / 00:00") + 6)
        self.clip_time.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setAccessibleName("Position in the clip")
        self.seek.setCursor(Qt.PointingHandCursor)
        self.seek.setMinimumWidth(80)
        self.seek.valueChanged.connect(self._seek_changed)
        self.seek.sliderPressed.connect(self._seek_pressed)
        self.seek.sliderReleased.connect(self._seek_released)
        self._slider_busy = False
        self._resume_after_seek = False
        self.speed = Segmented([("1×", "Normal speed"), ("2×", "Double speed")])
        self.speed.setAccessibleName("Playback speed")
        self.speed.changed.connect(lambda i: self.player.set_speed(2.0 if i == 1 else 1.0))
        self.clip_note = label("", "ClipNote")
        self.clip_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.view_toggle = Segmented([("Picture", "The alert picture with the AI's boxes"),
                                      ("Clip", "The video around this moment")])
        self.view_toggle.setAccessibleName("Show picture or clip")
        self.view_toggle.changed.connect(self._view_changed)
        for seg in (self.speed, self.view_toggle):
            for b in seg.group.buttons():
                b.setProperty("compact", True)
        for w in (self.play_btn, self.clip_time):
            row.addWidget(w)
        row.addWidget(self.seek, 1)
        row.addWidget(self.speed)
        row.addWidget(self.clip_note, 1)
        row.addWidget(self.view_toggle)
        self.media.setVisible(False)
        self._clip_playing(False)
        return self.media

    def clip_mode(self) -> bool:
        """The big view shows the clip (not the picture)."""
        return self._clip_alert is not None

    def _show_media(self, a) -> None:
        clip = clip_of(a)
        if clip and self.prefer_clip:
            self._show_clip(a, clip)
        else:
            self._stop_clip()
            self._load_picture(a)
        self._update_bar(a, clip)

    def _show_clip(self, a, clip: str) -> None:
        same_alert = self._clip_alert is not None and self._clip_alert.id == a.id
        self._clip_alert = a
        offset = max(0.0, getattr(a, "clip_offset_s", 0.0))
        if self.player.path == clip and (same_alert or self.player.loaded):
            if not same_alert:  # another moment of the same clip: jump there
                self.player.seek(offset)
                self.player.play()
            return  # a live update of the same moment must not restart the clip
        if not self.page._looking():  # opened (and played) once the page is shown
            self.player.close()
            self._suspended = (clip, offset, True)
            return
        # The old picture stays up until the first clip frame is decoded: no flicker.
        self._suspended = None
        self.player.open(clip, offset, play=True)

    def _stop_clip(self) -> None:
        self._clip_alert = None
        self._suspended = None
        if self.player.path:
            self.player.close()

    def _update_bar(self, a, clip: str) -> None:
        showing = self.clip_mode()
        saving = not clip and clip_saving(a)
        self.media.setVisible(bool(clip) or saving or self._clip_error(a))
        for w in (self.play_btn, self.clip_time, self.seek, self.speed):
            w.setVisible(showing)
        self.view_toggle.setVisible(bool(clip))
        self.view_toggle.set_current(1 if showing else 0)
        note = ""
        if saving:
            note = "Clip is being saved…"
        elif self._clip_error(a):
            note = "The clip of this moment is missing."
        elif clip and not showing:
            note = "A short clip of this moment is available."
        self.clip_note.setText(note)
        self.clip_note.setVisible(not showing)
        self.image.setToolTip("Play or pause the clip (Space)" if showing else "Open the full-size picture")
        self.files_btn.setToolTip("Show the clip file in Explorer" if showing else "Show the picture file in Explorer")

    @staticmethod
    def _clip_error(a) -> bool:
        return bool(getattr(a, "clip_path", "")) and not clip_of(a)

    def toggle_play(self) -> None:
        if self.clip_mode():
            self.player.toggle()

    def _view_changed(self, index: int) -> None:
        self.prefer_clip = index == 1
        a = self.current_alert()
        if a is not None:
            self._show_media(a)

    def _image_clicked(self) -> None:
        if self.clip_mode():
            self.toggle_play()
        else:
            self._open_picture()

    def _clip_frame(self, img: QImage) -> None:
        if self.clip_mode():
            self.image.set_image(img)

    def _clip_position(self, index: int, count: int) -> None:
        self._slider_busy = True
        self.seek.setRange(0, max(0, count - 1))
        if not self.seek.isSliderDown():
            self.seek.setValue(index)
        self._slider_busy = False
        fps = self.player.fps or 8.0
        self.clip_time.setText(f"{clock_text(index / fps)} / {clock_text(count / fps)}")

    def _clip_playing(self, on: bool) -> None:
        self.play_btn.setIcon(self.ctx.icon("pause" if on else "play", "text", size=14))
        self.play_btn.setToolTip(("Pause" if on else "Play") + " the clip (Space)")
        self.play_btn.setAccessibleName("Pause clip" if on else "Play clip")

    def _clip_failed(self, message: str) -> None:
        a = self._clip_alert
        self._stop_clip()
        cur = self.current_alert()
        if a is not None and cur is not None and cur.id == a.id:
            self._load_picture(a)
            self._update_bar(a, "")
            self.media.setVisible(True)
            self.clip_note.setText(message)

    def _seek_changed(self, value: int) -> None:
        if not self._slider_busy:
            self.player.seek_frame(value)

    def _seek_pressed(self) -> None:
        self._resume_after_seek = self.player.playing
        self.player.pause()

    def _seek_released(self) -> None:
        self.player.seek_frame(self.seek.value())
        if self._resume_after_seek:
            self.player.play()

    def suspend(self) -> None:
        """Page hidden: stop and let go of the file (cleanup may delete it meanwhile)."""
        if self.player.path and self._suspended is None:
            self._suspended = (self.player.path, self.player.position_s, self.player.playing)
            self.player.close()

    def resume(self) -> None:
        state, self._suspended = self._suspended, None
        if state and self.clip_mode() and not self.player.path and Path(state[0]).exists():
            self.player.open(state[0], state[1], play=state[2])

    def release(self) -> None:
        """Close the clip so its file can be deleted."""
        self._suspended = None
        self.player.close()

    def _load_picture(self, a) -> None:
        self._gen += 1
        gen = self._gen
        run_async(load_image, a.snapshot_path, QSize(1920, 1080), on_done=lambda img: self._picture(gen, img))

    def _picture(self, gen: int, img) -> None:
        if gen != self._gen:
            return
        if isinstance(img, QImage):
            self.image.set_image(img)
        else:
            self.image.set_image(None, "The picture file is missing.")

    def _pictures_loaded(self) -> None:
        if self.stack.currentIndex() == 1:
            self.film.update()
            for e in self.entries:
                e.refresh_thumb()

    def _open_picture(self) -> None:
        a = self.current_alert()
        if a is not None and Path(a.snapshot_path).exists():
            os.startfile(a.snapshot_path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # On big windows the picture would push the timeline out of sight: keep it to half the pane.
        self.image.setMaximumHeight(max(220, int(self.height() * 0.5)))

    def _show_file(self) -> None:
        a = self.current_alert()
        if a is None:
            return
        if self.clip_mode():
            if not show_in_explorer(a.clip_path):
                self.page.win.notify("The clip file for this moment is missing.", error=True)
        elif not show_in_explorer(a.snapshot_path):
            self.page.win.notify("The picture file for this moment is missing.", error=True)


# --- Page ------------------------------------------------------------------------------

class Segmented(QFrame):
    """Two or more exclusive choices drawn as one control ("Incidents | All alerts")."""

    changed = Signal(int)

    def __init__(self, items: list[tuple[str, str]]):
        super().__init__()
        self.setObjectName("Segmented")
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, (text, tip) in enumerate(items):
            b = QPushButton(text)
            b.setObjectName("SegButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setChecked(i == 0)
            self.group.addButton(b, i)
            row.addWidget(b)
        self.group.idClicked.connect(self.changed.emit)

    def current(self) -> int:
        return self.group.checkedId()

    def set_current(self, i: int) -> None:
        self.group.button(i).setChecked(True)


class IncidentsPage(QWidget):
    """One story per event: incident cards on the left, the selected incident on the right.
    The "All alerts" side of the toggle shows the classic AlertsPage."""

    changed = Signal()   # incidents were marked or deleted: badge and dashboard strip should refresh

    def __init__(self, ctx, win):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.incidents: dict[str, object] = {}   # id -> Incident, newest first by start_ts
        self.members: dict[str, list] = {}       # id -> its alerts, oldest first
        self.pictures = Pictures(self)
        self.pictures.loaded.connect(lambda: self.list.viewport().update())
        self._fresh: dict[str, float] = {}
        self._filter = "open"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.views = QStackedWidget()
        outer.addWidget(self.views)
        self.main = QWidget()
        self.views.addWidget(self.main)
        self.alerts = AlertsPage(ctx, win)
        self.views.addWidget(self.alerts)

        lay = QVBoxLayout(self.main)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(16)
        self.header = PageHeader("Incidents")
        self.toggle = Segmented([("Incidents", "One story per event"),
                                 ("All alerts", "Every single alert, newest first")])
        self.toggle.setAccessibleName("View")
        self.toggle.changed.connect(self.set_view)
        self.header.actions.addWidget(self.toggle, 0, Qt.AlignTop)
        lay.addWidget(self.header)

        self.filter_bar = QWidget()
        bar = QHBoxLayout(self.filter_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        self.chips = QButtonGroup(self)
        self.chips.setExclusive(True)
        for key, text, tip in FILTERS:
            chip = QPushButton(text)
            chip.setObjectName("FilterChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(tip)
            chip.setProperty("key", key)
            chip.setChecked(key == self._filter)
            self.chips.addButton(chip)
            bar.addWidget(chip)
        self.chips.buttonClicked.connect(lambda chip: self.set_filter(chip.property("key")))
        bar.addStretch(1)
        lay.addWidget(self.filter_bar)

        self.body = QStackedWidget()
        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        self.left = QStackedWidget()
        self.left.setFixedWidth(430)
        self.list = QListWidget()
        self.list.setObjectName("IncidentList")
        self.list.setItemDelegate(_IncidentDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setAccessibleName("Incidents")
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.itemClicked.connect(lambda item: self._mark_seen(self.incidents.get(item.data(ID_ROLE))))
        self.list.itemActivated.connect(lambda item: self.open_live(self.incidents.get(item.data(ID_ROLE))))
        self.list.installEventFilter(self)
        self.left.addWidget(self.list)
        no_match = QFrame()
        no_match.setObjectName("Card")
        nl = QVBoxLayout(no_match)
        nl.setContentsMargins(24, 24, 24, 24)
        nl.setSpacing(10)
        nl.addStretch(1)
        self.no_match_text = muted("")
        self.no_match_text.setAlignment(Qt.AlignCenter)
        nl.addWidget(self.no_match_text)
        self.no_match_btn = button("Show all incidents")
        self.no_match_btn.clicked.connect(lambda: self.set_filter("all"))
        nl.addWidget(self.no_match_btn, 0, Qt.AlignCenter)
        nl.addStretch(2)
        self.left.addWidget(no_match)
        row.addWidget(self.left)
        self.detail = _IncidentDetail(self)
        row.addWidget(self.detail, 1)
        for w in (self.detail.film, self.detail.image, self.detail.handled_btn, self.detail.false_btn,
                  self.detail.live_btn):
            w.installEventFilter(self)
        self.empty = EmptyState(ctx)
        self.body.addWidget(content)
        self.body.addWidget(self.empty)
        lay.addWidget(self.body, 1)

        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(40)
        self._glow_timer.timeout.connect(self._tick_glow)
        self._clock = QTimer(self)  # keeps "Today" / "Yesterday" right after midnight
        self._clock.setInterval(60_000)
        self._clock.timeout.connect(lambda: self.isVisible() and self.list.viewport().update())
        self._clock.start()

        bridge = getattr(ctx, "alert_bridge", None)
        if bridge is not None and hasattr(bridge, "incident_added"):
            bridge.incident_added.connect(self.add_incident)
            bridge.incident_updated.connect(self.update_incident)

    # --- data -------------------------------------------------------------------------

    @property
    def store(self):
        return self.ctx.alert_store

    def unread(self, incident_id: str) -> bool:
        return any(a.status == "new" for a in self.members.get(incident_id, ()))

    def reload(self) -> None:
        current = self._current_id()
        try:
            items = self.store.list_incidents(limit=LOAD_LIMIT)
            members = {i.id: self.store.incident_alerts(i.id) for i in items}
        except Exception as exc:  # database locked / unreadable: keep what we have
            self.win.notify(f"Could not read incidents: {exc}", error=True)
            return
        self.incidents = {i.id: i for i in items}
        self.members = members
        self._refill(select=current)

    def _put(self, inc) -> bool:
        """Store a fresh copy of an incident and its alerts; False when it no longer exists."""
        try:
            alerts = self.store.incident_alerts(inc.id)
        except Exception:
            return False
        if not alerts:
            return False
        self.incidents[inc.id] = inc
        self.incidents = dict(sorted(self.incidents.items(), key=lambda kv: kv[1].start_ts, reverse=True))
        self.members[inc.id] = alerts
        return True

    def add_incident(self, inc) -> None:
        if inc.id in self.incidents:
            self.update_incident(inc)
            return
        if not self._put(inc):
            return
        self._fresh[inc.id] = time.monotonic()
        self._glow_timer.start()
        sb = self.list.verticalScrollBar()
        before, value = self._content_height(), sb.value()
        self._refill(select=self._current_id())
        if value > 0:  # keep what the user is looking at in place while a card appears above it
            self.list.doItemsLayout()
            sb.setValue(value + self._content_height() - before)

    def update_incident(self, inc) -> None:
        if inc.id not in self.incidents:
            self.add_incident(inc)
            return
        was_in = self._row_of(inc.id) is not None
        if not self._put(inc):
            self._drop(inc.id)
            return
        if self._matches(inc) != was_in:
            sb = self.list.verticalScrollBar()
            value = sb.value()
            self._refill(select=self._current_id())
            sb.setValue(value)
        else:
            self._update_counts()
            self.list.viewport().update()
        if self._current_id() == inc.id:
            self.detail.show_incident(inc, self.members[inc.id])

    def _drop(self, incident_id: str) -> None:
        nxt = self._neighbour(incident_id)
        self.incidents.pop(incident_id, None)
        self.members.pop(incident_id, None)
        self._refill(select=nxt)

    def glow(self, incident_id: str) -> float:
        start = self._fresh.get(incident_id)
        return 0.0 if start is None else max(0.0, 1.0 - (time.monotonic() - start) / GLOW_S)

    def _tick_glow(self) -> None:
        now = time.monotonic()
        self._fresh = {k: t for k, t in self._fresh.items() if now - t < GLOW_S}
        if not self._fresh:
            self._glow_timer.stop()
        self.list.viewport().update()

    # --- list -------------------------------------------------------------------------

    def _matches(self, inc) -> bool:
        return self._filter == "all" or incident_bucket(inc) == self._filter

    def _content_height(self) -> int:
        return sum(DAY_H if self.list.item(i).data(KIND_ROLE) == "day" else CARD_H for i in range(self.list.count()))

    def _refill(self, select: str | None = None) -> None:
        if select is None:
            select = self._current_id()
        self.list.blockSignals(True)
        self.list.clear()
        shown = [i for i in self.incidents.values() if self._matches(i)]
        days: dict[str, int] = {}
        for inc in shown:
            key = day_label(inc.start_ts)
            days[key] = days.get(key, 0) + 1
        last_day = None
        for inc in shown:
            day = day_label(inc.start_ts)
            if day != last_day:
                head = QListWidgetItem(day)
                head.setData(KIND_ROLE, "day")
                head.setData(Qt.UserRole + 3, f"{days[day]} incident{'s' if days[day] != 1 else ''}")
                head.setFlags(Qt.NoItemFlags)
                self.list.addItem(head)
                last_day = day
            item = QListWidgetItem()
            item.setData(ID_ROLE, inc.id)
            item.setData(KIND_ROLE, "incident")
            item.setData(Qt.AccessibleTextRole, f"{inc.title}, {day} {time_range(inc)}, {inc.alert_count} alerts, "
                                                f"{incident_badge(inc)[0]}" + (", unread" if self.unread(inc.id) else ""))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._update_counts()
        if not self.incidents:
            self._show_empty()
            return
        self.body.setCurrentIndex(0)
        self.filter_bar.setVisible(True)
        if not shown:
            self.left.setCurrentIndex(1)
            names = {k: t for k, t, _ in FILTERS}
            self.no_match_text.setText("Nothing open. Every incident has been dealt with." if self._filter == "open"
                                       else f"No {names[self._filter].lower()} yet.")
            self.detail.show_incident(None, [])
            return
        self.left.setCurrentIndex(0)
        row = self._row_of(select) if select else None
        self.list.setCurrentRow(row if row is not None else self._first_row())

    def _first_row(self) -> int:
        return next((i for i in range(self.list.count()) if self.list.item(i).data(KIND_ROLE) == "incident"), -1)

    def _row_of(self, incident_id: str | None) -> int | None:
        for i in range(self.list.count()):
            if self.list.item(i).data(ID_ROLE) == incident_id and incident_id:
                return i
        return None

    def _current_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(ID_ROLE) if item else None

    def _neighbour(self, incident_id: str) -> str | None:
        row = self._row_of(incident_id)
        if row is None:
            return None
        for r in list(range(row + 1, self.list.count())) + list(range(row - 1, -1, -1)):
            if self.list.item(r).data(KIND_ROLE) == "incident":
                return self.list.item(r).data(ID_ROLE)
        return None

    def _show_empty(self) -> None:
        self.filter_bar.setVisible(False)
        cfg, mon = self.ctx.cfg, getattr(self.ctx, "monitor", None)
        text = "When monitoring spots something, it shows up here as one story per event."
        if cfg.active_site() is None:
            self.empty.configure("shield", "No incidents", text + " First, connect your cameras.",
                                 "Open Sites & cameras", lambda: self.win.go_page(self.win.sites))
        elif mon is not None and mon.running:
            self.empty.configure("shield", "No incidents", text + " Monitoring is on and all is quiet.",
                                 "Open live view", lambda: self.win.go_page(self.win.live))
        else:
            self.empty.configure("shield", "No incidents", text,
                                 "Turn on monitoring", lambda: self.win.set_monitoring(True))
        self.body.setCurrentIndex(1)
        self.detail.show_incident(None, [])

    def _update_counts(self) -> None:
        try:
            counts = self.store.incident_counts()
            week = sum(self.store.incident_counts(since=time.time() - 7 * 86400).values())
        except Exception:
            return
        counts["all"] = sum(counts.values())
        for chip in self.chips.buttons():
            key = chip.property("key")
            name = next(t for k, t, _ in FILTERS if k == key)
            chip.setText(f"{name}  {counts.get(key, 0)}" if counts.get(key) else name)
        if counts["all"]:
            opened = counts["open"]
            self.header.subtitle.setText(f"{opened} open · {week} in the last 7 days" if opened
                                         else f"Nothing open · {week} in the last 7 days")
        else:
            self.header.subtitle.setText("Related alerts grouped into one story per event")

    def set_filter(self, key: str) -> None:
        self._filter = key
        for chip in self.chips.buttons():
            chip.setChecked(chip.property("key") == key)
        self._refill()

    # --- selection & actions ---------------------------------------------------------

    def _looking(self) -> bool:
        """The user can see the incidents list right now (not another page, not All alerts)."""
        return self.isVisible() and self.views.currentIndex() == 0

    def _current_changed(self, item, _previous=None) -> None:
        inc = self.incidents.get(item.data(ID_ROLE)) if item else None
        self.detail.show_incident(inc, self.members.get(inc.id, []) if inc else [])
        self._mark_seen(inc)

    def _mark_seen(self, inc) -> None:
        """Opening an incident counts as seeing all its alerts, but only when the page is really
        on screen: the list also selects its top row by itself while another page is open."""
        if inc is None or not self._looking():
            return
        fresh = [a for a in self.members.get(inc.id, []) if a.status == "new"]
        for a in fresh:
            try:
                self.store.mark(a.id, "seen")
            except Exception as exc:
                self.win.notify(f"Could not update the incident: {exc}", error=True)
                return
            a.status = "seen"
        if fresh:
            inc.unread = 0
            self.list.viewport().update()
            self.changed.emit()

    def mark_alert_seen(self, a) -> None:
        if a.status == "new" and self._looking():
            try:
                self.store.mark(a.id, "seen")
            except Exception:
                return
            a.status = "seen"
            self.list.viewport().update()
            self.changed.emit()

    def show_incident(self, incident_id: str, alert_id: str | None = None) -> None:
        """Open one incident (from a toast or the dashboard), with one of its moments selected."""
        inc = self.store.get_incident(incident_id) if incident_id else None
        if inc is None or not self._put(inc):
            self.win.notify("That incident was deleted.", error=True)
            return
        self.set_view(0)
        if not self._matches(inc):
            bucket = incident_bucket(inc)
            self._filter = bucket if bucket in {k for k, _, _ in FILTERS} else "all"
            for chip in self.chips.buttons():
                chip.setChecked(chip.property("key") == self._filter)
        self._refill(select=incident_id)
        row = self._row_of(incident_id)
        if row is not None:
            self.list.scrollToItem(self.list.item(row), QAbstractItemView.PositionAtCenter)
        if alert_id:
            ids = [a.id for a in self.members[incident_id]]
            if alert_id in ids:
                self.detail.select(ids.index(alert_id))
        self.list.setFocus()

    def show_alert(self, alert_id: str) -> None:
        a = self.store.get(alert_id)
        if a is None:
            self.win.notify("That alert was deleted.", error=True)
            return
        self.show_incident(a.incident_id, alert_id)

    def _set_status(self, inc, status: str, message: str) -> None:
        try:
            self.store.mark_incident(inc.id, status)
        except Exception as exc:
            self.win.notify(f"Could not update the incident: {exc}", error=True)
            return
        nxt = self._neighbour(inc.id)
        fresh = self.store.get_incident(inc.id)
        if fresh is not None:
            self._put(fresh)
        if fresh is not None and self._matches(fresh):
            self._refill(select=inc.id)
        else:
            self._refill(select=nxt)
        self.win.notify(message)
        self.changed.emit()

    def toggle_handled(self, inc) -> None:
        if inc is None:
            return
        if inc.status == "handled":
            self._set_status(inc, "open", "Incident reopened.")
        else:
            self._set_status(inc, "handled", "Incident marked as handled.")

    def toggle_false_alarm(self, inc) -> None:
        if inc is None:
            return
        if inc.status == "false_alarm":
            self._set_status(inc, "open", "Incident moved back to Open.")
        else:
            self._set_status(inc, "false_alarm", "Incident marked as a false alarm.")

    def delete_incident(self, inc) -> None:
        if inc is None:
            return
        n = inc.alert_count
        box = QMessageBox(QMessageBox.Warning, "Delete incident",
                          f"Delete “{inc.title}”?\n\nIts {n} alert{'s' if n != 1 else ''} and their saved pictures "
                          "and clips will be deleted too. This cannot be undone.", parent=self)
        delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return
        if self.detail.incident is not None and self.detail.incident.id == inc.id:
            self.detail.release()  # the player keeps the clip file open
        try:
            left = self.store.delete_incident(inc.id)
        except Exception as exc:
            self.win.notify(f"Could not delete the incident: {exc}", error=True)
            return
        self._remove_files(left)
        for a in self.members.get(inc.id, []):
            self.pictures.drop(a.id)
        self._drop(inc.id)
        self.win.notify("Incident deleted.")
        self.changed.emit()

    def _remove_files(self, paths: list[str], tries: int = 5) -> None:
        """Windows refuses to delete a picture while it is open (a thumbnail loading): retry."""
        left = []
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                left.append(path)
        if left and tries > 1:
            QTimer.singleShot(600, lambda: self._remove_files(left, tries - 1))

    def open_live(self, inc) -> None:
        if inc is None or not inc.channels:
            return
        cfg = self.ctx.cfg
        if inc.site_id and inc.site_id != cfg.active_site_id and cfg.site(inc.site_id):
            cfg.active_site_id = inc.site_id
            self.ctx.save()
        self.win.open_live(inc.channels[0])

    # --- view toggle ----------------------------------------------------------------

    def set_view(self, index: int) -> None:
        """0 = incidents, 1 = all alerts. The toggle moves along so it sits in the same spot."""
        if index == self.views.currentIndex() and self.toggle.current() == index:
            return
        source, target = self.header.actions, self.alerts.header.actions
        if index == 0:
            source, target = target, source
        source.removeWidget(self.toggle)
        target.insertWidget(0, self.toggle, 0, Qt.AlignTop)
        self.toggle.set_current(index)
        if index == 1:
            self.alerts.reload()
        else:
            self.reload()  # the user may have marked alerts on the other side
        self.views.setCurrentIndex(index)
        self.toggle.group.button(index).setFocus()
        if index == 0:
            self._mark_seen(self.incidents.get(self._current_id()))
            self.detail.resume()
        else:
            self.detail.suspend()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and self.views.currentIndex() == 0:
            key, mods = event.key(), event.modifiers()
            if key in (Qt.Key_Left, Qt.Key_Right) and not mods:
                self.detail.step(-1 if key == Qt.Key_Left else 1)
                return True
            if (key == Qt.Key_Space and not mods and self.detail.clip_mode()
                    and obj in (self.list, self.detail.film, self.detail.image)):
                self.detail.toggle_play()
                return True
            inc = self.incidents.get(self._current_id())
            if obj is self.list and inc is not None and not mods:
                if key == Qt.Key_Delete:
                    self.delete_incident(inc)
                    return True
                if key == Qt.Key_H:
                    self.toggle_handled(inc)
                    return True
                if key == Qt.Key_F:
                    self.toggle_false_alarm(inc)
                    return True
        return False

    def showEvent(self, event):
        super().showEvent(event)
        self.list.viewport().update()
        if self.views.currentIndex() == 0:
            self._mark_seen(self.incidents.get(self._current_id()))  # the selected incident is now in view
            self.detail.resume()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.detail.suspend()


# --- Dashboard strip --------------------------------------------------------------------

class _StripItem(QFrame):
    clicked = Signal(str)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.incident_id = ""
        self.setObjectName("StripItem")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)
        self.bar = QFrame()
        self.bar.setObjectName("SevBar")
        self.bar.setFixedWidth(4)
        row.addWidget(self.bar)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label("", "CardTitle")
        self.title.setMinimumWidth(40)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.meta = muted(wrap=False)
        self.meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        col.addWidget(self.title)
        col.addWidget(self.meta)
        row.addLayout(col, 1)
        self.badge = label("", "Badge")
        row.addWidget(self.badge, 0, Qt.AlignVCenter)
        self.chev = QLabel()
        self.chev.setPixmap(ctx.icon("chev_right", "muted", size=16).pixmap(16, 16))
        row.addWidget(self.chev, 0, Qt.AlignVCenter)
        self._title = ""

    def set_incident(self, inc, unread: bool) -> None:
        self.incident_id = inc.id
        set_prop(self.bar, "severity", "muted" if is_quiet(inc) else inc.severity)
        self._title = ("● " if unread else "") + inc.title
        self.meta.setText(f"{time_range(inc)} · {rel_time(inc.end_ts)}")
        text, tone = incident_badge(inc)
        self.badge.setText(text)
        set_prop(self.badge, "tone", tone)
        self.chev.setPixmap(self.ctx.icon("chev_right", "muted", size=16).pixmap(16, 16))
        self.setToolTip(inc.title)
        self.setAccessibleName(f"Open incident: {inc.title}, {time_range(inc)}, {text}")
        self._elide()

    def _elide(self) -> None:
        self.title.ensurePolished()
        w = max(40, self.title.width())
        self.title.setText(QFontMetrics(self.title.font()).elidedText(self._title, Qt.ElideRight, w))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.incident_id)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self.incident_id)
            return
        super().keyPressEvent(event)


class IncidentStrip(QWidget):
    """Dashboard: the latest open incidents (at most two), hidden when there are none."""

    open_incident = Signal(str)
    see_all = Signal()

    MAX = 2

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.caption = label("OPEN INCIDENTS", "StripCaption")
        head.addWidget(self.caption)
        head.addStretch(1)
        self.all_btn = QPushButton("See all")
        self.all_btn.setObjectName("LinkButton")
        self.all_btn.setCursor(Qt.PointingHandCursor)
        self.all_btn.setToolTip("Open the Incidents page")
        self.all_btn.clicked.connect(self.see_all.emit)
        head.addWidget(self.all_btn)
        col.addLayout(head)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.items = [_StripItem(ctx) for _ in range(self.MAX)]
        for item in self.items:
            item.clicked.connect(self.open_incident.emit)
            row.addWidget(item, 1)
        col.addLayout(row)
        self.setVisible(False)
        bridge = getattr(ctx, "alert_bridge", None)
        if bridge is not None and hasattr(bridge, "incident_added"):
            bridge.incident_added.connect(lambda *_: self.refresh())
            bridge.incident_updated.connect(lambda *_: self.refresh())

    def refresh(self) -> None:
        store = getattr(self.ctx, "alert_store", None)
        try:
            incs = [i for i in store.list_incidents(limit=self.MAX * 5, status="open")
                    if i.verdict != "rejected"][:self.MAX] if store is not None else []
            total = store.open_incident_count() if store is not None else 0
        except Exception:
            incs, total = [], 0
        for item, inc in zip(self.items, incs + [None] * self.MAX):
            item.setVisible(inc is not None)
            if inc is not None:
                item.set_incident(inc, inc.unread > 0)
        self.caption.setText(f"OPEN INCIDENTS  ·  {total}")
        self.all_btn.setText("See all" if total > len(incs) else "Open incidents")
        self.setVisible(bool(incs))
