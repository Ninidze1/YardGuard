"""License plate reading for areas (task log_plates): find the plate on vehicle crops, read it, vote.

A vehicle brings up to MAX_CROPS pictures (its best moments near the line, areas.py). The plate is
looked for on all of them at once, every plate found is scored on its own (plate_score: width,
sharpness, shape, detector score, contrast), the best few are read and voted, and the best three are
also averaged into one cleaner picture (fuse_plates) that votes too.

Two small models, both plain PyTorch (no onnx, no native extras):
  - plate detector: YOLO11 fine-tuned on licence plates (morsetechlab, AGPL-3.0 like ultralytics itself)
  - text reader: PARSeq scene-text recogniser (Darwin Bautista, Apache-2.0), re-implemented below so the
    official weights load without timm / pytorch-lightning.
Models live in models/plates/ and download (resumable) on first use or from Settings.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from .config import ROOT

PLATES_DIR = ROOT / "models" / "plates"
DETECTOR = {"file": "license-plate-finetune-v1s.pt", "size": 19_173_715, "sha256": "95e50c25ab7066dd",
            "url": "https://huggingface.co/morsetechlab/yolov11-license-plate-detection/resolve/main/"
                   "license-plate-finetune-v1s.pt"}
READER = {"file": "parseq-bb5792a6.pt", "size": 95_392_675, "sha256": "bb5792a6",
          "url": "https://github.com/baudm/parseq/releases/download/v1.0.0/parseq-bb5792a6.pt"}
DOWNLOAD_MB = round((DETECTOR["size"] + READER["size"]) / 2**20)

CHARSET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
MIN_PLATE_CONF = 0.25  # detector score
MAX_CROPS = 12  # vehicle pictures searched for the plate (one detector batch, areas.PLATE_CANDIDATES)...
READ_TOP = 5    # ...the plates with the best plate_score are read...
FUSE_TOP = 3    # ...and the best few of them averaged into one more reading
FUSE_WEIGHT = 0.5  # the fused plate's vote, as a share of an ordinary reading's (it is made of the same
                   # frames, so it is no independent witness: at 1.0 it carried a wrong D/B/Q read over 0.3)
FUSE_H = 48     # px: fused plates are aligned at least this tall
FUSE_MIN_CC = 0.6  # alignment correlation below this: not the same view of the plate
PLATE_ASPECT = 4.5  # width : height of a plate seen from the front (EU / Georgian)
SCORE_H = 32    # plate_score: sharpness measured at this text height...
SCORE_SHARP = 300.0  # ...Laplacian variance that scores half
SCORE_PX = 80.0  # plate width that scores half (legible from ~85 px)
CONFIDENT = 0.3  # readings at or above this were right on the real-crop test set; below: show with a "?"
MIN_PLATE_PX = 60  # narrower plates were never readable on these cameras (legible from ~85 px)
MAX_PLATE_SHARE = 0.6  # of the vehicle crop width
GPU_MIN_FREE = 1500 * 2**20  # bytes of free VRAM needed before the reader goes on the GPU


# ---------------------------------------------------------------- download

def model_file(spec: dict) -> Path:
    return PLATES_DIR / spec["file"]


def models_ready() -> bool:
    return all(model_file(s).exists() and model_file(s).stat().st_size == s["size"] for s in (DETECTOR, READER))


def download_state() -> str:
    """"ready", "partial" (a paused / interrupted download can resume) or "missing" - for Settings."""
    if models_ready():
        return "ready"
    partial = any(model_file(s).with_suffix(".download").exists() or model_file(s).exists()
                  for s in (DETECTOR, READER))
    return "partial" if partial else "missing"


class Download:
    """Resumable download of both plate models (data goes to *.download, renamed when complete and
    checked). pause() keeps the partial file, cancel() deletes it; run() returns done/paused/cancelled."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._stop: str | None = None

    @property
    def stopped(self) -> bool:
        """The user paused or cancelled the last download (and has not resumed it)."""
        return self._stop in ("paused", "cancelled") and not self.lock.locked()

    def pause(self) -> None:
        self._stop = "paused"

    def cancel(self) -> None:
        self._stop = "cancelled"
        if not self.lock.locked():
            for spec in (DETECTOR, READER):
                model_file(spec).with_suffix(".download").unlink(missing_ok=True)

    def run(self, progress=None) -> str:
        """progress(done_bytes, total_bytes) covers both files together."""
        with self.lock:
            self._stop = None
            total = DETECTOR["size"] + READER["size"]
            base = 0
            for spec in (DETECTOR, READER):
                result = self._one(spec, lambda d: progress and progress(base + d, total))
                if result != "done":
                    return result
                base += spec["size"]
            return "done"

    def _one(self, spec: dict, progress) -> str:
        path = model_file(spec)
        if path.exists() and path.stat().st_size == spec["size"]:
            return "done"
        tmp = path.with_suffix(".download")
        path.parent.mkdir(parents=True, exist_ok=True)
        done = tmp.stat().st_size if tmp.exists() else 0
        if done < spec["size"]:
            done = self._fetch(spec, tmp, done, progress)
            if self._stop == "cancelled":
                tmp.unlink(missing_ok=True)
                return "cancelled"
            if self._stop == "paused":
                return "paused"
        if done != spec["size"]:
            tmp.unlink(missing_ok=True)  # oversized or the server changed the file: start over next time
            raise ConnectionError("Plate model download was incomplete. Try again.")
        _check_hash(spec, tmp)
        tmp.replace(path)
        return "done"

    def _fetch(self, spec: dict, tmp: Path, done: int, progress) -> int:
        headers = {"User-Agent": "YardGuard"}
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            resp = urllib.request.urlopen(urllib.request.Request(spec["url"], headers=headers), timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and done:
                return done
            raise ConnectionError(f"Plate model download failed (server error {exc.code}).") from exc
        except OSError as exc:
            raise ConnectionError(f"Plate model download failed. Check the internet connection. ({exc})") from exc
        with resp:
            if done and resp.status != 206:  # server ignored the resume request
                done = 0
            try:
                with open(tmp, "ab" if done else "wb") as f:
                    while chunk := resp.read(256 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        progress(done)
                        if self._stop:
                            break
            except OSError as exc:
                raise ConnectionError(f"Download interrupted. Resume to continue. ({exc})") from exc
        return done


def _check_hash(spec: dict, path: Path) -> None:
    """A broken or swapped file is never loaded (torch.load unpickles it)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    if not h.hexdigest().startswith(spec["sha256"]):
        path.unlink(missing_ok=True)
        raise ConnectionError("Plate model download was damaged. Try again.")


_download = Download()


def get_download() -> Download:
    return _download


def auto_download_ok() -> bool:
    """Reading a plate may fetch the models itself: they are there, or were never started. A paused,
    cancelled or half-done download is the user's to resume in Settings."""
    state = download_state()
    return state == "ready" or (state == "missing" and not _download.stopped)


def ensure_models(progress=None) -> None:
    if not models_ready():
        result = _download.run(progress)
        if result != "done":
            raise ConnectionError(f"The plate model download was {result}.")


def delete_models() -> None:
    for spec in (DETECTOR, READER):
        model_file(spec).unlink(missing_ok=True)
        model_file(spec).with_suffix(".download").unlink(missing_ok=True)


# ---------------------------------------------------------------- text cleanup and voting

TO_LETTER = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B"}
TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "J": "1", "Z": "2",
            "S": "5", "G": "6", "T": "7", "B": "8", "A": "4"}
# Georgian layouts: current AA-123-BB and the older ABC-123. L = letter, D = digit
# (layout, dash positions, look-alike fixes allowed: "00-340-20" -> "OO-512-ZO" needs 4; the shorter
# old layout is easier to hit by accident). Each fix also lowers the reading's confidence.
FORMATS = [("LLDDDLL", (2, 5), 4), ("LLLDDD", (3,), 2)]


def _fit(text: str, pattern: str) -> tuple[str, int] | None:
    """Text bent to the L/D pattern and the number of characters that had to change."""
    if len(text) != len(pattern):
        return None
    out, fixes = [], 0
    for ch, kind in zip(text, pattern):
        ok = ch.isalpha() if kind == "L" else ch.isdigit()
        if not ok:
            ch = (TO_LETTER if kind == "L" else TO_DIGIT).get(ch)
            if ch is None:
                return None
            fixes += 1
        out.append(ch)
    return "".join(out), fixes


def clean_plate(raw: str) -> tuple[str, bool]:
    """(plate text, matches a known format). Known layouts get dashes and look-alike fixes
    (0/O, 1/I, 8/B...); anything else is kept as read (letters and digits only), never forced."""
    text, ok, _ = _clean(raw)
    return text, ok


def _clean(raw: str) -> tuple[str, bool, int]:
    """clean_plate plus the number of characters that had to be changed or dropped."""
    text = re.sub(r"[^A-Z0-9]", "", raw.upper())
    # (candidate, characters dropped, look-alike fixes still allowed)
    candidates = [(text, 0, 99)]
    if text.startswith("GE") and len(text) > 7:  # the small country code on the blue band
        candidates.append((text[2:], 0, 99))
    if len(text) == 8:  # one stray character: the flag band / a bolt at an end, or a doubled stroke (PR5555HA)
        candidates += [(text[:i] + text[i + 1:], 1, 0) for i in range(8)  # only if the rest fits as read
                       if i in (0, 7) or text[i] in (text[i - 1], text[i + 1])]
    best = None
    for cand, dropped, allowed in candidates:
        for pattern, dashes, max_fixes in FORMATS:
            fit = _fit(cand, pattern)
            if fit and fit[1] <= min(allowed, max_fixes) and (best is None or fit[1] + dropped < best[1]):
                best = (fit[0], fit[1] + dropped, dashes)
    if best is None:
        return text, False, 0
    plate, fixes, dashes = best
    for i in reversed(dashes):
        plate = plate[:i] + "-" + plate[i:]
    return plate, True, fixes


def plausible(plate: str) -> bool:
    """Could this be a plate at all? The text model falls back on dictionary words ("GREEN", "CAFE")
    and clock digits when a plate is unreadable; real plates mix letters and digits."""
    if "-" in plate:  # matched a known layout
        return True
    digits = sum(c.isdigit() for c in plate)
    return 5 <= len(plate) <= 10 and digits >= 2 and len(plate) - digits >= 1


def vote(readings: list[tuple[str, float]]) -> tuple[str, float] | None:
    """Best-of-N: readings are (cleaned text, confidence 0..1). Character-wise weighted vote among the
    readings of the winning length; confidence drops when the readings disagree."""
    readings = [(t, c) for t, c in readings if t]
    if not readings:
        return None
    by_len: dict[int, float] = defaultdict(float)
    for text, conf in readings:
        by_len[len(text)] += conf * (2.0 if "-" in text else 1.0)  # a known layout is strong evidence
    length = max(by_len, key=by_len.get)
    same = [(t, c) for t, c in readings if len(t) == length]
    if any("-" in t for t, _ in same):  # a raw read of that length has its characters in other places
        same = [(t, c) for t, c in same if "-" in t]
    chars, margin = [], 1.0
    for i in range(length):
        scores: dict[str, float] = defaultdict(float)
        for text, conf in same:
            scores[text[i]] += conf
        ch = max(scores, key=scores.get)
        chars.append(ch)
        # every contested character lowers the confidence: on the real clips the wrong reads were the
        # ones where the frames kept disagreeing about one or two look-alikes (Y/V, D/B/Q, R/N)
        margin *= scores[ch] / sum(scores.values())
    text = "".join(chars)
    total = sum(c for _, c in readings)
    support = sum(c for t, c in readings if t == text)
    agreement = support / total if total else 0.0
    best = max((c for t, c in readings if t == text), default=0.0)
    if not support:  # consensus string nobody read exactly: trust it less
        best = sum(c for _, c in same) / len(same) * 0.6
        agreement = sum(c for _, c in same) / total
    # agreement ** 0.75 (it was the square root): a plate the frames read 3 ways at 0.3 was one too many
    return text, round(best * agreement ** 0.75 * margin, 3)


# ---------------------------------------------------------------- readability

def plate_quality(crop: np.ndarray | None, full_px: float = 360.0) -> float:
    """0..1 how promising a vehicle (or plate) crop is for reading: size (full at full_px wide),
    sharpness and contrast. Cheap enough to run on every tracked frame to keep the best few crops."""
    if crop is None or crop.size == 0 or min(crop.shape[:2]) < 8:
        return 0.0
    w = crop.shape[1]
    if w > 640:  # sharpness on a common scale so big crops are not penalised for downscaling noise
        crop = cv2.resize(crop, (640, max(1, round(crop.shape[0] * 640 / w))), interpolation=cv2.INTER_AREA)
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharp = lap / (lap + 150.0)
    size = min(1.0, w / full_px)
    contrast = min(1.0, float(gray.std()) / 50.0)
    return round(math.sqrt(sharp * size) * (0.5 + 0.5 * contrast), 4)


def plate_score(plate: np.ndarray | None, det_conf: float = 1.0, box_w: float | None = None,
                box_h: float | None = None) -> float:
    """How readable one found plate is, to pick the best moments of a vehicle: plate width (px) x
    sharpness (Laplacian on a common text height) x aspect fit (a frontal plate is ~4.5:1; seen from
    the side it gets narrower) x detector confidence x contrast. box_w / box_h: the detector's box
    (the plate picture may carry a margin)."""
    if plate is None or plate.size == 0 or min(plate.shape[:2]) < 4:
        return 0.0
    h, w = plate.shape[:2]
    box_w, box_h = float(box_w or w), float(box_h or h)
    gray = plate if plate.ndim == 2 else cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
    k = SCORE_H / h
    gray = cv2.resize(gray, (max(4, round(w * k)), SCORE_H), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharp = lap / (lap + SCORE_SHARP)
    size = box_w / (box_w + SCORE_PX)
    aspect = box_w / max(box_h, 1.0)
    fit = math.sqrt(min(aspect / PLATE_ASPECT, PLATE_ASPECT / max(aspect, 1e-3)))
    contrast = min(1.0, float(gray.std()) / 50.0)
    return round(size * sharp * fit * float(det_conf) * (0.5 + 0.5 * contrast), 5)


def fuse_plates(plates: list[np.ndarray]) -> np.ndarray | None:
    """Average of several pictures of one plate, each aligned to the first (ECC, affine): random noise
    (night grain, IR speckle) averages out while the characters add up. Pictures that do not align
    are left out; None when fewer than two are left."""
    plates = [p for p in plates if p is not None and p.size and min(p.shape[:2]) >= 6]
    if len(plates) < 2:
        return None
    ref = plates[0] if plates[0].ndim == 3 else cv2.cvtColor(plates[0], cv2.COLOR_GRAY2BGR)
    k = max(1.0, FUSE_H / ref.shape[0])
    size = (round(ref.shape[1] * k), round(ref.shape[0] * k))
    ref = cv2.resize(ref, size, interpolation=cv2.INTER_CUBIC)
    ref_g = cv2.GaussianBlur(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), (3, 3), 0).astype(np.float32)
    acc, n = ref.astype(np.float32), 1
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-4)
    for p in plates[1:]:
        p = cv2.resize(p if p.ndim == 3 else cv2.cvtColor(p, cv2.COLOR_GRAY2BGR), size, interpolation=cv2.INTER_CUBIC)
        g = cv2.GaussianBlur(cv2.cvtColor(p, cv2.COLOR_BGR2GRAY), (3, 3), 0).astype(np.float32)
        warp = np.eye(2, 3, dtype=np.float32)
        try:
            cc, warp = cv2.findTransformECC(ref_g, g, warp, cv2.MOTION_AFFINE, criteria, None, 5)
        except cv2.error:
            continue
        if cc < FUSE_MIN_CC or abs(warp[0, 0] - 1) > 0.25 or abs(warp[1, 1] - 1) > 0.25:
            continue  # a different picture (another part of the car, a big misfit): not averaged in
        acc += cv2.warpAffine(p, warp, size, flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_REPLICATE)
        n += 1
    return (acc / n).round().astype(np.uint8) if n >= 2 else None


def deskew(plate: np.ndarray) -> np.ndarray | None:
    """Plate cut out straight: cameras look down at an angle, so plates appear rotated in their box.
    The tilt comes from the plate's long edges (Hough lines), the text band from where vertical
    character strokes are. None when nothing plate-like is found."""
    if plate is None or min(plate.shape[:2]) < 8:
        return None
    scale = max(1.0, 96 / plate.shape[0])  # small plates: work on an enlarged copy
    img = cv2.resize(plate, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1 else plate
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape
    angle = _tilt(gray)
    rot = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    level = cv2.warpAffine(img, rot, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    band = _text_band(cv2.warpAffine(gray, rot, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE))
    if band is None:
        return None
    y1, y2, x1, x2 = band
    return level[y1:y2, x1:x2]


def _tilt(gray: np.ndarray) -> float:
    """Rotation (degrees, for cv2.getRotationMatrix2D) that levels the plate's long edges."""
    h, w = gray.shape
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=max(20, w // 5), minLineLength=w * 0.35,
                            maxLineGap=max(3, w // 30))
    if lines is None:
        return 0.0
    angles, weights = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4).tolist():
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if a > 90:
            a -= 180
        if a < -90:
            a += 180
        if abs(a) <= 40:
            angles.append(a)
            weights.append(math.hypot(x2 - x1, y2 - y1))
    if not angles:
        return 0.0
    order = np.argsort(angles)
    cum = np.cumsum(np.asarray(weights)[order])
    return float(np.asarray(angles)[order][np.searchsorted(cum, cum[-1] / 2)])  # length-weighted median


def _text_band(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """(y1, y2, x1, x2) of the characters on a levelled plate: rows/columns rich in vertical strokes."""
    h, w = gray.shape
    strokes = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    rows = cv2.GaussianBlur(strokes.mean(axis=1).reshape(-1, 1), (1, 0), sigmaX=1, sigmaY=max(1.0, h / 30)).ravel()
    peak = int(rows.argmax())
    if rows[peak] <= 0:
        return None
    keep = rows >= rows[peak] * 0.45
    y1 = y2 = peak
    while y1 > 0 and keep[y1 - 1]:
        y1 -= 1
    while y2 < h - 1 and keep[y2 + 1]:
        y2 += 1
    band_h = y2 - y1 + 1
    if band_h < 6:
        return None
    pad = round(band_h * 0.18)
    y1, y2 = max(0, y1 - pad), min(h, y2 + 1 + pad)
    cols = cv2.GaussianBlur(strokes[y1:y2].mean(axis=0).reshape(1, -1), (0, 1), sigmaX=max(1.0, w / 60)).ravel()
    on = np.flatnonzero(cols >= cols.max() * 0.25)
    if not len(on):
        return None
    x1, x2 = max(0, int(on[0]) - band_h // 3), min(w, int(on[-1]) + 1 + band_h // 3)
    if (x2 - x1) / (y2 - y1) < 1.5:
        return None
    return y1, y2, x1, x2


# ---------------------------------------------------------------- PARSeq (inference only)

def _parseq_net(embed: int, enc_heads: int, dec_heads: int):
    """PARSeq: ViT encoder (timm layout, so the official state dict loads) + one two-stream decoder layer."""
    import torch
    from torch import nn
    import torch.nn.functional as F

    class Block(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.norm1 = nn.LayerNorm(embed, eps=1e-6)
            self.attn = nn.Module()
            self.attn.qkv = nn.Linear(embed, embed * 3)
            self.attn.proj = nn.Linear(embed, embed)
            self.norm2 = nn.LayerNorm(embed, eps=1e-6)
            self.mlp = nn.Module()
            self.mlp.fc1 = nn.Linear(embed, embed * 4)
            self.mlp.fc2 = nn.Linear(embed * 4, embed)

        def forward(self, x):
            b, n, _ = x.shape
            q, k, v = self.attn.qkv(self.norm1(x)).reshape(b, n, 3, enc_heads, -1).permute(2, 0, 3, 1, 4)
            a = F.scaled_dot_product_attention(q, k, v).transpose(1, 2).reshape(b, n, embed)
            x = x + self.attn.proj(a)
            return x + self.mlp.fc2(F.gelu(self.mlp.fc1(self.norm2(x))))

    class Encoder(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.patch_embed = nn.Module()
            self.patch_embed.proj = nn.Conv2d(3, embed, kernel_size=(4, 8), stride=(4, 8))
            self.pos_embed = nn.Parameter(torch.zeros(1, 8 * 16, embed))
            self.blocks = nn.ModuleList(Block() for _ in range(12))
            self.norm = nn.LayerNorm(embed, eps=1e-6)

        def forward(self, x):
            x = self.patch_embed.proj(x).flatten(2).transpose(1, 2) + self.pos_embed
            for blk in self.blocks:
                x = blk(x)
            return self.norm(x)

    class DecoderLayer(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.self_attn = nn.MultiheadAttention(embed, dec_heads, batch_first=True)
            self.cross_attn = nn.MultiheadAttention(embed, dec_heads, batch_first=True)
            self.linear1 = nn.Linear(embed, embed * 4)
            self.linear2 = nn.Linear(embed * 4, embed)
            for name in ("norm1", "norm2", "norm_q", "norm_c"):
                setattr(self, name, nn.LayerNorm(embed))

        def stream(self, tgt, tgt_norm, tgt_kv, memory, mask, pad_mask):
            tgt = tgt + self.self_attn(tgt_norm, tgt_kv, tgt_kv, attn_mask=mask, key_padding_mask=pad_mask,
                                       need_weights=False)[0]
            tgt = tgt + self.cross_attn(self.norm1(tgt), memory, memory, need_weights=False)[0]
            return tgt + self.linear2(F.gelu(self.linear1(self.norm2(tgt))))

        def forward(self, query, content, memory, query_mask, content_mask, pad_mask, update_content):
            query_norm, content_norm = self.norm_q(query), self.norm_c(content)
            query = self.stream(query, query_norm, content_norm, memory, query_mask, pad_mask)
            if update_content:
                content = self.stream(content, content_norm, content_norm, memory, content_mask, pad_mask)
            return query, content

    class Net(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.max_len = 25
            self.eos, self.bos, self.pad = 0, len(CHARSET) + 1, len(CHARSET) + 2
            self.encoder = Encoder()
            self.decoder = nn.Module()
            self.decoder.layers = nn.ModuleList([DecoderLayer()])
            self.decoder.norm = nn.LayerNorm(embed)
            self.head = nn.Linear(embed, len(CHARSET) + 1)
            self.text_embed = nn.Module()
            self.text_embed.embedding = nn.Embedding(len(CHARSET) + 3, embed)
            self.pos_queries = nn.Parameter(torch.zeros(1, self.max_len + 1, embed))

        def embed_tokens(self, t):
            return math.sqrt(embed) * self.text_embed.embedding(t)

        def decode(self, tgt, memory, tgt_mask=None, pad_mask=None, query=None, query_mask=None):
            n, length = tgt.shape
            null_ctx = self.embed_tokens(tgt[:, :1])
            content = torch.cat([null_ctx, self.pos_queries[:, :length - 1] + self.embed_tokens(tgt[:, 1:])], 1)
            if query is None:
                query = self.pos_queries[:, :length].expand(n, -1, -1)
            layers = self.decoder.layers
            for i, layer in enumerate(layers):
                query, content = layer(query, content, memory, query_mask, tgt_mask, pad_mask,
                                       update_content=i < len(layers) - 1)
            return self.decoder.norm(query)

        def forward(self, images):
            """Autoregressive decoding + one refinement pass, as in the paper's default."""
            bs, steps, dev = images.shape[0], self.max_len + 1, images.device
            memory = self.encoder(images)
            pos_queries = self.pos_queries[:, :steps].expand(bs, -1, -1)
            tgt_mask = query_mask = torch.triu(torch.ones((steps, steps), dtype=torch.bool, device=dev), 1)
            tgt_in = torch.full((bs, steps), self.pad, dtype=torch.long, device=dev)
            tgt_in[:, 0] = self.bos
            logits = []
            for i in range(steps):
                j = i + 1
                out = self.decode(tgt_in[:, :j], memory, tgt_mask[:j, :j], query=pos_queries[:, i:j],
                                  query_mask=query_mask[i:j, :j])
                p = self.head(out)
                logits.append(p)
                if j < steps:
                    tgt_in[:, j] = p.squeeze(1).argmax(-1)
                    if (tgt_in == self.eos).any(dim=-1).all():
                        break
            logits = torch.cat(logits, dim=1)
            # refinement uses a cloze mask (the original shares this tensor with tgt_mask on purpose)
            query_mask[torch.triu(torch.ones(steps, steps, dtype=torch.bool, device=dev), 2)] = 0
            bos = torch.full((bs, 1), self.bos, dtype=torch.long, device=dev)
            tgt_in = torch.cat([bos, logits[:, :-1].argmax(-1)], dim=1)
            pad_mask = (tgt_in == self.eos).int().cumsum(-1) > 0
            out = self.decode(tgt_in, memory, tgt_mask, pad_mask, pos_queries, query_mask[:, :tgt_in.shape[1]])
            return self.head(out)

    return Net()


def _decode_text(probs) -> list[tuple[str, float]]:
    """Greedy decode of softmax output (N, L, C): text and sequence confidence (product incl. EOS)."""
    out = []
    best, ids = probs.max(-1)
    for p_row, id_row in zip(best.tolist(), ids.tolist()):
        end = id_row.index(0) if 0 in id_row else len(id_row)
        text = "".join(CHARSET[i - 1] for i in id_row[:end] if 0 < i <= len(CHARSET))
        conf = float(np.prod(p_row[:end + 1])) if text else 0.0
        out.append((text, conf))
    return out


# ---------------------------------------------------------------- reader

class PlateReader:
    """Finds and reads licence plates on vehicle crops. Thread-safe; models load on first use
    (GPU when available, CPU otherwise or when the GPU runs out of memory)."""

    def __init__(self, device: str | None = None, max_crops: int = MAX_CROPS, fuse: bool = True) -> None:
        self._device = device
        self._fixed_device = device
        self._detector = None
        self._reader = None
        self._lock = threading.Lock()
        self.max_crops = max_crops
        self.fuse = fuse            # also read the averaged best plates (fuse_plates)

    @property
    def loaded(self) -> bool:
        return self._reader is not None

    @property
    def device(self) -> str | None:
        return self._device

    def load(self, progress=None) -> None:
        with self._lock:
            self._load(progress)

    def unload(self) -> None:
        with self._lock:
            self._detector = self._reader = None
            if self._device == "cuda":
                import torch
                torch.cuda.empty_cache()
            self._device = self._fixed_device  # chosen again by free VRAM at the next load

    def _load(self, progress=None) -> None:
        if self._reader is not None:
            return
        import torch
        from ultralytics import YOLO

        ensure_models(progress)
        if self._device is None:
            # the AI brain may hold most of the GPU; plates are rare events, the CPU is fine for them
            free = torch.cuda.mem_get_info()[0] if torch.cuda.is_available() else 0
            self._device = "cuda" if free > GPU_MIN_FREE else "cpu"
        self._detector = YOLO(str(model_file(DETECTOR)))
        try:
            self._reader = self._build_reader(self._device)
        except torch.cuda.OutOfMemoryError:
            self._to_cpu()

    def _to_cpu(self) -> None:
        import torch
        self._device = "cpu"
        self._reader = self._build_reader("cpu")
        torch.cuda.empty_cache()

    @staticmethod
    def _build_reader(device: str):
        import torch
        state = torch.load(model_file(READER), map_location="cpu", weights_only=True)
        state = {k.removeprefix("model."): v for k, v in state.items()}
        net = _parseq_net(384, 6, 12)
        net.load_state_dict(state)
        return net.eval().to(device)

    # -- public API

    def read(self, crops: list[np.ndarray]) -> dict | None:
        """Best-of-N plate reading on BGR pictures of one vehicle (its best moments, best first).
        The plate detector runs on up to max_crops of them in one batch; the plates found are ranked
        by plate_score, the READ_TOP best are read, and the FUSE_TOP best are also averaged (fuse_plates)
        into one more reading; then a character vote.
        Returns {"text", "confidence", "plate_crop", "index" (the picture the plate crop is from),
        "plate_px", "score", "readings"}: text "" when a plate was found but not read; None when no
        picture shows a plate."""
        keep = [i for i, c in enumerate(crops) if c is not None and c.size and min(c.shape[:2]) >= 16]
        if len(keep) > self.max_crops:
            keep = sorted(keep, key=lambda i: plate_quality(crops[i], full_px=640), reverse=True)[:self.max_crops]
        if not keep:
            return None
        images = [crops[i] for i in keep]
        import torch
        with self._lock:
            self._load()
            try:
                result = self._read(images)
            except torch.cuda.OutOfMemoryError:  # the GPU filled up since loading: carry on on the CPU
                self._to_cpu()
                result = self._read(images)
        if result is not None:
            result["index"] = keep[result["index"]]
        return result

    def find_plates(self, images: list[np.ndarray]) -> list[tuple[np.ndarray, float]]:
        """The best plate crop (and detector score) per image that has one."""
        with self._lock:
            self._load()
            return [(cut, conf) for _, cut, conf, _ in self._find_plates(images)]

    # -- internals (lock held)

    def _read(self, images: list[np.ndarray]) -> dict | None:
        found = self._find_plates(images)
        if not found:
            return None
        # (score, image index, plate picture, detector score, plate width in px)
        ranked = sorted(((plate_score(cut, conf, bw, bh), i, cut, conf, bw) for i, cut, conf, (bw, bh) in found),
                        key=lambda r: -r[0])
        top = ranked[:READ_TOP]
        fused = None
        if self.fuse:
            members = [r for r in ranked if r[2].shape[1] >= MIN_PLATE_PX][:FUSE_TOP]
            straight = [deskew(r[2]) for r in members]
            fused = fuse_plates([s if s is not None else r[2] for s, r in zip(straight, members)])
        texts = self._recognise([r[2] for r in top] + ([fused] if fused is not None else []))
        readings, best = [], {}
        for r, (raw, conf, plate) in zip(top, texts):
            score, i, cut, det_conf, bw = r
            text, _, fixes = _clean(raw)
            if cut.shape[1] < MIN_PLATE_PX or not plausible(text):
                continue
            weight = conf * (0.5 + 0.5 * det_conf) * (1 - 0.1 * fixes)
            readings.append((text, weight))
            if weight > best.get(text, (-1,))[0]:
                best[text] = (weight, plate, i, bw, score)
        out = [(t, round(c, 3), "frame") for t, c in readings]
        if fused is not None:  # one more voter; its picture is no moment of the video, so never shown
            text, _, fixes = _clean(texts[-1][0])
            det_conf = sum(r[3] for r in members) / len(members)
            weight = texts[-1][1] * (0.5 + 0.5 * det_conf) * (1 - 0.1 * fixes) * FUSE_WEIGHT
            if plausible(text):
                readings.append((text, weight))
                out.append((text, round(weight, 3), "fused"))
        voted = vote(readings)
        if voted is None or not best:
            _, i, cut, _, bw = top[0]
            return {"text": "", "confidence": 0.0, "plate_crop": cut, "index": i, "plate_px": int(bw),
                    "score": top[0][0], "readings": out}
        text, conf = voted
        _, plate, i, bw, score = best.get(text) or max(best.values(), key=lambda v: v[0])
        return {"text": text, "confidence": conf, "plate_crop": plate, "index": i, "plate_px": int(bw),
                "score": score, "readings": out}

    def recognise(self, plates: list[np.ndarray]) -> list[tuple[str, float]]:
        """Raw text + confidence for already cut-out plate images."""
        with self._lock:
            self._load()
            return [(text, conf) for text, conf, _ in self._recognise(plates)]

    def _find_plates(self, images: list[np.ndarray]) -> list[tuple[int, np.ndarray, float, tuple]]:
        """(image index, plate picture with a margin, detector score, (box width, box height)) for
        every image that shows a plate."""
        results = self._detector.predict(images, conf=MIN_PLATE_CONF, imgsz=640, verbose=False,
                                         device=0 if self._device == "cuda" else "cpu")
        found = []
        for n, (img, res) in enumerate(zip(images, results)):
            # a "plate" spanning most of the crop is the camera's clock / name overlay or a sign, not a plate
            boxes = [(c, b) for c, b in zip(res.boxes.conf.tolist(), res.boxes.xyxy.tolist())
                     if b[2] - b[0] <= img.shape[1] * MAX_PLATE_SHARE]
            if not boxes:
                continue
            conf, (x1, y1, x2, y2) = max(boxes)
            pw, ph = x2 - x1, y2 - y1
            # a little margin: tight boxes clip the first / last character
            x1, x2 = max(0, int(x1 - pw * 0.06)), min(img.shape[1], int(math.ceil(x2 + pw * 0.06)))
            y1, y2 = max(0, int(y1 - ph * 0.12)), min(img.shape[0], int(math.ceil(y2 + ph * 0.12)))
            if x2 - x1 >= 8 and y2 - y1 >= 4:
                found.append((n, img[y1:y2, x1:x2].copy(), float(conf), (pw, ph)))
        return found

    def _recognise(self, plates: list[np.ndarray]) -> list[tuple[str, float, np.ndarray]]:
        """Each plate is read as cut and straightened (twice: the second pass trims what the first
        could not see while still tilted); the most confident reading wins."""
        import torch
        variants, owner = [], []
        for i, p in enumerate(plates):
            once = deskew(p)
            for v in (p, once, deskew(once) if once is not None else None):
                if v is not None:
                    variants.append(v if v.ndim == 3 else cv2.cvtColor(v, cv2.COLOR_GRAY2BGR))
                    owner.append(i)
        batch = np.stack([cv2.resize(cv2.cvtColor(v, cv2.COLOR_BGR2RGB), (128, 32), interpolation=cv2.INTER_CUBIC)
                          for v in variants]).astype(np.float32) / 255.0
        x = (torch.from_numpy(batch).permute(0, 3, 1, 2) - 0.5) / 0.5
        with torch.inference_mode():
            probs = self._reader(x.to(self._device)).softmax(-1).float().cpu()
        best: list[tuple[str, float, np.ndarray] | None] = [None] * len(plates)
        for i, v, (text, conf) in zip(owner, variants, _decode_text(probs)):
            if best[i] is None or conf > best[i][1]:
                best[i] = (text, conf, v)
        return best
