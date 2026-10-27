"""Uninstall.exe / Uninstall.cmd: removes everything YardGuard put on this PC.

Removes:  the YardGuard folder this file is in (app, its own Python in runtime\\, ffmpeg in
          tools\\, .venv, .venv-brain, models, cache, config), the snapshots/alerts folder set
          in config.json (and its parent if that is a "YardGuard" folder), desktop shortcuts
          pointing to YardGuard.
Keeps:    a Python or ffmpeg installed separately on the PC, NVR recordings.

Built with:  run.cmd -m PyInstaller --onefile --name Uninstall uninstall.py
Usage:       Uninstall.exe [--dry-run]   or   Uninstall.cmd [--dry-run]
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
DESKTOP = Path(os.environ.get("USERPROFILE", "")) / "Desktop"


def events_dirs() -> list[Path]:
    """The snapshots/alerts folder from config.json, outside the app folder."""
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        events = Path(cfg["settings"]["events_dir"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    target = events.parent if events.parent.name.lower() == "yardguard" else events
    if not target.exists() or ROOT in target.resolve().parents or target.resolve() == ROOT:
        return []  # inside the app folder: removed with it anyway
    return [target]


def size_gb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total / 1024**3


def shortcuts() -> list[Path]:
    if not DESKTOP.exists():
        return []
    return [p for p in DESKTOP.glob("*.lnk") if "yardguard" in p.name.lower()]


def remove_dir(path: Path, dry_run: bool) -> bool:
    if dry_run:
        print(f"  [dry run] would remove {path}")
        return True
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        print(f"  ! could not fully remove {path} (close YardGuard windows and run again)")
        return False
    print(f"  removed {path}")
    return True


def remove_root(dry_run: bool) -> None:
    """Delete the app folder; this exe lives in it, so finish after the exe exits."""
    me = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
    for child in ROOT.iterdir():
        if child == me:
            continue
        if dry_run:
            print(f"  [dry run] would remove {child}")
        elif child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                print(f"  ! could not remove {child}")
    if dry_run:
        print(f"  [dry run] would remove {ROOT} itself")
        return
    # Wait for this program (and the Python running it) to exit, then delete the rest of the folder
    root = str(ROOT).replace("'", "''")
    script = (f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
              f"for ($i = 0; $i -lt 10 -and (Test-Path -LiteralPath '{root}'); $i++) {{ "
              f"Start-Sleep -Seconds 1; Remove-Item -LiteralPath '{root}' -Recurse -Force "
              f"-ErrorAction SilentlyContinue }}")
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=ROOT.anchor, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    print(f"  removing {ROOT} ...")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    os.system("title YardGuard Uninstall")

    # The folder may be "YardGuard", "YardGuard-main" (Download ZIP) or renamed: check its contents
    if not ((ROOT / "app.py").exists() and (ROOT / "yardguard" / "config.py").exists()):
        print(f"Refusing to run: {ROOT} does not look like a YardGuard folder (no app.py / yardguard).")
        input("Press Enter to close...")
        return

    extra = events_dirs()
    links = shortcuts()

    print("=== YardGuard Uninstall ===\n")
    print("This will permanently delete:")
    print(f"  {ROOT}  ({size_gb(ROOT):.2f} GB)  app, its Python and ffmpeg, AI libraries and models, "
          "cache, login settings")
    for d in extra:
        print(f"  {d}  ({size_gb(d):.2f} GB)")
    for link in links:
        print(f"  {link}")
    print("\nKept: any Python or ffmpeg installed separately on this PC, all NVR recordings.")
    if dry_run:
        print("\nDRY RUN: nothing will be deleted.")

    try:
        answer = input('\nType YES to uninstall: ').strip().lstrip("\ufeff")
    except EOFError:
        answer = ""
    if answer != "YES":
        print("Cancelled, nothing was deleted.")
        if not dry_run:
            input("Press Enter to close...")
        return

    for d in extra:
        remove_dir(d, dry_run)
    for link in links:
        if dry_run:
            print(f"  [dry run] would remove {link}")
        else:
            link.unlink(missing_ok=True)
            print(f"  removed {link}")
    remove_root(dry_run)

    print("\nDone." if not dry_run else "\nDry run finished.")
    if not dry_run:
        input("Press Enter to close...")


if __name__ == "__main__":
    main()
