"""YardGuard.exe: starts the desktop app with the project's own Python (no console window).

Built by build.py. The exe must sit in the project folder, next to .venv and app.py.
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
APP = ROOT / "app.py"


def main() -> None:
    setup = ROOT / "YardGuard.cmd"
    if not PYTHONW.exists() and setup.exists():  # not installed yet: YardGuard.cmd sets everything up
        subprocess.Popen(["cmd.exe", "/c", str(setup)], cwd=ROOT)
        return
    if not PYTHONW.exists() or not APP.exists():
        ctypes.windll.user32.MessageBoxW(
            None, f"YardGuard files were not found next to this program:\n{ROOT}\n\n"
                  "YardGuard.exe must stay inside the YardGuard folder.", "YardGuard", 0x10)
        return
    cache = ROOT / ".cache"
    env = dict(os.environ, YOLO_CONFIG_DIR=str(cache / "ultralytics"), TORCH_HOME=str(cache / "torch"),
               PIP_CACHE_DIR=str(cache / "pip"), TEMP=str(cache), TMP=str(cache))
    subprocess.Popen([str(PYTHONW), str(APP)], cwd=ROOT, env=env)


if __name__ == "__main__":
    main()
