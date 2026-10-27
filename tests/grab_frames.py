"""Save one raw (unannotated) full-size frame per test camera to .cache/tests/frames/."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2
from yardguard.config import load_config
from yardguard.stream import FrameSource
import time

out = Path(__file__).resolve().parent.parent / ".cache" / "tests" / "frames"
out.mkdir(parents=True, exist_ok=True)
site = load_config().active_site()
srcs = {ch: FrameSource(site.rtsp_url(ch, main=True), 2, 0, gpu_decode=False) for ch in (1, 2, 5, 7)}
for s in srcs.values(): s.start()
t = time.time()
while time.time() - t < 20 and any(s.frames < 3 for s in srcs.values()):
    time.sleep(0.2)
for ch, s in srcs.items():
    f = s.latest()
    if f is not None:
        (out / f"ch{ch}.png").write_bytes(cv2.imencode(".png", f[0])[1].tobytes())
        print(ch, f[0].shape)
    s.stop()
