"""Incident clips (yardguard/clips.py and the Monitor / AlertStore parts): pre-event buffer,
extension and cap, shared clips, the real ffmpeg output, clean stop, store migration, storage
cleanup and deletion. Deterministic: a fake clock and a fake camera that plays a real night
recording (with the frame number drawn in as a block code, so the clip's content can be checked).

    run.cmd tests\\test_clips.py
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "clips"
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")  # never touch the user's config
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yardguard import clips  # noqa: E402
from yardguard.config import Camera, Config, Settings, Site  # noqa: E402
from yardguard.engine import Alert, AlertStore, Monitor  # noqa: E402

VIDEOS = [ROOT / ".cache" / "tests" / "night" / "long" / "ch1.mp4",
          ROOT / ".cache" / "tests" / "night" / "ch1_old_vs_new.mp4"]
VIDEO = next((v for v in VIDEOS if v.exists()), None)
NIGHT = datetime(2026, 10, 23, 1, 0, 0).timestamp()
W, H = 1280, 720
BITS, BLOCK = 12, 40
PERSON = 0
REPORT: dict[str, str] = {}


# --- fakes -----------------------------------------------------------------------------

def draw_code(frame: np.ndarray, n: int) -> None:
    """Frame number as black / white blocks along the bottom edge (survives JPEG + H.264)."""
    for i in range(BITS):
        x = 10 + i * BLOCK
        frame[H - 50:H - 10, x:x + BLOCK] = 255 if (n >> i) & 1 else 0


def read_code(frame: np.ndarray) -> int:
    k = frame.shape[1] / W
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    n = 0
    for i in range(BITS):
        x, y = int((10 + i * BLOCK + BLOCK / 2) * k), int((H - 30) * k)
        if grey[y - 3:y + 4, x - 3:x + 4].mean() > 128:
            n |= 1 << i
    return n


class VideoSource:
    """FrameSource stand-in: the real recording, scaled like FrameSource, at the fake clock's pace."""

    instances: dict[int, "VideoSource"] = {}
    T0 = NIGHT    # clock time of the recording's first frame

    def __init__(self, url: str, fps: float, long_side: int = 1280, gpu_decode: bool = True) -> None:
        m = re.search(r"channel=(\d+)", url)
        self.channel = int(m.group(1)) if m else 1
        self.fps = fps
        self.backend, self.error, self.size = "fake", None, (W, H)
        self.item = None
        self.frames = 0               # frames pushed (numbered from 1)
        self._cap = cv2.VideoCapture(str(VIDEO))
        self._vfps = self._cap.get(cv2.CAP_PROP_FPS) or 15.0
        self._pos, self._last = -1, None
        VideoSource.instances[self.channel] = self

    def start(self):
        pass

    def stop(self):
        self._cap.release()

    def latest(self):
        return self.item

    def push(self, ts: float) -> None:
        want = int(round((ts - self.T0) * self._vfps))
        while self._pos < want:
            ok, frame = self._cap.read()
            if not ok:  # loop the recording
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
            self._pos += 1
            self._last = frame
        frame = cv2.resize(self._last, (W, H), interpolation=cv2.INTER_AREA)
        self.frames += 1
        draw_code(frame, self.frames)
        frame[0, 0, 0] = self.channel   # the fake detector knows the camera
        self.item = (frame, ts)


class FakeDetector:
    backend = "fake"

    def __init__(self):
        self.dets: dict[int, np.ndarray] = {}
        self.calls = 0

    def predict(self, frames, conf):
        self.calls += 1
        return [self.dets.get(int(f[0, 0, 0]), np.zeros((0, 6), np.float32)) for f in frames]


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_monitor(name: str, **kw):
    events = OUT / name
    shutil.rmtree(events, ignore_errors=True)
    site = Site(name="Test", host="127.0.0.1", cameras=[Camera(1, "Yard parking", "Parking")])
    cfg = Config(sites=[site], active_site_id=site.id)
    cfg.settings = Settings(events_dir=str(events), brain_mode="off", night_start="00:00", night_end="06:00",
                            rule_loitering=False, rule_prowling=False, rule_car_moved=False)
    for k, v in kw.items():
        setattr(cfg.settings, k, v)
    store = AlertStore(str(events))
    clock = Clock(NIGHT)
    got = {"added": [], "updated": []}
    det = FakeDetector()
    VideoSource.instances.clear()
    mon = Monitor(cfg, store, None, on_alert=got["added"].append, on_alert_update=got["updated"].append,
                  detector=det, source_factory=VideoSource, clock=clock)
    mon._setup()
    return mon, store, det, clock, got


def run_for(mon, clock, seconds: float, person_from: float | None = None, det=None, tick: float = 0.0625,
            step_every: float | None = None) -> None:
    """Advance the fake clock: frames at the source rate, capture every tick, detection at the
    inference loop's tick (half the detection interval)."""
    src = VideoSource.instances[1]
    frame_dt = 1.0 / mon.source_fps()
    step_dt = step_every or 0.5 / mon.cfg.settings.detect_fps
    end = clock.t + seconds
    next_frame = getattr(src, "_next", clock.t)
    next_step = getattr(mon, "_test_next_step", clock.t)
    while clock.t < end - 1e-9:
        clock.t = round(clock.t + tick, 6)
        if det is not None and person_from is not None and clock.t >= person_from:
            det.dets[1] = np.array([[200, 300, 260, 460, 0.9, PERSON]], np.float32)
        while next_frame <= clock.t + 1e-9:
            src.push(next_frame)
            next_frame = round(next_frame + frame_dt, 6)
        mon.capture()
        pace(mon._clips)
        if clock.t >= next_step - 1e-9:
            mon.step()
            next_step = round(next_step + step_dt, 6)
    src._next, mon._test_next_step = next_frame, next_step


def pace(rec) -> None:
    """The fake clock runs much faster than real time; a real camera gives the writer 1/8 s per
    frame. Wait while a writer's queue is half full, so no frame is dropped."""
    if rec is None:
        return
    end = time.time() + 10
    while any(w._queue.qsize() > w._queue.maxsize // 2 for w in list(rec._writers)) and time.time() < end:
        time.sleep(0.005)


def wait_writers(rec, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while rec.recording() and time.time() < end:
        time.sleep(0.05)
    assert not rec.recording(), "clip writers did not finish"


def probe(path: Path) -> dict:
    cap = cv2.VideoCapture(str(path))
    assert cap.isOpened(), f"cv2 cannot open {path}"
    fps = cap.get(cv2.CAP_PROP_FPS)
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    return {"fps": fps, "count": count, "decoded": len(frames), "frames": frames,
            "size": (frames[0].shape[1], frames[0].shape[0]) if frames else None, "bytes": path.stat().st_size}


# --- tests -------------------------------------------------------------------------------

def test_store_migration_and_update_keeps_clip():
    folder = OUT / "migrate"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    old_cols = ["id", "site_id", "channel", "camera", "ts", "rule", "title", "detail", "severity", "snapshot_path",
                "crop_path", "boxes", "verdict", "brain_findings", "status", "incident_id"]
    db = sqlite3.connect(str(folder / "alerts.db"))
    db.execute(f"CREATE TABLE alerts ({', '.join(c + (' PRIMARY KEY' if c == 'id' else '') for c in old_cols)})")
    db.execute(f"INSERT INTO alerts VALUES ({', '.join('?' * len(old_cols))})",
               ["old1", "s1", 1, "Yard", NIGHT, "night_presence", "t", "d", "warning", "", "", "[]", "unverified",
                "[]", "new", ""])
    db.commit()
    db.close()
    store = AlertStore(str(folder))
    cols = {r[1]: r[2] for r in store._db.execute("PRAGMA table_info(alerts)")}
    assert cols.get("clip_path") == "TEXT" and cols.get("clip_offset_s") == "REAL", cols
    a = store.get("old1")
    assert a.clip_path == "" and a.clip_offset_s == -1.0 and a.incident_id, a
    a.clip_path, a.clip_offset_s = r"X:\clips\old1.mp4", 8.0
    store.update(a)
    stale = store.get("old1")
    stale.clip_path, stale.verdict = "", "confirmed"   # a copy saved before the clip was done
    store.update(stale)
    b = store.get("old1")
    assert b.clip_path == r"X:\clips\old1.mp4" and b.verdict == "confirmed", b
    assert store.clear_clip(r"X:\clips\old1.mp4") == ["old1"]
    assert store.get("old1").clip_path == "" and store.get("old1").clip_offset_s == -1.0
    store.close()


def test_detection_rate_unchanged_by_source_fps():
    rates = {}
    for detect_fps, clip_fps, clips_on in ((5, 8, True), (5, 8, False), (2, 8, True), (10, 8, True)):
        mon, store, det, clock, _ = make_monitor(f"rate_{detect_fps}_{clips_on}", detect_fps=detect_fps,
                                                 clip_fps=clip_fps, clips_enabled=clips_on, motion_gating=False)
        run_for(mon, clock, 20.0)
        rates[(detect_fps, clips_on)] = det.calls / 20.0
        if clips_on:
            assert mon.source_fps() == max(detect_fps, clip_fps)
            # every frame is buffered, also the ones detection skipped
            assert mon._clips.frames == VideoSource.instances[1].frames > 20 * 7, mon._clips.frames
        mon._clips and mon._clips.stop()
        store.close()
    REPORT["detection rate (checks/s)"] = ", ".join(f"detect {k[0]} clips {'on' if k[1] else 'off'}: {v:.2f}"
                                                    for k, v in rates.items())
    assert abs(rates[(5, True)] - 5) <= 0.15 and abs(rates[(5, False)] - 5) <= 0.15, rates
    assert abs(rates[(2, True)] - 2) <= 0.1 and abs(rates[(10, True)] - 10) <= 0.3, rates


def test_buffering_while_gated_idle():
    mon, store, det, clock, _ = make_monitor("gated", motion_gating=True)
    src = VideoSource.instances[1]
    still = None
    for i in range(80):  # 10 s of an unchanging picture: detection goes idle, buffering does not
        clock.t += 0.125
        if still is None:
            src.push(clock.t)
            still = src.item[0]
        src.item = (still, clock.t)
        mon.capture()
        mon.step()
    assert mon.camera_status()[1]["state"] == "idle"
    assert det.calls <= 5, det.calls
    buf = mon._clips.buffered(1)
    assert len(buf) == 65 and buf[-1][0] == clock.t and buf[-1][0] - buf[0][0] == clips.PRE_S, (len(buf),)
    kb = sum(len(j) for _, j in buf) / len(buf) / 1024
    REPORT["pre-buffer"] = f"{len(buf)} frames, {kb:.0f} KB per frame, {kb * len(buf) / 1024:.1f} MB per camera"
    mon._clips.stop()
    store.close()


def test_alert_clip_prebuffer_shared_and_playable():
    mon, store, det, clock, got = make_monitor("alert_clip", rule_loitering=True, loiter_seconds=5)
    t0 = clock.t
    run_for(mon, clock, 40.0, person_from=t0 + 12.0, det=det)
    rec = mon._clips
    wait_writers(rec)
    alerts = [store.get(a.id) for a in got["added"]]
    assert [a.rule for a in alerts] == ["night_presence", "loitering"], [a.rule for a in alerts]
    first, second = alerts
    assert first.clip_path and first.clip_path == second.clip_path, (first.clip_path, second.clip_path)
    assert Path(first.clip_path).name == f"{first.id}.mp4" and "clips" in Path(first.clip_path).parts
    assert abs(first.clip_offset_s - clips.PRE_S) < 0.2, first.clip_offset_s
    assert abs(second.clip_offset_s - (first.clip_offset_s + second.ts - first.ts)) < 0.01
    ups = [u for u in got["updated"] if u.clip_path]
    assert sorted(u.id for u in ups) == sorted(a.id for a in alerts), "alert_updated for both alerts"
    info = probe(Path(first.clip_path))
    expect_s = clips.PRE_S + (second.ts - first.ts) + clips.POST_S   # extended by the second alert
    n = info["decoded"]
    assert abs(n - expect_s * 8) <= 2 and abs(info["count"] - n) <= 1, (n, info["count"], expect_s)
    assert abs(info["fps"] - 8) < 0.01 and info["size"] == (960, 540), info
    # content: the first clip frame is the frame from 8 s before the alert, the cadence is even
    codes = [read_code(f) for f in info["frames"]]
    src_frame = round((first.ts - NIGHT) * 8) + 1  # source frame number at the alert (8 fps, numbered from 1)
    assert abs(codes[0] - (src_frame - 64)) <= 1, (codes[0], src_frame)
    assert abs(codes[round(first.clip_offset_s * 8)] - src_frame) <= 1
    assert all(b - a == 1 for a, b in zip(codes, codes[1:])), "every frame once, in order"
    assert not list(Path(first.clip_path).parent.glob("*.tmp"))
    REPORT["shared clip"] = (f"{n / 8:.1f} s, {info['bytes'] / 1024:.0f} KB, {info['size'][0]}x{info['size'][1]} "
                             f"@ {info['fps']:g} fps, offsets {first.clip_offset_s:.2f} s / {second.clip_offset_s:.2f} s")
    rec.stop()
    store.close()


def _recorder(name: str, done: list):
    folder = OUT / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    rec = clips.ClipRecorder(folder, 8, on_done=done.append)
    VideoSource.instances.clear()
    src = VideoSource("rtsp://x/?channel=1", 8)
    return folder, rec, src


def _alert(n: int, ts: float, channel: int = 1) -> Alert:
    return Alert(f"a{n:03d}", "s1", channel, "Yard", ts, "night_presence", "t", "d", "warning")


def test_size_cpu_and_20s_clip():
    done: list = []
    folder, rec, src = _recorder("size20", done)
    t = NIGHT
    alert = _alert(1, NIGHT + 10.0)
    while t < NIGHT + 30:
        t = round(t + 0.125, 6)
        src.push(t)
        rec.add_frame(1, src.item[0], t)
        pace(rec)
        if abs(t - alert.ts) < 1e-6:
            rec.on_alert(alert)
    wait_writers(rec)
    w = done[0]
    info = probe(w.path)
    assert w.ok and abs(info["decoded"] - 160) <= 1 and w.dropped == 0, (info["decoded"], w.dropped)
    REPORT["20 s clip"] = (f"{info['bytes'] / 1024:.0f} KB ({info['bytes'] * 8 / 20 / 1000:.0f} kbit/s), "
                           f"encode CPU {w.cpu_s:.2f} s" if w.cpu_s is not None else "encode CPU n/a")
    REPORT["encoder"] = "libx264" if clips.has_x264() else "mpeg4"


def test_extension_cap_and_next_clip():
    done: list = []
    folder, rec, src = _recorder("cap", done)
    alerts = {NIGHT + 10.0 * k: _alert(k, NIGHT + 10.0 * k) for k in range(1, 8)}   # every 10 s, 10..70
    t = NIGHT
    while t < NIGHT + 95:
        t = round(t + 0.125, 6)
        src.push(t)
        rec.add_frame(1, src.item[0], t)
        pace(rec)
        a = alerts.get(t)
        if a is not None:
            rec.on_alert(a)
    wait_writers(rec)
    assert len(done) == 2 and all(w.ok and not w.dropped for w in done), [(w.ok, w.error, w.dropped) for w in done]
    first, second = sorted(done, key=lambda w: w.start_ts)
    assert [a.id for a in first.alerts] == [f"a{k:03d}" for k in range(1, 7)], [a.id for a in first.alerts]
    assert abs(first.start_ts - (NIGHT + 2.0)) < 0.2 and abs(first.end_ts - first.start_ts - clips.MAX_CLIP_S) < 1e-6
    info = probe(first.path)
    assert abs(info["decoded"] - clips.MAX_CLIP_S * 8) <= 1, info["decoded"]
    assert [round(a.clip_offset_s) for a in first.alerts] == [8, 18, 28, 38, 48, 58]
    assert [a.id for a in second.alerts] == ["a007"] and abs(second.alerts[0].clip_offset_s - 8) < 0.2
    assert first.path.name == "a001.mp4" and second.path.name == "a007.mp4"
    REPORT["60 s capped clip"] = f"{info['bytes'] / 1024:.0f} KB, CPU {first.cpu_s:.2f} s" if first.cpu_s else ""


def test_stop_mid_clip_leaves_no_tmp_or_ffmpeg():
    writers = []
    real_init = clips.ClipWriter.__init__

    def spy(self, *a, **kw):
        real_init(self, *a, **kw)
        writers.append(self)

    clips.ClipWriter.__init__ = spy
    try:
        mon, store, det, clock, got = make_monitor("stop_mid")
        mon._clock = time.time                 # real threads now
        VideoSource.T0 = time.time()
        mon.start()
        src = VideoSource.instances[1]
        t_end = time.time() + 3.0
        raised = False
        while time.time() < t_end:
            src.push(time.time())
            if not raised and time.time() > t_end - 2.0:   # an alert one second into the run
                a = _alert(900, time.time())
                a.site_id, a.id = mon._site_id, f"stop-{int(time.time())}"
                mon._start_clip(a)
                store.add(a)
                raised = True
            time.sleep(0.04)
        t0 = time.time()
        mon.stop()
        stop_s = time.time() - t0
        assert writers and all(w._proc is not None and w._proc.poll() is not None for w in writers), "ffmpeg left"
        tmp = list((OUT / "stop_mid").rglob("*.tmp"))
        assert not tmp, tmp
        saved = store.get(a.id)
        assert saved.clip_path and Path(saved.clip_path).exists(), saved   # finished, not thrown away
        n = probe(Path(saved.clip_path))["decoded"]
        assert 8 <= n <= 8 * 12, n
        REPORT["stop mid-clip"] = f"stop took {stop_s:.2f} s, partial clip {n / 8:.1f} s kept, no .tmp, ffmpeg exited"
        store.close()
    finally:
        clips.ClipWriter.__init__ = real_init
        VideoSource.T0 = NIGHT


def _clip_file(folder: Path, name: str, ts: float, size: int = 1024) -> str:
    day = folder / "clips" / f"{datetime.fromtimestamp(ts):%Y-%m-%d}"
    day.mkdir(parents=True, exist_ok=True)
    p = day / f"{name}.mp4"
    p.write_bytes(b"\0" * size)
    return str(p)


def _cleanup_store(name: str):
    folder = OUT / name
    shutil.rmtree(folder, ignore_errors=True)
    return folder, AlertStore(str(folder), incident_gap_s=60)


def _add(store, n, ts, clip, verdict="unverified", site="s1"):
    a = Alert(f"c{n:03d}", site, 1, "Yard", ts, "night_presence", "t", "d", "warning", verdict=verdict)
    a.clip_path, a.clip_offset_s = clip, 8.0
    store.add(a, gap_s=60)
    return a


def test_cleanup_age_rules():
    folder, store = _cleanup_store("cleanup_age")
    now = NIGHT + 40 * 86400
    day = 86400
    fa = _add(store, 1, now - 2 * day, _clip_file(folder, "fa_old", now - 2 * day))           # false alarm, 2 days
    store.mark_incident(fa.incident_id, "false_alarm")
    fa_new = _add(store, 2, now - 3600, _clip_file(folder, "fa_new", now - 3600))           # false alarm, 1 h
    store.mark_incident(fa_new.incident_id, "false_alarm")
    old = _add(store, 3, now - 31 * day, _clip_file(folder, "old", now - 31 * day))          # 31 days
    keep = _add(store, 4, now - 10 * day, _clip_file(folder, "keep", now - 10 * day))        # 10 days
    missing = _add(store, 5, now - 5 * day, str(folder / "clips" / "gone.mp4"))              # file already gone
    orphan = Path(_clip_file(folder, "orphan", now - 5 * day))
    os.utime(orphan, (time.time() - 7200, time.time() - 7200))
    young_tmp = orphan.parent / "recording.mp4.tmp"
    young_tmp.write_bytes(b"x")
    s = Settings(clip_max_gb=5.0, clip_max_days=30, false_alarm_clip_days=1)
    res = clips.cleanup(store, s, now=now)
    left = {Path(p).stem for p in (folder / "clips").rglob("*.mp4")}
    assert left == {"fa_new", "keep"}, left
    assert young_tmp.exists(), "a clip being recorded is never touched"
    assert res.deleted == 3 and res.count == 2 and res.total == 2048, res
    assert store.get(fa.id).clip_path == "" and store.get(old.id).clip_path == "" and store.get(missing.id).clip_path == ""
    assert store.get(keep.id).clip_path and store.get(fa_new.id).clip_path
    assert store.get(fa.id).snapshot_path == fa.snapshot_path and store.get(fa.id) is not None, "records stay"
    store.close()


def test_cleanup_size_cap_order_and_locked_file():
    folder, store = _cleanup_store("cleanup_size")
    now = NIGHT + 86400
    hour = 3600
    kb = 1024
    # oldest first: handled (h1), confirmed (c1, handled but AI-confirmed), open (o1), handled (h2), handled (h3)
    h1 = _add(store, 1, now - 10 * hour, _clip_file(folder, "h1", now - 10 * hour, 4 * kb))
    store.mark_incident(h1.incident_id, "handled")
    c1 = _add(store, 2, now - 9 * hour, _clip_file(folder, "c1", now - 9 * hour, 4 * kb), verdict="confirmed")
    store.mark_incident(c1.incident_id, "handled")
    h2 = _add(store, 4, now - 7 * hour, _clip_file(folder, "h2", now - 7 * hour, 4 * kb))
    store.mark_incident(h2.incident_id, "handled")
    h3 = _add(store, 5, now - 6 * hour, _clip_file(folder, "h3", now - 6 * hour, 4 * kb))
    store.mark_incident(h3.incident_id, "handled")
    o1 = _add(store, 3, now - 1 * hour, _clip_file(folder, "o1", now - 1 * hour, 4 * kb))   # still open
    s = Settings(clip_max_gb=9 * kb / 1024 ** 3, clip_max_days=30, false_alarm_clip_days=1)  # room for two
    locked = open(h2.clip_path, "rb")   # e.g. the player has it open: Windows refuses to delete it
    try:
        res = clips.cleanup(store, s, now=now)
    finally:
        locked.close()
    left = {Path(p).stem for p in (folder / "clips").rglob("*.mp4")}
    # h1, h3 go (h2 is locked and skipped); still over the cap -> the protected c1 next; o1 is the newest protected
    assert left == {"h2", "o1"}, left
    assert res.locked == 1 and res.deleted == 3, res
    assert store.get(h2.id).clip_path, "a locked clip keeps its reference"
    res = clips.cleanup(store, s, now=now)   # retried next time: now it goes... only if still over the cap
    assert res.locked == 0 and {Path(p).stem for p in (folder / "clips").rglob("*.mp4")} == {"h2", "o1"}
    s.clip_max_gb = 5 * kb / 1024 ** 3
    res = clips.cleanup(store, s, now=now)
    assert {Path(p).stem for p in (folder / "clips").rglob("*.mp4")} == {"o1"} and res.deleted == 1
    used, count = clips.usage(folder)
    assert (used, count) == (4 * kb, 1)
    store.close()


def test_delete_incident_and_alert_delete_unused_clips():
    folder, store = _cleanup_store("delete")
    now = NIGHT
    shared = _clip_file(folder, "shared", now)
    own = _clip_file(folder, "own", now)
    a = _add(store, 1, now, shared)
    c = _add(store, 3, now + 5, own)
    store.mark_incident(a.incident_id, "handled")     # the next alert starts a new incident
    b = _add(store, 2, now + 10, shared)              # same clip, other incident
    assert b.incident_id != a.incident_id and c.incident_id == a.incident_id
    left = store.delete_incident(a.incident_id)
    assert not left and Path(shared).exists() and not Path(own).exists(), "only the unreferenced clip goes"
    store.delete(b.id)
    assert not Path(shared).exists(), "last reference gone: clip deleted"
    store.close()


def main() -> int:
    import faulthandler
    faulthandler.dump_traceback_later(300, exit=True)  # a hung writer fails the run instead of blocking it
    if VIDEO is None:
        print("No test recording found:", VIDEOS)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    t0 = time.perf_counter()
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {time.perf_counter() - t0:.1f} s")
    for k, v in REPORT.items():
        print(f"  {k}: {v}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
