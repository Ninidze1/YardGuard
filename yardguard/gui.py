"""YardGuard desktop app: dashboard, alerts, live view, sites & cameras, settings."""
from __future__ import annotations

import copy
import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import cv2
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSettings, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .alerts_ui import EXTRA_QSS as ALERTS_QSS, MonitorPill, ToastManager
from .areas_ui import EXTRA_QSS as AREAS_QSS, AreasPage, LiveAreas, areas_button
from . import plates
from .brain import Brain
from .config import APP_VERSION, BRANDS, MODELS, ROLES, ROOT, STREAMS, THEMES, Camera, load_config
from .engine import AlertStore, Monitor
from .incidents_ui import EXTRA_QSS as INCIDENTS_QSS, IncidentsPage, IncidentStrip
from .integrations import IntegrationHub
from .integrations_ui import EXTRA_QSS as INTEGRATIONS_QSS, IntegrationsPage  # also registers its icons
from .overlay import LOW_SCORE, STYLES as OVERLAY_STYLES, DisplayTracker, draw_tracks, track_counts
from .qt_util import run_async, set_prop, to_qimage
from .settings_ui import EXTRA_QSS as SETTINGS_QSS, BrainCard, ClipsSection, MonitoringCard, form_row
from .theme import PALETTES, app_icon, build_qss, logo_pixmap, qt_palette, resolve_theme, svg_icon, write_assets
from .video import (
    CLASSES, Detector, delete_model, ensure_model, get_download, grab_frame, model_ready, open_capture,
    partial_path, test_connection,
)
from .widgets import (
    EmptyState, ImageView, PageHeader, button, card, fetch_thumb, label, muted, password_field,
    set_status, shortcut, spin_box,
)
from .wizard import ScanDialog, SetupWizard, brand_combo, form_layout

log = logging.getLogger("yardguard")
CACHE = ROOT / ".cache"


class AlertBridge(QObject):
    """Carries Monitor callbacks (worker threads) into the GUI thread as queued signals."""

    alert_added = Signal(object)
    alert_updated = Signal(object)
    incident_added = Signal(object)     # an alert started a new incident
    incident_updated = Signal(object)   # an incident got another alert or a new verdict
    status_changed = Signal(object)
    area_event_added = Signal(object)   # an AreaEvent was logged


class AppContext(QObject):
    """Shared state: config, AI models, monitoring, alerts, theme and themed icons."""

    config_changed = Signal()

    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.cfg = load_config()
        self.detector = Detector()  # live view fallback when the fast detector cannot load
        self.palette = PALETTES["light"]
        self.ui = QSettings(str(CACHE / "ui.ini"), QSettings.IniFormat)  # kept next to the app, not the registry
        self._bindings: list = []
        self.brain = Brain()
        self.alert_bridge = AlertBridge()
        self.alert_store: AlertStore | None = None
        self.monitor: Monitor | None = None
        self._live_det = None
        self._live_key: tuple | None = None
        self._live_lock = threading.Lock()
        self.make_monitor()
        self.integrations: IntegrationHub | None = None   # messages to Telegram, email, Home Assistant ...
        self.web = None                                   # phone access server (off until the user turns it on)
        self.make_integrations()
        app.styleHints().colorSchemeChanged.connect(
            lambda *_: self.cfg.settings.theme == "system" and self.apply_theme())

    def make_monitor(self) -> None:
        """(Re)create the alert store and monitor for the current events folder. Monitor must be stopped."""
        if self.alert_store is not None:
            self.alert_store.close()
        gap = self.cfg.settings.incident_gap_s
        try:
            self.alert_store = AlertStore(self.cfg.settings.events_dir, incident_gap_s=gap)
        except Exception as exc:  # folder on a missing drive, or a broken database
            log.error("Alert store at %s failed (%s): using the app folder", self.cfg.settings.events_dir, exc)
            self.alert_store = AlertStore(str(ROOT / "events"), incident_gap_s=gap)
        b, store = self.alert_bridge, self.alert_store

        def added(alert) -> None:  # monitor threads: the signals queue everything to the GUI thread
            b.alert_added.emit(alert)
            inc = store.get_incident(alert.incident_id) if alert.incident_id else None
            if inc is not None:
                (b.incident_added if inc.alert_count == 1 else b.incident_updated).emit(inc)

        def updated(alert) -> None:
            b.alert_updated.emit(alert)
            inc = store.get_incident(alert.incident_id) if alert.incident_id else None
            if inc is not None:
                b.incident_updated.emit(inc)

        self.monitor = Monitor(self.cfg, self.alert_store, brain=self.brain, on_alert=added,
                               on_alert_update=updated, on_status=b.status_changed.emit,
                               on_area_event=b.area_event_added.emit)

    def make_integrations(self) -> None:
        """The integrations hub and the phone access server. Both read alert_store and monitor at use
        time, so make_monitor() may replace those later. Nothing is sent or listens until start()
        and only for what the user set up. A failure here must never stop the app itself."""
        try:
            self.integrations = IntegrationHub(self)
        except Exception:
            log.exception("Integrations could not start")
            self.integrations = None
            return
        try:
            from .webremote import WebRemote
            self.web = WebRemote(self, self.integrations)
        except Exception:
            log.exception("Phone access could not start")
            self.web = None

    def start_integrations(self) -> None:
        if self.integrations is not None:
            try:
                self.integrations.start()
            except Exception:
                log.exception("Integrations could not start")
        if self.web is not None:
            try:
                self.web.start()
            except Exception:
                log.exception("Phone access could not start")

    def stop_integrations(self) -> None:
        """Phone access first (its live streams read the hub's frames), then the hub (at most ~4 s)."""
        if self.web is not None:
            try:
                self.web.stop()
            except Exception:
                log.exception("Could not stop phone access cleanly")
        if self.integrations is not None:
            try:
                self.integrations.shutdown()
            except Exception:
                log.exception("Could not stop integrations cleanly")

    def live_detector(self):
        """Fast batched YOLO for the live view: the monitor's while it runs (one copy on the GPU),
        else our own, loaded once. Raises if the fast detector is not available."""
        s = self.cfg.settings
        det = getattr(self.monitor, "_detector", None)
        if det is not None and getattr(det, "model_name", s.model) == s.model:
            with self._live_lock:
                self._live_det = None
            return det
        with self._live_lock:
            key = (s.model, s.detect_size)
            if self._live_det is None or self._live_key != key:
                from .accel import load_fast_detector
                self._live_det = None
                self._live_det = load_fast_detector(s.model, s.detect_size)
                self._live_key = key
            return self._live_det

    def live_detector_loaded(self) -> bool:
        return self._live_det is not None or getattr(self.monitor, "_detector", None) is not None

    def apply_theme(self) -> None:
        self.palette = PALETTES[resolve_theme(self.cfg.settings.theme)]
        self.app.setPalette(qt_palette(self.palette))
        assets = write_assets(self.palette, CACHE / "ui")
        self.app.setStyleSheet(build_qss(self.palette, assets, ALERTS_QSS + INCIDENTS_QSS + AREAS_QSS
                                         + INTEGRATIONS_QSS) + SETTINGS_QSS)
        alive = []
        for widget, name, role in self._bindings:
            try:
                widget.setIcon(self.icon(name, role))
                alive.append((widget, name, role))
            except RuntimeError:
                pass  # widget was deleted
        self._bindings = alive

    def icon(self, name: str, role: str = "text", size: int = 18):
        p = self.palette
        if role == "nav":
            return svg_icon(name, p["muted"], active=p["nav_active_text"], size=size)
        return svg_icon(name, p[role], size=size)

    def bind_icon(self, widget, name: str, role: str = "text") -> None:
        if isinstance(widget, QPushButton) and widget.text() and not widget.text().startswith(" "):
            widget.setText(" " + widget.text())  # Qt draws no gap between icon and text
        widget.setIcon(self.icon(name, role))
        self._bindings.append((widget, name, role))

    def save(self) -> None:
        self.cfg.save()
        self.config_changed.emit()

    def save_snapshot(self, frame, site, cam) -> Path:
        folder = Path(self.cfg.settings.events_dir) / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w-]+", "_", f"{site.name}_{cam.label}").strip("_")
        path = folder / f"{safe}_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        ok, data = cv2.imencode(".jpg", frame)
        if not ok:
            raise OSError("Could not encode the picture")
        path.write_bytes(data.tobytes())  # works with non-English folder names, unlike cv2.imwrite
        return path


# --- Dashboard ----------------------------------------------------------------

class ElidedLabel(QLabel):
    """One line that never wraps: long text ends in "…" and the full text is the tooltip, so a
    changing status can never change the card's height."""

    def __init__(self, text: str = "", name: str = ""):
        super().__init__()
        if name:
            self.setObjectName(name)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text if text else "")
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(10, self.width()))
        super().setText(shown)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self):
        return QSize(10, self.fontMetrics().height())


class CameraThumb(ImageView):
    """The camera picture with the card's status pill, channel chip and name painted on top."""

    def __init__(self, ctx: AppContext, cam: Camera):
        super().__init__("Loading preview…")
        self.ctx, self.cam = ctx, cam
        self.pill_text, self.pill_tone = "Connecting…", "checking"
        self._color = QColor(TONE_FALLBACK["checking"])
        self._from = QColor(self._color)
        self._to = QColor(self._color)
        self._mix = 1.0
        self._pulse = 0.0
        self._fade = QTimer(self, interval=16)   # status colour cross-fade (~200 ms)
        self._fade.timeout.connect(self._step_fade)
        self._breath = QTimer(self, interval=60)  # the "watching" dot breathes gently
        self._breath.timeout.connect(self._step_breath)

    def _rescale(self):
        """Fill the whole tile (cropping the edges of non-16:9 cameras) instead of letterboxing."""
        if self._img is None or self.width() < 2:
            return
        dpr = self.devicePixelRatioF()
        w, h = int(self.width() * dpr), int(self.height() * dpr)
        pm = QPixmap.fromImage(self._img).scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        pm = pm.copy((pm.width() - w) // 2, (pm.height() - h) // 2, w, h)
        pm.setDevicePixelRatio(dpr)
        self.setPixmap(pm)

    def set_pill(self, text: str, tone: str) -> None:
        if (text, tone) == (self.pill_text, self.pill_tone):
            return
        self.pill_text, self.pill_tone = text, tone
        self._from, self._to, self._mix = QColor(self._color), self._tone_color(tone), 0.0
        self._fade.start()
        if tone == "watching":
            self._breath.start()
        else:
            self._breath.stop()
            self._pulse = 0.0
        self.update()

    def _tone_color(self, tone: str) -> QColor:
        p = self.ctx.palette
        key = {"watching": "success", "online": "success", "quiet": "success", "offline": "danger",
               "nosignal": "warning", "checking": "muted", "off": "muted"}.get(tone, "muted")
        return QColor(p.get(key, TONE_FALLBACK.get(tone, "#9aa3b2")))

    def _step_fade(self) -> None:
        self._mix = min(1.0, self._mix + 0.08)
        f, t, m = self._from, self._to, self._mix
        self._color = QColor(int(f.red() + (t.red() - f.red()) * m), int(f.green() + (t.green() - f.green()) * m),
                             int(f.blue() + (t.blue() - f.blue()) * m))
        if self._mix >= 1.0:
            self._fade.stop()
        self.update()

    def _step_breath(self) -> None:
        self._pulse = (self._pulse + 0.05) % 2.0
        self.update(self._pill_rect().adjusted(-4, -4, 4, 4).toRect())

    def _pill_rect(self) -> QRectF:
        fm = QFontMetrics(self._pill_font())
        w = fm.horizontalAdvance(self.pill_text) + 30
        return QRectF(10, 10, w, 24)

    def _pill_font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        f.setWeight(QFont.DemiBold)
        return f

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        # the picture fills the tile, rounded at the top like the card (the bottom meets the bar)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, w, h + 12), 11, 11)
        p.setClipPath(clip)
        p.fillRect(self.rect(), QColor(self.ctx.palette.get("video_bg", "#0b0d10")))
        pm = self.pixmap()
        if pm is not None and not pm.isNull():
            p.drawPixmap(0, 0, pm)
            if self.pill_tone in ("offline", "nosignal"):  # an old picture must not look live
                p.fillRect(self.rect(), QColor(0, 0, 0, 150))
        elif self.text():
            p.setPen(QColor(154, 163, 178))
            p.drawText(self.rect().adjusted(0, -20, 0, -20), Qt.AlignCenter, self.text())
        # bottom gradient so the name is readable on any picture
        grad = QLinearGradient(0, h - 64, 0, h)
        grad.setColorAt(0, QColor(0, 0, 0, 0))
        grad.setColorAt(1, QColor(0, 0, 0, 170))
        p.fillRect(QRectF(0, h - 64, w, 64), grad)
        p.setClipping(False)
        name_font = QFont(self.font())
        name_font.setPointSizeF(10.5)
        name_font.setWeight(QFont.DemiBold)
        p.setFont(name_font)
        p.setPen(QColor(255, 255, 255))
        fm = QFontMetrics(name_font)
        p.drawText(QPointF(14, h - 30), fm.elidedText(self.cam.label, Qt.ElideRight, w - 28))
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        p.setFont(small)
        p.setPen(QColor(255, 255, 255, 190))
        p.drawText(QPointF(14, h - 12), f"{self.cam.role} · channel {self.cam.channel}")
        # status pill, top left
        r = self._pill_rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 17, 21, 190))
        p.drawRoundedRect(r, 12, 12)
        dot = QPointF(r.left() + 13, r.center().y())
        if self.pill_tone == "watching":
            glow = QColor(self._color)
            glow.setAlpha(int(90 * (1 - abs(1 - self._pulse))))
            p.setBrush(glow)
            p.drawEllipse(dot, 7, 7)
        p.setBrush(self._color)
        p.drawEllipse(dot, 4, 4)
        p.setFont(self._pill_font())
        p.setPen(QColor(255, 255, 255))
        p.drawText(r.adjusted(22, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, self.pill_text)
        p.end()


class CameraCard(QFrame):
    """Dashboard tile: the picture with name and one merged status (connection + monitoring),
    one fixed-height detail line and the actions. Nothing in it can change its height."""

    live_requested = Signal(int)
    snapshot_requested = Signal(int)

    def __init__(self, ctx: AppContext, cam: Camera):
        super().__init__()
        self.setObjectName("CameraCard")
        self.ctx, self.cam = ctx, cam
        self.conn = "checking"            # preview connection: checking | online | offline | nosignal
        self.mon_state: dict | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.thumb = CameraThumb(ctx, cam)
        self.thumb.setObjectName("CardThumb")
        self.thumb.setCursor(Qt.PointingHandCursor)
        self.thumb.setToolTip(f"Open {cam.label} in Live view")
        self.thumb.clicked.connect(lambda: self.live_requested.emit(cam.channel))
        lay.addWidget(self.thumb)

        bar = QWidget()
        bar.setFixedHeight(52)
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 0, 10, 0)
        row.setSpacing(6)
        self.detail = ElidedLabel("", "CardDetail")
        self.detail.setProperty("muted", True)
        snap = button("", "ghost", "Save a snapshot")
        ctx.bind_icon(snap, "camera", "muted")
        snap.setAccessibleName(f"Save snapshot of {cam.label}")
        snap.clicked.connect(lambda: self.snapshot_requested.emit(cam.channel))
        live = button("Live", "primary", f"Open {cam.label} in Live view")
        ctx.bind_icon(live, "video", "on_primary")
        live.clicked.connect(lambda: self.live_requested.emit(cam.channel))
        row.addWidget(self.detail, 1)
        row.addWidget(snap)
        row.addWidget(live)
        lay.addWidget(bar)
        self._render()

    # Connection of the preview (from the thumbnail grab)
    def set_connection(self, status: str) -> None:
        self.conn = status
        self._render()

    # Live monitoring state (from Monitor.camera_status)
    def set_monitor(self, st: dict | None) -> None:
        self.mon_state = st or None
        self._render()

    def _render(self) -> None:
        text, tone, detail = card_status(self.conn, self.mon_state)
        self.thumb.set_pill(text, tone)
        self.detail.setText(detail)


TONE_FALLBACK = {"watching": "#16a34a", "online": "#16a34a", "quiet": "#16a34a", "offline": "#dc2626",
                 "nosignal": "#d97706", "checking": "#9aa3b2", "off": "#9aa3b2"}


def card_status(conn: str, mon: dict | None) -> tuple[str, str, str]:
    """One status for a camera tile from its preview connection and its monitoring state:
    (pill text, tone, detail line). Problems win over everything else."""
    state = (mon or {}).get("state")
    seen = object_text((mon or {}).get("objects") or {})
    if conn == "offline" or state == "offline":
        err = (mon or {}).get("error")
        return "Offline", "offline", str(err) if err else "No video from the camera"
    if conn == "nosignal":
        return "No signal", "nosignal", "The recorder sends a black picture on this channel"
    if state == "watching":
        fps = (mon or {}).get("fps")
        parts = [seen or "Nothing in view"] + ([f"{fps:.0f} checks/s"] if fps else [])
        return "Watching", "watching", "  ·  ".join(parts)
    if state == "idle":
        return "Quiet", "quiet", (seen + "  ·  " if seen else "") + "Nothing moving, checking now and then"
    if state == "starting":
        return "Starting…", "checking", "Monitoring is starting"
    if conn == "checking":
        return "Connecting…", "checking", "Loading the picture"
    return "Online", "online", "Monitoring is off"


def object_text(counts: dict[str, int]) -> str:
    """{'car': 3, 'person': 1} -> '1 person, 3 cars' (people first)."""
    order = sorted(counts.items(), key=lambda kv: (kv[0] != "person", kv[0]))
    plural = lambda k, n: ("people" if k == "person" else f"{k}es" if k.endswith(("s", "x", "ch", "sh"))
                           else f"{k}s") if n != 1 else k
    return ", ".join(f"{n} {plural(k, n)}" for k, n in order if n)


class DashboardPage(QWidget):
    def __init__(self, ctx: AppContext, win: "MainWindow"):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.cards: dict[int, CameraCard] = {}
        self.status: dict[int, str] = {}
        self.stale = False   # the config changed while hidden: reload when shown
        self._cols = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(20)
        self.header = PageHeader("Dashboard")
        self.refresh_btn = button("Refresh", tooltip="Reload all camera previews (F5)")
        ctx.bind_icon(self.refresh_btn, "refresh")
        self.refresh_btn.clicked.connect(self.refresh_thumbs)
        self.header.actions.addWidget(self.refresh_btn)
        lay.addWidget(self.header)
        self.strip = IncidentStrip(ctx)
        self.strip.open_incident.connect(lambda inc_id: win.open_incident(inc_id))
        self.strip.see_all.connect(lambda: win.go_page(win.incidents))
        lay.addWidget(self.strip)

        self.body = QStackedWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("Transparent")
        self.scroll.viewport().installEventFilter(self)
        self.grid = QGridLayout(host)
        self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setSpacing(16)
        self.grid.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(host)
        self.empty = EmptyState(ctx)
        self.body.addWidget(self.scroll)
        self.body.addWidget(self.empty)
        lay.addWidget(self.body, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self.refresh_thumbs)
        shortcut(self, "F5", lambda: self.isVisible() and self.refresh_thumbs())

    def reload(self) -> None:
        self.stale = False
        for c in self.cards.values():
            c.deleteLater()
        self.cards.clear()
        self.status.clear()
        self.strip.refresh()
        site = self.ctx.cfg.active_site()
        self.refresh_btn.setEnabled(False)
        if site is None:
            self.header.subtitle.setText("")
            self.empty.configure("shield", "Set up your first site",
                                 "Connect your recorder (NVR) or camera and your cameras will appear here.",
                                 "Start setup", self.win.open_wizard)
            self.body.setCurrentWidget(self.empty)
            return
        cams = site.enabled_cameras()
        if not cams:
            self.header.subtitle.setText(site.name)
            self.empty.configure("video", "No cameras selected",
                                 "Choose which channels to show in Sites & cameras.",
                                 "Open Sites & cameras", lambda: self.win.go_page(self.win.sites))
            self.body.setCurrentWidget(self.empty)
            return
        for cam in cams:
            c = CameraCard(self.ctx, cam)
            c.live_requested.connect(self.win.open_live)
            c.snapshot_requested.connect(self.snapshot)
            self.cards[cam.channel] = c
        self.body.setCurrentWidget(self.scroll)
        self.refresh_btn.setEnabled(True)
        self._cols = 0
        self._relayout()
        self.refresh_thumbs()
        self.set_monitor_status(self.win.monitor_status())

    def set_monitor_status(self, status: dict[int, dict]) -> None:
        for ch, c in self.cards.items():
            c.set_monitor(status.get(ch))

    def _relayout(self) -> None:
        cols = max(1, min(4, (self.scroll.viewport().width() + 16) // 340))
        if cols == self._cols or not self.cards:
            return
        self._cols = cols
        for i, c in enumerate(self.cards.values()):
            self.grid.addWidget(c, i // cols, i % cols)
        for col in range(4):
            self.grid.setColumnStretch(col, 1 if col < cols else 0)

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport() and event.type() == QEvent.Resize:
            self._relayout()
        return False

    def refresh_thumbs(self) -> None:
        site = self.ctx.cfg.active_site()
        if site is None:
            return
        for ch, c in self.cards.items():
            if c.conn != "online":   # an online card keeps its status while its picture refreshes
                c.set_connection("checking")
            self.status[ch] = "checking"
            url = site.rtsp_url(ch, main=True)
            run_async(fetch_thumb, url, on_done=lambda r, ch=ch, sid=site.id: self._thumb(sid, ch, r))
        self._summary()

    def _thumb(self, site_id: str, ch: int, result) -> None:
        site = self.ctx.cfg.active_site()
        c = self.cards.get(ch)
        if c is None or site is None or site.id != site_id:
            return
        img, status = (None, "offline") if isinstance(result, Exception) else result
        c.thumb.set_image(img, "No connection" if img is None else "")
        c.set_connection(status)
        self.status[ch] = status
        self._summary()

    def _summary(self) -> None:
        site = self.ctx.cfg.active_site()
        if site is None:
            return
        online = sum(1 for s in self.status.values() if s == "online")
        checking = any(s == "checking" for s in self.status.values())
        state = "checking…" if checking else f"{online} of {len(self.cards)} online"
        self.header.subtitle.setText(f"{site.name} · {site.host} · {state}")

    def snapshot(self, ch: int) -> None:
        site = self.ctx.cfg.active_site()
        cam = site.camera(ch)
        self.win.notify(f"Saving snapshot of {cam.label}…")
        main = self.ctx.cfg.settings.stream == "main"

        def done(frame):
            if frame is None or isinstance(frame, Exception):
                self.win.notify(f"Could not get a picture from {cam.label}.", error=True)
                return
            path = self.ctx.save_snapshot(frame, site, cam)
            self.win.notify(f"Snapshot saved: {path}")

        run_async(grab_frame, site.rtsp_url(ch, main=main), on_done=done)

    def showEvent(self, event):
        super().showEvent(event)
        if self.stale:
            self.reload()
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()


# --- Live view ------------------------------------------------------------------

TOAST_HOLD_MS = 15_000  # longest a pop-up waits for the AI brain's verdict (2-3 checks of ~1-3 s)
BACKEND_TEXT = {"pytorch-fp16": "GPU (fast mode)", "pytorch": "GPU", "cpu": "processor (slow)",
                "standard": "GPU (standard mode)"}
CPU_NOTE = ("No NVIDIA graphics card found: the AI runs on the processor. It works, but checks fewer "
            "pictures per second. Fewer cameras or a smaller AI model (Settings, AI models) help.")


def track_tuples(tracks, w: int, h: int) -> list[tuple]:
    """Tracks as (id, label, x1, y1, x2, y2) in 0..1 picture coordinates, for the areas canvas."""
    return [(t.id, t.label, float(t.box[0]) / w, float(t.box[1]) / h, float(t.box[2]) / w, float(t.box[3]) / h)
            for t in tracks]


class LiveWorker(QThread):
    frame_ready = Signal(QImage, str)
    state = Signal(str, str)  # status, text
    tracks_ready = Signal(list)  # [(id, label, x1, y1, x2, y2)] normalised, after each detection

    def __init__(self, url: str, ctx: AppContext, style: str, site_id: str = "", channel: int = 0):
        super().__init__()
        self.url, self.ctx = url, ctx
        self.style = style  # "boxes" | "corners" | "off"; changed live from the GUI thread
        self.site_id, self.channel = site_id, channel
        self.tracker = DisplayTracker()
        self.running = True
        self.last_frame = None
        self.raw_frame = None  # the camera picture without drawings (the AI brain needs it clean)
        self._fast_failed = False
        self.backend = ""

    def _detect(self, frame, settings):
        """Fast batched detector when available; the original Detector otherwise."""
        if not self._fast_failed:
            try:
                if not self.ctx.live_detector_loaded():
                    self.state.emit("checking", "Loading AI model…")
                det = self.ctx.live_detector()
                self.backend = getattr(det, "backend", "")
                # Detect with a low threshold: weak hits keep known objects alive on noisy night
                # frames, while only hits above the user's certainty start new objects.
                dets = det.predict([frame], min(LOW_SCORE, settings.confidence))[0]
                tracks = self.tracker.update(dets, high=settings.confidence)
                h, w = frame.shape[:2]
                self.tracks_ready.emit(track_tuples(tracks, w, h))
                return draw_tracks(frame, tracks, self.style), track_counts(tracks)
            except Exception:
                log.exception("Fast detector not available, using the standard one")
                self._fast_failed = True
        if not self.ctx.detector.is_ready(settings.model):
            self.state.emit("checking", "Loading AI model…")
        import torch
        self.backend = "standard" if torch.cuda.is_available() else "cpu"
        return self.ctx.detector.detect(frame, settings.model, settings.confidence)

    def _download_progress(self, done: int, total: int) -> None:
        pct = f" {done * 100 // total}%" if total else ""
        self.state.emit("checking", f"Downloading AI model ({done // 2**20} MB){pct}. Only needed once…")

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self.running and time.time() < end:
            self.msleep(100)

    def run(self) -> None:
        cap, fps, last = None, 0.0, time.time()
        settings = self.ctx.cfg.settings
        while self.running:
            if cap is None:
                self.state.emit("checking", "Connecting to camera…")
                cap = open_capture(self.url)
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self.state.emit("offline", "Camera not reachable. Retrying…")
                    self._sleep(3)
                    continue
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                self.state.emit("offline", "Connection lost. Reconnecting…")
                self._sleep(2)
                continue
            self.raw_frame = frame.copy()  # some overlay styles draw in place
            counts = {}
            detect = self.style != "off"
            if detect:
                try:
                    if not model_ready(settings.model):
                        ensure_model(settings.model, progress=self._download_progress)
                    frame, counts = self._detect(frame, settings)
                except Exception as exc:
                    log.exception("Detection failed")
                    self.style = "off"
                    self.state.emit("offline", f"AI detection stopped: {exc}")
            else:
                if self.tracker.tracks:
                    self.tracks_ready.emit([])  # no stale foot dots on the areas canvas
                self.tracker.reset()  # turning the overlay back on starts with fresh, unflickering tracks
            self.last_frame = frame  # snapshots keep the AI boxes; areas are painted by the canvas
            now = time.time()
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-6)
            last = now
            h, w = frame.shape[:2]
            found = object_text(counts) or "nothing found"
            ai = f" · {found} · AI on {BACKEND_TEXT.get(self.backend, self.backend)}" if detect else ""
            info = f"{w}×{h} · {fps:.0f} fps" + ai
            self.frame_ready.emit(to_qimage(frame), info)
        if cap is not None:
            cap.release()


class LivePage(QWidget):
    def __init__(self, ctx: AppContext, win: "MainWindow"):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.worker: LiveWorker | None = None
        self._shown: tuple | None = None   # (site id, channel, url) the worker is showing
        self._old: set[LiveWorker] = set()
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(32, 28, 32, 24)
        self._lay.setSpacing(16)
        self.header = PageHeader("Live view", "Watch a camera with AI detection.")
        self._lay.addWidget(self.header)

        self.toolbar = QWidget()
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(10)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(280)
        self.combo.setAccessibleName("Camera")
        self.combo.currentIndexChanged.connect(lambda _i: self.isVisible() and self.start())
        self.overlay_combo = QComboBox()
        self.overlay_combo.setAccessibleName("AI overlay")
        self.overlay_combo.setToolTip("How AI detections are drawn on the video")
        for key, text in OVERLAY_STYLES.items():
            self.overlay_combo.addItem(f"AI: {text}", key)
        s = ctx.cfg.settings
        start_style = s.overlay_style if s.show_detections else "off"
        self.overlay_combo.setCurrentIndex(max(0, self.overlay_combo.findData(start_style)))
        self.overlay_combo.currentIndexChanged.connect(self._style_changed)
        self.areas = LiveAreas(ctx, win, raw_frame=lambda: getattr(self.worker, "raw_frame", None))
        self.snap_btn = button("Snapshot", tooltip="Save the current picture (S)")
        ctx.bind_icon(self.snap_btn, "camera")
        self.snap_btn.clicked.connect(self.snapshot)
        self.full_btn = button("Full screen", tooltip="Full screen (F11, Esc to leave)")
        ctx.bind_icon(self.full_btn, "maximize")
        self.full_btn.clicked.connect(lambda: self.win.toggle_focus())
        bar.addWidget(self.combo)
        bar.addWidget(self.overlay_combo)
        bar.addSpacing(8)
        bar.addWidget(self.areas.show_cb)
        bar.addStretch(1)
        bar.addWidget(self.areas.add_btn)
        bar.addWidget(self.snap_btn)
        bar.addWidget(self.full_btn)
        self._lay.addWidget(self.toolbar)

        self.video = self.areas.canvas  # the picture with areas painted and drawn on (ImageView API)
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self.video, 1)
        body.addWidget(self.areas.panel)
        self._lay.addLayout(body, 1)
        info = QHBoxLayout()
        self.info = muted("", wrap=False)
        self.state = QLabel("")
        info.addWidget(self.info, 1)
        info.addWidget(self.state)
        self._lay.addLayout(info)

        shortcut(self, "S", lambda: self.isVisible() and self.snapshot())
        shortcut(self, "F11", lambda: self.isVisible() and self.win.toggle_focus())
        shortcut(self, "Esc", lambda: self.areas.escape() or (self.win.focus_mode and self.win.toggle_focus(False)))

    def populate(self, select: int | None = None) -> None:
        site = self.ctx.cfg.active_site()
        current = select if select is not None else self.combo.currentData()
        self.combo.blockSignals(True)
        self.combo.clear()
        for cam in site.enabled_cameras() if site else []:
            self.combo.addItem(f"{cam.label}  (channel {cam.channel})", cam.channel)
        idx = self.combo.findData(current)
        self.combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo.blockSignals(False)
        has = self.combo.count() > 0
        for w in (self.combo, self.snap_btn, self.full_btn, self.overlay_combo, self.areas.show_cb,
                  self.areas.add_btn):
            w.setEnabled(has)

    def start(self, force: bool = False) -> None:
        """Show the chosen camera. Keeps the running stream when it is the same camera address
        (saving an area must not reconnect the video), unless force."""
        site = self.ctx.cfg.active_site()
        ch = self.combo.currentData()
        if site is None or ch is None:
            self.stop()
            self.areas.set_camera(site, None)
            self.video.set_image(None, "No camera to show. Add one in Sites & cameras.")
            self.header.subtitle.setText("")
            self._shown = None
            return
        cam = site.camera(ch)
        switching = self._shown is None or self._shown[:2] != (site.id, ch)
        if switching and not self.areas.canvas.drafting:
            # no picture of the previous camera under the new one (area thumbnails are taken from it)
            self.video.set_image(None, "Connecting…")
        if not self.areas.set_camera(site, cam):  # the user kept drawing on the previous camera
            prev = self._shown[1] if self._shown else None
            idx = self.combo.findData(prev)
            if idx >= 0:
                self.combo.blockSignals(True)
                self.combo.setCurrentIndex(idx)
                self.combo.blockSignals(False)
            return
        self.header.subtitle.setText(f"{site.name} · {cam.label}")
        url = site.rtsp_url(ch, main=self.ctx.cfg.settings.stream == "main")
        if not force and self.worker is not None and self._shown == (site.id, ch, url):
            return
        self.stop()
        self._shown = (site.id, ch, url)
        self.worker = LiveWorker(url, self.ctx, self.overlay_combo.currentData(), site.id, ch)
        self.worker.frame_ready.connect(self._frame)
        self.worker.state.connect(self._state)
        self.worker.tracks_ready.connect(self.areas.set_tracks)
        self.areas.set_tracks([])
        if not self.areas.canvas.drafting:  # a drawing goes on over the last picture until frames come
            self.video.set_image(None, "Connecting…")
        self.worker.start()

    def stop(self) -> None:
        w = self.worker
        if w is None:
            return
        self.worker = None
        self._shown = None
        w.frame_ready.disconnect()
        w.state.disconnect()
        w.tracks_ready.disconnect()
        w.running = False
        self._old.add(w)
        w.finished.connect(lambda w=w: self._old.discard(w))

    def stop_all(self, wait_ms: int = 3000) -> None:
        self.stop()
        self.areas.stop()
        for w in list(self._old):
            w.wait(wait_ms)

    def _frame(self, img: QImage, info: str) -> None:
        self.video.set_image(img)
        self.info.setText(info)
        set_status(self.state, "online", "● Live")

    def _state(self, status: str, text: str) -> None:
        set_status(self.state, status, text)
        if status == "offline":
            self.video.set_image(None, text)

    def _style_changed(self) -> None:
        style = self.overlay_combo.currentData()
        if self.worker:
            self.worker.style = style
        s = self.ctx.cfg.settings
        s.show_detections = style != "off"
        if style != "off":
            s.overlay_style = style
        self.ctx.cfg.save()

    def snapshot(self) -> None:
        if not self.worker or self.worker.last_frame is None:
            self.win.notify("No picture yet. Wait until the video is shown.", error=True)
            return
        site = self.ctx.cfg.active_site()
        path = self.ctx.save_snapshot(self.worker.last_frame, site, site.camera(self.combo.currentData()))
        self.win.notify(f"Snapshot saved: {path}")

    def set_focus_layout(self, on: bool) -> None:
        self.header.setVisible(not on)
        self.toolbar.setVisible(not on)
        self._lay.setContentsMargins(*((0, 0, 0, 6) if on else (32, 28, 32, 24)))
        if on:
            self.info.setText("Press Esc to leave full screen")

    def showEvent(self, event):
        super().showEvent(event)
        self.populate()
        self.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self.win.focus_mode:
            self.stop()
            self.areas.stop()


# --- Sites & cameras ---------------------------------------------------------------

class SitesPage(QWidget):
    COLS = ("Show", "Channel", "Name", "Role", "Status", "Alerts", "Areas")

    def __init__(self, ctx: AppContext, win: "MainWindow"):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.site = None
        self.dirty = False
        self._loading = False
        self._gen = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(20)
        header = PageHeader("Sites & cameras",
                            "A site is one recorder (NVR) or camera. Add a site for every place you want to watch.")
        add = button("Add site", "primary")
        ctx.bind_icon(add, "plus", "on_primary")
        add.clicked.connect(self._add_site)
        header.actions.addWidget(add)
        lay.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(20)
        left, ll = card("Sites")
        left.setFixedWidth(250)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._row_changed)
        ll.addWidget(self.list, 1)
        self.del_btn = button("Delete site", "danger")
        ctx.bind_icon(self.del_btn, "trash", "danger")
        self.del_btn.clicked.connect(self._delete)
        ll.addWidget(self.del_btn)
        body.addWidget(left, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        editor = QWidget()
        editor.setObjectName("Transparent")
        self.editor = editor
        el = QVBoxLayout(editor)
        el.setContentsMargins(0, 0, 6, 0)
        el.setSpacing(16)

        conn, cl = card("Connection", "How YardGuard reaches the video. The address is on the recorder's "
                                      "network screen, or use Find on network.")
        form = form_layout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Home yard")
        host_row = QHBoxLayout()
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("e.g. 192.168.1.108")
        find = button("Find on network")
        ctx.bind_icon(find, "search")
        find.clicked.connect(self._find)
        host_row.addWidget(self.host_edit, 1)
        host_row.addWidget(find)
        self.port_spin = spin_box(1, 65535, 554)
        self.brand = brand_combo()
        self.path_edit = QLineEdit()
        self.path_edit.setToolTip("Use {channel} for the channel number and {subtype} for 0 = main, 1 = sub stream.")
        self.user_edit = QLineEdit()
        self.pass_edit = password_field(ctx)
        test_row = QHBoxLayout()
        self.test_btn = button("Test connection")
        ctx.bind_icon(self.test_btn, "check")
        self.test_btn.clicked.connect(self._test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        form.addRow("Site name", self.name_edit)
        form.addRow("Address", host_row)
        form.addRow("RTSP port", self.port_spin)
        form.addRow("Brand", self.brand)
        form.addRow("Stream path", self.path_edit)
        form.addRow("Username", self.user_edit)
        form.addRow("Password", self.pass_edit)
        form.addRow("", test_row)
        self.form = form
        cl.addLayout(form)
        el.addWidget(conn)

        cams, cml = card("Cameras", "Show: the camera appears on the dashboard.  Alerts: monitoring watches it "
                                    "and raises alerts (untick indoor cameras you do not want alerts for).")
        tools = QHBoxLayout()
        tools.addWidget(QLabel("Channels"))
        self.count_spin = spin_box(1, 64, 8)
        self.count_spin.setToolTip("How many channels to check")
        detect = button("Detect cameras", tooltip="Check every channel for video")
        ctx.bind_icon(detect, "scan")
        detect.clicked.connect(self._detect)
        add_ch = button("Add channel")
        ctx.bind_icon(add_ch, "plus")
        add_ch.clicked.connect(self._add_channel)
        self.rm_btn = button("Remove", tooltip="Remove the selected channel")
        ctx.bind_icon(self.rm_btn, "trash")
        self.rm_btn.clicked.connect(self._remove_channel)
        tools.addWidget(self.count_spin)
        tools.addWidget(detect)
        tools.addStretch(1)
        tools.addWidget(add_ch)
        tools.addWidget(self.rm_btn)
        cml.addLayout(tools)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setDefaultSectionSize(40)
        hh = self.table.horizontalHeader()
        for col, mode in enumerate((QHeaderView.ResizeToContents, QHeaderView.ResizeToContents,
                                    QHeaderView.Stretch, QHeaderView.Fixed, QHeaderView.Fixed,
                                    QHeaderView.ResizeToContents, QHeaderView.Fixed)):
            hh.setSectionResizeMode(col, mode)
        self.table.setColumnWidth(3, 128)
        self.table.setColumnWidth(4, 104)
        self.table.setColumnWidth(6, 132)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._fit_table()
        self.table.itemChanged.connect(self._mark_dirty)
        cml.addWidget(self.table)
        el.addWidget(cams)
        el.addStretch(1)
        scroll.setWidget(editor)

        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(scroll, 1)
        footer = QHBoxLayout()
        self.dirty_lbl = muted("", wrap=False)
        self.discard_btn = button("Discard changes")
        self.discard_btn.clicked.connect(lambda: self.load_site(self.ctx.cfg.site(self.site.id)))
        self.save_btn = button("Save changes", "primary", "Save (Ctrl+S)")
        self.save_btn.clicked.connect(self.save)
        footer.addWidget(self.dirty_lbl, 1)
        footer.addWidget(self.discard_btn)
        footer.addWidget(self.save_btn)
        right.addLayout(footer)
        body.addLayout(right, 1)
        lay.addLayout(body, 1)

        for w in (self.name_edit, self.host_edit, self.path_edit, self.user_edit, self.pass_edit):
            w.textChanged.connect(self._mark_dirty)
        self.port_spin.valueChanged.connect(self._mark_dirty)
        self.brand.currentIndexChanged.connect(self._brand_changed)
        shortcut(self, "Ctrl+S", lambda: self.isVisible() and self.dirty and self.save())
        self._set_dirty(False)

    # list of sites
    def refresh(self) -> None:
        keep = self.site.id if self.site else self.ctx.cfg.active_site_id
        self.list.blockSignals(True)
        self.list.clear()
        for s in self.ctx.cfg.sites:
            item = QListWidgetItem(f"{s.name}\n{s.host}")
            item.setData(Qt.UserRole, s.id)
            self.list.addItem(item)
        self.list.blockSignals(False)
        ids = [s.id for s in self.ctx.cfg.sites]
        if not ids:
            self.site = None
            self.editor.setEnabled(False)
            self.del_btn.setEnabled(False)
            self._set_dirty(False)
            return
        self.editor.setEnabled(True)
        self.del_btn.setEnabled(True)
        row = ids.index(keep) if keep in ids else 0
        self.list.blockSignals(True)
        self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        if not self.dirty or self.site is None or self.site.id != ids[row]:
            self.load_site(self.ctx.cfg.sites[row])

    def _row_changed(self, row: int) -> None:
        if row < 0 or self.site is None:
            return
        new_id = self.list.item(row).data(Qt.UserRole)
        if new_id == self.site.id:
            return
        if self.dirty and not self.confirm_leave():
            ids = [s.id for s in self.ctx.cfg.sites]
            self.list.blockSignals(True)
            self.list.setCurrentRow(ids.index(self.site.id))
            self.list.blockSignals(False)
            return
        self.load_site(self.ctx.cfg.site(new_id))

    def confirm_leave(self) -> bool:
        """Ask about unsaved changes. Returns False if the user cancels."""
        box = QMessageBox(QMessageBox.Question, "Unsaved changes",
                          f"Save changes to “{self.name_edit.text() or self.site.name}”?",
                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, self)
        answer = box.exec()
        if answer == QMessageBox.Save:
            return self.save()
        if answer == QMessageBox.Discard:
            self._set_dirty(False)
            return True
        return False

    # editor
    def load_site(self, site) -> None:
        if site is None:
            return
        self._loading = True
        self.site = copy.deepcopy(site)
        self.name_edit.setText(site.name)
        self.host_edit.setText(site.host)
        self.port_spin.setValue(site.port)
        self.brand.setCurrentIndex(max(0, self.brand.findData(site.brand)))
        self.path_edit.setText(site.custom_path)
        self.user_edit.setText(site.username)
        self.pass_edit.clear()
        self.pass_edit.setPlaceholderText("Saved. Type a new one to change it" if site.has_password else "Password")
        self.test_result.setText("")
        self.table.setRowCount(0)
        for cam in sorted(site.cameras, key=lambda c: c.channel):
            self._add_row(cam)
        self._fit_table()
        self.count_spin.setValue(max([c.channel for c in site.cameras] + [8]))
        self._brand_changed()
        self._loading = False
        self._set_dirty(False)

    def _brand_changed(self) -> None:
        self.form.setRowVisible(self.path_edit, self.brand.currentData() == "custom")
        self._mark_dirty()

    def _add_row(self, cam: Camera) -> int:
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        show = QTableWidgetItem()
        show.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        show.setCheckState(Qt.Checked if cam.enabled else Qt.Unchecked)
        ch = QTableWidgetItem()
        ch.setData(Qt.DisplayRole, cam.channel)  # numeric, so 10 sorts after 9
        ch.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        ch.setData(Qt.UserRole, cam.channel)
        name = QTableWidgetItem(cam.name)
        status = QTableWidgetItem("")
        status.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        role = QComboBox()
        role.addItems(ROLES)
        role.setCurrentText(cam.role)
        role.currentIndexChanged.connect(self._mark_dirty)
        watch = QTableWidgetItem()
        watch.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        watch.setCheckState(Qt.Checked if cam.monitor else Qt.Unchecked)
        watch.setToolTip("Include this camera in monitoring and alerts")
        show.setToolTip("Show this camera on the dashboard")
        for col, item in ((0, show), (1, ch), (2, name), (4, status), (5, watch)):
            self.table.setItem(row, col, item)
        self.table.setCellWidget(row, 3, role)
        areas = areas_button(cam)
        self.ctx.bind_icon(areas, "pentagon", "muted")
        areas.setAccessibleName(f"Areas of channel {cam.channel}")
        # Deferred: saving first rebuilds the table, which deletes this button inside its own click.
        areas.clicked.connect(lambda _=False, c=cam.channel: QTimer.singleShot(0, lambda: self._open_areas(c)))
        self.table.setCellWidget(row, 6, areas)
        self.table.blockSignals(False)
        self._fit_table()
        return row

    def _open_areas(self, channel: int) -> None:
        """Areas are drawn on the live picture: open Live view on that camera (in add mode when
        it has none yet)."""
        if self.site is None:
            return
        if self.dirty:
            answer = QMessageBox.question(
                self, "Save changes first",
                f"Save the changes to “{self.name_edit.text() or self.site.name}” before drawing areas?",
                QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Save)
            if answer != QMessageBox.Save or not self.save():
                return
        site = self.ctx.cfg.site(self.site.id)
        cam = site.camera(channel) if site else None
        if cam is None:
            self.win.notify("Save the site first, then draw its areas.", error=True)
            return
        if not cam.enabled:
            self.win.notify("Tick Show for this camera first: areas are drawn in Live view.", error=True)
            return
        if site.id != self.ctx.cfg.active_site_id:
            self.ctx.cfg.active_site_id = site.id
            self.ctx.save()
        self.win.open_live(channel)
        if not cam.areas:
            self.win.live.areas.request(channel, "add")

    def _row_for(self, channel: int) -> int:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 1).data(Qt.UserRole) == channel:
                return r
        return -1

    def _cameras(self) -> list[Camera]:
        cams = []
        for r in range(self.table.rowCount()):
            ch = self.table.item(r, 1).data(Qt.UserRole)
            old = self.site.camera(ch) if self.site else None  # keep what the table does not show
            cams.append(Camera(ch, self.table.item(r, 2).text().strip() or f"Camera {ch}",
                               self.table.cellWidget(r, 3).currentText(),
                               self.table.item(r, 0).checkState() == Qt.Checked,
                               monitor=self.table.item(r, 5).checkState() == Qt.Checked,
                               areas=copy.deepcopy(old.areas) if old else []))
        return cams

    def collect(self):
        s = copy.deepcopy(self.site)
        s.name = self.name_edit.text().strip() or s.name
        s.host = self.host_edit.text().strip()
        s.port = self.port_spin.value()
        s.brand = self.brand.currentData()
        s.custom_path = self.path_edit.text().strip() or s.custom_path
        s.username = self.user_edit.text().strip()
        if self.pass_edit.text():
            s.set_password(self.pass_edit.text())
        s.cameras = self._cameras()
        return s

    def save(self) -> bool:
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "Address missing", "Enter the device address before saving.")
            self.host_edit.setFocus()
            return False
        site = self.collect()
        sites = self.ctx.cfg.sites
        idx = next(i for i, s in enumerate(sites) if s.id == site.id)
        sites[idx] = site
        self.site = copy.deepcopy(site)
        self._set_dirty(False)
        self.pass_edit.clear()
        self.pass_edit.setPlaceholderText("Saved. Type a new one to change it" if site.has_password else "Password")
        self.ctx.save()
        self.win.notify(f"Saved “{site.name}”.")
        return True

    def _mark_dirty(self, *_):
        if not self._loading and self.site is not None:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.save_btn.setEnabled(dirty)
        self.discard_btn.setEnabled(dirty)
        self.dirty_lbl.setText("You have unsaved changes." if dirty else "")

    # actions
    def _add_site(self) -> None:
        if self.dirty and not self.confirm_leave():
            return
        self.win.open_wizard()

    def _delete(self) -> None:
        if self.site is None:
            return
        answer = QMessageBox.question(
            self, "Delete site",
            f"Delete “{self.site.name}”?\n\nThis removes its saved login and camera list from this PC. "
            "Recordings on the device are not affected.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        cfg = self.ctx.cfg
        cfg.sites = [s for s in cfg.sites if s.id != self.site.id]
        if cfg.active_site_id == self.site.id:
            cfg.active_site_id = cfg.sites[0].id if cfg.sites else ""
        name = self.site.name
        self.site = None
        self._set_dirty(False)
        self.ctx.save()
        self.win.notify(f"Deleted “{name}”.")

    def _find(self) -> None:
        dlg = ScanDialog(self.ctx, self)
        if dlg.exec() and dlg.device:
            self.host_edit.setText(dlg.device.ip)
            self.brand.setCurrentIndex(max(0, self.brand.findData(dlg.device.brand)))
            self.port_spin.setValue(554)

    def _test(self) -> None:
        site = self.collect()
        cams = site.enabled_cameras() or site.cameras
        ch = cams[0].channel if cams else 1
        self.test_btn.setEnabled(False)
        set_status(self.test_result, "checking", f"Connecting to channel {ch}…")

        def done(result):
            self.test_btn.setEnabled(True)
            ok, msg = (False, str(result)) if isinstance(result, Exception) else result
            set_status(self.test_result, "online" if ok else "offline", ("✓ " if ok else "✕ ") + msg)

        run_async(test_connection, site.rtsp_url(ch, main=False), on_done=done)

    def _detect(self) -> None:
        site = self.collect()
        self._gen += 1
        for ch in range(1, self.count_spin.value() + 1):
            row = self._row_for(ch)
            if row < 0:
                row = self._add_row(Camera(ch, f"Camera {ch}", "Other", False))
                self.table.item(row, 0).setData(Qt.UserRole, "new")
            self._set_row_status(row, "checking")
            run_async(fetch_thumb, site.rtsp_url(ch, main=True), on_done=lambda r, ch=ch, g=self._gen: self._detected(ch, r, g))
        self.table.sortItems(1)
        self._mark_dirty()

    def _detected(self, ch: int, result, gen: int) -> None:
        row = self._row_for(ch)
        if row < 0 or gen != self._gen:
            return
        status = "offline" if isinstance(result, Exception) else result[1]
        self._set_row_status(row, status)
        show = self.table.item(row, 0)
        if show.data(Qt.UserRole) == "new":
            show.setData(Qt.UserRole, None)
            show.setCheckState(Qt.Checked if status == "online" else Qt.Unchecked)

    def _set_row_status(self, row: int, status: str) -> None:
        self.table.blockSignals(True)
        item = self.table.item(row, 4)
        item.setText({"online": "● Video", "nosignal": "● No signal", "offline": "● Offline",
                      "checking": "● Checking…"}[status])
        key = {"online": "success", "nosignal": "warning", "offline": "danger", "checking": "muted"}[status]
        item.setForeground(QColor(self.ctx.palette[key]))
        self.table.blockSignals(False)

    def _add_channel(self) -> None:
        used = {c.channel for c in self._cameras()}
        ch = next(i for i in range(1, 257) if i not in used)
        row = self._add_row(Camera(ch, f"Camera {ch}"))
        self.table.sortItems(1)
        self.table.selectRow(self._row_for(ch))
        self.table.editItem(self.table.item(self._row_for(ch), 2))
        self._mark_dirty()

    def _remove_channel(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.table.removeRow(rows[0].row())
            self._fit_table()
            self._mark_dirty()

    def _fit_table(self) -> None:
        """The table is as tall as its rows, so only the page scrolls (no nested scroll bars)."""
        rows = max(self.table.rowCount(), 3)
        header = self.table.horizontalHeader().sizeHint().height()
        self.table.setFixedHeight(header + rows * self.table.verticalHeader().defaultSectionSize()
                                  + 2 * self.table.frameWidth() + 2)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()


# --- Settings -------------------------------------------------------------------------

def _plate_partial() -> int:
    """Bytes of the plate models already on disk (complete files and partial downloads)."""
    files = [plates.model_file(s) for s in (plates.DETECTOR, plates.READER)]
    return sum(p.stat().st_size for f in files for p in (f, f.with_suffix(".download")) if p.exists())


# Optional models that area tasks use (not detection models): key -> title, text and their download API.
ADDONS = {
    "plates": {"title": "Number plates", "text": "Reads number plates for ‘Log number plates’ areas",
               "mb": plates.DOWNLOAD_MB, "state": plates.download_state, "download": plates.get_download,
               "delete": plates.delete_models, "partial": _plate_partial},
}


def model_info(key: str) -> tuple[str, str, float]:
    """(title, description, size in MB) of a detection model or an add-on."""
    if key in ADDONS:
        a = ADDONS[key]
        return a["title"], a["text"], a["mb"]
    return MODELS[key]


class _DownloadSignals(QObject):
    progress = Signal(str, int, int)   # model, done bytes, total bytes
    finished = Signal(str, str)        # model, "done" | "paused" | "cancelled" | error text


class ModelRow(QFrame):
    """One AI model: info, status/progress and the actions that fit its state."""

    def __init__(self, ctx: AppContext, card: "ModelsCard", key: str):
        super().__init__()
        self.ctx, self.card, self.key = ctx, card, key
        self.addon = key in ADDONS
        title, desc, size = model_info(key)
        self.size = size
        self.setObjectName("ModelRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(16)

        info = QVBoxLayout()
        info.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(label(title, "CardTitle"))
        self.in_use = label("In use", "Chip")
        set_prop(self.in_use, "accent", True)
        top.addWidget(self.in_use)
        if self.addon:
            top.addWidget(label("Optional", "Chip"))
        top.addStretch(1)
        info.addLayout(top)
        info.addWidget(muted(f"{desc}  ·  {size:.0f} MB" if self.addon else f"{desc}  ·  {key}  ·  {size:.0f} MB",
                             wrap=False))
        row.addLayout(info, 1)

        status = QVBoxLayout()
        status.setSpacing(4)
        self.status = QLabel()
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setFixedWidth(200)
        status.addWidget(self.status)
        status.addWidget(self.bar, 0, Qt.AlignRight)
        row.addLayout(status)

        actions_box = QWidget()
        actions_box.setFixedWidth(240)  # same width in every row, so status columns line up
        actions = QHBoxLayout(actions_box)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addStretch(1)
        row.addWidget(actions_box)
        self.buttons = {}
        for name, text, kind, icon, tip in (
            ("download", "Download", "secondary", "download", "Download this model"),
            ("pause", "Pause", "secondary", "pause", "Pause the download"),
            ("resume", "Resume", "secondary", "download", "Continue the download"),
            ("cancel", "Cancel", "ghost", "x", "Stop and delete the partial download"),
            ("use", "Use this model", "primary", None, "Use this model for AI detection"),
            ("delete", "", "ghost", "trash", "Delete this model from the PC"),
        ):
            b = button(text, kind, tip)
            if icon:
                ctx.bind_icon(b, icon, "danger" if name == "delete" else "on_primary" if kind == "primary" else "text")
            b.clicked.connect(lambda _=False, n=name: card.action(key, n))
            actions.addWidget(b)
            self.buttons[name] = b
        self.buttons["delete"].setAccessibleName(f"Delete {title} model")

    def show_state(self, state: str, done: int = 0, total: int = 0, error: str = "") -> None:
        """state: ready | missing | downloading | paused | error"""
        in_use = not self.addon and self.ctx.cfg.settings.model == self.key
        self.in_use.setVisible(in_use and state == "ready")
        show = {
            "ready": ["delete"] if in_use or self.addon else ["use", "delete"],
            "missing": ["download"],
            "downloading": ["pause", "cancel"],
            "paused": ["resume", "cancel"],
            "error": ["resume", "cancel"] if done else ["download"],
        }[state]
        for name, b in self.buttons.items():
            b.setVisible(name in show)
        self.buttons["delete"].setEnabled(not in_use)
        self.buttons["delete"].setToolTip("Switch to another model before deleting this one" if in_use
                                          else "Delete this model from the PC")
        mb = lambda n: f"{n / 2**20:.1f}"
        if state == "ready":
            set_status(self.status, "online", "✓ Downloaded")
        elif state == "missing":
            set_status(self.status, "nosignal" if in_use else "checking",
                       "Selected, download needed" if in_use else
                       "Downloads on first use" if self.addon else "Not downloaded")
        elif state == "downloading":
            pct = f"{done * 100 // total}%  ·  " if total else ""
            set_status(self.status, "checking", f"Downloading {pct}{mb(done)} of {mb(total) if total else '?'} MB")
        elif state == "paused":
            set_status(self.status, "nosignal", f"Paused at {mb(done)} MB")
        else:
            set_status(self.status, "offline", f"✕ {error}")
            self.status.setToolTip(error)
        self.bar.setVisible(state in ("downloading", "paused") or (state == "error" and done > 0))
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(done)


class ModelsCard(QFrame):
    """Download manager for the AI models: download, pause, resume, cancel, delete, use."""

    def __init__(self, ctx: AppContext, win: "MainWindow"):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 20)
        lay.setSpacing(12)
        lay.addWidget(label("AI models", "SectionTitle"))
        lay.addWidget(muted("Larger models find more (for example dark or very close cars) but use more GPU. "
                            "Each model is downloaded once and then works offline."))
        self.rows: dict[str, ModelRow] = {}
        self.active: set[str] = set()     # downloads running right now
        self.progress: dict[str, tuple[int, int]] = {}
        self.errors: dict[str, str] = {}
        self.signals = _DownloadSignals()
        self.signals.progress.connect(self._progress)
        self.signals.finished.connect(self._finished)
        for key in MODELS:
            r = ModelRow(ctx, self, key)
            self.rows[key] = r
            lay.addWidget(r)
        # Add-ons: extra models some area tasks use; not detection models, so never "in use".
        lay.addSpacing(6)
        lay.addWidget(label("Add-ons", "CardTitle"))
        self.addon_rows: dict[str, ModelRow] = {}
        for key in ADDONS:
            r = ModelRow(ctx, self, key)
            self.addon_rows[key] = r
            lay.addWidget(r)
        self.refresh()

    def _state(self, key: str) -> str:
        if key in self.active:
            return "downloading"
        if key in self.errors:
            return "error"
        if key in ADDONS:
            state = ADDONS[key]["state"]()
            return "paused" if state == "partial" else state
        if model_ready(key):
            return "ready"
        return "paused" if partial_path(key).exists() else "missing"

    def refresh(self) -> None:
        for key, r in {**self.rows, **self.addon_rows}.items():
            state = self._state(key)
            done, total = self.progress.get(key, (0, 0))
            if state in ("paused", "error"):
                part = ADDONS[key]["partial"]() if key in ADDONS else (
                    partial_path(key).stat().st_size if partial_path(key).exists() else 0)
                if part:
                    done = part
                    total = total or int(model_info(key)[2] * 2**20)
            r.show_state(state, done, total, self.errors.get(key, ""))

    def action(self, key: str, name: str) -> None:
        dl = ADDONS[key]["download"]() if key in ADDONS else get_download(key)
        if name in ("download", "resume"):
            self.errors.pop(key, None)
            self.active.add(key)
            self.refresh()
            last = [0.0]

            def progress(done, total):
                now = time.time()
                if now - last[0] > 0.15 or done == total:
                    last[0] = now
                    self.signals.progress.emit(key, done, total)

            def work():
                try:
                    self.signals.finished.emit(key, dl.run(progress))
                except Exception as exc:
                    self.signals.finished.emit(key, str(exc))

            threading.Thread(target=work, daemon=True, name=f"download-{key}").start()
        elif name == "pause":
            dl.pause()
        elif name == "cancel":
            dl.cancel()
            self.errors.pop(key, None)
            self.progress.pop(key, None)
            self.refresh()
        elif name == "use":
            self.ctx.cfg.settings.model = key
            self.ctx.cfg.save()
            self.win.notify(f"Now using the {MODELS[key][0]} model.")
            self.win.setting_changed("model")
            self.refresh()
        elif name == "delete":
            title, _desc, size = model_info(key)
            note = "\nNumber plate areas log crossings without the plate until it is downloaded again." \
                if key in ADDONS else ""
            if QMessageBox.question(self, "Delete model",
                                    f"Delete the {title} model ({size:.0f} MB) from this PC?\n"
                                    "You can download it again at any time." + note,
                                    QMessageBox.Yes | QMessageBox.Cancel) == QMessageBox.Yes:
                if key in ADDONS:
                    ADDONS[key]["delete"]()
                else:
                    delete_model(key)
                self.progress.pop(key, None)
                self.win.notify(f"Deleted the {title} model.")
                self.refresh()

    def _progress(self, key: str, done: int, total: int) -> None:
        self.progress[key] = (done, total)
        if key in self.active:
            {**self.rows, **self.addon_rows}[key].show_state("downloading", done, total)

    def _finished(self, key: str, result: str) -> None:
        self.active.discard(key)
        title = model_info(key)[0]
        if result == "done":
            self.progress.pop(key, None)
            self.win.notify(f"{title} model downloaded." + (
                " Number plate areas use it automatically." if key in ADDONS else
                " Click “Use this model” to switch to it."))
        elif result == "paused":
            self.win.notify(f"{title} model download paused.")
        elif result == "cancelled":
            self.progress.pop(key, None)
            self.win.notify(f"{title} model download cancelled.")
        else:
            self.errors[key] = result
            self.win.notify(f"{title} model: {result}", error=True)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def gpu_name() -> str:
    import torch
    return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "No NVIDIA GPU found (AI runs on the CPU)"


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, win: "MainWindow"):
        super().__init__()
        self.ctx, self.win = ctx, win
        s = ctx.cfg.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(20)
        lay.addWidget(PageHeader("Settings", "Changes are saved automatically."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("Transparent")
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(16)

        det, dl = card("AI detection")
        form = form_layout()
        conf_row = QHBoxLayout()
        conf_row.setSpacing(16)
        self.conf = QSlider(Qt.Horizontal)
        self.conf.setRange(15, 80)
        self.conf.setValue(int(s.confidence * 100))
        self.conf.setAccessibleName("Detection sensitivity")
        self.conf_lbl = QLabel()
        self.conf_lbl.setMinimumWidth(170)
        self.conf_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.conf.valueChanged.connect(self._conf_changed)
        conf_row.addWidget(self.conf, 1)
        conf_row.addWidget(self.conf_lbl)
        self.stream = QComboBox()
        for key, text in STREAMS.items():
            self.stream.addItem(text, key)
        self.stream.setCurrentIndex(max(0, self.stream.findData(s.stream)))
        self.stream.currentIndexChanged.connect(lambda: self._set("stream", self.stream.currentData()))
        self.show_det = QCheckBox("Show AI detections when live view opens")
        self.show_det.setChecked(s.show_detections)
        self.show_det.toggled.connect(lambda v: self._set("show_detections", v))
        form_row(form, "Minimum certainty", conf_row)
        form_row(form, "", muted("Lower values show more objects but also more wrong boxes. 35% suits most yards."))
        form_row(form, "Video quality", self.stream)
        form_row(form, "", self.show_det)
        dl.addLayout(form)
        self.brain_card = BrainCard(ctx, win)
        self.monitoring_card = MonitoringCard(ctx, win)
        for c in (self.brain_card, self.monitoring_card):
            c.changed.connect(win.setting_changed)
        self.brain_card.changed.connect(
            lambda key: key == "brain_mode" and self.monitoring_card.brain_mode_changed())
        self.models_card = ModelsCard(ctx, win)
        col.addWidget(self.models_card)
        col.addWidget(self.brain_card)
        col.addWidget(det)
        col.addWidget(self.monitoring_card)

        sto, sl = card("Storage", "Where snapshots, alert pictures, incident clips and the alert history are saved.")
        row = QHBoxLayout()
        self.folder = QLineEdit(s.events_dir)
        self.folder.setReadOnly(True)
        browse = button("Change…")
        browse.clicked.connect(self._browse)
        open_btn = button("Open folder")
        ctx.bind_icon(open_btn, "folder")
        open_btn.clicked.connect(self._open_folder)
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        row.addWidget(open_btn)
        row.setSpacing(8)
        sform = form_layout()
        form_row(sform, "Folder", row)
        sl.addLayout(sform)
        line = QFrame()
        line.setObjectName("Divider")
        sl.addWidget(line)
        self.clips = ClipsSection(ctx, win)
        self.clips.changed.connect(win.setting_changed)
        self.clips.cleaned.connect(lambda: win.incidents.reload())
        sl.addWidget(self.clips)
        col.addWidget(sto)

        app, al = card("Appearance")
        form = form_layout()
        self.theme = QComboBox()
        for key, text in THEMES.items():
            self.theme.addItem(text, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        self.theme.currentIndexChanged.connect(self._theme_changed)
        self.theme.setMaximumWidth(300)
        form_row(form, "Theme", self.theme)
        al.addLayout(form)
        col.addWidget(app)

        mnt, ml = card("Maintenance")
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.addWidget(label("Download cache", "CardTitle"))
        self.cache_lbl = muted("Calculating size…")
        text.addWidget(self.cache_lbl)
        row.addLayout(text, 1)
        self.clear_btn = button("Clear cache")
        self.clear_btn.clicked.connect(self._clear_cache)
        row.addWidget(self.clear_btn)
        ml.addLayout(row)
        line = QFrame()
        line.setObjectName("Divider")
        ml.addWidget(line)
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.addWidget(label("Uninstall YardGuard", "CardTitle"))
        text.addWidget(muted("Removes the app, AI libraries, settings and saved snapshots from this PC. "
                             "Recordings on your NVR are not touched."))
        row.addLayout(text, 1)
        uninstall = button("Uninstall…", "danger")
        ctx.bind_icon(uninstall, "power", "danger")
        uninstall.clicked.connect(self._uninstall)
        row.addWidget(uninstall, 0, Qt.AlignTop)
        ml.addLayout(row)
        col.addWidget(mnt)

        about, abl = card("About")
        self.about = QLabel()
        self.about.setTextFormat(Qt.RichText)
        self.about.setWordWrap(True)
        self.about.setTextInteractionFlags(Qt.TextSelectableByMouse)
        abl.addWidget(self.about)
        col.addWidget(about)
        col.addStretch(1)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        self._conf_changed(self.conf.value(), save=False)
        self._gpu = "checking…"
        self._render_about()

    def _set(self, key: str, value) -> None:
        setattr(self.ctx.cfg.settings, key, value)
        self.ctx.cfg.save()
        self.win.notify("Settings saved.")
        self.win.setting_changed(key)

    def _conf_changed(self, value: int, save: bool = True) -> None:
        hint = "finds more" if value < 30 else "balanced" if value <= 45 else "fewer, surer boxes"
        self.conf_lbl.setText(f"{value}%  ({hint})")
        if save:
            self._set("confidence", value / 100)

    def _theme_changed(self) -> None:
        self._set("theme", self.theme.currentData())
        self.ctx.apply_theme()

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder for snapshots and clips", self.folder.text())
        if path:
            self.folder.setText(os.path.normpath(path))
            self._set("events_dir", self.folder.text())

    def _open_folder(self) -> None:
        path = Path(self.folder.text())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def _cache_dirs(self) -> list[Path]:
        return [CACHE / "pip", CACHE / "build"]

    def _update_cache(self) -> None:
        run_async(lambda: sum(folder_size(p) for p in self._cache_dirs()), on_done=self._cache_done)

    def _cache_done(self, size) -> None:
        if isinstance(size, Exception):
            size = 0
        self.cache_lbl.setText(f"{size / 1024 ** 3:.2f} GB of installer downloads and build files. "
                               "Safe to clear, the app keeps working.")
        self.clear_btn.setEnabled(size > 0)

    def _clear_cache(self) -> None:
        if QMessageBox.question(self, "Clear cache", "Delete the download cache? The app keeps working.",
                                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        for p in self._cache_dirs():
            shutil.rmtree(p, ignore_errors=True)
        self.win.notify("Download cache cleared.")
        self._update_cache()

    def _uninstall(self) -> None:
        exe, cmd = ROOT / "Uninstall.exe", ROOT / "Uninstall.cmd"  # the .exe is built locally, the .cmd ships
        if not exe.exists() and not cmd.exists():
            QMessageBox.warning(self, "Uninstall", f"Uninstall.cmd was not found in {ROOT}.")
            return
        if QMessageBox.question(self, "Uninstall YardGuard",
                                "YardGuard will close and the uninstaller will open.\nContinue?",
                                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        args = [str(exe)] if exe.exists() else ["cmd.exe", "/c", str(cmd)]
        subprocess.Popen(args, cwd=ROOT.anchor, creationflags=subprocess.CREATE_NEW_CONSOLE)
        QApplication.quit()

    def _render_about(self) -> None:
        rows = [
            ("Version", APP_VERSION),
            ("App folder", str(ROOT)),
            ("GPU", self._gpu),
            ("Privacy", "Video is processed only on this PC. Passwords are encrypted with your Windows account."),
            ("Shortcuts", f"Ctrl+1–{len(MainWindow.NAV)} pages · F5 refresh previews · S snapshot · A add area "
                          "(Live view) · F11 full screen · Ctrl+S save"),
        ]
        self.about.setText("<table cellspacing=5>" + "".join(
            f"<tr><td style='padding-right:18px; color:{self.ctx.palette['muted']}'>{k}</td><td>{v}</td></tr>"
            for k, v in rows) + "</table>")

    def showEvent(self, event):
        super().showEvent(event)
        self._update_cache()
        if self._gpu == "checking…":
            run_async(gpu_name, on_done=self._gpu_done)
        self._render_about()

    def _gpu_done(self, name) -> None:
        self._gpu = "unknown" if isinstance(name, Exception) else name
        self._render_about()


# --- Main window ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    NAV = (("Dashboard", "grid"), ("Incidents", "shield"), ("Areas", "pentagon"), ("Integrations", "plug"),
           ("Live view", "video"), ("Sites & cameras", "server"), ("Settings", "sliders"))

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.focus_mode = False
        self._was_maximized = False
        self._mon_busy = False          # a start/stop/restart is running on the thread pool
        self._mon_wanted: bool | None = None  # what the user asked for while busy
        self._brain_ours = False        # monitoring started the brain, so it stops it too
        self.setWindowTitle("YardGuard")
        self.setMinimumSize(1040, 680)
        root = QWidget()
        root.setObjectName("Root")
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.sidebar = self._sidebar()
        row.addWidget(self.sidebar)
        self.stack = QStackedWidget()
        self.dashboard = DashboardPage(ctx, self)
        self.incidents = IncidentsPage(ctx, self)
        self.alerts = self.incidents.alerts  # the "All alerts" side of the Incidents page
        self.areas_page = AreasPage(ctx, self)
        self.live = LivePage(ctx, self)
        self.sites = SitesPage(ctx, self)
        self.settings = SettingsPage(ctx, self)
        self.integrations_page = IntegrationsPage(ctx, self)   # after settings: its brain link opens that page
        for page in (self.dashboard, self.incidents, self.areas_page, self.integrations_page, self.live, self.sites,
                     self.settings):  # same order as NAV
            self.stack.addWidget(page)
        self._wire_areas()
        row.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.statusBar().setSizeGripEnabled(False)

        for i in range(len(self.NAV)):
            shortcut(self, f"Ctrl+{i + 1}", lambda i=i: self.go(i))
        # A: add an area from any page (Live view on the last camera); text fields keep their "a"
        shortcut(self, "A", self._shortcut_add_area)
        ctx.config_changed.connect(self._config_changed)

        self.toasts = ToastManager(self, ctx)
        self.toasts.open_alert.connect(self._open_alert)
        self._held_toasts: dict = {}  # alert id -> alert waiting for the AI's verdict before popping up
        bridge = ctx.alert_bridge
        bridge.alert_added.connect(self._alert_added)
        bridge.alert_updated.connect(self._alert_updated)
        bridge.incident_added.connect(lambda *_: self._refresh_badge())
        bridge.incident_updated.connect(lambda *_: self._refresh_badge())
        bridge.status_changed.connect(self._status_changed)
        self.alerts.unread_changed.connect(lambda *_: self._refresh_badge())
        self.incidents.changed.connect(self._incidents_changed)
        self.pill.toggled.connect(self.set_monitoring)
        self._mon_timer = QTimer(self)
        self._mon_timer.setInterval(1500)
        self._mon_timer.timeout.connect(self._refresh_monitoring)
        self._mon_timer.start()
        self._wire_integrations()

        self._fill_sites()
        self.dashboard.reload()
        self.incidents.reload()
        self._refresh_badge()
        self.go(0)
        geo = ctx.ui.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1320, 840)
        self._refresh_monitoring()
        if ctx.cfg.settings.monitoring_on:  # restore the user's last choice once the window is up
            QTimer.singleShot(1200, lambda: self.set_monitoring(True))
        QTimer.singleShot(1500, ctx.start_integrations)  # after the window is up; a no-op when nothing is set up

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(232)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(16, 20, 16, 16)
        lay.setSpacing(4)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(logo_pixmap(30))
        brand.addWidget(logo)
        brand.addSpacing(6)
        brand.addWidget(label("YardGuard", "AppTitle"), 1)
        lay.addLayout(brand)
        lay.addSpacing(22)
        lay.addWidget(label("SITE", "SidebarCaption"))
        self.site_combo = QComboBox()
        self.site_combo.setAccessibleName("Active site")
        self.site_combo.setToolTip("Switch between places you watch")
        self.site_combo.currentIndexChanged.connect(self._site_picked)
        lay.addWidget(self.site_combo)
        lay.addSpacing(18)
        lay.addWidget(label("MENU", "SidebarCaption"))
        lay.addSpacing(2)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for i, (text, icon) in enumerate(self.NAV):
            b = QPushButton(f"  {text.replace('&', '&&')}")  # && shows a literal & (not a shortcut key)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"{text} (Ctrl+{i + 1})")
            self.ctx.bind_icon(b, icon, "nav")
            self.nav.addButton(b, i)
            lay.addWidget(b)
            if text == "Incidents":
                self.incident_badge = self._badge(b)
            elif text == "Integrations":
                self.integrations_badge = self._badge(b)   # "!" while a connection has a problem
        self.nav.idClicked.connect(self.go)
        lay.addStretch(1)
        self.pill = MonitorPill(self.ctx)
        lay.addWidget(self.pill)
        lay.addSpacing(12)
        lay.addWidget(muted(f"Version {APP_VERSION}", wrap=False))
        return side

    @staticmethod
    def _badge(nav_button: QPushButton) -> QLabel:
        """Unread count shown at the right end of a nav button."""
        row = QHBoxLayout(nav_button)
        row.setContentsMargins(0, 0, 10, 0)
        row.addStretch(1)
        badge = QLabel()
        badge.setObjectName("NavBadge")
        badge.setAlignment(Qt.AlignCenter)
        badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        badge.setVisible(False)
        row.addWidget(badge, 0, Qt.AlignVCenter)
        return badge

    def go(self, index: int) -> None:
        sites = self.stack.indexOf(self.sites)
        if self.stack.currentIndex() == sites and index != sites and self.sites.dirty \
                and not self.sites.confirm_leave():
            self.nav.button(sites).setChecked(True)
            return
        self.stack.setCurrentIndex(index)
        self.nav.button(index).setChecked(True)
        if self.stack.widget(index) is self.integrations_page:
            self._set_integrations_problem(False)  # the cards show the problem from here on

    # --- integrations --------------------------------------------------------------------

    def _wire_integrations(self) -> None:
        """Remote actions (Telegram buttons, phone, Home Assistant) run the same code as this window:
        the hub executes them on the GUI thread and calls these handlers."""
        ctx, hub = self.ctx, self.ctx.integrations
        if hub is not None:
            hub.set_handlers(set_monitoring=lambda on: self.set_monitoring(on), open_incident=self.open_incident)
            hub.incidents_changed.connect(self._incidents_marked_remotely)
            hub.problem.connect(self._integration_problem)
            self.incidents.changed.connect(hub.desktop_incidents_changed)   # edit sent messages to match
        if ctx.web is not None:
            self.incidents.changed.connect(lambda: ctx.web.notify_change("incident"))  # open phone pages refresh

    def _incidents_marked_remotely(self) -> None:
        """An incident was marked from Telegram, a phone or Home Assistant: show it here too."""
        self.incidents.reload()
        self.alerts.reload()
        self._incidents_changed()

    def _integration_problem(self, _dest_id: str, text: str) -> None:
        self.notify(text, error=True)
        if self.stack.currentWidget() is not self.integrations_page:
            self._set_integrations_problem(True)

    def _set_integrations_problem(self, on: bool) -> None:
        badge = self.integrations_badge
        badge.setText("!")
        badge.setVisible(on)
        i = self.stack.indexOf(self.integrations_page)
        b = self.nav.button(i)
        b.setToolTip(f"Integrations: a connection has a problem (Ctrl+{i + 1})" if on
                     else f"Integrations (Ctrl+{i + 1})")
        b.setAccessibleName("Integrations, a connection has a problem" if on else "Integrations")

    def go_page(self, page: QWidget) -> None:
        self.go(self.stack.indexOf(page))

    def open_live(self, channel: int) -> None:
        self.live.populate(select=channel)
        if self.stack.currentWidget() is self.live:
            self.live.start()
        self.go_page(self.live)

    # --- areas ---------------------------------------------------------------------------

    def _wire_areas(self) -> None:
        hub, live = self.areas_page, self.live.areas
        hub.edit_requested.connect(self._edit_area)
        hub.add_requested.connect(self._add_area)
        hub.open_incidents.connect(lambda _ch: self.go_page(self.incidents))
        live.open_log.connect(self._open_area_log)

    def _edit_area(self, channel: int, area_id: str) -> None:
        """Areas are edited on the live picture of their camera."""
        site = self.ctx.cfg.active_site()
        cam = site.camera(channel) if site else None
        if cam is None or not cam.enabled:
            self.notify("Tick Show for this camera in Sites & cameras to edit its areas in Live view.", error=True)
            return
        self.open_live(channel)
        self.live.areas.request(channel, "edit", area_id)

    def _shortcut_add_area(self) -> None:
        if not self.live.isVisible():
            self._add_area(-1)
        elif not self.live.areas.canvas.drafting:   # already drawing one: A does nothing
            self.live.areas.start_add()

    def _add_area(self, channel: int) -> None:
        """Hub "Add area": Live view on that camera (-1: the one shown last, else the first) in add mode."""
        site = self.ctx.cfg.active_site()
        cams = site.enabled_cameras() if site else []
        if not cams:
            self.notify("Add a camera in Sites & cameras first.", error=True)
            self.go_page(self.sites)
            return
        if channel < 0 or not any(c.channel == channel for c in cams):
            current = self.live.combo.currentData()
            channel = current if any(c.channel == current for c in cams) else cams[0].channel
        self.open_live(channel)
        self.live.areas.request(channel, "add")

    def _open_area_log(self, area_id: str) -> None:
        if self.focus_mode:
            self.toggle_focus(False)
        self.areas_page.show_area(area_id)
        self.go_page(self.areas_page)

    # --- alerts ------------------------------------------------------------------------

    def _unread(self) -> int:
        """Open incidents with alerts nobody has looked at yet."""
        try:
            return self.ctx.alert_store.unread_incident_count()
        except Exception:
            log.exception("Could not count unread incidents")
            return 0

    def _refresh_badge(self) -> None:
        count = self._unread()
        self.incident_badge.setText(str(count) if count < 100 else "99+")
        self.incident_badge.setVisible(count > 0)
        i = self.stack.indexOf(self.incidents)
        b = self.nav.button(i)
        b.setToolTip(f"Incidents: {count} new (Ctrl+{i + 1})" if count else f"Incidents (Ctrl+{i + 1})")
        b.setAccessibleName(f"Incidents, {count} new" if count else "Incidents")

    def _incidents_changed(self) -> None:
        self._refresh_badge()
        self.dashboard.strip.refresh()

    def _alert_added(self, alert) -> None:
        self._refresh_badge()
        verdict = getattr(alert, "verdict", "")
        if verdict == "rejected":
            return
        if verdict == "pending" and self.ctx.brain.status() in ("ready", "busy"):
            # Verify before notify: hold the pop-up until the AI brain has looked (a few seconds), so
            # alerts it dismisses never pop up. Shown anyway if no answer comes in time.
            self._held_toasts[alert.id] = alert
            QTimer.singleShot(TOAST_HOLD_MS, lambda a=alert: self._release_toast(a.id))
            return
        self.toasts.show_alert(alert)

    def _alert_updated(self, alert) -> None:
        if alert.id in self._held_toasts and alert.verdict != "pending":
            del self._held_toasts[alert.id]
            if alert.verdict != "rejected":
                self.toasts.show_alert(alert)
            return
        self.toasts.update_alert(alert)

    def _release_toast(self, alert_id: str) -> None:
        alert = self._held_toasts.pop(alert_id, None)
        if alert is not None:
            self.toasts.show_alert(alert)

    def _open_alert(self, alert_id: str) -> None:
        """Toast click: the incident that contains the alert, with that moment selected."""
        a = self.ctx.alert_store.get(alert_id)
        if a is None:
            self.notify("That alert was deleted.", error=True)
            return
        self.open_incident(a.incident_id, alert_id)

    def open_incident(self, incident_id: str, alert_id: str | None = None) -> None:
        if self.focus_mode:
            self.toggle_focus(False)
        # Select first: showing the page marks its selected incident seen, and that must be this one.
        self.incidents.show_incident(incident_id, alert_id)
        self.go_page(self.incidents)
        if self.stack.currentWidget() is self.incidents:
            self.incidents.list.setFocus()

    # --- monitoring ----------------------------------------------------------------------

    def monitor_status(self) -> dict[int, dict]:
        mon = self.ctx.monitor
        return mon.camera_status() if mon is not None and mon.running else {}

    def brain_user_action(self) -> None:
        """The user started or stopped the AI brain themselves (Settings): from now on it is theirs,
        so switching monitoring off does not stop it."""
        self._brain_ours = False

    def set_monitoring(self, on: bool, user: bool = True) -> None:
        """Turn live monitoring on or off (the choice is remembered for the next app start).
        user=False: an internal restart (events folder switch), which keeps a user's Stop of the brain."""
        s = self.ctx.cfg.settings
        if s.monitoring_on != on:
            s.monitoring_on = on
            self.ctx.cfg.save()
        self.pill.switch.set_checked_quietly(on)
        self.pill._show("busy", "Starting…" if on else "Stopping…", "")
        if self._mon_busy:
            self._mon_wanted = on
            return
        mon = self.ctx.monitor
        if on == mon.running:
            self._refresh_monitoring()
            return
        self._mon_busy = True
        brain = self.ctx.brain
        if on:
            if user:  # turning monitoring on again lifts an earlier "Stop AI brain"
                brain.user_stopped = False
            wanted = s.brain_mode != "off" and not brain.user_stopped
            # "or": an internal restart (events folder switch) keeps the brain running, and it
            # still belongs to monitoring. Stopping monitoring clears the flag.
            starts_brain = wanted and brain.status() in ("stopped", "error")
            self._brain_ours = self._brain_ours or starts_brain
            note = " The AI brain needs about 20 seconds to load." if starts_brain else ""
            self.notify("Monitoring is starting." + note)
            run_async(mon.start, on_done=lambda r: self._mon_done(r, "start"))
        else:
            ours, self._brain_ours = self._brain_ours, False

            def stop():
                mon.stop()
                if ours:
                    brain.stop()  # monitoring started it, so free its GPU memory again

            run_async(stop, on_done=lambda r: self._mon_done(r, "stop"))

    def _mon_done(self, result, what: str) -> None:
        self._mon_busy = False
        if isinstance(result, Exception):
            self.notify(f"Could not {what} monitoring: {result}", error=True)
        elif what == "stop":
            self.notify("Monitoring is off.")
        wanted, self._mon_wanted = self._mon_wanted, None
        if wanted is not None and wanted != self.ctx.monitor.running:
            self.set_monitoring(wanted)  # the user asked while busy
            return
        if self.ctx.monitor.running and self.ctx.monitor.needs_restart():
            self._restart_monitor("Settings saved.")  # a setting changed while starting / stopping
            return
        self._refresh_monitoring()
        self.alerts.reload()  # updates the empty state wording
        self.incidents.reload()

    def _restart_monitor(self, reason: str) -> None:
        mon = self.ctx.monitor
        if not mon.running or self._mon_busy:
            return
        self._mon_busy = True
        self.notify(f"{reason} Monitoring restarts with the new settings.")
        run_async(mon.restart, on_done=lambda r: self._mon_done(r, "restart"))

    def setting_changed(self, key: str) -> None:
        """A setting was autosaved (Settings page cards). Apply it to running monitoring."""
        mon, brain, s = self.ctx.monitor, self.ctx.brain, self.ctx.cfg.settings
        if key == "events_dir":
            self._switch_events_dir()
        elif key == "incident_gap_s":
            self.ctx.alert_store.incident_gap_s = float(s.incident_gap_s)  # new alerts use it at once
        elif key == "brain_mode" and mon.running:
            # A user's "Stop AI brain" stays in force: choosing the mode does not start it.
            if s.brain_mode != "off" and brain.status() in ("stopped", "error") and not brain.user_stopped:
                self._brain_ours = True
                run_async(brain.start)
                self.notify("Starting the AI brain. It needs about 20 seconds to load.")
            elif s.brain_mode == "off" and self._brain_ours:
                self._brain_ours = False
                run_async(brain.stop)
        elif key == "model":
            self._model_changed()
        if key != "events_dir" and mon.needs_restart():  # e.g. the brain mode decides GPU video decoding
            self._restart_monitor("Settings saved.")

    def _model_changed(self) -> None:
        """The main model was switched in AI models: the live view follows at once (monitoring
        restarts through needs_restart)."""
        if self.stack.currentWidget() is self.live:
            self.live.start(force=True)  # the live view switches detector too

    def _switch_events_dir(self) -> None:
        was = self.ctx.monitor.running
        self._mon_busy = True

        def done(result):
            self.ctx.make_monitor()
            if self.ctx.integrations is not None:
                self.ctx.integrations.events_dir_changed()  # its delivery log lives in the events folder
            self._mon_busy = False
            self.alerts.reload()
            self.incidents.reload()
            self.dashboard.strip.refresh()
            self._refresh_badge()
            if was:
                self.set_monitoring(True, user=False)

        run_async(self.ctx.monitor.stop, on_done=done)

    def _status_changed(self, status: dict) -> None:
        if self.stack.currentWidget() is self.dashboard and not self._mon_busy:
            self.dashboard.set_monitor_status(status if self.ctx.monitor.running else {})

    def _refresh_monitoring(self) -> None:
        if self._mon_busy:
            return
        mon, s = self.ctx.monitor, self.ctx.cfg.settings
        brain = "off" if s.brain_mode == "off" else self.ctx.brain.status()
        sm = mon.summary()
        self.pill.set_state(mon.running, sm["cameras"], sm["watching"], brain)
        problem = getattr(mon, "_detector_error", None)
        if mon.running and problem:
            self.pill._show("error", "The AI could not start", "")
            self.pill.setToolTip(problem)
        elif mon.running and sm.get("detector") == "cpu":
            self.pill.setToolTip(CPU_NOTE)
            if not getattr(self, "_cpu_noted", False):  # once per run: it is a fact about this PC
                self._cpu_noted = True
                self.notify(CPU_NOTE)
        if self.stack.currentWidget() is self.dashboard:
            self.dashboard.set_monitor_status(self.monitor_status())

    def open_wizard(self) -> None:
        wiz = SetupWizard(self.ctx, self)
        if wiz.exec():
            site = wiz.site
            self.ctx.cfg.sites.append(site)
            self.ctx.cfg.active_site_id = site.id
            self.ctx.save()
            self.go_page(self.dashboard)
            self.notify(f"Site “{site.name}” added.")

    def notify(self, text: str, error: bool = False) -> None:
        self.statusBar().showMessage(("⚠ " if error else "") + text, 8000)
        log.info(text)

    def toggle_focus(self, on: bool | None = None) -> None:
        on = (not self.focus_mode) if on is None else on
        if on == self.focus_mode:
            return
        self.focus_mode = on
        self.sidebar.setVisible(not on)
        self.statusBar().setVisible(not on)
        self.live.set_focus_layout(on)
        if on:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        elif self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def _fill_sites(self) -> None:
        cfg = self.ctx.cfg
        self.site_combo.blockSignals(True)
        self.site_combo.clear()
        for s in cfg.sites:
            self.site_combo.addItem(s.name, s.id)
        active = cfg.active_site()
        if active:
            self.site_combo.setCurrentIndex(self.site_combo.findData(active.id))
        else:
            self.site_combo.addItem("No site yet")
        self.site_combo.setEnabled(bool(cfg.sites))
        self.site_combo.blockSignals(False)

    def _site_picked(self) -> None:
        site_id = self.site_combo.currentData()
        if site_id and site_id != self.ctx.cfg.active_site_id:
            self.ctx.cfg.active_site_id = site_id
            self.ctx.save()

    def _config_changed(self) -> None:
        self._fill_sites()
        if self.stack.currentWidget() is self.dashboard:
            self.dashboard.reload()
        else:  # e.g. an area saved in Live view: no camera previews fetched for a hidden page
            self.dashboard.stale = True
        if self.stack.currentWidget() is self.sites:
            self.sites.refresh()
        if self.stack.currentWidget() is self.live:
            self.live.populate()
            self.live.start()
        if self.ctx.monitor.needs_restart():
            self._restart_monitor("Cameras changed.")

    def closeEvent(self, event):
        if self.stack.currentWidget() is self.sites and self.sites.dirty and not self.sites.confirm_leave():
            event.ignore()
            return
        if not self.focus_mode:
            self.ctx.ui.setValue("geometry", self.saveGeometry())
        self._mon_timer.stop()
        self.toasts.clear()
        self.live.stop_all()
        # Before the monitor stops: phone live streams and snapshots read its frames. Unsent messages
        # stay in the delivery log and go out at the next start.
        if self.integrations_page.routes is not None:
            self.integrations_page.routes.flush()  # a "What to send" change still waiting to be saved
        self.ctx.stop_integrations()
        pool = QThreadPool.globalInstance()
        pool.clear()             # drop queued previews
        pool.waitForDone(7000)   # let running camera opens (and a monitoring start/stop) finish before exit
        # Brain first: its pending questions fail at once, so the monitor's brain thread exits quickly.
        # settings.monitoring_on is left as it is, so monitoring comes back on at the next start.
        try:
            self.ctx.brain.stop()
            self.ctx.monitor.stop()
        except Exception:
            log.exception("Could not stop monitoring cleanly")
        super().closeEvent(event)


def _excepthook(exc_type, exc, tb):
    log.error("Unhandled error", exc_info=(exc_type, exc, tb))
    if QApplication.instance():
        QMessageBox.critical(None, "YardGuard error",
                             f"Something went wrong:\n{exc}\n\nDetails were saved to {CACHE / 'yardguard.log'}")


def run() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=CACHE / "yardguard.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    sys.excepthook = _excepthook
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YardGuard.App")  # own taskbar icon
    app = QApplication(sys.argv)
    app.setApplicationName("YardGuard")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(app_icon())
    QThreadPool.globalInstance().setMaxThreadCount(6)
    ctx = AppContext(app)
    ctx.apply_theme()
    win = MainWindow(ctx)
    win.show()
    if not ctx.cfg.sites:
        QTimer.singleShot(400, win.open_wizard)
    return app.exec()
