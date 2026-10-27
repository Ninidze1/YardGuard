"""Replay the recorded night clips through the tracker and a set of areas drawn over the parked
cars, the driveway and the yard, and list every area event. Nothing moves in these clips (quiet
night footage of parked cars), so every "occupied" after the first minute, every freed / blocked /
presence / crossing / changed event is a false event.

Usage:  run.cmd tests\\replay_areas.py [--cached]
Detections are cached in .cache/tests/areas/replay (--cached replays without the GPU).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "areas" / "replay"
os.environ.setdefault("YARDGUARD_CONFIG", str(OUT / "config.json"))
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yardguard.areas import AreaEvaluator, describe, draw_area  # noqa: E402
from yardguard.config import Settings, new_area  # noqa: E402
from yardguard.engine import CameraPipeline, box_dict, draw_boxes  # noqa: E402

LONG = ROOT / ".cache" / "tests" / "night" / "long"
NIGHT = datetime(2026, 10, 23, 1, 0, 0).timestamp()
W, H = 1280, 720


def rect(x1, y1, x2, y2):
    return [[x1 / W, y1 / H], [x2 / W, y1 / H], [x2 / W, y2 / H], [x1 / W, y2 / H]]


def poly(*pts):
    return [[x / W, y / H] for x, y in pts]


def areas_for(clip: str) -> list[dict]:
    """Drawn by eye on the first frame of each clip (1280 x 720 pixels)."""
    if clip == "ch1":
        driveway = poly((200, 300), (900, 240), (900, 420), (200, 470))
        return [
            new_area("parking_spot", "polygon", rect(420, 120, 720, 260), "White Lexus"),
            new_area("parking_spot", "polygon", rect(620, 60, 880, 160), "Silver sedan"),
            new_area("parking_spot", "polygon", rect(0, 180, 210, 400), "White SUV"),
            new_area("parking_spot", "polygon", rect(470, 520, 1030, 720), "Front car"),
            new_area("no_parking", "polygon", driveway, "Driveway", params={"max_minutes": 1}),
            new_area("presence", "polygon", driveway, "Driveway people", objects=["person"]),
            new_area("loitering", "polygon", rect(0, 60, 1280, 720), "Near the cars", params={"seconds": 10}),
            new_area("count", "line", poly((820, 180), (820, 430)), "Road line", objects=["car", "person"]),
            new_area("line_cross", "line", poly((250, 300), (250, 470)), "Driveway entry", objects=["person"]),
            new_area("left_object", "polygon", rect(250, 300, 750, 430), "Driveway floor"),
            new_area("state_watch", "polygon", rect(450, 0, 640, 80), "Building door"),
        ]
    yard = rect(100, 150, 820, 420)
    return [
        new_area("parking_spot", "polygon", rect(280, 430, 730, 720), "Black car"),
        new_area("parking_spot", "polygon", rect(430, 20, 600, 100), "White car 1"),
        new_area("parking_spot", "polygon", rect(610, 40, 810, 125), "White car 2"),
        new_area("no_parking", "polygon", yard, "Yard", params={"max_minutes": 1}),
        new_area("presence", "polygon", rect(100, 150, 850, 720), "Yard people", objects=["person"]),
        new_area("count", "line", poly((100, 300), (850, 300)), "Yard line", objects=["car", "person"]),
        new_area("left_object", "polygon", rect(150, 200, 700, 420), "Yard floor"),
        new_area("state_watch", "polygon", rect(1030, 180, 1280, 650), "Laundry"),
        new_area("state_watch", "polygon", rect(380, 140, 720, 270), "Bench"),
    ]


def frames(clip: Path, fps: float):
    """(index, ts, frame at 1280 px) at the detection rate, like FrameSource delivers them."""
    cap = cv2.VideoCapture(str(clip))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 15
    every = max(1, round(src_fps / fps))
    i = 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if i % every == 0:
            yield i, NIGHT + i / src_fps, cv2.resize(f, (W, H), interpolation=cv2.INTER_AREA)
        i += 1
    cap.release()


def detections(clip: Path, s: Settings, fps: float, model: str, cached: bool) -> dict[int, np.ndarray]:
    path = OUT / f"{clip.stem}_dets.npz"
    if path.exists():
        data = np.load(path)
        return {int(k): data[k] for k in data.files}
    if cached:
        raise SystemExit(f"No cached detections for {clip.name}: run once without --cached")
    from yardguard.accel import load_fast_detector
    det = load_fast_detector(model, s.detect_size)
    out = {i: det.predict([f], 0.10)[0] for i, _, f in frames(clip, fps)}
    np.savez_compressed(path, **{str(k): v for k, v in out.items()})
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    real = {}
    real_path = ROOT / "config.json"
    if real_path.exists():  # read only: the user's model and certainty
        real = json.loads(real_path.read_text(encoding="utf-8")).get("settings", {})
    s = Settings(confidence=float(real.get("confidence", 0.35)), night_start="00:00", night_end="06:00")
    fps = float(real.get("detect_fps", 5.0))
    model = real.get("model", "yolo11s")
    cached = "--cached" in sys.argv
    for old in OUT.glob("ch*_ev*.jpg"):
        old.unlink()
    print(f"model {model}, certainty {s.confidence:.2f}, {fps:g} checks/s\n")
    for clip in sorted(LONG.glob("ch*.mp4")):
        dets = detections(clip, s, fps, model, cached)
        areas = areas_for(clip.stem)
        pipe = CameraPipeline(1, clip.stem, "Parking")
        ev = AreaEvaluator(1, clip.stem)
        hits, n, last_ts = [], 0, NIGHT
        for i, ts, frame in frames(clip, fps):
            pipe.process(dets.get(i, np.zeros((0, 6), np.float32)), ts, (W, H), [], s)
            tracks = [t for t in pipe.tracker.tracks if t.confirmed]
            for hit in ev.update(areas, tracks, frame, ts, (s.night_start, s.night_end)):
                hits.append(hit)
                img = draw_area(frame.copy(), hit.area)
                if hit.box is not None:
                    draw_boxes(img, [box_dict(hit.box, hit.event.label, "subject")])
                cv2.imwrite(str(OUT / f"{clip.stem}_ev{len(hits):02d}_{hit.event.kind}.jpg"), img)
            n, last_ts = n + 1, ts
        print(f"{clip.stem}: {(last_ts - NIGHT) / 60:.1f} min, {n} checks, {len(areas)} areas, "
              f"{len(hits)} events {dict(Counter(h.event.kind for h in hits))}")
        for k, h in enumerate(hits, 1):
            e = h.event
            box = [round(v) for v in h.box] if h.box is not None else ""
            print(f"    {k:2d}. {e.ts - NIGHT:6.1f} s  {e.area_name:16s} {e.kind:10s} {describe(e)}  {box}")
        live = ev.live(last_ts)
        print("    live at the end: " + "; ".join(f"{a['name']}: {live[a['id']]['text']}" for a in areas
                                                   if a["id"] in live))
        print()


if __name__ == "__main__":
    main()
