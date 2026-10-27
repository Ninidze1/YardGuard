"""Live pictures for phones: one shared JPEG encoder per camera, however many phones watch.

An encoder runs only while someone watches (or asked for a still picture in the last 30 s): every
1/fps seconds it takes the newest frame from the shared FrameProvider, shrinks it and encodes it
once; every viewer gets the same bytes. Nothing here knows camera addresses or passwords.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import cv2
import numpy as np

log = logging.getLogger("yardguard")

IDLE_STOP_S = 30.0       # an encoder without viewers stops after this long
JPEG_QUALITY = 70
MAX_PER_CAMERA = 4
MAX_TOTAL = 8


def encode_frame(frame: np.ndarray, max_side: int, quality: int = JPEG_QUALITY) -> bytes | None:
    """Shrink (never enlarge) to max_side and encode as JPEG."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    h, w = frame.shape[:2]
    scale = min(1.0, float(max_side) / max(h, w)) if max_side else 1.0
    if scale < 1.0:
        frame = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes() if ok else None


class LiveEncoder:
    """Encodes one camera's newest frame at `fps` while it has viewers."""

    def __init__(self, channel: int, get_frame: Callable[[int], np.ndarray | None], fps: float = 2.0,
                 max_side: int = 960, idle_s: float = IDLE_STOP_S) -> None:
        self.channel = int(channel)
        self._get_frame = get_frame
        self.fps = max(0.2, min(10.0, float(fps or 2.0)))
        self.max_side = int(max_side or 960)
        self.idle_s = float(idle_s)
        self.viewers = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._seq = 0
        self._jpeg: bytes | None = None
        self._jpeg_ts = 0.0
        self._last_use = time.monotonic()
        self._last_frame = None
        self._thread = threading.Thread(target=self._run, name=f"yg-live-{self.channel}", daemon=True)

    # -- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def touch(self) -> None:
        self._last_use = time.monotonic()

    # -- viewers ------------------------------------------------------------------------
    def wait_frame(self, after_seq: int, timeout: float) -> tuple[int, bytes] | None:
        """The next frame newer than after_seq (waits up to timeout s), or None."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= after_seq and not self._stop.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            if self._seq > after_seq and self._jpeg is not None:
                return self._seq, self._jpeg
        return None

    def latest(self, max_age: float = 3.0) -> bytes | None:
        with self._cond:
            if self._jpeg is not None and time.monotonic() - self._jpeg_ts <= max_age:
                return self._jpeg
        return None

    def snapshot(self, timeout: float = 8.0) -> bytes | None:
        """A current picture: the latest one when fresh, else waits for the next one."""
        self.touch()
        fresh = self.latest()
        if fresh is not None:
            return fresh
        with self._cond:
            seq = self._seq
        got = self.wait_frame(seq, timeout)
        return got[1] if got else None

    # -- encoder thread -----------------------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.fps
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                if self.viewers <= 0 and t0 - self._last_use > self.idle_s:
                    break
                try:
                    frame = self._get_frame(self.channel)
                except Exception as exc:  # the frame source must never kill the encoder
                    log.debug("Live frame for camera %s failed: %s", self.channel, exc.__class__.__name__)
                    frame = None
                if frame is not None and frame is not self._last_frame:
                    self._last_frame = frame
                    try:
                        jpeg = encode_frame(frame, self.max_side)
                    except Exception as exc:
                        log.debug("Live encode for camera %s failed: %s", self.channel, exc.__class__.__name__)
                        jpeg = None
                    if jpeg:
                        with self._cond:
                            self._seq += 1
                            self._jpeg, self._jpeg_ts = jpeg, time.monotonic()
                            self._cond.notify_all()
                elif frame is not None:
                    with self._cond:   # same frame again: still fresh
                        self._jpeg_ts = time.monotonic()
                self._stop.wait(max(0.02, period - (time.monotonic() - t0)))
        finally:
            self._stop.set()
            self._last_frame = None
            with self._cond:
                self._cond.notify_all()


class LiveHub:
    """The encoders of all cameras with the viewer limits (per camera and in total)."""

    def __init__(self, get_frame: Callable[[int], np.ndarray | None], fps: Callable[[], float],
                 max_side: Callable[[], int], max_per_camera: int = MAX_PER_CAMERA, max_total: int = MAX_TOTAL,
                 idle_s: float = IDLE_STOP_S) -> None:
        self._get_frame = get_frame
        self._fps = fps
        self._max_side = max_side
        self.max_per_camera = max_per_camera
        self.max_total = max_total
        self.idle_s = idle_s
        self._lock = threading.Lock()
        self._encoders: dict[int, LiveEncoder] = {}
        self._closed = False

    def _encoder_locked(self, channel: int) -> LiveEncoder | None:
        if self._closed:
            return None
        enc = self._encoders.get(channel)
        if enc is None or not enc.alive:
            enc = LiveEncoder(channel, self._get_frame, self._fps(), self._max_side(), self.idle_s)
            self._encoders[channel] = enc
            enc.start()
        return enc

    def encoder(self, channel: int) -> LiveEncoder | None:
        """The camera's running encoder (started if needed), without taking a viewer slot."""
        with self._lock:
            enc = self._encoder_locked(int(channel))
            if enc is not None:
                enc.touch()
            return enc

    def acquire(self, channel: int) -> LiveEncoder | None:
        """Take a viewer slot: None when the limits are reached (or the hub is closed)."""
        channel = int(channel)
        with self._lock:
            if self._closed:
                return None
            total = sum(e.viewers for e in self._encoders.values() if e.alive)
            enc = self._encoders.get(channel)
            here = enc.viewers if enc is not None and enc.alive else 0
            if total >= self.max_total or here >= self.max_per_camera:
                return None
            enc = self._encoder_locked(channel)
            enc.viewers += 1
            enc.touch()
            return enc

    def release(self, enc: LiveEncoder) -> None:
        with self._lock:
            enc.viewers = max(0, enc.viewers - 1)
            enc.touch()

    def viewers(self) -> int:
        with self._lock:
            return sum(e.viewers for e in self._encoders.values() if e.alive)

    def open(self) -> None:
        with self._lock:
            self._closed = False

    def close(self, timeout: float = 1.5) -> None:
        """Stop every encoder (viewers see their stream end)."""
        with self._lock:
            self._closed = True
            encoders = list(self._encoders.values())
            self._encoders.clear()
        for enc in encoders:
            enc.stop(0)
        deadline = time.monotonic() + timeout
        for enc in encoders:
            enc.stop(max(0.0, deadline - time.monotonic()))
