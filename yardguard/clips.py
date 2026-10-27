"""Short preview clips of incidents: a rolling pre-event buffer per camera, one ffmpeg writer per
clip, and storage cleanup.

The monitor hands every new camera frame to ClipRecorder.add_frame (its capture thread). The last
PRE_S seconds are kept in memory as small JPEGs with the time stamped on them. An alert starts a
clip (that buffer plus POST_S seconds after the alert) or extends the camera's running clip, so
alerts close together share one file. Each clip is encoded by its own ffmpeg (H.264 MP4) fed on
its own thread through a bounded queue: encoding never blocks detection.

cleanup() keeps clips within the user's limits. It deletes clip files only, never pictures or
alert records, and clears the clip reference of the alerts that pointed at a deleted file.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .stream import FFMPEG_MISSING, NO_WINDOW, _assign_to_job, ffmpeg_path

log = logging.getLogger("yardguard")

PRE_S = 8.0             # seconds kept before an alert
POST_S = 12.0           # seconds recorded after the (last) alert of a clip
MAX_CLIP_S = 60.0       # a clip never gets longer than this, however many alerts extend it
LONG_SIDE = 960         # clip frame size (long side, px)
JPEG_QUALITY = 80       # pre-buffer frames: ~40 KB each at 960 px
END_GRACE_S = 5.0       # no frame this long after a clip's end (camera gone): finish with what is there
ORPHAN_AGE_S = 3600.0   # unreferenced clip files and crash leftovers (.tmp) older than this are removed
CLEANUP_EVERY_S = 3600.0
FINISH_TIMEOUT_S = 20.0
CLIPS_DIR = "clips"
NO_CLIP = -1.0          # Alert.clip_offset_s when the alert has no clip (or it was deleted)
_BENCH_RE = re.compile(r"utime=([\d.]+)s\s+stime=([\d.]+)s")


def clips_dir(store_dir) -> Path:
    return Path(store_dir) / CLIPS_DIR


def clip_path_for(store_dir, alert_id: str, ts: float) -> Path:
    return clips_dir(store_dir) / f"{datetime.fromtimestamp(ts):%Y-%m-%d}" / f"{alert_id}.mp4"


# --- Frames --------------------------------------------------------------------------

def encode_frame(frame: np.ndarray, ts: float) -> bytes | None:
    """Clip-sized JPEG of a camera frame with the capture time stamped in the corner."""
    h, w = frame.shape[:2]
    k = LONG_SIDE / max(h, w)
    if k < 1:
        size = (max(2, round(w * k) // 2 * 2), max(2, round(h * k) // 2 * 2))
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    else:
        small = frame.copy()  # the stamp draws in place; the source's frame must stay untouched
    _stamp(small, ts)
    ok, data = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return data.tobytes() if ok else None


def _stamp(img: np.ndarray, ts: float) -> None:
    """Bottom-right on a dark box: cameras put their own clock and name in the other corners."""
    text = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    h, w = img.shape[:2]
    scale = max(0.4, w / 1800)
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    pad, margin = max(3, w // 240), max(4, w // 160)
    x2, y2 = w - margin, h - margin
    x1, y1 = max(0, x2 - tw - 2 * pad), max(0, y2 - th - base - 2 * pad)
    roi = img[y1:y2, x1:x2]
    roi[:] = (roi * 0.4).astype(np.uint8)
    cv2.putText(img, text, (x1 + pad, y2 - pad - base), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1,
                cv2.LINE_AA)


# --- Encoder -------------------------------------------------------------------------

_x264: bool | None = None


def has_x264() -> bool:
    """libx264 is in most ffmpeg builds; a minimal build gets the mpeg4 fallback."""
    global _x264
    if _x264 is None:
        ffmpeg = ffmpeg_path()
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, timeout=15,
                                 creationflags=NO_WINDOW).stdout.decode("utf-8", "replace") if ffmpeg else ""
        except (OSError, subprocess.SubprocessError):
            out = ""
        _x264 = " libx264 " in out
        if ffmpeg and not _x264:
            log.warning("ffmpeg has no libx264: clips use MPEG-4 part 2")
    return _x264


def encoder_args(fps: float) -> list[str]:
    gop = str(max(1, round(fps * 2)))  # a key frame every 2 s keeps seeking in the player quick
    if has_x264():
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p", "-g", gop]
    return ["-c:v", "mpeg4", "-q:v", "7", "-pix_fmt", "yuv420p", "-g", gop]


class ClipWriter:
    """One clip being recorded: JPEG frames in, one ffmpeg out, on its own thread.

    Frames arrive whenever the camera delivers them; the writer puts them on an even
    1/fps grid (repeating or skipping frames), so the clip's time matches the real time
    and clip_offset_s points at the right moment."""

    def __init__(self, path: Path, fps: float, frames: list[tuple[float, bytes]], alert_ts: float,
                 on_done=None) -> None:
        self.path = Path(path)
        self.tmp = self.path.with_name(self.path.name + ".tmp")
        self.fps = float(fps)
        self.start_ts = frames[0][0] if frames else alert_ts
        self.end_ts = min(alert_ts + POST_S, self.start_ts + MAX_CLIP_S)
        self.last_ts = frames[-1][0] if frames else -1e18
        self.alerts: list = []
        self.ok: bool | None = None
        self.error = ""
        self.frames_written = 0
        self.dropped = 0
        self.cpu_s: float | None = None
        self.closing = False
        self._on_done = on_done
        self._queue: queue.Queue = queue.Queue(maxsize=len(frames) + int(self.fps * PRE_S) + 32)
        for f in frames:
            self._queue.put_nowait(f)
        self._finished = threading.Event()
        self._abort = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name=f"yg-clip {self.path.stem}", daemon=True)
        self._thread.start()

    @property
    def duration_s(self) -> float:
        return self.frames_written / self.fps if self.fps else 0.0

    def feed(self, ts: float, jpg: bytes) -> None:
        """Never blocks: when the encoder falls behind, frames are dropped (the grid repeats the last)."""
        if ts <= self.last_ts or self.closing:
            return
        self.last_ts = ts
        try:
            self._queue.put_nowait((ts, jpg))
        except queue.Full:
            self.dropped += 1

    def extend(self, alert_ts: float) -> None:
        self.end_ts = max(self.end_ts, min(alert_ts + POST_S, self.start_ts + MAX_CLIP_S))

    def finish(self) -> None:
        """No more frames: encode what is queued and close the file."""
        self.closing = True
        self._finished.set()

    def abort(self) -> None:
        self.closing = True
        self._abort.set()
        self._finished.set()

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # --- writer thread ---

    def _run(self) -> None:
        try:
            self.ok = self._encode()
        except Exception as exc:
            log.exception("Clip %s failed", self.path.name)
            self.ok, self.error = False, str(exc)
        finally:
            self._kill()
            if not self.ok:
                try:
                    self.tmp.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not delete %s", self.tmp)
        if self._on_done:
            try:
                self._on_done(self)
            except Exception:
                log.exception("Clip callback failed")

    def _command(self, ffmpeg: str) -> list[str]:
        fps = f"{self.fps:g}"
        return [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", "-benchmark",
                "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", fps, "-i", "-", "-an",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", *encoder_args(self.fps), "-r", fps,
                "-movflags", "+faststart", "-f", "mp4", "-y", str(self.tmp)]

    def _encode(self) -> bool:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            self.error = FFMPEG_MISSING
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._proc = proc = subprocess.Popen(self._command(ffmpeg), stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                             creationflags=NO_WINDOW)
        _assign_to_job(proc.pid)
        tail: deque[str] = deque(maxlen=60)
        drain = threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True)
        drain.start()
        try:
            self._pump(proc)
            proc.stdin.close()
        except OSError as exc:  # ffmpeg died: its log says why
            self.error = f"ffmpeg stopped ({exc})"
        if self._abort.is_set():
            return False
        try:
            rc = proc.wait(timeout=FINISH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.error = "ffmpeg did not finish in time"
            return False
        drain.join(timeout=2)
        log_text = "\n".join(tail)
        m = _BENCH_RE.search(log_text)
        if m:
            self.cpu_s = float(m.group(1)) + float(m.group(2))
        if rc != 0 or not self.frames_written or not self.tmp.exists():
            self.error = self.error or (tail[-1] if tail else f"ffmpeg exit code {rc}")
            return False
        os.replace(self.tmp, self.path)
        return True

    def _pump(self, proc: subprocess.Popen) -> None:
        step = 1.0 / self.fps
        half = step / 2
        max_slots = int(MAX_CLIP_S * self.fps) + 1
        last: tuple[float, bytes] | None = None
        while not self._abort.is_set():
            try:
                ts, jpg = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._finished.is_set():
                    break
                continue
            if ts > self.end_ts + half:
                continue
            while last is not None and self.start_ts + self.frames_written * step + half < ts \
                    and self.frames_written < max_slots:
                self._write(proc, last[1])
            last = (ts, jpg)
        if last is not None and not self._abort.is_set():  # the final frame fills its own slot
            while self.start_ts + self.frames_written * step <= last[0] + half and self.frames_written < max_slots:
                self._write(proc, last[1])

    def _write(self, proc: subprocess.Popen, jpg: bytes) -> None:
        proc.stdin.write(jpg)
        self.frames_written += 1

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("Clip ffmpeg %s did not exit", proc.pid)
        for pipe in (proc.stdin, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass


def _drain(pipe, tail: deque) -> None:
    try:
        for raw in iter(pipe.readline, b""):
            tail.append(raw.decode("utf-8", "replace").rstrip())
    except (OSError, ValueError):
        pass


# --- Recorder ------------------------------------------------------------------------

class ClipRecorder:
    """Pre-event buffers of all cameras and the clips being written. Thread-safe: frames come
    from the capture thread, alerts from the inference and brain threads."""

    def __init__(self, store_dir, fps: float, on_done=None) -> None:
        self.store_dir = Path(store_dir)
        self.fps = max(1.0, float(fps))
        self.on_done = on_done              # on_done(ClipWriter), on the writer's thread
        self._lock = threading.Lock()
        self._buffers: dict[int, deque] = {}
        self._last: dict[int, float] = {}
        self._active: dict[int, ClipWriter] = {}
        self._writers: list[ClipWriter] = []
        self.frames = 0                     # frames buffered so far

    def add_frame(self, channel: int, frame: np.ndarray, ts: float) -> bool:
        """Buffer a frame (only frames newer than the last one of this camera); feeds the
        camera's running clip and closes it once its end is reached."""
        with self._lock:
            if ts <= self._last.get(channel, -1e18):
                return False
            self._last[channel] = ts
        jpg = encode_frame(frame, ts)
        if jpg is None:
            return False
        with self._lock:
            self.frames += 1
            buf = self._buffers.setdefault(channel, deque())
            buf.append((ts, jpg))
            while buf and buf[0][0] < ts - PRE_S:
                buf.popleft()
            w = self._active.get(channel)
            if w is not None:
                if ts <= w.end_ts + 0.5 / self.fps:
                    w.feed(ts, jpg)
                if ts >= w.end_ts:
                    self._close_locked(channel)
        return True

    def on_alert(self, alert) -> ClipWriter | None:
        """Start or extend the alert's camera clip; sets alert.clip_offset_s (clip_path follows
        once the file is complete). None without ffmpeg: the alert keeps "no clip"."""
        if not ffmpeg_path():
            return None
        with self._lock:
            w = self._active.get(alert.channel)
            if w is not None and alert.ts <= w.end_ts:
                w.extend(alert.ts)
            else:
                if w is not None:
                    self._close_locked(alert.channel)
                frames = [f for f in self._buffers.get(alert.channel, ()) if f[0] >= alert.ts - PRE_S]
                w = ClipWriter(clip_path_for(self.store_dir, alert.id, alert.ts), self.fps, frames, alert.ts,
                               self._done)
                self._active[alert.channel] = w
                self._writers.append(w)
            w.alerts.append(alert)
            alert.clip_offset_s = round(max(0.0, alert.ts - w.start_ts), 3)
            return w

    def tick(self, now: float) -> None:
        """Finish clips whose camera stopped sending frames before their end."""
        with self._lock:
            for ch, w in list(self._active.items()):
                if now > w.end_ts + END_GRACE_S:
                    self._close_locked(ch)

    def buffered(self, channel: int) -> list[tuple[float, bytes]]:
        with self._lock:
            return list(self._buffers.get(channel, ()))

    def recording(self) -> int:
        with self._lock:
            return len(self._writers)

    def stop(self, timeout: float = FINISH_TIMEOUT_S) -> None:
        """Finish every running clip and wait for the writers; ffmpeg processes that do not
        end in time are killed and their partial files deleted."""
        with self._lock:
            for ch in list(self._active):
                self._close_locked(ch)
            writers = list(self._writers)
            self._buffers.clear()
        deadline = time.monotonic() + timeout
        for w in writers:
            if not w.join(max(0.1, deadline - time.monotonic())):
                log.warning("Clip %s did not finish in time: dropped", w.path.name)
                w.abort()
                w.join(5)

    def _close_locked(self, channel: int) -> None:
        w = self._active.pop(channel, None)
        if w is not None:
            w.finish()

    def _done(self, w: ClipWriter) -> None:
        with self._lock:
            if w in self._writers:
                self._writers.remove(w)
        if w.ok:
            log.info("Clip %s saved: %.1f s, %d KB, %s s CPU", w.path.name, w.duration_s,
                     w.path.stat().st_size // 1024, f"{w.cpu_s:.2f}" if w.cpu_s is not None else "?")
        else:
            log.warning("Clip %s not saved: %s", w.path.name, w.error)
        if self.on_done:
            self.on_done(w)


# --- Storage ---------------------------------------------------------------------------

@dataclass
class CleanupResult:
    deleted: int = 0      # clip files removed
    freed: int = 0        # bytes
    locked: int = 0       # files in use (e.g. open in the player): retried next time
    total: int = 0        # bytes of clips left
    count: int = 0        # clips left


def usage(store_dir) -> tuple[int, int]:
    """(bytes, number of clips) on disk."""
    total = count = 0
    folder = clips_dir(store_dir)
    if folder.exists():
        for p in folder.rglob("*.mp4"):
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def cleanup(store, s, now: float | None = None) -> CleanupResult:
    """Apply the clip limits, in this order:
    1. clips of incidents marked as false alarms, older than false_alarm_clip_days;
    2. clips older than clip_max_days;
    3. while the total is above clip_max_gb: oldest first, clips of open or AI-confirmed
       incidents last.
    Unreferenced files and crash leftovers older than an hour go first. Files in use are skipped."""
    now = time.time() if now is None else now
    res = CleanupResult()
    folder = clips_dir(store.dir)
    files: dict[str, Path] = {}
    if folder.exists():
        for p in folder.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".mp4", ".tmp"):
                files[_key(p)] = p
    sizes = {k: _size(p) for k, p in files.items()}
    refs = {_key(r["clip_path"]): r for r in store.clip_refs()}

    def drop(key: str, path) -> bool:
        p = Path(path)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.info("Clip %s is in use: kept for now", p.name)
            res.locked += 1
            return False
        if key in refs:
            store.clear_clip(refs[key]["clip_path"])
        if key in sizes:
            res.deleted += 1
            res.freed += sizes.pop(key)
        return True

    for key, r in list(refs.items()):  # the file is gone already: forget it
        if key not in files:
            store.clear_clip(r["clip_path"])
            refs.pop(key)
    for key, p in files.items():  # nobody points at it (or a crash left a .tmp)
        if key not in refs and time.time() - _mtime(p) > ORPHAN_AGE_S:  # file times are wall-clock
            drop(key, p)
    fa_age, max_age = float(s.false_alarm_clip_days) * 86400, float(s.clip_max_days) * 86400
    for key, r in sorted(refs.items(), key=lambda kv: kv[1]["ts"]):
        if key not in sizes:
            continue
        age = now - r["ts"]
        if (r["false_alarm"] and age > fa_age) or age > max_age:
            drop(key, r["clip_path"])
    cap = float(s.clip_max_gb) * 1024 ** 3
    if sum(sizes.values()) > cap:
        order = sorted((kv for kv in refs.items() if kv[0] in sizes), key=lambda kv: (kv[1]["keep"], kv[1]["ts"]))
        for key, r in order:
            if sum(sizes.values()) <= cap:
                break
            drop(key, r["clip_path"])
    res.total = sum(v for k, v in sizes.items() if k.endswith(".mp4"))
    res.count = sum(1 for k in sizes if k.endswith(".mp4"))
    for day in (sorted(folder.iterdir(), reverse=True) if folder.exists() else []):
        if day.is_dir():
            try:
                day.rmdir()  # only empty day folders
            except OSError:
                pass
    if res.deleted or res.locked:
        log.info("Clip cleanup: %d deleted (%d MB), %d in use", res.deleted, res.freed // 2 ** 20, res.locked)
    return res


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return time.time()
