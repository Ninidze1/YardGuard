"""LocateAnything "brain": client side of the slow, smart second-opinion model.

The model needs pinned library versions (transformers 4.57.1, ...) that must not touch the
app's own venv, so it lives in its own venv (.venv-brain) and runs in its own process
(yardguard/brain_worker.py). Stopping that process frees all of its GPU memory.

Brain is thread-safe and has no Qt dependency: the UI calls it through run_async.
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .config import ROOT

BRAIN_VENV = ROOT / ".venv-brain"
BRAIN_PYTHON = BRAIN_VENV / "Scripts" / "python.exe"
BUNDLED_PYTHON = ROOT / "runtime" / "python" / "python.exe"
MODEL_REPO = "nvidia/LocateAnything-3B"
MODEL_DIR = ROOT / "models" / "LocateAnything-3B"
CACHE = ROOT / ".cache"
WORK_DIR = CACHE / "brain"
LOG_PATH = WORK_DIR / "worker.log"
MARKER = BRAIN_VENV / "yardguard-brain.json"  # written when the install was verified
WORKER = Path(__file__).with_name("brain_worker.py")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TORCH_INDEX = "https://download.pytorch.org/whl/cu128"  # CUDA 12.8 wheels: RTX 20xx .. 50xx
# Same version as the app (setup/torch-cu128.txt)
TORCH_PACKAGES = ["torch==2.9.1", "torchvision==0.24.1"]
# Only what the model's remote code imports. decord / lmdb / opencv are imported by the
# model's processor for video/dataset loading but never used: the worker stubs them
# (their native DLLs break torch on Windows).
PACKAGES = ["transformers==4.57.1", "huggingface_hub>=0.34,<1.0", "tokenizers>=0.22,<0.23",
            "peft", "accelerate", "pillow", "numpy", "requests", "safetensors"]
MODEL_FILES = ["config.json", "model.safetensors.index.json", "model-00001-of-00002.safetensors",
               "model-00002-of-00002.safetensors", "modeling_locateanything.py", "tokenizer_config.json"]
MODEL_SIZE_MB = 7300
MAX_UPSCALE = 3.0   # ask(upscale=True) enlarges a small picture at most this much

_DOWNLOAD_SCRIPT = r'''
import json, os, sys, threading, time
from pathlib import Path
from huggingface_hub import HfApi, snapshot_download
repo, dest = sys.argv[1], Path(sys.argv[2])
ignore = ["assets/*", "*.mp4"]
def say(**kw):
    print(json.dumps(kw), flush=True)
try:
    info = HfApi().model_info(repo, files_metadata=True)
    total = sum((s.size or 0) for s in info.siblings if not s.rfilename.startswith("assets/"))
except Exception:
    total = 0
def size():
    n = 0
    for p in dest.rglob("*"):
        try:
            if p.is_file():
                n += p.stat().st_size
        except OSError:
            pass
    return n
err = []
def run():
    try:
        snapshot_download(repo, local_dir=str(dest), ignore_patterns=ignore, max_workers=8)
    except Exception as e:
        err.append(f"{type(e).__name__}: {e}")
t = threading.Thread(target=run, daemon=True)
t.start()
while t.is_alive():
    say(done=size(), total=total)
    t.join(2)
if err:
    say(error=err[0]); sys.exit(1)
say(done=total or size(), total=total or size(), finished=True)
'''


class BrainError(Exception):
    pass


def base_python() -> Path:
    """The real Python installation to create a new venv from: the app's own copy in
    runtime\\python (put there by setup\\bootstrap.ps1) when present, else the one behind the
    running interpreter. Under a venv sys.executable is the venv's copy; _base_executable is
    the original. Prefer python.exe over pythonw.exe so pip output can be read."""
    candidates = [str(BUNDLED_PYTHON), getattr(sys, "_base_executable", ""), sys.executable]
    cfg = ROOT / ".venv" / "pyvenv.cfg"
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "home":
                candidates.append(str(Path(value.strip()) / "python.exe"))
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if p.name.lower() == "pythonw.exe":
            p = p.with_name("python.exe")
        if p.exists() and ".venv" not in {part.lower() for part in p.parts}:
            return p
    raise BrainError("Could not find the Python installation to create the AI model environment.")


def smart_app_control_on() -> bool:
    """Windows 11 Smart App Control in enforcing mode (1; 0 = off, 2 = evaluation, which does not block)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as k:
            return winreg.QueryValueEx(k, "VerifiedAndReputablePolicyState")[0] == 1
    except (OSError, ImportError):
        return False


def has_nvidia_gpu() -> bool:
    """An NVIDIA driver is installed (it brings nvidia-smi.exe). Cheap: no CUDA start-up.
    The brain only runs on an NVIDIA GPU; the fast detector also works on the processor."""
    if shutil.which("nvidia-smi"):
        return True
    system32 = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
    return (system32 / "nvidia-smi.exe").exists()


def _env() -> dict:
    """Keep every cache and temp file inside the YardGuard folder."""
    tmp = CACHE / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(PIP_CACHE_DIR=str(CACHE / "pip"), TEMP=str(tmp), TMP=str(tmp), HF_HOME=str(CACHE / "hf"),
               HF_MODULES_CACHE=str(CACHE / "hf" / "modules"), TORCH_HOME=str(CACHE / "torch"),
               HF_HUB_DISABLE_TELEMETRY="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION="python", PIP_DISABLE_PIP_VERSION_CHECK="1")
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(key, None)
    return env


def model_downloaded() -> bool:
    return all((MODEL_DIR / f).exists() for f in MODEL_FILES)


def installed() -> bool:
    return BRAIN_PYTHON.exists() and MARKER.exists() and model_downloaded()


class Brain:
    def __init__(self) -> None:
        self._lock = threading.RLock()       # guards state below
        self._ask_lock = threading.Lock()    # one request in flight at a time
        self._proc: subprocess.Popen | None = None
        self._state = "stopped"              # stopped | starting | ready | busy | error
        self._installing = False
        self._cancel = threading.Event()
        self._install_proc: subprocess.Popen | None = None
        self._pending: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._latencies: deque[float] = deque(maxlen=20)
        self._last_latency: float | None = None
        self._vram_mb: int | None = None
        self.load_s: float | None = None
        self.last_error: str | None = None
        # The user pressed Stop: nothing starts the worker again on its own (monitoring starts,
        # restarts, error recovery) until the user presses Start or turns monitoring on again.
        self.user_stopped = False
        self.install_step: tuple[str, int, int] | None = None  # (text, done, total) while installing
        self.last_answer: dict = {}   # details of the newest answer (model calls, raw text) for diagnosis
        atexit.register(self.stop)

    # --- state -------------------------------------------------------------

    def status(self) -> str:
        with self._lock:
            if self._installing:
                return "installing"
            if self._state in ("starting", "ready", "busy", "error"):
                return self._state
            return "stopped" if installed() else "not_installed"

    def stats(self) -> dict:
        with self._lock:
            avg = sum(self._latencies) / len(self._latencies) if self._latencies else None
            return {"last_latency_s": self._last_latency, "vram_mb": self._vram_mb,
                    "avg_latency_s": avg, "load_s": self.load_s}

    # --- install -----------------------------------------------------------

    def install(self, progress=None) -> None:
        """Set up everything a fresh PC needs. Blocking, idempotent and resumable: finished
        steps are skipped, an interrupted model download continues where it stopped."""
        with self._lock:
            if self._installing:
                raise BrainError("Installation is already running.")
            self._installing = True
            self._cancel.clear()
            self.install_step = ("Preparing…", 0, 0)

        def report(text, done, total) -> None:  # every view of the install shows the same step
            self.install_step = (str(text), int(done), int(total))
            if progress:
                progress(text, done, total)

        try:
            steps = 4
            if not has_nvidia_gpu():
                raise BrainError("LocateAnything needs an NVIDIA graphics card, and none was found on this PC.")
            if not BRAIN_PYTHON.exists():
                report("Creating the AI model environment", 0, steps)
                self._run([str(base_python()), "-m", "venv", str(BRAIN_VENV)])
            if not self._has_packages():
                report("Installing PyTorch for the GPU (about 3 GB)", 1, steps)
                self._run([str(BRAIN_PYTHON), "-m", "pip", "install", "--progress-bar", "off",
                           *TORCH_PACKAGES, "--index-url", TORCH_INDEX])
                report("Installing model libraries", 2, steps)
                self._run([str(BRAIN_PYTHON), "-m", "pip", "install", "--progress-bar", "off", *PACKAGES])
            report("Downloading LocateAnything-3B (about 7.3 GB)", 3, steps)
            self._download(report, steps)
            report("Checking the installation", 4, steps)
            self._verify()
            report("Installed", steps, steps)
        finally:
            with self._lock:
                self._installing = False
                self._install_proc = None
                self.install_step = None

    def cancel_install(self) -> None:
        self._cancel.set()
        with self._lock:
            proc = self._install_proc
        if proc and proc.poll() is None:
            _kill_tree(proc)

    def _has_packages(self) -> bool:
        if not BRAIN_PYTHON.exists():
            return False
        code = "import torch, torchvision, transformers, peft, accelerate, PIL; print(transformers.__version__)"
        r = subprocess.run([str(BRAIN_PYTHON), "-I", "-c", code], capture_output=True, text=True,
                           env=_env(), creationflags=NO_WINDOW)
        return r.returncode == 0 and r.stdout.strip() == "4.57.1"

    def _run(self, cmd: list[str], on_line=None) -> None:
        """Run an install command, cancellable; raise BrainError with the useful tail of its output."""
        if self._cancel.is_set():
            raise BrainError("Installation cancelled.")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", env=_env(), cwd=str(CACHE),
                                creationflags=NO_WINDOW)
        with self._lock:
            self._install_proc = proc
        tail: deque[str] = deque(maxlen=15)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                if on_line:
                    on_line(line)
        proc.wait()
        if self._cancel.is_set():
            raise BrainError("Installation cancelled.")
        if proc.returncode != 0:
            text = "\n".join(tail)
            if "blocked" in text.lower() and "policy" in text.lower():
                text = "Windows blocked a file (is Smart App Control on?):\n" + text
            raise BrainError(f"Install step failed ({Path(cmd[0]).name} exit {proc.returncode}):\n{text}")

    def _download(self, report, steps: int) -> None:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        script = WORK_DIR / "download_model.py"
        script.write_text(_DOWNLOAD_SCRIPT, encoding="utf-8")
        MODEL_DIR.mkdir(parents=True, exist_ok=True)

        def on_line(line: str) -> None:
            try:
                msg = json.loads(line)
            except ValueError:
                return
            if "done" in msg:
                done, total = msg["done"] / 2**20, (msg.get("total") or MODEL_SIZE_MB * 2**20) / 2**20
                report(f"Downloading LocateAnything-3B: {done / 1024:.1f} of {total / 1024:.1f} GB",
                       int(done), int(max(total, done)))
            elif "error" in msg:
                raise BrainError(f"Model download failed: {msg['error']}")

        self._run([str(BRAIN_PYTHON), "-I", str(script), MODEL_REPO, str(MODEL_DIR)], on_line)
        if not model_downloaded():
            raise BrainError("Model download did not complete. Run the installation again to resume.")

    def _verify(self) -> None:
        code = ("import torch, transformers; assert torch.cuda.is_available(), 'CUDA GPU not available'; "
                "print(torch.__version__, transformers.__version__, torch.cuda.get_device_name(0))")
        r = subprocess.run([str(BRAIN_PYTHON), "-I", "-c", code], capture_output=True, text=True,
                           env=_env(), creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise BrainError("The AI model environment does not work:\n" + "\n".join(r.stderr.splitlines()[-6:]))
        MARKER.write_text(json.dumps({"versions": r.stdout.strip(), "installed": time.time()}), encoding="utf-8")

    # --- worker lifecycle --------------------------------------------------

    def start(self, user: bool = False) -> None:
        """Spawn the worker (non-blocking). status(): starting -> ready once the model is loaded.
        user=True: the user pressed Start, which lifts an earlier user Stop."""
        with self._lock:
            if user:
                self.user_stopped = False
            if self._proc and self._proc.poll() is None and self._state in ("starting", "ready", "busy"):
                return
            if not installed():
                raise BrainError("LocateAnything is not installed.")
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            self._state = "starting"
            self.last_error = None
            log = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
            log.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            log.flush()
            # -I: isolated mode, the script folder (yardguard/) and PYTHON* variables are not used
            self._proc = subprocess.Popen(
                [str(BRAIN_PYTHON), "-I", str(WORKER), "--model", str(MODEL_DIR), "--parent-pid", str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, cwd=str(WORK_DIR), env=_env(),
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW)
            log.close()
            threading.Thread(target=self._reader, args=(self._proc,), daemon=True, name="brain-reader").start()

    def stop(self, user: bool = False) -> None:
        """Stop the worker and free its GPU memory. user=True: the user pressed Stop, so it stays
        stopped (see user_stopped)."""
        with self._lock:
            if user:
                self.user_stopped = True
            proc, self._proc = self._proc, None
            self._state = "stopped"
            self._fail_pending("The AI model was stopped.")
        if not proc or proc.poll() is not None:
            return
        try:
            proc.stdin.write('{"op": "quit"}\n')
            proc.stdin.flush()
            proc.wait(3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            _kill_tree(proc)

    def wait_ready(self, timeout: float = 300) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = self.status()
            if s in ("ready", "busy"):
                return True
            if s != "starting":
                return False
            time.sleep(0.2)
        return False

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                if proc is not self._proc:
                    continue
                event = msg.get("event")
                if event == "ready":
                    self._state = "ready"
                    self.load_s = msg.get("load_s")
                    self._vram_mb = msg.get("vram_mb")
                elif event == "error":
                    self._state = "error"
                    self.last_error = msg.get("error") or "The AI model failed to load."
                elif "id" in msg and msg["id"] in self._pending:
                    slot = self._pending[msg["id"]]
                    slot["resp"] = msg
                    slot["event"].set()
        proc.wait()
        with self._lock:
            if proc is self._proc:
                if self._state != "error":
                    self.last_error = _log_tail() or f"The AI model stopped unexpectedly (exit {proc.returncode})."
                self._state = "error"
                self._fail_pending(self.last_error)

    def _fail_pending(self, error: str) -> None:
        for slot in self._pending.values():
            slot["resp"] = {"ok": False, "error": error}
            slot["event"].set()

    # --- requests ----------------------------------------------------------

    def ask(self, image_bgr, phrases: list[str], mode: str = "fast", timeout: float = 60,
            max_side: int = 896, upscale: bool = False, seed: int = 0) -> list[dict]:
        """Find the phrases in the picture. Boxes are in pixels of image_bgr:
        {"label": phrase, "x1", "y1", "x2", "y2": float, "score": None}.
        upscale: a smaller picture (a crop around a far person) is enlarged to max_side (at most
        MAX_UPSCALE times): the model hallucinates on small inputs. seed: sampling seed, so a caller
        can take independent samples of one picture."""
        phrases = [p.strip() for p in phrases if p and p.strip()]
        if not phrases:
            return []
        if self.status() not in ("ready", "busy"):
            raise BrainError(self.last_error if self.status() == "error" and self.last_error
                             else "The AI model is not running.")
        import cv2
        h, w = image_bgr.shape[:2]
        scale = max_side / max(w, h)
        scale = min(scale, MAX_UPSCALE) if upscale else min(1.0, scale)
        small = image_bgr if abs(scale - 1.0) < 1e-3 else cv2.resize(
            image_bgr, (max(1, round(w * scale)), max(1, round(h * scale))),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise BrainError("Could not encode the picture.")
        rid = str(next(self._ids))
        path = WORK_DIR / f"ask_{os.getpid()}_{rid}.jpg"
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpg.tobytes())
        deadline = time.monotonic() + timeout
        try:
            if not self._ask_lock.acquire(timeout=timeout):
                raise BrainError("The AI model is busy (timed out waiting).")
            try:
                resp = self._request(rid, {"op": "ask", "image": str(path), "phrases": phrases, "mode": mode,
                                           "max_side": max_side, "seed": int(seed)},
                                     max(1.0, deadline - time.monotonic()))
            finally:
                self._ask_lock.release()
        finally:
            path.unlink(missing_ok=True)
        if not resp.get("ok"):
            raise BrainError(resp.get("error") or "The AI model could not answer.")
        with self._lock:
            self.last_answer = {"calls": resp.get("calls"), "raw": resp.get("raw", ""), "size": resp.get("size"),
                                "phrases": phrases, "seed": int(seed)}
            self._last_latency = resp.get("latency_s")
            if self._last_latency is not None:
                self._latencies.append(self._last_latency)
            self._vram_mb = resp.get("vram_mb", self._vram_mb)
        return [{"label": b["label"], "x1": b["x1"] * w, "y1": b["y1"] * h, "x2": b["x2"] * w,
                 "y2": b["y2"] * h, "score": b.get("score")} for b in resp.get("boxes", [])]

    def _request(self, rid: str, req: dict, timeout: float) -> dict:
        slot = {"event": threading.Event(), "resp": None}
        with self._lock:
            proc = self._proc
            if not proc or proc.poll() is not None or self._state not in ("ready", "busy"):
                raise BrainError(self.last_error or "The AI model is not running.")
            self._pending[rid] = slot
            self._state = "busy"
            try:
                proc.stdin.write(json.dumps({"id": rid, **req}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                self._pending.pop(rid, None)
                raise BrainError(f"The AI model stopped: {e}") from e
        try:
            if not slot["event"].wait(timeout):
                raise BrainError(f"The AI model did not answer within {timeout:.0f} s.")
            return slot["resp"]
        finally:
            with self._lock:
                self._pending.pop(rid, None)
                if self._state == "busy" and not self._pending:
                    self._state = "ready"


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=NO_WINDOW, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
        proc.wait(5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _log_tail(lines: int = 4) -> str:
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    useful = [ln for ln in text[-40:] if ln.strip() and "it/s]" not in ln]
    return "\n".join(useful[-lines:])


def uninstall() -> None:
    """Remove the model environment and model files (frees about 12 GB)."""
    shutil.rmtree(BRAIN_VENV, ignore_errors=True)
    shutil.rmtree(MODEL_DIR, ignore_errors=True)
