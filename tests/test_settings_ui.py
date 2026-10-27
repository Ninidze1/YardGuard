"""Offscreen render + behaviour checks for yardguard/settings_ui.py.

Run:  .venv\\Scripts\\python.exe tests\\test_settings_ui.py
PNGs go to .cache\\tests\\settings_ui\\. Uses a synthetic config (never the real config.json)
and a Brain stub, so nothing is downloaded and no camera is contacted.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "settings_ui"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QPointF, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

from yardguard import config  # noqa: E402
from yardguard.config import Camera, Config, Site  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"


class StubBrain:
    """Brain-like object with a settable status."""

    def __init__(self):
        self.state = "not_installed"
        self.last_error = None
        self.calls: list[str] = []
        self.cancelled = False
        self._stats = {"last_latency_s": None, "vram_mb": None, "avg_latency_s": None}

    def status(self):
        return self.state

    def install(self, progress=None):
        self.calls.append("install")
        self.cancelled = False
        steps = ["Creating the brain runtime", "Installing PyTorch", "Downloading model weights"]
        for i in range(30):
            if self.cancelled:
                raise RuntimeError("cancelled")
            if progress:
                progress(steps[min(i // 10, 2)], i + 1, 30)
            time.sleep(0.01)
        self.state = "stopped"

    def cancel_install(self):
        self.calls.append("cancel")
        self.cancelled = True

    user_stopped = False
    install_step = None

    def start(self, user=False):
        self.calls.append("start")
        self.state = "starting"
        if user:
            self.user_stopped = False

    def stop(self, user=False):
        self.calls.append("stop")
        self.state = "stopped"
        if user:
            self.user_stopped = True

    def stats(self):
        return dict(self._stats)


class FakeWin:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, text, error=False):
        self.messages.append(("ERR " if error else "") + text)


def make_cfg() -> Config:
    site = Site(name="Test yard", host="127.0.0.1", username="", cameras=[
        Camera(1, "Yard parking", "Parking"), Camera(2, "Stairwell", "Entrance"),
        Camera(5, "Yard / street side", "Parking"), Camera(7, "Hallway", "Indoor", monitor=False)])
    site.cameras[0].zones = [{"name": "Parking row", "points": [[0.1, 0.55], [0.6, 0.5], [0.75, 0.95], [0.05, 0.95]]}]
    cfg = Config(sites=[site], active_site_id=site.id)
    return cfg


def pump(app, ms=60):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def apply_theme(ctx, app, name, extra):
    ctx.cfg.settings.theme = name
    ctx.apply_theme()
    app.setStyleSheet(app.styleSheet() + extra)


def snapshot_frame():
    snaps_dir = Path(os.environ.get("YARDGUARD_TEST_SNAPS", str(ROOT / "events" / "snapshots")))
    snaps = sorted(snaps_dir.glob("check_ch1_*.jpg"), key=lambda p: -p.stat().st_size)
    if snaps:
        return cv2.imdecode(np.frombuffer(snaps[0].read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    img = np.full((720, 1280, 3), 60, np.uint8)
    cv2.rectangle(img, (300, 400), (700, 650), (90, 90, 160), -1)
    return img


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    from yardguard.gui import AppContext
    from yardguard import settings_ui as su

    ctx = AppContext(app)
    ctx.cfg = make_cfg()
    ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)
    ctx.ui.clear()
    ctx.brain = StubBrain()
    win = FakeWin()
    failures: list[str] = []

    def check(cond, what):
        print(("PASS " if cond else "FAIL ") + what)
        if not cond:
            failures.append(what)

    for theme in ("light", "dark"):
        apply_theme(ctx, app, theme, su.EXTRA_QSS)
        page = QWidget()
        page.setObjectName("Root")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("Transparent")
        col = QVBoxLayout(host)
        col.setContentsMargins(32, 28, 38, 24)
        col.setSpacing(16)
        mon = su.MonitoringCard(ctx, win)
        brain = su.BrainCard(ctx, win)
        col.addWidget(mon)
        col.addWidget(brain)
        col.addStretch(1)
        scroll.setWidget(host)
        outer.addWidget(scroll)
        page.resize(1048, 760)  # 1280 wide window minus the 232 px sidebar
        page.show()
        pump(app, 150)
        h = host.sizeHint().height()
        host.resize(1048, h)
        mon.grab().save(str(OUT / f"monitoring_{theme}.png"))

        stub = ctx.brain
        for state in ("not_installed", "installing", "stopped", "starting", "ready", "error"):
            stub.state = state if state != "installing" else "not_installed"
            brain._installing = state == "installing"
            brain._step = ("Downloading model weights (3.1 of 7.3 GB)", 42, 100)
            brain._error = "The brain process exited (CUDA out of memory)." if state == "error" else None
            stub._stats = {"avg_latency_s": 1.18, "last_latency_s": 1.2, "vram_mb": 7980} if state == "ready" else {}
            brain.refresh()
            pump(app, 40)
            brain.grab().save(str(OUT / f"brain_{state}_{theme}.png"))
        brain._installing = False

        stub.state = "ready"
        stub._stats = {"avg_latency_s": 1.18, "last_latency_s": 1.2, "vram_mb": 7980}
        check(sorted(brain.mode_options) == ["off", "verify"], f"[{theme}] only Off and Verify")
        brain.mode_options["off"].radio.click()
        pump(app, 40)
        brain.grab().save(str(OUT / f"brain_off_{theme}.png"))
        check(not brain.details.isEnabled() and brain.off_note.isVisible(), f"[{theme}] off disables details")
        brain.mode_options["verify"].radio.click()
        pump(app, 40)
        brain.refresh(force=True)
        pump(app, 40)
        brain.grab().save(str(OUT / f"brain_verify_{theme}.png"))
        check(ctx.cfg.settings.brain_mode == "verify" and brain.details.isEnabled()
              and not hasattr(brain, "builtin") and not hasattr(brain, "objects_box")
              and brain.prompts_label.text() == "Things to look for"
              and "really there" in brain.prompts_help.text(), f"[{theme}] verify: no LA-main parts")
        check(brain.buttons["stop"].isEnabled() and mon.fps.isEnabled() and mon.size.isEnabled()
              and not hasattr(mon, "la_banner"), f"[{theme}] Stop and YOLO performance controls enabled")
        page.close()
        page.deleteLater()
        pump(app, 40)

    # --- behaviour checks (light theme) ---
    apply_theme(ctx, app, "light", su.EXTRA_QSS)
    stub = ctx.brain = StubBrain()
    brain = su.BrainCard(ctx, win)
    brain.resize(1000, 900)
    brain.show()
    pump(app)

    ctx.cfg.settings.brain_mode = "verify"
    brain._select_mode("verify")

    def answer(text):
        def go():
            box = next((w for w in app.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()), None)
            if box is None:
                QTimer.singleShot(30, go)
                return
            btn = next(b for b in box.buttons() if b.text() == text)
            btn.click()
        QTimer.singleShot(30, go)

    # install: progress, finish -> auto start because mode is verify
    brain._action("install")
    for _ in range(200):
        pump(app, 10)
        if not brain._installing:
            break
    pump(app, 200)  # start() runs on the thread pool
    brain.refresh()
    check(stub.calls[:2] == ["install", "start"], f"install then auto-start ({stub.calls})")
    check(stub.state == "starting" and brain.buttons["stop"].isVisibleTo(brain), "starting shows Stop")
    stub.state = "ready"
    brain.refresh()
    check("Running" in brain.st_title.text(), "ready title")
    brain._action("stop")
    pump(app, 200)
    check(stub.state == "stopped" and brain.buttons["start"].isVisibleTo(brain), "stop -> Start shown")
    check(stub.user_stopped and "stays off" in brain.st_text.text(), "user Stop is remembered and explained")

    # prompt list editing
    pl = brain.prompts
    n = len(ctx.cfg.settings.brain_prompts)
    pl.list.setCurrentRow(0)
    pl._remove()
    check(len(ctx.cfg.settings.brain_prompts) == n - 1, "remove prompt saves")
    pl._add()
    pump(app)
    item = pl.list.item(pl.list.count() - 1)
    pl.list.closePersistentEditor(item)
    item.setText("  person   climbing the fence ")
    pump(app)
    check(ctx.cfg.settings.brain_prompts[-1] == "person climbing the fence", "edit prompt normalised + saved")
    item.setText("")
    pump(app, 80)
    check("" not in ctx.cfg.settings.brain_prompts and pl.list.count() == n - 1, "empty prompt removed")
    answer("&Yes")
    pl._reset()
    pump(app, 100)
    check(ctx.cfg.settings.brain_prompts == su._defaults("brain_prompts"), "reset restores defaults")
    brain.generation.setCurrentIndex(brain.generation.findData("hybrid"))
    check(ctx.cfg.settings.brain_generation == "hybrid", "generation saved")

    # monitoring card
    mon = su.MonitoringCard(ctx, win)
    mon.fps.setValue(8)
    mon.size.setCurrentIndex(mon.size.findData(1600))
    mon.r_loiter.setChecked(False)
    from PySide6.QtCore import QTime
    mon.night_start.setTime(QTime(22, 30))
    s = ctx.cfg.settings
    check(s.detect_fps == 8.0 and s.detect_size == 1600, "fps + size saved")
    check(s.rule_loitering is False and not mon.loiter.isEnabled(), "rule off disables its control")
    check(s.night_start == "22:30" and mon.night_len.text() == "(7 h 30 min)", f"night hours ({mon.night_len.text()})")
    check(mon.incident_gap.value() == 5 and mon.incident_gap.suffix() == " min", "incident gap shown in minutes")
    mon.incident_gap.setValue(7)
    check(s.incident_gap_s == 420, f"incident gap saved in seconds ({s.incident_gap_s})")
    saved = config.load_config().settings
    check(saved.detect_size == 1600 and saved.night_start == "22:30", "written to the test config file")
    check(saved.incident_gap_s == 420, "incident gap written to the test config file")

    # zone editor
    site = ctx.cfg.active_site()
    cam = site.camera(1)
    frame = snapshot_frame()
    for theme in ("light", "dark"):
        apply_theme(ctx, app, theme, su.EXTRA_QSS)
        dlg = su.ZoneEditorDialog(ctx, site, cam, frame=frame)
        dlg.resize(1120, 700)
        dlg.show()
        pump(app, 100)
        c = dlg.canvas
        before = len(c.zones)
        r = c.image_rect()
        px = lambda x, y: QPointF(r.x() + x * r.width(), r.y() + y * r.height())
        for x, y in ((0.55, 0.35), (0.9, 0.3), (0.95, 0.6), (0.6, 0.65)):
            c.points.append(c.to_norm(px(x, y)))
        c.mouse = px(0.551, 0.352)  # hovering the first point -> close indicator
        dlg._update()
        c.update()
        pump(app, 60)
        dlg.grab().save(str(OUT / f"zones_drawing_{theme}.png"))
        check(c.near_first(), f"[{theme}] hover near first point detected")
        c.close_zone()
        pump(app, 60)
        check(len(c.zones) == before + 1 and dlg.list.count() == before + 1 and dlg.dirty, f"[{theme}] zone closed and listed")
        dlg.grab().save(str(OUT / f"zones_done_{theme}.png"))
        if theme == "light":
            dlg.list.item(1).setText("Street side cars")
            check(c.zones[1]["name"] == "Street side cars", "rename zone")
            dlg._save()
            check(cam.zones[1]["name"] == "Street side cars" and len(cam.zones[1]["points"]) == 4, "zones saved")
            saved_cam = config.load_config().active_site().camera(1)
            check(len(saved_cam.zones) == 2, "zones in config file")
        else:
            dlg.dirty = False
            dlg.done(0)
        dlg.deleteLater()

    # live theme switch restyles the colour-tinted banner
    apply_theme(ctx, app, "light", su.EXTRA_QSS)
    banner = su.Banner(ctx, "Title", "Text")
    banner.show()
    pump(app, 60)
    light_css = banner.styleSheet()
    apply_theme(ctx, app, "dark", su.EXTRA_QSS)
    pump(app, 120)
    check(banner.styleSheet() != light_css and "251, 191, 36" in banner.styleSheet(), "banner follows theme switch")
    banner.close()

    # empty state + failed grab
    apply_theme(ctx, app, "light", su.EXTRA_QSS)
    dlg = su.ZoneEditorDialog(ctx, site, site.camera(5), frame=frame)
    dlg.show()
    pump(app, 80)
    dlg.grab().save(str(OUT / "zones_empty_light.png"))
    dlg._frame_loaded(None)
    pump(app, 40)
    dlg.grab().save(str(OUT / "zones_failed_light.png"))
    check(dlg.retry_btn.isVisible(), "failed grab shows Try again")
    dlg.done(0)

    print("notifications:", win.messages[-5:])
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
