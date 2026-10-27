"""Probe LocateAnything raw outputs (run with the brain venv python)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "yardguard"))
from local_data import BRAIN_MODEL, SNAPS, need_brain, need_snapshots  # noqa: E402

need_brain()
need_snapshots("check_ch1_20261023_010006.jpg", "check_ch5_20261023_010046.jpg")
import brain_worker as bw  # noqa: E402

t0 = time.perf_counter()
w = bw.Worker(str(BRAIN_MODEL), mem_efficient="--math" not in sys.argv)
print("load", time.perf_counter() - t0, "vram", w.vram_mb(), file=bw.PROTO, flush=True)
from PIL import Image  # noqa: E402

imgs = [str(p) for p in sorted(SNAPS.glob("check_ch1_*.jpg"))[:1] + sorted(SNAPS.glob("check_ch5_*.jpg"))[:1]]
for path in imgs:
    for phrases in (["person", "car"], ["car"], ["person crouching next to a car", "open car door", "car"]):
        for mode in ("fast", "hybrid"):
            im = w._prepare(Image.open(path).convert("RGB"), 896)
            t = time.perf_counter()
            raw = w.generate(im, phrases, mode, 1024, 1.0)
            w.torch.cuda.synchronize()
            dt = time.perf_counter() - t
            print(f"\n== {path[-28:]} {phrases} {mode} {dt:.2f}s size={im.size}", file=bw.PROTO)
            print(repr(raw), file=bw.PROTO)
            print(bw.parse_answer(raw, phrases)[:4], file=bw.PROTO, flush=True)
