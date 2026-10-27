"""Benchmark yardguard.stream.FrameSource on the real cameras.

Usage:  run.cmd tests\\bench_stream.py [--seconds 60] [--mode gpu|cpu|both] [--fps 5] [--size 1280]
Prints delivered fps per camera, ffmpeg CPU use and checks that stop() leaves no ffmpeg behind.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from yardguard.config import load_config  # noqa: E402
from yardguard.stream import FrameSource  # noqa: E402

CHANNELS = (1, 2, 5, 7)


def ffmpeg_children() -> list[psutil.Process]:
    return [p for p in psutil.Process().children(recursive=True) if p.name().lower().startswith("ffmpeg")]


def run(mode: str, seconds: float, fps: float, size: int) -> dict:
    site = load_config().active_site()
    gpu = _nvml()
    vram0 = _vram_mb(gpu)
    sources = {ch: FrameSource(site.rtsp_url(ch, main=True), fps, size, gpu_decode=mode == "gpu") for ch in CHANNELS}
    t0 = time.time()
    for s in sources.values():
        s.start()
    first: dict[int, float] = {}
    while time.time() - t0 < 25 and len(first) < len(sources):
        for ch, s in sources.items():
            if ch not in first and s.latest() is not None:
                first[ch] = time.time() - t0
        time.sleep(0.05)
    start_frames = {ch: s.frames for ch, s in sources.items()}
    procs = ffmpeg_children()
    for p in procs:
        p.cpu_percent(None)
    vram1 = _vram_mb(gpu)
    dec_samples, mem_samples = [], []
    t1 = time.time()
    while time.time() - t1 < seconds:
        time.sleep(1)
        if gpu:
            import pynvml
            dec_samples.append(pynvml.nvmlDeviceGetDecoderUtilization(gpu)[0])
    elapsed = time.time() - t1
    cpu = sum(p.cpu_percent(None) for p in procs if p.is_running())
    mem = sum(p.memory_info().rss for p in procs if p.is_running()) / 2**20
    res = {"mode": mode, "seconds": round(elapsed, 1), "ffmpeg_processes": len(procs),
           "ffmpeg_cpu_percent_of_one_core": round(cpu, 1),
           "ffmpeg_cpu_percent_of_machine": round(cpu / psutil.cpu_count(), 2),
           "ffmpeg_rss_mb": round(mem), "nvdec_util_avg": round(sum(dec_samples) / len(dec_samples), 1) if dec_samples else None,
           "vram_delta_mb": (vram1 - vram0) if gpu else None, "cameras": {}}
    for ch, s in sources.items():
        res["cameras"][ch] = {"backend": s.backend, "size": s.size, "first_frame_s": round(first.get(ch, -1), 2),
                              "fps": round((s.frames - start_frames[ch]) / elapsed, 2), "error": s.error,
                              "restarts": s.restarts}
    t2 = time.time()
    for s in sources.values():
        s.stop()
    res["stop_s"] = round(time.time() - t2, 2)
    time.sleep(0.5)
    res["ffmpeg_left_after_stop"] = len(ffmpeg_children())
    return res


def _vram_mb(handle) -> int:
    if not handle:
        return 0
    import pynvml
    return pynvml.nvmlDeviceGetMemoryInfo(handle).used // 2**20


def _nvml():
    try:
        import pynvml
        pynvml.nvmlInit()
        return pynvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:  # noqa: BLE001 - optional measurement
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--mode", default="both")
    ap.add_argument("--fps", type=float, default=5)
    ap.add_argument("--size", type=int, default=1280)
    args = ap.parse_args()
    modes = ["gpu", "cpu"] if args.mode == "both" else [args.mode]
    results = [run(m, args.seconds, args.fps, args.size) for m in modes]
    out = Path(__file__).resolve().parent.parent / ".cache" / "tests" / "bench_stream.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
