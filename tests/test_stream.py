"""Unit checks for yardguard.stream.FrameSource without cameras: file source, bad address, clean stop."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2, numpy as np, psutil
from yardguard.stream import FrameSource, ffmpeg_path

out = Path(__file__).resolve().parent.parent / ".cache" / "tests" / "stream"
out.mkdir(parents=True, exist_ok=True)
clip = out / "clip.mp4"
if not clip.exists():
    vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (1920, 1080))
    for i in range(45):
        f = np.zeros((1080, 1920, 3), np.uint8); cv2.circle(f, (40 * i, 540), 80, (0, 200, 255), -1); vw.write(f)
    vw.release()

def kids():
    return [p for p in psutil.Process().children(recursive=True) if p.name().lower().startswith("ffmpeg")]

print("ffmpeg:", bool(ffmpeg_path()))
for gpu in (True, False):
    s = FrameSource(str(clip), 5, 1280, gpu_decode=gpu); s.start()
    t = time.time()
    while time.time() - t < 15 and s.frames < 15: time.sleep(0.1)
    f = s.latest()
    print(f"file gpu={gpu}: backend={s.backend} size={s.size} frames={s.frames} fps={s.fps_actual:.1f} "
          f"shape={None if f is None else f[0].shape} alive={s.alive}")
    assert f is not None and f[0].shape == (720, 1280, 3)
    s.stop(); time.sleep(0.3); assert not kids(), "ffmpeg left running"

s = FrameSource("rtsp://user:secret@192.0.2.254:554/none", 5); s.start()
time.sleep(14)
print("bad address:", s.error, "| restarts", s.restarts, "| alive", s.alive)
assert s.latest() is None and s.error and "secret" not in s.error
t = time.time(); s.stop(); print("stop took", round(time.time() - t, 2), "s"); time.sleep(0.3)
assert not kids()
print("OK")
