"""Integrations page: connect phones and other apps, choose what YardGuard sends, see what was sent.

IntegrationsPage  nav page with three tabs (Connections | What to send | Activity), "Pause messages"
                  and the paused chip. Talks to ctx.integrations (integrations.hub.IntegrationHub) and
                  ctx.web (webremote.WebRemote) only through their public methods and signals.
ConnectionsView   phone access card, a catalogue card per kind not connected yet, a card per destination.
ActivityView      the delivery log (hub.log) with statuses, "Why?" and Retry.

Setup dialogs live in integrations_setup, the routing editor in integrations_routes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, QModelIndex, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QBoxLayout, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .alerts_ui import Switch
from .incidents_ui import Segmented
from .integrations_routes import RoutesEditor
from .integrations_setup import (
    ElidedLabel, IconTile, ManagePhonesDialog, PhoneAccessDialog, apply_web, ask_text, catalog, channel_class, chip, clock,
    confirm, dest_labels, fix_step, groups,
    icon_tile, integ, link_button, mqtt_available, remote_mode, remote_settings, safe_text, secret_keys, set_chip,
    set_conf_name, setup_dialog, web_signals,
)
from .qt_util import set_prop
from .settings_ui import Banner
from .widgets import EmptyState, PageHeader, button, label, muted, set_status

log = logging.getLogger("yardguard")

TABS = (("Connections", "Phones and apps YardGuard sends to"), ("What to send", "Choose what each one receives"),
        ("Activity", "What was sent, and why something was not"))
CARD_H = 128
NARROW_W = 900              # below this the cards stack in one column
DETAIL_BELOW_W = 1100       # below this the Activity detail panel goes under the list
ROW_H = 56
ID_ROLE = Qt.UserRole + 1
ROW_ROLE = Qt.UserRole + 2
MAX_ATTEMPTS = 6

EXTRA_QSS = """
#IntegrationCard { background: $surface; border: 1px solid $border; border-radius: 10px; }
#IntegrationCard:hover { border-color: $border_strong; }
#IntegrationCard:focus { border: 1px solid $primary; }
#IntegrationCard[problem="true"] { border-color: $danger; }
QLabel#IconTile { background: $nav_active_bg; border-radius: 8px; }
#SnoozeIcon { background: transparent; }
#Chip[tone="danger"] { background: $danger_hover; color: $danger; font-weight: 600; }
#Chip[tone="success"] { color: $success; font-weight: 600; }
#Chip[tone="warning"] { color: $warning; font-weight: 600; }
#QrBox { background: #ffffff; border: 1px solid $border; border-radius: 8px; padding: 8px; }
#StepStrip { background: transparent; }
#StepNumber { background: $nav_active_bg; color: $nav_active_text; border-radius: 11px; font-weight: 700;
    font-size: 9pt; }
#StepStatus { padding: 2px 0; }
#ErrText { color: $danger; }
#OkText { color: $success; }
#PairCode { font-family: "Cascadia Mono", Consolas; font-size: 20pt; font-weight: 700; letter-spacing: 2px; }
QLineEdit#MonoField { font-family: "Cascadia Mono", Consolas; }
QPlainTextEdit#Mono { background: $surface2; border: 1px solid $border; border-radius: 6px; padding: 6px;
    font-family: "Cascadia Mono", Consolas; font-size: 9pt; color: $text; }
#SnoozeBar { background: $surface2; border: 1px solid $border; border-radius: 8px; }
#SnoozeBar QLabel { color: $warning; font-weight: 600; }
QPushButton#KindChip { background: $surface; border: 1px solid $border_strong; border-radius: 12px;
    padding: 2px 10px; min-height: 18px; color: $muted; font-size: 9pt; }
QPushButton#KindChip:hover { color: $text; }
QPushButton#KindChip:checked { background: $nav_active_bg; border-color: $nav_active_bg; color: $nav_active_text;
    font-weight: 600; }
QPushButton#KindChip:disabled { color: $border_strong; border-color: $border; background: $surface2; }
QCheckBox:disabled { color: $muted; }
QPushButton#KindChip:focus { border-color: $primary; }
#AreaRouteRow { background: $bg; border: 1px solid $border; border-radius: 8px; }
#AreaCamHead { color: $muted; font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; padding-top: 4px; }
#PreviewBox { background: $surface2; border-radius: 8px; padding: 10px 12px; color: $text; }
#PreviewBox:disabled { color: $muted; }
QListWidget#ActivityList { background: $surface; border: 1px solid $border; border-radius: 10px; padding: 4px; }
#ActivityList::item { padding: 0; margin: 0; border: none; background: transparent; }
#ActivityDetail { background: $surface; border: 1px solid $border; border-radius: 10px; }
#DetailKey { color: $muted; font-size: 9pt; }
QMenu#IntegrationsMenu { background: $surface; border: 1px solid $border_strong; border-radius: 8px; padding: 4px; }
QMenu#IntegrationsMenu::item { padding: 6px 18px 6px 12px; border-radius: 5px; color: $text; }
QMenu#IntegrationsMenu::item:selected { background: $nav_active_bg; color: $nav_active_text; }
QMenu#IntegrationsMenu::item:disabled { color: $muted; }
QMenu#IntegrationsMenu::separator { height: 1px; background: $border; margin: 4px 6px; }
"""

STATUS_WORDS = {
    "queued": ("nosignal", "Waiting to send"),
    "waiting_ai": ("nosignal", "Waiting for the AI"),
    "held": ("nosignal", "Held for quiet hours"),
    "sending": ("checking", "Sending…"),
    "sent": ("online", "Sent"),
    "edited": ("online", "Updated"),
    "retrying": ("nosignal", "Retrying"),
    "failed": ("offline", "Failed"),
    "skipped": ("checking", "Skipped"),
    "expired": ("offline", "Expired (older than 30 min)"),
    "dropped": ("offline", "Dropped (too many waiting)"),
}
EVENT_WORDS = {"summary": "Daily summary", "test": "Test", "camera_offline": "Camera stopped sending video",
               "camera_online": "Camera is back", "monitoring": "Monitoring switched", "health": "YardGuard status",
               "plate": "Number plate", "area": "Area event"}
RETRYABLE = ("failed", "expired", "dropped")


def took_text(ms) -> str:
    """ "took 1 s" / "took less than a second" for a send time in milliseconds."""
    try:
        s = float(ms) / 1000
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    return "took less than a second" if s < 1 else f"took {round(s)} s"


def status_line(row: dict, timing: bool = False) -> tuple[str, str]:
    """(set_status tone, plain words) for a delivery log row; timing adds "took 1 s" to Sent
    (the detail pane; the list keeps plain words)."""
    st = row.get("status") or ""
    tone, text = STATUS_WORDS.get(st, ("checking", st.capitalize() or "Unknown"))
    reason = safe_text(row.get("reason") or "")
    if st in ("sent", "edited") and timing and took_text(row.get("ms")):
        text = f"{text} · {took_text(row.get('ms'))}"
    elif st == "retrying":
        text = f"Retrying ({int(row.get('attempts') or 1)} of {MAX_ATTEMPTS})"
    elif st in ("failed", "skipped") and reason:
        text = f"{text}: {reason}"
    return tone, text


def what_text(row: dict) -> str:
    title = (row.get("title") or "").strip()
    kind = row.get("event_kind") or ""
    if kind in ("summary", "test") or not title:
        return EVENT_WORDS.get(kind, title or "Message")
    return safe_text(title)


def next_morning(now: float | None = None, hour: int = 7) -> float:
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now)
    target = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= dt:
        target += timedelta(days=1)
    return target.timestamp()


def _hub(ctx):
    return getattr(ctx, "integrations", None)


def _menu(parent) -> QMenu:
    m = QMenu(parent)
    m.setObjectName("IntegrationsMenu")
    return m


# --- Cards -----------------------------------------------------------------------------------------

class _BaseCard(QFrame):
    """Fixed-height card (the grid lines up):

        [icon] Title  [chip]                         [switch]
               detail line (describe() or blurb)
               second line ("Last sent 14:02")
               status / problem line       [actions, right]
    """

    def __init__(self, ctx, icon: str, title: str):
        super().__init__()
        self.ctx = ctx
        self.setObjectName("IntegrationCard")
        self.setFixedHeight(CARD_H)
        self.setMinimumWidth(420)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(14)
        self.tile = icon_tile(ctx, icon)
        lay.addWidget(self.tile, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = ElidedLabel(title, "CardTitle")
        self.title.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        top.addWidget(self.title, 0, Qt.AlignVCenter)
        self.chip = chip()
        top.addWidget(self.chip, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.corner = QHBoxLayout()
        self.corner.setSpacing(6)
        top.addLayout(self.corner)
        col.addLayout(top)
        col.addSpacing(2)
        self.line1 = ElidedLabel("", muted_text=True)
        self.line2 = ElidedLabel("", muted_text=True)
        col.addWidget(self.line1)
        col.addWidget(self.line2)
        col.addStretch(1)
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.status = ElidedLabel("")
        self.status.setVisible(False)
        bottom.addWidget(self.status, 1, Qt.AlignVCenter)
        bottom.addStretch(0)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        bottom.addLayout(self.actions)
        col.addLayout(bottom)
        lay.addLayout(col, 1)
        self.text_col = col

    def set_title(self, text: str) -> None:
        self.title.setText(text)

    def show_status(self, tone: str, text: str) -> None:
        self.status.setText(text)
        set_prop(self.status, "status", tone)
        self.status.setVisible(bool(text))


class CatalogCard(_BaseCard):
    """A kind nobody connected yet: what it does, and Set up."""

    setup = Signal(str)

    def __init__(self, ctx, kind: str, info: dict):
        super().__init__(ctx, info.get("icon", "plug"), info.get("title", kind))
        self.kind = kind
        self.set_title(info.get("title", kind))
        self.blurb = muted(info.get("blurb", ""))
        self.blurb.setWordWrap(True)
        # the blurb may wrap to two lines: it replaces the two single-line rows
        self.line1.setVisible(False)
        self.line2.setVisible(False)
        self.text_col.insertWidget(2, self.blurb)
        needs = kind == "mqtt" and not mqtt_available()
        # no chip for "not set up yet": the Set up button says it (a chip there read as a second button)
        set_chip(self.chip, "Needs add-on" if needs else "", "warning" if needs else "")
        self.btn = button("Set up", "primary")
        self.btn.setAccessibleName(f"Set up {info.get('title', kind)}")
        self.btn.clicked.connect(lambda: self.setup.emit(self.kind))
        self.actions.addWidget(self.btn)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.setup.emit(self.kind)
            return
        super().keyPressEvent(event)


class DestinationCard(_BaseCard):
    """A connected destination: switch, Send test (or Fix), More."""

    def __init__(self, page: "ConnectionsView", conf):
        info = catalog().get(conf.kind, {})
        super().__init__(page.ctx, info.get("icon", "plug"), conf.name)
        self.page, self.hub = page, page.hub
        self.dest_id, self.kind = conf.id, conf.kind
        self.title_text = info.get("title", conf.kind)
        self.switch = Switch(self.ctx)
        self.switch.toggled.connect(self._toggled)
        self.test_btn = button("Send test")
        self.test_btn.clicked.connect(self.send_test)
        self.fix_btn = button("Fix", "primary")
        self.fix_btn.clicked.connect(self.fix)
        self.more_btn = button("More", "ghost")
        self.ctx.bind_icon(self.more_btn, "more", "muted")
        self.more_btn.clicked.connect(self._more)
        self.corner.addWidget(self.switch, 0, Qt.AlignVCenter)
        self.actions.addWidget(self.test_btn)
        self.actions.addWidget(self.fix_btn)
        self.actions.addWidget(self.more_btn)
        self._testing = False
        self._test_note = ("", "")
        self.refresh()

    def conf(self):
        return next((d for d in self.page.destinations() if d.id == self.dest_id), None)

    def problem_text(self, conf, channel) -> str:
        for key in secret_keys(self.kind):
            try:
                if conf.secret_unreadable(key):
                    return "The saved password or token cannot be read on this Windows account. Enter it again."
            except Exception:
                pass
        if channel is not None and getattr(channel, "status", "") == "problem":
            return safe_text(getattr(channel, "status_text", "") or "Messages could not be delivered.")
        try:   # the hub's "3 failures in a row" text, until the next success
            text = self.hub.problem_text(conf.id) if hasattr(self.hub, "problem_text") else ""
        except Exception:
            text = ""
        return safe_text(text) if text else ""

    def refresh(self) -> None:
        conf = self.conf()
        if conf is None:
            return
        title = self.page.dest_label(conf)
        self.set_title(title)
        channel = self.hub.channel(conf.id) if self.hub is not None else None
        self.switch.set_checked_quietly(bool(conf.enabled))
        for b, verb in ((self.test_btn, "Send a test message to"), (self.fix_btn, "Fix"), (self.more_btn, "More for"),
                        (self.switch, "Send to")):
            b.setAccessibleName(f"{verb} {title}")
        problem = self.problem_text(conf, channel) if conf.enabled else ""
        status = getattr(channel, "status", "") if channel is not None else ""
        if not conf.enabled:
            set_chip(self.chip, "Paused", "warning")
        elif problem:
            set_chip(self.chip, "Problem", "danger")
        elif status == "connecting":
            set_chip(self.chip, "Connecting", "")
        else:
            set_chip(self.chip, "Connected", "accent")
        set_prop(self, "problem", bool(problem))
        detail = ""
        try:
            desc_ch = channel
            if desc_ch is None:
                # a paused destination has no running channel: describe it from its settings
                cls = channel_class(conf.kind)
                desc_ch = cls(conf, None) if cls is not None else None
            detail = (desc_ch.describe() or "") if desc_ch is not None else ""
        except Exception:
            detail = ""
        self.line1.setText(safe_text(detail) or self.title_text)
        self.line2.setText(self._last_sent(conf.id))
        self.fix_btn.setVisible(bool(problem))
        self.test_btn.setVisible(not problem)
        self.test_btn.setEnabled(conf.enabled and not self._testing)
        if problem:
            self.show_status("offline", problem)
        elif self._test_note[1]:
            self.show_status(*self._test_note)
        else:
            self.show_status("", "")

    def _last_sent(self, dest_id: str) -> str:
        log_ = getattr(self.hub, "log", None)
        if log_ is None:
            return "Nothing sent yet"
        try:
            st = log_.stats(dest_id) or {}
        except Exception:
            return "Nothing sent yet"
        last = st.get("last_ok")
        text = f"Last sent {clock(last)}" if last else "Nothing sent yet"
        if st.get("queued"):
            text += f" · {st['queued']} waiting"
        return text

    # --- actions ---
    def _toggled(self, on: bool) -> None:
        try:
            self.hub.set_enabled(self.dest_id, on)
        except Exception:
            log.exception("Switching a destination failed")
        self._test_note = ("", "")
        self.refresh()

    def send_test(self) -> None:
        conf = self.conf()
        if conf is None or self._testing or not conf.enabled:
            return
        self._testing = True
        self._test_note = ("checking", "Sending a test…")
        self.refresh()

        def done(res) -> None:
            try:
                self._testing = False
                if isinstance(res, Exception) or res is None:
                    self._test_note = ("offline", "The test could not be sent.")
                elif getattr(res, "ok", False):
                    ms = getattr(res, "ms", 0)
                    self._test_note = ("online", f"Test sent{f' · {ms} ms' if ms else ''}. Check that it arrived.")
                else:
                    self._test_note = ("offline", "Test failed: " + safe_text(getattr(res, "error", "") or "unknown"))
                self.refresh()
            except RuntimeError:
                pass   # the card was rebuilt while the test ran

        try:
            self.hub.send_test(self.dest_id, done)
        except Exception:
            log.exception("Send test failed")
            done(None)

    def fix(self) -> None:
        conf = self.conf()
        if conf is not None:
            self.page.open_setup(conf.kind, conf, fix_step(conf.kind, conf, self.hub.channel(conf.id)))

    def _more(self) -> None:
        conf = self.conf()
        if conf is None:
            return
        m = _menu(self)
        m.addAction("Edit connection…", lambda: self.page.open_setup(conf.kind, conf))
        m.addAction("Choose what to send", lambda: self.page.show_routes.emit(conf.id))
        m.addAction("Send test", self.send_test).setEnabled(conf.enabled and not self._testing)
        m.addAction("Rename…", self.rename)
        m.addSeparator()
        m.addAction("Remove…", self.remove)
        m.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def rename(self) -> None:
        conf = self.conf()
        if conf is None:
            return
        name = ask_text(self, "Rename", "Name shown in YardGuard:", conf.name)
        name = " ".join((name or "").split())[:40]
        if not name or name == conf.name:
            return
        set_conf_name(conf, name)
        self.hub.update_destination(conf.id)
        self.page.reload()

    def remove(self) -> None:
        conf = self.conf()
        if conf is None:
            return
        extra = {"telegram": " Your bot stays in Telegram.", "discord": " The webhook stays in Discord.",
                 "mqtt": " Home Assistant removes the YardGuard device."}.get(conf.kind, "")
        if not confirm(self, "Remove", f"Remove {conf.name}? YardGuard stops sending messages there.{extra}",
                       "Remove", danger=True):
            return
        self.hub.remove_destination(conf.id)
        self.page.reload()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.fix() if self.fix_btn.isVisible() else self.page.show_routes.emit(self.dest_id)
            return
        if key == Qt.Key_T and not event.modifiers():
            self.send_test()
            return
        if key == Qt.Key_Space:
            self.switch.toggle()
            return
        super().keyPressEvent(event)


class PhoneAccessCard(_BaseCard):
    """Phone access (the built-in web app): off, on, or a problem."""

    def __init__(self, page: "ConnectionsView"):
        super().__init__(page.ctx, "smartphone", "Open YardGuard on your phone")
        self.page = page
        self.ctx = page.ctx
        self.set_title("Open YardGuard on your phone")
        self.switch = Switch(self.ctx)
        self.switch.setAccessibleName("Phone access")
        self.switch.toggled.connect(self._toggled)
        self.setup_btn = button("Set up", "primary")
        self.setup_btn.setAccessibleName("Set up phone access")
        self.setup_btn.clicked.connect(lambda: self.open_dialog(0))
        self.add_btn = button("Add phone")
        self.add_btn.setAccessibleName("Add a phone")
        self.add_btn.clicked.connect(lambda: self.open_dialog(PhoneAccessDialog.STEP_PAIR))
        self.more_btn = button("More", "ghost")
        self.more_btn.setAccessibleName("More phone access options")
        self.ctx.bind_icon(self.more_btn, "more", "muted")
        self.more_btn.clicked.connect(self._more)
        self.corner.addWidget(self.switch, 0, Qt.AlignVCenter)
        self.actions.addWidget(self.setup_btn)
        self.actions.addWidget(self.add_btn)
        self.actions.addWidget(self.more_btn)
        self.refresh()

    def web(self):
        return getattr(self.ctx, "web", None)

    def refresh(self) -> None:
        web, r = self.web(), remote_settings(self.ctx)
        on = bool(r.get("enabled"))
        error = safe_text(getattr(web, "error", "") or "") if web is not None and on else ""
        self.switch.set_checked_quietly(on)
        self.switch.setVisible(on)
        self.setup_btn.setVisible(not on)
        self.setup_btn.setEnabled(web is not None)
        self.add_btn.setVisible(on and not error)
        self.more_btn.setVisible(on)
        if web is None:
            set_chip(self.chip, "Not available", "")
            self.line1.setText("This version of YardGuard has no phone access.")
            self.line2.setText("")
            self.show_status("", "")
            return
        if not on:
            set_chip(self.chip, "Off", "")
            self.line1.setText("See cameras, incidents and pictures in your phone's browser.")
            self.line2.setText("Off until you turn it on. Nothing listens on the network while it is off.")
            self.show_status("", "")
            return
        mode = remote_mode(r)
        where = {"lan": "On your home Wi-Fi", "away": "At home and away (Tailscale)",
                 "local": "Only on this PC"}.get(mode, "On")
        try:
            devices = list(web.devices() or [])
            connected = int(web.connected_count() or 0)
        except Exception:
            devices, connected = [], 0
        n = len(devices)
        parts = [where, f"{n} phone{'s' if n != 1 else ''} paired" if n else "No phone paired yet"]
        if connected:
            parts.append(f"{connected} connected now")
        self.line1.setText(" · ".join(parts))
        actions = "phones can mark incidents" if r.get("allow_actions") else "read-only"
        self.line2.setText(f"Phones see cameras and incidents ({actions}).")
        if error:
            set_chip(self.chip, "Problem", "danger")
            self.show_status("offline", error)
        elif not getattr(web, "running", False):
            set_chip(self.chip, "Starting", "")
            self.show_status("", "")
        else:
            set_chip(self.chip, "On", "accent")
            self.show_status("", "")
        set_prop(self, "problem", bool(error))

    def _toggled(self, on: bool) -> None:
        web, r = self.web(), remote_settings(self.ctx)
        if not on and not confirm(self, "Turn off phone access",
                                  "Turn off phone access? Paired phones stay paired and work again when you turn it "
                                  "back on.", "Turn off"):
            self.switch.set_checked_quietly(True)
            return
        r["enabled"] = on
        self.ctx.cfg.save()
        if web is not None:
            apply_web(self.ctx)
        self.refresh()

    def open_dialog(self, step: int) -> None:
        dlg = PhoneAccessDialog(self.ctx, _hub(self.ctx), step, self.window())
        dlg.exec()
        self.page.reload()

    def _more(self) -> None:
        m = _menu(self)
        m.addAction("Use it away from home (Tailscale)…", lambda: self.open_dialog(PhoneAccessDialog.STEP_TAILSCALE))
        m.addAction("Change where it works…", lambda: self.open_dialog(PhoneAccessDialog.STEP_WHERE))
        m.addAction("Manage phones…", self.manage)
        m.addSeparator()
        m.addAction("Turn off", lambda: self.switch.setChecked(False))
        m.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def manage(self) -> None:
        ManagePhonesDialog(self.ctx, self.window()).exec()
        self.refresh()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.open_dialog(0 if not remote_settings(self.ctx).get("enabled") else PhoneAccessDialog.STEP_PAIR)
            return
        if event.key() == Qt.Key_Space and self.switch.isVisible():
            self.switch.toggle()
            return
        super().keyPressEvent(event)


# --- Connections tab ---------------------------------------------------------------------------------

class ConnectionsView(QScrollArea):
    show_routes = Signal(str)        # destination id: open "What to send" for it
    changed = Signal()               # destinations were added / removed / renamed

    def __init__(self, ctx, hub, win):
        super().__init__()
        self.ctx, self.hub, self.win = ctx, hub, win
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("Transparent")
        self.col = QVBoxLayout(host)
        self.col.setContentsMargins(0, 0, 8, 8)
        self.col.setSpacing(14)
        self.setWidget(host)
        self.viewport().setAutoFillBackground(False)

        self.empty_banner = Banner(ctx, "Get alerts on your phone",
                                   "YardGuard runs on this PC. To hear about a break-in while you are away, connect "
                                   "your phone.", "info")
        b = button("Connect Telegram", "primary")
        b.clicked.connect(lambda: self.open_setup("telegram"))
        self.empty_banner.actions.addWidget(b)
        self.col.addWidget(self.empty_banner)
        self.mon_banner = Banner(ctx, "Monitoring is off, so nothing will be sent.",
                                 "Alerts are only found and sent while monitoring runs.", "warning")
        self.start_btn = button("Start monitoring")
        self.start_btn.clicked.connect(self._start_monitoring)
        self.mon_banner.actions.addWidget(self.start_btn)
        self.col.addWidget(self.mon_banner)

        self.col.addWidget(label("Phone access", "SectionTitle"))
        self.phone = PhoneAccessCard(self)
        self.col.addWidget(self.phone)
        self.groups_host = QVBoxLayout()
        self.groups_host.setSpacing(14)
        self.col.addLayout(self.groups_host)
        self.col.addSpacing(4)
        foot = muted("YardGuard only sends messages out. Passwords and tokens are encrypted with your Windows account. "
                     "Pictures you choose to send go through the provider's servers (Telegram, ntfy.sh, your mail "
                     "provider).")
        self.col.addWidget(foot)
        self.col.addStretch(1)
        self.cards: dict[str, DestinationCard] = {}
        self.catalog_cards: dict[str, CatalogCard] = {}
        self._grids: list[tuple[QGridLayout, list[QWidget]]] = []
        self._columns = 2
        self.reload()

    def destinations(self) -> list:
        try:
            return list(self.hub.destinations()) if self.hub is not None else []
        except Exception:
            log.exception("Reading destinations failed")
            return []

    def dest_label(self, conf) -> str:
        """The card title: the name, plus who it sends to when two destinations share the name."""
        return dest_labels(self.destinations()).get(conf.id, conf.name)

    # --- building ---
    def reload(self) -> None:
        _clear_layout(self.groups_host)
        self.cards, self.catalog_cards, self._grids = {}, {}, []
        dests = self.destinations()
        cat = catalog()
        for key, title in groups():
            kinds = [k for k, info in cat.items() if info.get("group") == key]
            if not kinds:
                continue
            widgets: list[QWidget] = []
            any_connected = False
            for kind in kinds:
                mine = [d for d in dests if d.kind == kind]
                if mine:
                    any_connected = True
                    for conf in mine:
                        card = DestinationCard(self, conf)
                        self.cards[conf.id] = card
                        widgets.append(card)
                else:
                    card = CatalogCard(self.ctx, kind, cat[kind])
                    card.setup.connect(self.open_setup)
                    self.catalog_cards[kind] = card
                    widgets.append(card)
            head = QHBoxLayout()
            head.addWidget(label(title, "SectionTitle"))
            head.addStretch(1)
            if any_connected:
                add = link_button("+ Add another", "Connect one more phone, bot or app")
                add.clicked.connect(lambda _=False, kinds=kinds, b=add: self._add_menu(kinds, b))
                head.addWidget(add)
            self.groups_host.addLayout(head)
            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(14)
            self.groups_host.addLayout(grid)
            self._grids.append((grid, widgets))
        self._layout_grids(force=True)
        self.refresh_state()
        self.changed.emit()

    def _layout_grids(self, force: bool = False) -> None:
        cols = 1 if self.viewport().width() < NARROW_W else 2
        if cols == self._columns and not force:
            return
        self._columns = cols
        for grid, widgets in self._grids:
            for w in widgets:
                grid.removeWidget(w)
            for i, w in enumerate(widgets):
                grid.addWidget(w, i // cols, i % cols)
            for c in range(2):
                grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_grids()

    def refresh_state(self) -> None:
        """Banners and card states (cheap; called on hub signals and when shown)."""
        has = bool(self.destinations())
        self.empty_banner.setVisible(not has)
        running = bool(getattr(getattr(self.ctx, "monitor", None), "running", False))
        self.mon_banner.setVisible(has and not running)
        for card in self.cards.values():
            card.refresh()
        self.phone.refresh()

    def refresh_card(self, dest_id: str) -> None:
        card = self.cards.get(dest_id)
        if card is not None:
            card.refresh()

    # --- actions ---
    def _add_menu(self, kinds: list[str], anchor: QPushButton) -> None:
        m = _menu(self)
        cat = catalog()
        for kind in kinds:
            m.addAction(cat.get(kind, {}).get("title", kind) + "…", lambda k=kind: self.open_setup(k))
        m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def open_setup(self, kind: str, conf=None, step: int = 0) -> None:
        if self.hub is None:
            return
        dlg = setup_dialog(kind, self.ctx, self.hub, conf, step, self.window())
        if dlg is None:
            return
        added: list[str] = []
        dlg.finished_dest.connect(added.append)
        dlg.exec()
        self.reload()
        if added and conf is None:
            self.show_routes.emit(added[0])

    def _start_monitoring(self) -> None:
        model = integ("model")
        if self.hub is None or model is None:
            return
        self.start_btn.setEnabled(False)
        try:
            self.hub.request(model.Action("set_monitoring", {"on": True}, "This PC"))
        except Exception:
            log.exception("Start monitoring request failed")
        QTimer.singleShot(2500, self._after_start)

    def _after_start(self) -> None:
        try:
            self.start_btn.setEnabled(True)
            self.refresh_state()
        except RuntimeError:
            pass


# --- Activity tab ------------------------------------------------------------------------------------

class _ActivityDelegate(QStyledItemDelegate):
    """Row: time | icon + destination | what | status (coloured)."""

    def __init__(self, view: "ActivityView"):
        super().__init__(view)
        self.view = view
        self._icons: dict = {}

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_H)

    def _icon(self, kind: str):
        key = (kind, id(self.view.ctx.palette))
        if key not in self._icons:
            name = catalog().get(kind, {}).get("icon", "plug")
            self._icons[key] = self.view.ctx.icon(name, "muted", 16).pixmap(16, 16)
        return self._icons[key]

    def paint(self, painter: QPainter, option, index: QModelIndex):
        row = index.data(ROW_ROLE) or {}
        p = self.view.ctx.palette
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        if selected or hover:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p["nav_active_bg"] if selected else p["surface2"]))
            painter.drawRoundedRect(QRectF(r), 6, 6)
        font = QFont(option.font)
        fm = QFontMetrics(font)
        small = QFont(font)
        small.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        sfm = QFontMetrics(small)
        x = r.left() + 12
        top = r.top() + 9
        # time
        painter.setFont(small)
        painter.setPen(QColor(p["muted"]))
        ts = row.get("ts") or 0
        painter.drawText(QRect(x, top, 92, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter, clock(ts) if ts else "")
        # destination
        painter.drawPixmap(x, top + sfm.height() + 3, self._icon(row.get("dest_kind", "")))
        dest_w = self.view.dest_w
        dest = sfm.elidedText(self.view.dest_text(row), Qt.ElideRight, dest_w)
        painter.drawText(QRect(x + 20, top + sfm.height() + 2, dest_w + 2, sfm.height() + 2),
                         Qt.AlignLeft | Qt.AlignVCenter, dest)
        x += max(104, 20 + dest_w + 14)
        width = r.right() - x - 12
        status_w = max(160, min(320, int(width * 0.42)))
        what_w = width - status_w - 16
        bold = QFont(font)
        bold.setWeight(QFont.DemiBold)
        painter.setFont(bold)
        painter.setPen(QColor(p["nav_active_text"] if selected else p["text"]))
        bfm = QFontMetrics(bold)
        painter.drawText(QRect(x, top, what_w, bfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         bfm.elidedText(what_text(row), Qt.ElideRight, what_w))
        painter.setFont(small)
        painter.setPen(QColor(p["muted"]))
        sub = {"incident_new": "New incident", "incident_update": "Update", "incident_status": "Marked",
               }.get(row.get("event_kind", ""), EVENT_WORDS.get(row.get("event_kind", ""), ""))
        painter.drawText(QRect(x, top + bfm.height() + 2, what_w, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         sfm.elidedText(sub, Qt.ElideRight, what_w))
        # status
        tone, text = status_line(row)
        color = {"online": p["success"], "offline": p["danger"], "nosignal": p["warning"]}.get(tone, p["muted"])
        sx = r.right() - 12 - status_w
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        dot_y = r.center().y() - 3
        painter.drawEllipse(QRectF(sx, dot_y, 7, 7))
        painter.setFont(font)
        painter.setPen(QColor(color if tone != "checking" else p["muted"]))
        painter.drawText(QRect(sx + 14, r.top(), status_w - 14, r.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(text, Qt.ElideRight, status_w - 14))
        # separator
        painter.setPen(QColor(p["border"]))
        if not selected:
            painter.drawLine(r.left() + 8, r.bottom(), r.right() - 8, r.bottom())
        painter.restore()


class ActivityView(QWidget):
    def __init__(self, ctx, hub, win):
        super().__init__()
        self.ctx, self.hub, self.win = ctx, hub, win
        self._dirty = True
        self._rows: list[dict] = []
        self._labels: dict[str, str] = {}   # destination id -> name that tells same-named ones apart
        self.dest_w = 70                     # width of the destination column (sized from the labels)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.filter = Segmented([("All", "Every message"), ("Problems", "Failed, expired and retrying messages")])
        self.filter.setAccessibleName("Show")
        self.filter.changed.connect(lambda *_: self.refresh())
        bar.addWidget(self.filter)
        self.dest = QComboBox()
        self.dest.setAccessibleName("Destination")
        self.dest.setMinimumWidth(200)
        self.dest.currentIndexChanged.connect(lambda *_: self.refresh())
        bar.addWidget(self.dest)
        bar.addStretch(1)
        self.count_lbl = muted("", wrap=False)
        bar.addWidget(self.count_lbl)
        self.clear_btn = button("Clear log", "ghost", "Delete the activity history (not the alerts)")
        ctx.bind_icon(self.clear_btn, "trash", "muted")
        self.clear_btn.clicked.connect(self.clear)
        bar.addWidget(self.clear_btn)
        lay.addLayout(bar)

        self.body = QStackedWidget()
        self.empty = EmptyState(ctx)
        self.empty.configure("send", "Nothing sent yet", "Connect your phone in Connections. Every message YardGuard "
                             "sends, or decides not to send, shows up here.", "Go to Connections", self._go_connections)
        self.body.addWidget(self.empty)
        content = QWidget()
        self.split = QHBoxLayout(content)
        self.split.setContentsMargins(0, 0, 0, 0)
        self.split.setSpacing(14)
        self.list = QListWidget()
        self.list.setObjectName("ActivityList")
        self.list.setItemDelegate(_ActivityDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setAccessibleName("Activity")
        self.list.currentItemChanged.connect(lambda *_: self._show_detail())
        self.detail = self._build_detail()
        self._detail_below = False
        self._content = content
        self.split.addWidget(self.list, 1)
        self.split.addWidget(self.detail)
        self.body.addWidget(content)
        lay.addWidget(self.body, 1)
        self.go_connections = None

    def _build_detail(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ActivityDetail")
        frame.setFixedWidth(330)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        self.d_title = label("", "CardTitle", wrap=True)
        lay.addWidget(self.d_title)
        self.d_status = label("", wrap=True)
        lay.addWidget(self.d_status)
        self.d_rows = QGridLayout()
        self.d_rows.setHorizontalSpacing(12)
        self.d_rows.setVerticalSpacing(6)
        self.d_vals = {}
        for i, (key, text) in enumerate((("why", "Why?"), ("dest", "Sent to"), ("when", "When"),
                                         ("attempts", "Attempts"), ("updated", "Last change"))):
            k = label(text, "DetailKey")
            k.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            v = QLabel()
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.d_rows.addWidget(k, i, 0)
            self.d_rows.addWidget(v, i, 1)
            self.d_vals[key] = v
        self.d_rows.setColumnStretch(1, 1)
        lay.addLayout(self.d_rows)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.retry_btn = button("Retry", "primary")
        self.retry_btn.clicked.connect(self.retry)
        self.open_btn = button("Open incident")
        self.open_btn.clicked.connect(self.open_incident)
        row.addWidget(self.retry_btn)
        row.addWidget(self.open_btn)
        row.addStretch(1)
        lay.addLayout(row)
        # the hint sits at the top, centred, where it is seen (the newest row is selected anyway)
        self.d_none = muted("Select a row to see why it was sent, held or skipped.")
        self.d_none.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.d_none.setContentsMargins(0, 24, 0, 0)
        lay.insertWidget(0, self.d_none)
        lay.addStretch(1)
        return frame

    def dest_text(self, row: dict) -> str:
        return self._labels.get(row.get("dest_id") or "") or safe_text(row.get("dest_name") or "")

    def _size_dest_column(self) -> None:
        font = QFont(self.list.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        fm = QFontMetrics(font)
        texts = {self.dest_text(r) for r in self._rows[:300]}
        widest = max((fm.horizontalAdvance(t) for t in texts if t), default=0)
        self.dest_w = max(70, min(160, widest + 2))

    def _go_connections(self) -> None:
        if self.go_connections:
            self.go_connections()

    # --- data ---
    def mark_dirty(self) -> None:
        self._dirty = True
        if self.isVisible():
            QTimer.singleShot(0, self.refresh)

    def _fill_dests(self) -> None:
        current = self.dest.currentData()
        self.dest.blockSignals(True)
        self.dest.clear()
        self.dest.addItem("All destinations", "")
        try:
            dests = list(self.hub.destinations()) if self.hub is not None else []
        except Exception:
            dests = []
        self._labels = dest_labels(dests)
        for d in dests:
            self.dest.addItem(self._labels.get(d.id, d.name), d.id)
        self.dest.setCurrentIndex(max(0, self.dest.findData(current)))
        self.dest.blockSignals(False)

    def refresh(self) -> None:
        if not self.isVisible():
            self._dirty = True
            return
        self._dirty = False
        self._fill_dests()
        log_ = getattr(self.hub, "log", None)
        rows: list[dict] = []
        if log_ is not None:
            try:
                rows = list(log_.list(limit=300, problems_only=self.filter.current() == 1,
                                      dest_id=self.dest.currentData() or None) or [])
            except Exception:
                log.exception("Reading the activity log failed")
        keep = self._current_id()
        self._rows = rows
        self._size_dest_column()
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        select = None
        for row in rows:
            item = QListWidgetItem()
            item.setData(ID_ROLE, row.get("id"))
            item.setData(ROW_ROLE, row)
            tone, text = status_line(row)
            item.setData(Qt.AccessibleTextRole, f"{what_text(row)}, {self.dest_text(row)}, {text}")
            self.list.addItem(item)
            if row.get("id") == keep:
                select = item
        if select is None and self.list.count():
            select = self.list.item(0)        # the newest row, so the detail pane is never empty
        self.list.setUpdatesEnabled(True)
        any_rows = bool(rows) or self.filter.current() == 1 or bool(self.dest.currentData())
        self.body.setCurrentIndex(1 if any_rows else 0)
        self.clear_btn.setEnabled(bool(rows) or any_rows)
        self.count_lbl.setText(f"{len(rows)} shown" if rows else "")
        if select is not None:
            self.list.setCurrentItem(select)
        self._show_detail()

    def _current_id(self) -> str:
        item = self.list.currentItem()
        return item.data(ID_ROLE) if item is not None else ""

    def _current_row(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(ROW_ROLE) if item is not None else None

    def _show_detail(self) -> None:
        row = self._current_row()
        has = row is not None
        for w in (self.d_title, self.d_status, self.retry_btn, self.open_btn):
            w.setVisible(has)
        for i in range(self.d_rows.count()):
            self.d_rows.itemAt(i).widget().setVisible(has)
        self.d_none.setVisible(not has)
        if not has:
            return
        self.d_title.setText(what_text(row))
        tone, text = status_line({**row, "reason": ""}, timing=True)   # the reason has its own "Why?" row
        set_status(self.d_status, tone, text)
        reason = safe_text(row.get("reason") or "")
        self.d_vals["why"].setText(reason or {"sent": "It matched what you chose to receive.",
                                              "edited": "The incident grew, so the message was updated."}.get(
            row.get("status"), "-"))
        self.d_vals["dest"].setText(self.dest_text(row) or "-")
        self.d_vals["when"].setText(clock(row["ts"]) if row.get("ts") else "-")
        attempts = int(row.get("attempts") or 0)
        self.d_vals["attempts"].setText(f"{attempts} of {MAX_ATTEMPTS}" if attempts else "-")
        self.d_vals["updated"].setText(clock(row["updated"]) if row.get("updated") else "-")
        self.retry_btn.setVisible(row.get("status") in RETRYABLE)
        self.open_btn.setVisible(bool(row.get("incident_id")) and (hasattr(self.hub, "open_incident")
                                                                   or hasattr(self.win, "open_incident")))

    # --- actions ---
    def retry(self) -> None:
        row = self._current_row()
        if row is None or self.hub is None:
            return
        try:
            ok = self.hub.retry(row.get("id"))
        except Exception:
            log.exception("Retry failed")
            ok = False
        notify = getattr(self.win, "notify", None)
        if notify:
            notify("Sending it again." if ok else "This message cannot be sent again.", error=not ok)
        self._dirty = True
        self.refresh()

    def open_incident(self) -> None:
        row = self._current_row()
        if not row or not row.get("incident_id"):
            return
        try:
            if hasattr(self.hub, "open_incident") and self.hub.open_incident(row["incident_id"]):
                return
        except Exception:
            log.exception("Opening the incident failed")
        if hasattr(self.win, "open_incident"):
            self.win.open_incident(row["incident_id"])

    def clear(self) -> None:
        log_ = getattr(self.hub, "log", None)
        if log_ is None:
            return
        if not confirm(self, "Clear log", "Delete the activity history? Alerts and incidents are not touched.",
                       "Clear log", danger=True):
            return
        try:
            log_.clear()
        except Exception:
            log.exception("Clearing the activity log failed")
        self.refresh()

    # --- layout ---
    def resizeEvent(self, event):
        super().resizeEvent(event)
        below = self.width() < DETAIL_BELOW_W - 232 - 64     # window width minus sidebar and page margins
        if below != self._detail_below:
            self._detail_below = below
            self.split.setDirection(QBoxLayout.TopToBottom if below else QBoxLayout.LeftToRight)
            self.detail.setFixedWidth(16777215 if below else 330)
            self.detail.setMaximumHeight(230 if below else 16777215)
            self.detail.setMinimumWidth(0)

    def showEvent(self, event):
        super().showEvent(event)
        if self._dirty:
            QTimer.singleShot(0, self.refresh)


# --- Page ------------------------------------------------------------------------------------------------

class IntegrationsPage(QWidget):
    def __init__(self, ctx, win):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.hub = _hub(ctx)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(20)
        self.header = PageHeader("Integrations", "Get alerts on your phone, by email or in your smart home. Nothing is "
                                                 "sent until you connect something.")
        self.tabs = Segmented(list(TABS))
        self.tabs.setAccessibleName("Integrations view")
        self.tabs.changed.connect(self.select_tab)
        self.header.actions.addWidget(self.tabs, 0, Qt.AlignTop)
        self.pause_btn = button("Pause messages", "ghost", "Stop messages for a while (critical alerts still come)")
        ctx.bind_icon(self.pause_btn, "bell-off", "muted")
        self.pause_btn.clicked.connect(self._pause_menu)
        self.header.actions.addWidget(self.pause_btn, 0, Qt.AlignTop)
        lay.addWidget(self.header)

        self.snooze_bar = QFrame()
        self.snooze_bar.setObjectName("SnoozeBar")
        sb = QHBoxLayout(self.snooze_bar)
        sb.setContentsMargins(12, 6, 8, 6)
        sb.setSpacing(8)
        self.snooze_icon = IconTile(18, 16)
        self.snooze_icon.setObjectName("SnoozeIcon")
        ctx.bind_icon(self.snooze_icon, "bell-off", "warning")
        sb.addWidget(self.snooze_icon)
        self.snooze_lbl = QLabel()
        sb.addWidget(self.snooze_lbl)
        self.resume_btn = link_button("Resume", "Send messages again now")
        self.resume_btn.clicked.connect(lambda: self.snooze(0))
        sb.addWidget(self.resume_btn)
        sb.addStretch(1)
        self.snooze_bar.setToolTip("Critical alerts still come through while messages are paused.")
        self.snooze_bar.setVisible(False)
        lay.addWidget(self.snooze_bar)

        self.stack = QStackedWidget()
        if self.hub is None:
            empty = EmptyState(ctx)
            empty.configure("plug", "Integrations are not available", "This part of YardGuard did not start. "
                            "Restart YardGuard; if it stays like this, reinstall it.", "Open Settings",
                            lambda: hasattr(win, "go_page") and win.go_page(getattr(win, "settings", self)))
            self.stack.addWidget(empty)
            self.connections = self.routes = self.activity = None
            self.tabs.setEnabled(False)
            self.pause_btn.setEnabled(False)
            lay.addWidget(self.stack, 1)
            return
        self.connections = ConnectionsView(ctx, self.hub, win)
        self.routes = RoutesEditor(ctx, self.hub, win)
        self.activity = ActivityView(ctx, self.hub, win)
        self.activity.go_connections = lambda: self.select_tab(0)
        self.routes.go_connections.connect(lambda: self.select_tab(0))
        for w in (self.connections, self.routes, self.activity):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        self.connections.show_routes.connect(self.show_routes)
        self.connections.changed.connect(self._dests_changed)

        hub = self.hub
        for sig, slot in (("deliveries_changed", self._deliveries_changed),
                          ("destination_status", self._dest_status),
                          ("snooze_changed", self._snooze_changed)):   # bound methods: Qt drops them with us
            s = getattr(hub, sig, None)
            if s is not None:
                s.connect(slot)
        if hasattr(hub, "add_listener"):
            listener = self._hub_event
            hub.add_listener(listener)
            if hasattr(hub, "remove_listener"):   # a closed page must not stay in the hub's feed
                self.destroyed.connect(lambda *_, h=hub, fn=listener: h.remove_listener(fn))
        sig = web_signals(ctx)
        if sig is not None:
            sig.changed.connect(self._web_changed)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._refresh_visible)
        self._clock = QTimer(self)
        self._clock.setInterval(15_000)     # snooze end, monitoring banner, "last sent" lines
        self._clock.timeout.connect(self._tick)

        for key, step in (("Ctrl+Tab", 1), ("Ctrl+Shift+Tab", -1), ("Ctrl+Backtab", -1)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda step=step: self.select_tab((self.stack.currentIndex() + step) % 3))
        try:
            tab = int(ctx.ui.value("integrations/tab", 0) or 0)
        except (TypeError, ValueError):
            tab = 0
        self.select_tab(tab if 0 <= tab < 3 else 0, remember=False)
        self._update_snooze()

    # --- tabs ---
    def select_tab(self, index: int, remember: bool = True) -> None:
        if self.connections is None:
            return
        if self.stack.currentWidget() is self.routes and index != 1:
            self.routes.flush()
        self.stack.setCurrentIndex(index)
        self.tabs.set_current(index)
        if remember:
            self.ctx.ui.setValue("integrations/tab", index)
        if index == 2:
            self.activity.mark_dirty()

    def show_routes(self, dest_id: str) -> None:
        self.routes.reload()
        self.routes.select(dest_id)
        self.select_tab(1)

    def _dests_changed(self) -> None:
        if self.routes is not None:
            self.routes.reload()
        if self.activity is not None:
            self.activity.mark_dirty()

    # --- hub feed ---
    def _deliveries_changed(self, *_) -> None:
        self.activity.mark_dirty()
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _dest_status(self, dest_id: str) -> None:
        if self.connections is not None:
            self.connections.refresh_card(dest_id)

    def _hub_event(self, kind: str, payload) -> None:
        if kind not in ("monitoring", "config"):
            return
        try:
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
            if kind == "config":
                self._update_snooze()
        except RuntimeError:
            pass   # the page is being destroyed

    def _web_changed(self) -> None:
        if self.connections is not None:
            self.connections.phone.refresh()

    def _refresh_visible(self) -> None:
        if self.isVisible() and self.connections is not None:
            self.connections.refresh_state()

    def _tick(self) -> None:
        self._update_snooze()
        self._refresh_visible()

    # --- snooze ---
    def snoozed_until(self) -> float:
        try:
            return float(self.hub.snoozed_until() or 0) if self.hub is not None else 0.0
        except Exception:
            return 0.0

    def snooze(self, until: float) -> None:
        try:
            self.hub.snooze(until)
        except Exception:
            log.exception("Snooze failed")
        self._update_snooze()
        notify = getattr(self.win, "notify", None)
        if notify:
            notify(f"Messages paused until {clock(until)}. Critical alerts still come through." if until
                   else "Messages are on again.")

    def _snooze_changed(self, *_) -> None:
        self._update_snooze()

    def _update_snooze(self) -> None:
        until = self.snoozed_until()
        on = until > time.time()
        self.snooze_bar.setVisible(on)
        if on:
            self.snooze_lbl.setText(f"Messages paused until {clock(until)}")
            self.snooze_lbl.setToolTip("Critical alerts still come through.")
        self.pause_btn.setText(" Paused" if on else " Pause messages")

    def _pause_menu(self) -> None:
        m = _menu(self)
        m.addAction("For 1 hour", lambda: self.snooze(time.time() + 3600))
        morning = next_morning()
        when = "tomorrow 07:00" if datetime.fromtimestamp(morning).date() != datetime.now().date() else "07:00"
        m.addAction(f"Until {when}", lambda: self.snooze(morning))
        m.addSeparator()
        resume = m.addAction("Resume", lambda: self.snooze(0))
        resume.setEnabled(self.snoozed_until() > time.time())
        m.exec(self.pause_btn.mapToGlobal(self.pause_btn.rect().bottomLeft()))

    # --- visibility ---
    def showEvent(self, event):
        super().showEvent(event)
        if self.connections is not None:
            self._update_snooze()
            self.connections.refresh_state()
            self._clock.start()

    def hideEvent(self, event):
        if self.routes is not None:
            self.routes.flush()
        self._clock.stop()
        super().hideEvent(event)


def _clear_layout(lay) -> None:
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())
            item.layout().deleteLater()
