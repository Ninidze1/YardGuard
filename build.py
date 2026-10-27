"""Build YardGuard.exe and Uninstall.exe (with the app icon).

Usage:  run.cmd build.py
"""
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication

from yardguard.theme import logo_pixmap

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".cache" / "build"
ICON = BUILD / "yardguard.ico"


def make_icon() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    logo_pixmap(256, 1.0).save(buf, "PNG")
    png = BUILD / "yardguard_256.png"
    png.write_bytes(bytes(buf.data()))
    Image.open(png).save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    del app


def pyinstaller(script: str, name: str, windowed: bool) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--name", name,
           "--icon", str(ICON), "--distpath", str(ROOT), "--workpath", str(BUILD), "--specpath", str(BUILD),
           "--exclude-module", "torch", "--exclude-module", "cv2", "--exclude-module", "numpy",
           "--exclude-module", "PySide6", "--exclude-module", "PIL",
           "--windowed" if windowed else "--console", script]
    subprocess.run(cmd, cwd=ROOT, check=True)


if __name__ == "__main__":
    make_icon()
    pyinstaller("launcher.py", "YardGuard", windowed=True)
    pyinstaller("uninstall.py", "Uninstall", windowed=False)
    print("Built YardGuard.exe and Uninstall.exe")
