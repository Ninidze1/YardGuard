"""Steady, good-looking AI overlay for live video.

A detector looks at every frame on its own. On noisy night video a parked car may score 0.6 on
one frame and 0.25 on the next, so raw boxes wobble and blink out. DisplayTracker follows the
usual practice for stable video detection:
- two-tier association (the ByteTrack idea): confident detections start objects, weak ones
  only keep existing objects alive, so a car does not vanish on a noisy frame;
- a One Euro filter per box edge: heavy smoothing while still, quick response when moving;
- parked memory: an object that has been still for a few seconds survives longer gaps;
- time-based lifetimes, so behaviour does not change with the camera's frame rate.
draw_tracks() renders boxes or corner brackets with anti-aliased lines and rounded label chips.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .video import CLASSES

STYLES = {"boxes": "Boxes", "corners": "Corners", "off": "Off"}

# BGR, chosen to read well on dark night video and to match the app's palette
COLORS = {"person": (68, 68, 239), "car": (246, 130, 59), "motorcycle": (11, 158, 245),
          "bus": (247, 85, 168), "truck": (129, 185, 16)}
NAMES = {"person": "Person", "car": "Car", "motorcycle": "Motorbike", "bus": "Bus", "truck": "Truck"}

LOW_SCORE = 0.10       # weakest detection that may keep an existing object alive
CONFIRM_S = 0.25       # an object must be seen this long (>= 2 frames) before it is drawn
KEEP_MOVING_S = 0.8    # a moving object is kept this long after its last detection
KEEP_PARKED_S = 20.0   # a parked object survives this long without detections (noise, occlusion)
PARKED_AFTER_S = 3.0   # still for this long -> treated as parked
STILL_SPEED = 0.06     # box sizes per second below which an object counts as still
MATCH_IOU = 0.25
MAX_MISSES = 6         # kept for tests/compatibility: drawing fades over this many missed frames


def _iou(a, b) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class OneEuro:
    """One Euro filter (Casiez et al.) on the 4 box edges, speed measured in box sizes per second,
    so a big near car and a small far car are smoothed alike."""

    def __init__(self, box: np.ndarray, t: float, min_cutoff: float = 0.3, beta: float = 1.0,
                 d_cutoff: float = 1.0) -> None:
        self.x, self.dx, self.t = box.astype(float), np.zeros(4), t
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff

    @staticmethod
    def _alpha(cutoff, dt: float):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, box: np.ndarray, t: float, trust: float = 1.0) -> np.ndarray:
        dt = max(1e-3, t - self.t)
        self.t = t
        size = max(8.0, float(min(self.x[2] - self.x[0], self.x[3] - self.x[1])))
        dx = (box - self.x) / dt / size
        self.dx = self.dx + self._alpha(self.d_cutoff, dt) * (dx - self.dx)
        cutoff = self.min_cutoff + self.beta * np.abs(self.dx)
        # A weak (low-score) detection is noisier: trust it less
        self.x = self.x + self._alpha(cutoff, dt) * trust * (box - self.x)
        return self.x

    def speed(self) -> float:
        return float(np.abs(self.dx).max())


@dataclass
class Track:
    id: int
    label: str
    box: np.ndarray            # smoothed x1, y1, x2, y2
    filt: OneEuro
    born: float
    seen: float                # last time it was matched
    hits: int = 1
    still_since: float | None = None
    misses: int = 0            # consecutive frames without a match (drawing fade only)
    confirmed: bool = False

    @property
    def title(self) -> str:
        return f"{NAMES.get(self.label, self.label.title())} {self.id}"

    @property
    def color(self) -> tuple[int, int, int]:
        return COLORS.get(self.label, (200, 200, 200))

    def parked(self, now: float) -> bool:
        return self.still_since is not None and now - self.still_since >= PARKED_AFTER_S

    def alive(self, now: float) -> bool:
        return now - self.seen <= (KEEP_PARKED_S if self.parked(now) else KEEP_MOVING_S)


class DisplayTracker:
    """Two-tier association + One Euro smoothing + parked memory (see module docstring)."""

    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self._next: dict[str, int] = {}

    def reset(self) -> None:
        self.tracks.clear()
        self._next.clear()

    def update(self, dets: np.ndarray, high: float = 0.35, now: float | None = None) -> list[Track]:
        """dets: (N, 6) x1, y1, x2, y2, score, class_id from a detector run with a LOW threshold
        (about LOW_SCORE). high: the user's certainty setting; only detections at least this sure
        start new objects. Returns the tracks to draw."""
        now = time.monotonic() if now is None else now
        names = [CLASSES.get(int(d[5])) for d in dets] if len(dets) else []
        if len(dets):
            keep = [i for i in range(len(dets)) if dets[i][4] >= LOW_SCORE and i < len(names)]
            dets, names = dets[keep], [names[i] for i in keep]
        strong = [i for i in range(len(dets)) if dets[i][4] >= high]
        weak = [i for i in range(len(dets)) if dets[i][4] < high]
        free = set(range(len(self.tracks)))
        # Stage 1: confident detections; stage 2: weak ones may only continue the leftovers
        for group, trust in ((strong, 1.0), (weak, 0.5)):
            for ti, di in self._match(free, group, dets, names):
                self._follow(self.tracks[ti], dets[di], now, trust)
                free.discard(ti)
                group.remove(di)
        for ti in free:
            self.tracks[ti].misses += 1
        for di in strong:  # unmatched confident detections become new objects...
            label = names[di]
            if label is not None and not self._duplicate(label, dets[di][:4]):
                self._next[label] = self._next.get(label, 0) + 1
                box = dets[di][:4].astype(float)
                self.tracks.append(Track(self._next[label], label, box.copy(), OneEuro(box, now), now, now))
        self.tracks = self._merge_twins([t for t in self.tracks if t.alive(now)])
        for t in self.tracks:
            if not t.confirmed and t.hits >= 2 and now - t.born >= CONFIRM_S:
                t.confirmed = True
        return [t for t in self.tracks if t.confirmed]

    @staticmethod
    def _merge_twins(tracks: list[Track]) -> list[Track]:
        """Two tracks that drifted onto the same object: keep the established one."""
        drop = set()
        for i, a in enumerate(tracks):
            for b in tracks[i + 1:]:
                if a.label == b.label and id(a) not in drop and id(b) not in drop and _iou(a.box, b.box) >= 0.6:
                    drop.add(id(b) if (a.hits, -a.born) >= (b.hits, -b.born) else id(a))
        return [t for t in tracks if id(t) not in drop]

    def _duplicate(self, label: str, box: np.ndarray) -> bool:
        """...unless they mostly cover an object we already have (a second, shifted box for the
        same car, which noisy night frames produce)."""
        area = max(1.0, float((box[2] - box[0]) * (box[3] - box[1])))
        for t in self.tracks:
            if t.label != label:
                continue
            ix = max(0.0, min(box[2], t.box[2]) - max(box[0], t.box[0]))
            iy = max(0.0, min(box[3], t.box[3]) - max(box[1], t.box[1]))
            t_area = max(1.0, float((t.box[2] - t.box[0]) * (t.box[3] - t.box[1])))
            if _iou(t.box, box) >= 0.45 or ix * iy / min(area, t_area) >= 0.8:
                return True
        return False

    def _match(self, free: set[int], group: list[int], dets: np.ndarray, names: list) -> list[tuple[int, int]]:
        pairs = []
        for ti in free:
            t = self.tracks[ti]
            for di in group:
                if names[di] == t.label:
                    score = _iou(t.box, dets[di][:4])
                    if score >= MATCH_IOU:
                        pairs.append((score, ti, di))
        used_t, used_d, out = set(), set(), []
        for _, ti, di in sorted(pairs, reverse=True):
            if ti not in used_t and di not in used_d:
                used_t.add(ti)
                used_d.add(di)
                out.append((ti, di))
        return out

    @staticmethod
    def _follow(t: Track, det: np.ndarray, now: float, trust: float) -> None:
        t.box = t.filt(det[:4].astype(float), now, trust).copy()
        t.seen = now
        t.hits += 1
        t.misses = 0
        if t.filt.speed() < STILL_SPEED:
            t.still_since = t.still_since if t.still_since is not None else now
        else:
            t.still_since = None


# --- Drawing -------------------------------------------------------------------------

def _font_path() -> str | None:
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("segoeuisb.ttf", "segoeui.ttf", "arial.ttf"):
        if (fonts / name).exists():
            return str(fonts / name)
    return None


@lru_cache(maxsize=256)
def _chip(text: str, color: tuple[int, int, int], height: int) -> np.ndarray:
    """Rounded label chip as a BGRA image, rendered once per text/colour/size."""
    from PIL import Image, ImageDraw, ImageFont

    path = _font_path()
    size = max(10, int(height * 0.62))
    font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    pad = max(5, height // 3)
    left, top, right, bottom = font.getbbox(text)
    w = right - left + 2 * pad
    img = Image.new("RGBA", (w, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, w - 1, height - 1), radius=height // 2,
                           fill=(color[2], color[1], color[0], 235))
    draw.text((pad - left, (height - (bottom - top)) / 2 - top), text, font=font, fill=(255, 255, 255, 255))
    rgba = np.array(img)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)


def _paste(frame: np.ndarray, chip: np.ndarray, x: int, y: int) -> None:
    h, w = chip.shape[:2]
    fh, fw = frame.shape[:2]
    x, y = min(max(0, x), fw - w), min(max(0, y), fh - h)
    if x < 0 or y < 0:
        return
    region = frame[y:y + h, x:x + w]
    alpha = chip[:, :, 3:4].astype(np.float32) / 255.0
    region[:] = (chip[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)


def _corners(frame, p1, p2, color, thick) -> None:
    (x1, y1), (x2, y2) = p1, p2
    arm = max(8, int(min(x2 - x1, y2 - y1) * 0.22))
    for (cx, cy), (dx, dy) in (((x1, y1), (1, 1)), ((x2, y1), (-1, 1)), ((x1, y2), (1, -1)), ((x2, y2), (-1, -1))):
        cv2.line(frame, (cx, cy), (cx + dx * arm, cy), color, thick, cv2.LINE_AA)
        cv2.line(frame, (cx, cy), (cx, cy + dy * arm), color, thick, cv2.LINE_AA)


def _color(t) -> tuple[int, int, int]:
    return t.color if hasattr(t, "color") else COLORS.get(t.label, (200, 200, 200))


def draw_tracks(frame: np.ndarray, tracks: list[Track], style: str = "boxes") -> np.ndarray:
    if style == "off" or not tracks:
        return frame
    h, w = frame.shape[:2]
    thick = max(2, w // 700)
    chip_h = max(18, w // 60)
    if style == "boxes":  # soft tint inside the boxes, so objects stand out without hiding them
        tint = frame.copy()
        for t in tracks:
            x1, y1, x2, y2 = (int(v) for v in t.box)
            cv2.rectangle(tint, (x1, y1), (x2, y2), _color(t), -1)
        frame = cv2.addWeighted(tint, 0.12, frame, 0.88, 0)
    # nearer (lower) objects drawn last, on top
    for t in sorted(tracks, key=lambda t: t.box[3]):
        x1, y1, x2, y2 = (int(round(v)) for v in t.box)
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w - 1, x2), min(h - 1, y2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        color = _color(t)
        # A briefly missed moving object fades; a parked one is kept on purpose, so it stays solid
        parked = getattr(t, "still_since", None) is not None
        fade = 1.0 if t.misses == 0 or parked else max(0.35, 1 - t.misses / (MAX_MISSES + 1))
        draw_color = tuple(int(c * fade) for c in color)
        if style == "corners":
            _corners(frame, (x1, y1), (x2, y2), draw_color, thick + 1)
        else:
            cv2.rectangle(frame, (x1, y1), (x2, y2), draw_color, thick, cv2.LINE_AA)
        chip = _chip(t.title, color, chip_h)
        above = y1 - chip_h - 4
        _paste(frame, chip, x1, above if above >= 0 else y1 + 4)
    return frame


def track_counts(tracks: list[Track]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tracks:
        counts[t.label] = counts.get(t.label, 0) + 1
    return counts
