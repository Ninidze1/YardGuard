"""'What to send' tab of the Integrations page: per destination, which events become messages
(incidents, areas, plates, camera problems, daily summary), with pictures, quiet hours and limits.

Every change is written into the destination's routes dict (Settings.integrations[i]["routes"],
format in integrations.policy) and saved through hub.update_destination(id), debounced 600 ms.
Picking "Only important" or "Everything" applies policy.apply_preset; any other edit makes the
preset "custom".
"""
from __future__ import annotations

import copy
import logging
import time

from PySide6.QtCore import Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QTimeEdit, QVBoxLayout, QWidget,
)

from .alerts_ui import Switch
from .areas_canvas import KIND_NAMES, TASK_KINDS
from .areas_ui import Choice, _FlowRow
from .config import TEMPLATE_INFO
from .incidents_ui import Segmented
from .integrations_setup import ElidedLabel, caps, catalog, dest_labels, integ, link_button, safe_text
from .qt_util import run_async, set_prop
from .settings_ui import _ModeOption
from .widgets import EmptyState, button, card, label, muted, spin_box

log = logging.getLogger("yardguard")

SAVE_DELAY_MS = 600
LEVELS = ("critical", "warning", "info")                  # Choice order
LEVEL_TEXT = ("Critical only", "Critical and warning", "Everything")
UPDATES = ("edit", "each", "quiet")
PICTURES = ("crop", "snapshot", "both", "none")
PICTURE_TEXT = ("Close-up", "Whole picture", "Both", "Text only")
AREA_MODES = ("off", "instant", "summary")
AREA_MODE_TEXT = ("Off", "Every time", "Daily summary")
PLATES = ("off", "unknown", "known", "all")
PLATE_TEXT = ("Off", "Unknown plates", "Known plates (e.g. tell me when my son is home)", "All plates")
QUIET_MODES = ("silent", "hold", "drop")
QUIET_TEXT = ("Send silently", "Hold and send a summary at the end", "Do not send")
DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
PRESETS = (("important", "Only important", "Incidents that matter (warning and critical), camera problems. One "
                                           "message per incident, updated as it grows.", True),
           ("everything", "Everything", "Every incident, every area event and plate, plus a daily summary.", False),
           ("custom", "Custom", "Your own choices below. Changing anything below selects this.", False))
# areas.ALERT_KINDS (read lazily: another part of the app owns areas.py); used when an area's kinds are []
ALERT_KINDS_FALLBACK = {"enter", "cross_in", "cross_out", "occupied", "blocked", "loitering", "presence", "plate",
                        "appeared", "removed", "changed", "answer_yes"}


def alert_kinds() -> set:
    try:
        from .areas import ALERT_KINDS
        return set(ALERT_KINDS)
    except Exception:
        return set(ALERT_KINDS_FALLBACK)


def area_kinds(area: dict) -> tuple:
    kinds = TASK_KINDS.get(area.get("task", ""), ())
    if area.get("task") == "count":
        line = area.get("shape") == "line"
        kinds = tuple(k for k in kinds if (k in ("cross_in", "cross_out")) == line)
    return kinds


def ensure_routes(routes: dict, kind: str = "") -> dict:
    """Fill missing keys (older or hand-edited configs) so the editor can read every value."""
    policy = integ("policy")
    if policy is not None and hasattr(policy, "normalize_routes"):
        try:
            return policy.normalize_routes(routes, kind)
        except Exception:
            log.exception("Normalising routes failed")
    defaults = {}
    if policy is not None:
        try:
            defaults = policy.default_routes(kind)
        except Exception:
            log.exception("Default routes failed")
    fallback = {
        "preset": "important",
        "incidents": {"enabled": True, "min_severity": "warning", "confirmed_only": True, "send_unchecked": True,
                      "updates": "edit", "cameras": []},
        "areas": {}, "plates": "off", "offline": {"enabled": True, "minutes": 5, "back": True},
        "health": {"enabled": True}, "summary": {"enabled": False, "time": "08:00"}, "pictures": "crop",
        "clip": False,
        "quiet": {"enabled": False, "start": "23:00", "end": "07:00", "days": [0, 1, 2, 3, 4, 5, 6],
                  "mode": "silent", "critical_through": True},
        "rate": {"cooldown_s": 60, "max_per_10min": 10},
    }
    for key, value in fallback.items():
        value = copy.deepcopy(defaults.get(key, value))
        if key not in routes or type(routes[key]) is not type(value):
            routes[key] = value
        elif isinstance(value, dict):
            for k, v in value.items():
                routes[key].setdefault(k, v)
    return routes


def _qtime(text: str, fallback: str) -> QTime:
    t = QTime.fromString(str(text or ""), "HH:mm")
    return t if t.isValid() else QTime.fromString(fallback, "HH:mm")


def _time_edit(name: str) -> QTimeEdit:
    edit = QTimeEdit()
    edit.setDisplayFormat("HH:mm")
    edit.setFixedWidth(90)
    edit.setAccessibleName(name)
    # no setButtonSymbols(NoButtons): under a style sheet it collapses the text (settings_ui hides the arrows)
    edit.setToolTip("Type a time, or use the arrow keys / mouse wheel")
    return edit


def _chip_button(text: str, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("KindChip")
    b.setCheckable(True)
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


def _row(*widgets, stretch: bool = True, spacing: int = 8) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(spacing)
    for w in widgets:
        if isinstance(w, str):
            row.addWidget(QLabel(w))
        elif isinstance(w, int):
            row.addSpacing(w)
        else:
            row.addWidget(w)
    if stretch:
        row.addStretch(1)
    return row


def _section(title: str, subtitle: str = "", switch=None) -> tuple[QFrame, QVBoxLayout]:
    """Card with a title row (and a Switch at its right end)."""
    frame, lay = card()
    head = QHBoxLayout()
    head.setSpacing(12)
    col = QVBoxLayout()
    col.setSpacing(2)
    col.addWidget(label(title, "SectionTitle"))
    if subtitle:
        col.addWidget(muted(subtitle))
    head.addLayout(col, 1)
    if switch is not None:
        head.addWidget(switch, 0, Qt.AlignTop)
    lay.addLayout(head)
    return frame, lay


def _field_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFixedWidth(180)
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    lbl.setContentsMargins(0, 6, 0, 0)
    return lbl


class RoutesEditor(QWidget):
    """Destination picker plus cards; edits the selected destination's routes in place."""

    go_connections = Signal()

    def __init__(self, ctx, hub, win):
        super().__init__()
        self.ctx, self.hub, self.win = ctx, hub, win
        self.dest_id = ""
        self._loading = False
        self._dirty_ids: set[str] = set()
        self._summary_cache: tuple[float, str] = (0.0, "")
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self.flush)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(label("Send to", "CardTitle"))
        self.picker_host = QHBoxLayout()
        self.picker_host.setSpacing(0)
        bar.addLayout(self.picker_host)
        self.copy_btn = button("Copy settings to…", "ghost", "Use these choices for another destination too")
        ctx.bind_icon(self.copy_btn, "copy", "muted")
        self.copy_btn.clicked.connect(self._copy_menu)
        bar.addWidget(self.copy_btn)
        bar.addStretch(1)
        self.saved_lbl = muted("", wrap=False)
        bar.addWidget(self.saved_lbl)
        self.bar = QWidget()
        self.bar.setLayout(bar)
        outer.addWidget(self.bar)
        self.picker = None

        self.empty = EmptyState(ctx)
        self.empty.configure("send", "Nothing connected yet",
                             "Connect Telegram, phone push or email first, then choose here what YardGuard sends.",
                             "Go to Connections", self.go_connections.emit)
        outer.addWidget(self.empty, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("Transparent")
        self.col = QVBoxLayout(host)
        self.col.setContentsMargins(0, 0, 8, 8)
        self.col.setSpacing(16)
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)

        self._build_presets()
        self._build_incidents()
        self._build_pictures()
        self._build_areas()
        self._build_plates()
        self._build_status()
        self._build_summary()
        self._build_quiet()
        self._build_limits()
        self.col.addStretch(1)
        self.reload()

    # --- building -------------------------------------------------------------------------------
    def _build_presets(self) -> None:
        frame, lay = _section("How much", "Start from a ready choice, then fine-tune anything below.")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.preset_group = QButtonGroup(self)
        self.preset_opts: dict[str, _ModeOption] = {}
        for i, (key, title, text, rec) in enumerate(PRESETS):
            opt = _ModeOption(title if key != "important" else "Only important", text, rec, self.preset_group, i)
            opt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.preset_opts[key] = opt
            row.addWidget(opt, 1)
        self.preset_group.idClicked.connect(self._preset_clicked)
        lay.addLayout(row)
        self.explain_lbl = muted()
        self.explain_lbl.setTextFormat(Qt.RichText)
        lay.addWidget(self.explain_lbl)
        self.col.addWidget(frame)

    def _build_incidents(self) -> None:
        self.inc_switch = Switch(self.ctx)
        self.inc_switch.setAccessibleName("Send incidents")
        frame, lay = _section("Incidents", "Break-ins, people near cars at night and other alerts YardGuard groups "
                                           "into incidents.", self.inc_switch)
        self.inc_body = QWidget()
        bl = QVBoxLayout(self.inc_body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(12)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_field_label("Which incidents"))
        self.level = Choice(list(LEVEL_TEXT))
        self.level.setFixedWidth(260)
        self.level.setAccessibleName("Which incidents")
        row.addWidget(self.level)
        row.addStretch(1)
        bl.addLayout(row)
        self.confirmed = QCheckBox("Only when the AI brain confirms it (fewer false alarms, may arrive about "
                                   "10 s later)")
        self.unchecked = QCheckBox("If the AI does not answer within a minute, send it anyway")
        self.brain_link = link_button("Turn on the AI brain in Settings")
        self.brain_link.clicked.connect(self._open_settings)
        ai = QVBoxLayout()
        ai.setSpacing(6)
        ai.addWidget(self.confirmed)
        sub = QHBoxLayout()
        sub.setContentsMargins(26, 0, 0, 0)
        sub.addWidget(self.unchecked)
        sub.addStretch(1)
        ai.addLayout(sub)
        ai.addWidget(self.brain_link, 0, Qt.AlignLeft)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_field_label("AI check"))
        row.addLayout(ai, 1)
        bl.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_field_label("When more happens in the same incident"))
        self.updates = Choice(["Update the same message", "Send each alert", "Stay quiet"])
        self.updates.setFixedWidth(260)
        self.updates.setAccessibleName("When more happens in the same incident")
        row.addWidget(self.updates)
        row.addStretch(1)
        bl.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_field_label("Cameras"))
        cams = QVBoxLayout()
        cams.setSpacing(6)
        self.all_cams = QCheckBox("All cameras")
        cams.addWidget(self.all_cams)
        self.cam_flow = _FlowRow()
        cams.addWidget(self.cam_flow)
        self.cam_boxes: dict[int, QCheckBox] = {}
        row.addLayout(cams, 1)
        bl.addLayout(row)
        lay.addWidget(self.inc_body)
        self.inc_switch.toggled.connect(self._edited)
        self.level.changed.connect(self._edited)
        self.confirmed.toggled.connect(self._edited)
        self.unchecked.toggled.connect(self._edited)
        self.updates.changed.connect(self._edited)
        self.all_cams.toggled.connect(self._all_cams_toggled)
        self.col.addWidget(frame)

    def _build_pictures(self) -> None:
        frame, lay = _section("Pictures")
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_field_label("Picture with each message"))
        self.pictures = Segmented([(t, "") for t in PICTURE_TEXT])
        self.pictures.setAccessibleName("Picture with each message")
        row.addWidget(self.pictures)
        row.addStretch(1)
        lay.addLayout(row)
        self.clip = QCheckBox("Also send the video clip when it is ready")
        self.clip_row = QWidget()
        cl = QHBoxLayout(self.clip_row)
        cl.setContentsMargins(196, 0, 0, 0)
        cl.addWidget(self.clip)
        cl.addStretch(1)
        lay.addWidget(self.clip_row)
        self.size_hint = muted()
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(196, 0, 0, 0)
        hint_row.addWidget(self.size_hint, 1)
        lay.addLayout(hint_row)
        self.pictures.changed.connect(self._edited)
        self.clip.toggled.connect(self._edited)
        self.pictures_card = frame
        self.col.addWidget(frame)

    def _build_areas(self) -> None:
        frame, lay = _section("Areas", "Events of the areas you drew in Live view: counts, parking spots, things "
                                       "left behind…")
        self.areas_box = QVBoxLayout()
        self.areas_box.setSpacing(8)
        lay.addLayout(self.areas_box)
        self.area_widgets: dict[str, dict] = {}
        self.col.addWidget(frame)

    def _build_plates(self) -> None:
        frame, lay = _section("Number plates", "From areas that log number plates.")
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_field_label("Tell me about"))
        self.plates = Choice(list(PLATE_TEXT))
        self.plates.setFixedWidth(400)
        self.plates.setAccessibleName("Number plates to tell me about")
        row.addWidget(self.plates)
        row.addStretch(1)
        lay.addLayout(row)
        self.known_link = link_button("Edit known plates")
        self.known_link.clicked.connect(self._open_areas)
        kl = QHBoxLayout()
        kl.setContentsMargins(192, 0, 0, 0)
        kl.addWidget(self.known_link)
        kl.addStretch(1)
        lay.addLayout(kl)
        self.plates.changed.connect(self._edited)
        self.plates_card = frame
        self.col.addWidget(frame)

    def _build_status(self) -> None:
        frame, lay = _section("Camera problems and YardGuard status")
        self.offline = QCheckBox("Tell me if a camera stops sending video for more than")
        self.offline_min = spin_box(1, 60, 5)
        self.offline_min.setFixedWidth(80)
        self.offline_min.setAccessibleName("Minutes without video")
        lay.addLayout(_row(self.offline, self.offline_min, "minutes"))
        self.back = QCheckBox("and when it is back")
        lay.addLayout(_row(26, self.back))
        self.health = QCheckBox("Tell me when YardGuard was not running or monitoring was switched off")
        lay.addWidget(self.health)
        self.offline.toggled.connect(self._edited)
        self.offline_min.valueChanged.connect(self._edited)
        self.back.toggled.connect(self._edited)
        self.health.toggled.connect(self._edited)
        self.col.addWidget(frame)

    def _build_summary(self) -> None:
        self.sum_switch = Switch(self.ctx)
        self.sum_switch.setAccessibleName("Daily summary")
        frame, lay = _section("Daily summary", "One message a day with what happened in the last 24 hours.",
                              self.sum_switch)
        self.sum_time = _time_edit("Daily summary time")
        lay.addLayout(_row("Send it at", self.sum_time))
        self.sum_preview = QLabel()
        self.sum_preview.setObjectName("PreviewBox")
        self.sum_preview.setWordWrap(True)
        self.sum_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.sum_preview)
        self.sum_switch.toggled.connect(self._edited)
        self.sum_time.timeChanged.connect(self._edited)
        self.col.addWidget(frame)

    def _build_quiet(self) -> None:
        self.quiet_switch = Switch(self.ctx)
        self.quiet_switch.setAccessibleName("Quiet hours")
        frame, lay = _section("Quiet hours", "Fewer messages at night or while you sleep.", self.quiet_switch)
        self.quiet_body = QWidget()
        ql = QVBoxLayout(self.quiet_body)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.setSpacing(10)
        self.q_start = _time_edit("Quiet hours from")
        self.q_end = _time_edit("Quiet hours to")
        ql.addLayout(_row(_field_label_inline("From"), self.q_start, "to", self.q_end))
        days = QHBoxLayout()
        days.setSpacing(4)
        days.addWidget(_field_label_inline("On"))
        self.day_btns = []
        for i, d in enumerate(DAYS):
            b = _chip_button(d, "Quiet hours that start on this day")
            b.setAccessibleName(f"Quiet hours on {d}")
            b.toggled.connect(self._edited)
            self.day_btns.append(b)
            days.addWidget(b)
        days.addStretch(1)
        ql.addLayout(days)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_field_label("During quiet hours"))
        self.q_mode = Choice(list(QUIET_TEXT))
        self.q_mode.setFixedWidth(330)
        self.q_mode.setAccessibleName("During quiet hours")
        row.addWidget(self.q_mode)
        row.addStretch(1)
        ql.addLayout(row)
        self.q_critical = QCheckBox("Critical alerts always get through (break-ins still wake you up)")
        ql.addWidget(self.q_critical)
        ql.addWidget(muted("Quiet hours are not the same as YardGuard's night hours in Settings: night hours decide "
                           "when YardGuard watches harder; quiet hours decide when it messages you less."))
        lay.addWidget(self.quiet_body)
        self.quiet_switch.toggled.connect(self._edited)
        self.q_start.timeChanged.connect(self._edited)
        self.q_end.timeChanged.connect(self._edited)
        self.q_mode.changed.connect(self._edited)
        self.q_critical.toggled.connect(self._edited)
        self.col.addWidget(frame)

    def _build_limits(self) -> None:
        frame, lay = _section("Limits", "So a busy evening does not flood your phone.")
        self.cooldown = spin_box(0, 3600, 60)
        self.cooldown.setFixedWidth(90)
        self.cooldown.setAccessibleName("Seconds between messages per camera")
        lay.addLayout(_row("At most one message per camera every", self.cooldown, "seconds"))
        self.cap = spin_box(1, 100, 10)
        self.cap.setFixedWidth(80)
        self.cap.setAccessibleName("Messages in 10 minutes")
        lay.addLayout(_row("At most", self.cap, "messages in 10 minutes; the rest are combined into one message"))
        self.cooldown.valueChanged.connect(self._edited)
        self.cap.valueChanged.connect(self._edited)
        self.col.addWidget(frame)

    # --- destinations ---------------------------------------------------------------------------
    def destinations(self) -> list:
        try:
            return list(self.hub.destinations()) if self.hub is not None else []
        except Exception:
            log.exception("Reading destinations failed")
            return []

    def conf(self):
        return next((d for d in self.destinations() if d.id == self.dest_id), None)

    def reload(self) -> None:
        """Destinations changed (added, renamed, removed): rebuild the picker, keep the selection."""
        dests = self.destinations()
        has = bool(dests)
        self.empty.setVisible(not has)
        self.scroll.setVisible(has)
        self.bar.setVisible(has)
        if self.picker is not None:
            self.picker_host.removeWidget(self.picker)
            self.picker.deleteLater()
            self.picker = None
        if not has:
            self.dest_id = ""
            return
        if self.dest_id not in {d.id for d in dests}:
            self.dest_id = dests[0].id
        labels = dest_labels(dests)
        names = [labels[d.id] for d in dests]
        if len(dests) <= 4:
            self.picker = Segmented([(n, f"What YardGuard sends to {n}") for n in names])
            self.picker.setAccessibleName("Destination")
            self.picker.set_current([d.id for d in dests].index(self.dest_id))
            self.picker.changed.connect(lambda i: self.select(self.destinations()[i].id))
        else:
            self.picker = QComboBox()
            self.picker.setAccessibleName("Destination")
            for d, n in zip(dests, names):
                self.picker.addItem(n, d.id)
            self.picker.setCurrentIndex(max(0, self.picker.findData(self.dest_id)))
            self.picker.currentIndexChanged.connect(lambda _i: self.select(self.picker.currentData()))
        self.picker_host.addWidget(self.picker)
        self.copy_btn.setVisible(len(dests) > 1)
        self._load()

    def select(self, dest_id: str) -> None:
        if not dest_id:
            return
        if dest_id != self.dest_id:
            self.flush()
            self.dest_id = dest_id
            self._summary_cache = (0.0, "")
        ids = [d.id for d in self.destinations()]
        if isinstance(self.picker, Segmented) and dest_id in ids:
            self.picker.set_current(ids.index(dest_id))
        elif isinstance(self.picker, QComboBox):
            self.picker.blockSignals(True)
            self.picker.setCurrentIndex(max(0, self.picker.findData(dest_id)))
            self.picker.blockSignals(False)
        self._load()

    # --- load -----------------------------------------------------------------------------------
    def _site(self):
        return self.ctx.cfg.active_site()

    def _load(self) -> None:
        conf = self.conf()
        if conf is None:
            return
        r = ensure_routes(conf.routes, conf.kind)
        cap = caps(conf.kind)
        settings = self.ctx.cfg.settings
        self._loading = True
        try:
            preset = r.get("preset", "custom")
            self.preset_opts.get(preset, self.preset_opts["custom"]).radio.setChecked(True)
            inc = r["incidents"]
            self.inc_switch.set_checked_quietly(bool(inc.get("enabled", True)))
            self.level.set_current(LEVELS.index(inc.get("min_severity")) if inc.get("min_severity") in LEVELS else 1)
            brain_on = getattr(settings, "brain_mode", "off") != "off"
            self.confirmed.setChecked(bool(inc.get("confirmed_only", True)))
            self.unchecked.setChecked(bool(inc.get("send_unchecked", True)))
            self.confirmed.setEnabled(brain_on)
            self.brain_link.setVisible(not brain_on)
            self.updates.set_text(0, "Update the same message" if cap.get("edit") else "Only if it gets more serious")
            self.updates.set_current(UPDATES.index(inc.get("updates")) if inc.get("updates") in UPDATES else 0)
            self._load_cameras(inc.get("cameras") or [])
            self.pictures.set_current(PICTURES.index(r["pictures"]) if r["pictures"] in PICTURES else 0)
            self.pictures_card.setVisible(bool(cap.get("pictures", True)))
            self.clip_row.setVisible(bool(cap.get("clip")))
            self.clip.setChecked(bool(r.get("clip")))
            self._load_areas(r["areas"])
            self.plates.set_current(PLATES.index(r["plates"]) if r["plates"] in PLATES else 0)
            off = r["offline"]
            self.offline.setChecked(bool(off.get("enabled", True)))
            self.offline_min.setValue(int(off.get("minutes", 5) or 5))
            self.back.setChecked(bool(off.get("back", True)))
            self.health.setChecked(bool(r["health"].get("enabled", True)))
            sm = r["summary"]
            self.sum_switch.set_checked_quietly(bool(sm.get("enabled")))
            self.sum_time.setTime(_qtime(sm.get("time"), "08:00"))
            q = r["quiet"]
            self.quiet_switch.set_checked_quietly(bool(q.get("enabled")))
            self.q_start.setTime(_qtime(q.get("start"), "23:00"))
            self.q_end.setTime(_qtime(q.get("end"), "07:00"))
            days = set(q.get("days") if isinstance(q.get("days"), list) else range(7))
            for i, b in enumerate(self.day_btns):
                b.setChecked(i in days)
            self.q_mode.set_current(QUIET_MODES.index(q.get("mode")) if q.get("mode") in QUIET_MODES else 0)
            self.q_critical.setChecked(bool(q.get("critical_through", True)))
            rate = r["rate"]
            self.cooldown.setValue(int(rate.get("cooldown_s", 60) or 0))
            self.cap.setValue(int(rate.get("max_per_10min", 10) or 10))
        finally:
            self._loading = False
        self._sync_enabled()
        self._explain()
        self._refresh_summary()

    def _load_cameras(self, chosen: list) -> None:
        self.cam_flow.clear()
        self.cam_boxes = {}
        site = self._site()
        cams = site.enabled_cameras() if site else []
        chosen = {int(c) for c in chosen if str(c).lstrip("-").isdigit()}
        for cam in cams:
            box = QCheckBox(cam.label or f"Camera {cam.channel}")
            box.setChecked(not chosen or cam.channel in chosen)
            box.toggled.connect(self._edited)
            self.cam_boxes[cam.channel] = box
            self.cam_flow.add(box)
        self.all_cams.setChecked(not chosen)
        self.cam_flow.setVisible(bool(chosen) and bool(cams))
        self.all_cams.setEnabled(bool(cams))

    def _load_areas(self, routes_areas: dict) -> None:
        while self.areas_box.count():
            item = self.areas_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
            elif item.layout() is not None:
                _clear(item.layout())
        self.area_widgets = {}
        site = self._site()
        has_plates = False
        any_area = False
        alert_set = alert_kinds()
        for cam in (site.enabled_cameras() if site else []):
            areas = [a for a in (getattr(cam, "areas", None) or []) if isinstance(a, dict) and a.get("enabled", True)]
            if not areas:
                continue
            any_area = True
            head = label(cam.label or f"Camera {cam.channel}", "AreaCamHead")
            self.areas_box.addWidget(head)
            for area in areas:
                has_plates = has_plates or area.get("task") == "log_plates"
                self.areas_box.addWidget(self._area_row(area, routes_areas.get(area.get("id"), {}), alert_set))
        if not any_area:
            empty = QWidget()
            el = QHBoxLayout(empty)
            el.setContentsMargins(0, 0, 0, 0)
            el.addWidget(muted("No areas yet - draw one in Areas.", wrap=False))
            link = link_button("Open Areas")
            link.clicked.connect(self._open_areas)
            el.addWidget(link)
            el.addStretch(1)
            self.areas_box.addWidget(empty)
        self.plates_card.setVisible(has_plates)

    def _area_row(self, area: dict, rule: dict, alert_set: set) -> QWidget:
        row = QFrame()
        row.setObjectName("AreaRouteRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(12)
        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        name = ElidedLabel(area.get("name") or "Area", "CardTitle")
        name.setFixedWidth(200)
        name_col.addWidget(name)
        task = TEMPLATE_INFO.get(area.get("task", ""), {}).get("title", "")
        t = ElidedLabel(task, muted_text=True)
        t.setFixedWidth(200)
        name_col.addWidget(t)
        lay.addLayout(name_col)
        kinds = area_kinds(area)
        if area.get("alert"):
            lay.addWidget(muted("Already sent as an incident", wrap=False))
            lay.addStretch(1)
            return row
        if not kinds:
            lay.addWidget(muted("Nothing to send for this area", wrap=False))
            lay.addStretch(1)
            return row
        mode = Segmented([(t, "") for t in AREA_MODE_TEXT])
        for b in mode.group.buttons():
            set_prop(b, "compact", True)
        mode.setAccessibleName(f"Send events of {area.get('name')}")
        mode.set_current(AREA_MODES.index(rule.get("mode")) if rule.get("mode") in AREA_MODES else 0)
        lay.addWidget(mode)
        chips = _FlowRow()
        chosen = rule.get("kinds") or []
        chip_btns = {}
        for k in kinds:
            b = _chip_button(KIND_NAMES.get(k, k), "Send this kind of event")
            b.setChecked(k in chosen if chosen else k in alert_set)
            b.toggled.connect(self._edited)
            chip_btns[k] = b
            chips.add(b)
        lay.addWidget(chips, 1)
        mode.changed.connect(self._edited)
        self.area_widgets[area["id"]] = {"mode": mode, "chips": chip_btns, "kinds": kinds}
        for b in chip_btns.values():
            b.setEnabled(mode.current() != 0)
        return row

    # --- edits ----------------------------------------------------------------------------------
    def _sync_enabled(self) -> None:
        self.inc_body.setEnabled(self.inc_switch.isChecked())
        brain_on = getattr(self.ctx.cfg.settings, "brain_mode", "off") != "off"
        self.unchecked.setEnabled(brain_on and self.confirmed.isChecked())
        self.offline_min.setEnabled(self.offline.isChecked())
        self.back.setEnabled(self.offline.isChecked())
        self.sum_time.setEnabled(self.sum_switch.isChecked())
        self.sum_preview.setEnabled(self.sum_switch.isChecked())
        self.quiet_body.setEnabled(self.quiet_switch.isChecked())
        for w in self.area_widgets.values():
            for b in w["chips"].values():
                b.setEnabled(w["mode"].current() != 0)
        pics = PICTURES[max(0, self.pictures.current())]
        conf = self.conf()
        kind = conf.kind if conf else ""
        hint = {"none": "Messages arrive as text only.",
                "both": "The close-up and the whole picture are sent together."}.get(
            pics, "Pictures are made smaller before sending, so they arrive quickly.")
        if kind == "telegram" and pics == "both":
            hint += " Telegram shows them as an album with the buttons in a second message."
        self.size_hint.setText(hint)

    def _all_cams_toggled(self, on: bool) -> None:
        if self._loading:
            return
        self._loading = True
        for b in self.cam_boxes.values():
            b.setChecked(True)
        self._loading = False
        self.cam_flow.setVisible(not on and bool(self.cam_boxes))
        self._edited()

    def _edited(self, *_) -> None:
        if self._loading:
            return
        conf = self.conf()
        if conf is None:
            return
        self._collect(conf.routes)
        conf.routes["preset"] = "custom"
        self._loading = True
        self.preset_opts["custom"].radio.setChecked(True)
        self._loading = False
        self._sync_enabled()
        self._explain()
        self._schedule_save()

    def _collect(self, r: dict) -> None:
        inc = r.setdefault("incidents", {})
        inc["enabled"] = self.inc_switch.isChecked()
        inc["min_severity"] = LEVELS[max(0, self.level.current())]
        inc["confirmed_only"] = self.confirmed.isChecked()
        inc["send_unchecked"] = self.unchecked.isChecked()
        inc["updates"] = UPDATES[max(0, self.updates.current())]
        if self.all_cams.isChecked() or not self.cam_boxes:
            inc["cameras"] = []
        else:
            picked = [ch for ch, b in self.cam_boxes.items() if b.isChecked()]
            if not picked:   # no camera at all would silently mean "all": keep the last one ticked
                first = next(iter(self.cam_boxes))
                self._loading = True
                self.cam_boxes[first].setChecked(True)
                self._loading = False
                picked = [first]
            inc["cameras"] = [] if len(picked) == len(self.cam_boxes) else picked
        r["pictures"] = PICTURES[max(0, self.pictures.current())]
        r["clip"] = self.clip.isChecked()
        areas = r.setdefault("areas", {})
        alert_set = alert_kinds()
        for area_id, w in self.area_widgets.items():
            mode = AREA_MODES[max(0, w["mode"].current())]
            picked = [k for k, b in w["chips"].items() if b.isChecked()]
            default = [k for k in w["kinds"] if k in alert_set]
            if not picked and mode != "off":
                # nothing ticked would read as "the usual kinds" ([]): it means off instead
                mode = "off"
                self._loading = True
                w["mode"].set_current(0)
                for k, b in w["chips"].items():
                    b.setChecked(k in default)
                self._loading = False
                picked = default
            areas[area_id] = {"mode": mode, "kinds": [] if sorted(picked) == sorted(default) else picked}
        r["plates"] = PLATES[max(0, self.plates.current())]
        r["offline"] = {"enabled": self.offline.isChecked(), "minutes": self.offline_min.value(),
                        "back": self.back.isChecked()}
        r["health"] = {**(r.get("health") or {}), "enabled": self.health.isChecked()}
        r["summary"] = {**(r.get("summary") or {}), "enabled": self.sum_switch.isChecked(),
                        "time": self.sum_time.time().toString("HH:mm")}
        r["quiet"] = {"enabled": self.quiet_switch.isChecked(), "start": self.q_start.time().toString("HH:mm"),
                      "end": self.q_end.time().toString("HH:mm"),
                      "days": [i for i, b in enumerate(self.day_btns) if b.isChecked()],
                      "mode": QUIET_MODES[max(0, self.q_mode.current())],
                      "critical_through": self.q_critical.isChecked()}
        r["rate"] = {"cooldown_s": self.cooldown.value(), "max_per_10min": self.cap.value()}

    def _preset_clicked(self, i: int) -> None:
        if self._loading:
            return
        conf = self.conf()
        if conf is None:
            return
        key = PRESETS[i][0]
        if key == "custom":
            conf.routes["preset"] = "custom"
            self._explain()
            self._schedule_save()
            return
        policy = integ("policy")
        try:
            new = policy.apply_preset(copy.deepcopy(conf.routes), key, self.ctx.cfg) if policy else None
        except Exception:
            log.exception("Applying the preset failed")
            new = None
        if not isinstance(new, dict):
            self.saved_lbl.setText("Could not apply this choice.")
            return
        new["preset"] = key
        conf.routes.clear()
        conf.routes.update(new)
        self._load()
        self._schedule_save()

    def _explain(self) -> None:
        conf = self.conf()
        policy = integ("policy")
        lines = []
        if conf is not None and policy is not None:
            try:
                lines = list(policy.explain(conf.routes, self.ctx.cfg) or [])
            except Exception:
                log.exception("Explaining routes failed")
        self.explain_lbl.setText("<b>Right now:</b> " + " · ".join(_esc(x) for x in lines[:4]) if lines else "")
        self.explain_lbl.setVisible(bool(lines))

    # --- saving ---------------------------------------------------------------------------------
    def _schedule_save(self) -> None:
        if self.dest_id:
            self._dirty_ids.add(self.dest_id)
        self.saved_lbl.setText("Saving…")
        self._save_timer.start()

    def flush(self) -> None:
        """Save now whatever is waiting (also called when switching destination or leaving the tab)."""
        self._save_timer.stop()
        ids, self._dirty_ids = self._dirty_ids, set()
        known = {d.id for d in self.destinations()}
        for dest_id in ids:
            if dest_id not in known:
                continue
            try:
                self.hub.update_destination(dest_id)
            except Exception:
                log.exception("Saving routes failed")
                self.saved_lbl.setText("Could not save. Try again.")
                return
        if ids:
            self.saved_lbl.setText("All changes saved")

    def _copy_menu(self) -> None:
        conf = self.conf()
        if conf is None:
            return
        menu = QMenu(self)
        menu.setObjectName("IntegrationsMenu")
        labels = dest_labels(self.destinations())
        for d in self.destinations():
            if d.id != conf.id:
                menu.addAction(labels[d.id], lambda d=d: self._copy_to(d))
        menu.exec(self.copy_btn.mapToGlobal(self.copy_btn.rect().bottomLeft()))

    def _copy_to(self, other) -> None:
        conf = self.conf()
        if conf is None:
            return
        self._collect(conf.routes)
        other.routes.clear()
        other.routes.update(copy.deepcopy(conf.routes))
        self._dirty_ids.add(other.id)
        self.flush()
        notify = getattr(self.win, "notify", None)
        if notify:
            labels = dest_labels(self.destinations())
            notify(f"Copied the choices of {labels.get(conf.id, conf.name)} to {labels.get(other.id, other.name)}.")

    # --- daily summary preview --------------------------------------------------------------------
    def _refresh_summary(self) -> None:
        when, text = self._summary_cache
        if text and time.time() - when < 60:
            self.sum_preview.setText(text)
            return
        media = integ("media")
        store = getattr(self.ctx, "alert_store", None)
        if media is None or store is None:
            self.sum_preview.setText("A preview appears once YardGuard has some history.")
            return
        self.sum_preview.setText("Preparing a preview…")
        cfg = self.ctx.cfg
        run_async(media.build_summary, store, cfg, time.time() - 86400, on_done=self._summary_done)

    def _summary_done(self, ev) -> None:
        if isinstance(ev, Exception) or ev is None:
            self.sum_preview.setText("A preview appears once YardGuard has some history.")
            return
        lines = [getattr(ev, "title", "")] + [x for x in str(getattr(ev, "text", "") or "").splitlines() if x.strip()]
        text = "Preview: " + safe_text("\n".join(x for x in lines if x))
        self._summary_cache = (time.time(), text)
        self.sum_preview.setText(text)

    # --- links ----------------------------------------------------------------------------------
    def _open_settings(self) -> None:
        page = getattr(self.win, "settings", None)
        if page is not None and hasattr(self.win, "go_page"):
            self.win.go_page(page)

    def _open_areas(self) -> None:
        page = getattr(self.win, "areas_page", None)
        if page is not None and hasattr(self.win, "go_page"):
            self.win.go_page(page)

    def hideEvent(self, event):
        self.flush()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.conf() is not None:
            self._load()


def _field_label_inline(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFixedWidth(40)
    return lbl


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clear(lay) -> None:
    while lay.count():
        item = lay.takeAt(0)
        if item.widget() is not None:
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())
