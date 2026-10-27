"""Camera stream helpers and the YOLO detector shared by the app and scripts."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

# Force TCP for RTSP: far fewer broken/grey frames than UDP
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import numpy as np

from .config import ROOT, masked_url

CLASSES = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
TIMEOUT_MS = 6000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def open_capture(url: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, TIMEOUT_MS,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, TIMEOUT_MS,
    ])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def grab_frame(url: str):
    cap = open_capture(url)
    try:
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def _tool(name: str) -> str | None:
    """ffmpeg / ffprobe, also when this process started with a PATH older than the ffmpeg install."""
    from .stream import ffmpeg_path
    ffmpeg = ffmpeg_path()
    if name == "ffmpeg" or not ffmpeg:
        return ffmpeg
    probe = Path(ffmpeg).with_name(f"{name}.exe")
    return str(probe) if probe.exists() else shutil.which(name)


def grab_frame_fast(url: str, timeout: float = 15):
    """One frame via a separate ffmpeg process. OpenCV opens streams one at a time,
    separate processes let all camera previews load in parallel."""
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        return grab_frame(url)
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-rtsp_transport", "tcp", "-timeout", "6000000", "-i", url,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"],
            capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def is_no_signal(frame) -> bool:
    """True for the black 'no video' picture NVRs send on empty channels."""
    gray = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
    return float((gray > 40).mean()) < 0.03


def test_connection(url: str) -> tuple[bool, str]:
    """Open the stream and explain any failure in plain words."""
    ffprobe = _tool("ffprobe")
    if not ffprobe:
        ok = grab_frame(url) is not None
        return ok, "Connected." if ok else "Could not open the video. Check address, login and channel."

    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", "6000000",
             "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", "-i", url],
            capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "The device did not answer. Check the address and that this PC is on the same network."

    if r.returncode == 0:
        size = re.search(r"(\d+),(\d+)", r.stdout)
        return True, f"Connected: video {size.group(1)}×{size.group(2)}." if size else "Connected."

    out = (r.stdout + r.stderr).lower()
    if "401" in out or "unauthorized" in out:
        return False, "Wrong username or password."
    if "404" in out or "not found" in out:
        return False, "Connected, but this channel or stream path does not exist."
    if "refused" in out:
        return False, "The device refused the connection. Check the RTSP port (usually 554)."
    if any(w in out for w in ("timed out", "timeout", "unreachable", "no route", "10060")):
        return False, "Device not reachable. Check the IP address."
    last = masked_url((r.stderr.strip().splitlines() or ["unknown error"])[-1])
    return False, f"Could not open the video ({last[:160]})."


MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/{name}.pt"
_download_lock = threading.Lock()


def model_path(name: str) -> Path:
    return ROOT / "models" / f"{name}.pt"


def model_ready(name: str) -> bool:
    path = model_path(name)
    return path.exists() and path.stat().st_size > 1_000_000


def partial_path(name: str) -> Path:
    return model_path(name).with_suffix(".download")


class ModelDownload:
    """Resumable download of one model. The data goes to <name>.download and is renamed
    to <name>.pt only when complete, so a broken file is never used as a model.
    pause() keeps the partial file (resume continues from there), cancel() deletes it."""

    def __init__(self, name: str):
        self.name = name
        self.lock = threading.Lock()
        self._stop: str | None = None

    def pause(self) -> None:
        self._stop = "paused"

    def cancel(self) -> None:
        self._stop = "cancelled"
        if not self.lock.locked():
            partial_path(self.name).unlink(missing_ok=True)

    def run(self, progress=None) -> str:
        """Download until done, paused or cancelled; returns that word.
        progress(done_bytes, total_bytes) is called while downloading."""
        with self.lock:
            self._stop = None
            if model_ready(self.name):
                return "done"
            path, tmp = model_path(self.name), partial_path(self.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            done = tmp.stat().st_size if tmp.exists() else 0
            headers = {"User-Agent": "YardGuard"}
            if done:
                headers["Range"] = f"bytes={done}-"
            req = urllib.request.Request(MODEL_URL.format(name=self.name), headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and done:  # server says nothing is left: the file is complete
                    tmp.replace(path)
                    return "done"
                raise ConnectionError(f"Download failed (server error {exc.code}).") from exc
            except OSError as exc:
                raise ConnectionError(f"Download failed. Check the internet connection. ({exc})") from exc
            with resp:
                if done and resp.status != 206:  # server ignored the resume request: start over
                    done = 0
                total = done + int(resp.headers.get("Content-Length") or 0)
                try:
                    with open(tmp, "ab" if done else "wb") as f:
                        while chunk := resp.read(256 * 1024):
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
                            if self._stop:
                                break
                except OSError as exc:
                    raise ConnectionError(f"Download interrupted. Resume to continue. ({exc})") from exc
            if self._stop == "cancelled":
                tmp.unlink(missing_ok=True)
                return "cancelled"
            if self._stop == "paused":
                return "paused"
            if total and done < total:
                raise ConnectionError("Download interrupted. Resume to continue.")
            tmp.replace(path)
            return "done"


_downloads: dict[str, ModelDownload] = {}


def get_download(name: str) -> ModelDownload:
    with _download_lock:
        return _downloads.setdefault(name, ModelDownload(name))


def ensure_model(name: str, progress=None) -> Path:
    """Make sure a model is on disk, downloading (or finishing a paused download) if needed."""
    if not model_ready(name):
        result = get_download(name).run(progress)
        if result != "done":
            raise ConnectionError(f"The model download was {result}. Finish it in Settings → AI models.")
    return model_path(name)


def delete_model(name: str) -> None:
    model_path(name).unlink(missing_ok=True)
    partial_path(name).unlink(missing_ok=True)


class Detector:
    """YOLO model loaded once and shared; safe to call from worker threads."""

    def __init__(self) -> None:
        self._model = None
        self._name = None
        self._device = None
        self._lock = threading.Lock()

    def is_ready(self, name: str) -> bool:
        return self._name == name

    def detect(self, frame, model_name: str, conf: float):
        with self._lock:
            if self._name != model_name:
                import torch
                from ultralytics import YOLO

                self._device = 0 if torch.cuda.is_available() else "cpu"
                self._model = YOLO(str(ensure_model(model_name)))
                self._name = model_name
            result = self._model.predict(frame, classes=list(CLASSES), conf=conf,
                                         device=self._device, verbose=False)[0]
        counts: dict[str, int] = {}
        for cls in result.boxes.cls.int().tolist():
            counts[CLASSES[cls]] = counts.get(CLASSES[cls], 0) + 1
        return result.plot(line_width=2), counts
