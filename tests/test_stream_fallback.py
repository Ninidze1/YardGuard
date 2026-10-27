"""GPU path that opens the input but cannot produce frames must switch to CPU decode."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yardguard.stream as st

orig = st.FrameSource._command
def broken(self, gpu):
    c = orig(self, gpu)
    if gpu:
        i = c.index("-vf"); c[i + 1] = c[i + 1].replace("format=nv12", "format=bgr0", 1)  # unsupported by scale_cuda
    return c
st.FrameSource._command = broken
clip = Path(__file__).resolve().parent.parent / ".cache" / "tests" / "stream" / "clip.mp4"
from local_data import need, skip
need(clip, what="the test clip made by tests/test_stream.py")
if not st.ffmpeg_path():
    skip("ffmpeg not installed")
s = st.FrameSource(str(clip), 5, 1280, gpu_decode=True); s.start()
t = time.time()
while time.time() - t < 20 and s.frames < 5: time.sleep(0.1)
print("backend", s.backend, "frames", s.frames, "took", round(time.time() - t, 1), "s")
assert s.backend == "cpu" and s.frames >= 5
s.stop(); print("OK")
