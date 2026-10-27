"""Replay the recorded daytime clips (1920x1080, ch5 driveway + barrier, ch1 yard) through the real
Monitor + areas + plate reader with number plate lines drawn where cars pass, and list every plate
event: what was read, how sure, how wide the chosen plate was and when the best moment was.

Usage:  run.cmd tests\\replay_plates.py <run name> [--before] [--no-fuse] [--between] [--cands N] [--read-top N]
        [--dump] [--start S] [--minutes N] [--clips ch5,ch1] [--fps 6]
--before replays the code saved in .cache/tests/plates_best/before (the pipeline before the best-moment
search). Detections come from .cache/tests/plates_best/work/<clip>_dets8.npz (yolo11s on every frame of
the 8 fps grid at native size, made by work/cache_dets.py), so no detector runs here; the plate models
run on the CPU. A fake clock runs the clips as fast as the CPU allows.
Output: .cache/tests/plates_best/runs/<run name>/ (events.json, the event pictures and clips).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / ".cache" / "tests" / "plates_best"
os.environ["YARDGUARD_CONFIG"] = str(BASE / "config.json")
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"  # the app may hold the GPU: measure on the CPU
BEFORE = "--before" in sys.argv
sys.path.insert(0, str(BASE / "before" if BEFORE else ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yardguard import areas as A  # noqa: E402
from yardguard.config import Camera, Config, Settings, Site, new_area, normalize_area  # noqa: E402
from yardguard.engine import AlertStore, Monitor  # noqa: E402

CLIPS = ROOT / ".cache" / "tests" / "plates"
GRID_FPS = 8.0
T0 = datetime(2026, 10, 23, 12, 9, 24).timestamp()   # the clips' first frame (camera clock)
NATIVE = 1920

# Drawn by eye on the 1920x1080 frames: the user's barrier line on ch5 (as in config.json), a line across
# the ch5 driveway where cars pass close to the camera, and a line across the ch1 yard exit.
LINES = {
    5: [("Barrier", [[0.2208, 0.411], [0.5781, 0.569]]), ("Driveway", [[0.0, 0.80], [0.52, 0.80]])],
    1: [("Yard exit", [[0.30, 0.78], [0.62, 0.78]])],
}
FRAME_IDX: dict[int, int] = {}   # id(frame) -> grid index, for the fake detector
DUMP: dict = {}


def arg(name: str, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


class ClipSource:
    """FrameSource stand-in: the recording on the 8 fps grid, scaled to long_side like ffmpeg would."""
    instances: dict[int, "ClipSource"] = {}

    def __init__(self, url: str, fps: float, long_side: int = 1280, gpu_decode: bool = True) -> None:
        self.channel = int(url.split("channel=")[1].split("&")[0])
        self.long_side = long_side
        self.backend, self.error, self.alive, self.size = "replay", None, True, None
        self.cap = cv2.VideoCapture(str(CLIPS / f"ch{self.channel}.mp4"))
        self.src_fps = self.cap.get(cv2.CAP_PROP_FPS) or 15.0
        self.pos, self.k, self.item = -1, -1, None
        self.done = False
        ClipSource.instances[self.channel] = self

    def start(self):
        pass

    def stop(self):
        self.cap.release()

    def latest(self):
        return self.item

    def advance(self, now: float) -> None:
        """The newest grid frame at the clock time now (frames skipped on the way are not decoded)."""
        k = int((now - T0) * GRID_FPS + 1e-6)
        if self.done or k <= self.k:
            return
        self.k = k
        want = round(k * self.src_fps / GRID_FPS)
        while self.pos < want:
            if not self.cap.grab():
                self.done = True
                return
            self.pos += 1
        ok, frame = self.cap.retrieve()
        if not ok:
            self.done = True
            return
        h, w = frame.shape[:2]
        scale = self.long_side / max(h, w)
        if scale < 1:
            frame = cv2.resize(frame, (round(w * scale / 2) * 2, round(h * scale / 2) * 2),
                               interpolation=cv2.INTER_AREA)
        if self.item is not None:
            FRAME_IDX.pop(id(self.item[0]), None)
        FRAME_IDX[id(frame)] = self.channel * 10_000_000 + k
        self.size = (frame.shape[1], frame.shape[0])
        self.item = (frame, T0 + k / GRID_FPS)


class CachedDetector:
    backend = "replay"

    def __init__(self, dets: dict[int, dict]) -> None:
        self.dets = dets
        self.calls = 0

    def predict(self, frames, conf):
        out = []
        for f in frames:
            key = FRAME_IDX.get(id(f), -1)
            d = self.dets.get(key // 10_000_000, {}).get(str(key % 10_000_000))
            d = np.zeros((0, 6), np.float32) if d is None else d.copy()
            d[:, :4] *= f.shape[1] / NATIVE
            out.append(d[d[:, 4] >= conf])
            self.calls += 1
        return out


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def main() -> None:
    run = sys.argv[1]
    out = BASE / "runs" / run
    shutil.rmtree(out, ignore_errors=True)
    (out / "events").mkdir(parents=True)
    fps = float(arg("--fps", 6))
    minutes = float(arg("--minutes", 26))
    clips = [int(c.strip().removeprefix("ch")) for c in arg("--clips", "ch5,ch1").split(",")]
    dets = {}
    for ch in clips:
        data = np.load(BASE / "work" / f"ch{ch}_dets8.npz")
        dets[ch] = {k: data[k] for k in data.files}
    cams = []
    for ch in clips:
        cam = Camera(ch, f"CAM {ch}", "Parking")
        cam.areas = [normalize_area(new_area("log_plates", "line", pts, name, objects=["car", "truck"],
                                             params={"min_plate_px": 80, "known": []})) for name, pts in LINES[ch]]
        cams.append(cam)
    site = Site(name="Replay", host="127.0.0.1", cameras=cams)
    cfg = Config(sites=[site], active_site_id=site.id)
    cfg.settings = Settings(events_dir=str(out / "events"), detect_fps=fps, clip_fps=GRID_FPS, clips_enabled=True,
                            confidence=0.55, motion_gating=True, brain_mode="off", rule_night_presence=False,
                            rule_loitering=False, rule_prowling=False, rule_car_moved=False)
    store = AlertStore(str(out / "events"))
    clock = Clock(T0)
    det = CachedDetector(dets)
    got = []
    mon = Monitor(cfg, store, None, detector=det, source_factory=ClipSource, clock=clock, on_area_event=got.append)
    mon._setup()
    mon._running = True
    reader = mon._plates()
    if "--no-fuse" in sys.argv:
        reader.fuse = False
    if "--cands" in sys.argv:
        A.PLATE_CANDIDATES = reader.max_crops = int(arg("--cands"))
    if "--read-top" in sys.argv:
        import yardguard.plates as P
        P.READ_TOP = int(arg("--read-top"))
    if "--no-between" in sys.argv:
        mon._areas_between = lambda *a, **k: None
    if "--between" in sys.argv:  # frames between detections too (areas.PLATE_BETWEEN, off by default)
        A.PLATE_BETWEEN = True
    widths: dict[str, list] = {"last": []}
    find = reader._find_plates

    def find_plates(images):  # plate widths found (box px; the old code returns cuts with a 12 % margin)
        found = find(images)
        widths["last"] = [f[3][0] if len(f) == 4 else f[0].shape[1] / 1.12 for f in found]
        return found

    reader._find_plates = find_plates
    if "--dump" in sys.argv:  # every vehicle picture searched, and what was read: out/dump/<time>/
        read = reader.read

        def dump_read(crops):
            result = read(crops)
            hit = DUMP["hit"]
            folder = out / "dump" / f"{hit.event.ts - T0:07.1f}_{hit.event.area_name}"
            folder.mkdir(parents=True, exist_ok=True)
            for i, c in enumerate(hit.plate_cands or []):
                cv2.imwrite(str(folder / f"c{i:02d}_{c.ts - T0:.2f}_{c.score:.3f}{'' if c.detected else '_b'}.jpg"),
                            c.crop)
            info = {k: v for k, v in (result or {}).items() if k != "plate_crop"}
            (folder / "result.json").write_text(json.dumps(info, indent=1, default=str), encoding="utf-8")
            return result

        reader.read = dump_read
        read_plate = mon._read_plate

        def dump_hit(hit):
            DUMP["hit"] = hit
            return read_plate(hit)

        mon._read_plate = dump_hit
    srcs = [ClipSource.instances[ch] for ch in clips]
    print(f"{run}: {'BEFORE' if BEFORE else 'AFTER'} code, frames {srcs[0].long_side} px, {fps:g} detections/s, "
          f"clips {clips}", flush=True)
    tick = 0.5 / fps
    t_wall, read_s, reads = time.time(), 0.0, 0
    start = float(arg("--start", 0))
    now = T0 + start
    max_w: dict[str, float] = {}
    while now < T0 + start + minutes * 60 and not all(s.done for s in srcs):
        now += tick
        clock.t = now
        for s in srcs:
            s.advance(now)
        mon.step(now)
        mon.capture(now)
        t = time.time()
        n = len(got)
        widths["last"] = []
        while mon.area_step():
            pass
        if len(got) > n:
            read_s += time.time() - t
            reads += len(got) - n
            for e in got[n:]:
                max_w[e.id] = round(max(widths["last"], default=0))
                print(f"  {e.ts - T0:7.1f} s ch{e.channel} {e.area_name:9s} {e.extra.get('direction', ''):3s} "
                      f"{e.value or '-':11s} {e.confidence:.2f} plate {e.extra.get('plate_px', 0):3d} px "
                      f"(max {max_w[e.id]:3d}) best {e.extra.get('best_ts', e.ts) - e.ts:+5.1f} s", flush=True)
        if int(now - T0) % 120 == 0 and int(now - T0 - tick) % 120 != 0:
            print(f"  [{(now - T0) / 60:.0f} min, {time.time() - t_wall:.0f} s]", flush=True)
    mon._running = False
    if mon._clips is not None:
        mon._clips.stop()
    rows = []
    for e in got:
        fresh = store.get_area_event(e.id) or e
        rows.append({"id": e.id, "channel": e.channel, "area": e.area_name, "t": round(e.ts - T0, 2),
                     "direction": e.extra.get("direction"), "value": e.value, "confidence": e.confidence,
                     "plate_px": e.extra.get("plate_px", 0), "max_plate_px": max_w.get(e.id, 0),
                     "note": e.extra.get("note", ""),
                     "best_t": round(e.extra["best_ts"] - T0, 2) if "best_ts" in e.extra else None,
                     "best_offset_s": e.extra.get("best_offset_s"), "clip_offset_s": fresh.clip_offset_s,
                     "clip": fresh.clip_path, "crop": e.crop_path, "snapshot": e.snapshot_path})
    (out / "events.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"{len(rows)} plate events, {reads} read in {read_s:.0f} s ({read_s / max(reads, 1):.2f} s each), "
          f"{det.calls} detections, {time.time() - t_wall:.0f} s wall")


if __name__ == "__main__":
    main()
