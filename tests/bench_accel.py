"""Benchmark yardguard.accel on the saved night frames: PyTorch FP32 vs FP16 (batched, landscape input).

Usage:  run.cmd tests\\bench_accel.py [--models yolo11s yolo11m] [--imgsz 1280] [--iters 40]
Each configuration runs in its own process so the VRAM numbers are clean. Writes
.cache/tests/bench_accel.json and prints a summary table.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
FRAMES = ROOT / ".cache" / "tests" / "frames"
OUT = ROOT / ".cache" / "tests"
CONF = 0.35
CONFIGS = ["fp32-loop640", "fp32", "fp16-square", "fp16", "auto"]


def load_frames():
    import cv2
    import numpy as np

    frames = []
    for ch in (1, 2, 5, 7):
        img = cv2.imdecode(np.fromfile(FRAMES / f"ch{ch}.png", np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        s = min(1.0, 1280 / max(h, w))  # what FrameSource delivers at long side 1280
        if s < 1:
            img = cv2.resize(img, (int(w * s) // 2 * 2, int(h * s) // 2 * 2), interpolation=cv2.INTER_AREA)
        frames.append(img)
    return frames


def vram_used() -> int:
    import pynvml

    pynvml.nvmlInit()
    return pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0)).used // 2**20


def child(model: str, config: str, imgsz: int, iters: int) -> dict:
    import numpy as np
    import torch
    from ultralytics import YOLO

    from yardguard import accel
    from yardguard.video import ensure_model

    frames = load_frames()
    base = vram_used()
    t0 = time.time()
    if config == "fp32-loop640":  # what the live view Detector does today: one frame at a time, default 640
        yolo = YOLO(str(ensure_model(model)))

        def run():
            return [yolo.predict(f, conf=CONF, classes=list(accel.CLASSES), device=0, verbose=False)[0]
                    .boxes.data.cpu().numpy() for f in frames]
        backend = "pytorch-fp32 loop"
    elif config in ("fp32", "fp16", "fp16-square"):
        det = accel.FastDetector(YOLO(str(ensure_model(model))), "pytorch" if config == "fp32" else "pytorch-fp16",
                                 model, accel.input_shape(imgsz, landscape=config != "fp16-square"), 4)
        run = lambda: det.predict(frames, CONF)  # noqa: E731
        backend = det.backend
    else:
        det = accel.load_fast_detector(model, imgsz, progress=print)
        run = lambda: det.predict(frames, CONF)  # noqa: E731
        backend = det.backend
        if det.fallback_reason:
            print("fallback:", det.fallback_reason)
    load_s = time.time() - t0
    for _ in range(5):
        run()
    torch.cuda.synchronize()
    lat = []
    for _ in range(iters):
        t = time.perf_counter()
        res = run()
        lat.append((time.perf_counter() - t) * 1000)
    vram = vram_used() - base
    dets = [np.asarray(r, np.float32).reshape(-1, 6).round(1).tolist() for r in res]
    lat.sort()
    return {"model": model, "config": config, "backend": backend, "load_s": round(load_s, 1),
            "median_ms": round(statistics.median(lat), 2), "p90_ms": round(lat[int(len(lat) * 0.9)], 2),
            "min_ms": round(lat[0], 2), "vram_mb": vram, "detections": dets}


def iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def compare(ref: list, other: list) -> dict:
    """Greedy same-class IoU>=0.5 matching of other's boxes to the reference boxes, over all frames."""
    matched, ious, n_ref, n_other = 0, [], 0, 0
    for r_boxes, o_boxes in zip(ref, other):
        n_ref += len(r_boxes)
        n_other += len(o_boxes)
        used = set()
        for r in r_boxes:
            best, bj = 0.0, -1
            for j, o in enumerate(o_boxes):
                if j not in used and o[5] == r[5]:
                    v = iou(r, o)
                    if v > best:
                        best, bj = v, j
            if best >= 0.5:
                used.add(bj)
                matched += 1
                ious.append(best)
    return {"ref_boxes": n_ref, "boxes": n_other, "matched": matched,
            "mean_iou": round(sum(ious) / len(ious), 3) if ious else None}


def counts(dets: list) -> list[dict]:
    from yardguard.video import CLASSES

    out = []
    for frame in dets:
        c: dict[str, int] = {}
        for d in frame:
            c[CLASSES[int(d[5])]] = c.get(CLASSES[int(d[5])], 0) + 1
        out.append(c)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["yolo11s", "yolo11m"])
    ap.add_argument("--configs", nargs="+", default=CONFIGS)
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--iters", type=int, default=40)
    ap.add_argument("--child", nargs=2)
    args = ap.parse_args()
    if args.child:
        print("RESULT " + json.dumps(child(args.child[0], args.child[1], args.imgsz, args.iters)))
        return

    results = []
    for model in args.models:
        for config in args.configs:
            r = subprocess.run([sys.executable, __file__, "--child", model, config, "--imgsz", str(args.imgsz),
                                "--iters", str(args.iters)], capture_output=True, text=True)
            line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
            if not line:
                print(f"{model} {config} FAILED\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}")
                continue
            res = json.loads(line[7:])
            results.append(res)
            print(f"{model:8} {config:13} {res['backend']:18} median {res['median_ms']:7.2f} ms  "
                  f"p90 {res['p90_ms']:7.2f} ms  VRAM +{res['vram_mb']} MB  load {res['load_s']} s  "
                  f"counts {counts(res['detections'])}", flush=True)
        ref = next((r for r in results if r["model"] == model and r["config"] == "fp32"), None)
        if ref:
            for r in results:
                if r["model"] == model and r is not ref:
                    r["vs_fp32"] = compare(ref["detections"], r["detections"])
                    print(f"   {r['config']:13} vs fp32: {r['vs_fp32']}")
    (OUT / "bench_accel.json").write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
