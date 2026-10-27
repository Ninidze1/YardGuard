"""Measure the brain worker's real memory: private (committed) RAM vs working set, and VRAM.

Usage:  run.cmd tests\\brain_memory.py      (loads the model once, ~1-2 min, then stops it)
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from local_data import SNAPS, need_brain, need_snapshots
from yardguard.brain import Brain

need_brain()
need_snapshots("check_ch5_20261023_010046.jpg")


def worker_memory() -> str:
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'brain_worker' -and "
          "$_.Name -eq 'python.exe' -and $_.WorkingSetSize -gt 200MB } | ForEach-Object { "
          "'working set {0:N1} GB, private {1:N1} GB' -f ($_.WorkingSetSize/1GB), ($_.PrivatePageCount/1GB) }")
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()


def vram() -> str:
    return subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
                          capture_output=True, text=True).stdout.strip()


brain = Brain()
print("VRAM before:", vram())
t = time.time()
brain.start()
while brain.status() not in ("ready", "error") and time.time() - t < 400:
    time.sleep(2)
print(f"status {brain.status()} after {time.time() - t:.0f} s", brain.last_error or "")
print("after load:", worker_memory(), "| VRAM", vram())
frame = cv2.imread(str(sorted(SNAPS.glob("check_ch5_*.jpg"))[-1]))
t = time.time()
boxes = brain.ask(frame, ["person crouching next to a car", "car"], mode="fast")
print(f"ask {time.time() - t:.2f} s ->", [b["label"] for b in boxes])
print("after ask:", worker_memory(), "| VRAM", vram(), "| stats", brain.stats())
brain.stop()
time.sleep(3)
print("after stop: VRAM", vram())
