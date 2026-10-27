"""Variant benchmark: decode with D3D11VA (NVDEC through DirectX, no CUDA context) instead of CUDA."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yardguard.stream as st
import bench_stream

orig = st.FrameSource._command
def cmd(self, gpu):
    c = orig(self, False)
    if gpu:
        i = c.index("-threads"); c[i:i+2] = ["-hwaccel", "d3d11va"]
    return c
st.FrameSource._command = cmd
r = bench_stream.run("gpu", float(sys.argv[1]) if len(sys.argv) > 1 else 30, 5, 1280)
print(json.dumps(r, indent=1))
