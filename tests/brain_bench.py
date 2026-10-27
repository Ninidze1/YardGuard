"""Benchmark the LocateAnything worker on real night frames (run with .venv-brain python).

Writes .cache\\tests\\brain_bench.json and annotated PNGs next to it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "yardguard"))
import brain_worker as bw  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

OUT = ROOT / ".cache" / "tests" / "brain_bench"
OUT.mkdir(parents=True, exist_ok=True)
from local_data import SNAPS as SNAP, need_brain, need_snapshots  # noqa: E402

need_brain()
need_snapshots("check_ch1_20261023_010006.jpg", "check_ch5_20261023_010046.jpg")
PROMPTS = ["person crouching next to a car", "hand on a car door handle", "open car door",
           "person lying under a car", "broken car window", "person looking into a car window"]
ALL = PROMPTS + ["person", "car"]


def gpu_used_mb() -> int:
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True)
    return int(r.stdout.strip().splitlines()[0])


def draw(img: Image.Image, boxes: list[dict], path: Path) -> None:
    img = img.copy()
    d = ImageDraw.Draw(img)
    w, h = img.size
    for b in boxes:
        xy = (b["x1"] * w, b["y1"] * h, b["x2"] * w, b["y2"] * h)
        color = (0, 255, 0) if b["label"] == "car" else (255, 60, 60) if b["label"] == "person" else (255, 200, 0)
        d.rectangle(xy, outline=color, width=3)
        d.text((xy[0] + 3, xy[1] + 2), b["label"][:24], fill=color)
    img.save(path)


def main() -> None:
    before = gpu_used_mb()
    t0 = time.perf_counter()
    w = bw.Worker(str(ROOT / "models" / "LocateAnything-3B"))
    load_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    w.warm_up()
    warm_s = time.perf_counter() - t1
    res = {"load_s": load_s, "warmup_s": warm_s, "gpu_before_mb": before, "gpu_after_load_mb": gpu_used_mb(),
           "torch_reserved_mb": w.vram_mb(), "runs": []}
    print(json.dumps({k: v for k, v in res.items() if k != "runs"}), file=bw.PROTO, flush=True)

    frames = {"ch1": SNAP / "check_ch1_20261023_010006.jpg", "ch5": SNAP / "check_ch5_20261023_010046.jpg"}
    # Car crops, the kind of picture "verify" mode sends (person + nearby cars)
    ch5 = Image.open(frames["ch5"]).convert("RGB")
    crops = {"ch5_crop_frontcar": ch5.crop((100, 560, 1180, 1080)),
             "ch5_crop_mid": ch5.crop((560, 60, 1360, 480))}
    tmp = OUT / "in"
    tmp.mkdir(exist_ok=True)
    images = dict(frames)
    for name, im in crops.items():
        p = tmp / f"{name}.jpg"
        im.save(p, quality=92)
        images[name] = p

    def run(name, path, phrases, mode, side, tag="", guard=True):
        w.guard.enabled = guard
        r = w.ask({"image": str(path), "phrases": phrases, "mode": mode, "max_side": side})
        w.guard.enabled = True
        counts = {}
        for b in r["boxes"]:
            counts[b["label"]] = counts.get(b["label"], 0) + 1
        row = {"image": name, "mode": mode, "max_side": side, "phrases": tag or ",".join(phrases),
               "latency_s": r["latency_s"], "calls": r["calls"], "size": r["size"], "counts": counts,
               "vram_mb": r["vram_mb"], "guard": guard, "raw_head": r["raw"][:300]}
        res["runs"].append(row)
        print(json.dumps(row), file=bw.PROTO, flush=True)
        with Image.open(path) as im:
            draw(im.convert("RGB"), r["boxes"], OUT / f"{name}_{mode}_{side}_{tag or 'x'}_{'g' if guard else 'ng'}.png")

    for name, path in images.items():
        for mode in ("fast", "hybrid"):
            for side in (640, 896, 1280):
                run(name, path, ALL, mode, side, "all8")
        run(name, path, ["person", "car"], "fast", 896, "person_car")
    # Guard on/off cost comparison
    run("ch5", frames["ch5"], ["car"], "fast", 896, "car", guard=False)
    run("ch5", frames["ch5"], ["car"], "fast", 896, "car", guard=True)
    # Repeatability: same input three times
    for _ in range(3):
        run("ch5", frames["ch5"], ["person", "car"], "fast", 896, "repeat")
    res["peak_reserved_mb"] = int(w.torch.cuda.max_memory_reserved() / 2**20)
    (ROOT / ".cache" / "tests" / "brain_bench.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print("DONE", file=bw.PROTO, flush=True)


if __name__ == "__main__":
    main()
