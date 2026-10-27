"""Settings cards for live monitoring ("eyes"), the LocateAnything brain, and the zone editor.

BrainCard      install / start / stop LocateAnything, choose how it is used, edit its prompts.
MonitoringCard detection performance and alert rules.
ZoneEditorDialog draw watch zones on a camera picture (older editor: the app now draws every
               area, watch zones included, in Live view; see areas_ui. Kept for its review scripts).
"""
from __future__ import annotations

import shutil
import threading

from PySide6.QtCore import QByteArray, QEvent, QObject, QPointF, QRectF, Qt, QTime, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QRadioButton, QSizePolicy, QStackedWidget, QTimeEdit,
    QVBoxLayout, QWidget,
)

from .config import ROOT, Settings, zone_color
from .qt_util import run_async, set_prop, to_qimage
from .widgets import button, label, muted, set_status, spin_box
from .wizard import form_layout

LICENCE_URL = "https://huggingface.co/nvidia/LocateAnything-3B"
DOWNLOAD_GB = 7.6
VRAM_GB = 9
_gpu_found: bool | None = None


def _nvidia_gpu() -> bool:
    """Checked once: the brain card offers the install only on PCs with an NVIDIA GPU."""
    global _gpu_found
    if _gpu_found is None:
        from .brain import has_nvidia_gpu
        _gpu_found = has_nvidia_gpu()
    return _gpu_found


MODES = (
    ("off", "Off", "Only the fast detector watches. No extra GPU memory is used.", False),
    ("verify", "Verify suspicious moments",
     "The fast detector watches all the time. When a rule fires, the AI brain looks closely at that moment "
     "and confirms or rejects the alert. Fewer false alarms, little extra load.", True),
)
GENERATION = {
    "fast": ("Fast", "Quickest answers, about a second per check. Right for most yards."),
    "hybrid": ("Hybrid", "A little slower, more careful on dark or crowded scenes."),
    "slow": ("Slow", "Most careful reasoning, several seconds per check."),
}
DETECT_SIZES = (
    (640, "640 px  ·  fastest"),
    (960, "960 px"),
    (1280, "1280 px  ·  recommended"),
    (1600, "1600 px  ·  small or far objects"),
    (1920, "1920 px  ·  finest detail, most GPU"),
)
STATUS_TEXT = {
    "not_installed": ("checking", "Not installed"),
    "installing": ("checking", "Installing…"),
    "stopped": ("checking", "● Stopped"),
    "starting": ("nosignal", "● Starting…"),
    "ready": ("online", "● Running"),
    "busy": ("online", "● Running · checking"),
    "error": ("offline", "● Problem"),
}

# Structural rules only; colours come from palette roles so both themes work without substitution.
EXTRA_QSS = """
#OptionCard { background: palette(base); border: 1px solid palette(mid); border-radius: 8px; padding: 1px; }
#OptionCard:hover { border-color: palette(dark); }
#OptionCard[selected="true"] { border: 2px solid palette(highlight); padding: 0px; }
#OptionCard:disabled { background: palette(alternate-base); }
#OptionCard[selected="true"]:disabled { border: 1px solid palette(mid); padding: 1px; }
QRadioButton:disabled { color: palette(placeholder-text); }
QRadioButton { spacing: 8px; }
QRadioButton#OptionTitle { font-weight: 600; }
QRadioButton::indicator {
    width: 14px; height: 14px; border: 1px solid palette(placeholder-text); border-radius: 8px;
    background: palette(base);
}
QRadioButton::indicator:hover { border-color: palette(highlight); }
QRadioButton::indicator:checked {
    width: 8px; height: 8px; border: 4px solid palette(highlight); border-radius: 8px; background: palette(base);
}
QTimeEdit {
    background: palette(base); border: 1px solid palette(dark); border-radius: 6px;
    padding: 5px 8px; min-height: 22px; selection-background-color: palette(highlight);
}
QTimeEdit::up-button, QTimeEdit::down-button { width: 0px; border: none; }
QTimeEdit:focus { border: 1px solid palette(highlight); }
QTimeEdit:disabled { color: palette(placeholder-text); background: palette(alternate-base); }
QListWidget:disabled { color: palette(placeholder-text); }
QPushButton[kind="danger"]:disabled { color: palette(placeholder-text); border-color: palette(mid); }
#ZoneCanvas { background: #0b0d10; border-radius: 8px; }
#ZoneList::item { padding: 7px 8px; }
"""


def _defaults(name: str):
    """Default value of a Settings field (fresh copy for lists)."""
    return getattr(Settings(), name)


def _rgba(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    return line


LABEL_W = 160  # same label column in every form of these cards, so fields line up


def _help(text: str, indent: int = 0) -> QWidget:
    lbl = muted(text)
    if not indent:
        return lbl
    # a wrapper, not label margins: wrapped labels with margins report too much height in forms
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(indent, 0, 0, 0)
    lay.addWidget(lbl)
    return box


def _form():
    form = form_layout()
    form.setContentsMargins(0, 0, 0, 0)
    return form


def form_row(form, text: str, field) -> None:
    """Form row with the shared label column; the other Settings cards use it too."""
    lbl = QLabel(text)
    lbl.setMinimumWidth(LABEL_W)
    form.addRow(lbl, field)


_row = form_row


class _ThemeWatcher(QObject):
    """Calls fn() after the app theme changes (palette or style sheet), at most once per change."""

    def __init__(self, widget: QWidget, ctx, fn):
        super().__init__(widget)
        self._ctx, self._fn, self._last = ctx, fn, None
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and self._last is not self._ctx.palette:
            self._last = self._ctx.palette
            QTimer.singleShot(0, self._fn)
        return False

    def mark(self) -> None:
        self._last = self._ctx.palette


class Banner(QFrame):
    """Inline message with a tinted background (tone: warning | info | danger)."""

    def __init__(self, ctx, title: str = "", text: str = "", tone: str = "warning"):
        super().__init__()
        self.ctx, self.tone = ctx, tone
        self.setObjectName("Banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(20, 20)
        lay.addWidget(self.icon, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.title = label(title, "CardTitle", wrap=True)
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        col.addWidget(self.title)
        col.addWidget(self.text)
        lay.addLayout(col, 1)
        self.actions = QVBoxLayout()
        self.actions.setSpacing(8)
        lay.addLayout(self.actions)
        self._watch = _ThemeWatcher(self, ctx, self.restyle)
        self.restyle()

    def restyle(self) -> None:
        p = self.ctx.palette
        color = {"warning": p["warning"], "danger": p["danger"]}.get(self.tone, p["primary"])
        self.setStyleSheet(
            f"#Banner {{ background: {_rgba(color, 0.10)}; border: 1px solid {_rgba(color, 0.45)};"
            f" border-radius: 8px; }} #Banner QLabel {{ background: transparent; }}"
            f" #Banner #CardTitle {{ color: {color}; }}")
        self.icon.setPixmap(_warning_pixmap(color, svg=_INFO_SVG if self.tone == "info" else _WARN_SVG))
        self._watch.mark()


_WARN_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" '
             'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
             '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>'
             '<path d="M12 9v4"/><path d="M12 17h.01"/></svg>')
_INFO_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" '
             'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
             '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>')


def _warning_pixmap(color: str, size: int = 20, svg: str = _WARN_SVG) -> QPixmap:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    QSvgRenderer(QByteArray(svg.format(c=color).encode())).render(painter)
    painter.end()
    pm.setDevicePixelRatio(2.0)
    return pm


# --- Prompt list -----------------------------------------------------------------------------

class PromptList(QWidget):
    """Editable list of short text prompts with Add / Remove / Reset. Emits changed(list) on edits."""

    changed = Signal(list)

    def __init__(self, ctx, items: list[str], defaults: list[str], placeholder: str, noun: str = "prompt"):
        super().__init__()
        self.defaults, self.placeholder, self.noun = list(defaults), placeholder, noun
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.list = QListWidget()
        self.list.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                  | QAbstractItemView.SelectedClicked)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setMinimumHeight(150)
        self.list.setMaximumHeight(220)
        self.list.setAccessibleName(f"{noun.capitalize()} list")
        self.list.setToolTip("Double-click or press F2 to edit. Delete removes the selected entry.")
        lay.addWidget(self.list, 1)
        side = QVBoxLayout()
        side.setSpacing(8)
        self.add_btn = button("Add", tooltip=f"Add a new {noun}")
        ctx.bind_icon(self.add_btn, "plus")
        self.remove_btn = button("Remove", tooltip=f"Remove the selected {noun} (Delete)")
        ctx.bind_icon(self.remove_btn, "trash")
        self.reset_btn = button("Reset to defaults", "ghost", f"Replace the list with the built-in {noun}s")
        ctx.bind_icon(self.reset_btn, "refresh", "muted")
        for b in (self.add_btn, self.remove_btn, self.reset_btn):
            side.addWidget(b)
        side.addStretch(1)
        lay.addLayout(side)
        self.add_btn.clicked.connect(self._add)
        self.remove_btn.clicked.connect(self._remove)
        self.reset_btn.clicked.connect(self._reset)
        self.list.itemChanged.connect(self._edited)
        self.list.currentRowChanged.connect(self._update_buttons)
        self.list.installEventFilter(self)
        self.set_items(items)

    def items(self) -> list[str]:
        return [self.list.item(i).text().strip() for i in range(self.list.count())
                if self.list.item(i).text().strip()]

    def set_items(self, items: list[str]) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for text in items:
            self._append(text)
        self.list.blockSignals(False)
        self._update_buttons()

    def _append(self, text: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.list.addItem(item)
        return item

    def _add(self) -> None:
        self.list.blockSignals(True)
        item = self._append(self.placeholder)
        self.list.blockSignals(False)
        self.list.setCurrentItem(item)
        self.list.setFocus()
        self.list.editItem(item)
        self._emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self.list.takeItem(row)
        self._emit()

    def _reset(self) -> None:
        if self.items() == self.defaults:
            return
        if QMessageBox.question(self, "Reset to defaults",
                                f"Replace your {self.noun}s with the {len(self.defaults)} built-in ones?",
                                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.set_items(self.defaults)
        self._emit()

    def _edited(self, item: QListWidgetItem) -> None:
        text = " ".join(item.text().split())
        if not text:  # an emptied entry is removed rather than kept blank
            QTimer.singleShot(0, lambda: self._drop(item))
            return
        if text != item.text():
            self.list.blockSignals(True)
            item.setText(text)
            self.list.blockSignals(False)
        self._emit()

    def _drop(self, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        if row >= 0:
            self.list.takeItem(row)
            self._emit()

    def _emit(self) -> None:
        seen, unique = set(), []
        for text in self.items():
            if text.lower() not in seen:
                seen.add(text.lower())
                unique.append(text)
        self._update_buttons()
        self.changed.emit(unique)

    def _update_buttons(self, *_) -> None:
        self.remove_btn.setEnabled(self.list.currentRow() >= 0 and self.isEnabled())
        self.reset_btn.setEnabled(self.items() != self.defaults)

    def eventFilter(self, obj, event):
        if obj is self.list and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Delete \
                and self.list.state() != QAbstractItemView.EditingState:
            self._remove()
            return True
        return False


# --- Brain card ------------------------------------------------------------------------------

class _ModeOption(QFrame):
    """One selectable mode: radio title, optional chip, description. The whole card is clickable."""

    def __init__(self, title: str, text: str, recommended: bool, group: QButtonGroup, mode_id: int):
        super().__init__()
        self.setObjectName("OptionCard")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.radio = QRadioButton(title)
        self.radio.setObjectName("OptionTitle")
        group.addButton(self.radio, mode_id)
        top.addWidget(self.radio)
        if recommended:
            chip = label("Recommended", "Chip")
            set_prop(chip, "accent", True)
            top.addWidget(chip)
        top.addStretch(1)
        lay.addLayout(top)
        lay.addWidget(_help(text, indent=24))
        self.radio.toggled.connect(lambda on: set_prop(self, "selected", on))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.radio.click()
            self.radio.setFocus()
        super().mousePressEvent(event)


class _BrainSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(object)   # None = ok, Exception = failed


class BrainCard(QFrame):
    """LocateAnything: install, run, choose how it is used and what it looks for."""

    changed = Signal(str)   # name of the Settings field that changed

    def __init__(self, ctx, win):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.setObjectName("Card")
        self.brain = self._get_brain()
        self.signals = _BrainSignals()
        self.signals.progress.connect(self._install_progress)
        self.signals.finished.connect(self._install_finished)
        self._installing = False
        self._cancelling = False
        self._step = ("Preparing…", 0, 0)
        self._error: str | None = None
        self._last_action = "start"
        self._state = ""
        s = ctx.cfg.settings

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 20)
        lay.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(label("AI brain (LocateAnything)", "SectionTitle"))
        head.addStretch(1)
        self.chip = QLabel()
        head.addWidget(self.chip)
        lay.addLayout(head)
        self.intro = muted("A second AI that double-checks suspicious moments: it understands requests like "
                           "“hand on a car door handle” and points to where it sees them.")
        lay.addWidget(self.intro)

        lay.addWidget(self._build_status())
        lay.addWidget(_divider())
        lay.addWidget(label("How to use it", "CardTitle"))
        self.mode_group = QButtonGroup(self)
        self.mode_options: dict[str, _ModeOption] = {}
        for i, (key, title, text, rec) in enumerate(MODES):
            opt = _ModeOption(title, text, rec, self.mode_group, i)
            self.mode_options[key] = opt
            lay.addWidget(opt)

        lay.addWidget(_divider())
        self.details = QWidget()
        dform = self.dform = _form()
        self.details.setLayout(dform)
        gen_col = QVBoxLayout()
        gen_col.setSpacing(6)
        self.generation = QComboBox()
        self.generation.setAccessibleName("Answer mode")
        for key, (title, _) in GENERATION.items():
            self.generation.addItem(title, key)
        self.generation.setCurrentIndex(max(0, self.generation.findData(s.brain_generation)))
        self.generation.setMaximumWidth(220)
        self.gen_help = _help("")
        gen_col.addWidget(self.generation)
        gen_col.addWidget(self.gen_help)
        self.generation.currentIndexChanged.connect(self._generation_changed)
        self.gen_label = QLabel("Answer mode")
        self.gen_label.setMinimumWidth(LABEL_W)
        dform.addRow(self.gen_label, gen_col)
        self.prompts = PromptList(ctx, s.brain_prompts, _defaults("brain_prompts"), "new thing to look for",
                                  noun="prompt")
        self.prompts.changed.connect(lambda items: self._set("brain_prompts", items))
        pcol = QVBoxLayout()
        pcol.setSpacing(6)
        self.prompts_help = _help(
            "When an alert needs checking, the AI brain first makes sure the person or car is really there, "
            "then looks at that person for each of these. Seen at night in at least two pictures, the alert "
            "becomes critical. Describe what you would worry about, in plain words.")
        pcol.addWidget(self.prompts_help)
        pcol.addWidget(self.prompts)
        self.prompts_label = QLabel("Things to look for")
        self.prompts_label.setMinimumWidth(LABEL_W)
        dform.addRow(self.prompts_label, pcol)
        self.off_note = _help("Choose “Verify suspicious moments” above to use these settings.")
        lay.addWidget(self.off_note)
        lay.addWidget(self.details)

        self._generation_changed(save=False)
        self._select_mode(s.brain_mode if s.brain_mode in self.mode_options else "verify")
        self.mode_group.idToggled.connect(self._mode_toggled)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self._watch = _ThemeWatcher(self, ctx, self._restyle)
        self.refresh(force=True)

    # -- status panel ----------------------------------------------------------------

    def _build_status(self) -> QFrame:
        box = QFrame()
        box.setObjectName("ModelRow")
        row = QHBoxLayout(box)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(16)
        info = QVBoxLayout()
        info.setSpacing(4)
        self.st_title = label("", "CardTitle", wrap=True)
        self.st_text = muted()
        self.st_text.setTextFormat(Qt.RichText)
        self.st_text.setOpenExternalLinks(True)
        self.st_text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.st_step = muted()
        info.addWidget(self.st_title)
        info.addWidget(self.st_text)
        info.addSpacing(2)
        info.addWidget(self.bar)
        info.addWidget(self.st_step)
        row.addLayout(info, 1)
        actions = QVBoxLayout()
        actions.setSpacing(8)
        self.buttons = {}
        for name, text, kind, icon, tip in (
            ("install", f"Install ({DOWNLOAD_GB:.1f} GB)", "primary", "download",
             "Download LocateAnything and its runtime (resumable)"),
            ("cancel", "Cancel", "secondary", "x", "Stop the installation. It continues where it left off next time."),
            ("start", "Start AI brain", "primary", "power", "Load the AI brain onto the GPU"),
            ("stop", "Stop AI brain", "secondary", "pause", "Unload the AI brain and free its GPU memory"),
            ("retry", "Retry", "primary", "refresh", "Try again"),
        ):
            b = button(text, kind, tip)
            b.setMinimumWidth(170)
            self.ctx.bind_icon(b, icon, "on_primary" if kind == "primary" else "text")
            b.clicked.connect(lambda _=False, n=name: self._action(n))
            actions.addWidget(b)
            self.buttons[name] = b
        actions.addStretch(1)
        row.addLayout(actions)
        return box

    def _get_brain(self):
        brain = getattr(self.ctx, "brain", None)
        if brain is None:
            try:
                from .brain import Brain
                brain = Brain()
                self.ctx.brain = brain
            except Exception as exc:  # brain module missing or broken: card shows the problem
                self._import_error = f"The brain component could not be loaded ({exc})."
                return None
        return brain

    def _status(self) -> str:
        if self.brain is None:
            return "error"
        if self._installing:
            return "installing"
        try:
            status = self.brain.status()
        except Exception as exc:
            self._error = str(exc)
            return "error"
        if status == "error" and not self._error:
            self._error = getattr(self.brain, "last_error", None)
        return status

    def _stats(self) -> dict:
        try:
            return self.brain.stats() or {}
        except Exception:
            return {}

    def refresh(self, force: bool = False) -> None:
        state = self._status()
        stats = self._stats() if state in ("ready", "busy") else {}
        if state == "installing" and not self._installing:  # started from the AI models card
            self._step = getattr(self.brain, "install_step", None) or self._step
        # the poll timer calls this twice a second: only touch widgets when something changed
        sig = (state, self._step, self._error, self._cancelling, self._mode(), tuple(sorted(stats.items())),
               id(self.ctx.palette))
        if sig == getattr(self, "_sig", None) and not force:
            return
        self._sig = sig
        self._state = state
        tone, chip = STATUS_TEXT.get(state, ("checking", state))
        set_status(self.chip, tone, chip)
        self.chip.setVisible(state not in ("not_installed",))
        show: list[str] = []
        busy_bar = False
        self.bar.setVisible(False)
        self.st_step.setVisible(False)
        p = self.ctx.palette
        link = f"<a style='color:{p['primary']}; text-decoration:none' href='{LICENCE_URL}'>Read the licence</a>"
        if state == "not_installed" and not _nvidia_gpu():
            self.st_title.setText("Needs an NVIDIA graphics card")
            self.st_text.setText(
                "LocateAnything by NVIDIA runs only on an NVIDIA graphics card with about "
                f"{VRAM_GB} GB of memory, and none was found on this PC. YardGuard keeps working "
                "without it: the fast detector runs on the processor.")
        elif state == "not_installed":
            free = self._free_gb()
            self.st_title.setText("Not installed")
            drive = ROOT.anchor.rstrip("\\/")
            space = f" You have {free:.0f} GB free on drive {drive}." if free is not None else ""
            self.st_text.setText(
                f"LocateAnything by NVIDIA. Download about {DOWNLOAD_GB:.1f} GB including its runtime, "
                f"needs about {VRAM_GB} GB of GPU memory while running.{space}<br>"
                f"Free for personal, non-commercial use only (NVIDIA licence). {link}")
            show = ["install"]
        elif state == "installing":
            text, done, total = self._step
            self.st_title.setText("Installing LocateAnything…")
            self.st_text.setText("You can keep using YardGuard. If you cancel or close the app, "
                                 "the installation continues where it left off next time.")
            self.bar.setVisible(True)
            self.bar.setMaximum(max(total, 1) if total else 0)
            self.bar.setValue(done)
            self.st_step.setVisible(True)
            pct = f"{done * 100 // total}%  ·  " if total else ""
            self.st_step.setText(f"{pct}{'Cancelling…' if self._cancelling else text}")
            show = ["cancel"]
        elif state == "stopped":
            self.st_title.setText("Installed, not running")
            if getattr(self.brain, "user_stopped", False) and self._mode() == "verify":
                note = ("You stopped it, so it stays off until you start it again or turn monitoring on again. "
                        "Meanwhile alerts are marked “Unverified”.")
            else:
                note = {"off": "Start it when you want to use it.",
                        "verify": "Start it so alerts can be double-checked. Until then they are marked “Unverified”."
                        }[self._mode()]
            self.st_text.setText(f"{note} It uses about {VRAM_GB} GB of GPU memory while running.")
            show = ["start"]
        elif state == "starting":
            self.st_title.setText("Starting…")
            self.st_text.setText("Loading the model onto the GPU. This takes about 20 seconds, up to 2 minutes "
                                 "the first time after the PC starts.")
            self.bar.setVisible(True)
            self.bar.setMaximum(0)
            busy_bar = True
            show = ["stop"]
        elif state in ("ready", "busy"):
            lat = stats.get("avg_latency_s") or stats.get("last_latency_s")
            vram = stats.get("vram_mb")
            parts = [f"≈ {lat:.1f} s per check" if lat else "Speed is measured after the first check"]
            if vram:
                parts.append(f"{vram / 1024:.1f} GB GPU memory")
            self.st_title.setText("Running" if state == "ready" else "Running, checking a picture…")
            self.st_text.setText("  ·  ".join(parts) + ".  Stop it to free the GPU memory for games or other apps.")
            show = ["stop"]
        else:  # error
            self.st_title.setText("Something went wrong")
            msg = getattr(self, "_import_error", None) or self._error or "The AI brain stopped unexpectedly."
            friendly = _friendly_error(msg)
            if friendly:
                self.st_text.setText(f"{_escape(friendly)}<br>Point here to see the technical details.")
            else:
                self.st_text.setText(_escape(msg))
            self.st_text.setToolTip(msg)
            show = ["retry"] if self.brain is not None else []
        if not busy_bar and state != "installing":
            self.bar.setMaximum(1)
        if state != "error":
            self.st_text.setToolTip("")
        for name, b in self.buttons.items():
            b.setVisible(name in show)
        self.buttons["cancel"].setEnabled(not self._cancelling)
        self._watch.mark()

    def _free_gb(self) -> float | None:
        try:
            return shutil.disk_usage(ROOT.anchor).free / 1024 ** 3
        except OSError:
            return None

    def _action(self, name: str) -> None:
        if name == "retry":
            name = self._last_action
            self._error = None
        if name == "install":
            self._last_action = "install"
            self._error = None
            self._installing, self._cancelling = True, False
            self._step = ("Preparing…", 0, 0)
            threading.Thread(target=self._install_worker, daemon=True, name="brain-install").start()
            self.win.notify("Installing LocateAnything in the background…")
        elif name == "cancel":
            self._cancelling = True
            try:
                self.brain.cancel_install()
            except Exception as exc:
                self.win.notify(f"Could not cancel: {exc}", error=True)
        elif name == "start":
            self._last_action = "start"
            self._error = None
            self._user_action()
            run_async(lambda: self.brain.start(user=True), on_done=self._started)
            self.win.notify("Starting the AI brain…")
        elif name == "stop":
            # The user's Stop is final until they press Start or turn monitoring on again: nothing
            # (monitoring restarts, error recovery) starts it behind their back.
            self.brain.user_stopped = True
            self._user_action()
            run_async(lambda: self.brain.stop(user=True), on_done=self._stopped)
        self.refresh()

    def _user_action(self) -> None:
        """The user took over the brain: monitoring no longer stops it when it is switched off."""
        fn = getattr(self.win, "brain_user_action", None)
        if fn:
            fn()

    def _install_worker(self) -> None:
        def progress(text: str, done: int, total: int) -> None:
            self.signals.progress.emit(str(text), int(done), int(total))

        try:
            self.brain.install(progress)
            self.signals.finished.emit(None)
        except Exception as exc:
            self.signals.finished.emit(exc)

    def _install_progress(self, text: str, done: int, total: int) -> None:
        self._step = (text, done, total)
        if self._state == "installing":
            self.refresh()

    def _install_finished(self, result) -> None:
        self._installing = False
        cancelled, self._cancelling = self._cancelling, False
        if result is None:
            wanted = self._mode() != "off"
            self.win.notify("LocateAnything is installed." + (" Starting it…" if wanted else ""))
            if wanted:
                self._action("start")
        elif cancelled:
            self.win.notify("Installation paused. Click Install to continue where it left off.")
        else:
            self._error = f"Installation failed: {result}"
            self.win.notify(self._error, error=True)
        self.refresh()

    def _started(self, result) -> None:
        if isinstance(result, Exception):
            self._error = f"Could not start the AI brain: {result}"
            self.win.notify(self._error, error=True)
        self.refresh()

    def _stopped(self, result) -> None:
        if isinstance(result, Exception):
            self.win.notify(f"Could not stop the AI brain: {result}", error=True)
        else:
            self.win.notify("AI brain stopped, GPU memory freed.")
        self.refresh()

    # -- mode and prompts ------------------------------------------------------------

    def _mode(self) -> str:
        return MODES[max(0, self.mode_group.checkedId())][0]

    def _select_mode(self, key: str) -> None:
        self.mode_group.blockSignals(True)
        idx = [m[0] for m in MODES].index(key)
        self.mode_group.button(idx).setChecked(True)
        self.mode_group.blockSignals(False)
        for k, opt in self.mode_options.items():
            set_prop(opt, "selected", k == key)
        self._show_mode(key)

    def _show_mode(self, key: str) -> None:
        self.details.setEnabled(key != "off")
        self.off_note.setVisible(key == "off")

    def _mode_toggled(self, idx: int, checked: bool) -> None:
        if not checked:
            return
        key = MODES[idx][0]
        self._show_mode(key)
        self._set("brain_mode", key, f"AI brain: {MODES[idx][1]}.")
        self.refresh()

    def _generation_changed(self, *_, save: bool = True) -> None:
        key = self.generation.currentData()
        self.gen_help.setText(GENERATION[key][1])
        if save:
            self._set("brain_generation", key)

    def _set(self, key: str, value, message: str = "Settings saved.") -> None:
        setattr(self.ctx.cfg.settings, key, value)
        self.ctx.cfg.save()
        self.win.notify(message)
        self.changed.emit(key)

    def _restyle(self) -> None:
        self.refresh(force=True)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()


_KNOWN_ERRORS = (
    (("out of memory", "outofmemory", "cuda error: out", "cudaerrormemoryallocation"),
     "Not enough GPU memory. Close games or other apps that use the graphics card, or pick a smaller "
     "detection model, then click Retry."),
    (("not installed", "no such file", "cannot find the file"),
     "Some files of the AI brain are missing. Click Retry; if it fails again, install it again."),
    (("timed out", "timeout"),
     "The AI brain took too long to answer. Click Retry. If it keeps happening, restart the PC."),
    (("no cuda", "cuda driver", "nvidia driver", "cuda is not available"),
     "The graphics card driver could not be used. Update the NVIDIA driver, then click Retry."),
)


def _friendly_error(message: str) -> str | None:
    """Plain-language cause and next step for failures we recognise; None if unknown."""
    low = message.lower()
    for needles, text in _KNOWN_ERRORS:
        if any(n in low for n in needles):
            return text
    return None


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Monitoring card -------------------------------------------------------------------------

class MonitoringCard(QFrame):
    """Detection performance and alert rules. Every change is saved at once."""

    changed = Signal(str)   # name of the Settings field that changed

    def __init__(self, ctx, win):
        super().__init__()
        self.ctx, self.win = ctx, win
        self.setObjectName("Card")
        s = ctx.cfg.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 20)
        lay.setSpacing(12)
        lay.addWidget(label("Monitoring", "SectionTitle"))
        lay.addWidget(muted("How the cameras are watched while monitoring is on, and which situations raise an "
                            "alert."))

        lay.addWidget(label("Performance", "CardTitle"))
        form = _form()
        self.fps = spin_box(1, 15, int(round(s.detect_fps)))
        self.fps.setSuffix(" per second")
        self.fps.setMaximumWidth(170)
        self.fps.setAccessibleName("AI checks per second per camera")
        self.fps.valueChanged.connect(lambda v: self._set("detect_fps", float(v)))
        _row(form, "AI checks", self._with_help(self.fps, "How often the AI looks at each camera. 5 is enough to "
                                                           "follow people walking; more uses more GPU."))
        self.size = QComboBox()
        self.size.setAccessibleName("Detection size")
        for px, text in DETECT_SIZES:
            self.size.addItem(text, px)
        if self.size.findData(s.detect_size) < 0:
            self.size.addItem(f"{s.detect_size} px  ·  custom", s.detect_size)
        self.size.setCurrentIndex(self.size.findData(s.detect_size))
        self.size.setMaximumWidth(300)
        self.size.currentIndexChanged.connect(lambda: self._set("detect_size", self.size.currentData()))
        _row(form, "Detection size", self._with_help(self.size, "Larger finds small, far or dark objects but "
                                                                 "takes more GPU time per picture."))
        self.gpu = QCheckBox("Decode video on the graphics card (NVDEC)")
        self.gpu.setChecked(s.gpu_decode)
        self.gpu.toggled.connect(lambda v: (self._set("gpu_decode", v), self.brain_mode_changed()))
        gpu_box = self._with_help(self.gpu, "Frees a little processor time but uses about 200 MB of GPU memory "
                                            "per camera.", indent=24)
        self.gpu_paused = _help("", indent=24)
        gpu_box.layout().addWidget(self.gpu_paused)
        _row(form, "Video and GPU", gpu_box)
        self.motion = QCheckBox("Skip the AI when nothing moves")
        self.motion.setChecked(s.motion_gating)
        self.motion.toggled.connect(lambda v: self._set("motion_gating", v))
        _row(form, "", self._with_help(self.motion, "Saves a lot of GPU on quiet nights. A slow check still "
                                                     "runs so nothing is missed for long.", indent=24))
        lay.addLayout(form)
        lay.addWidget(_divider())

        lay.addWidget(label("Rules", "CardTitle"))
        rform = _form()
        rform.setVerticalSpacing(14)
        nights = QHBoxLayout()
        nights.setSpacing(8)
        self.night_start = self._time_edit(s.night_start, "Night starts at")
        self.night_end = self._time_edit(s.night_end, "Night ends at")
        self.night_len = muted(wrap=False)
        nights.addWidget(self.night_start)
        nights.addWidget(QLabel("to"))
        nights.addWidget(self.night_end)
        nights.addSpacing(8)
        nights.addWidget(self.night_len)
        nights.addStretch(1)
        self.night_start.timeChanged.connect(lambda t: self._time_changed("night_start", t))
        self.night_end.timeChanged.connect(lambda t: self._time_changed("night_end", t))
        _row(rform, "Night hours", self._with_help(nights, "Times like 22:00 to 06:00 that cross midnight "
                                                           "work too."))
        self.rules_when = QComboBox()
        self.rules_when.addItem("At night only (recommended for a busy yard)", "night")
        self.rules_when.addItem("Always, day and night", "always")
        self.rules_when.setCurrentIndex(max(0, self.rules_when.findData(s.rules_when)))
        self.rules_when.setAccessibleName("When lingering, car to car and car moved alert")
        self.rules_when.currentIndexChanged.connect(lambda: self._set("rules_when", self.rules_when.currentData()))
        _row(rform, "Rules below alert", self._with_help(
            self.rules_when, "Lingering, going from car to car and a parked car moving only alert during "
                             "night hours. By day people chat, wait and load cars in a yard all the time. "
                             "Areas have their own schedule."))
        rules = QWidget()
        rlay = QVBoxLayout(rules)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(14)
        self.r_night = self._rule("rule_night_presence", "A person is near the cars at night",
                                  "Alerts as soon as a person is seen in a watch zone during night hours.", rlay)
        self.loiter = spin_box(5, 900, s.loiter_seconds)
        self.loiter.setSuffix(" s")
        self.loiter.setAccessibleName("Loitering time in seconds")
        self.loiter.valueChanged.connect(lambda v: self._set("loiter_seconds", v))
        self.r_loiter = self._rule("rule_loitering", "Someone lingers",
                                   "A person stays in a watch zone longer than this.", rlay,
                                   ["for more than", self.loiter])
        self.prowl_cars = spin_box(2, 10, s.prowl_cars)
        self.prowl_cars.setSuffix(" cars")
        self.prowl_cars.setAccessibleName("Number of cars visited")
        self.prowl_cars.valueChanged.connect(lambda v: self._set("prowl_cars", v))
        self.prowl_win = spin_box(30, 1800, s.prowl_window_s)
        self.prowl_win.setSuffix(" s")
        self.prowl_win.setSingleStep(30)
        self.prowl_win.setAccessibleName("Within this many seconds")
        self.prowl_win.valueChanged.connect(lambda v: self._set("prowl_window_s", v))
        self.r_prowl = self._rule("rule_prowling", "Someone goes from car to car",
                                  "Typical when someone tries door handles along a row of cars.", rlay,
                                  ["visits", self.prowl_cars, "within", self.prowl_win])
        self.r_moved = self._rule("rule_car_moved", "A parked car moves or is hit",
                                  "A parked car shifts or shakes, for example after a bump.", rlay)
        _row(rform, "Alert when", rules)
        self.cooldown = spin_box(10, 3600, s.alert_cooldown_s)
        self.cooldown.setSuffix(" s")
        self.cooldown.setSingleStep(30)
        self.cooldown.setFixedWidth(110)
        self.cooldown.setAccessibleName("Seconds before the same alert repeats")
        self.cooldown.valueChanged.connect(lambda v: self._set("alert_cooldown_s", v))
        _row(rform, "Repeat alerts after", self._with_help(
            self.cooldown, "The same rule for the same person on the same camera alerts at most once in this "
                           "time, so one event does not flood you."))
        self.incident_gap = spin_box(1, 60, max(1, round(s.incident_gap_s / 60)))
        self.incident_gap.setSuffix(" min")
        self.incident_gap.setFixedWidth(110)
        self.incident_gap.setAccessibleName("Minutes between alerts of one incident")
        self.incident_gap.valueChanged.connect(lambda v: self._set("incident_gap_s", int(v) * 60))
        gap_row = QHBoxLayout()
        gap_row.setSpacing(8)
        gap_row.addWidget(QLabel("Group alerts into one incident when they are less than"))
        gap_row.addWidget(self.incident_gap)
        gap_row.addWidget(QLabel("apart"))
        gap_row.addStretch(1)
        _row(rform, "Incidents", self._with_help(
            gap_row, "Alerts close together, on any camera of the site, are one event: they show up as one "
                     "incident with a timeline instead of many separate alerts."))
        lay.addLayout(rform)
        self._update_night_len()
        self.brain_mode_changed()

    def brain_mode_changed(self) -> None:
        """The engine decodes on the processor while the AI brain is used (it needs the GPU
        memory), so a ticked NVDEC box says it is paused instead of looking active. The box stays
        editable: it is the choice for when the AI brain is off."""
        s = self.ctx.cfg.settings
        paused = s.brain_mode != "off" and self.gpu.isChecked()
        self.gpu_paused.setVisible(paused)
        self.gpu_paused.findChild(QLabel).setText(
            "Paused while the AI brain is in use: it needs that GPU memory, so video is decoded on the processor.")

    def _with_help(self, control, text: str, indent: int = 0) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        if isinstance(control, QHBoxLayout):
            col.addLayout(control)
        else:
            col.addWidget(control, 0, Qt.AlignLeft if not isinstance(control, QCheckBox) else Qt.Alignment())
        col.addWidget(_help(text, indent))
        return box

    def _time_edit(self, value: str, name: str) -> QTimeEdit:
        edit = QTimeEdit(QTime.fromString(value, "HH:mm") if QTime.fromString(value, "HH:mm").isValid()
                         else QTime(0, 0))
        edit.setDisplayFormat("HH:mm")
        # arrows are hidden by EXTRA_QSS (NoButtons collapses the text field under a style sheet);
        # typing, arrow keys and the mouse wheel still change the time
        edit.setAccessibleName(name)
        edit.setToolTip("Type a time, or use the arrow keys / mouse wheel")
        edit.setFixedWidth(84)
        edit.setAlignment(Qt.AlignCenter)
        return edit

    def _rule(self, key: str, title: str, text: str, lay: QVBoxLayout, controls: list | None = None) -> QCheckBox:
        block = QVBoxLayout()
        block.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        check = QCheckBox(title)
        check.setChecked(getattr(self.ctx.cfg.settings, key))
        f = check.font()
        f.setWeight(QFont.DemiBold)
        check.setFont(f)
        top.addWidget(check)
        top.addSpacing(8)
        widgets = []
        for c in controls or []:
            w = muted(c, wrap=False) if isinstance(c, str) else c
            if not isinstance(c, str):
                w.setFixedWidth(110)
            widgets.append(w)
            top.addWidget(w)
        top.addStretch(1)
        block.addLayout(top)
        block.addWidget(_help(text, indent=24))
        lay.addLayout(block)

        def toggled(on: bool) -> None:
            for w in widgets:
                w.setEnabled(on)
            self._set(key, on)

        for w in widgets:
            w.setEnabled(check.isChecked())
        check.toggled.connect(toggled)
        return check

    def _time_changed(self, key: str, t: QTime) -> None:
        self._set(key, t.toString("HH:mm"))
        self._update_night_len()

    def _update_night_len(self) -> None:
        start, end = self.night_start.time(), self.night_end.time()
        minutes = (start.secsTo(end) // 60) % (24 * 60)
        if minutes == 0:
            self.night_len.setText("(whole day)")
        else:
            h, m = divmod(minutes, 60)
            self.night_len.setText(f"({h} h{f' {m} min' if m else ''})")

    def _set(self, key: str, value) -> None:
        setattr(self.ctx.cfg.settings, key, value)
        self.ctx.cfg.save()
        self.win.notify("Settings saved.")
        self.changed.emit(key)


# --- Incident clips (in the Storage card) ----------------------------------------------------------

def size_text(n: float) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB".replace(".0 GB", " GB")
    return f"{max(0, round(n / 1024 ** 2))} MB"


class ClipsSection(QWidget):
    """Short clips of incidents: on / off, the storage limits, current use and a clean-up button.
    Every change is saved at once. The limits also apply while recording is off."""

    changed = Signal(str)   # name of the Settings field that changed
    cleaned = Signal()      # clips were deleted: incident views should refresh

    def __init__(self, ctx, win):
        super().__init__()
        self.ctx, self.win = ctx, win
        s = ctx.cfg.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(label("Incident clips", "CardTitle"))
        form = _form()
        self.enabled = QCheckBox("Save short clips of incidents")
        self.enabled.setChecked(s.clips_enabled)
        self.enabled.toggled.connect(lambda v: self._set("clips_enabled", v))
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        col.addWidget(self.enabled)
        col.addWidget(_help("About 20 seconds around each alert: 8 s before it and 12 s after. They play in "
                            "the Incidents page.", 24))
        _row(form, "Clips", box)
        self.max_gb = self._spin(1, 1000, round(s.clip_max_gb), " GB", "Most space clips may use, in GB")
        self.max_gb.valueChanged.connect(lambda v: self._set("clip_max_gb", float(v)))
        _row(form, "Keep at most", self._line(self.max_gb, "of clips. The oldest go first; clips of open or "
                                                           "AI-confirmed incidents are kept longest."))
        self.max_days = self._spin(1, 365, int(s.clip_max_days), " days", "Delete clips after this many days")
        self.max_days.valueChanged.connect(lambda v: self._set("clip_max_days", int(v)))
        _row(form, "Delete clips after", self._line(self.max_days, ""))
        self.fa_days = self._spin(0, 365, int(s.false_alarm_clip_days), " days",
                                  "Delete clips of false alarms after this many days")
        self.fa_days.setSpecialValueText("At once")
        self.fa_days.valueChanged.connect(lambda v: self._set("false_alarm_clip_days", int(v)))
        _row(form, "False-alarm clips", self._line(self.fa_days, "Clips of incidents you marked as false alarms "
                                                                 "are deleted once they are this old."))
        self.area_days = self._spin(1, 365, int(s.area_log_days), " days", "Days to keep the area log")
        self.area_days.valueChanged.connect(lambda v: self._set("area_log_days", int(v)))
        _row(form, "Area log: keep", self._line(self.area_days, "Area events (counts, plates, parking…) and "
                                                                "their pictures older than this are deleted."))
        for box in (self.max_days, self.fa_days, self.area_days):  # "1 day", "2 days"
            box.valueChanged.connect(lambda v, box=box: box.setSuffix(" day" if v == 1 else " days"))
            box.setSuffix(" day" if box.value() == 1 else " days")
        lay.addLayout(form)
        use = QHBoxLayout()
        use.setSpacing(12)
        self.usage = muted("Calculating…")
        self.usage.setAccessibleName("Space used by clips")
        self.clean_btn = button("Clean up now", tooltip="Apply these limits now. Pictures and alerts are kept.")
        self.clean_btn.clicked.connect(self.clean_up)
        use.addWidget(self.usage, 1)
        use.addWidget(self.clean_btn, 0, Qt.AlignVCenter)
        lay.addLayout(use)

    @staticmethod
    def _spin(low: int, high: int, value: int, suffix: str, name: str):
        box = spin_box(low, high, value)
        box.setSuffix(suffix)
        box.setFixedWidth(110)
        box.setAccessibleName(name)
        return box

    @staticmethod
    def _line(control, text: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(control)
        if text:
            row.addWidget(muted(text), 1)
        else:
            row.addStretch(1)
        return box

    def _store(self):
        return getattr(self.ctx, "alert_store", None)

    def refresh(self) -> None:
        store = self._store()
        if store is not None:
            from .clips import usage
            run_async(usage, store.dir, on_done=self._usage_done)

    def _usage_done(self, result, note: str = "") -> None:
        if not isinstance(result, tuple):
            self.usage.setText("Could not read the clips folder.")
            return
        used, count = result
        cap = self.ctx.cfg.settings.clip_max_gb * 1024 ** 3
        text = (f"Clips use {size_text(used)} ({count} clip{'s' if count != 1 else ''}) of {size_text(cap)}."
                if count else f"No clips saved yet. They may use up to {size_text(cap)}.")
        self.usage.setText(text + (f" {note}" if note else ""))

    def clean_up(self) -> None:
        store = self._store()
        if store is None:
            return
        from .clips import cleanup
        self.clean_btn.setEnabled(False)
        self.clean_btn.setText("Cleaning up…")
        settings = self.ctx.cfg.settings

        def work():
            area_cleanup = getattr(store, "cleanup_area_events", None)
            if area_cleanup is not None:  # the area log follows its own limit at the same time
                area_cleanup(settings.area_log_days)
            return cleanup(store, settings)

        run_async(work, on_done=self._cleaned)

    def _cleaned(self, res) -> None:
        self.clean_btn.setEnabled(True)
        self.clean_btn.setText("Clean up now")
        if isinstance(res, Exception) or res is None:
            self.win.notify(f"Could not clean up the clips: {res}", error=True)
            return
        if res.deleted:
            note = f"Deleted {res.deleted} clip{'s' if res.deleted != 1 else ''}, {size_text(res.freed)} freed."
        else:
            note = "Nothing to delete: every clip is within your limits."
        if res.locked:
            note += f" {res.locked} clip{'s' if res.locked != 1 else ''} in use will go next time."
        self._usage_done((res.total, res.count), note)
        self.win.notify(note)
        if res.deleted:
            self.cleaned.emit()

    def _set(self, key: str, value) -> None:
        setattr(self.ctx.cfg.settings, key, value)
        self.ctx.cfg.save()
        self.win.notify("Settings saved.")
        self.changed.emit(key)
        if key == "clip_max_gb":
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()


# --- Zone editor -----------------------------------------------------------------------------

CLOSE_RADIUS = 12  # px: clicking this close to the first point finishes the zone


class ZoneCanvas(QWidget):
    """Camera picture with zone polygons; click to add points of a new zone."""

    zone_closed = Signal(list)   # normalised points of a finished zone
    drawing_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("ZoneCanvas")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.CrossCursor)
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName("Camera picture. Click to add zone points.")
        self.image = None
        self.message = "Getting a picture from the camera…"
        self.zones: list[dict] = []
        self.selected = -1
        self.points: list[tuple[float, float]] = []   # zone being drawn, normalised
        self.mouse: QPointF | None = None

    # geometry: the picture is scaled to fit and centred, keeping its aspect ratio
    def image_rect(self) -> QRectF:
        if self.image is None:
            return QRectF(self.rect())
        w, h = self.image.width(), self.image.height()
        scale = min(self.width() / w, self.height() / h)
        dw, dh = w * scale, h * scale
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)

    def to_norm(self, pos: QPointF) -> tuple[float, float]:
        r = self.image_rect()
        x = min(max((pos.x() - r.x()) / r.width(), 0.0), 1.0)
        y = min(max((pos.y() - r.y()) / r.height(), 0.0), 1.0)
        return round(x, 4), round(y, 4)

    def to_px(self, pt) -> QPointF:
        r = self.image_rect()
        return QPointF(r.x() + pt[0] * r.width(), r.y() + pt[1] * r.height())

    def near_first(self) -> bool:
        if len(self.points) < 3 or self.mouse is None:
            return False
        d = self.to_px(self.points[0]) - self.mouse
        return d.x() ** 2 + d.y() ** 2 <= CLOSE_RADIUS ** 2

    def heightForWidth(self, w: int) -> int:
        return int(w * 9 / 16)

    # editing
    def undo_point(self) -> bool:
        if not self.points:
            return False
        self.points.pop()
        self._changed()
        return True

    def cancel_drawing(self) -> bool:
        if not self.points:
            return False
        self.points.clear()
        self._changed()
        return True

    def close_zone(self) -> bool:
        if len(self.points) < 3:
            return False
        pts = [list(p) for p in self.points]
        self.points.clear()
        self._changed()
        self.zone_closed.emit(pts)
        return True

    def _changed(self) -> None:
        self.update()
        self.drawing_changed.emit()

    # events
    def mousePressEvent(self, event):
        if self.image is None:
            return
        if event.button() == Qt.RightButton:
            self.undo_point()
            return
        if event.button() != Qt.LeftButton:
            return
        if self.near_first():
            self.close_zone()
            return
        if not self.image_rect().contains(event.position()):
            return
        pt = self.to_norm(event.position())
        if not self.points or pt != self.points[-1]:
            self.points.append(pt)
            self._changed()

    def mouseDoubleClickEvent(self, event):
        # the first click of the double-click already added a point
        if event.button() == Qt.LeftButton and self.image is not None:
            self.close_zone()

    def mouseMoveEvent(self, event):
        self.mouse = event.position()
        self.update()

    def leaveEvent(self, event):
        self.mouse = None
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter) and self.points:
            self.close_zone()  # with fewer than 3 points nothing happens; the hint says why
            return
        if key == Qt.Key_Backspace and self.undo_point():
            return
        if key == Qt.Key_Escape and self.cancel_drawing():
            return
        super().keyPressEvent(event)

    # painting
    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.image is None:
            p.setPen(QColor("#9aa3b2"))
            p.drawText(self.rect().adjusted(24, 24, -24, -24), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        r = self.image_rect()
        p.drawImage(r, self.image)
        if not self.zones and not self.points:
            p.fillRect(r, QColor(0, 0, 0, 40))
        # selected zone on top, and all name tags above all shapes so none is hidden
        order = [i for i in range(len(self.zones)) if i != self.selected]
        order += [self.selected] if 0 <= self.selected < len(self.zones) else []
        for i in order:
            self._paint_zone(p, self.zones[i], zone_color(i), i == self.selected)
        for i in order:  # same tag as the live view: zone colour, at the top-most corner
            pts = [self.to_px(pt) for pt in self.zones[i].get("points", [])]
            if len(pts) >= 3:
                self._paint_zone_tag(p, self.zones[i].get("name") or f"Zone {i + 1}", pts, QColor(zone_color(i)))
        if self.points:
            self._paint_drawing(p)

    def _paint_zone(self, p: QPainter, zone: dict, color: str, selected: bool) -> None:
        pts = [self.to_px(pt) for pt in zone.get("points", [])]
        if len(pts) < 3:
            return
        c = QColor(color)
        fill = QColor(c)
        fill.setAlpha(95 if selected else 60)
        p.setBrush(fill)
        p.setPen(QPen(c, 3 if selected else 2))
        p.drawPolygon(QPolygonF(pts))
        if selected:
            p.setBrush(QColor("white"))
            p.setPen(QPen(c, 2))
            for pt in pts:
                p.drawEllipse(pt, 4, 4)

    def _paint_tag(self, p: QPainter, text: str, at: QPointF, color: QColor) -> None:
        font = QFont(self.font())
        font.setPointSizeF(9)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        fm = QFontMetrics(font)
        w, h = fm.horizontalAdvance(text) + 16, fm.height() + 6
        box = QRectF(at.x() - w / 2, at.y() - h / 2, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 17, 21, 210))
        p.drawRoundedRect(box, h / 2, h / 2)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignCenter, text)

    def _paint_zone_tag(self, p: QPainter, text: str, pts: list[QPointF], color: QColor) -> None:
        font = QFont(self.font())
        font.setPointSizeF(9)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        fm = QFontMetrics(font)
        top = min(pts, key=lambda pt: (pt.y(), pt.x()))
        w, h = fm.horizontalAdvance(text) + 12, fm.height() + 4
        x = min(max(0.0, top.x()), self.width() - w)
        box = QRectF(x, top.y(), w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QColor(20, 20, 20))
        p.drawText(box, Qt.AlignCenter, text)

    def _paint_drawing(self, p: QPainter) -> None:
        color = QColor(zone_color(len(self.zones)))
        pts = [self.to_px(pt) for pt in self.points]
        closing = self.near_first()
        if len(pts) >= 3:
            fill = QColor(color)
            fill.setAlpha(45)
            path = QPainterPath()
            path.addPolygon(QPolygonF(pts + ([self.mouse] if self.mouse and not closing else [])))
            path.closeSubpath()
            p.fillPath(path, fill)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(color, 2))
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)
        if self.mouse is not None:
            p.setPen(QPen(color, 2, Qt.DashLine))
            p.drawLine(pts[-1], pts[0] if closing else self.mouse)
        for i, pt in enumerate(pts):
            first = i == 0
            radius = 8 if first and closing else 5 if first else 4
            p.setPen(QPen(color, 2))
            p.setBrush(color if first and closing else QColor("white"))
            p.drawEllipse(pt, radius, radius)
        if closing:
            p.setPen(QPen(QColor("white"), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(pts[0], 12, 12)
            self._paint_tag(p, "Click to finish", pts[0] + QPointF(0, -26), color)


class ZoneEditorDialog(QDialog):
    """Draw watch zones for one camera. Only people and cars inside a zone count for alerts."""

    def __init__(self, ctx, site, cam, parent=None, frame=None):
        super().__init__(parent)
        self.ctx, self.site, self.cam = ctx, site, cam
        self.setWindowTitle(f"Watch zones · {cam.label}")
        self.setModal(True)
        self.resize(1120, 700)
        self.setMinimumSize(860, 540)
        self.dirty = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(label(f"Watch zones for {cam.label}", "PageTitle"))
        lay.addWidget(muted("Draw around the places that matter, for example the parking spaces. Alerts only "
                            "consider people and cars inside a zone. Without zones the whole picture is watched."))
        body = QHBoxLayout()
        body.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.canvas = ZoneCanvas()
        self.canvas.zones = [{"name": z.get("name", ""), "points": [list(p) for p in z.get("points", [])]}
                             for z in cam.zones]
        left.addWidget(self.canvas, 1)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(8)
        self.hint = muted()
        self.hint.setObjectName("ZoneHint")
        hint_row.addWidget(self.hint, 1)
        self.retry_btn = button("Try again", tooltip="Get a new picture from the camera")
        self.ctx.bind_icon(self.retry_btn, "refresh")
        self.retry_btn.clicked.connect(self._load_frame)
        self.retry_btn.hide()
        hint_row.addWidget(self.retry_btn)
        self.undo_btn = button("Undo point", tooltip="Remove the last point (Backspace or right-click)")
        self.undo_btn.clicked.connect(lambda: (self.canvas.undo_point(), self.canvas.setFocus()))
        self.finish_btn = button("Finish zone", tooltip="Close the zone (Enter or double-click)")
        self.ctx.bind_icon(self.finish_btn, "check")
        self.finish_btn.clicked.connect(lambda: (self.canvas.close_zone(), self.canvas.setFocus()))
        hint_row.addWidget(self.undo_btn)
        hint_row.addWidget(self.finish_btn)
        self.hint.setWordWrap(False)
        self.hint.setMinimumWidth(0)
        self.hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hint_bar = QWidget()
        hint_bar.setLayout(hint_row)
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_bar.setFixedHeight(max(36, self.finish_btn.sizeHint().height()))  # 36: styled button height
        left.addWidget(hint_bar)
        body.addLayout(left, 1)

        side = QFrame()
        side.setObjectName("Card")
        side.setFixedWidth(270)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 14, 16, 16)
        sl.setSpacing(10)
        sl.addWidget(label("Zones", "SectionTitle"))
        self.list = QListWidget()
        self.list.setObjectName("ZoneList")
        self.list.setAccessibleName("Zones")
        self.list.setToolTip("Double-click or press F2 to rename")
        self.list.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.list.currentRowChanged.connect(self._select)
        self.list.itemChanged.connect(self._renamed)
        self.list.installEventFilter(self)
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 0, 0, 0)
        el.addWidget(_help("No zones yet, so the whole picture is watched. Click on the picture to place the "
                           "first corner of a zone."))
        el.addStretch(1)
        self.lists = QStackedWidget()
        self.lists.addWidget(self.empty)
        self.lists.addWidget(self.list)
        sl.addWidget(self.lists, 1)
        self.rename_hint = _help("Double-click a zone to rename it.")
        sl.addWidget(self.rename_hint)
        self.delete_btn = button("Delete zone", "danger", "Delete the selected zone (Delete)")
        self.ctx.bind_icon(self.delete_btn, "trash", "danger")
        self.delete_btn.clicked.connect(self._delete)
        self.clear_btn = button("Delete all", "ghost", "Remove every zone of this camera")
        self.clear_btn.clicked.connect(self._clear)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addWidget(self.delete_btn)
        btns.addWidget(self.clear_btn)
        btns.addStretch(1)
        sl.addLayout(btns)
        body.addWidget(side)
        lay.addLayout(body, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.summary = muted(wrap=False)
        foot.addWidget(self.summary, 1)
        self.cancel_btn = button("Cancel", tooltip="Close without saving (Esc)")
        self.cancel_btn.setAutoDefault(False)
        self.save_btn = button("Save zones", "primary", "Save the zones of this camera")
        self.save_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn.clicked.connect(self._save)
        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.save_btn)
        lay.addLayout(foot)

        self.canvas.zone_closed.connect(self._zone_closed)
        self.canvas.drawing_changed.connect(self._update)
        self._fill_list()
        if frame is not None:
            self._frame_loaded(frame)
        else:
            self._load_frame()
        self.canvas.setFocus()

    def _load_frame(self) -> None:
        from .video import grab_frame_fast

        self.retry_btn.hide()
        self.canvas.image = None
        self.canvas.message = f"Getting a picture from {self.cam.label}…"
        self.canvas.update()
        url = self.site.rtsp_url(self.cam.channel, main=True)
        run_async(grab_frame_fast, url, on_done=self._frame_loaded)
        self._update()

    def _frame_loaded(self, frame) -> None:
        if frame is None or isinstance(frame, Exception):
            self.canvas.image = None
            self.canvas.message = (f"Could not get a picture from {self.cam.label}. Check that the camera is "
                                   "online, then try again.")
            self.retry_btn.show()
        else:
            self.canvas.image = to_qimage(frame)
        self.canvas.update()
        self._update()

    def _fill_list(self, select: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for i, zone in enumerate(self.canvas.zones):
            item = QListWidgetItem(zone.get("name") or f"Zone {i + 1}")
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            item.setIcon(_swatch(zone_color(i)))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)
        else:
            self.canvas.selected = -1
        self._update()

    def _next_name(self) -> str:
        names = {z.get("name") for z in self.canvas.zones}
        n = len(self.canvas.zones) + 1
        while f"Zone {n}" in names:
            n += 1
        return f"Zone {n}"

    def _zone_closed(self, points: list) -> None:
        self.canvas.zones.append({"name": self._next_name(), "points": points})
        self.dirty = True
        self._fill_list(select=len(self.canvas.zones) - 1)
        self.canvas.update()

    def _select(self, row: int) -> None:
        self.canvas.selected = row
        self.canvas.update()
        self._update()

    def _renamed(self, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        name = " ".join(item.text().split()) or f"Zone {row + 1}"
        if name != item.text():
            self.list.blockSignals(True)
            item.setText(name)
            self.list.blockSignals(False)
        if self.canvas.zones[row].get("name") != name:
            self.canvas.zones[row]["name"] = name
            self.dirty = True
        self.canvas.update()
        self._update()

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self.canvas.zones.pop(row)
        self.dirty = True
        self._fill_list(select=min(row, len(self.canvas.zones) - 1))
        self.canvas.update()

    def _clear(self) -> None:
        if not self.canvas.zones:
            return
        if QMessageBox.question(self, "Delete all zones",
                                f"Delete all {len(self.canvas.zones)} zones of {self.cam.label}?\n"
                                "Without zones the whole picture is watched.",
                                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.canvas.zones.clear()
        self.dirty = True
        self._fill_list()
        self.canvas.update()

    def _update(self) -> None:
        c = self.canvas
        n = len(c.points)
        if c.image is None:
            self.hint.setText("The picture is needed to draw zones.")
        elif n == 0:
            self.hint.setText("Click on the picture to place the first corner of a zone.")
        elif n < 3:
            self.hint.setText(f"Click to add corners ({n} of at least 3).  Backspace: undo  ·  Esc: cancel")
        else:
            self.hint.setText("Double-click, Enter or click the first corner to finish.  Esc: cancel")
        # the buttons always keep their place, so the picture never moves while drawing
        self.undo_btn.setEnabled(n > 0)
        self.finish_btn.setEnabled(n >= 3)
        has = bool(c.zones)
        self.lists.setCurrentWidget(self.list if has else self.empty)
        self.rename_hint.setVisible(has)
        self.delete_btn.setEnabled(self.list.currentRow() >= 0)
        self.clear_btn.setEnabled(has)
        zones = len(c.zones)
        text = f"{zones} zone{'s' if zones != 1 else ''}" if zones else "No zones: the whole picture is watched"
        self.summary.setText(text + ("  ·  unsaved changes" if self.dirty else ""))

    def _save(self) -> None:
        if self.canvas.points and not self._confirm("The zone you are drawing is not finished. Save without it?",
                                                    "Save without it"):
            return
        self.cam.zones = [{"name": z["name"], "points": z["points"]} for z in self.canvas.zones]
        self.ctx.save()
        self.accept()

    def _confirm(self, text: str, yes_text: str) -> bool:
        box = QMessageBox(QMessageBox.Question, self.windowTitle(), text, parent=self)
        yes = box.addButton(yes_text, QMessageBox.AcceptRole)
        box.addButton("Keep editing", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def reject(self) -> None:
        if (self.dirty or self.canvas.points) and not self._confirm("Discard the changes to these zones?",
                                                                    "Discard"):
            return
        super().reject()

    def keyPressEvent(self, event):
        # Esc first cancels a zone being drawn, a second Esc closes the dialog
        if event.key() == Qt.Key_Escape and self.canvas.cancel_drawing():
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.canvas.close_zone():
                return
            if self.list.hasFocus():
                return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self.list and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Delete \
                and self.list.state() != QAbstractItemView.EditingState:
            self._delete()
            return True
        return False


def _swatch(color: str) -> QIcon:
    pm = QPixmap(28, 28)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    fill = QColor(c)
    fill.setAlpha(110)
    p.setBrush(fill)
    p.setPen(QPen(c, 3))
    p.drawRoundedRect(QRectF(3, 3, 22, 22), 6, 6)
    p.end()
    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)
