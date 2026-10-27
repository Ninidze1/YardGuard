"""Alerts page, pop-up toasts and the sidebar monitoring switch.

Alerts come from the engine (yardguard.engine.Alert / AlertStore); this module only
reads their attributes, so it has no import-time dependency on the engine.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QBuffer, QByteArray, QEasingCurve, QEvent, QIODevice, QObject, QPoint, QPropertyAnimation, QRect, QRectF, QSize,
    Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QImageReader, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QButtonGroup, QComboBox, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QStackedWidget,
    QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .qt_util import run_async, set_prop
from .widgets import EmptyState, ImageView, PageHeader, button, label, muted

# Rules in plain words: short name for chips, long explanation for the detail pane.
RULES = {
    "night_presence": ("Person at night", "Someone was close to the parked cars during night hours ({night})."),
    "loitering": ("Loitering", "Someone stayed in a watched area for more than {loiter} seconds."),
    "prowling": ("Checking cars", "One person walked up to {cars} or more different cars within {window} minutes. "
                                  "This is how people look for unlocked cars."),
    "car_moved": ("Car moved", "A parked car moved or shook. It may have been hit, pushed or broken into."),
    "brain_detect": ("Suspicious behaviour", "LocateAnything saw one of the suspicious things you listed "
                                             "(Settings → AI brain), for example a hand on a car door handle."),
}
FILTERS = (
    ("all", "All", "Every alert"),
    ("new", "New", "Alerts you have not opened yet"),
    ("confirmed", "Confirmed", "The AI brain took a closer look and confirmed it"),
    ("review", "Needs review", "Still being checked, or the AI brain was not running"),
    ("false", "False alarms", "Marked as false alarm by you, or dismissed by the AI brain"),
)
ID_ROLE = Qt.UserRole + 1
ROW_HEIGHT = 84
THUMB = QSize(104, 64)

EXTRA_QSS = """
#AlertList { padding: 6px; }
#AlertList::item { padding: 0; margin: 0; border: none; }
QPushButton#FilterChip {
    background: transparent; border: 1px solid $border_strong; border-radius: 15px;
    padding: 4px 14px; min-height: 20px; color: $muted;
}
QPushButton#FilterChip:hover { background: $surface2; color: $text; }
QPushButton#FilterChip:checked { background: $nav_active_bg; border-color: $nav_active_bg; color: $nav_active_text; font-weight: 600; }
QPushButton#FilterChip:focus { border-color: $primary; }
#Badge { border-radius: 9px; padding: 2px 9px; font-size: 8.5pt; font-weight: 600; }
#Badge[tone="danger"] { background: $danger_hover; color: $danger; }
#Badge[tone="primary"] { background: $nav_active_bg; color: $nav_active_text; }
#Badge[tone="neutral"] { background: $surface2; color: $muted; }
#Badge[tone="muted"] { background: transparent; color: $muted; border: 1px solid $border; }
#DetailTitle { font-size: 14pt; font-weight: 600; }
#Toast { background: $surface; border: 1px solid $border_strong; border-radius: 10px; }
#Toast:hover { border-color: $primary; }
#ToastBar { border-top-left-radius: 9px; border-bottom-left-radius: 9px; background: $primary; }
#ToastBar[severity="critical"] { background: $danger; }
#ToastBar[severity="warning"] { background: $warning; }
#ToastThumb { background: $video_bg; border-radius: 6px; }
#MonitorPill { background: $bg; border: 1px solid $border; border-radius: 8px; }
#StateDot { border-radius: 4px; background: $muted; }
#StateDot[state="on"] { background: $success; }
#StateDot[state="busy"] { background: $warning; }
#StateDot[state="error"] { background: $danger; }
"""


# --- Plain-language helpers ------------------------------------------------------

def verdict_badge(alert) -> tuple[str, str]:
    """(text, tone) for the badge of an alert; tones match #Badge[tone=...]."""
    if alert.status == "false_alarm":
        return "False alarm", "neutral"
    # Confirmed = the AI saw the person / car is really there; red only when it also saw suspicious
    # behaviour (the alert became critical)
    return {
        "confirmed": ("Confirmed", "danger" if alert.severity == "critical" else "primary"),
        "pending": ("Checking…", "primary"),
        "rejected": ("Dismissed by AI", "muted"),
    }.get(alert.verdict, ("Not verified", "neutral"))


def is_unread(alert) -> bool:
    """New and not dismissed by the brain (same rule as AlertStore.unread_count)."""
    return alert.status == "new" and alert.verdict != "rejected"


def is_dismissed(alert) -> bool:
    return alert.status == "false_alarm" or alert.verdict == "rejected"


def matches(alert, key: str) -> bool:
    if key == "new":
        return is_unread(alert)
    if key == "confirmed":
        return alert.verdict == "confirmed" and alert.status != "false_alarm"
    if key == "review":
        return alert.verdict in ("pending", "unverified") and alert.status != "false_alarm"
    if key == "false":
        return is_dismissed(alert)
    return True


def rule_name(rule: str) -> str:
    if rule.startswith("area_") and rule not in RULES:  # raised by an area drawn in Live view
        from .config import TEMPLATE_INFO
        info = TEMPLATE_INFO.get(rule[5:])
        return f"Area: {info['title']}" if info else "Area"
    return RULES.get(rule, (rule.replace("_", " ").capitalize(), ""))[0]


def rule_text(rule: str, settings) -> str:
    if rule.startswith("area_") and rule not in RULES:
        return "An area you drew in Live view raised this alert. Its full log is on the Areas page."
    text = RULES.get(rule, ("", ""))[1]
    return text.format(night=f"{settings.night_start}–{settings.night_end}", loiter=settings.loiter_seconds,
                       cars=settings.prowl_cars, window=max(1, round(settings.prowl_window_s / 60)))


def rel_time(ts: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    age = max(0.0, now - ts)
    if age < 45:
        return "just now"
    if age < 3600:
        return f"{max(1, round(age / 60))} min ago"
    dt, today = datetime.fromtimestamp(ts), date.fromtimestamp(now)
    if dt.date() == today:
        return f"{int(age // 3600)} h ago" if age < 4 * 3600 else f"Today {dt:%H:%M}"
    if dt.date() == today - timedelta(days=1):
        return f"Yesterday {dt:%H:%M}"
    if (today - dt.date()).days < 7:
        return f"{dt:%a %H:%M}"
    return f"{dt.day} {dt:%b %H:%M}"


def full_time(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"{dt:%A} {dt.day} {dt:%b %Y}, {dt:%H:%M:%S}"


def detail_meta(alert) -> str:
    when, rel = full_time(alert.ts), rel_time(alert.ts)
    if rel == "just now" or rel.endswith("ago"):  # other forms just repeat the date
        when += f" ({rel})"
    return f"{alert.camera} (CH {alert.channel}) · {when}"


def _mix(color: str, base: str, amount: float) -> QColor:
    a, b = QColor(color), QColor(base)
    return QColor(round(a.red() * amount + b.red() * (1 - amount)),
                  round(a.green() * amount + b.green() * (1 - amount)),
                  round(a.blue() * amount + b.blue() * (1 - amount)))


def _tone_colors(p: dict, tone: str) -> tuple[QColor | None, QColor, QColor | None]:
    """(fill, text, outline) for a badge tone, painted to match the #Badge style sheet rules."""
    if tone == "danger":
        return QColor(p["danger_hover"]), QColor(p["danger"]), None
    if tone == "primary":
        return QColor(p["nav_active_bg"]), QColor(p["nav_active_text"]), None
    if tone == "muted":
        return None, QColor(p["muted"]), QColor(p["border_strong"])
    return QColor(p["surface2"]), QColor(p["muted"]), None


def load_image(path: str, max_size: QSize | None = None, cover: bool = False) -> QImage | None:
    """Read a JPEG from disk, decoding at reduced size when asked (safe off the GUI thread)."""
    try:
        # One quick read, so the file is not held open while decoding (it may be deleted meanwhile).
        data = QByteArray(Path(path).read_bytes()) if path else None
    except OSError:
        return None
    if not data:
        return None
    buf = QBuffer(data)
    buf.open(QIODevice.ReadOnly)
    reader = QImageReader(buf)
    reader.setAutoTransform(True)
    if max_size is not None and reader.size().isValid():
        mode = Qt.KeepAspectRatioByExpanding if cover else Qt.KeepAspectRatio
        reader.setScaledSize(reader.size().scaled(max_size, mode))
    img = reader.read()
    if img.isNull():
        return None
    if cover and max_size is not None:
        x, y = (img.width() - max_size.width()) // 2, (img.height() - max_size.height()) // 2
        img = img.copy(max(0, x), max(0, y), max_size.width(), max_size.height())
    return img


def show_in_explorer(path: str) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    subprocess.Popen(f'explorer /select,"{p}"', creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True


def _rounded_pixmap(img: QImage, size: QSize, radius: float, dpr: float) -> QPixmap:
    pm = QPixmap(int(size.width() * dpr), int(size.height() * dpr))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, pm.width(), pm.height()), radius * dpr, radius * dpr)
    painter.setClipPath(path)
    painter.drawImage(QRect(0, 0, pm.width(), pm.height()), img)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


# --- Alert list ------------------------------------------------------------------

class _AlertDelegate(QStyledItemDelegate):
    """Paints one alert row: thumbnail, title, camera and time, verdict badge."""

    def __init__(self, page: "AlertsPage"):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        a = self.page.alert_for(index.data(ID_ROLE))
        if a is None:
            return
        p = self.page.ctx.palette
        r = option.rect.adjusted(0, 1, 0, -1)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        selected = bool(option.state & QStyle.State_Selected)
        fill = None
        if selected:
            fill = QColor(p["nav_active_bg"])
        elif option.state & QStyle.State_MouseOver:
            fill = QColor(p["surface2"])
        glow = self.page.glow(a.id)
        if glow > 0 and not selected:  # just arrived: a soft tint that fades out
            fill = _mix(p["warning"], (fill or QColor(p["surface"])).name(), 0.16 * glow)
        if fill is not None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(QRectF(r), 7, 7)
        if glow > 0:
            bar = QColor(p["warning"])
            bar.setAlphaF(glow)
            painter.setPen(Qt.NoPen)
            painter.setBrush(bar)
            painter.drawRoundedRect(QRectF(r.left(), r.top() + 10, 3, r.height() - 20), 1.5, 1.5)
        if selected and self.page.list.hasFocus():
            painter.setPen(QPen(QColor(p["primary"]), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)

        thumb = QRect(r.left() + 10, r.top() + (r.height() - THUMB.height()) // 2, THUMB.width(), THUMB.height())
        pm = self.page.thumb(a)
        if pm is None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["video_bg"]))
            painter.drawRoundedRect(QRectF(thumb), 6, 6)
        else:
            dismissed = is_dismissed(a)
            painter.setOpacity(0.5 if dismissed else 1.0)  # dismissed alerts stay listed, but step back
            painter.drawPixmap(thumb, pm)
            painter.setOpacity(1.0)

        unread = is_unread(a)
        x, right = thumb.right() + 12, r.right() - 10
        base = QFont(option.font)
        title_font = QFont(base)
        title_font.setWeight(QFont.Bold if unread else QFont.DemiBold)
        meta_font = QFont(base)
        meta_font.setPointSizeF(base.pointSizeF() * 0.9)
        badge_font = QFont(base)
        badge_font.setPointSizeF(base.pointSizeF() * 0.82)
        badge_font.setWeight(QFont.DemiBold)
        tf, mf, bf = QFontMetrics(title_font), QFontMetrics(meta_font), QFontMetrics(badge_font)
        bh = bf.height() + 3
        line2 = tf.height() + 2
        line3 = line2 + mf.height() + 5
        top = r.top() + (r.height() - (line3 + bh)) // 2

        # Third line: verdict badge, then the rule in plain words.
        text, tone = verdict_badge(a)
        bw = bf.horizontalAdvance(text) + 16
        brect = QRectF(x, top + line3, bw, bh)
        bg, fg, outline = _tone_colors(p, tone)
        if selected and bg is not None and bg == fill:
            bg = QColor(p["surface"])  # keep the badge visible on the selection colour
        painter.setPen(QPen(outline, 1) if outline else Qt.NoPen)
        painter.setBrush(bg if bg else Qt.NoBrush)
        painter.drawRoundedRect(brect.adjusted(0.5, 0.5, -0.5, -0.5), bh / 2, bh / 2)
        painter.setFont(badge_font)
        painter.setPen(fg)
        painter.drawText(brect, Qt.AlignCenter, text)
        rx = int(brect.right()) + 8
        painter.setFont(meta_font)
        painter.setPen(QColor(p["muted"]))
        painter.drawText(QRect(rx, int(brect.top()), right - rx, bh), Qt.AlignLeft | Qt.AlignVCenter,
                         mf.elidedText(rule_name(a.rule), Qt.ElideRight, right - rx))

        tx = x
        if unread:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["primary"]))
            painter.drawEllipse(QRectF(x, top + tf.height() / 2 - 3.5, 7, 7))
            tx += 13
        painter.setFont(title_font)
        painter.setPen(QColor(p["muted"] if is_dismissed(a) else p["text"]))
        painter.drawText(QRect(tx, top, right - tx, tf.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         tf.elidedText(a.title, Qt.ElideRight, right - tx))
        painter.setFont(meta_font)
        painter.setPen(QColor(p["muted"]))
        when = rel_time(a.ts)
        camera = mf.elidedText(a.camera, Qt.ElideRight, max(40, right - x - mf.horizontalAdvance(f" · {when}")))
        painter.drawText(QRect(x, top + line2, right - x, mf.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         f"{camera} · {when}")  # the time never gets cut off, a long camera name does
        painter.restore()


# --- Detail pane -----------------------------------------------------------------

class _DetailPane(QFrame):
    """Large picture of the selected alert with its explanation and actions."""

    def __init__(self, page: "AlertsPage"):
        super().__init__()
        self.page, self.ctx = page, page.ctx
        self.setObjectName("Card")
        self.alert = None
        self._gen = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        placeholder = QWidget()
        pl = QVBoxLayout(placeholder)
        pl.addStretch(1)
        hint = muted("Select an alert to see the picture and what happened.")
        hint.setAlignment(Qt.AlignCenter)
        pl.addWidget(hint)
        pl.addStretch(1)
        self.stack.addWidget(placeholder)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("Transparent")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        scroll.setWidget(body)
        self.stack.addWidget(scroll)

        self.image = ImageView("Loading picture…")
        self.image.setCursor(Qt.PointingHandCursor)
        self.image.setToolTip("Open the full-size picture")
        self.image.clicked.connect(self._open_picture)
        lay.addWidget(self.image)

        self.title = label("", "DetailTitle", wrap=True)
        lay.addSpacing(4)
        lay.addWidget(self.title)
        sub = QHBoxLayout()
        sub.setSpacing(10)
        self.badge = label("", "Badge")
        self.meta = muted()
        sub.addWidget(self.badge, 0, Qt.AlignVCenter)
        sub.addWidget(self.meta, 1)
        lay.addLayout(sub)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.live_btn = button("Open live view", "primary", "Watch this camera live (Enter)")
        self.ctx.bind_icon(self.live_btn, "video", "on_primary")
        self.live_btn.clicked.connect(lambda: self.alert and self.page.open_live(self.alert))
        self.seen_btn = button("Mark as new")
        self.seen_btn.clicked.connect(lambda: self.alert and self.page.toggle_seen(self.alert))
        self.false_btn = button("Mark as false alarm", tooltip="Nothing wrong happened here (F)")
        self.false_btn.clicked.connect(lambda: self.alert and self.page.toggle_false_alarm(self.alert))
        self.full_btn = button("", "ghost", "Open the full-size picture")
        self.full_btn.setAccessibleName("Open full-size picture")
        self.ctx.bind_icon(self.full_btn, "maximize", "muted")
        self.full_btn.clicked.connect(self._open_picture)
        self.file_btn = button("", "ghost", "Show the picture file in Explorer")
        self.file_btn.setAccessibleName("Show file")
        self.ctx.bind_icon(self.file_btn, "folder", "muted")
        self.file_btn.clicked.connect(lambda: self.alert and self.page.show_file(self.alert))
        self.delete_btn = button("", "ghost", "Delete this alert and its pictures (Delete)")
        self.delete_btn.setAccessibleName("Delete alert")
        self.ctx.bind_icon(self.delete_btn, "trash", "danger")
        self.delete_btn.clicked.connect(lambda: self.alert and self.page.delete_alert(self.alert))
        for w in (self.live_btn, self.seen_btn, self.false_btn):
            acts.addWidget(w)
        acts.addStretch(1)
        for w in (self.full_btn, self.file_btn, self.delete_btn):
            acts.addWidget(w)
        lay.addLayout(acts)

        lay.addSpacing(4)
        lay.addWidget(self._divider())
        lay.addWidget(label("What happened", "CardTitle"))
        self.rule = muted()
        lay.addWidget(self.rule)
        self.detail = muted()
        lay.addWidget(self.detail)
        lay.addWidget(self._divider())
        lay.addWidget(label("AI check", "CardTitle"))
        self.ai = muted()
        lay.addWidget(self.ai)
        self.findings = label("", wrap=True)
        lay.addWidget(self.findings)
        lay.addStretch(1)

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("Divider")
        return line

    def show_alert(self, alert) -> None:
        same = self.alert is not None and alert is not None and self.alert.id == alert.id
        self.alert = alert
        if alert is None:
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        self.title.setText(alert.title)
        text, tone = verdict_badge(alert)
        self.badge.setText(text)
        set_prop(self.badge, "tone", tone)
        self.meta.setText(detail_meta(alert))
        self.seen_btn.setText("Mark as new" if alert.status != "new" else "Mark as seen")
        self.seen_btn.setToolTip("Show this alert as unread again" if alert.status != "new" else "Mark as read")
        self.false_btn.setText("Not a false alarm" if alert.status == "false_alarm" else "Mark as false alarm")
        self.rule.setText(rule_text(alert.rule, self.ctx.cfg.settings) or rule_name(alert.rule))
        self.detail.setText(alert.detail)
        self.detail.setVisible(bool(alert.detail))
        self._fill_ai(alert)
        # Reload on updates too: the engine redraws the snapshot with the AI brain's boxes.
        self._load_picture(alert, keep_old=same)

    def _fill_ai(self, a) -> None:
        found = [f for f in (a.brain_findings or []) if f]
        texts = {
            "pending": "The AI brain is taking a closer look at this moment…",
            "confirmed": "The AI brain looked closer and confirmed it.",
            "rejected": "The AI brain looked closer and found nothing there, so it dismissed this alert.",
            # findings on an unverified alert say why the brain could not judge it (too far away...)
            "unverified": "The AI brain looked but could not judge this one." if found else
                          "Not double-checked: the AI brain was not running when this happened. "
                          "You can turn it on in Settings.",
        }
        self.ai.setText(texts.get(a.verdict, texts["unverified"]))
        self.findings.setText("\n".join(f"•  {f}" for f in found))
        self.findings.setVisible(bool(found))

    def _load_picture(self, alert, keep_old: bool = False) -> None:
        """The engine saves the snapshot with the boxes already drawn, so it is shown as is."""
        self._gen += 1
        if not keep_old:
            self.image.set_image(None, "Loading picture…")
        gen, path = self._gen, alert.snapshot_path
        run_async(load_image, path, QSize(1920, 1080), on_done=lambda img: self._picture(gen, img))

    def _picture(self, gen: int, img) -> None:
        if gen != self._gen:
            return
        if isinstance(img, QImage):
            self.image.set_image(img)
        else:
            self.image.set_image(None, "The picture file is missing.")

    def _open_picture(self) -> None:
        if self.alert and Path(self.alert.snapshot_path).exists():
            os.startfile(self.alert.snapshot_path)


# --- Alerts page -----------------------------------------------------------------

class AlertsPage(QWidget):
    """Everything the monitor noticed: filterable list on the left, the selected alert on the right."""

    unread_changed = Signal(int)

    def __init__(self, ctx, win):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.alerts: dict[str, object] = {}  # id -> Alert, newest first by ts
        self._thumbs: dict[str, QPixmap | None] = {}
        self._loading: set[str] = set()
        self._fresh: dict[str, float] = {}
        self._filter = "all"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(16)
        self.header = PageHeader("Alerts")
        self.seen_all_btn = button("Mark all as seen", tooltip="Mark every new alert as read")
        ctx.bind_icon(self.seen_all_btn, "check")
        self.seen_all_btn.clicked.connect(self.mark_all_seen)
        folder_btn = button("Open folder", tooltip="Open the folder with the saved alert pictures")
        ctx.bind_icon(folder_btn, "folder")
        folder_btn.clicked.connect(self.open_folder)
        self.header.actions.addWidget(self.seen_all_btn)
        self.header.actions.addWidget(folder_btn)
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
            chip.setChecked(key == "all")
            self.chips.addButton(chip)
            bar.addWidget(chip)
        self.chips.buttonClicked.connect(self._chip_clicked)
        bar.addStretch(1)
        self.camera_combo = QComboBox()
        self.camera_combo.setAccessibleName("Camera filter")
        self.camera_combo.setToolTip("Show alerts from one camera only")
        self.camera_combo.setMinimumWidth(200)
        self.camera_combo.currentIndexChanged.connect(lambda *_: self._refill())
        bar.addWidget(self.camera_combo)
        lay.addWidget(self.filter_bar)

        self.body = QStackedWidget()
        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)

        self.left = QStackedWidget()
        self.left.setFixedWidth(380)
        self.list = QListWidget()
        self.list.setObjectName("AlertList")
        self.list.setItemDelegate(_AlertDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setAccessibleName("Alerts")
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.itemActivated.connect(lambda item: self._activate(item))
        self.list.installEventFilter(self)
        self.left.addWidget(self.list)
        no_match = QFrame()
        no_match.setObjectName("Card")
        nl = QVBoxLayout(no_match)
        nl.setContentsMargins(24, 24, 24, 24)
        nl.setSpacing(10)
        nl.addStretch(1)
        self.no_match_text = muted("No alerts match this filter.")
        self.no_match_text.setAlignment(Qt.AlignCenter)
        nl.addWidget(self.no_match_text)
        show_all = button("Show all alerts")
        show_all.clicked.connect(self.clear_filters)
        nl.addWidget(show_all, 0, Qt.AlignCenter)
        nl.addStretch(2)
        self.left.addWidget(no_match)
        row.addWidget(self.left)

        self.detail = _DetailPane(self)
        row.addWidget(self.detail, 1)
        self.empty = EmptyState(ctx)
        self.body.addWidget(content)
        self.body.addWidget(self.empty)
        lay.addWidget(self.body, 1)

        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(40)
        self._glow_timer.timeout.connect(self._tick_glow)
        self._clock = QTimer(self)  # keeps "5 min ago" current
        self._clock.setInterval(30_000)
        self._clock.timeout.connect(self._tick_clock)
        self._clock.start()

        bridge = getattr(ctx, "alert_bridge", None)
        if bridge is not None:
            bridge.alert_added.connect(self.add_alert)
            bridge.alert_updated.connect(self.update_alert)

    # --- data ---------------------------------------------------------------------

    @property
    def store(self):
        return self.ctx.alert_store

    def alert_for(self, alert_id):
        return self.alerts.get(alert_id)

    def reload(self) -> None:
        current = self._current_id()
        try:
            items = self.store.list(limit=500)
        except Exception as exc:  # database locked / unreadable: keep what we have
            self.win.notify(f"Could not read alerts: {exc}", error=True)
            return
        self.alerts = {a.id: a for a in sorted(items, key=lambda a: a.ts, reverse=True)}
        self._refill(select=current)

    def add_alert(self, alert) -> None:
        self.alerts[alert.id] = alert
        self.alerts = dict(sorted(self.alerts.items(), key=lambda kv: kv[1].ts, reverse=True))
        self._fresh[alert.id] = time.monotonic()
        self._glow_timer.start()
        self._refill(select=self._current_id())

    def update_alert(self, alert) -> None:
        if alert.id not in self.alerts:
            self.add_alert(alert)
            return
        old = self.alerts[alert.id]
        self.alerts[alert.id] = alert
        if old.crop_path != alert.crop_path or old.snapshot_path != alert.snapshot_path:
            self._thumbs.pop(alert.id, None)
        current = self._current_id()
        if self._row_matches(alert) != (self._row_of(alert.id) is not None):
            self._refill(select=current)  # it moved in or out of the active filter
        else:
            self._update_counts()
            self.list.viewport().update()
        if current == alert.id:
            self.detail.show_alert(alert)

    def thumb(self, alert) -> QPixmap | None:
        if alert.id in self._thumbs:
            return self._thumbs[alert.id]
        if alert.id not in self._loading:
            self._loading.add(alert.id)
            size = THUMB * 2  # sharp on high-DPI screens
            run_async(load_image, alert.crop_path or alert.snapshot_path, size, True,
                      on_done=lambda img, aid=alert.id: self._thumb_loaded(aid, img))
        return None

    def _thumb_loaded(self, alert_id: str, img) -> None:
        self._loading.discard(alert_id)
        self._thumbs[alert_id] = (_rounded_pixmap(img, THUMB, 6, 2.0) if isinstance(img, QImage) else None)
        self.list.viewport().update()

    def glow(self, alert_id: str) -> float:
        start = self._fresh.get(alert_id)
        if start is None:
            return 0.0
        return max(0.0, 1.0 - (time.monotonic() - start) / 4.0)

    def _tick_glow(self) -> None:
        now = time.monotonic()
        self._fresh = {k: t for k, t in self._fresh.items() if now - t < 4.0}
        if not self._fresh:
            self._glow_timer.stop()
        self.list.viewport().update()

    def _tick_clock(self) -> None:
        if self.isVisible():
            self.list.viewport().update()
            if self.detail.alert is not None:
                a = self.detail.alert
                self.detail.meta.setText(detail_meta(a))

    # --- filtering ----------------------------------------------------------------

    def _camera_key(self) -> str | None:
        return self.camera_combo.currentData() or None

    @staticmethod
    def _cam_id(a) -> str:
        return f"{a.site_id}:{a.channel}"  # a plain string survives the round trip through Qt item data

    def _row_matches(self, a) -> bool:
        cam = self._camera_key()
        return matches(a, self._filter) and (cam is None or cam == self._cam_id(a))

    def _fill_cameras(self) -> None:
        keep = self._camera_key()
        cams: dict[str, object] = {}
        for a in self.alerts.values():
            cams.setdefault(self._cam_id(a), a)
        sites = {a.site_id for a in cams.values()}
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        self.camera_combo.addItem("All cameras", "")
        for key, a in sorted(cams.items(), key=lambda kv: (kv[1].site_id, kv[1].channel)):
            site = self.ctx.cfg.site(a.site_id) if len(sites) > 1 else None
            self.camera_combo.addItem(f"{a.camera} · {site.name}" if site else a.camera, key)
        idx = self.camera_combo.findData(keep) if keep is not None else 0
        self.camera_combo.setCurrentIndex(max(0, idx))
        self.camera_combo.setEnabled(len(cams) > 1 or keep is not None)
        self.camera_combo.blockSignals(False)

    def _refill(self, select: str | None = None) -> None:
        self._fill_cameras()
        if select is None:
            select = self._current_id()
        self.list.blockSignals(True)
        self.list.clear()
        for a in self.alerts.values():
            if self._row_matches(a):
                item = QListWidgetItem()
                item.setData(ID_ROLE, a.id)
                item.setData(Qt.AccessibleTextRole, f"{a.title}, {a.camera}, {rel_time(a.ts)}, {verdict_badge(a)[0]}")
                self.list.addItem(item)
        self.list.blockSignals(False)
        self._update_counts()
        self.filter_bar.setVisible(bool(self.alerts))
        if not self.alerts:
            self._show_empty()
            return
        self.body.setCurrentIndex(0)
        if self.list.count() == 0:
            self.left.setCurrentIndex(1)
            names = {k: t for k, t, _ in FILTERS}
            cam = self.camera_combo.currentText() if self._camera_key() else ""
            what = names[self._filter].lower() if self._filter != "all" else "alerts"
            self.no_match_text.setText(f"No {what} for {cam}." if cam else f"No {what} right now.")
            self.detail.show_alert(None)
            return
        self.left.setCurrentIndex(0)
        row = self._row_of(select) if select else None
        self.list.setCurrentRow(row if row is not None else 0)

    def _show_empty(self) -> None:
        mon = getattr(self.ctx, "monitor", None)
        running = bool(mon and mon.running)
        if running:
            self.empty.configure("shield", "All quiet",
                                 "Monitoring is on. Anything suspicious near your cars will show up here.",
                                 "Open live view", lambda: self.win.go(self._page_index("live", 1)))
        elif hasattr(self.win, "set_monitoring"):
            self.empty.configure("shield", "No alerts yet",
                                 "Turn on monitoring and YardGuard will watch your parking cameras and "
                                 "list anything suspicious here.",
                                 "Turn on monitoring", lambda: self.win.set_monitoring(True))
        else:
            self.empty.configure("shield", "No alerts yet",
                                 "When monitoring is on, anything suspicious near your cars will show up here.",
                                 "Open live view", lambda: self.win.go(self._page_index("live", 1)))
        self.body.setCurrentIndex(1)
        self.detail.show_alert(None)

    def _page_index(self, attr: str, default: int) -> int:
        page = getattr(self.win, attr, None)
        stack = getattr(self.win, "stack", None)
        return stack.indexOf(page) if page is not None and stack is not None else default

    def _update_counts(self) -> None:
        cam = self._camera_key()
        pool = [a for a in self.alerts.values() if cam is None or cam == self._cam_id(a)]
        for chip in self.chips.buttons():
            key = chip.property("key")
            n = sum(1 for a in pool if matches(a, key))
            name = next(t for k, t, _ in FILTERS if k == key)
            chip.setText(f"{name}  {n}" if n else name)
        total = len(self.alerts)
        new = sum(1 for a in self.alerts.values() if is_unread(a))
        review = sum(1 for a in self.alerts.values() if matches(a, "review"))
        if total:
            parts = [f"{total} alert{'s' if total != 1 else ''}", f"{new} new"]
            if review:
                parts.append(f"{review} need{'s' if review == 1 else ''} review")
            self.header.subtitle.setText(" · ".join(parts))
        else:
            self.header.subtitle.setText("Suspicious moments the cameras noticed")
        self.seen_all_btn.setEnabled(any(a.status == "new" for a in self.alerts.values()))
        self.unread_changed.emit(new)

    def _chip_clicked(self, chip) -> None:
        self._filter = chip.property("key")
        self._refill()

    def clear_filters(self) -> None:
        self._filter = "all"
        for chip in self.chips.buttons():
            chip.setChecked(chip.property("key") == "all")
        self.camera_combo.setCurrentIndex(0)
        self._refill()

    def _current_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(ID_ROLE) if item else None

    def _row_of(self, alert_id: str | None) -> int | None:
        for i in range(self.list.count()):
            if self.list.item(i).data(ID_ROLE) == alert_id:
                return i
        return None

    # --- selection & actions ------------------------------------------------------

    def _current_changed(self, item, _previous=None) -> None:
        a = self.alerts.get(item.data(ID_ROLE)) if item else None
        # Only an alert the user actually looks at is "seen": the list also selects its top row
        # by itself when a new alert arrives while another page is open.
        if a is not None and a.status == "new" and self.isVisible():
            self._set_status(a, "seen")
        self.detail.show_alert(a)

    def _activate(self, item) -> None:
        a = self.alerts.get(item.data(ID_ROLE)) if item else None
        if a is not None:
            self.open_live(a)

    def show_alert(self, alert_id: str) -> None:
        """Open one alert (from a toast): clears filters that would hide it."""
        if alert_id not in self.alerts:
            a = self.store.get(alert_id)
            if a is None:
                self.win.notify("That alert was deleted.", error=True)
                return
            self.alerts[a.id] = a
            self.alerts = dict(sorted(self.alerts.items(), key=lambda kv: kv[1].ts, reverse=True))
        if not self._row_matches(self.alerts[alert_id]):
            self._filter = "all"
            for chip in self.chips.buttons():
                chip.setChecked(chip.property("key") == "all")
            self.camera_combo.blockSignals(True)
            self.camera_combo.setCurrentIndex(0)
            self.camera_combo.blockSignals(False)
        self._refill(select=alert_id)
        row = self._row_of(alert_id)
        if row is not None:
            self.list.scrollToItem(self.list.item(row), QAbstractItemView.PositionAtCenter)
        self.list.setFocus()

    def _set_status(self, a, status: str) -> None:
        try:
            self.store.mark(a.id, status)
        except Exception as exc:
            self.win.notify(f"Could not update the alert: {exc}", error=True)
            return
        a.status = status
        self._update_counts()
        self.list.viewport().update()

    def toggle_seen(self, a) -> None:
        new = a.status != "new"
        self._set_status(a, "new" if new else "seen")
        self.detail.show_alert(a)
        self.win.notify("Marked as new." if new else "Marked as seen.")

    def toggle_false_alarm(self, a) -> None:
        undo = a.status == "false_alarm"
        self._set_status(a, "seen" if undo else "false_alarm")
        self.win.notify("Alert restored." if undo else "Marked as false alarm.")
        if self._row_matches(a):
            self.detail.show_alert(a)
        else:
            self._select_neighbour_and_refill(a.id)

    def _neighbour(self, alert_id: str) -> str | None:
        """The row to select after this one leaves the list: the next one, else the previous one."""
        row = self._row_of(alert_id)
        if row is not None:
            for r in (row + 1, row - 1):
                if 0 <= r < self.list.count():
                    return self.list.item(r).data(ID_ROLE)
        return None

    def _select_neighbour_and_refill(self, alert_id: str) -> None:
        self._refill(select=self._neighbour(alert_id))

    def mark_all_seen(self) -> None:
        fresh = [a for a in self.alerts.values() if a.status == "new"]
        for a in fresh:
            try:
                self.store.mark(a.id, "seen")
                a.status = "seen"
            except Exception as exc:
                self.win.notify(f"Could not update alerts: {exc}", error=True)
                break
        self._refill()
        self.win.notify(f"Marked {len(fresh)} alert{'s' if len(fresh) != 1 else ''} as seen.")

    def delete_alert(self, a) -> None:
        box = QMessageBox(QMessageBox.Warning, "Delete alert",
                          f"Delete “{a.title}” from {a.camera}?\n\nIts saved pictures will be deleted too. "
                          "This cannot be undone.", parent=self)
        delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return
        try:
            self.store.delete(a.id)
        except Exception as exc:
            if self.store.get(a.id) is not None:  # the record is still there: really failed
                self.win.notify(f"Could not delete the alert: {exc}", error=True)
                return
            # Only removing a picture failed (file briefly open elsewhere); retried below.
        self._remove_files([a.snapshot_path, a.crop_path])
        nxt = self._neighbour(a.id)
        self.alerts.pop(a.id, None)
        self._thumbs.pop(a.id, None)
        self._refill(select=nxt)
        self.win.notify("Alert deleted.")

    def _remove_files(self, paths: list[str], tries: int = 5) -> None:
        """Delete the alert pictures; Windows refuses while a file is open, so retry a few times."""
        left = []
        for path in paths:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    left.append(path)
        if left and tries > 1:
            QTimer.singleShot(600, lambda: self._remove_files(left, tries - 1))

    def open_live(self, a) -> None:
        cfg = self.ctx.cfg
        if a.site_id and a.site_id != cfg.active_site_id and cfg.site(a.site_id):
            cfg.active_site_id = a.site_id
            self.ctx.save()
        self.win.open_live(a.channel)

    def show_file(self, a) -> None:
        if not show_in_explorer(a.snapshot_path):
            self.win.notify("The picture file for this alert is missing.", error=True)

    def open_folder(self) -> None:
        path = Path(self.ctx.cfg.settings.events_dir) / "alerts"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def eventFilter(self, obj, event):
        if obj is self.list and event.type() == QEvent.KeyPress:
            a = self.alerts.get(self._current_id())
            if a is not None and event.key() == Qt.Key_Delete:
                self.delete_alert(a)
                return True
            if a is not None and event.key() == Qt.Key_F and not event.modifiers():
                self.toggle_false_alarm(a)
                return True
        return False

    def showEvent(self, event):
        super().showEvent(event)
        self.list.viewport().update()
        self._current_changed(self.list.currentItem())  # the selected alert is now in view


# --- Toasts ----------------------------------------------------------------------

class _Toast(QFrame):
    clicked = Signal(str)
    closed = Signal(object)

    WIDTH = 360

    def __init__(self, ctx, alert, parent: QWidget):
        super().__init__(parent)
        self.ctx, self.alert_id = ctx, alert.id
        self.setObjectName("Toast")
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(shadow)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.bar = QFrame()
        self.bar.setObjectName("ToastBar")
        self.bar.setFixedWidth(5)
        row.addWidget(self.bar)
        inner = QHBoxLayout()
        inner.setContentsMargins(12, 12, 8, 12)
        inner.setSpacing(12)
        self.thumb = QLabel()
        self.thumb.setObjectName("ToastThumb")
        self.thumb.setFixedSize(72, 44)
        inner.addWidget(self.thumb, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label("", "CardTitle")
        self.meta = muted(wrap=False)
        self.verdict = muted(wrap=False)
        col.addWidget(self.title)
        col.addWidget(self.meta)
        col.addWidget(self.verdict)
        inner.addLayout(col, 1)
        close = button("", "ghost", "Dismiss")
        close.setAccessibleName("Dismiss notification")
        close.setFixedSize(28, 28)
        close.setStyleSheet("padding: 0;")
        ctx.bind_icon(close, "x", "muted")
        close.clicked.connect(lambda: self.closed.emit(self))
        inner.addWidget(close, 0, Qt.AlignTop)
        row.addLayout(inner, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.closed.emit(self))
        self.set_alert(alert, load_thumb=True)

    def set_alert(self, alert, load_thumb: bool = False) -> None:
        set_prop(self.bar, "severity", alert.severity)
        avail = self.WIDTH - 5 - 12 - 72 - 12 - 28 - 8 - 12
        self.title.ensurePolished()  # the style sheet's bold title font, not the default one
        self.meta.ensurePolished()
        fm = QFontMetrics(self.title.font())
        self.title.setText(fm.elidedText(alert.title, Qt.ElideRight, avail))
        self.title.setToolTip(alert.title)
        self.meta.setText(QFontMetrics(self.meta.font()).elidedText(
            f"{alert.camera} · {datetime.fromtimestamp(alert.ts):%H:%M}", Qt.ElideRight, avail))
        self.verdict.setText({
            "pending": "AI brain is checking…",
            "confirmed": "Confirmed by the AI brain",
            "rejected": "Dismissed by the AI brain",
        }.get(alert.verdict, "Click to see the picture"))
        if load_thumb:
            img = load_image(alert.crop_path or alert.snapshot_path, QSize(144, 88), cover=True)
            if img is not None:
                self.thumb.setPixmap(_rounded_pixmap(img, QSize(72, 44), 6, 2.0))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.alert_id)
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.timer.stop()  # stay while the user reads it
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.timer.start(4000)
        super().leaveEvent(event)


class ToastManager(QObject):
    """Stacked, non-modal alert cards in the top-right corner of the main window."""

    open_alert = Signal(str)

    MAX = 3
    MARGIN = 20
    TOP = 88     # below the page header, so its action buttons (Refresh, Mark all as seen) stay usable
    GAP = 10
    TIMEOUT_MS = 8000

    def __init__(self, window: QWidget, ctx):
        super().__init__(window)
        self.window, self.ctx = window, ctx
        self.toasts: list[_Toast] = []  # newest first
        self._anims: dict[_Toast, QPropertyAnimation] = {}
        window.installEventFilter(self)

    def show_alert(self, alert) -> None:
        for t in self.toasts:
            if t.alert_id == alert.id:
                t.set_alert(alert)
                return
        toast = _Toast(self.ctx, alert, self.window)
        toast.clicked.connect(self._clicked)
        toast.closed.connect(self._close)
        toast.adjustSize()
        self.toasts.insert(0, toast)
        while len(self.toasts) > self.MAX:
            self._close(self.toasts[-1])
        x, y = self._slot(0, toast)
        toast.move(x + 40, y)
        toast.show()
        toast.raise_()
        toast.timer.start(self.TIMEOUT_MS)
        self._layout(animate_new=toast)
        if alert.severity in ("warning", "critical"):
            QApplication.alert(self.window, 0)  # flash the taskbar button if the window is in the background

    def update_alert(self, alert) -> None:
        """Refresh a visible toast when the AI check finishes; dismissed alerts leave quickly."""
        for t in self.toasts:
            if t.alert_id == alert.id:
                t.set_alert(alert)
                if alert.verdict == "rejected" and not t.underMouse():
                    t.timer.start(2500)

    def clear(self) -> None:
        for t in list(self.toasts):
            self._close(t)

    def _slot(self, index: int, toast: _Toast) -> tuple[int, int]:
        y = self.TOP
        for t in self.toasts[:index]:
            y += t.height() + self.GAP
        return self.window.width() - toast.width() - self.MARGIN, y

    def _layout(self, animate_new: _Toast | None = None) -> None:
        for i, t in enumerate(self.toasts):
            x, y = self._slot(i, t)
            anim = self._anims.get(t)
            if anim is None:
                anim = QPropertyAnimation(t, b"pos", self)
                anim.setDuration(180)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                self._anims[t] = anim
            anim.stop()
            if t.isVisible() and (t is animate_new or t.pos() != QPoint(x, y)):
                anim.setStartValue(t.pos())
                anim.setEndValue(QPoint(x, y))
                anim.start()
            else:
                t.move(x, y)
            t.raise_()

    def _clicked(self, alert_id: str) -> None:
        self.open_alert.emit(alert_id)
        for t in list(self.toasts):
            if t.alert_id == alert_id:
                self._close(t)

    def _close(self, toast: _Toast) -> None:
        if toast not in self.toasts:
            return
        self.toasts.remove(toast)
        anim = self._anims.pop(toast, None)
        if anim is not None:
            anim.stop()
        toast.timer.stop()
        toast.hide()
        toast.deleteLater()
        self._layout()

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QEvent.Resize and self.toasts:
            self._layout()
        return False


# --- Sidebar monitoring switch ---------------------------------------------------

class Switch(QAbstractButton):
    """On/off switch in the Windows 11 style."""

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(40, 22)
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._moved)
        self.toggled.connect(self._animate)

    def _moved(self, value) -> None:
        self._pos = float(value)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def set_checked_quietly(self, on: bool) -> None:
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(40, 22)

    def paintEvent(self, event):
        p = self.ctx.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = QRectF(1, 1, self.width() - 2, self.height() - 2)
        radius = track.height() / 2
        on = self.isChecked()
        if not self.isEnabled():
            painter.setPen(QPen(QColor(p["border"]), 1))
            painter.setBrush(QColor(p["surface2"]))
        elif on:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["primary_hover"] if self.underMouse() else p["primary"]))
        else:
            painter.setPen(QPen(QColor(p["text"] if self.underMouse() else p["muted"]), 1))
            painter.setBrush(QColor(p["surface"]))
        painter.drawRoundedRect(track, radius, radius)
        knob = track.height() - (8 if not on else 6)
        x = track.left() + 4 + self._pos * (track.width() - 8 - knob)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p["on_primary"] if on else p["muted"]))
        painter.drawEllipse(QRectF(x, track.center().y() - knob / 2, knob, knob))
        if self.hasFocus():
            painter.setPen(QPen(QColor(p["primary"]), 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius + 1, radius + 1)
        painter.end()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


BRAIN_TEXT = {
    "ready": "AI brain ready",
    "busy": "AI brain checking…",
    "starting": "AI brain starting…",
    "installing": "AI brain installing…",
    "stopped": "AI brain stopped",
    "not_installed": "AI brain not installed",
    "error": "AI brain has a problem",
}


class MonitorPill(QFrame):
    """Sidebar block: Monitoring on/off switch and what it is doing right now."""

    toggled = Signal(bool)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.setObjectName("MonitorPill")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel()
        self.dot.setObjectName("StateDot")
        self.dot.setFixedSize(8, 8)
        top.addWidget(self.dot, 0, Qt.AlignVCenter)
        title = label("Monitoring", "CardTitle")
        top.addWidget(title, 1)
        self.switch = Switch(ctx)
        self.switch.setAccessibleName("Monitoring")
        self.switch.setToolTip("Watch the cameras and raise alerts")
        self.switch.toggled.connect(self._user_toggled)
        title.setBuddy(self.switch)
        top.addWidget(self.switch)
        lay.addLayout(top)
        self.state = muted()
        self.brain = muted()
        lay.addWidget(self.state)
        lay.addWidget(self.brain)
        self.set_state(False, 0, 0, "off")

    def mouseReleaseEvent(self, event):
        # The whole block is a big click target; the switch itself handles its own clicks.
        if event.button() == Qt.LeftButton:
            self.switch.toggle()
        super().mouseReleaseEvent(event)

    def _user_toggled(self, on: bool) -> None:
        self._show("busy", "Starting…" if on else "Stopping…", self.brain.text())
        self.toggled.emit(on)

    def set_state(self, running: bool, cameras: int, watching: int, brain_status: str) -> None:
        """brain_status: Brain.status() value, or "off" when the brain mode is Off."""
        self.switch.set_checked_quietly(running)
        if not running:
            self._show("off", "Off. Turn on to get alerts.", "")
            return
        brain = BRAIN_TEXT.get(brain_status, "")
        if cameras == 0:
            self._show("error", "No cameras to watch", brain)
        elif watching == 0:
            self._show("busy", "Starting…", brain)
        elif watching < cameras:
            self._show("busy", f"Watching {watching} of {cameras} cameras", brain)
        else:
            self._show("error" if brain_status == "error" else "on",
                       f"Watching {cameras} camera{'s' if cameras != 1 else ''}", brain)

    def _show(self, state: str, text: str, brain: str) -> None:
        set_prop(self.dot, "state", state)
        self.state.setText(text)
        self.brain.setText(brain)
        self.brain.setVisible(bool(brain))
        self.setToolTip(" · ".join(t for t in (f"Monitoring: {text}", brain) if t))
