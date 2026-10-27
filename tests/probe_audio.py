"""Which cameras send sound? Probes every channel's main stream for an audio track.

Usage:  run.cmd tests\\probe_audio.py
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yardguard.config import load_config
from yardguard.stream import ffmpeg_path

site = load_config().active_site()
ffprobe = str(Path(ffmpeg_path()).with_name("ffprobe.exe"))
for cam in site.cameras:
    r = subprocess.run([ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000", "-show_entries",
                        "stream=codec_type,codec_name,sample_rate,channels", "-of", "json",
                        site.rtsp_url(cam.channel, main=True)],
                       capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except ValueError:
        streams = []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not streams:
        print(f"ch{cam.channel} {cam.label:20} no answer")
    elif audio:
        a = audio[0]
        print(f"ch{cam.channel} {cam.label:20} SOUND: {a.get('codec_name')} {a.get('sample_rate')} Hz, "
              f"{a.get('channels')} channel(s)")
    else:
        print(f"ch{cam.channel} {cam.label:20} no sound (video {video[0].get('codec_name') if video else '?'})")
