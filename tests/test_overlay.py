"""Overlay tests: tracking stability on synthetic jitter, then a real-camera render of both styles.

Usage:  run.cmd tests\\test_overlay.py           (unit checks + real CAM 1 / CAM 5 frames)
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

from yardguard.overlay import KEEP_MOVING_S, KEEP_PARKED_S, DisplayTracker, draw_tracks

DT = 1 / 15  # camera frame interval

failures = 0


def check(name: str, ok: bool) -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name)


rng = np.random.default_rng(0)
CAR = 2

# 1) a parked car whose raw box jitters by +-4 px: drawn box must move much less
tr = DisplayTracker()
base = np.array([400, 300, 700, 500], float)
raw_moves, shown_moves, prev_raw, prev_shown = [], [], None, None
for i in range(60):
    raw = base + rng.uniform(-4, 4, 4)
    tracks = tr.update(np.array([[*raw, 0.8, CAR]], np.float32), now=i * DT)
    if tracks:
        if prev_raw is not None:
            raw_moves.append(np.abs(raw - prev_raw).max())
            shown_moves.append(np.abs(tracks[0].box - prev_shown).max())
        prev_raw, prev_shown = raw, tracks[0].box.copy()
check(f"parked car jitter reduced ({np.mean(raw_moves):.1f} px raw -> {np.mean(shown_moves):.2f} px shown)",
      np.mean(shown_moves) < np.mean(raw_moves) * 0.3)
check("one stable id for the parked car", len({t.id for t in tr.tracks}) == 1 and tr.tracks[0].title == "Car 1")

# 2) one-frame detections are never drawn (flicker filter)
tr = DisplayTracker()
ghost = np.array([[10, 10, 60, 60, 0.5, 0]], np.float32)
check("single-frame ghost not drawn", tr.update(ghost, now=0.0) == [])
for i in range(1, 6):
    shown = tr.update(ghost, now=i * DT)
check("drawn once seen for a few frames", len(shown) == 1)

# 3) a moving object survives a short gap and is dropped after KEEP_MOVING_S
empty = np.zeros((0, 6), np.float32)
check("kept through a short miss", len(tr.update(empty, now=6 * DT)) == 1)
check("dropped after the keep time", tr.update(empty, now=5 * DT + KEEP_MOVING_S + 0.1) == [])

# 4) ByteTrack idea: weak detections keep a known car, but never start a new one
tr = DisplayTracker()
car = [400, 300, 700, 500]
for i in range(10):
    tr.update(np.array([[*car, 0.8, CAR]], np.float32), high=0.4, now=i * DT)
weak = np.array([[*car, 0.2, CAR]], np.float32)
check("weak detection keeps the known car", all(len(tr.update(weak, high=0.4, now=(10 + i) * DT)) == 1 for i in range(30)))
tr2 = DisplayTracker()
check("weak detections never start a new object",
      all(tr2.update(weak, high=0.4, now=i * DT) == [] for i in range(20)))

# 5) a parked car survives a long noisy gap, a moving one does not
tr = DisplayTracker()
for i in range(90):  # 6 s still -> parked
    tr.update(np.array([[*car, 0.8, CAR]], np.float32), now=i * DT)
gap_end = 90 * DT + KEEP_PARKED_S * 0.75
check(f"parked car kept through {KEEP_PARKED_S * 0.75:.0f} s without detections", len(tr.update(empty, now=gap_end)) == 1)

# 6) a walking person is followed closely (no lag)
tr = DisplayTracker()
lag = []
for i in range(40):
    box = np.array([100 + i * 12, 200, 160 + i * 12, 380], float)
    tracks = tr.update(np.array([[*box, 0.9, 0]], np.float32), now=i * DT)
    if tracks and i > 5:
        lag.append(abs(tracks[0].box[0] - box[0]))
check(f"walking person followed (avg lag {np.mean(lag):.1f} px at 12 px/frame)", np.mean(lag) < 15)

# 7) two cars keep separate ids; a shifted second box on the same car is not a new car
tr = DisplayTracker()
for i in range(5):
    tracks = tr.update(np.array([[0, 0, 100, 80, 0.9, CAR], [300, 0, 400, 80, 0.9, CAR]], np.float32), now=i * DT)
check("two cars, two ids", sorted(t.title for t in tracks) == ["Car 1", "Car 2"])
tracks = tr.update(np.array([[0, 0, 100, 80, 0.9, CAR], [8, 4, 104, 84, 0.9, CAR],
                             [300, 0, 400, 80, 0.9, CAR]], np.float32), now=5 * DT)
check("duplicate box on the same car ignored", len(tracks) == 2 and sum(tr._next.values()) == 2)

# 8) drawing speed on a 1080p frame with 12 objects
frame = np.full((1080, 1920, 3), 40, np.uint8)
tr = DisplayTracker()
dets = np.array([[60 + i * 150, 300, 200 + i * 150, 420, 0.8, CAR if i % 3 else 0] for i in range(12)], np.float32)
for i in range(6):
    tracks = tr.update(dets, now=i * DT)
for style in ("boxes", "corners"):
    t0 = time.perf_counter()
    for _ in range(20):
        draw_tracks(frame.copy(), tracks, style)
    ms = (time.perf_counter() - t0) / 20 * 1000
    check(f"{style}: draw 12 objects on 1080p in {ms:.1f} ms", ms < 15)

# 7) real frames from the cameras, both styles, saved for viewing
out = ROOT / ".cache" / "tests" / "overlay"
out.mkdir(parents=True, exist_ok=True)
try:
    import cv2

    from yardguard.accel import load_fast_detector
    from yardguard.config import load_config

    s = load_config().settings
    det = load_fast_detector(s.model, s.detect_size)
    for path in sorted((ROOT / ".cache" / "tests" / "frames").glob("ch[15].png")):
        img = cv2.imread(str(path))
        tr = DisplayTracker()
        for i in range(6):  # a few frames so tracks are confirmed, as in live view
            tracks = tr.update(det.predict([img], 0.1)[0], high=s.confidence, now=i * DT)
        for style in ("boxes", "corners"):
            cv2.imwrite(str(out / f"{path.stem}_{style}.png"), draw_tracks(img.copy(), tracks, style))
    print(f"real frames rendered to {out} with {det.backend}")
except Exception as exc:  # no GPU/model: unit checks above still count
    print("real-frame render skipped:", exc)

print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)
