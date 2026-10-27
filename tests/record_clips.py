"""Record short clips straight from the cameras (stream copy, no re-encoding) for offline tests.

Usage:  run.cmd tests\\record_clips.py [seconds] [channels...]     default: 15 s of channels 1 and 5
Output: .cache/tests/night/ch<N>.mp4, or the folder in the CLIP_DIR environment variable
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yardguard.config import load_config
from yardguard.stream import ffmpeg_path

seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 15
channels = [int(c) for c in sys.argv[2:]] or [1, 5]
out = Path(os.environ.get("CLIP_DIR") or ROOT / ".cache" / "tests" / "night")
out.mkdir(parents=True, exist_ok=True)
site = load_config().active_site()
procs = []
for ch in channels:
    dest = out / f"ch{ch}.mp4"
    cmd = [ffmpeg_path(), "-y", "-v", "error", "-rtsp_transport", "tcp", "-i", site.rtsp_url(ch, main=True),
           "-t", str(seconds), "-an", "-c:v", "copy", str(dest)]
    procs.append((ch, dest, subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)))
for ch, dest, p in procs:
    p.wait(timeout=seconds + 60)
    size = dest.stat().st_size / 2**20 if dest.exists() else 0
    print(f"ch{ch}: exit {p.returncode}, {size:.1f} MB -> {dest}")
