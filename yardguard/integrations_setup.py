"""Setup dialogs of the Integrations page: one step-by-step dialog per kind of destination
(Telegram, ntfy, email, Discord, webhook, Home Assistant) and the Phone access dialog, plus the
small widgets the Integrations page, its "What to send" editor and these dialogs share.

Nothing is saved until Finish: a dialog works on a copy of the destination and keeps typed
secrets in memory only. Credentials are checked with Channel.check() on the thread pool, and the
test message goes through a temporary channel instance built from the copy. Saved secrets are
never decrypted into a text field ("Saved - type to replace").
"""
from __future__ import annotations

import copy
import importlib
import importlib.util
import logging
import re
import secrets as pysecrets
import subprocess
import sys
import time
import uuid
from typing import Callable

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .config import ROOT, encrypt
from .qt_util import run_async, set_prop
from .settings_ui import LABEL_W, _ModeOption, form_row
from .theme import ICONS
from .widgets import button, label, muted, password_field, set_status, spin_box
from .wizard import form_layout

log = logging.getLogger("yardguard")

# Icon shapes this page adds to the app's set (Lucide, ISC licence); theme.svg_icon reads ICONS
ICON_PATHS = {
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/>'
            '<path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    "send": '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024'
            '.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
    "bell": '<path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673'
            'C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/>',
    "bell-off": '<path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 '
                '12.499 6 8a6 6 0 0 1 .258-1.742"/><path d="m2 2 20 20"/><path d="M8.668 3.01A6 6 0 0 1 18 8c0 '
                '2.687.77 4.653 1.707 6.05"/>',
    "mail": '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "house": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999'
             'a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "webhook": '<path d="M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2"/>'
               '<path d="m6 17 3.13-5.78c.53-.97.1-2.18-.5-3.1a4 4 0 1 1 6.89-4.06"/>'
               '<path d="m12 6 3.13 5.73C15.66 12.7 16.9 13 18 13a4 4 0 0 1 0 8"/>',
    "smartphone": '<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
    "qr": '<rect width="5" height="5" x="3" y="3" rx="1"/><rect width="5" height="5" x="16" y="3" rx="1"/>'
          '<rect width="5" height="5" x="3" y="16" rx="1"/><path d="M21 16h-3a2 2 0 0 0-2 2v3"/>'
          '<path d="M21 21v.01"/><path d="M12 7v3a2 2 0 0 1-2 2H7"/><path d="M3 12h.01"/><path d="M12 3h.01"/>'
          '<path d="M12 16v.01"/><path d="M16 12h1"/><path d="M21 12v.01"/><path d="M12 21v-1"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>'
            '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/>'
                '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
}
for _name, _shape in ICON_PATHS.items():
    ICONS.setdefault(_name, _shape)

# Used when a channel module cannot be imported (the dialog still opens and explains the problem)
EMAIL_PRESETS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "security": "starttls",
              "help": "Gmail needs an app password, not your normal password. Turn on 2-Step Verification first, "
                      "then create an app password.",
              "help_url": "https://myaccount.google.com/apppasswords"},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "security": "starttls",
                "help": "Outlook.com may refuse passwords from apps; if it does, use Gmail or your provider's SMTP."},
    "yahoo": {"host": "smtp.mail.yahoo.com", "port": 465, "security": "ssl",
              "help": "Yahoo needs an app password (Account security > Generate app password).",
              "help_url": "https://login.yahoo.com/account/security"},
    "other": {"host": "", "port": 587, "security": "starttls"},
}
NTFY_APPS = (("App Store", "https://apps.apple.com/app/ntfy/id1625396347"),
             ("Google Play", "https://play.google.com/store/apps/details?id=io.heckel.ntfy"))
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
DISCORD_RE = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
CHECK_TIMEOUT_S = 40       # the UI gives up waiting for a check (the channel has its own, shorter timeouts)
TEST_TIMEOUT_S = 60
LISTEN_S = 180             # Telegram "Connect" listens this long for /start

HELP = {
    "telegram": ["Open the chat with your bot in Telegram: the message may be there with notifications muted.",
                 "Check that the chat you ticked is the one you are looking at (private chat or the group).",
                 "If you blocked the bot, open it and press Restart or Start."],
    "ntfy": ["Open the ntfy app and check that you subscribed to exactly this topic name.",
             "Allow notifications for the ntfy app in your phone's settings.",
             "If you use your own server, the app must use the same server address."],
    "email": ["Look in the spam or junk folder and mark the message as not spam.",
              "Check the address under Send to.",
              "Some providers hold the first message from a new app for a few minutes."],
    "discord": ["Open the channel the webhook belongs to: the message appears there, not as a direct message.",
                "Check that notifications for that server or channel are not muted."],
    "webhook": ["Look at your server's log: it should show a POST from YardGuard.",
                "Check that the address is reachable from this PC (no firewall in between)."],
    "mqtt": ["In Home Assistant open Settings > Devices & services > MQTT and look for a device called YardGuard.",
             "Check that the MQTT integration in Home Assistant uses the same broker."],
}


# --- Shared helpers -----------------------------------------------------------------------------

def integ(name: str):
    """yardguard.integrations.<name>, or None when it cannot be imported (missing optional package)."""
    try:
        return importlib.import_module(f"yardguard.integrations.{name}")
    except Exception as exc:  # an optional part is missing or broken: the page degrades, it never crashes
        log.warning("Integrations part %s is not available (%s)", name, type(exc).__name__)
        return None


def webremote_part(name: str):
    try:
        return importlib.import_module(f"yardguard.webremote.{name}")
    except Exception as exc:
        log.warning("Phone access part %s is not available (%s)", name, type(exc).__name__)
        return None


def catalog() -> dict:
    base = integ("base")
    return dict(getattr(base, "CATALOG", {}) or {})


def groups() -> tuple:
    base = integ("base")
    return tuple(getattr(base, "GROUPS", ()) or ())


def channel_class(kind: str):
    base = integ("base")
    try:
        return base.load_channel_class(kind) if base else None
    except Exception:
        log.exception("Loading the %s channel failed", kind)
        return None


DEFAULT_CAPS = {"pictures": True, "clip": False, "edit": False, "buttons": False, "two_way": False,
                "max_upload": 10_000_000, "updates": "drop"}


def caps(kind: str) -> dict:
    cls = channel_class(kind)
    return {**DEFAULT_CAPS, **(getattr(cls, "CAPS", None) or {})}


def secret_keys(kind: str) -> tuple:
    return tuple(getattr(channel_class(kind), "SECRET_KEYS", ()) or ())


def mqtt_available() -> bool:
    mod = integ("mqtt_ha")
    if mod is not None and hasattr(mod, "AVAILABLE"):
        return bool(mod.AVAILABLE)
    return importlib.util.find_spec("paho") is not None


def clock(ts: float) -> str:
    """'21:04' today, 'Yesterday 21:04', 'Mon 21:04' this week, else '3 Oct 21:04'."""
    from datetime import date, datetime, timedelta
    dt, today = datetime.fromtimestamp(ts), date.today()
    if dt.date() == today:
        return f"{dt:%H:%M}"
    if dt.date() == today - timedelta(days=1):
        return f"Yesterday {dt:%H:%M}"
    if 0 < (today - dt.date()).days < 7:
        return f"{dt:%a %H:%M}"
    return f"{dt.day} {dt:%b %H:%M}"


_LONG_ID = re.compile(r"-?\d{6,}")
_TOKENISH = re.compile(r"(bot)?\d{5,12}:[A-Za-z0-9_-]{20,}")
_URL_SECRET = re.compile(r"(https?://)([^/\s:@]+:[^/\s@]+@)?([^/\s?#]+)[^\s]*")


def _url_shown(m: re.Match) -> str:
    """https://user:pw@host/path?q -> https://host/*** (paths and queries may hold secrets)."""
    scheme, host = m.group(1), m.group(3)
    rest = m.group(0)[len(scheme) + len(m.group(2) or "") + len(host):]
    return f"{scheme}{host}/***" if rest.strip("/") else f"{scheme}{host}"


def safe_text(text: str) -> str:
    """Text for the screen: bot tokens and URL paths hidden, long numbers (chat ids) shortened to
    their last 3 digits. Core already scrubs; this is a second guard for anything shown."""
    text = _TOKENISH.sub("***", str(text or ""))
    text = _URL_SECRET.sub(_url_shown, text)
    return _LONG_ID.sub(lambda m: "…" + m.group(0)[-3:], text)


def dest_target(conf) -> str:
    """Who or where a destination sends to, in a few words: "Alex", "me@example.com", a topic."""
    p = getattr(conf, "params", None) or {}
    kind = getattr(conf, "kind", "")
    try:
        if kind == "telegram":
            names = [str(c.get("title") or "") for c in p.get("chats") or []
                     if isinstance(c, dict) and c.get("enabled", True)]
            return ", ".join(n for n in names if n)
        if kind == "email":
            to = p.get("to") or [p.get("sender") or ""]
            to = [str(t) for t in (to if isinstance(to, list) else [to]) if t]
            return (to[0] + (f" +{len(to) - 1}" if len(to) > 1 else "")) if to else ""
        if kind == "ntfy":
            topic = str(p.get("topic") or "")
            return topic if len(topic) <= 16 else topic[:13] + "…"
        if kind == "discord":
            return str(p.get("webhook_name") or "")
        if kind == "mqtt":
            return str(p.get("host") or "")
        if kind == "webhook" and conf.has_secret("url"):
            from urllib.parse import urlsplit
            return urlsplit(conf.secret("url")).hostname or ""
    except Exception:
        return ""
    return ""


def dest_labels(confs) -> dict[str, str]:
    """{destination id: name for lists}. Destinations that share a name get who they send to added
    ("Telegram · Alex", "Telegram · Sam"), so pickers and the activity list tell them apart."""
    cat = catalog()
    names = {c.id: (c.name or cat.get(c.kind, {}).get("title", c.kind)) for c in confs}
    count: dict[str, int] = {}
    for n in names.values():
        count[n] = count.get(n, 0) + 1
    out = {}
    for c in confs:
        n = names[c.id]
        target = safe_text(dest_target(c)) if count[n] > 1 else ""
        out[c.id] = f"{n} · {target}" if target else n
    return out


def set_conf_name(conf, name: str) -> None:
    """Rename a destination: through the property when it has a setter, else in the wrapped dict."""
    try:
        conf.name = name
        return
    except AttributeError:
        pass
    for attr in ("d", "data", "raw", "_d"):
        d = getattr(conf, attr, None)
        if isinstance(d, dict):
            d["name"] = name
            return
    log.warning("Could not rename the destination: IntegrationConf has no name setter")


def confirm(parent: QWidget, title: str, text: str, yes: str, no: str = "Cancel", danger: bool = False) -> bool:
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


def ask_text(parent: QWidget, title: str, prompt: str, text: str = "") -> str | None:
    """One-line text question; None when cancelled. Tests replace this function."""
    value, ok = QInputDialog.getText(parent, title, prompt, QLineEdit.Normal, text)
    return value if ok else None


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


def link_button(text: str, tooltip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("LinkButton")
    b.setCursor(Qt.PointingHandCursor)
    if tooltip:
        b.setToolTip(tooltip)
    return b


def chip(text: str = "", tone: str = "") -> QLabel:
    c = label(text, "Chip")
    c.setAlignment(Qt.AlignCenter)
    set_chip(c, text, tone)
    return c


def set_chip(c: QLabel, text: str, tone: str = "") -> None:
    """tone: "" plain, accent, success, warning, danger."""
    c.setText(text)
    c.setVisible(bool(text))
    set_prop(c, "accent", tone == "accent")
    set_prop(c, "tone", tone if tone in ("success", "warning", "danger") else "")


class IconTile(QLabel):
    """Rounded square with an icon. Has setIcon() so ctx.bind_icon re-colours it with the theme."""

    def __init__(self, size: int = 36, icon_size: int = 18):
        super().__init__()
        self.setObjectName("IconTile")
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self._icon_size = icon_size

    def setIcon(self, icon) -> None:
        self.setPixmap(icon.pixmap(self._icon_size, self._icon_size))


def icon_tile(ctx, name: str, size: int = 36, role: str = "primary") -> IconTile:
    tile = IconTile(size, max(14, int(size * 0.5)))
    ctx.bind_icon(tile, name, role)
    return tile


def heading(text: str) -> QLabel:
    return label(text, "CardTitle", wrap=True)


def numbered(items: list) -> QWidget:
    """1. 2. 3. instructions; an item may be (text, widget) to put a button under the text."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    for i, item in enumerate(items, 1):
        text, extra = (item, None) if isinstance(item, str) else item
        row = QHBoxLayout()
        row.setSpacing(10)
        num = label(str(i), "StepNumber")
        num.setFixedSize(22, 22)
        num.setAlignment(Qt.AlignCenter)
        row.addWidget(num, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        t = QLabel(text)
        t.setWordWrap(True)
        t.setTextFormat(Qt.RichText)
        col.addWidget(t)
        if extra is not None:
            col.addWidget(extra, 0, Qt.AlignLeft)
        row.addLayout(col, 1)
        lay.addLayout(row)
    return w


class ElidedLabel(QLabel):
    """One line that never wraps: long text ends in an ellipsis and the full text is the tooltip."""

    def __init__(self, text: str = "", name: str = "", muted_text: bool = False):
        super().__init__()
        if name:
            self.setObjectName(name)
        if muted_text:
            self.setProperty("muted", True)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def _elide(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(10, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self):
        return QSize(10, self.fontMetrics().height())

    def sizeHint(self):
        m = self.contentsMargins()
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + m.left() + m.right() + 2,
                     self.fontMetrics().height() + m.top() + m.bottom())


def base_address(url: str) -> str:
    """ "http://192.168.1.5:8765/#pair=AB12CD34" -> "http://192.168.1.5:8765" (short enough to type)."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


class CopyField(QWidget):
    """Read-only text with a Copy button (topics, links, codes)."""

    def __init__(self, ctx, text: str = "", accessible: str = "", mono: bool = False):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.edit = QLineEdit(text)
        self.edit.setReadOnly(True)
        self.copy_value = ""          # copied instead of the shown text when set (a link with a code in it)
        if mono:
            self.edit.setObjectName("MonoField")
        if accessible:
            self.edit.setAccessibleName(accessible)
        self.copy_btn = button("Copy", tooltip="Copy to the clipboard")
        ctx.bind_icon(self.copy_btn, "copy")
        self.copy_btn.setAccessibleName(f"Copy {accessible}".strip())
        self.copy_btn.clicked.connect(self.copy)
        row.addWidget(self.edit, 1)
        row.addWidget(self.copy_btn)

    def set_text(self, text: str) -> None:
        self.edit.setText(text)
        self.edit.setCursorPosition(0)

    def text(self) -> str:
        return self.edit.text()

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.copy_value or self.edit.text())
        self.copy_btn.setText(" Copied")
        QTimer.singleShot(1500, lambda: self._reset())

    def _reset(self) -> None:
        try:
            self.copy_btn.setText(" Copy")
        except RuntimeError:
            pass


class QrView(QLabel):
    """A QR code in a white box (black on white in both themes, for phone cameras). Hidden, and
    set_text() returns False, when the QR part is not installed: callers then show the link only."""

    def __init__(self, size: int = 160):
        super().__init__()
        self.setObjectName("QrBox")
        self.setAlignment(Qt.AlignCenter)
        self._size = size
        self.setFixedSize(size + 18, size + 18)
        self.text_value = ""

    def set_text(self, text: str) -> bool:
        self.text_value = text
        qr = integ("qr")
        img = None
        if qr is not None and text:
            try:
                img = qr.to_qimage(text, scale=8)
            except Exception:
                log.exception("QR code failed")
                img = None
        if img is None or img.isNull():
            self.setPixmap(QPixmap())
            self.setVisible(False)
            return False
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap.fromImage(img).scaled(int(self._size * dpr), int(self._size * dpr), Qt.KeepAspectRatio,
                                           Qt.FastTransformation)
        pm.setDevicePixelRatio(dpr)
        self.setPixmap(pm)
        self.setVisible(True)
        return True


class Disclosure(QWidget):
    """'Advanced' toggle that shows or hides a body."""

    def __init__(self, title: str, body: QWidget, opened: bool = False):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.title = title
        self.toggle = link_button("")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self._toggled)
        self.body = body
        lay.addWidget(self.toggle, 0, Qt.AlignLeft)
        lay.addWidget(body)
        self.toggle.setChecked(opened)
        self._toggled(opened)

    def _toggled(self, on: bool) -> None:
        self.toggle.setText(f"Hide {self.title.lower()}" if on else f"{self.title}…")
        self.body.setVisible(on)


def field_error() -> QLabel:
    lbl = label("", "ErrText", wrap=True)
    lbl.setVisible(False)
    return lbl


def with_error(field) -> tuple[QWidget, QLabel]:
    """A form field with its error line underneath (one form row, so hidden rows leave no gap)."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    if isinstance(field, QWidget):
        lay.addWidget(field)
    else:
        lay.addLayout(field)
    err = field_error()
    lay.addWidget(err)
    return box, err


def show_error(lbl: QLabel, text: str) -> None:
    lbl.setText(text)
    lbl.setVisible(bool(text))


class WebSignals(QObject):
    """Phone access changes (paired phones, server start/stop) carried from server threads into the
    GUI thread. One per WebRemote, shared by the page and the dialogs."""

    changed = Signal()


def web_signals(ctx) -> WebSignals | None:
    """WebSignals for ctx.web: fed by WebRemote.signals (devices_changed / status_changed) when it has
    them, else through its `changed` hook (called from server threads; the signal queues it)."""
    web = getattr(ctx, "web", None)
    if web is None:
        return None
    sig = getattr(ctx, "_integrations_web_signals", None)
    if sig is None:
        sig = WebSignals(ctx if isinstance(ctx, QObject) else None)
        try:
            ctx._integrations_web_signals = sig
        except AttributeError:
            pass
        own = getattr(web, "signals", None)
        hooked = False
        for name in ("devices_changed", "status_changed"):
            s = getattr(own, name, None)
            if s is not None and hasattr(s, "connect"):
                s.connect(sig.changed.emit)
                hooked = True
        if not hooked and getattr(web, "changed", None) is None:
            try:
                web.changed = sig.changed.emit
            except Exception:
                log.debug("WebRemote takes no change hook")
    return sig


def apply_web(ctx) -> None:
    """After Settings.remote changed: start, stop or rebind phone access as needed."""
    web = getattr(ctx, "web", None)
    if web is None:
        return
    try:
        if hasattr(web, "apply_settings"):
            web.apply_settings()
        elif remote_settings(ctx).get("enabled"):
            web.restart() if getattr(web, "running", False) else web.start()
        else:
            web.stop()
    except Exception:
        log.exception("Applying phone access settings failed")


# --- Step dialog shell ----------------------------------------------------------------------------

class Step(QWidget):
    """One page of a StepDialog. Subclasses fill self.body and override the hooks."""

    title = ""
    subtitle = ""
    strip = ""                    # short name in the step strip
    changed = Signal()

    def __init__(self, dlg: "StepDialog"):
        super().__init__()
        self.dlg = dlg
        self.ctx = dlg.ctx
        self.focus_widget: QWidget | None = None   # gets the keyboard focus when the step opens
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("Transparent")
        self.body = QVBoxLayout(host)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(12)
        scroll.setWidget(host)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)
        self.status = label("", "StepStatus", wrap=True)
        self.status.setVisible(False)
        self.errors: dict[str, QLabel] = {}

    def finish_body(self) -> None:
        self.body.addWidget(self.status)
        self.body.addStretch(1)

    def skip(self) -> bool:
        return False

    def enter(self) -> None:
        pass

    def leave(self) -> None:
        pass

    def complete(self) -> bool:
        return True

    def validate(self, done: Callable[[bool], None]) -> None:
        """Check the input before Next; done(True) moves on. May finish later (thread pool)."""
        done(True)

    def set_state(self, state: str, text: str) -> None:
        """state: online | offline | checking | nosignal | "" (hide)."""
        if not text:
            self.status.setVisible(False)
            return
        set_status(self.status, state, text)
        self.status.setVisible(True)

    def clear_errors(self) -> None:
        for lbl in self.errors.values():
            show_error(lbl, "")
        self.set_state("", "")

    def show_problem(self, message: str, field: str = "") -> None:
        if field and field in self.errors:
            show_error(self.errors[field], message)
            self.set_state("", "")
        else:
            self.set_state("offline", message)


class StepDialog(QDialog):
    """Dialog with a step strip, a body per step and Back / Next / Cancel (Enter = Next,
    Esc = Cancel, Alt+B = Back)."""

    def __init__(self, ctx, title: str, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(title)
        self.resize(640, 560)
        self.setMinimumSize(560, 480)
        self.dirty = False
        self._busy = 0          # generation of the running check; 0 = idle
        self._gen = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(28, 20, 28, 8)
        tl.setSpacing(8)
        self.strip = QWidget()
        self.strip.setObjectName("StepStrip")
        self.strip_row = QHBoxLayout(self.strip)
        self.strip_row.setContentsMargins(0, 0, 0, 0)
        self.strip_row.setSpacing(4)
        tl.addWidget(self.strip)
        tl.addSpacing(4)
        self.title = label("", "PageTitle", wrap=True)
        self.subtitle = muted()
        tl.addWidget(self.title)
        tl.addWidget(self.subtitle)
        lay.addWidget(top)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(28, 8, 20, 12)
        self.stack = QStackedWidget()
        bl.addWidget(self.stack)
        lay.addWidget(body, 1)
        footer = QWidget()
        footer.setObjectName("WizardFooter")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(28, 12, 28, 14)
        self.cancel_btn = button("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.back_btn = button("&Back")
        self.back_btn.setToolTip("Back (Alt+B)")
        self.back_btn.clicked.connect(self.back)
        self.next_btn = button("Next", "primary")
        self.next_btn.setMinimumWidth(110)
        self.next_btn.setDefault(True)
        self.next_btn.setAutoDefault(True)
        self.next_btn.clicked.connect(self.next)
        fl.addWidget(self.cancel_btn)
        fl.addStretch(1)
        fl.addWidget(self.back_btn)
        fl.addWidget(self.next_btn)
        lay.addWidget(footer)
        self.steps: list[Step] = []
        self.index = -1
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._check_timed_out)

    # --- setup ---
    def set_steps(self, steps: list[Step], start: int = 0) -> None:
        self.steps = steps
        for s in steps:
            self.stack.addWidget(s)
            s.changed.connect(self._update)
        start = max(0, min(start, len(steps) - 1))
        while start < len(steps) - 1 and steps[start].skip():
            start += 1
        self._go(start)

    def visible_steps(self) -> list[Step]:
        return [s for s in self.steps if not s.skip()]

    def _render_strip(self) -> None:
        while self.strip_row.count():
            item = self.strip_row.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        shown = self.visible_steps()
        cur = self.steps[self.index]
        cur_i = shown.index(cur) if cur in shown else 0
        for i, s in enumerate(shown):
            if i:
                sep = muted("·", wrap=False)
                self.strip_row.addWidget(sep)
            state = "done" if i < cur_i else "current" if i == cur_i else "todo"
            lbl = label(f"{'✓' if state == 'done' else i + 1}  {s.strip or s.title}", "Step")
            set_prop(lbl, "state", state)
            self.strip_row.addWidget(lbl)
        self.strip_row.addStretch(1)

    def _go(self, index: int) -> None:
        if 0 <= self.index < len(self.steps) and self.index != index:
            self.steps[self.index].leave()
        self.index = index
        step = self.steps[index]
        self.stack.setCurrentWidget(step)
        self.title.setText(step.title)
        self.subtitle.setText(step.subtitle)
        self.subtitle.setVisible(bool(step.subtitle))
        self._render_strip()
        step.enter()
        self._update()
        QTimer.singleShot(0, self._focus_step)

    def _focus_step(self) -> None:
        """Focus the step's first input (so a paste works at once), else Next."""
        if not (0 <= self.index < len(self.steps)):
            return
        w = self.steps[self.index].focus_widget
        try:
            if w is not None and w.isVisible() and w.isEnabled():
                w.setFocus(Qt.OtherFocusReason)
            elif self.next_btn.isEnabled():
                self.next_btn.setFocus(Qt.OtherFocusReason)
        except RuntimeError:     # closed meanwhile
            pass

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._focus_step)

    def is_last(self) -> bool:
        return all(s.skip() for s in self.steps[self.index + 1:])

    def _update(self) -> None:
        if self.index < 0:
            return
        step = self.steps[self.index]
        self.back_btn.setVisible(any(not s.skip() for s in self.steps[:self.index]))
        self.next_btn.setText(self.finish_text() if self.is_last() else "Next")
        self.next_btn.setEnabled(not self._busy and step.complete())
        self.back_btn.setEnabled(not self._busy)

    def finish_text(self) -> str:
        return "Finish"

    def mark_dirty(self, *_) -> None:
        self.dirty = True

    # --- navigation ---
    def next(self) -> None:
        if self._busy or self.index < 0:
            return
        step = self.steps[self.index]
        if not step.complete():
            return
        step.clear_errors()
        self._gen += 1
        gen = self._busy = self._gen
        self._watchdog.start(CHECK_TIMEOUT_S * 1000)
        self._update()

        def done(ok: bool, gen=gen) -> None:
            if gen != self._busy:
                return      # cancelled, timed out or the user went back meanwhile
            self._busy = 0
            self._watchdog.stop()
            self._update()
            if ok:
                self._advance()

        try:
            step.validate(done)
        except Exception as exc:  # a broken optional part must not leave the dialog stuck
            log.exception("Setup step check failed")
            step.show_problem(f"Something went wrong: {type(exc).__name__}.")
            done(False)

    def _check_timed_out(self) -> None:
        if not self._busy:
            return
        self._busy = 0
        self.steps[self.index].show_problem(
            f"No answer after {CHECK_TIMEOUT_S} seconds. Check the internet connection and try again.")
        self._update()

    def _advance(self) -> None:
        if self.is_last():
            self.finish()
            return
        i = self.index + 1
        while i < len(self.steps) - 1 and self.steps[i].skip():
            i += 1
        self._go(i)

    def back(self) -> None:
        self._busy = 0
        self._watchdog.stop()
        i = self.index - 1
        while i > 0 and self.steps[i].skip():
            i -= 1
        if i >= 0 and not self.steps[i].skip():
            self._go(i)

    def finish(self) -> None:
        self.accept()

    def checking(self, step: Step, text: str = "Checking…") -> None:
        step.set_state("checking", text)

    # --- closing ---
    def reject(self) -> None:
        if self.dirty and not confirm(self, self.windowTitle(), "Discard this setup?", "Discard", "Keep editing"):
            return
        super().reject()

    def done(self, result: int) -> None:
        self._busy = 0
        self._watchdog.stop()
        for s in self.steps:
            try:
                s.leave()
            except Exception:
                log.exception("Leaving a setup step failed")
        self.cleanup()
        super().done(result)

    def cleanup(self) -> None:
        pass


# --- Destination setup ------------------------------------------------------------------------------

class SetupDialog(StepDialog):
    """Connect (or edit) one destination. Works on a copy; hub.add_destination / update_destination
    only on Finish. finished_dest(id) tells the page which destination to select next."""

    finished_dest = Signal(str)
    KIND = ""
    NOUN = ""

    def __init__(self, ctx, hub, conf=None, start_step: int = 0, parent=None):
        self.hub = hub
        self.conf = conf                              # IntegrationConf when editing, else None
        self.editing = conf is not None
        title = catalog().get(self.KIND, {}).get("title", self.KIND.title())
        super().__init__(ctx, f"Connect {self.NOUN or title}" if not self.editing else f"Edit {conf.name}", parent)
        base = integ("base")
        if self.editing:
            self.params = copy.deepcopy(conf.params)
            self.routes = copy.deepcopy(conf.routes)
            self.name = conf.name
        else:
            d = base.new_integration(self.KIND) if base else {"params": {}, "routes": {}, "name": title}
            self.params = copy.deepcopy(d.get("params") or {})
            self.routes = copy.deepcopy(d.get("routes") or {})
            self.name = d.get("name") or title
            self._new = d
        self.typed: dict[str, str] = {}          # secrets typed in this dialog (memory only)
        self.result_id = ""
        self.tested = False

    # --- secrets ---
    def has_saved(self, key: str) -> bool:
        return bool(self.editing and self.conf.has_secret(key))

    def saved_unreadable(self, key: str) -> bool:
        return bool(self.editing and self.conf.secret_unreadable(key))

    def secret(self, key: str) -> str:
        """Plain value for checks and the test channel: typed now, else the saved one."""
        if key in self.typed:
            return self.typed[key]
        return self.conf.secret(key) if self.editing else ""

    def set_typed(self, key: str, value: str) -> None:
        value = value.strip()
        if value:
            self.typed[key] = value
        else:
            self.typed.pop(key, None)

    def secrets_dict(self) -> dict[str, str]:
        keys = set(secret_keys(self.KIND)) | set(self.typed)
        return {k: v for k in keys if (v := self.secret(k))}

    # --- checks and tests ---
    def run_check(self, step: Step, done: Callable[[bool], None], on_ok: Callable[[object], None] | None = None,
                  busy_text: str = "Checking…") -> None:
        cls = channel_class(self.KIND)
        if cls is None:
            step.show_problem("This part of YardGuard is missing. Reinstall or update YardGuard.")
            done(False)
            return
        params, secrets = copy.deepcopy(self.params), self.secrets_dict()
        self.checking(step, busy_text)

        def finished(res) -> None:
            if isinstance(res, Exception) or res is None:
                step.show_problem("The check did not work. Try again in a moment.")
                done(False)
                return
            if not getattr(res, "ok", False):
                step.show_problem(safe_text(getattr(res, "message", "") or "Not accepted."), getattr(res, "field", ""))
                done(False)
                return
            if on_ok:
                on_ok(res)
            step.set_state("online", safe_text(getattr(res, "message", "") or "Looks good."))
            done(True)

        run_async(cls.check, params, secrets, on_done=finished)

    def temp_conf(self):
        """IntegrationConf for a throw-away channel instance with the dialog's values."""
        base = integ("base")
        d = {"id": f"tmp-{uuid.uuid4().hex[:8]}", "kind": self.KIND, "name": self.name, "enabled": True,
             "created": time.time(), "secrets": {k: encrypt(v) for k, v in self.secrets_dict().items()},
             "params": copy.deepcopy(self.params), "routes": copy.deepcopy(self.routes)}
        if hasattr(base, "normalize_integration"):
            base.normalize_integration(d)
        return base.IntegrationConf(d)

    def run_test(self, on_done: Callable[[object], None]) -> None:
        cls = channel_class(self.KIND)
        if cls is None:
            on_done(Exception("missing"))
            return
        try:
            channel = cls(self.temp_conf(), self.hub)
        except Exception as exc:
            log.exception("Building a test channel failed")
            on_done(exc)
            return

        def work():
            try:
                return channel.test()
            finally:
                try:
                    channel.stop()
                except Exception:
                    pass

        run_async(work, on_done=on_done)

    # --- finish ---
    def apply_extra(self) -> None:
        """Last changes to self.params from the final step (subclasses)."""

    def finish(self) -> None:
        self.apply_extra()
        base = integ("base")
        if self.editing:
            self.conf.params.clear()
            self.conf.params.update(self.params)
            for k, v in self.typed.items():
                self.conf.set_secret(k, v)
            self.hub.update_destination(self.conf.id)
            self.result_id = self.conf.id
        else:
            d = self._new
            d["params"] = self.params
            d["routes"] = self.routes
            d["name"] = self.name
            d["enabled"] = True
            conf = base.IntegrationConf(d)
            for k, v in self.typed.items():
                conf.set_secret(k, v)
            added = self.hub.add_destination(d)
            self.result_id = getattr(added, "id", "") or d.get("id", "")
        self.typed.clear()
        self.dirty = False
        self.finished_dest.emit(self.result_id)
        self.accept()

    def cleanup(self) -> None:
        self.typed.clear()


class TestStep(Step):
    """Last step of every destination: send a sample, then 'Did it arrive?'."""

    title = "Send a test"
    strip = "Test"

    def __init__(self, dlg: SetupDialog, intro: str = ""):
        super().__init__(dlg)
        self.subtitle = intro or "YardGuard sends a sample alert with a picture, so you can see how it looks."
        self.extra = QVBoxLayout()
        self.extra.setSpacing(10)
        self.body.addLayout(self.extra)
        row = QHBoxLayout()
        self.send_btn = button("Send a test", "primary")
        dlg.ctx.bind_icon(self.send_btn, "send", "on_primary")
        self.send_btn.setAccessibleName(f"Send a test message to {dlg.name}")
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.send_btn)
        row.addStretch(1)
        self.body.addLayout(row)
        self.result = label("", "StepStatus", wrap=True)
        self.result.setVisible(False)
        self.body.addWidget(self.result)
        self.ask = QWidget()
        al = QHBoxLayout(self.ask)
        al.setContentsMargins(0, 4, 0, 0)
        al.setSpacing(8)
        al.addWidget(label("Did it arrive?", "CardTitle"))
        self.yes_btn = button("Yes, finish", "primary")
        self.yes_btn.clicked.connect(dlg.next)
        self.no_btn = button("No, help me")
        self.no_btn.clicked.connect(self._help)
        al.addSpacing(8)
        al.addWidget(self.yes_btn)
        al.addWidget(self.no_btn)
        al.addStretch(1)
        self.ask.setVisible(False)
        self.body.addWidget(self.ask)
        self.help = muted()
        self.help.setTextFormat(Qt.RichText)
        self.help.setVisible(False)
        self.body.addWidget(self.help)
        self._gen = 0
        self.finish_body()

    def send(self) -> None:
        self._gen += 1
        gen = self._gen
        self.send_btn.setEnabled(False)
        set_status(self.result, "checking", "Sending a test…")
        self.result.setVisible(True)
        self.ask.setVisible(False)
        QTimer.singleShot(TEST_TIMEOUT_S * 1000, lambda: self._timeout(gen))
        self.dlg.run_test(lambda res: self._done(res, gen))

    def _timeout(self, gen: int) -> None:
        if gen == self._gen and not self.send_btn.isEnabled():
            self._gen += 1
            self.send_btn.setEnabled(True)
            set_status(self.result, "offline", f"No answer after {TEST_TIMEOUT_S} seconds. Try again.")

    def _done(self, res, gen: int) -> None:
        if gen != self._gen:
            return
        self.send_btn.setEnabled(True)
        self.send_btn.setText(" Send again")
        self.dlg.tested = True
        if isinstance(res, Exception) or res is None:
            set_status(self.result, "offline", "The test could not be sent. Try again in a moment.")
            return
        if getattr(res, "ok", False):
            note = getattr(res, "note", "")
            set_status(self.result, "online", "Test sent." + (f" {safe_text(note)}" if note else ""))
            self.ask.setVisible(True)
            self.yes_btn.setFocus()
        else:
            set_status(self.result, "offline", "Not sent: " + safe_text(getattr(res, "error", "") or "unknown problem"))
            self.ask.setVisible(False)

    def _help(self) -> None:
        tips = HELP.get(self.dlg.KIND, [])
        self.help.setText("<ul style='margin-left:-20px'>" + "".join(f"<li>{t}</li>" for t in tips) + "</ul>")
        self.help.setVisible(True)

    def leave(self) -> None:
        self._gen += 1


# --- Telegram ---------------------------------------------------------------------------------------

def clean_token(text: str) -> str:
    t = "".join(text.split())
    return t[3:] if t.lower().startswith("bot") and ":" in t[3:] else t


class TgTokenStep(Step):
    title = "Create your bot"
    strip = "Create bot"
    subtitle = "A Telegram bot is your private messenger for YardGuard. It is free and takes about two minutes."

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        open_bf = button("Open BotFather")
        dlg.ctx.bind_icon(open_bf, "external")
        open_bf.clicked.connect(lambda: open_url("https://t.me/BotFather"))
        self.body.addWidget(numbered([
            ("Open Telegram and search for <b>@BotFather</b> (the official one, with a blue tick).", open_bf),
            "Send <b>/newbot</b> and choose any name, for example <i>Our yard</i>.",
            "BotFather answers with a token like <b>123456789:AA…</b>. Copy it and paste it here.",
        ]))
        self.body.addSpacing(4)
        form = form_layout()
        self.token = password_field(dlg.ctx, "Paste the bot token")
        self.token.setAccessibleName("Bot token")
        if dlg.has_saved("token"):
            self.token.setPlaceholderText("Saved - type to replace")
        self.token.textEdited.connect(dlg.mark_dirty)
        self.token.textChanged.connect(lambda *_: self.changed.emit())
        self.focus_widget = self.token
        box, self.errors["token"] = with_error(self.token)
        form_row(form, "Bot token", box)
        self.body.addLayout(form)
        if dlg.saved_unreadable("token"):
            show_error(self.errors["token"], "The saved token cannot be read on this Windows account. Enter it again.")
        self.finish_body()

    def complete(self) -> bool:
        return bool(self.token.text().strip()) or (self.dlg.has_saved("token") and not self.dlg.saved_unreadable("token"))

    def validate(self, done) -> None:
        typed = clean_token(self.token.text())
        if typed:
            if not TOKEN_RE.match(typed):
                self.show_problem("This does not look like a bot token. It looks like 123456789:AA… "
                                  "(numbers, a colon, then about 35 letters).", "token")
                done(False)
                return
            self.dlg.set_typed("token", typed)

        def ok(res) -> None:
            data = getattr(res, "data", {}) or {}
            for k in ("bot_username", "bot_name"):
                if data.get(k):
                    self.dlg.params[k] = data[k]
            if not self.dlg.editing and data.get("bot_username"):
                self.dlg.name = "Telegram"

        self.dlg.run_check(self, done, ok, "Checking the token with Telegram…")


class ChatRow(QFrame):
    """A found chat: tick box, title, what kind of chat, how it was found."""

    def __init__(self, chat: dict, ticked: bool, detail: str):
        super().__init__()
        self.setObjectName("OptionCard")
        self.chat = chat
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        kind = "private chat" if chat.get("type") == "private" else "group"
        self.box = QCheckBox(f"{chat.get('title') or 'Chat'} · {kind}")
        self.box.setObjectName("OptionTitle")
        self.box.setChecked(ticked)
        self.box.toggled.connect(lambda on: set_prop(self, "selected", on))
        set_prop(self, "selected", ticked)
        lay.addWidget(self.box)
        lay.addStretch(1)
        self.detail = muted(detail, wrap=False)
        lay.addWidget(self.detail)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.box.toggle()
        super().mousePressEvent(event)


class TgChatStep(Step):
    title = "Connect your chat"
    strip = "Connect chat"
    subtitle = ("Press Connect, then open your bot with the button (or the code) and press Start in Telegram. "
                "For a family group, add the bot to the group, send /start there and tick the group below.")

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        self.listener = None
        self.paused = False
        self.code = ""
        self.deadline = 0.0
        self.rows: dict[int, ChatRow] = {}
        self.bot_lbl = label("", "OkText")
        self.body.addWidget(self.bot_lbl)
        top = QHBoxLayout()
        top.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.connect_btn = button("Connect", "primary")
        self.connect_btn.setMinimumWidth(160)
        self.connect_btn.setAccessibleName("Connect: start listening for /start")
        self.connect_btn.clicked.connect(self.start_listening)
        left.addWidget(self.connect_btn, 0, Qt.AlignLeft)
        self.wait = label("", "StepStatus", wrap=True)
        self.wait.setVisible(False)
        left.addWidget(self.wait)
        self.open_bot = button("Open the bot")
        dlg.ctx.bind_icon(self.open_bot, "external")
        self.open_bot.clicked.connect(self._open_bot)
        self.open_bot.setVisible(False)
        left.addWidget(self.open_bot, 0, Qt.AlignLeft)
        self.qr_hint = muted("Or scan the code with your phone's camera to open the bot.")
        self.qr_hint.setVisible(False)
        left.addWidget(self.qr_hint)
        left.addStretch(1)
        top.addLayout(left, 1)
        self.qr = QrView(132)
        self.qr.setVisible(False)
        top.addWidget(self.qr, 0, Qt.AlignTop)
        self.body.addLayout(top)
        self.list_title = heading("Chats that answered")
        self.body.addWidget(self.list_title)
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(6)
        self.body.addLayout(self.list_box)
        self.empty = muted("No chat yet. Press Connect and send /start to your bot.")
        self.body.addWidget(self.empty)
        self.unsure = muted("Anyone can message a bot. Tick a chat that did not send the code only if you "
                            "know it is yours or your family's.")
        self.unsure.setVisible(False)
        self.body.addWidget(self.unsure)
        self.manual_link = link_button("Enter a chat ID instead", "For experts: add a chat by its number")
        self.manual_link.clicked.connect(self._show_manual)
        self.body.addWidget(self.manual_link, 0, Qt.AlignLeft)
        self.manual = QWidget()
        ml = QHBoxLayout(self.manual)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(8)
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("Chat ID, for example 123456789 or -1001234567890")
        self.manual_edit.setAccessibleName("Chat ID")
        add = button("Add")
        add.clicked.connect(self._add_manual)
        self.manual_edit.returnPressed.connect(self._add_manual)
        ml.addWidget(self.manual_edit, 1)
        ml.addWidget(add)
        self.manual.setVisible(False)
        self.body.addWidget(self.manual)
        self.errors["chats"] = field_error()
        self.body.addWidget(self.errors["chats"])
        self.finish_body()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        for chat in dlg.params.get("chats") or []:
            self._add_row(chat, bool(chat.get("enabled", True)), "Already connected")

    def _bot(self) -> str:
        return self.dlg.params.get("bot_username", "") or ""

    def enter(self) -> None:
        bot = self._bot()
        self.open_bot.setText(f" Open @{bot}" if bot else " Open the bot")
        self.bot_lbl.setText(f"Found your bot @{bot}." if bot else "")
        self.bot_lbl.setVisible(bool(bot))

    def start_listening(self) -> None:
        tg = integ("telegram")
        if tg is None:
            self.show_problem("The Telegram part of YardGuard is missing. Reinstall or update YardGuard.")
            return
        self.stop_listening()
        self.clear_errors()
        if self.dlg.editing:
            ch = self.dlg.hub.channel(self.dlg.conf.id)
            if ch is not None and hasattr(ch, "pause_polling"):
                try:
                    ch.pause_polling()
                    self.paused = True
                except Exception:
                    log.exception("Pausing the Telegram poller failed")
        self.code = tg.new_pair_code()
        try:
            self.listener = tg.PairingListener(self.dlg.secret("token"), self.code,
                                               api_base=self.dlg.params.get("api_base") or "https://api.telegram.org")
            self.listener.start()
        except Exception as exc:
            log.exception("Telegram listener failed to start")
            self.listener = None
            self.show_problem(f"Could not start listening ({type(exc).__name__}). Try again.")
            self._resume()
            return
        self.deadline = time.time() + LISTEN_S
        self.connect_btn.setText("Listening…")
        self.connect_btn.setEnabled(False)
        bot = self._bot()
        link = tg.deep_link(bot, self.code) if bot else ""
        self.open_bot.setVisible(bool(link))
        has_qr = self.qr.set_text(link) if link else False
        self.qr_hint.setVisible(has_qr)
        self.timer.start()
        self._poll()

    def _open_bot(self) -> None:
        tg = integ("telegram")
        if tg and self._bot():
            open_url(tg.deep_link(self._bot(), self.code))

    def _poll(self) -> None:
        if self.listener is None:
            return
        try:
            found = self.listener.found() or []
        except Exception:
            log.exception("Telegram listener failed")
            found = []
        # only chats that sent the code are ticked: anybody can message a bot, so a chat that only
        # sent a message waits for the user to tick it
        for chat in sorted(found, key=lambda c: (not c.get("with_code"), c.get("type") != "private")):
            cid = chat.get("id")
            if cid is None:
                continue
            who = chat.get("user")
            by = f" · {who}" if who and who != chat.get("title") else ""
            row = self.rows.get(cid)
            if row is not None:
                # a chat that first sent something else and then the code
                if chat.get("with_code") and row.detail.text().startswith("Sent a message"):
                    row.detail.setText(f"Sent the code{by}")
                    row.box.setChecked(True)
                continue
            code = bool(chat.get("with_code"))
            how = "Sent the code" if code else "Sent a message, not the code"
            self._add_row({"id": cid, "title": chat.get("title") or who or "Chat", "type": chat.get("type", "private"),
                           "enabled": True}, code, f"{how}{by}")
            if not code:
                self.unsure.setVisible(True)
            self.dlg.mark_dirty()
        error = getattr(self.listener, "error", "")
        left = int(self.deadline - time.time())
        if error:
            set_status(self.wait, "offline", safe_text(error))
            self.wait.setVisible(True)
            self.stop_listening()
        elif left <= 0:
            self.stop_listening()
            set_status(self.wait, "checking", "Stopped waiting. Press Connect to listen again.")
            self.wait.setVisible(True)
        else:
            set_status(self.wait, "nosignal", f"Waiting for /start… {left // 60}:{left % 60:02d} left")
            self.wait.setVisible(True)

    def _ticked(self) -> list[dict]:
        return [r.chat for r in self.rows.values() if r.box.isChecked()]

    def _add_row(self, chat: dict, ticked: bool, detail: str) -> None:
        row = ChatRow(chat, ticked, detail)
        row.box.toggled.connect(lambda *_: self.changed.emit())
        row.box.toggled.connect(self.dlg.mark_dirty)
        self.rows[chat["id"]] = row
        self.list_box.addWidget(row)
        self.empty.setVisible(False)
        self.changed.emit()

    def _show_manual(self) -> None:
        self.manual.setVisible(True)
        self.manual_link.setVisible(False)
        self.manual_edit.setFocus()

    def _add_manual(self) -> None:
        text = self.manual_edit.text().strip()
        try:
            cid = int(text)
        except ValueError:
            show_error(self.errors["chats"], "A chat ID is a number, for example 123456789 or -1001234567890.")
            return
        show_error(self.errors["chats"], "")
        if cid not in self.rows:
            self._add_row({"id": cid, "title": f"Chat {safe_text(str(cid))}", "type": "group" if cid < 0 else "private",
                           "enabled": True}, True, "Added by ID")
        self.manual_edit.clear()

    def complete(self) -> bool:
        return bool(self._ticked())

    def validate(self, done) -> None:
        chats = []
        for cid, row in self.rows.items():
            if row.box.isChecked() or row.detail.text() == "Already connected":
                chats.append({**row.chat, "enabled": row.box.isChecked()})
        self.dlg.params["chats"] = chats
        self.stop_listening()
        done(True)

    def stop_listening(self) -> None:
        """Stop the listener off the GUI thread (it may wait up to a few seconds for its last poll),
        then let the destination's own poller run again (never both at once: Telegram answers 409)."""
        self.timer.stop()
        listener, self.listener = self.listener, None
        if listener is not None:
            hub, conf, paused = self.dlg.hub, self.dlg.conf, self.paused
            self.paused = False

            def stop() -> None:
                try:
                    listener.stop()
                except Exception:
                    log.exception("Stopping the Telegram listener failed")

            run_async(stop, on_done=lambda _r: paused and _resume_poller(hub, conf))
        else:
            self._resume()
        try:
            self.connect_btn.setText("Connect")
            self.connect_btn.setEnabled(True)
        except RuntimeError:
            pass

    def _resume(self) -> None:
        if self.paused:
            self.paused = False
            _resume_poller(self.dlg.hub, self.dlg.conf)

    def leave(self) -> None:
        self.stop_listening()


def _resume_poller(hub, conf) -> None:
    ch = hub.channel(conf.id) if hub is not None and conf is not None else None
    if ch is not None and hasattr(ch, "resume_polling"):
        try:
            ch.resume_polling()
        except Exception:
            log.exception("Resuming the Telegram poller failed")


class TelegramSetup(SetupDialog):
    KIND = "telegram"
    NOUN = "Telegram"

    def __init__(self, ctx, hub, conf=None, start_step: int = 0, parent=None):
        super().__init__(ctx, hub, conf, start_step, parent)
        self.test_step = TestStep(self, "YardGuard sends a sample alert with a picture and buttons to the chats you "
                                        "ticked.")
        self.allow = QCheckBox("Let people in these chats turn monitoring on or off and mark incidents")
        self.allow.setAccessibleName("Let people in these chats turn monitoring on or off, pause messages and "
                                     "mark incidents")
        self.allow.setChecked(bool(self.params.get("allow_control", True)))
        self.allow.toggled.connect(self.mark_dirty)
        self.test_step.extra.addWidget(self.allow)
        allow_note = muted("Also pausing messages. Commands: /arm, /disarm, /mute, and the Handled / False alarm "
                           "buttons.")
        allow_note.setContentsMargins(26, 0, 0, 0)
        self.test_step.extra.addWidget(allow_note)
        self.test_step.extra.addWidget(muted("Telegram chats are not end-to-end encrypted; pictures are stored on "
                                             "Telegram's servers."))
        self.set_steps([TgTokenStep(self), TgChatStep(self), self.test_step], start_step)

    def apply_extra(self) -> None:
        self.params["allow_control"] = self.allow.isChecked()

    def temp_conf(self):
        self.apply_extra()
        return super().temp_conf()


# --- ntfy ------------------------------------------------------------------------------------------------

class NtfyStep(Step):
    title = "Install ntfy on your phone"
    strip = "Phone app"
    subtitle = "ntfy is a free app for push notifications. No account is needed."

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        ntfy = integ("ntfy")
        p = dlg.params
        if not p.get("topic"):
            p["topic"] = ntfy.random_topic() if ntfy else "yardguard-" + pysecrets.token_hex(10)
        p.setdefault("server", "https://ntfy.sh")
        stores = QHBoxLayout()
        stores.setSpacing(8)
        for name, url in NTFY_APPS:
            b = button(name)
            dlg.ctx.bind_icon(b, "external")
            b.clicked.connect(lambda _=False, u=url: open_url(u))
            stores.addWidget(b)
        stores.addStretch(1)
        stores_w = QWidget()
        stores_w.setLayout(stores)
        row = QHBoxLayout()
        row.setSpacing(16)
        steps = numbered([
            ("Install the ntfy app.", stores_w),
            "In the app, tap <b>+</b> and enter the topic name below, or scan the code with your phone's camera.",
            "Press Next. YardGuard checks the server, then sends a test.",
        ])
        row.addWidget(steps, 1)
        self.qr = QrView(132)
        row.addWidget(self.qr, 0, Qt.AlignTop)
        self.body.addLayout(row)
        form = form_layout()
        self.topic = CopyField(dlg.ctx, p["topic"], "Topic name", mono=True)
        form_row(form, "Topic name", self.topic)
        self.body.addLayout(form)
        self.body.addWidget(muted("Anyone who knows this topic name can read the messages. That is why it is long "
                                  "and random."))
        adv = QWidget()
        af = form_layout()
        af.setContentsMargins(0, 0, 0, 0)
        adv.setLayout(af)
        self.server = QLineEdit(p.get("server") or "https://ntfy.sh")
        self.server.setAccessibleName("ntfy server")
        self.server.textEdited.connect(dlg.mark_dirty)
        self.server.editingFinished.connect(self._refresh_qr)
        form_row(af, "Server", self.server)
        self.token = password_field(dlg.ctx, "Only for your own server or a paid account")
        self.token.setAccessibleName("Access token")
        if dlg.has_saved("token"):
            self.token.setPlaceholderText("Saved - type to replace")
        self.token.textEdited.connect(dlg.mark_dirty)
        form_row(af, "Access token (optional)", self.token)
        self.body.addWidget(Disclosure("Advanced", adv, opened=self.server.text() != "https://ntfy.sh"
                                       or dlg.has_saved("token")))
        self.errors["server"] = field_error()
        self.errors["token"] = field_error()
        self.body.addWidget(self.errors["server"])
        self.body.addWidget(self.errors["token"])
        self.finish_body()
        self._refresh_qr()

    def _subscribe_url(self) -> str:
        ntfy = integ("ntfy")
        server, topic = self.server.text().strip().rstrip("/") or "https://ntfy.sh", self.dlg.params["topic"]
        try:
            return ntfy.subscribe_url(server, topic) if ntfy else f"{server}/{topic}"
        except Exception:
            return f"{server}/{topic}"

    def _refresh_qr(self) -> None:
        self.qr.set_text(self._subscribe_url())

    def validate(self, done) -> None:
        server = self.server.text().strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", server):
            self.show_problem("The server address starts with https://, for example https://ntfy.sh", "server")
            done(False)
            return
        self.dlg.params["server"] = server
        self.dlg.set_typed("token", self.token.text())
        self.dlg.run_check(self, done, busy_text="Checking the ntfy server…")


class NtfySetup(SetupDialog):
    KIND = "ntfy"
    NOUN = "phone push (ntfy)"

    def __init__(self, ctx, hub, conf=None, start_step: int = 0, parent=None):
        super().__init__(ctx, hub, conf, start_step, parent)
        self.set_steps([NtfyStep(self), TestStep(self)], start_step)


# --- Email -----------------------------------------------------------------------------------------------

def presets() -> dict:
    mod = integ("emailer")
    return dict(getattr(mod, "PRESETS", None) or EMAIL_PRESETS)


class EmailProviderStep(Step):
    title = "Which email do you use?"
    strip = "Provider"
    subtitle = "YardGuard sends the email from your own address, through your provider."
    NAMES = (("gmail", "Gmail"), ("outlook", "Outlook / Hotmail"), ("yahoo", "Yahoo"), ("other", "Other"))

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        self.group = QButtonGroup(self)
        self.options: dict[str, _ModeOption] = {}
        pre = presets()
        for i, (key, name) in enumerate(self.NAMES):
            text = pre.get(key, {}).get("help") or "Your provider's mail server settings (SMTP). Your provider's " \
                                                   "help pages list them."
            opt = _ModeOption(name, text, key == "gmail", self.group, i)
            self.options[key] = opt
            self.body.addWidget(opt)
        self.help_btn = button("Open the app password page")
        dlg.ctx.bind_icon(self.help_btn, "external")
        self.help_btn.clicked.connect(self._open_help)
        self.body.addWidget(self.help_btn, 0, Qt.AlignLeft)
        current = dlg.params.get("preset") or "gmail"
        self.options.get(current, self.options["gmail"]).radio.setChecked(True)
        self.group.idToggled.connect(self._picked)
        self._picked()
        self.finish_body()

    def key(self) -> str:
        return self.NAMES[max(0, self.group.checkedId())][0]

    def _picked(self, *_) -> None:
        self.help_btn.setVisible(bool(presets().get(self.key(), {}).get("help_url")))
        self.dlg.mark_dirty()

    def _open_help(self) -> None:
        url = presets().get(self.key(), {}).get("help_url")
        if url:
            open_url(url)

    def validate(self, done) -> None:
        key = self.key()
        p = self.dlg.params
        if p.get("preset") != key:
            p["preset"] = key
            pre = presets().get(key, {})
            if key != "other":
                p.update(host=pre.get("host", ""), port=pre.get("port", 587), security=pre.get("security", "starttls"))
        done(True)


class EmailAccountStep(Step):
    title = "Your email account"
    strip = "Account"

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        p = dlg.params
        form = form_layout()
        self.address = QLineEdit(p.get("sender") or p.get("username") or "")
        self.address.setPlaceholderText("you@gmail.com")
        self.address.setAccessibleName("Your address")
        self.focus_widget = self.address
        self.password = password_field(dlg.ctx, "App password")
        self.password.setAccessibleName("App password")
        if dlg.has_saved("password"):
            self.password.setPlaceholderText("Saved - type to replace")
        pw_box = QWidget()
        pw_row = QHBoxLayout(pw_box)
        pw_row.setContentsMargins(0, 0, 0, 0)
        pw_row.setSpacing(8)
        pw_row.addWidget(self.password, 1)
        self.help_btn = link_button("Get an app password", "Opens your provider's app password page in the browser")
        self.help_btn.clicked.connect(self._open_help)
        pw_row.addWidget(self.help_btn)
        self.to = QLineEdit(", ".join(p.get("to") or []))
        self.to.setPlaceholderText("Same as your address; separate several with commas")
        self.to.setAccessibleName("Send to")
        self.username = QLineEdit(p.get("username") if p.get("username") != p.get("sender") else "")
        self.username.setPlaceholderText("Same as your address")
        self.host = QLineEdit(p.get("host") or "")
        self.host.setPlaceholderText("smtp.example.com")
        self.port = spin_box(1, 65535, int(p.get("port") or 587))
        self.port.setFixedWidth(110)
        self.security = QComboBox()
        for key, text in (("starttls", "STARTTLS (port 587)"), ("ssl", "SSL/TLS (port 465)"),
                          ("none", "None (not encrypted)")):
            self.security.addItem(text, key)
        self.security.setCurrentIndex(max(0, self.security.findData(p.get("security") or "starttls")))
        # the port follows the security choice until the user types a port of their own
        self._port_typed = int(p.get("port") or 587) not in (587, 465)
        self._setting_port = False
        self.port.valueChanged.connect(self._port_edited)
        self.security.currentIndexChanged.connect(self._security_changed)
        for w in (self.address, self.password, self.to, self.username, self.host):
            w.textEdited.connect(dlg.mark_dirty)
            w.textChanged.connect(lambda *_: self.changed.emit())
        self.rows: dict[str, QWidget] = {}
        for key, text, w in (("sender", "Your address", self.address), ("password", "App password", pw_box),
                             ("to", "Send to", self.to), ("username", "User name", self.username),
                             ("host", "Mail server", self.host), ("port", "Port", self.port),
                             ("security", "Security", self.security)):
            box, self.errors[key] = with_error(w)
            form_row(form, text, box)
            self.rows[key] = box
        self.errors["user"] = self.errors["username"]
        self.errors["address"] = self.errors["sender"]
        self.body.addLayout(form)
        self.app_note = muted("")
        self.body.addWidget(self.app_note)
        self.finish_body()
        self._form = form

    EXAMPLES = {"gmail": "you@gmail.com", "outlook": "you@outlook.com", "yahoo": "you@yahoo.com"}

    def _preset(self) -> str:
        return self.dlg.params.get("preset") or "gmail"

    def _example(self) -> str:
        return self.EXAMPLES.get(self._preset(), "you@example.com")

    def enter(self) -> None:
        other = self._preset() == "other"
        for key in ("username", "host", "port", "security"):
            self._form.setRowVisible(self.rows[key], other)
        pre = presets().get(self._preset(), {})
        self.address.setPlaceholderText(self._example())
        self.app_note.setText(pre.get("help", ""))
        self.app_note.setVisible(bool(pre.get("help")))
        self.help_btn.setVisible(bool(pre.get("help_url")))
        self.password.setPlaceholderText("Saved - type to replace" if self.dlg.has_saved("password")
                                         else "App password" if pre.get("help_url") else "Password")

    def _open_help(self) -> None:
        url = presets().get(self._preset(), {}).get("help_url")
        if url:
            open_url(url)

    def _port_edited(self, *_) -> None:
        if not self._setting_port:
            self._port_typed = True
            self.dlg.mark_dirty()

    def _security_changed(self, *_) -> None:
        self.dlg.mark_dirty()
        if self._port_typed:
            return
        self._setting_port = True
        try:
            self.port.setValue(465 if self.security.currentData() == "ssl" else 587)
        finally:
            self._setting_port = False

    def show_problem(self, message: str, field: str = "") -> None:
        if field == "password" and message.startswith("Password not accepted"):
            # name the provider the user picked, and say how to get an app password there
            name = {"gmail": "Gmail", "outlook": "Outlook", "yahoo": "Yahoo"}.get(self._preset())
            if self._preset() == "gmail":
                message = ("Gmail did not accept this password. Use an app password, not your normal password: "
                           "turn on 2-Step Verification first, then create an app password.")
            elif self._preset() == "yahoo":
                message = "Yahoo did not accept this password. Use an app password (Get an app password)."
            elif name:
                message = f"{name} did not accept this password. Check it and try again."
            else:
                message = "The mail server did not accept the user name or password."
        super().show_problem(message, field)

    def complete(self) -> bool:
        has_pw = bool(self.password.text().strip()) or self.dlg.has_saved("password")
        return bool(self.address.text().strip()) and has_pw

    def validate(self, done) -> None:
        p = self.dlg.params
        address = self.address.text().strip()
        if not EMAIL_RE.match(address):
            self.show_problem(f"Enter your full email address, for example {self._example()}", "sender")
            done(False)
            return
        to = [t.strip() for t in re.split(r"[,;\s]+", self.to.text()) if t.strip()] or [address]
        bad = [t for t in to if not EMAIL_RE.match(t)]
        if bad:
            self.show_problem(f"This is not an email address: {bad[0]}", "to")
            done(False)
            return
        p["sender"] = address
        p["to"] = to
        if p.get("preset") == "other":
            host = self.host.text().strip()
            if not host:
                self.show_problem("Enter your provider's mail server, for example smtp.example.com", "host")
                done(False)
                return
            p.update(host=host, port=self.port.value(), security=self.security.currentData(),
                     username=self.username.text().strip() or address)
        else:
            p["username"] = address
        self.dlg.set_typed("password", self.password.text())
        if not self.dlg.editing:
            self.dlg.name = "Email"
        self.dlg.run_check(self, done, busy_text="Logging in to your mail server…")


class EmailSetup(SetupDialog):
    KIND = "email"
    NOUN = "email"

    def __init__(self, ctx, hub, conf=None, start_step: int = 0, parent=None):
        super().__init__(ctx, hub, conf, start_step, parent)
        self.set_steps([EmailProviderStep(self), EmailAccountStep(self),
                        TestStep(self, "YardGuard sends a sample alert email with the picture to the address under "
                                       "Send to.")], start_step)


# --- Discord ------------------------------------------------------------------------------------------------

class DiscordStep(Step):
    title = "Create a webhook in Discord"
    strip = "Webhook"
    subtitle = "A webhook lets YardGuard post into one channel of your Discord server."

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        self.body.addWidget(numbered([
            "In Discord open <b>Server settings &gt; Integrations &gt; Webhooks</b>.",
            "Press <b>New webhook</b>, choose the channel, then <b>Copy webhook URL</b>.",
            "Paste it here.",
        ]))
        form = form_layout()
        self.url = password_field(dlg.ctx, "https://discord.com/api/webhooks/…")
        self.url.setAccessibleName("Webhook URL")
        self.focus_widget = self.url
        if dlg.has_saved("url"):
            self.url.setPlaceholderText("Saved - type to replace")
        self.url.textEdited.connect(dlg.mark_dirty)
        self.url.textChanged.connect(lambda *_: self.changed.emit())
        box, self.errors["url"] = with_error(self.url)
        form_row(form, "Webhook URL", box)
        self.body.addLayout(form)
        self.body.addWidget(muted("Keep this link private: anyone who has it can post into the channel."))
        self.finish_body()

    def complete(self) -> bool:
        return bool(self.url.text().strip()) or self.dlg.has_saved("url")

    def validate(self, done) -> None:
        url = self.url.text().strip()
        if url:
            if not DISCORD_RE.match(url):
                self.show_problem("This is not a Discord webhook link. It starts with "
                                  "https://discord.com/api/webhooks/", "url")
                done(False)
                return
            self.dlg.set_typed("url", url)

        def ok(res) -> None:
            name = (getattr(res, "data", {}) or {}).get("name")
            if name and not self.dlg.editing:
                self.dlg.name = "Discord"

        self.dlg.run_check(self, done, ok, "Checking the webhook…")


class DiscordSetup(SetupDialog):
    KIND = "discord"
    NOUN = "Discord"

    def __init__(self, ctx, hub, conf=None, start_step: int = 0, parent=None):
        super().__init__(ctx, hub, conf, start_step, parent)
        self.set_steps([DiscordStep(self), TestStep(self)], start_step)


# --- Webhook ------------------------------------------------------------------------------------------------

SAMPLE_PAYLOAD = """{
  "schema": "yardguard.v1",
  "event": "incident_new",
  "id": "3f9a1c2e4b5d",
  "ts": 1761200045.0,
  "time": "2026-10-23T02:14:05+04:00",
  "site": {"id": "a1b2c3d4", "name": "Home"},
  "camera": {"channel": 1, "name": "Front gate"},
  "title": "Person near the car at night",
  "text": "Front gate · 02:14 · Critical",
  "severity": "critical",
  "verdict": "confirmed",
  "ai_note": "hand on door handle",
  "incident": {"id": "inc-0123456789ab", "status": "open", "alert_count": 2, "cameras": ["Front gate"]},
  "labels": ["person", "car"],
  "area": null,
  "plate": null,
  "image": null,
  "web_url": null
}"""


class WebhookStep(Step):
    title = "Where should YardGuard send alerts?"
    strip = "Address"
    subtitle = "Each alert is sent as JSON with an HTTP POST, for example to n8n, Node-RED or your own server."

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        form = form_layout()
        self.url = password_field(dlg.ctx, "https://example.com/yardguard")
        self.url.setAccessibleName("Webhook address")
        self.focus_widget = self.url
        if dlg.has_saved("url"):
            self.url.setPlaceholderText("Saved - type to replace")
        self.url.textEdited.connect(dlg.mark_dirty)
        self.url.textChanged.connect(lambda *_: self.changed.emit())
        box, self.errors["url"] = with_error(self.url)
        form_row(form, "Address (URL)", box)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.secret = password_field(dlg.ctx, "Optional")
        self.secret.setAccessibleName("Signing secret")
        if dlg.has_saved("hmac_secret"):
            self.secret.setPlaceholderText("Saved - type to replace")
        self.secret.textEdited.connect(dlg.mark_dirty)
        gen = button("Generate")
        gen.setToolTip("Make a long random secret")
        gen.clicked.connect(self._generate)
        srow.addWidget(self.secret, 1)
        srow.addWidget(gen)
        form_row(form, "Signing secret", srow)
        form.addRow("", muted("With a secret, each request carries X-YardGuard-Signature: sha256=HMAC(secret, "
                              "\"timestamp.body\") so your server can check it came from YardGuard."))
        self.picture = QCheckBox("Include the picture (base64 inside the JSON)")
        self.picture.setChecked(dlg.params.get("include_image") == "base64")
        self.picture.toggled.connect(dlg.mark_dirty)
        form_row(form, "", self.picture)
        self.body.addLayout(form)
        head = QHBoxLayout()
        head.addWidget(heading("What YardGuard sends"))
        head.addStretch(1)
        copy_btn = button("Copy", "ghost", "Copy the example to the clipboard")
        dlg.ctx.bind_icon(copy_btn, "copy", "muted")
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(SAMPLE_PAYLOAD))
        head.addWidget(copy_btn)
        self.body.addLayout(head)
        self.preview = QPlainTextEdit(SAMPLE_PAYLOAD)
        self.preview.setObjectName("Mono")
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(150)
        self.preview.setAccessibleName("Example message")
        self.body.addWidget(self.preview)
        self.finish_body()

    def _generate(self) -> None:
        self.secret.setText(pysecrets.token_urlsafe(32))
        self.secret.setEchoMode(QLineEdit.Normal)
        self.dlg.mark_dirty()

    def complete(self) -> bool:
        return bool(self.url.text().strip()) or self.dlg.has_saved("url")

    def validate(self, done) -> None:
        url = self.url.text().strip()
        if url:
            if not re.match(r"^https?://[^\s/]+", url):
                self.show_problem("The address starts with http:// or https://", "url")
                done(False)
                return
            self.dlg.set_typed("url", url)
        self.dlg.set_typed("hmac_secret", self.secret.text())
        self.dlg.params["include_image"] = "base64" if self.picture.isChecked() else "none"
        self.dlg.params.setdefault("method", "POST")
        self.dlg.run_check(self, done, busy_text="Sending a test request…")


class WebhookSetup(SetupDialog):
    KIND = "webhook"
    NOUN = "a webhook"

    def __init__(self, ctx, hub, conf=None, start_step: int = 0, parent=None):
        super().__init__(ctx, hub, conf, start_step, parent)
        self.set_steps([WebhookStep(self), TestStep(self, "YardGuard sends a sample alert to your address.")],
                       start_step)


# --- Home Assistant (MQTT) -------------------------------------------------------------------------------

def pip_install(package: str) -> tuple[bool, str]:
    """Install a pure-Python add-on into YardGuard's own Python (run.cmd keeps caches off C:)."""
    runner = ROOT / "run.cmd"
    cmd = [str(runner), "-m", "pip", "install", package] if runner.exists() and sys.platform == "win32" \
        else [sys.executable, "-m", "pip", "install", package]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=flags, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return False, "The download took too long. Check the internet connection and try again."
    except OSError as exc:
        return False, f"Could not run the installer ({type(exc).__name__})."
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip().splitlines()[-1:] or ["unknown error"]
        return False, "The add-on could not be installed: " + safe_text(tail[0])[:200]
    importlib.invalidate_caches()
    return True, ""


class MqttAddonStep(Step):
    title = "Install the Home Assistant add-on"
    strip = "Add-on"
    subtitle = "This needs a small add-on (about 1 MB) that lets YardGuard talk to Home Assistant over MQTT."

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        self._installed = mqtt_available()
        self._skip = self._installed       # decided once: after installing here, Next moves on
        row = QHBoxLayout()
        self.install_btn = button("Install add-on", "primary")
        self.install_btn.clicked.connect(self.install)
        row.addWidget(self.install_btn)
        row.addStretch(1)
        self.body.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.body.addWidget(self.progress)
        self.finish_body()

    def skip(self) -> bool:
        return self._skip

    def complete(self) -> bool:
        return self._installed

    def install(self) -> None:
        self.install_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.set_state("checking", "Downloading and installing… this takes about a minute.")
        run_async(pip_install, "paho-mqtt", on_done=self._done)

    def _done(self, res) -> None:
        self.progress.setVisible(False)
        self.install_btn.setEnabled(True)
        ok, msg = res if isinstance(res, tuple) else (False, "The add-on could not be installed.")
        if ok:
            mod = sys.modules.get("yardguard.integrations.mqtt_ha")
            if mod is not None:
                try:
                    importlib.reload(mod)
                except Exception:
                    log.exception("Reloading the MQTT part failed")
            self._installed = importlib.util.find_spec("paho") is not None
        if ok and self._installed:
            self.set_state("online", "Add-on installed.")
            self.install_btn.setVisible(False)
        else:
            self.set_state("offline", msg or "The add-on could not be installed.")
        self.changed.emit()


class MqttBrokerStep(Step):
    title = "Your MQTT broker"
    strip = "Broker"
    subtitle = ("Home Assistant usually runs the Mosquitto broker add-on. Use the user name and password of an "
                "MQTT user (Settings > People > Users in Home Assistant).")

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        p = dlg.params
        form = form_layout()
        self.host = QLineEdit(p.get("host") or "")
        self.host.setPlaceholderText("homeassistant.local")
        self.host.setAccessibleName("Broker address")
        self.focus_widget = self.host
        self.port = spin_box(1, 65535, int(p.get("port") or 1883))
        self.port.setFixedWidth(110)
        self.port.setAccessibleName("Broker port")
        self.user = QLineEdit(p.get("username") or "")
        self.user.setAccessibleName("MQTT user name")
        self.password = password_field(dlg.ctx, "MQTT password")
        self.password.setAccessibleName("MQTT password")
        if dlg.has_saved("password"):
            self.password.setPlaceholderText("Saved - type to replace")
        self.tls = QCheckBox("Use TLS (encrypted, usually port 8883)")
        self.tls.setChecked(bool(p.get("tls")))
        self.tls.toggled.connect(lambda on: self.port.setValue(8883 if on and self.port.value() == 1883 else
                                                               1883 if not on and self.port.value() == 8883
                                                               else self.port.value()))
        for key, text, w in (("host", "Broker address", self.host), ("port", "Port", self.port),
                             ("username", "User name", self.user), ("password", "Password", self.password),
                             ("tls", "", self.tls)):
            box, self.errors[key] = with_error(w)
            form_row(form, text, box)
        for w in (self.host, self.user, self.password):
            w.textEdited.connect(dlg.mark_dirty)
            w.textChanged.connect(lambda *_: self.changed.emit())
        self.body.addLayout(form)
        self.finish_body()

    def complete(self) -> bool:
        return bool(self.host.text().strip())

    def validate(self, done) -> None:
        p = self.dlg.params
        p.update(host=self.host.text().strip(), port=self.port.value(), username=self.user.text().strip(),
                 tls=self.tls.isChecked())
        self.dlg.set_typed("password", self.password.text())
        if not self.dlg.editing:
            self.dlg.name = "Home Assistant"
        self.dlg.run_check(self, done, busy_text="Connecting to the broker…")


class MqttDiscoveryStep(Step):
    title = "Let Home Assistant find YardGuard"
    strip = "Discovery"

    def __init__(self, dlg: SetupDialog):
        super().__init__(dlg)
        self.discovery = QCheckBox("Add YardGuard to Home Assistant automatically (recommended)")
        self.discovery.setChecked(bool(dlg.params.get("discovery", True)))
        self.discovery.toggled.connect(dlg.mark_dirty)
        self.body.addWidget(self.discovery)
        self.entities = muted()
        self.body.addWidget(self.entities)
        head = QHBoxLayout()
        head.addWidget(heading("Topics, for your own automations"))
        head.addStretch(1)
        self.copy_btn = button("Copy", "ghost", "Copy the topic list")
        dlg.ctx.bind_icon(self.copy_btn, "copy", "muted")
        self.copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.topics.toPlainText()))
        head.addWidget(self.copy_btn)
        self.body.addLayout(head)
        self.topics = QPlainTextEdit()
        self.topics.setObjectName("Mono")
        self.topics.setReadOnly(True)
        self.topics.setMinimumHeight(140)
        self.topics.setAccessibleName("MQTT topics")
        self.body.addWidget(self.topics)
        self.finish_body()

    def enter(self) -> None:
        p = self.dlg.params
        node = getattr(self.ctx.cfg.settings, "install_id", "") or "<id>"
        b = f"{p.get('base_topic') or 'yardguard'}/{node}"
        site = self.ctx.cfg.active_site()
        cams = site.enabled_cameras() if site else []
        lines = [f"{b}/availability", f"{b}/monitoring/state", f"{b}/monitoring/set", f"{b}/events",
                 f"{b}/incident", f"{b}/incident/set"]
        for cam in cams[:8]:
            c = f"{b}/cam/{cam.channel}"
            lines += [f"{c}/online", f"{c}/person", f"{c}/vehicle", f"{c}/person_count", f"{c}/alert",
                      f"{c}/image", f"{c}/snapshot/press"]
        self.topics.setPlainText("\n".join(lines))
        names = ", ".join(c.label for c in cams[:4]) + (" and more" if len(cams) > 4 else "")
        self.entities.setText(
            "Home Assistant will show a device called YardGuard with a monitoring switch and, for each camera"
            + (f" ({names})" if names else "") + ": online, person, vehicle, person count, the last alert picture, "
            "an alert event and a snapshot button.")

    def validate(self, done) -> None:
        self.dlg.params["discovery"] = self.discovery.isChecked()
        done(True)


class MqttSetup(SetupDialog):
    KIND = "mqtt"
    NOUN = "Home Assistant"

    def __init__(self, ctx, hub, conf=None, start_step: int = 0, parent=None):
        super().__init__(ctx, hub, conf, start_step, parent)
        self.addon = MqttAddonStep(self)
        self.set_steps([self.addon, MqttBrokerStep(self), MqttDiscoveryStep(self),
                        TestStep(self, "YardGuard publishes a test alert. Home Assistant should now show a device "
                                       "called YardGuard.")], start_step)


SETUP_CLASSES = {"telegram": TelegramSetup, "ntfy": NtfySetup, "email": EmailSetup, "discord": DiscordSetup,
                 "webhook": WebhookSetup, "mqtt": MqttSetup}


def setup_dialog(kind: str, ctx, hub, conf=None, start_step: int = 0, parent=None) -> SetupDialog | None:
    cls = SETUP_CLASSES.get(kind)
    return cls(ctx, hub, conf, start_step, parent) if cls else None


def fix_step(kind: str, conf, channel=None) -> int:
    """Step to reopen for "Fix": secrets that cannot be read or a credentials problem -> step 1
    (Home Assistant: the broker step), else the step after it (e.g. Telegram chats)."""
    text = (getattr(channel, "status_text", "") or "").lower()
    if kind == "telegram" and any(w in text for w in ("blocked", "removed", "chat")) and \
            not any(conf.secret_unreadable(k) for k in secret_keys(kind)):
        return 1
    return 1 if kind == "mqtt" else 0


# --- Phone access --------------------------------------------------------------------------------------------

PHONE_MODES = (
    ("lan", "Only on my home Wi-Fi",
     "Phones connected to your home Wi-Fi can open it. Windows may ask whether YardGuard may use the network: "
     "allow it on Private networks only.", True),
    ("away", "At home and away (needs Tailscale)",
     "Tailscale is a free app that links your own devices securely. No router changes, nothing open to the "
     "internet.", False),
    ("local", "Only on this PC (testing)", "Opens only in a browser on this PC. Good for trying it out.", False),
)


def remote_settings(ctx) -> dict:
    """The live Settings.remote dict (through WebRemote.remote, which normalises it, when there is one)."""
    web = getattr(ctx, "web", None)
    if web is not None:
        try:
            r = web.remote
            if isinstance(r, dict):
                return r
        except Exception:
            pass
    s = ctx.cfg.settings
    r = getattr(s, "remote", None)
    if not isinstance(r, dict):
        r = {}
        try:
            s.remote = r
        except AttributeError:
            pass
    return r


def remote_mode(r: dict) -> str:
    """lan | away | local from Settings.remote (never set up: lan, the recommended choice)."""
    if r.get("public_url"):
        return "away"
    if r.get("lan") or not r.get("enabled"):
        return "lan"
    return "local"


def tailscale_state(ts) -> tuple[str, str, str]:
    """(state, text, dns name) from webremote.tailscale; state: missing | signed_out | ready | error."""
    if ts is None:
        return "error", "The Tailscale helper of YardGuard is missing.", ""
    try:
        if not ts.find_cli():
            return "missing", "Tailscale is not installed on this PC yet.", ""
        st = ts.status() or {}
    except Exception as exc:
        return "error", f"Could not ask Tailscale ({type(exc).__name__}).", ""
    me = st.get("Self") if isinstance(st.get("Self"), dict) else {}
    dns = str(st.get("dns_name") or st.get("DNSName") or me.get("DNSName") or "").rstrip(".")
    backend = st.get("backend_state") or st.get("BackendState") or ""
    running = st.get("running", backend == "Running")
    if st.get("installed") is False:
        return "missing", "Tailscale is not installed on this PC yet.", ""
    if st.get("error") or not running or not dns:
        text = st.get("error") or "Tailscale is installed but not signed in. Open Tailscale and sign in."
        return "signed_out", safe_text(text), dns
    return "ready", f"Tailscale is ready on this PC: {dns}", dns


class PhoneWhereStep(Step):
    title = "Where will you use it?"
    strip = "Where"
    subtitle = "See your cameras, incidents and pictures in your phone's browser. Phones are paired once with a code."

    def __init__(self, dlg: "PhoneAccessDialog"):
        super().__init__(dlg)
        r = dlg.remote
        self.group = QButtonGroup(self)
        self.options: dict[str, _ModeOption] = {}
        for i, (key, title, text, rec) in enumerate(PHONE_MODES):
            opt = _ModeOption(title, text, rec, self.group, i)
            self.options[key] = opt
            self.body.addWidget(opt)
        self.options[dlg.mode].radio.setChecked(True)
        self.group.idToggled.connect(lambda *_: (dlg.mark_dirty(), self.changed.emit()))
        self.body.addWidget(muted("Nobody else can open it, even on your Wi-Fi: each phone needs a one-time "
                                  "pairing code from this PC. You can remove a phone at any time."))
        self.actions = QCheckBox("Let phones mark incidents and switch monitoring")
        self.actions.setChecked(bool(r.get("allow_actions")))
        self.actions.toggled.connect(dlg.mark_dirty)
        self.body.addWidget(self.actions)
        adv = QWidget()
        af = form_layout()
        af.setContentsMargins(0, 0, 0, 0)
        adv.setLayout(af)
        self.port = spin_box(1024, 65535, int(r.get("port") or 8765))
        self.port.setFixedWidth(110)
        self.port.setAccessibleName("Port")
        self.port.valueChanged.connect(dlg.mark_dirty)
        form_row(af, "Port", self.port)
        self.errors["port"] = field_error()
        af.addRow("", self.errors["port"])
        self.body.addWidget(Disclosure("Advanced", adv, opened=int(r.get("port") or 8765) != 8765))
        self.finish_body()

    def key(self) -> str:
        return PHONE_MODES[max(0, self.group.checkedId())][0]

    def validate(self, done) -> None:
        self.dlg.mode = self.key()
        self.dlg.port = self.port.value()
        self.dlg.allow_actions = self.actions.isChecked()
        done(True)


class PhoneTailscaleStep(Step):
    title = "Use it away from home"
    strip = "Tailscale"
    subtitle = "No router changes. Only your own devices signed in to your Tailscale account can open it."

    def __init__(self, dlg: "PhoneAccessDialog"):
        super().__init__(dlg)
        dl = button("Download Tailscale")
        dlg.ctx.bind_icon(dl, "external")
        dl.clicked.connect(lambda: open_url("https://tailscale.com/download/windows"))
        self.check_btn = button("Check again")
        dlg.ctx.bind_icon(self.check_btn, "refresh")
        self.check_btn.clicked.connect(self.check)
        self.body.addWidget(numbered([
            ("Install Tailscale on this PC and sign in.", dl),
            "Install Tailscale on your phone (App Store or Google Play) and sign in with the <b>same account</b>.",
            ("Come back here and check.", self.check_btn),
        ]))
        self.state_lbl = label("", "StepStatus", wrap=True)
        self.body.addWidget(self.state_lbl)
        row = QHBoxLayout()
        self.enable_btn = button("Enable secure link", "primary")
        self.enable_btn.clicked.connect(self.enable)
        self.enable_btn.setEnabled(False)
        row.addWidget(self.enable_btn)
        row.addStretch(1)
        self.body.addLayout(row)
        self.link = CopyField(dlg.ctx, "", "Secure link")
        self.link.setVisible(False)
        self.body.addWidget(self.link)
        self.finish_body()
        self._state = ""

    def skip(self) -> bool:
        return self.dlg.mode != "away"

    def enter(self) -> None:
        if self.dlg.public_url:
            self.link.set_text(self.dlg.public_url)
            self.link.setVisible(True)
        self.check()

    def check(self) -> None:
        set_status(self.state_lbl, "checking", "Checking Tailscale…")
        self.check_btn.setEnabled(False)
        run_async(tailscale_state, webremote_part("tailscale"), on_done=self._checked)

    def _checked(self, res) -> None:
        self.check_btn.setEnabled(True)
        state, text, _dns = res if isinstance(res, tuple) else ("error", "Could not ask Tailscale.", "")
        self._state = state
        set_status(self.state_lbl, {"ready": "online", "missing": "checking"}.get(state, "nosignal"), text)
        self.enable_btn.setEnabled(state == "ready")
        self.changed.emit()

    def enable(self) -> None:
        ts = webremote_part("tailscale")
        if ts is None:
            return
        self.enable_btn.setEnabled(False)
        set_status(self.state_lbl, "checking", "Turning on the secure link…")
        run_async(ts.serve_on, self.dlg.port, on_done=self._enabled)

    def _enabled(self, res) -> None:
        self.enable_btn.setEnabled(self._state == "ready")
        ok, value = res if isinstance(res, tuple) else (False, "Tailscale did not answer.")
        if ok:
            self.dlg.public_url = str(value)
            self.link.set_text(self.dlg.public_url)
            self.link.setVisible(True)
            set_status(self.state_lbl, "online", "Secure link is on. Phones signed in to Tailscale can open it.")
            self.dlg.mark_dirty()
        else:
            set_status(self.state_lbl, "offline", safe_text(value))
        self.changed.emit()

    def complete(self) -> bool:
        return bool(self.dlg.public_url)


class PhonePairStep(Step):
    title = "Add your phone"
    strip = "Add phone"
    subtitle = "Open your phone's camera and point it at the code. The page opens in the browser."

    def __init__(self, dlg: "PhoneAccessDialog"):
        super().__init__(dlg)
        row = QHBoxLayout()
        row.setSpacing(20)
        self.qr = QrView(220)
        row.addWidget(self.qr, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(8)
        self.addr_head = heading("Or type this address on the phone")
        col.addWidget(self.addr_head)
        # the short address to type; Copy and Open copy/open the full link with the code in it
        self.url = CopyField(dlg.ctx, "", "Address")
        col.addWidget(self.url)
        self.open_btn = button("Open in browser", "primary")
        dlg.ctx.bind_icon(self.open_btn, "external")
        self.open_btn.clicked.connect(lambda: self._link and open_url(self._link))
        self.open_btn.setVisible(False)
        col.addWidget(self.open_btn, 0, Qt.AlignLeft)
        self._link = ""
        self.code_hint = muted("Then enter the code:")
        col.addWidget(self.code_hint)
        self.code = label("", "PairCode")
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.code)
        self.expiry = muted("")
        col.addWidget(self.expiry)
        self.wait = label("", "StepStatus", wrap=True)
        col.addWidget(self.wait)
        self.another = button("Add another phone")
        self.another.clicked.connect(self.new_code)
        col.addWidget(self.another, 0, Qt.AlignLeft)
        col.addStretch(1)
        row.addLayout(col, 1)
        self.body.addLayout(row)
        self.finish_body()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.known: set[str] = set()
        self.paired: list[str] = []
        self.expires = 0.0
        sig = web_signals(dlg.ctx)
        if sig is not None:
            sig.changed.connect(self._tick)

    def enter(self) -> None:
        ok = self.dlg.apply_remote()
        web = self.dlg.web
        if not ok or web is None:
            err = getattr(web, "error", "") if web is not None else "Phone access is not part of this YardGuard."
            self.set_state("offline", safe_text(err) or "Phone access could not start.")
            self.qr.setVisible(False)
            self.url.setVisible(False)
            self.code.setText("")
            self.another.setVisible(False)
            self.wait.setVisible(False)
            return
        self.set_state("", "")
        self.url.setVisible(True)
        self.another.setVisible(True)
        self.wait.setVisible(True)
        local = self.dlg.mode == "local"
        # "Only on this PC": a phone cannot open 127.0.0.1, so no QR code and no phone wording
        self.dlg.subtitle.setText("Open this address in a browser on this PC. The page asks for the code once."
                                  if local else self.subtitle)
        self.addr_head.setText("Address on this PC" if local else "Or type this address on the phone")
        self.open_btn.setVisible(local)
        self.another.setText("Pair another browser" if local else "Add another phone")
        self.known = {d.get("id") for d in self._devices()}
        self.new_code()
        if local:
            self.qr.setVisible(False)

    def _devices(self) -> list[dict]:
        try:
            return list(self.dlg.web.devices() or [])
        except Exception:
            return []

    def new_code(self) -> None:
        try:
            code, url = self.dlg.web.new_pairing()
        except Exception as exc:
            log.exception("New pairing code failed")
            self.set_state("offline", f"Could not make a pairing code ({type(exc).__name__}).")
            return
        self._link = url
        if self.dlg.mode == "local":
            self.qr.setVisible(False)
        else:
            self.qr.set_text(url)
        self.url.set_text(base_address(url))
        self.url.copy_value = url
        self.code.setText(code)
        self.expires = time.time() + 300
        set_status(self.wait, "nosignal", "Waiting for the browser…" if self.dlg.mode == "local"
                   else "Waiting for your phone…")
        self.timer.start()
        self._tick()

    def _tick(self) -> None:
        if self.dlg.web is None or not self.timer.isActive() and not self.paired and self.expires <= 0:
            return
        for d in self._devices():
            if d.get("id") not in self.known:
                self.known.add(d.get("id"))
                self.paired.append(d.get("name") or "Phone")
        if self.paired:
            set_status(self.wait, "online", "Paired: " + ", ".join(self.paired))
        left = int(self.expires - time.time())
        if left > 0:
            self.expiry.setText(f"The code works for {left // 60}:{left % 60:02d} more and only once.")
        else:
            self.expiry.setText("This code has expired. Press Add another phone for a new one.")
            if not self.paired:
                set_status(self.wait, "checking", "The code expired before a phone used it.")
            self.timer.stop()

    def leave(self) -> None:
        self.timer.stop()


class PhoneAccessDialog(StepDialog):
    """Turn on phone access: where (home Wi-Fi / Tailscale / this PC), then pair phones by QR code."""

    STEP_WHERE, STEP_TAILSCALE, STEP_PAIR = 0, 1, 2

    def __init__(self, ctx, hub=None, start_step: int = 0, parent=None):
        super().__init__(ctx, "Phone access", parent)
        self.hub = hub
        self.web = getattr(ctx, "web", None)
        self.remote = remote_settings(ctx)
        self._before = copy.deepcopy(self.remote)
        self.mode = remote_mode(self.remote) if self.remote.get("enabled") else "lan"
        if start_step == self.STEP_TAILSCALE:
            self.mode = "away"
        self.port = int(self.remote.get("port") or 8765)
        self.allow_actions = bool(self.remote.get("allow_actions"))
        self.public_url = self.remote.get("public_url", "")
        self.applied = False
        self.pair = PhonePairStep(self)
        self.set_steps([PhoneWhereStep(self), PhoneTailscaleStep(self), self.pair], start_step)

    def finish_text(self) -> str:
        return "Done"

    def apply_remote(self) -> bool:
        """Save the choices and (re)start the server when something changed. True when it runs."""
        self.remote = r = remote_settings(self.ctx)
        want = {"enabled": True, "lan": self.mode == "lan", "port": self.port, "allow_actions": self.allow_actions,
                "public_url": self.public_url if self.mode == "away" else ""}
        changed = any(r.get(k) != v for k, v in want.items())
        r.update(want)
        if changed:
            self.ctx.cfg.save()
        self.applied = True
        if self.web is None:
            return False
        if changed or not getattr(self.web, "running", False):
            apply_web(self.ctx)
        return bool(getattr(self.web, "running", False)) and not getattr(self.web, "error", "")

    def finish(self) -> None:
        self.dirty = False
        self.accept()

    def reject(self) -> None:
        if self.applied and not self._before.get("enabled") and not self.pair.paired:
            # cancelled before any phone was paired: back to off, as it was (paired phones are kept)
            r = remote_settings(self.ctx)
            devices = r.get("devices")
            r.update({k: v for k, v in self._before.items() if k != "devices"})
            r["enabled"] = False
            if devices is not None:
                r["devices"] = devices
            self.ctx.cfg.save()
            apply_web(self.ctx)
            self.dirty = False
        elif self.applied:
            self.dirty = False
        super().reject()


class ManagePhonesDialog(QDialog):
    """Paired phones: name, added, last seen, Remove."""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.web = getattr(ctx, "web", None)
        self.setWindowTitle("Paired phones")
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)
        lay.addWidget(label("Paired phones", "PageTitle"))
        lay.addWidget(muted("Removing a phone signs it out at once. It can be paired again with a new code."))
        self.list_host = QWidget()
        self.rows = QVBoxLayout(self.list_host)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.list_host.setObjectName("Transparent")
        scroll.setWidget(self.list_host)
        lay.addWidget(scroll, 1)
        foot = QHBoxLayout()
        foot.addStretch(1)
        close = button("Close", "primary")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        lay.addLayout(foot)
        sig = web_signals(ctx)
        if sig is not None:
            sig.changed.connect(self.reload)
        self.reload()

    def reload(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        try:
            devices = list(self.web.devices() or []) if self.web is not None else []
        except Exception:
            devices = []
        if not devices:
            self.rows.addWidget(muted("No phone is paired yet."))
        for d in devices:
            row = QFrame()
            row.setObjectName("OptionCard")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 8, 8, 8)
            rl.setSpacing(10)
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(label(d.get("name") or "Phone", "CardTitle"))
            seen = d.get("last_seen") or 0
            col.addWidget(muted(f"Added {clock(d.get('created') or time.time())} · "
                                + (f"last seen {clock(seen)}" if seen else "not used yet"), wrap=False))
            rl.addLayout(col, 1)
            rm = button("Remove", "danger")
            rm.setAccessibleName(f"Remove {d.get('name') or 'phone'}")
            rm.clicked.connect(lambda _=False, dev=d: self.remove(dev))
            rl.addWidget(rm)
            self.rows.addWidget(row)
        self.rows.addStretch(1)

    def remove(self, dev: dict) -> None:
        if not confirm(self, "Remove phone", f"Remove {dev.get('name') or 'this phone'}? It is signed out at once.",
                       "Remove", danger=True):
            return
        try:
            self.web.remove_device(dev.get("id"))
        except Exception:
            log.exception("Removing a phone failed")
        self.reload()
