"""Grab one frame per channel of the active site, run detection and save snapshots.

Usage:  run.cmd scripts\\check_stream.py [--channel N]   (default: every camera of the active site)
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from yardguard.config import load_config, masked_url
from yardguard.video import Detector, grab_frame, is_no_signal


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int)
    args = ap.parse_args()

    cfg = load_config()
    site = cfg.active_site()
    if site is None:
        raise SystemExit("No site set up yet. Open YardGuard.exe and add one.")
    s = cfg.settings
    channels = [args.channel] if args.channel else [c.channel for c in site.cameras]
    out = Path(s.events_dir) / "snapshots"
    out.mkdir(parents=True, exist_ok=True)
    detector = Detector()

    for ch in channels:
        url = site.rtsp_url(ch, main=s.stream == "main")
        frame = grab_frame(url)
        if frame is None:
            print(f"CH {ch}: FAILED, no video ({masked_url(url)})")
            continue
        h, w = frame.shape[:2]
        if is_no_signal(frame):
            print(f"CH {ch}: {w}x{h}, no signal (black picture)")
            continue
        view, counts = detector.detect(frame, s.model, s.confidence)
        path = out / f"check_ch{ch}_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        import cv2
        path.write_bytes(cv2.imencode(".jpg", view)[1].tobytes())
        found = ", ".join(f"{n} {k}" for k, n in counts.items()) or "nothing"
        print(f"CH {ch}: OK {w}x{h}, detected: {found}  -> {path}")


if __name__ == "__main__":
    main()
