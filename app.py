"""YardGuard desktop app. Started by YardGuard.cmd (or YardGuard.exe, or: .venv\\Scripts\\pythonw.exe app.py)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
# Keep every cache inside the app folder, nothing in the user profile
os.environ.setdefault("YOLO_CONFIG_DIR", str(CACHE / "ultralytics"))
os.environ.setdefault("MPLCONFIGDIR", str(CACHE / "matplotlib"))
os.environ.setdefault("TORCH_HOME", str(CACHE / "torch"))
os.environ.setdefault("PIP_CACHE_DIR", str(CACHE / "pip"))
os.environ.setdefault("CUDA_CACHE_PATH", str(CACHE / "cuda"))
os.environ.setdefault("YOLO_AUTOINSTALL", "false")  # never pip-install packages at runtime


def smart_app_control_blocks() -> bool:
    """Smart App Control blocks the AI libraries, so YardGuard refuses to start while it is on."""
    from yardguard.brain import smart_app_control_on
    if not smart_app_control_on():
        return False
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        None, "Windows Smart App Control is turned on, and it blocks the AI libraries YardGuard runs on.\n\n"
              "Open Windows Security > App & browser control > Smart App Control settings, choose Off, "
              "then start YardGuard again.", "YardGuard", 0x10)
    return True


if __name__ == "__main__":
    if smart_app_control_blocks():
        sys.exit(1)
    from yardguard.gui import run
    sys.exit(run())
