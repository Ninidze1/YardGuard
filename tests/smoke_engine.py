"""Smoke test of yardguard.engine against the real NVR: starts the Monitor on the active
site's monitored cameras for a short while, prints live status, then checks that every
thread and ffmpeg process is gone. Reads config.json but never saves it.

    .venv\\Scripts\\python.exe tests\\smoke_engine.py [seconds]
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "engine_smoke"
sys.path.insert(0, str(ROOT))
for key, sub in (("TEMP", "tmp"), ("TMP", "tmp"), ("YOLO_CONFIG_DIR", "ultralytics"), ("TORCH_HOME", "torch")):
    os.environ.setdefault(key, str(ROOT / ".cache" / sub))

from local_data import need_config  # noqa: E402
from yardguard.config import load_config  # noqa: E402
from yardguard.engine import AlertStore, Monitor  # noqa: E402

need_config()


def ffmpeg_count() -> int:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/NH"], capture_output=True, text=True)
    return r.stdout.lower().count("ffmpeg.exe")


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
    cfg = load_config()
    s = cfg.settings
    s.events_dir = str(OUT)          # in memory only; config.json is not saved
    s.night_start = s.night_end = "00:00"  # treat the whole day as night so people raise alerts
    if os.environ.get("SMOKE_NO_GATING"):
        s.motion_gating = False
    site = cfg.active_site()
    print("site:", site.name if site else None,
          "monitored:", [c.channel for c in site.enabled_cameras() if c.monitor] if site else [])
    before = ffmpeg_count()
    threads_before = threading.active_count()
    store = AlertStore(s.events_dir)
    alerts = []
    mon = Monitor(cfg, store, None, on_alert=alerts.append)
    t0 = time.time()
    mon.start()
    while time.time() - t0 < seconds:
        time.sleep(3)
        st = mon.camera_status()
        src_fps = {c.channel: round(getattr(c.source, "fps_actual", 0.0), 1) for c in mon._cams}
        line = "  ".join(f"ch{ch}:{v['state']} {v['fps']}/{src_fps.get(ch)}fps {v['backend']} {v['objects']}"
                         + (f" ERR {v['error']}" if v["error"] else "") for ch, v in st.items())
        print(f"[{time.time() - t0:5.1f}s] {mon.message} | det={mon.detector_backend} | {line}")
    t1 = time.time()
    mon.stop()
    print(f"stop took {time.time() - t1:.2f} s; running={mon.running}")
    time.sleep(1)
    after = ffmpeg_count()
    print(f"ffmpeg processes before={before} after={after}; threads before={threads_before} "
          f"after={threading.active_count()} ({[t.name for t in threading.enumerate()]})")
    print(f"alerts raised: {len(alerts)}")
    for a in alerts[:10]:
        print("  ", a.id, a.rule, a.title, a.severity, a.verdict, a.snapshot_path)
    store.close()
    return 0 if after <= before and not mon.running else 1


if __name__ == "__main__":
    sys.exit(main())
