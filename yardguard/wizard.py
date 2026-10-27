"""Step-by-step setup for a new site: device, login, cameras, name."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QScrollArea, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .config import BRANDS, ROLES, Camera, Site
from .qt_util import run_async, set_prop
from .scan import scan_network, subnet_label
from .video import test_connection
from .widgets import (
    ImageView, button, card, fetch_thumb, label, muted, password_field, set_status, spin_box,
)


def brand_combo() -> QComboBox:
    combo = QComboBox()
    for key, text in BRANDS.items():
        combo.addItem(text, key)
    return combo


def form_layout() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    return form


class DeviceFinder(QWidget):
    """Scan button + table of recorders/cameras found on the local network."""

    picked = Signal(object)      # Device selected
    activated = Signal(object)   # Device double-clicked

    def __init__(self, ctx):
        super().__init__()
        self.devices = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        row = QHBoxLayout()
        self.scan_btn = button("Scan my network", "primary")
        ctx.bind_icon(self.scan_btn, "search", "on_primary")
        self.scan_btn.clicked.connect(self.scan)
        self.state = muted("", wrap=False)
        row.addWidget(self.scan_btn)
        row.addWidget(self.state, 1)
        lay.addLayout(row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Address", "Brand (guess)", "Open ports"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(150)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.activated.emit(self.devices[r]))
        lay.addWidget(self.table)

    def scan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.state.setText(f"Scanning {subnet_label()} … this takes a few seconds.")
        self.table.setRowCount(0)
        run_async(scan_network, on_done=self._done)

    def _done(self, result) -> None:
        self.scan_btn.setEnabled(True)
        if isinstance(result, Exception):
            self.state.setText("Scan failed. Enter the address manually below.")
            return
        self.devices = result
        self.table.setRowCount(len(result))
        for r, d in enumerate(result):
            for c, text in enumerate((d.ip, d.brand_label, ", ".join(map(str, d.ports)))):
                self.table.setItem(r, c, QTableWidgetItem(text))
        if result:
            self.state.setText(f"Found {len(result)} device(s). Select your recorder (NVR) or camera.")
            self.table.selectRow(0)
        else:
            self.state.setText("No cameras found. Check the PC is on the same network, or enter the address below.")

    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.picked.emit(self.devices[rows[0].row()])


class ScanDialog(QDialog):
    """Stand-alone 'Find on network' dialog used by the Sites page."""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find devices on your network")
        self.resize(620, 420)
        self.device = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)
        lay.addWidget(label("Find devices on your network", "SectionTitle"))
        lay.addWidget(muted("Looks for recorders and cameras that offer video (RTSP) on this network."))
        self.finder = DeviceFinder(ctx)
        self.finder.picked.connect(self._pick)
        self.finder.activated.connect(lambda d: (self._pick(d), self.accept()))
        lay.addWidget(self.finder, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        self.use_btn = button("Use this device", "primary")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.use_btn)
        lay.addLayout(row)
        self.finder.scan()

    def _pick(self, device) -> None:
        self.device = device
        self.use_btn.setEnabled(True)


# --- Wizard pages -----------------------------------------------------------

class WizardPage(QWidget):
    changed = Signal()
    title = ""
    subtitle = ""

    def __init__(self, wiz):
        super().__init__()
        self.wiz = wiz
        self.ctx = wiz.ctx

    def enter(self) -> None: ...
    def leave(self) -> None: ...
    def complete(self) -> bool:
        return True


class DevicePage(WizardPage):
    title = "Find your recorder"
    subtitle = "YardGuard reads video directly from your recorder (NVR) or IP camera over the local network."

    def __init__(self, wiz):
        super().__init__(wiz)
        self._scanned = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.finder = DeviceFinder(self.ctx)
        self.finder.picked.connect(self._use_device)
        self.finder.activated.connect(lambda d: (self._use_device(d), wiz.next()))
        lay.addWidget(self.finder, 1)

        frame, inner = card("Device details", "Filled in when you pick a device above, or type them yourself.")
        form = form_layout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("e.g. 192.168.1.108")
        self.brand = brand_combo()
        self.port = spin_box(1, 65535, 554)
        self.path = QLineEdit(wiz.site.custom_path)
        self.path.setToolTip("Use {channel} for the channel number and {subtype} for 0 = main, 1 = sub stream.")
        form.addRow("Address", self.host)
        form.addRow("Brand", self.brand)
        form.addRow("RTSP port", self.port)
        form.addRow("Stream path", self.path)
        inner.addLayout(form)
        self._form = form
        lay.addWidget(frame)

        self.host.textChanged.connect(self.changed)
        self.brand.currentIndexChanged.connect(self._brand_changed)
        self._brand_changed()

    def _brand_changed(self) -> None:
        self._form.setRowVisible(self.path, self.brand.currentData() == "custom")

    def _use_device(self, device) -> None:
        self.host.setText(device.ip)
        self.brand.setCurrentIndex(self.brand.findData(device.brand))
        self.port.setValue(554)

    def enter(self) -> None:
        if not self._scanned:
            self._scanned = True
            self.finder.scan()

    def leave(self) -> None:
        s = self.wiz.site
        s.host, s.brand, s.port = self.host.text().strip(), self.brand.currentData(), self.port.value()
        s.custom_path = self.path.text().strip() or s.custom_path

    def complete(self) -> bool:
        return bool(self.host.text().strip())


class LoginPage(WizardPage):
    title = "Sign in to the recorder"
    subtitle = ("Use an account that exists on the recorder. A view-only user is safest: "
                "YardGuard only reads video and never changes recorder settings.")

    def __init__(self, wiz):
        super().__init__(wiz)
        self.ok = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        frame, inner = card("Login")
        form = form_layout()
        self.user = QLineEdit()
        self.user.setPlaceholderText("e.g. admin or a view-only user")
        self.pw = password_field(self.ctx)
        form.addRow("Username", self.user)
        form.addRow("Password", self.pw)
        inner.addLayout(form)

        row = QHBoxLayout()
        self.test_btn = button("Test connection")
        self.ctx.bind_icon(self.test_btn, "check")
        self.test_btn.clicked.connect(self.test)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        row.addWidget(self.test_btn)
        row.addWidget(self.result, 1)
        inner.addLayout(row)
        self.skip = QCheckBox("Skip the test for now")
        self.skip.toggled.connect(self.changed)
        inner.addWidget(self.skip)
        lay.addWidget(frame)
        lay.addWidget(muted("The password is encrypted with your Windows account and stays on this PC."))
        lay.addStretch(1)
        for edit in (self.user, self.pw):
            edit.textChanged.connect(self._edited)

    def _edited(self) -> None:
        self.ok = False
        self.result.setText("")
        self.changed.emit()

    def test(self) -> None:
        site = copy.deepcopy(self.wiz.site)
        site.username = self.user.text().strip()
        url = site.rtsp_url(1, main=False, password=self.pw.text())
        self.test_btn.setEnabled(False)
        set_status(self.result, "checking", "Connecting…")
        run_async(test_connection, url, on_done=self._tested)

    def _tested(self, result) -> None:
        self.test_btn.setEnabled(True)
        ok, msg = (False, str(result)) if isinstance(result, Exception) else result
        self.ok = ok
        set_status(self.result, "online" if ok else "offline", ("✓ " if ok else "✕ ") + msg)
        self.changed.emit()

    def leave(self) -> None:
        self.wiz.site.username = self.user.text().strip()
        self.wiz.site.set_password(self.pw.text())

    def complete(self) -> bool:
        return self.ok or self.skip.isChecked()


class ChannelTile(QFrame):
    toggled = Signal()

    def __init__(self, channel: int):
        super().__init__()
        self.channel = channel
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 12)
        lay.setSpacing(8)
        self.thumb = ImageView("Checking…")
        self.thumb.setFixedHeight(118)
        self.thumb.clicked.connect(lambda: self.check.toggle())
        lay.addWidget(self.thumb)
        top = QHBoxLayout()
        self.check = QCheckBox(f"Channel {channel}")
        self.check.toggled.connect(self._toggled)
        self.status = QLabel()
        set_status(self.status, "checking")
        top.addWidget(self.check, 1)
        top.addWidget(self.status)
        lay.addLayout(top)
        self.name = QLineEdit()
        self.name.setPlaceholderText(f"Name, e.g. Yard parking")
        self.role = QComboBox()
        self.role.addItems(ROLES)
        lay.addWidget(self.name)
        lay.addWidget(self.role)

    def _toggled(self) -> None:
        set_prop(self, "selected", self.check.isChecked())
        self.toggled.emit()


class CamerasPage(WizardPage):
    title = "Choose cameras"
    subtitle = "YardGuard checks every channel and shows a preview. Tick the cameras to watch and give them clear names."

    def __init__(self, wiz):
        super().__init__(wiz)
        self.tiles: dict[int, ChannelTile] = {}
        self._checked_for = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        row = QHBoxLayout()
        row.addWidget(QLabel("Number of channels"))
        self.count = spin_box(1, 64, 8)
        self.count.setToolTip("How many camera inputs the recorder has (4, 8, 16 …). Use 1 for a single IP camera.")
        self.detect_btn = button("Detect cameras")
        self.ctx.bind_icon(self.detect_btn, "scan")
        self.detect_btn.clicked.connect(self.detect)
        self.state = muted()
        row.addWidget(self.count)
        row.addWidget(self.detect_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.state)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("Transparent")
        self.grid = QGridLayout(host)
        self.grid.setContentsMargins(0, 0, 6, 0)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignTop)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)

    def enter(self) -> None:
        s = self.wiz.site
        key = (s.host, s.port, s.brand, s.custom_path, s.username, s.password)
        if key != self._checked_for:
            self._checked_for = key
            self.detect()

    def detect(self) -> None:
        for tile in self.tiles.values():
            tile.deleteLater()
        self.tiles.clear()
        n = self.count.value()
        self._pending = n
        self._gen = getattr(self, "_gen", 0) + 1
        self.state.setText(f"Checking {n} channel(s)…")
        for i, ch in enumerate(range(1, n + 1)):
            tile = ChannelTile(ch)
            tile.toggled.connect(self.changed)
            self.tiles[ch] = tile
            self.grid.addWidget(tile, i // 3, i % 3)
            url = self.wiz.site.rtsp_url(ch, main=True)
            run_async(fetch_thumb, url, on_done=lambda r, ch=ch, g=self._gen: self._thumb(ch, r, g))
        self.changed.emit()

    def _thumb(self, ch: int, result, gen: int) -> None:
        tile = self.tiles.get(ch)
        if tile is None or gen != self._gen:
            return
        img, status = (None, "offline") if isinstance(result, Exception) else result
        tile.thumb.set_image(img, {"offline": "No connection", "nosignal": "No signal"}.get(status, ""))
        set_status(tile.status, status)
        tile.check.setChecked(status == "online")
        self._pending -= 1
        if self._pending <= 0:
            online = sum(1 for t in self.tiles.values() if t.check.isChecked())
            if online:
                self.state.setText(f"{online} camera(s) with video found. Channels without video are left unticked.")
            elif all(t.status.property("status") == "offline" for t in self.tiles.values()):
                self.state.setText("No channel could be opened. Go Back and check the address and login "
                                   "(use Test connection), then try again.")
            else:
                self.state.setText("No channel has video right now. Tick the cameras you want anyway, "
                                   "or check that the cameras are switched on.")

    def leave(self) -> None:
        self.wiz.site.cameras = [
            Camera(ch, t.name.text().strip() or f"Camera {ch}", t.role.currentText(), t.check.isChecked())
            for ch, t in self.tiles.items()
        ]

    def complete(self) -> bool:
        return any(t.check.isChecked() for t in self.tiles.values())


class FinishPage(WizardPage):
    title = "Name this site"
    subtitle = "A clear name helps when you add more places later, e.g. “Home yard” or “Office parking”."

    def __init__(self, wiz):
        super().__init__(wiz)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        frame, inner = card()
        form = form_layout()
        self.name = QLineEdit("Home")
        self.name.textChanged.connect(self.changed)
        form.addRow("Site name", self.name)
        inner.addLayout(form)
        lay.addWidget(frame)
        summary, sl = card("Summary")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        sl.addWidget(self.summary)
        lay.addWidget(summary)
        lay.addStretch(1)

    def enter(self) -> None:
        s = self.wiz.site
        chosen = [c.label for c in s.cameras if c.enabled]
        rows = [
            ("Device", f"{s.host}:{s.port}"),
            ("Brand", BRANDS.get(s.brand, s.brand)),
            ("Login", s.username or "none"),
            ("Cameras", f"{len(chosen)} selected: " + ", ".join(chosen) if chosen else "none"),
        ]
        self.summary.setText("<table cellspacing=6>" + "".join(
            f"<tr><td style='padding-right:16px'><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows) + "</table>")
        self.name.setFocus()
        self.name.selectAll()

    def leave(self) -> None:
        self.wiz.site.name = self.name.text().strip()

    def complete(self) -> bool:
        return bool(self.name.text().strip())


class SetupWizard(QDialog):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.site = Site()
        self.setWindowTitle("Add a site · YardGuard")
        self.resize(860, 700)
        self.setMinimumSize(720, 580)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(32, 24, 32, 12)
        tl.setSpacing(10)
        steps = QHBoxLayout()
        steps.setSpacing(4)
        self.step_labels = []
        for i, name in enumerate(("Device", "Login", "Cameras", "Finish")):
            if i:
                steps.addWidget(muted("—", wrap=False))
            lbl = label(f"{i + 1}  {name}", "Step")
            self.step_labels.append(lbl)
            steps.addWidget(lbl)
        steps.addStretch(1)
        tl.addLayout(steps)
        tl.addSpacing(6)
        self.title = label("", "PageTitle")
        self.subtitle = muted()
        tl.addWidget(self.title)
        tl.addWidget(self.subtitle)
        lay.addWidget(top)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(32, 8, 32, 16)
        self.stack = QStackedWidget()
        bl.addWidget(self.stack)
        lay.addWidget(body, 1)

        footer = QWidget()
        footer.setObjectName("WizardFooter")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(32, 14, 32, 16)
        self.cancel_btn = button("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.back_btn = button("Back")
        self.back_btn.clicked.connect(self.back)
        self.next_btn = button("Next", "primary")
        self.next_btn.setMinimumWidth(110)
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self.next)
        fl.addWidget(self.cancel_btn)
        fl.addStretch(1)
        fl.addWidget(self.back_btn)
        fl.addWidget(self.next_btn)
        lay.addWidget(footer)

        self.pages = [DevicePage(self), LoginPage(self), CamerasPage(self), FinishPage(self)]
        for page in self.pages:
            self.stack.addWidget(page)
            page.changed.connect(self._update)
        self.index = -1
        self._go(0)

    def _go(self, index: int) -> None:
        if self.index >= 0:
            self.pages[self.index].leave()
        self.index = index
        page = self.pages[index]
        self.stack.setCurrentWidget(page)
        self.title.setText(page.title)
        self.subtitle.setText(page.subtitle)
        for i, lbl in enumerate(self.step_labels):
            name = lbl.text().split("  ", 1)[1]
            state = "done" if i < index else "current" if i == index else "todo"
            lbl.setText(f"{'✓' if state == 'done' else i + 1}  {name}")
            set_prop(lbl, "state", state)
        page.enter()
        self._update()
        self.next_btn.setFocus()

    def _update(self) -> None:
        last = self.index == len(self.pages) - 1
        self.back_btn.setVisible(self.index > 0)
        self.next_btn.setText("Finish" if last else "Next")
        self.next_btn.setEnabled(self.pages[self.index].complete())

    def next(self) -> None:
        if not self.pages[self.index].complete():
            return
        if self.index == len(self.pages) - 1:
            self.pages[self.index].leave()
            self.accept()
        else:
            self._go(self.index + 1)

    def back(self) -> None:
        if self.index > 0:
            self._go(self.index - 1)
