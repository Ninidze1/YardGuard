"""How often does the brain report suspicious behaviour on frames with nobody in them?

Uses clean frames (no drawn boxes) from .cache/tests/frames and the default brain prompts.
Usage:  run.cmd tests\\brain_false_positives.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2

from yardguard.brain import Brain
from yardguard.config import Settings

prompts = Settings().brain_prompts
frames = sorted((ROOT / ".cache" / "tests" / "frames").glob("ch*.png"))
brain = Brain()
brain.start()
t = time.time()
while brain.status() not in ("ready", "error") and time.time() - t < 400:
    time.sleep(2)
print("brain", brain.status(), f"{time.time() - t:.0f} s")
try:
    for path in frames:
        frame = cv2.imread(str(path))
        h, w = frame.shape[:2]
        crops = {"full": frame, "left half": frame[:, : w // 2], "right half": frame[:, w // 2 :]}
        for name, img in crops.items():
            for run in range(2):
                boxes = brain.ask(img, prompts, mode="fast")
                hits = [b["label"] for b in boxes if b["label"] in prompts]
                print(f"{path.stem:4} {name:10} run{run}: {len(hits)} behaviour hits {sorted(set(hits))}")
finally:
    brain.stop()
