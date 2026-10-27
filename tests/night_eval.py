"""Compare overlay stability on recorded night clips: old tracker vs new, with and without denoising.

Usage:  run.cmd tests\\night_eval.py        (clips from tests\\record_clips.py in .cache/tests/night)

Metrics per clip (the yard is mostly parked cars, so a good result is a steady count):
  raw@conf   detections per frame the plain detector reports at the user's certainty (min-max)
  ids        distinct objects created over the clip (ideal: about the number of cars)
  count      objects drawn per frame, mean and std (std near 0 = nothing blinking)
  blinks     times an object disappeared from the drawing and came back (lower is better)
  jitter     average movement of drawn box edges between frames, in pixels (lower is better)
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from yardguard.accel import load_fast_detector
from yardguard.config import load_config
from yardguard.overlay import LOW_SCORE, DisplayTracker, _iou

NIGHT = ROOT / ".cache" / "tests" / "night"


def old_tracker_class():
    src = (NIGHT / "overlay_old.py").read_text(encoding="utf-8").replace("from .video import", "from yardguard.video import")
    path = NIGHT / "_overlay_old_mod.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("overlay_old", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["overlay_old"] = mod  # dataclasses look the module up while building the class
    spec.loader.exec_module(mod)
    return mod.DisplayTracker


def frames(path: Path):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 15
    i = 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        yield i / fps, f
        i += 1
    cap.release()


def detections(det, path: Path, denoise: str) -> list[tuple[float, np.ndarray]]:
    out, avg = [], None
    for t, f in frames(path):
        if denoise == "temporal":  # running average of frames: random sensor noise cancels, still scenes stay sharp
            if avg is None:
                avg = f.astype(np.float32)
            else:
                cv2.accumulateWeighted(f, avg, 0.5)
            f = cv2.convertScaleAbs(avg)
        out.append((t, det.predict([f], LOW_SCORE)[0]))
    return out


def score(dets_seq, make, high: float, new_api: bool) -> dict:
    tr = make()
    shown_prev, boxes_prev, counts, blinks, moves, gone, dups = set(), {}, [], 0, [], set(), []
    for t, d in dets_seq:
        if new_api:
            tracks = tr.update(d, high=high, now=t)
        else:
            tracks = tr.update(d[d[:, 4] >= high])
        ids = {x.title for x in tracks}
        blinks += len(ids & gone)
        gone = (gone | (shown_prev - ids)) - ids
        for x in tracks:
            if x.title in boxes_prev:
                moves.append(float(np.abs(x.box - boxes_prev[x.title]).mean()))
        boxes_prev = {x.title: x.box.copy() for x in tracks}
        for i, a in enumerate(tracks):  # two drawn boxes of the same class mostly on top of each other
            for b in tracks[i + 1:]:
                if a.label == b.label and _iou(a.box, b.box) >= 0.6:
                    dups.append(1)
        shown_prev = ids
        counts.append(len(tracks))
    c = np.array(counts[10:])  # skip warm-up frames
    return {"ids": sum(tr._next.values()), "dups": f"{len(dups) / len(counts):.2f}",
            "count": f"{c.mean():.1f}±{c.std():.2f}", "blinks": blinks, "jitter": f"{np.mean(moves):.2f}"}


def main() -> None:
    s = load_config().settings
    det = load_fast_detector(s.model, s.detect_size)
    Old = old_tracker_class()
    print(f"model {s.model} @ {s.detect_size}, certainty {s.confidence:.2f}, backend {det.backend}\n")
    for clip in sorted(NIGHT.glob("ch*.mp4")):
        for denoise in ("none", "temporal"):
            seq = detections(det, clip, denoise)
            raw = [int((d[:, 4] >= s.confidence).sum()) for _, d in seq]
            print(f"{clip.stem} denoise={denoise:8} raw@{s.confidence:.2f}: {min(raw)}-{max(raw)} per frame "
                  f"(mean {np.mean(raw):.1f})")
            for name, make, new_api in (("old tracker", Old, False), ("new tracker", DisplayTracker, True)):
                r = score(seq, make, s.confidence, new_api)
                print(f"    {name:12} ids {r['ids']:3}  count {r['count']:>9}  duplicates/frame {r['dups']}  "
                      f"jitter {r['jitter']} px")
        print()


if __name__ == "__main__":
    main()
