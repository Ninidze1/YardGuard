"""End-to-end test of yardguard.brain.Brain from the MAIN venv.

start() -> wait ready -> ask() -> stats() -> stop(), and checks that GPU memory is freed.
Run: .venv\\Scripts\\python.exe tests\\test_brain_client.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from local_data import SNAPS, need_brain, need_snapshots  # noqa: E402
from yardguard.brain import Brain, BrainError, base_python  # noqa: E402

need_brain()
need_snapshots("check_ch5_20261023_010046.jpg")


def gpu_used_mb() -> int:
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True)
    return int(r.stdout.strip().splitlines()[0])


def main() -> int:
    print("base python:", base_python())
    brain = Brain()
    print("status:", brain.status())
    assert brain.status() == "stopped", brain.status()
    try:
        brain.ask(np.zeros((100, 100, 3), np.uint8), ["car"])
        raise AssertionError("ask() before start() must raise")
    except BrainError as e:
        print("ask before start -> BrainError:", e)

    before = gpu_used_mb()
    t0 = time.perf_counter()
    brain.start()
    assert brain.status() == "starting"
    assert brain.wait_ready(600), f"not ready: {brain.status()} {brain.last_error}"
    cold = time.perf_counter() - t0
    loaded = gpu_used_mb()
    print(f"ready after {cold:.1f} s (worker load_s={brain.load_s}), GPU {before} -> {loaded} MB")

    frame = cv2.imread(str(SNAPS / "check_ch5_20261023_010046.jpg"))
    h, w = frame.shape[:2]
    for mode in ("fast", "hybrid"):
        t = time.perf_counter()
        boxes = brain.ask(frame, ["person", "car"], mode=mode, timeout=120)
        print(f"{mode}: {len(boxes)} boxes in {time.perf_counter() - t:.2f} s (round trip)")
        for b in boxes[:5]:
            print("   ", b["label"], [round(b[k]) for k in ("x1", "y1", "x2", "y2")])
        assert all(0 <= b["x1"] < b["x2"] <= w and 0 <= b["y1"] < b["y2"] <= h for b in boxes)
        assert any(b["label"] == "car" for b in boxes), "expected cars in ch5"
    crop = frame[560:1080, 100:1180]
    boxes = brain.ask(crop, ["person crouching next to a car", "open car door", "car"])
    print("crop:", [(b["label"], [round(b[k]) for k in ("x1", "y1", "x2", "y2")]) for b in boxes])
    print("empty phrases ->", brain.ask(frame, []))
    print("stats:", brain.stats())
    assert brain.stats()["last_latency_s"] is not None

    brain.stop()
    print("status after stop:", brain.status())
    time.sleep(3)
    after = gpu_used_mb()
    print(f"GPU after stop: {after} MB (freed {loaded - after} MB)")
    assert loaded - after > 5000, "GPU memory was not freed"
    try:
        brain.ask(frame, ["car"])
        raise AssertionError("ask() after stop() must raise")
    except BrainError:
        pass
    # Restart works after stop
    brain.start()
    assert brain.wait_ready(600)
    print("restart ok, ask:", len(brain.ask(frame, ["car"])), "boxes")
    brain.stop()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
