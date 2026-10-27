"""Plate feasibility survey on recorded clips: follow every vehicle, keep its sharpest crops, find and read
the plate, and save a table + picture per vehicle so readability can be judged (and labelled) by eye.

Usage:  run.cmd tests\\plates_survey.py <clip.mp4> [fps] [max_minutes]
Output: .cache/tests/plates/survey/<clip name>/  (vehicles.json, v<N>_car.jpg, v<N>_plate.png, v<N>/c<k>.jpg)
Runs on the GPU only when it is nearly free (the app's AI brain may be using it), otherwise on the CPU.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from yardguard import plates

VEHICLES = [2, 3, 5, 7]
KEEP = 6  # best crops kept per vehicle


def gpu_free() -> bool:
    try:
        used = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=10).stdout.split()[0]
        return int(used) < 3000
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return False


def iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-6)


def main() -> None:
    clip = Path(sys.argv[1])
    fps = float(sys.argv[2]) if len(sys.argv) > 2 else 3
    max_min = float(sys.argv[3]) if len(sys.argv) > 3 else 1e9
    out = ROOT / ".cache" / "tests" / "plates" / "survey" / clip.stem
    out.mkdir(parents=True, exist_ok=True)
    device = "cuda" if gpu_free() else "cpu"
    print("device", device, flush=True)
    from ultralytics import YOLO
    veh_model = YOLO(str(ROOT / "models" / "yolo11s.pt"))
    reader = plates.PlateReader(device=device)

    cap = cv2.VideoCapture(str(clip))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 15
    step = max(1, round(src_fps / fps))
    tracks: list[dict] = []
    frame_no, t0 = 0, time.time()
    while frame_no / src_fps < max_min * 60:
        ok = cap.grab()
        if not ok:
            break
        frame_no += 1
        if frame_no % step:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        res = veh_model.predict(frame, classes=VEHICLES, conf=0.35, device=0 if device == "cuda" else "cpu",
                                verbose=False)[0]
        for box in res.boxes.xyxy.int().tolist():
            x1, y1, x2, y2 = box
            if x2 - x1 < 60:
                continue
            # long gap allowed: night detections of parked cars flicker
            match = max((t for t in tracks if frame_no - t["last"] <= src_fps * 10), key=lambda t: iou(t["box"], box),
                        default=None)
            if match is None or iou(match["box"], box) < 0.3:
                match = {"id": len(tracks), "first": frame_no, "crops": [], "widths": []}
                tracks.append(match)
            match.update(box=box, last=frame_no)
            match["widths"].append(x2 - x1)
            crop = frame[y1:y2, x1:x2].copy()
            match["crops"].append((plates.plate_quality(crop), frame_no, crop))
            match["crops"] = sorted(match["crops"], key=lambda c: -c[0])[:KEEP]
        if frame_no % (step * 100) == 0:
            print(f"{frame_no / src_fps / 60:.1f} min, {len(tracks)} vehicles, {time.time() - t0:.0f} s", flush=True)

    rows = []
    for t in tracks:
        crops = [c for _, _, c in t["crops"]]
        found = reader.find_plates(crops)
        plate_w = max((p.shape[1] for p, _ in found), default=0)
        result = reader.read(crops)
        row = {"id": t["id"], "t_s": round(t["first"] / src_fps, 1), "frames": len(t["widths"]),
               "car_px": max(t["widths"]), "plate_found": f"{len(found)}/{len(crops)}", "plate_px": plate_w,
               "text": result["text"] if result else "", "conf": result["confidence"] if result else 0.0,
               "quality": round(t["crops"][0][0], 3)}
        rows.append(row)
        cv2.imwrite(str(out / f"v{t['id']:03d}_car.jpg"), t["crops"][0][2])
        keep = out / f"v{t['id']:03d}"  # all kept crops: material for a labelled best-of-N evaluation
        keep.mkdir(exist_ok=True)
        for k, (_, _, crop) in enumerate(t["crops"]):
            cv2.imwrite(str(keep / f"c{k}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if result and result["plate_crop"] is not None:
            pc = result["plate_crop"]
            cv2.imwrite(str(out / f"v{t['id']:03d}_plate.png"),
                        cv2.resize(pc, None, fx=max(1, 240 / pc.shape[1]), fy=max(1, 240 / pc.shape[1])))
        elif found:
            cv2.imwrite(str(out / f"v{t['id']:03d}_plate.png"), max(found, key=lambda f: f[1])[0])
        print(row, flush=True)
    (out / "vehicles.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    seen = [r for r in rows if r["plate_px"]]
    read = [r for r in rows if r["conf"] >= 0.5]
    print(f"vehicles {len(rows)}, plate seen {len(seen)}, read with conf>=0.5 {len(read)}, "
          f"median plate px {int(np.median([r['plate_px'] for r in seen])) if seen else 0}")


if __name__ == "__main__":
    main()
