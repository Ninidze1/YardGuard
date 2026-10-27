"""Plate reader tests: format cleanup, best-of-N voting, readability score and deskew (no models needed),
then, when the models are on disk, synthetic plates and an evaluation table on real labelled camera crops.

Usage:  run.cmd tests\\test_plates.py            (unit checks + model checks if models/plates/ exists)
        run.cmd tests\\test_plates.py --unit     (unit checks only)
Real crops: .cache/tests/plates/eval/<vehicle>/*.jpg (several crops of one vehicle, from tests\\plates_survey.py)
with labels.json {"<vehicle>": "AA-123-BB" or "" when a person cannot read it either}.
Uses the GPU only when it is nearly free (the app's AI brain may be using it), otherwise the CPU.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from yardguard import plates
from yardguard.plates import clean_plate, deskew, plate_quality, vote

failures = 0


def check(name: str, ok: bool) -> None:
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name)


def synthetic_plate(text: str, height: int = 60, angle: float = 0.0, blur: int = 0) -> np.ndarray:
    """White Georgian-style plate with a blue band, on a grey car-like background."""
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, height / 40, max(2, height // 20))
    plate = np.full((th + base + height // 3, tw + height // 2 + 12, 3), 235, np.uint8)
    plate[:, :height // 4] = (160, 60, 20)
    cv2.rectangle(plate, (0, 0), (plate.shape[1] - 1, plate.shape[0] - 1), (30, 30, 30), 2)
    cv2.putText(plate, text, (height // 4 + 6, th + height // 6), cv2.FONT_HERSHEY_SIMPLEX, height / 40,
                (20, 20, 20), max(2, height // 20), cv2.LINE_AA)
    ph, pw = plate.shape[:2]
    canvas = np.full((ph * 3, pw + ph * 2, 3), 120, np.uint8)
    y, x = ph, ph
    canvas[y:y + ph, x:x + pw] = plate
    if angle:
        rot = cv2.getRotationMatrix2D((canvas.shape[1] / 2, canvas.shape[0] / 2), angle, 1.0)
        canvas = cv2.warpAffine(canvas, rot, canvas.shape[1::-1], borderValue=(120, 120, 120))
    if blur:
        canvas = cv2.GaussianBlur(canvas, (blur | 1, blur | 1), 0)
    return canvas


# ------------------------------------------------------------------ 1) format cleanup
check("clean: Georgian current format gets dashes", clean_plate("kk318mk") == ("KK-318-MK", True))
check("clean: dashes / dots / spaces ignored", clean_plate("KK-318.MK ") == ("KK-318-MK", True))
check("clean: 0 in a letter slot becomes O", clean_plate("T0085VT")[0] == "TO-085-VT")
check("clean: O and B in digit slots become 0 and 8", clean_plate("TTO8BVT")[0] == "TT-088-VT")
check("clean: GE country code is dropped", clean_plate("GEKK318MK") == ("KK-318-MK", True))
check("clean: old format ABC-123", clean_plate("abc123") == ("ABC-123", True))
check("clean: unknown layout kept as read, not forced", clean_plate("W123ABC9") == ("W123ABC9", False))
check("clean: too many look-alike fixes is not forced", clean_plate("0085S55")[1] is False)
check("clean: digit-heavy misread of a real plate still fits (00-512-20)", clean_plate("00-512-20") == ("OO-512-ZO", True))
check("clean: symbols only -> empty", clean_plate("-.-") == ("", False))
check("clean: doubled stroke dropped", clean_plate("PR5555HA") == ("PR-555-HA", True))
check("clean: stray flag-band letter dropped", clean_plate("GRW459SX") == ("RW-459-SX", True))
check("clean: 8-char foreign plate not squeezed into the Georgian layout", clean_plate("AB1234CD") == ("AB1234CD", False))

check("plausible: known layout", plates.plausible("KK-318-MK"))
check("plausible: foreign mix of letters and digits", plates.plausible("W123ABC"))
check("plausible: dictionary words are not plates", not any(map(plates.plausible, ["GREEN", "EXPRESS", "CAFE"])))
check("plausible: clock digits / short junk are not plates",
      not any(map(plates.plausible, ["23122935", "CAM5", "1338", "AB1"])))

# ------------------------------------------------------------------ 2) voting
check("vote: nothing -> None", vote([]) is None and vote([("", 0.9)]) is None)
text, conf = vote([("KK-318-MK", 0.9)])
check("vote: single reading keeps its confidence", text == "KK-318-MK" and abs(conf - 0.9) < 1e-6)
text, conf = vote([("KK-318-MK", 0.8), ("KK-318-MK", 0.7), ("KK-319-MK", 0.6)])
check("vote: majority wins", text == "KK-318-MK")
check("vote: disagreement lowers confidence", conf < 0.8)
text, _ = vote([("KK-318-MK", 0.5), ("KK-318-MI", 0.5), ("KI-318-MK", 0.5)])
check("vote: character-wise consensus fixes single slips", text == "KK-318-MK")
text, _ = vote([("KK-318-MK", 0.6), ("KK318M", 0.9)])
check("vote: known-format length preferred over a partial read", text == "KK-318-MK")
text, conf = vote([("AB-123-CD", 0.95), ("XY-987-ZW", 0.1)])
check("vote: confident reading beats a weak different one", text == "AB-123-CD" and conf >= plates.CONFIDENT)
_, agreed = vote([("PQ-512-ZO", 0.52), ("PQ-512-ZO", 0.55)])
_, contested = vote([("PQ-512-ZO", 0.52), ("PQ-512-ZO", 0.55), ("OO-512-ZO", 0.34), ("PU-512-ZO", 0.21)])
check("vote: frames that keep disagreeing about look-alikes make it unsure (real D/B/Q plate)",
      agreed >= plates.CONFIDENT > contested)
text, conf = vote([("LF617LFLF", 0.63), ("LF-617-LF", 0.49), ("LF-617-LF", 0.59)])
check("vote: a raw read of the same length does not vote per character", text == "LF-617-LF" and conf >= 0.3)

# ------------------------------------------------------------------ 3) readability score
sharp = synthetic_plate("KK-318-MK", 60)
check("quality: in 0..1", 0 <= plate_quality(sharp) <= 1)
check("quality: blurred < sharp", plate_quality(cv2.GaussianBlur(sharp, (9, 9), 0)) < plate_quality(sharp))
check("quality: small < large", plate_quality(cv2.resize(sharp, None, fx=0.3, fy=0.3)) < plate_quality(sharp))
check("quality: flat image ~ 0", plate_quality(np.full((200, 300, 3), 128, np.uint8)) < 0.05)
check("quality: None / tiny -> 0", plate_quality(None) == 0 and plate_quality(np.zeros((4, 4, 3), np.uint8)) == 0)

# ------------------------------------------------------------------ 3b) plate score: which moment shows the plate best
from yardguard.plates import fuse_plates, plate_score  # noqa: E402

cut = synthetic_plate("KK-318-MK", 40)[40:80, 40:-40]  # the plate alone, ~4.5:1
h0, w0 = cut.shape[:2]
s0 = plate_score(cut, 0.9)
check("score: in 0..1, > 0 for a plate", 0 < s0 < 1)
check("score: wider plate (closer car) scores higher",
      plate_score(cv2.resize(cut, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 0.9) > s0)
check("score: blurred (motion) scores lower", plate_score(cv2.GaussianBlur(cut, (7, 7), 0), 0.9) < s0)
check("score: seen from the side (squeezed to 2:1) scores lower",
      plate_score(cv2.resize(cut, (h0 * 2, h0)), 0.9, h0 * 2, h0) < plate_score(cut, 0.9, w0, h0))
check("score: detector doubt lowers it", plate_score(cut, 0.3) < s0)
check("score: flat grey ~ 0", plate_score(np.full_like(cut, 128), 0.9) < 0.01)
check("score: None -> 0", plate_score(None) == 0)

# ------------------------------------------------------------------ 3c) fusion: aligned average of the best plates
rng = np.random.default_rng(1)
clean = cv2.resize(cut, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)


def shot(dx: float, dy: float, scale: float = 1.0) -> np.ndarray:
    """The plate shifted / slightly zoomed, with heavy sensor noise (night grain)."""
    m = np.float32([[scale, 0, dx], [0, scale, dy]])
    moved = cv2.warpAffine(clean, m, clean.shape[1::-1], borderMode=cv2.BORDER_REPLICATE)
    return np.clip(moved + rng.normal(0, 28, moved.shape), 0, 255).astype(np.uint8)


shots = [shot(0, 0), shot(2.5, -1.5, 1.03), shot(-2, 1, 0.98)]
fused = fuse_plates(shots)
check("fuse: same size as the first picture", fused is not None and fused.shape == shots[0].shape)
err = lambda img: float(np.abs(img.astype(np.float32) - clean.astype(np.float32)).mean())  # noqa: E731
check(f"fuse: less noise than any single picture ({err(fused):.1f} < {min(map(err, shots)):.1f})",
      fused is not None and err(fused) < 0.8 * min(map(err, shots)))
check("fuse: a picture that does not align is left out",
      fuse_plates([shots[0], rng.integers(0, 255, shots[0].shape, dtype=np.uint8)]) is None)
check("fuse: one picture -> None", fuse_plates(shots[:1]) is None and fuse_plates([]) is None)

# ------------------------------------------------------------------ 4) deskew
for angle in (0, 12, -18):
    img = synthetic_plate("AB-123-CD", 50, angle)
    d = deskew(img)
    ok = d is not None and 2.0 <= d.shape[1] / d.shape[0] <= 9.0
    check(f"deskew {angle:+d} deg: level, plate-shaped output", ok)
check("deskew: blank -> None", deskew(np.full((60, 120, 3), 128, np.uint8)) is None)

if "--unit" in sys.argv or not plates.models_ready():
    if not plates.models_ready():
        print("SKIP model checks: plate models not downloaded (models/plates/)")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


# ------------------------------------------------------------------ 5) models: synthetic plates
def gpu_free() -> bool:
    try:
        used = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=10).stdout.split()[0]
        return int(used) < 3000
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return False


device = "cuda" if gpu_free() else "cpu"
reader = plates.PlateReader(device=device)
t = time.time()
reader.load()
print(f"models loaded on {reader.device} in {time.time() - t:.1f} s")

samples = {"AB-123-CD": 0, "KK-318-MK": 10, "GG-777-RR": -15, "ZX-404-QA": 20}
ok = 0
for text, angle in samples.items():
    got = reader.recognise([synthetic_plate(text, 48, angle)])  # recognise() also tries it deskewed
    got_clean = clean_plate(got[0][0])[0]
    ok += got_clean == text
    print(f"   synthetic {text} at {angle:+d} deg -> {got_clean} ({got[0][1]:.2f})")
check("synthetic plates read (>= 3 of 4)", ok >= 3)

car = np.full((360, 520, 3), 90, np.uint8)
plate = synthetic_plate("KK-318-MK", 34)[34:68, 34:-34]
car[250:250 + plate.shape[0], 140:140 + plate.shape[1]] = plate
r = reader.read([car, cv2.GaussianBlur(car, (3, 3), 0)])
check("read: returns the contract dict", r is None or {"text", "confidence", "plate_crop"} <= set(r))
check("read: no vehicles -> None", reader.read([]) is None)
check("read: blank crops -> None", reader.read([np.full((200, 300, 3), 128, np.uint8)]) is None)
t = time.time()
for _ in range(3):
    reader.read([car] * 5)
print(f"   read() of 5 crops: {(time.time() - t) / 3 * 1000:.0f} ms on {reader.device}")

# ------------------------------------------------------------------ 6) real labelled crops: evaluation table
EVAL = ROOT / ".cache" / "tests" / "plates" / "eval"
labels_file = EVAL / "labels.json"
if labels_file.exists():
    labels = json.loads(labels_file.read_text(encoding="utf-8"))
    print(f"\n{'vehicle':12} {'crops':>5} {'plate px':>8} {'truth':>11} {'read':>11} {'conf':>5}  result")
    right = wrong = missed = quiet = 0
    for name, truth in labels.items():
        crops = [cv2.imread(str(p)) for p in sorted((EVAL / name).glob("*.jpg"))]
        if not crops:
            continue
        found = reader.find_plates(crops)
        res = reader.read(crops)
        text, conf = (res["text"], res["confidence"]) if res else ("", 0.0)
        accepted = conf >= plates.CONFIDENT
        if truth:
            outcome = "right" if accepted and text == truth else ("WRONG" if accepted else "missed")
        else:
            outcome = "false read" if accepted else "ok (unreadable)"
        right += outcome == "right"
        wrong += outcome in ("WRONG", "false read")
        missed += outcome == "missed"
        quiet += outcome == "ok (unreadable)"
        px = max((f[0].shape[1] for f in found), default=0)
        print(f"{name:12} {len(crops):5} {px:8} {truth or '-':>11} {text or '-':>11} {conf:5.2f}  {outcome}")
    readable = sum(1 for v in labels.values() if v)
    print(f"readable plates {readable}: right {right}, wrong {wrong}, missed {missed}; unreadable kept quiet {quiet}")
    check("real crops: no wrong plate accepted on more than 1 in 10", wrong <= max(1, len(labels) // 10))
else:
    print(f"SKIP real crops: no {labels_file}")

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
