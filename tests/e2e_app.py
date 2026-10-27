"""End-to-end run of the real YardGuard window (offscreen) against the real NVR.

Uses a copy of config.json under .cache/tests/e2e (never the real one), turns monitoring on
through the saved 'monitoring_on' choice, samples camera status / GPU / CPU, injects one
synthetic alert through the store + bridge + brain queue, screenshots every page in light
and dark, then closes the window and checks that nothing is left running.

    .venv\\Scripts\\python.exe tests\\e2e_app.py [monitor_seconds]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "e2e"
CACHE = ROOT / ".cache"
for key, sub in (("YOLO_CONFIG_DIR", "ultralytics"), ("TORCH_HOME", "torch"), ("PIP_CACHE_DIR", "pip"),
                 ("CUDA_CACHE_PATH", "cuda"), ("HF_HOME", "hf")):
    os.environ[key] = str(CACHE / sub)
os.environ["TEMP"] = os.environ["TMP"] = str(CACHE / "tmp")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
sys.path.insert(0, str(ROOT))
from local_data import need_config  # noqa: E402

need_config()
if OUT.exists():
    shutil.rmtree(OUT)
(OUT / "shots").mkdir(parents=True)
real = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
real["settings"]["events_dir"] = str(OUT / "events")
real["settings"]["monitoring_on"] = True
real["settings"]["theme"] = "light"
for site in real["sites"]:
    if site["id"] == real.get("active_site_id"):
        for cam in site["cameras"]:
            if cam["channel"] == 1:  # one zone, so the live view overlay and zone filtering are exercised
                cam["zones"] = [{"name": "Parking row", "points": [[0.05, 0.45], [0.95, 0.45], [0.98, 0.98],
                                                                     [0.02, 0.98]]}]
(OUT / "config.json").write_text(json.dumps(real, indent=2), encoding="utf-8")

import logging  # noqa: E402

logging.basicConfig(filename=OUT / "app.log", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(threadName)s %(message)s")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
from PySide6.QtCore import QSettings, QThreadPool, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from yardguard import config  # noqa: E402
from yardguard.engine import Alert, box_dict, crop_region, draw_boxes  # noqa: E402
from yardguard.gui import AppContext, MainWindow  # noqa: E402
from yardguard.settings_ui import ZoneEditorDialog  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", config.CONFIG_PATH
MONITOR_S = float(sys.argv[1]) if len(sys.argv) > 1 else 90
# Saved night frames of the real cameras (the user's events folder), for the synthetic alert and zone editor
SNAPS = Path(os.environ.get("YG_SNAPSHOTS") or Path(json.loads((ROOT / "config.json").read_text(
    encoding="utf-8"))["settings"]["events_dir"]) / "snapshots")
report: dict = {"samples": [], "checks": {}, "notes": []}
me = psutil.Process()


def say(*a) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def gpu() -> dict:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        used, util = (int(x) for x in r.stdout.strip().split(","))
        return {"vram_mb": used, "gpu_util": util}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def procs() -> list[psutil.Process]:
    return [me] + me.children(recursive=True)


def cpu_sample(seconds: float = 1.0) -> dict:
    ps = procs()
    for p in ps:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(seconds)
    out = {}
    for p in ps:
        try:
            name = p.name()
            out[name] = out.get(name, 0.0) + p.cpu_percent(None)
        except psutil.Error:
            pass
    total_ram = 0
    for p in ps:
        try:
            total_ram += p.memory_info().rss
        except psutil.Error:
            pass
    return {"cpu_pct_of_one_core": {k: round(v, 1) for k, v in out.items()},
            "machine_pct": round(sum(out.values()) / psutil.cpu_count(), 2), "ram_mb": total_ram // 2**20}


app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)  # the after-close checks run once the window is gone
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
QThreadPool.globalInstance().setMaxThreadCount(6)
ctx = AppContext(app)
ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)  # keep the user's window geometry untouched
ctx.apply_theme()
gpu_before = gpu()
say("GPU before", gpu_before)
win = MainWindow(ctx)
win.resize(1280, 800)
win.show()
theme = "light"


def shot(name: str) -> None:
    app.processEvents()
    path = OUT / "shots" / f"{theme}_{name}.png"
    win.grab().save(str(path))
    say("shot", path.name)


def settle(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        yield 0.1


def scroll_settings(fraction: float) -> None:
    sa = win.settings.findChild(QScrollArea)
    bar = sa.verticalScrollBar()
    bar.setValue(int(bar.maximum() * fraction))


def scroll_to(widget) -> None:
    sa = win.settings.findChild(QScrollArea)
    sa.ensureWidgetVisible(widget, 0, 0)
    sa.verticalScrollBar().setValue(widget.y())


def all_pages(tag: str):
    win.go_page(win.dashboard)
    yield from settle(1.5)
    shot(f"{tag}01_dashboard")
    win.go_page(win.alerts)
    yield from settle(1.5)
    shot(f"{tag}02_alerts")
    win.open_live(1)
    yield from settle(12)
    shot(f"{tag}03_live")
    win.go_page(win.sites)
    yield from settle(1.5)
    sa = win.sites.findChild(QScrollArea)
    sa.verticalScrollBar().setValue(sa.verticalScrollBar().maximum())
    yield from settle(0.5)
    shot(f"{tag}04_sites")
    site = ctx.cfg.active_site()
    src = next(iter(sorted(SNAPS.glob("check_ch1_*.jpg"), reverse=True)), None)
    frame = cv2.imdecode(np.fromfile(str(src), np.uint8), cv2.IMREAD_COLOR) if src else None
    dlg = ZoneEditorDialog(ctx, site, site.camera(1), parent=win, frame=frame)
    dlg.show()
    yield from settle(1.5)
    dlg.grab().save(str(OUT / "shots" / f"{theme}_{tag}04b_zone_editor.png"))
    dlg.done(0)
    dlg.deleteLater()
    win.go_page(win.settings)
    yield from settle(1)
    scroll_settings(0)
    shot(f"{tag}05_settings_top")
    scroll_to(win.settings.brain_card)
    yield from settle(0.8)
    shot(f"{tag}06_settings_brain")
    scroll_to(win.settings.monitoring_card)
    yield from settle(0.8)
    shot(f"{tag}07_settings_monitoring")
    scroll_settings(1)
    yield from settle(0.5)
    shot(f"{tag}08_settings_bottom")
    win.go_page(win.dashboard)


def inject_alert() -> Alert:
    src = next(iter(sorted(SNAPS.glob("check_ch5_*.jpg"))), None)
    frame = cv2.imdecode(np.fromfile(str(src), np.uint8), cv2.IMREAD_COLOR) if src else None
    if frame is None:
        frame = np.full((720, 1280, 3), 40, np.uint8)
    h, w = frame.shape[:2]
    subject = (w * 0.42, h * 0.45, w * 0.50, h * 0.80)
    car = (w * 0.30, h * 0.50, w * 0.62, h * 0.85)
    boxes = [box_dict(subject, "person", "subject"), box_dict(car, "car", "context")]
    ts = time.time()
    stamp = datetime.fromtimestamp(ts)
    a = Alert(id=f"{stamp:%Y%m%d-%H%M%S}-5-e2etest", site_id=ctx.cfg.active_site().id, channel=5,
              camera="Yard / street side", ts=ts, rule="prowling", title="Person checking several cars (test)",
              detail="Synthetic alert injected by tests/e2e_app.py to exercise toasts, the Alerts page and "
                     "the brain check.", severity="warning", boxes=boxes, verdict="pending")
    annotated = draw_boxes(frame.copy(), boxes[::-1])
    region = crop_region([np.array(subject), np.array(car)], w, h)
    mon = ctx.monitor
    mon._save_pictures(a, annotated, region, stamp)
    ctx.alert_store.add(a)
    ctx.alert_bridge.alert_added.emit(a)
    x1, y1, x2, y2 = region
    mon._queue_verify("5:e2e", (0, 0, ts), a, annotated, frame[y1:y2, x1:x2].copy(), (x1, y1))
    report["notes"].append(f"synthetic alert from {src.name if src else 'grey frame'}, crop {x2 - x1}x{y2 - y1}")
    return a


def script():
    global theme
    t0 = time.time()
    yield from settle(2)
    report["checks"]["monitoring_restored"] = ctx.monitor.running
    say("monitor running after restore:", ctx.monitor.running, "brain:", ctx.brain.status())
    next_shot = True
    while time.time() - t0 < MONITOR_S:
        yield from settle(5)
        sm = ctx.monitor.summary()
        st = ctx.monitor.camera_status()
        sample = {"t": round(time.time() - t0, 1), "summary": sm, "brain": ctx.brain.status(), **gpu(),
                  "cams": {ch: {k: v for k, v in s.items() if k in ("state", "fps", "objects", "backend", "error")}
                           for ch, s in st.items()}}
        sample.update(cpu_sample(1.0))
        report["samples"].append(sample)
        say(f"t={sample['t']}", sm["message"], "|", ctx.brain.status(), "| vram", sample.get("vram_mb"),
            "| cpu", sample["machine_pct"], "%", "|",
            {ch: (s["state"], s["fps"], s["backend"]) for ch, s in sample["cams"].items()})
        if next_shot and sm["watching"]:
            win.go_page(win.dashboard)
            yield from settle(3)
            shot("00_dashboard_monitoring")
            next_shot = False
    # Brain: wait until loaded (2 min cold start) so the verification path really runs
    t_wait = time.time()
    while ctx.brain.status() in ("starting", "installing") and time.time() - t_wait < 300:
        yield from settle(5)
    report["checks"]["brain_status_before_inject"] = ctx.brain.status()
    report["brain_stats"] = ctx.brain.stats()
    say("brain", ctx.brain.status(), ctx.brain.stats(), "vram", gpu())
    report["gpu_with_brain"] = gpu()
    report["cpu_with_brain"] = cpu_sample(2.0)

    updates = []
    ctx.alert_bridge.alert_updated.connect(lambda a: updates.append((a.id, a.verdict, list(a.brain_findings))))
    win.go_page(win.dashboard)
    yield from settle(1)
    a = inject_alert()
    yield from settle(1.5)
    report["checks"]["toast_shown"] = len(win.toasts.toasts)
    report["checks"]["badge_while_pending"] = win.alert_badge.text() if not win.alert_badge.isHidden() else ""
    shot("09_toast")
    t_inj = time.time()
    while not any(u[0] == a.id for u in updates) and time.time() - t_inj < 150:
        yield from settle(1)
    got = ctx.alert_store.get(a.id)
    report["checks"]["verdict"] = got.verdict if got else None
    report["checks"]["findings"] = got.brain_findings if got else None
    report["checks"]["verify_s"] = round(time.time() - t_inj, 1)
    report["checks"]["badge"] = win.alert_badge.text() if win.alert_badge.isVisible() else ""
    say("verdict", report["checks"]["verdict"], report["checks"]["findings"], "in", report["checks"]["verify_s"], "s")
    # Toast click path: open the alert on the Alerts page
    win._open_alert(a.id)
    yield from settle(2)
    shot("10_alert_detail")
    report["checks"]["alerts_page_current"] = win.stack.currentWidget() is win.alerts

    yield from all_pages("")
    theme = "dark"
    ctx.cfg.settings.theme = "dark"
    ctx.apply_theme()
    yield from settle(1)
    win._open_alert(a.id)
    yield from settle(1.5)
    shot("10_alert_detail")
    yield from all_pages("")
    report["final_status"] = ctx.monitor.camera_status()
    report["checks"]["children_before_close"] = sorted(p.name() for p in me.children(recursive=True))

    t_close = time.time()
    win.close()
    report["checks"]["close_s"] = round(time.time() - t_close, 2)
    yield from settle(2)
    report["checks"]["monitor_running_after_close"] = ctx.monitor.running
    report["checks"]["brain_after_close"] = ctx.brain.status()
    report["checks"]["children_after_close"] = sorted(p.name() for p in me.children(recursive=True))
    report["gpu_after_close"] = gpu()
    say("after close", report["checks"])
    app.quit()


gen = script()


def tick() -> None:
    try:
        delay = next(gen)
    except StopIteration:
        return
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        report["error"] = traceback.format_exc()
        app.quit()
        return
    QTimer.singleShot(int(delay * 1000), tick)


QTimer.singleShot(0, tick)
app.exec()
(OUT / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
say("report", OUT / "report.json")
