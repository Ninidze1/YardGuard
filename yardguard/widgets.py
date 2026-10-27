"""Reusable widgets for the YardGuard UI."""
from __future__ import annotations

import cv2
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from .qt_util import set_prop, to_qimage
from .video import grab_frame_fast, is_no_signal

STATUS_TEXT = {
    "online": "● Online",
    "nosignal": "● No signal",
    "offline": "● Offline",
    "checking": "● Checking…",
}


def button(text: str, kind: str = "secondary", tooltip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind != "secondary":
        b.setProperty("kind", kind)
    b.setCursor(Qt.PointingHandCursor)
    b.setIconSize(QSize(16, 16))
    if tooltip:
        b.setToolTip(tooltip)
    return b


def spin_box(low: int, high: int, value: int) -> QSpinBox:
    box = QSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    box.lineEdit().setTextMargins(6, 0, 0, 0)  # style sheet padding is ignored by spin boxes
    return box


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def muted(text: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("muted", True)
    lbl.setWordWrap(wrap)
    return lbl


def card(title: str = "", subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 18, 20, 20)
    lay.setSpacing(12)
    if title:
        lay.addWidget(label(title, "SectionTitle"))
    if subtitle:
        s = muted(subtitle)
        lay.addWidget(s)
        lay.addSpacing(2)
    return frame, lay


def set_status(lbl: QLabel, status: str, text: str = "") -> None:
    lbl.setText(text or STATUS_TEXT[status])
    set_prop(lbl, "status", status)


def shortcut(parent: QWidget, key, fn) -> QShortcut:
    sc = QShortcut(QKeySequence(key), parent)
    sc.setContext(Qt.WindowShortcut)
    sc.activated.connect(fn)
    return sc


def password_field(ctx, placeholder: str = "Password") -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.Password)
    edit.setPlaceholderText(placeholder)
    toggle = QAction(edit)
    toggle.setToolTip("Show or hide the password")
    ctx.bind_icon(toggle, "eye", "muted")

    def flip():
        hidden = edit.echoMode() == QLineEdit.Password
        edit.setEchoMode(QLineEdit.Normal if hidden else QLineEdit.Password)

    toggle.triggered.connect(flip)
    edit.addAction(toggle, QLineEdit.TrailingPosition)
    return edit


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label(title, "PageTitle")
        self.subtitle = muted(subtitle)
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        row.addLayout(col, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)


class ImageView(QLabel):
    """Shows a video frame scaled to fit, keeping its aspect ratio."""

    clicked = Signal()

    def __init__(self, placeholder: str = "", name: str = "Thumb", ratio: float | None = 9 / 16):
        """ratio: keep this height/width (thumbnails); None = fill the space given (live video)."""
        super().__init__(placeholder)
        self.setObjectName(name)
        self.setAlignment(Qt.AlignCenter)
        self._ratio = ratio
        if ratio:
            policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            policy.setHeightForWidth(True)
        else:
            policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setSizePolicy(policy)
        self._img: QImage | None = None

    def hasHeightForWidth(self) -> bool:
        return self._ratio is not None

    def heightForWidth(self, width: int) -> int:
        return int(width * self._ratio)

    def sizeHint(self) -> QSize:
        return QSize(320, 180) if self._ratio else QSize(640, 360)

    def minimumSizeHint(self) -> QSize:
        return QSize(160, 90)

    def set_image(self, img: QImage | None, text: str = "") -> None:
        self._img = img
        if img is None:
            self.setPixmap(QPixmap())
            self.setText(text)
        else:
            self.setText("")
            self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._img is None or self.width() < 2:
            return
        dpr = self.devicePixelRatioF()
        pm = QPixmap.fromImage(self._img).scaled(
            int(self.width() * dpr), int(self.height() * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self.setPixmap(pm)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class EmptyState(QFrame):
    """Centered message with one clear next action."""

    def __init__(self, ctx):
        super().__init__()
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 48, 40, 48)
        lay.setSpacing(10)
        lay.addStretch(1)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignCenter)
        self.title = label("", "SectionTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.text = muted()
        self.text.setAlignment(Qt.AlignCenter)
        self.action = button("", "primary")
        self.action.setMinimumWidth(180)
        for w in (self.icon, self.title, self.text):
            lay.addWidget(w)
        lay.addSpacing(8)
        lay.addWidget(self.action, 0, Qt.AlignCenter)
        lay.addStretch(2)
        self._ctx = ctx
        self._callback = None
        self.action.clicked.connect(lambda: self._callback and self._callback())

    def configure(self, icon: str, title: str, text: str, action: str, callback) -> None:
        self.icon.setPixmap(self._ctx.icon(icon, "muted", size=40).pixmap(40, 40))
        self.title.setText(title)
        self.text.setText(text)
        self.action.setText(action)
        self._callback = callback


def fetch_thumb(url: str, width: int = 480):
    """Grab one frame; returns (QImage or None, status)."""
    frame = grab_frame_fast(url)
    if frame is None:
        return None, "offline"
    status = "nosignal" if is_no_signal(frame) else "online"
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (width, max(1, int(width * h / w))), interpolation=cv2.INTER_AREA)
    return to_qimage(small), status
