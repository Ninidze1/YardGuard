"""Side-by-side video of the old and new overlay on a recorded night clip.

Usage:  run.cmd tests\\night_compare_video.py [ch1]     -> .cache/tests/night/<clip>_old_vs_new.mp4
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from night_eval import NIGHT, frames, old_tracker_class
from yardguard.accel import load_fast_detector
from yardguard.config import load_config
from yardguard.overlay import LOW_SCORE, DisplayTracker, draw_tracks
from yardguard.stream import ffmpeg_path

clip = NIGHT / f"{sys.argv[1] if len(sys.argv) > 1 else 'ch1'}.mp4"
s = load_config().settings
det = load_fast_detector(s.model, s.detect_size)
old, new = old_tracker_class()(), DisplayTracker()
out = clip.with_name(f"{clip.stem}_old_vs_new.mp4")
W, H = 1920, 540  # two 960x540 halves
enc = subprocess.Popen([ffmpeg_path(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                        "-r", "15", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(out)],
                       stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)


def label(img: np.ndarray, text: str) -> np.ndarray:
    cv2.rectangle(img, (0, 0), (img.shape[1], 34), (20, 20, 20), -1)
    cv2.putText(img, text, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA)
    return img


for t, f in frames(clip):
    d = det.predict([f], LOW_SCORE)[0]
    a = draw_tracks(f.copy(), old.update(d[d[:, 4] >= s.confidence]), "boxes")
    b = draw_tracks(f.copy(), new.update(d, high=s.confidence, now=t), "boxes")
    left = label(cv2.resize(a, (960, 540), interpolation=cv2.INTER_AREA), "BEFORE: per-frame boxes")
    right = label(cv2.resize(b, (960, 540), interpolation=cv2.INTER_AREA), "AFTER: tracked + smoothed + parked memory")
    enc.stdin.write(np.hstack([left, right]).tobytes())
enc.stdin.close()
enc.wait()
print(out, f"{out.stat().st_size / 2**20:.1f} MB")
