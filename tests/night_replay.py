"""Replay recorded night clips through the old and new monitoring rules and count the alerts.

The clips are quiet night footage of parked cars, so every alert is a false alert.
Usage:  run.cmd tests\\night_replay.py     (clips: .cache/tests/night/long/ch*.mp4 from record_clips.py)
"""
import importlib.util
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from yardguard import engine as new_engine
from yardguard.accel import load_fast_detector
from yardguard.config import Settings, load_config

LONG = ROOT / ".cache" / "tests" / "night" / "long"
NIGHT = datetime(2026, 10, 23, 1, 0, 0).timestamp()


def old_engine():
    src = (ROOT / ".cache" / "tests" / "night" / "engine_old.py").read_text(encoding="utf-8")
    src = src.replace("from .video import", "from yardguard.video import").replace("from .", "from yardguard.")
    path = LONG / "_engine_old_mod.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("engine_old", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engine_old"] = mod
    spec.loader.exec_module(mod)
    return mod


def sampled_detections(det, clip: Path, fps: float):
    """Detections at the monitoring rate (detect_fps), low threshold so both engines can filter."""
    cap = cv2.VideoCapture(str(clip))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 15
    every = max(1, round(src_fps / fps))
    out, i, size = [], 0, None
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if i % every == 0:
            size = (f.shape[1], f.shape[0])
            out.append((NIGHT + i / src_fps, det.predict([f], 0.10)[0]))
        i += 1
    cap.release()
    return out, size


def save_alert_frame(clip: Path, seq, hit, name: str) -> None:
    ts = getattr(hit, "_ts", seq[0][0])
    cap = cv2.VideoCapture(str(clip))
    cap.set(cv2.CAP_PROP_POS_MSEC, (ts - NIGHT) * 1000)
    ok, f = cap.read()
    cap.release()
    if ok:
        x1, y1, x2, y2 = (int(v) for v in hit.subject[0])
        cv2.rectangle(f, (x1, y1), (x2, y2), (0, 0, 255), 3)
        crop = f[max(0, y1 - 150):y2 + 150, max(0, x1 - 250):x2 + 250]
        cv2.imwrite(str(LONG / f"{name}.png"), f)
        cv2.imwrite(str(LONG / f"{name}_zoom.png"), cv2.resize(crop, None, fx=2, fy=2))
        print(f"        frame at {(ts - NIGHT):.1f} s saved: {LONG / (name + '.png')}")


def replay(mod, seq, size, s, old_api: bool):
    pipe = mod.CameraPipeline(1, "Yard", "Parking")
    hits = []
    for ts, d in seq:
        if old_api:  # the old engine got detections already cut at the user's certainty
            d = d[d[:, 4] >= s.confidence]
        new = pipe.process(d, ts, size, [], s)
        for h in new:
            h._ts = ts
        hits += new
    created = next(pipe.tracker._ids) - 1
    return hits, created, len(pipe.parked)


def main() -> None:
    real = load_config().settings
    s = Settings(confidence=real.confidence, night_start="00:00", night_end="06:00")  # all rules on
    det = load_fast_detector(real.model, real.detect_size)
    old = old_engine()
    print(f"model {real.model}, certainty {s.confidence:.2f}, {real.detect_fps:g} checks/s, all rules on, whole frame\n")
    for clip in sorted(LONG.glob("ch*.mp4")):
        seq, size = sampled_detections(det, clip, real.detect_fps)
        minutes = (seq[-1][0] - seq[0][0]) / 60 if seq else 0
        print(f"{clip.stem}: {minutes:.1f} min, {len(seq)} checks")
        for name, mod, old_api in (("old engine", old, True), ("new engine", new_engine, False)):
            hits, created, parked = replay(mod, seq, size, s, old_api)
            by_rule = dict(Counter(h.rule for h in hits))
            print(f"    {name}: {len(hits)} alerts {by_rule or ''}  | objects created {created}, parked cars {parked}")
            for h in hits:  # save the frame of each alert so it can be checked by eye
                at = h.ts if hasattr(h, "ts") else None
                print(f"        {h.rule}: {h.title} | box {np.round(h.subject[0]).astype(int).tolist()}")
                save_alert_frame(clip, seq, h, f"{clip.stem}_{name.split()[0]}_{h.rule}")
        print()


if __name__ == "__main__":
    main()
