"""Console live view of one camera with YOLO boxes (the app's Live view does the same).

Keys in the video window:  q = quit,  s = save snapshot
Usage:
    run.cmd scripts\\test_camera.py --channel 5     # camera of the active site
    run.cmd scripts\\test_camera.py --source x.mp4  # test on a video file
    run.cmd scripts\\test_camera.py --no-show       # headless, prints detections
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from yardguard.config import load_config, masked_url
from yardguard.video import Detector, open_capture


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="RTSP URL or video file (default: active site in config.json)")
    ap.add_argument("--channel", type=int, help="channel of the active site (default: first enabled camera)")
    ap.add_argument("--no-show", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    s = cfg.settings
    source = args.source
    if not source:
        site = cfg.active_site()
        if site is None:
            raise SystemExit("No site set up yet. Open YardGuard.exe and add one.")
        channel = args.channel or (site.enabled_cameras() or site.cameras)[0].channel
        source = site.rtsp_url(channel, main=s.stream == "main")
    snaps = Path(s.events_dir) / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    detector = Detector()
    print(f"Opening {masked_url(source)}")
    cap = open_capture(source)
    fps, last = 0.0, time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            print("No frame, reconnecting in 3 s ...")
            cap.release()
            time.sleep(3)
            cap = open_capture(source)
            continue
        view, counts = detector.detect(frame, s.model, s.confidence)
        now = time.time()
        fps = 0.9 * fps + 0.1 / max(now - last, 1e-6)
        last = now
        summary = ", ".join(f"{n} {k}" for k, n in counts.items()) or "nothing"
        if args.no_show:
            print(f"{datetime.now():%H:%M:%S}  {fps:5.1f} fps  {summary}")
            continue
        cv2.putText(view, f"{fps:.1f} fps | {summary}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        cv2.imshow("YardGuard (q = quit, s = snapshot)", view)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("s"):
            path = snaps / f"snap_{datetime.now():%Y%m%d_%H%M%S}.jpg"
            path.write_bytes(cv2.imencode(".jpg", view)[1].tobytes())
            print(f"Saved {path}")
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
