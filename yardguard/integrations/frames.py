"""Latest picture of a camera, for phones, Telegram /snapshot and Home Assistant.

While monitoring runs, the monitor's own frames are used (read-only; no extra camera stream).
Otherwise a low-resolution sub-stream is opened on demand and closed again after a minute
without requests. Never sends camera addresses or passwords anywhere.
"""
from __future__ import annotations

import logging
import threading
import time

from .media import encode_jpeg

log = logging.getLogger("yardguard")

IDLE_STOP_S = 60.0       # own streams close after this long without requests
FIRST_FRAME_S = 8.0      # an own stream gets this long for its first picture
OWN_FPS = 2.0
OWN_LONG_SIDE = 960


def _source_factory(url: str, fps: float, long_side: int, gpu_decode: bool):
    from ..stream import FrameSource
    return FrameSource(url, fps, long_side=long_side, gpu_decode=gpu_decode)


class FrameProvider:
    """Latest BGR frame / JPEG per camera of the active site; safe to use from any thread.
    latest() may block up to 8 s when it has to open a stream: never call it on the GUI thread."""

    def __init__(self, ctx, source_factory=None) -> None:
        self.ctx = ctx
        self.source_factory = source_factory or _source_factory
        self.idle_stop_s = IDLE_STOP_S
        self.first_frame_s = FIRST_FRAME_S
        self._lock = threading.Lock()
        self._own: dict[int, dict] = {}       # channel -> {"src", "used", "url_key"}
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()
        self._closed = False

    # -- public --------------------------------------------------------------------------------
    def cameras(self) -> list[tuple[int, str]]:
        """(channel, label) of the enabled cameras of the active site."""
        try:
            site = self.ctx.cfg.active_site()
        except Exception:
            return []
        return [(c.channel, c.label) for c in site.enabled_cameras()] if site is not None else []

    def camera_name(self, channel: int) -> str:
        return next((name for ch, name in self.cameras() if ch == channel), f"Camera {channel}")

    def latest(self, channel: int, max_age: float = 5.0, wait: float | None = None):
        """The newest frame (BGR numpy array) of a camera, or None."""
        if self._closed:
            return None
        found, frame = self._from_monitor(channel, max_age)
        if found:
            return frame
        return self._from_own(int(channel), max_age, self.first_frame_s if wait is None else wait)

    def latest_jpeg(self, channel: int, max_side: int = 1280, quality: int = 80) -> bytes | None:
        frame = self.latest(channel)
        return encode_jpeg(frame, max_side, quality, 5_000_000) if frame is not None else None

    def reset(self) -> None:
        """Close the own streams (cameras or passwords changed); they reopen on the next request."""
        with self._lock:
            own, self._own = self._own, {}
        for item in own.values():
            self._stop_source(item["src"])

    def close(self) -> None:
        self._closed = True
        self._stop.set()
        self.reset()
        t = self._reaper
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._reaper = None

    # -- monitor frames --------------------------------------------------------------------------
    def _from_monitor(self, channel: int, max_age: float):
        """(found, frame): found is True when the running monitor watches this camera (then its
        frame is the answer, even if None, so the NVR does not get a second connection)."""
        mon = getattr(self.ctx, "monitor", None)
        try:
            if mon is None or not mon.running:
                return False, None
            cams = list(getattr(mon, "_cams", []) or [])   # private, read-only use (engine.py unchanged)
        except Exception:
            return False, None
        for cam in cams:
            try:
                if int(getattr(cam, "channel", -1)) != int(channel):
                    continue
                got = cam.source.latest()
            except Exception:
                return False, None
            if got is None:
                return True, None
            frame, ts = got
            if frame is None or time.time() - float(ts) > max_age:
                return True, None
            return True, frame
        return False, None

    # -- own streams ------------------------------------------------------------------------------
    def _url_for(self, channel: int) -> str | None:
        try:
            site = self.ctx.cfg.active_site()
            cam = site.camera(channel) if site is not None else None
            if cam is None or not cam.enabled or not site.host:
                return None
            return site.rtsp_url(channel, main=False)
        except Exception:
            return None

    def _from_own(self, channel: int, max_age: float, wait: float):
        url = self._url_for(channel)
        if url is None:
            return None
        with self._lock:
            if self._closed:
                return None
            item = self._own.get(channel)
            if item is not None and item["url"] != url:
                self._own.pop(channel)
                old, item = item, None
                threading.Thread(target=self._stop_source, args=(old["src"],), daemon=True,
                                 name="yg-frames-stop").start()
            if item is None:
                try:
                    src = self.source_factory(url, OWN_FPS, OWN_LONG_SIDE, False)
                    src.start()
                except Exception as exc:
                    log.warning("Could not open camera %s for a snapshot: %s", channel, type(exc).__name__)
                    return None
                item = {"src": src, "used": time.monotonic(), "url": url}
                self._own[channel] = item
            item["used"] = time.monotonic()
            self._ensure_reaper()
        src = item["src"]
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            got = None
            try:
                got = src.latest()
            except Exception:
                got = None
            if got is not None and got[0] is not None and time.time() - float(got[1]) <= max(max_age, 10.0):
                with self._lock:
                    item["used"] = time.monotonic()
                return got[0]
            if time.monotonic() >= deadline or self._stop.is_set():
                return None
            self._stop.wait(0.05)

    def _ensure_reaper(self) -> None:
        if self._reaper is None or not self._reaper.is_alive():
            self._reaper = threading.Thread(target=self._reap_loop, name="yg-frames", daemon=True)
            self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(min(1.0, max(0.05, self.idle_stop_s / 4))):
            now = time.monotonic()
            with self._lock:
                idle = [ch for ch, it in self._own.items() if now - it["used"] > self.idle_stop_s]
                items = [self._own.pop(ch) for ch in idle]
                empty = not self._own
            for it in items:
                self._stop_source(it["src"])
            if empty:
                with self._lock:
                    if not self._own:
                        self._reaper = None
                        return

    @staticmethod
    def _stop_source(src) -> None:
        try:
            src.stop()
        except Exception:
            pass

    def own_channels(self) -> list[int]:
        """Channels with an own stream open right now (for tests and diagnostics)."""
        with self._lock:
            return sorted(self._own)
