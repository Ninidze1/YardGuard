"""Continuous camera frames for the AI pipeline, decoded by an ffmpeg subprocess.

One FrameSource per camera: ffmpeg decodes the RTSP stream (on the GPU with NVDEC when
possible), drops frames down to the AI rate, scales to the detection size and pipes raw
BGR pixels to a reader thread that keeps only the newest frame. Decoding in a separate
process keeps the GIL free for the app and survives broken streams; a supervisor thread
restarts ffmpeg with backoff, and falls back to CPU decoding when the GPU path fails.
"""
from __future__ import annotations

import ctypes
import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

import numpy as np

from .config import ROOT, masked_url

log = logging.getLogger(__name__)
BUNDLED_FFMPEG = ROOT / "tools" / "ffmpeg" / "bin"
FFMPEG_MISSING = "ffmpeg is not installed. Run Update.cmd in the YardGuard folder to install it."

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STALL_S = 12.0          # no frame for this long while ffmpeg runs: restart it
OPEN_TIMEOUT_S = 20.0   # time allowed from spawn to the first frame
BACKOFF_S = (1, 2, 5, 10, 20, 30)
_SIZE_RE = re.compile(r"Video: rawvideo.*?\b(\d{2,5})x(\d{2,5})\b")


# --- ffmpeg location ---------------------------------------------------------

_ffmpeg: str | None = None


def ffmpeg_path() -> str | None:
    """ffmpeg.exe from the app's own tools folder (put there by setup\\bootstrap.ps1), else from
    PATH, the persistent user/machine PATH and the WinGet folder. Apps started before ffmpeg was
    installed still have the old PATH in their environment."""
    global _ffmpeg
    if _ffmpeg and os.path.exists(_ffmpeg):
        return _ffmpeg
    bundled = BUNDLED_FFMPEG / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    found = str(bundled) if bundled.exists() else shutil.which("ffmpeg")
    if not found and os.name == "nt":
        found = _ffmpeg_from_registry() or _ffmpeg_from_winget()
    _ffmpeg = found
    return found


def _ffmpeg_from_registry() -> str | None:
    import winreg

    keys = [(winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")]
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                value = os.path.expandvars(winreg.QueryValueEx(k, "Path")[0])
        except OSError:
            continue
        found = shutil.which("ffmpeg", path=value)
        if found:
            return found
    return None


def _ffmpeg_from_winget() -> str | None:
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    hits = sorted(glob.glob(os.path.join(base, "*FFmpeg*", "*", "bin", "ffmpeg.exe")))
    return hits[-1] if hits else None


# --- Kill ffmpeg together with this process ----------------------------------

class _KillOnCloseJob:
    """Windows job object: every ffmpeg assigned to it dies when YardGuard exits,
    even after a crash, so no orphaned decoders keep the GPU and network busy."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = k32.CreateJobObjectW(None, None)
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes on x64; LimitFlags sits at offset 16
            info = (ctypes.c_byte * 144)()
            ctypes.c_uint32.from_buffer(info, 16).value = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if handle and k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle = handle
        except (AttributeError, OSError):
            self.handle = None

    def add(self, pid: int) -> None:
        if not self.handle:
            return
        k32 = ctypes.windll.kernel32
        proc = k32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if proc:
            k32.AssignProcessToJobObject(self.handle, proc)
            k32.CloseHandle(proc)


_job: _KillOnCloseJob | None = None
_job_lock = threading.Lock()


def _assign_to_job(pid: int) -> None:
    global _job
    with _job_lock:
        if _job is None:
            _job = _KillOnCloseJob()
    _job.add(pid)


# --- Frame source ------------------------------------------------------------

def _explain(stderr: str) -> str:
    """Turn the tail of ffmpeg's log into a short plain-language reason."""
    text = stderr.lower()
    if "401" in text or "unauthorized" in text:
        return "Wrong username or password."
    if "404" in text or "not found" in text and "rtsp" in text:
        return "This channel or stream path does not exist."
    if "refused" in text:
        return "The device refused the connection."
    if any(w in text for w in ("timed out", "timeout", "unreachable", "10060", "no route")):
        return "Camera not reachable."
    lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    return masked_url(lines[-1])[:160] if lines else "Video stream stopped."


class FrameSource:
    """Newest frame of one camera, decoded continuously in the background.

    latest() never blocks; it returns (bgr_frame, capture_time) or None until the first
    frame arrives. The frame array is replaced, never written to, so callers may keep it.
    """

    def __init__(self, url: str, fps: float, long_side: int = 1280, gpu_decode: bool = True) -> None:
        self.url = url
        self.fps = float(fps)
        self.long_side = int(long_side or 0)
        self.gpu_decode = gpu_decode
        self._gpu_ok = gpu_decode       # cleared once the GPU path fails while the CPU path works
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._frame: tuple[np.ndarray, float] | None = None
        self._times: deque[float] = deque(maxlen=30)
        self._size: tuple[int, int] | None = None
        self._backend = "nvdec" if gpu_decode else "cpu"
        self._error: str | None = None
        self._alive = False
        self.frames = 0                 # frames delivered since start()
        self.restarts = 0

    # --- public API ---

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name=f"FrameSource {masked_url(self.url)}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        self._alive = False

    def latest(self) -> tuple[np.ndarray, float] | None:
        with self._lock:
            return self._frame

    @property
    def alive(self) -> bool:
        """True while frames keep arriving."""
        return self._alive and self._frame is not None and time.time() - self._frame[1] < STALL_S

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def size(self) -> tuple[int, int] | None:
        return self._size

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def fps_actual(self) -> float:
        """Frames per second delivered over the last few seconds."""
        with self._lock:
            times = list(self._times)
        if len(times) < 2 or time.time() - times[-1] > STALL_S:
            return 0.0
        return (len(times) - 1) / max(times[-1] - times[0], 1e-6)

    # --- ffmpeg command ---

    def _command(self, gpu: bool) -> list[str]:
        ffmpeg = ffmpeg_path()
        filters = [f"fps={self.fps:g}"] if self.fps > 0 else []
        box = ""
        if self.long_side > 0:
            # fit inside long_side x long_side, never upscale, keep even sizes for the chroma planes
            n = self.long_side
            box = f"w='min({n},iw)':h='min({n},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        if gpu:
            # decode + resize stay in GPU memory; only the small frame is downloaded
            filters.append(f"scale_cuda={box + ':' if box else ''}format=nv12")
            filters += ["hwdownload", "format=nv12"]
            pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        else:
            if box:
                filters.append(f"scale={box}")
            pre = ["-threads", "2"]
        src = ["-rtsp_transport", "tcp", "-timeout", "6000000"] if self.url.lower().startswith("rtsp") \
            else ["-re", "-stream_loop", "-1"]
        return [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info", *pre,
                "-fflags", "nobuffer+discardcorrupt", "-flags", "low_delay", *src, "-i", self.url,
                "-an", "-sn", "-dn", "-vf", ",".join(filters), "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]

    # --- supervisor ---

    def _supervise(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if not ffmpeg_path():
                self._error = FFMPEG_MISSING
                self._stop.wait(30)
                continue
            gpu = self._gpu_ok
            got_frames, opened, reason = self._run_once(gpu)
            if self._stop.is_set():
                break
            self._alive = False
            if gpu and opened and not got_frames:
                # the stream opened but the GPU pipeline gave nothing: NVDEC/CUDA is the problem
                log.info("GPU decode gave no frames for %s (%s); trying CPU", masked_url(self.url), reason)
                cpu_frames, _, cpu_reason = self._run_once(False)
                if self._stop.is_set():
                    break
                if cpu_frames:
                    self._gpu_ok = False
                    log.warning("Using CPU decode for %s: GPU decode failed (%s)", masked_url(self.url), reason)
                    attempt = 0
                    continue
                reason = cpu_reason
            attempt = 0 if got_frames else attempt + 1
            self._error = reason
            self.restarts += 1
            delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)] if attempt else 1
            log.info("Stream %s stopped (%s); reconnecting in %ss", masked_url(self.url), reason, delay)
            self._stop.wait(delay)

    def _run_once(self, gpu: bool) -> tuple[bool, bool, str]:
        """Run one ffmpeg until it ends or stalls.
        Returns (delivered frames?, input stream opened?, reason it ended)."""
        self._backend = "nvdec" if gpu else "cpu"
        try:
            proc = subprocess.Popen(self._command(gpu), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0, creationflags=NO_WINDOW)
        except OSError as exc:
            return False, False, f"Could not start ffmpeg ({exc})."
        _assign_to_job(proc.pid)
        self._proc = proc
        log_tail: deque[str] = deque(maxlen=40)
        size_ready = threading.Event()
        state = {"frames": 0, "last": time.time(), "size": None, "opened": False}
        err_thread = threading.Thread(target=self._drain_stderr, args=(proc, log_tail, size_ready, state),
                                      daemon=True)
        err_thread.start()
        reader = threading.Thread(target=self._read_frames, args=(proc, size_ready, state), daemon=True)
        reader.start()

        started = time.time()
        reason = ""
        while not self._stop.is_set():
            reader.join(timeout=0.5)
            if not reader.is_alive():
                break
            now = time.time()
            if state["frames"] == 0 and now - started > OPEN_TIMEOUT_S:
                reason = "The camera did not send video in time."
                break
            if state["frames"] and now - state["last"] > STALL_S:
                reason = "The video stream froze."
                break
        self._kill(proc)
        reader.join(timeout=3)
        err_thread.join(timeout=3)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        if not reason:
            reason = _explain("\n".join(log_tail))
        return state["frames"] > 0, state["opened"], reason

    def _drain_stderr(self, proc: subprocess.Popen, tail: deque, size_ready: threading.Event, state: dict) -> None:
        in_output = False
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                tail.append(line)
                if line.startswith("Input #0"):
                    state["opened"] = True
                elif line.startswith("Output #0"):
                    in_output = True
                elif in_output and not size_ready.is_set():
                    m = _SIZE_RE.search(line)
                    if m:
                        state["size"] = self._size = (int(m.group(1)), int(m.group(2)))
                        size_ready.set()
        except (OSError, ValueError):  # pipe closed while stopping
            pass
        size_ready.set()  # ffmpeg ended: unblock the reader

    def _read_frames(self, proc: subprocess.Popen, size_ready: threading.Event, state: dict) -> None:
        if not size_ready.wait(OPEN_TIMEOUT_S + 5) or state["size"] is None:
            return
        w, h = state["size"]
        nbytes = w * h * 3
        out = proc.stdout
        while not self._stop.is_set():
            buf = bytearray(nbytes)
            view = memoryview(buf)
            got = 0
            while got < nbytes:
                try:
                    n = out.readinto(view[got:])
                except (OSError, ValueError):  # pipe closed while stopping
                    return
                if not n:
                    return
                got += n
            now = time.time()
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            with self._lock:
                self._frame = (frame, now)
                self._times.append(now)
            state["frames"] += 1
            state["last"] = now
            self.frames += 1
            self._alive = True
            self._error = None

    def _kill(self, proc: subprocess.Popen | None = None) -> None:
        """Kill ffmpeg (the given one, or the running one) and reap it. ffmpeg starts no
        children, so killing the process is the whole tree."""
        proc = proc or self._proc
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
            log.warning("ffmpeg %s did not exit", proc.pid)
        if self._proc is proc:
            self._proc = None

    def __repr__(self) -> str:
        return f"FrameSource({masked_url(self.url)}, {self.fps:g} fps, {self._backend}, size={self._size})"
