"""Deterministic tests for yardguard.areas and the area parts of engine.py / config.py: synthetic
tracks and frames, a fake clock, fake camera, detector, brain and plate reader.
Run with the main venv:  run.cmd tests\\test_areas.py
"""
from __future__ import annotations

import csv
import functools
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "areas"
os.environ.setdefault("YARDGUARD_CONFIG", str(OUT / "config.json"))
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yardguard import areas as A  # noqa: E402
from yardguard import config  # noqa: E402
from yardguard.config import Camera, Config, Settings, Site, active_zones, new_area  # noqa: E402
from yardguard.engine import AlertStore, Monitor, Zones, plate_distance  # noqa: E402

W, H = 1280, 720
PERSON, CAR = 0, 2
DAY = datetime(2026, 10, 23, 14, 0, 0).timestamp()
NIGHT_WIN = ("00:00", "06:00")
FRAME = np.full((H, W, 3), 80, np.uint8)


class T:
    """A track as the evaluator sees it."""

    def __init__(self, tid, label, box):
        self.id, self.label, self.box = tid, label, np.array(box, np.float32)


def person(x, y, tid=1, h=160):
    """A person whose feet are at (x, y)."""
    return T(tid, "person", [x - 30, y - h, x + 30, y])


def car(x, y, tid=1, w=300, h=200, label="car"):
    return T(tid, label, [x - w / 2, y - h, x + w / 2, y])


def rect(x1, y1, x2, y2):
    """Polygon points (normalised) of a pixel rectangle."""
    return [[x1 / W, y1 / H], [x2 / W, y1 / H], [x2 / W, y2 / H], [x1 / W, y2 / H]]


def run(ev, areas, steps, frame=FRAME, night=NIGHT_WIN):
    """steps: (ts, [tracks]) or (ts, [tracks], frame). Returns all hits."""
    hits = []
    for step in steps:
        ts, tracks = step[0], step[1]
        hits += ev.update(areas, tracks, step[2] if len(step) > 2 else frame, ts, night)
    return hits


def kinds(hits) -> list[str]:
    return [h.event.kind for h in hits]


def walk(t0, xs, y, dt=0.2, tid=1, make=person):
    return [(t0 + i * dt, [make(x, y, tid)]) for i, x in enumerate(xs)]


# --- polygons -----------------------------------------------------------------------------

def test_polygon_enter_exit_hysteresis():
    area = new_area("count", "polygon", rect(0, 0, 640, 720), objects=["person"])
    ev = A.AreaEvaluator(1)
    t = DAY
    steps = [(t, [person(900, 500)]), (t + 0.2, [person(500, 500)]), (t + 0.4, [person(900, 500)])]  # one blip in
    assert kinds(run(ev, [area], steps)) == []
    steps = [(t + 0.6, [person(500, 500)]), (t + 0.8, [person(480, 500)])]
    hits = run(ev, [area], steps)
    assert kinds(hits) == ["enter"] and hits[0].event.ts == t + 0.8 and hits[0].event.value == "1"
    # two checks outside, then back: no exit
    steps = [(t + 1.0, [person(700, 500)]), (t + 1.2, [person(700, 500)]), (t + 1.4, [person(500, 500)])]
    assert kinds(run(ev, [area], steps)) == []
    # out for good: exit only after 3 checks AND 1.5 s
    steps = [(t + 1.4 + 0.2 * i, [person(800, 500)]) for i in range(1, 12)]
    hits = run(ev, [area], steps)
    assert kinds(hits) == ["exit"], kinds(hits)
    assert hits[0].event.ts >= t + 1.4 + 1.5
    live = ev.live(t + 4)[area["id"]]
    assert live["count_in"] == 1 and live["count_out"] == 1 and live["text"] == "In 1 · Out 1"


def test_polygon_id_switch_and_vanish():
    area = new_area("count", "polygon", rect(0, 0, 640, 720), objects=["person"])
    ev = A.AreaEvaluator(1)
    t = DAY
    hits = run(ev, [area], [(t + 0.2 * i, [person(400, 500, tid=1)]) for i in range(5)])
    assert kinds(hits) == ["enter"]
    # track 1 vanishes, track 2 turns up in the same place a second later: the same person
    hits = run(ev, [area], [(t + 1.0, []), (t + 1.5, []), (t + 2.0, [person(405, 502, tid=2)]),
                            (t + 2.2, [person(410, 505, tid=2)])])
    assert kinds(hits) == [], kinds(hits)
    # gone for good: exit once the grace time passed
    hits = run(ev, [area], [(t + 2.4 + 0.5 * i, []) for i in range(10)])
    assert kinds(hits) == ["exit"]
    assert hits[0].event.ts - (t + 2.2) >= A.LOST_GRACE_S["person"]


# --- lines ------------------------------------------------------------------------------

LINE = [[0.5, 0.1], [0.5, 0.9]]  # vertical, A at the top: a_to_b = from the right of the picture to the left


def test_line_direction_convention():
    a, b = (640, 72), (640, 648)
    assert A.side_of(a, b, (900, 400)) < 0 < A.side_of(a, b, (300, 400))
    nx, ny = A.arrow_normal(a, b)
    assert nx < 0 and abs(ny) < 1e-9  # the a_to_b arrow points to the left of the picture


def test_line_crossing_both_directions():
    area = new_area("count", "line", LINE, objects=["person"])
    ev = A.AreaEvaluator(1)
    t = DAY
    hits = run(ev, [area], walk(t, range(900, 360, -40), 500))
    assert kinds(hits) == ["cross_in"] and hits[0].event.extra["direction"] == "in", kinds(hits)
    hits = run(ev, [area], walk(t + 10, range(360, 920, 40), 500))
    assert kinds(hits) == ["cross_out"]
    hits = run(ev, [area], walk(t + 20, range(900, 360, -40), 500, tid=2))
    assert kinds(hits) == ["cross_in"] and hits[0].event.value == "2"
    live = ev.live(t + 30)[area["id"]]
    assert (live["count_in"], live["count_out"]) == (2, 1) and live["text"] == "In 2 · Out 1"


def test_line_direction_filter():
    area = new_area("line_cross", "line", LINE, direction="a_to_b", objects=["person"])
    ev = A.AreaEvaluator(1)
    hits = run(ev, [area], walk(DAY, range(900, 360, -40), 500))
    hits += run(ev, [area], walk(DAY + 10, range(360, 920, 40), 500))
    assert kinds(hits) == ["cross_in"]
    assert "watched direction" in A.describe(hits[0].event)


def test_line_jitter_never_double_counts():
    area = new_area("count", "line", LINE, objects=["person"])
    ev = A.AreaEvaluator(1)
    t = DAY
    small = [(t + 0.2 * i, [person(640 + (10 if i % 2 else -10), 500)]) for i in range(60)]
    assert kinds(run(ev, [area], small)) == []
    ev = A.AreaEvaluator(1)
    wide = [(t + 0.2 * i, [person(640 + (30 if i % 2 else -30), 500)]) for i in range(60)]
    assert kinds(run(ev, [area], wide)) == []
    # one real crossing, then standing on the line, dithering two frames each side: one count only
    ev = A.AreaEvaluator(1)
    hits = run(ev, [area], walk(t, range(900, 560, -40), 500))
    dither = [(t + 5 + 0.2 * i, [person(610 if (i // 2) % 2 == 0 else 670, 500)]) for i in range(80)]
    hits += run(ev, [area], dither)
    assert kinds(hits) == ["cross_in"], kinds(hits)


def test_line_ignores_crossings_beyond_its_ends():
    area = new_area("count", "line", [[0.5, 0.1], [0.5, 0.5]], objects=["person"])
    ev = A.AreaEvaluator(1)
    assert kinds(run(ev, [area], walk(DAY, range(900, 360, -40), 650))) == []  # below the end at y=360
    assert kinds(run(ev, [area], walk(DAY + 10, range(900, 360, -40), 300, tid=2))) == ["cross_in"]


def test_line_crossing_again_after_going_round_its_end():
    """Over the fence, round its end back to the start, over again: two crossings the same way."""
    area = new_area("line_cross", "line", [[0.5, 200 / H], [0.5, 500 / H]], objects=["person"])
    ev = A.AreaEvaluator(1)
    path = [(x, 400) for x in range(500, 800, 20)]
    path += [(780, y) for y in range(400, 660, 20)] + [(x, 650) for x in range(780, 480, -20)]
    path += [(500, y) for y in range(650, 390, -20)] + [(x, 400) for x in range(500, 800, 20)]
    hits = run(ev, [area], [(DAY + 0.4 * i, [person(x, y, tid=7, h=100)]) for i, (x, y) in enumerate(path)])
    assert kinds(hits) == ["cross_out", "cross_out"], kinds(hits)


def test_line_queue_does_not_inherit_across():
    """Car 1 crosses and vanishes; car 2 appears right behind it on the other side and crosses
    too: two counts (car 2 must not take over car 1's state)."""
    area = new_area("count", "line", [[0.5, 0.0], [0.5, 1.0]], objects=["car"])
    ev = A.AreaEvaluator(1)
    t = DAY
    hits = run(ev, [area], walk(t, range(1000, 420, -40), 600, make=car))
    hits += run(ev, [area], [(t + 3.0, []), (t + 3.2, [car(820, 600, tid=2)])])
    hits += run(ev, [area], walk(t + 3.4, range(780, 300, -40), 600, tid=2, make=car))
    assert kinds(hits) == ["cross_in", "cross_in"], kinds(hits)


def test_count_seeded_and_object_filter():
    area = new_area("count", "line", LINE, objects=["car"])
    ev = A.AreaEvaluator(1, seed=lambda area_id: (7, 3))
    hits = run(ev, [area], walk(DAY, range(900, 360, -40), 500))  # a person: not counted
    hits += run(ev, [area], walk(DAY + 5, range(1000, 300, -40), 600, tid=2, make=car))
    truck = lambda x, y, tid: car(x, y, tid, label="truck")  # noqa: E731  YOLO calls it a truck: still a car
    hits += run(ev, [area], walk(DAY + 15, range(300, 1000, 40), 600, tid=3, make=truck))
    assert kinds(hits) == ["cross_in", "cross_out"]
    assert ev.live(DAY + 30)[area["id"]]["text"] == "In 8 · Out 4"


# --- dwell ------------------------------------------------------------------------------------

SPOT = rect(400, 300, 800, 620)


def test_parking_spot_occupied_and_freed():
    area = new_area("parking_spot", "polygon", SPOT)
    ev = A.AreaEvaluator(1)
    t = DAY
    hits = run(ev, [area], [(t + i, [car(600, 560)]) for i in range(30)])
    assert kinds(hits) == ["occupied"] and hits[0].event.label == "car"
    assert t + 20 <= hits[0].event.ts <= t + 21
    live = ev.live(t + 29)[area["id"]]
    assert live["state"] == "occupied" and live["text"].startswith("Occupied")
    # the parked car's track breaks up at night: a new id in the same place 10 s later
    hits = run(ev, [area], [(t + 30 + i, []) for i in range(10)] + [(t + 40 + i, [car(602, 561, tid=9)])
                                                                    for i in range(20)])
    assert kinds(hits) == [], kinds(hits)
    # it drives away: free again once the grace and free_after_s passed
    hits = run(ev, [area], [(t + 60 + i, []) for i in range(120)])
    assert kinds(hits) == ["freed"]
    assert 60 + 30 + 45 - 2 <= hits[0].event.ts - t <= 60 + 30 + 45 + 2
    assert hits[0].event.value  # how long it was there
    assert ev.live(t + 200)[area["id"]]["text"] == "Free"


def test_no_parking_blocked_once_then_freed():
    area = new_area("no_parking", "polygon", SPOT, name="Gate")
    ev = A.AreaEvaluator(1)
    t = DAY
    jitter = [car(600 + (i % 3) - 1, 560 + (i % 2), tid=4) for i in range(200)]
    hits = run(ev, [area], [(t + 2 * i, [jitter[i]]) for i in range(200)])
    assert kinds(hits) == ["blocked"], kinds(hits)
    assert 300 <= hits[0].event.ts - t <= 304 and hits[0].event.value == "5 min"
    assert A.describe(hits[0].event) == "Car blocking Gate for 5 min"
    hits = run(ev, [area], walk(t + 400, range(600, 1200, 60), 560, dt=0.5, tid=4, make=car))
    assert kinds(hits) == ["freed"]


def test_polygon_edges_do_not_flicker():
    """Found on the night clips: a parked car's box jittering on the edge of an area, a spot drawn
    tightly around a car, a car cut by the bottom of the picture."""
    t = DAY
    # a parked car whose bumper reaches a little into the "no parking" area: nothing happens
    gate = new_area("no_parking", "polygon", rect(200, 540, 900, 720), params={"max_minutes": 1})
    ev = A.AreaEvaluator(1)
    steps = [(t + i, [car(500, 556 + (i % 3) * 4, w=300, h=220)]) for i in range(200)]
    assert kinds(run(ev, [gate], steps)) == []
    # a spot drawn tightly around the car (its box bottom on the spot's edge): occupied, never freed
    spot = new_area("parking_spot", "polygon", rect(450, 360, 750, 560))
    ev = A.AreaEvaluator(1)
    steps = [(t + i, [car(600 + (i % 2) * 3, 558 + (i % 3) * 3)]) for i in range(300)]
    assert kinds(run(ev, [spot], steps)) == ["occupied"]
    # a car cut by the bottom of the picture, its spot drawn down to the edge
    low = new_area("parking_spot", "polygon", rect(400, 500, 1000, 720))
    ev = A.AreaEvaluator(1)
    assert kinds(run(ev, [low], [(t + i, [car(700, 714, w=500, h=260)]) for i in range(60)])) == ["occupied"]
    # a person standing on the edge of an area, feet jittering across it: in once, never out
    area = new_area("count", "polygon", rect(0, 0, 640, 720), objects=["person"])
    ev = A.AreaEvaluator(1)
    steps = [(t + 0.2 * i, [person(630 + (i % 2) * 14, 500)]) for i in range(5)]
    steps += [(t + 1 + 0.2 * i, [person(636 + (i % 2) * 8, 500)]) for i in range(100)]
    hits = run(ev, [area], steps)
    assert kinds(hits) == [], kinds(hits)  # never clearly inside: never counted
    steps = [(t + 30 + 0.2 * i, [person(600, 500)]) for i in range(5)]
    steps += [(t + 31 + 0.2 * i, [person(636 + (i % 2) * 8, 500)]) for i in range(100)]
    assert kinds(run(ev, [area], steps)) == ["enter"]


def test_no_parking_ignores_a_car_driving_through():
    area = new_area("no_parking", "polygon", SPOT, params={"max_minutes": 0.1})
    ev = A.AreaEvaluator(1)
    hits = run(ev, [area], walk(DAY, range(300, 900, 10), 560, dt=0.5, make=car))  # slow, never still
    assert kinds(hits) == []


def test_loitering_once_across_id_switch():
    area = new_area("loitering", "polygon", SPOT, name="Door")
    ev = A.AreaEvaluator(1)
    t = DAY
    steps = [(t + 0.5 * i, [person(600, 560, tid=1)]) for i in range(30)]
    steps += [(t + 15.0, []), (t + 15.5, [])]
    steps += [(t + 16 + 0.5 * i, [person(603, 562, tid=2)]) for i in range(60)]
    hits = run(ev, [area], steps)
    assert kinds(hits) == ["loitering"], kinds(hits)
    assert 30 <= hits[0].event.ts - t <= 31 and hits[0].event.track_id == 2


def test_presence_night_schedule_across_midnight():
    area = new_area("presence", "polygon", SPOT, schedule={"mode": "custom", "start": "22:00", "end": "06:00"})
    d = datetime(2026, 10, 23)
    ev = A.AreaEvaluator(1)
    t = d.replace(hour=21, minute=59, second=40).timestamp()
    hits = run(ev, [area], [(t + 0.5 * i, [person(600, 560)]) for i in range(80)])
    assert kinds(hits) == ["presence"] and hits[0].event.ts >= d.replace(hour=22).timestamp()
    ev = A.AreaEvaluator(1)
    t = d.replace(hour=23, minute=59, second=50).timestamp()
    hits = run(ev, [area], [(t + 0.5 * i, [person(600, 560)]) for i in range(40)])
    assert kinds(hits) == ["presence"] and hits[0].event.ts < t + 2
    ev = A.AreaEvaluator(1)
    t = d.replace(hour=7).timestamp()
    assert kinds(run(ev, [area], [(t + 0.5 * i, [person(600, 560)]) for i in range(40)])) == []
    assert ev.live(t + 20)[area["id"]]["state"] == "paused"
    # "night" follows the app's night hours
    night_area = new_area("presence", "polygon", SPOT, schedule={"mode": "night"})
    ev = A.AreaEvaluator(1)
    t = d.replace(hour=1).timestamp()
    assert kinds(run(ev, [night_area], [(t + 0.5 * i, [person(600, 560)]) for i in range(10)])) == ["presence"]
    ev = A.AreaEvaluator(1)
    assert kinds(run(ev, [night_area], [(DAY + 0.5 * i, [person(600, 560)]) for i in range(10)])) == []


# --- pictures of an area ----------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _scene(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    small = rng.integers(30, 110, (36, 64, 3), dtype=np.uint8)
    return cv2.resize(small, (W, H), interpolation=cv2.INTER_CUBIC)


def scene(seed=3) -> np.ndarray:
    """A night-ish yard: smooth blotches, not a flat colour."""
    return _scene(seed).copy()


@functools.lru_cache(maxsize=1)
def _noise_bank() -> list[np.ndarray]:
    rng = np.random.default_rng(99)
    return [rng.normal(0, 6, (H, W, 3)).astype(np.float32) for _ in range(12)]


def noisy(img: np.ndarray, rng, gain: float = 1.0) -> np.ndarray:
    """Sensor noise like a night picture (std 6), a different frame every call."""
    bank = _noise_bank()
    out = img.astype(np.float32) * gain + bank[int(rng.integers(len(bank)))]
    return np.clip(out, 0, 255).astype(np.uint8)


def with_package(img: np.ndarray) -> np.ndarray:
    out = img.copy()
    box = out[420:520, 560:660]
    box[:] = 40
    for y in range(0, 100, 20):
        for x in range((y // 20) % 2 * 20, 100, 40):
            box[y:y + 20, x:x + 20] = 230
    return out


def test_left_object_appears_and_is_taken_back():
    area = new_area("left_object", "polygon", rect(450, 300, 760, 620), name="Door step")
    ev = A.AreaEvaluator(1)
    rng = np.random.default_rng(1)
    base = scene()
    t = DAY
    hits = []
    for i in range(200):
        img = with_package(base) if 10 <= i < 120 else base
        hits += ev.update([area], [], noisy(img, rng), t + i, NIGHT_WIN)
    assert kinds(hits) == ["appeared", "removed"], kinds(hits)
    assert 70 <= hits[0].event.ts - t <= 75, hits[0].event.ts - t
    assert hits[1].event.value == "back to before" and 120 < hits[1].event.ts - t <= 150
    assert ev.live(t + 200)[area["id"]]["state"] == "normal"


def test_left_object_quiet_on_lighting_noise_and_people():
    area = new_area("left_object", "polygon", rect(450, 300, 760, 620))
    ev = A.AreaEvaluator(1)
    rng = np.random.default_rng(2)
    base = scene(5)
    t = DAY
    hits = []
    for i in range(400):
        gain = 1.0 + 0.2 * np.sin(i / 60.0)          # slow light changes (clouds, dusk)
        if i >= 150:
            gain *= 0.55                              # the IR switch: everything darker at once
        img = noisy(base, rng, gain)
        tracks = []
        if 200 <= i < 330:                            # a person standing in the area for 2 min
            img[400:600, 560:640] = 200
            tracks = [person(600, 600, tid=3, h=200)]
        hits += ev.update([area], tracks, img, t + i, NIGHT_WIN)
    assert kinds(hits) == [], kinds(hits)


def test_state_watch_changed_and_restored():
    area = new_area("state_watch", "polygon", rect(450, 300, 760, 620), name="Gate")
    ev = A.AreaEvaluator(1)
    rng = np.random.default_rng(4)
    base = scene(7)
    opened = base.copy()
    opened[320:600, 470:640] = np.flipud(base[100:380, 900:1070])  # the street seen through the open gate
    t = DAY
    hits = []
    for i in range(400):
        img = opened if 20 <= i < 220 else base
        hits += ev.update([area], [], noisy(img, rng), t + i, NIGHT_WIN)
    assert kinds(hits) == ["changed", "changed"], kinds(hits)
    assert hits[0].event.value == "changed" and 140 <= hits[0].event.ts - t <= 145
    assert hits[1].event.value == "restored" and A.describe(hits[1].event) == "Gate back to normal"


def test_light_jump_clears_the_shown_state():
    """After the IR switch the current look is the new normal: no 'Changed' left showing."""
    px = A._PixelWatch()
    px.ref = px.cur = np.zeros((4, 4), np.float32)
    px.state, px.state_since, px.last_kind = "changed", DAY, "changed"
    px.reset()
    assert (px.state, px.state_since, px.last_kind) == ("normal", None, "")


# --- ask_ai -----------------------------------------------------------------------------------

def test_ask_ai_rate_limit_and_change_only():
    area = new_area("ask_ai", "polygon", rect(450, 300, 760, 620), params={"question": "barrier arm is raised",
                                                                           "interval_s": 30})
    ev = A.AreaEvaluator(1)
    base = scene(9)
    changed = with_package(base)
    t = DAY
    asked = []

    def tick(i, img, answer=None):
        ev.update([area], [], img, t + i, NIGHT_WIN)
        ask = ev.take_ask()
        if ask is None:
            return []
        asked.append(i)
        assert ask["crop"].shape[0] <= A.AI_CROP_MAX and ask["question"] == "barrier arm is raised"
        return ev.ai_answer(ask, answer)

    hits = []
    for i in range(0, 50):
        hits += tick(i, base, True)
    assert asked == [0] and kinds(hits) == ["answer_yes"]    # unchanged picture: asked once
    for i in range(50, 85):
        hits += tick(i, changed, True)
    assert asked == [0, 50] and kinds(hits) == ["answer_yes"]  # changed, same answer: nothing logged
    for i in range(85, 130):
        hits += tick(i, base, False)
    assert asked == [0, 50, 85] and kinds(hits) == ["answer_yes", "answer_no"]
    assert ev.live(t + 130)[area["id"]]["state"] == "no"


# --- store ------------------------------------------------------------------------------------

def _event(area, ts, kind, **kw):
    return A.new_event(area, 1, ts, kind, site_id="s1", **kw)


def test_area_events_store_round_trip_filters_csv_retention():
    folder = OUT / "store"
    shutil.rmtree(folder, ignore_errors=True)
    store = AlertStore(str(folder))
    gate = new_area("log_plates", "line", LINE, name="Barrier")
    spot = new_area("parking_spot", "polygon", SPOT, name="My spot")
    pics = folder / "areas" / "old"
    pics.mkdir(parents=True)
    old_pic = pics / "x.jpg"
    old_pic.write_bytes(b"jpg")
    now = DAY
    events = [
        _event(gate, now - 40 * 86400, "plate", label="car", value="OLD-001", snapshot_path=str(old_pic),
               extra={"direction": "in"}),
        _event(gate, now - 3600, "plate", label="car", value="AA-123-BB", confidence=0.91, track_id=5,
               extra={"direction": "in"}),
        _event(gate, now - 1800, "plate", label="truck", value="", extra={"direction": "in", "note": "plate unreadable"}),
        _event(gate, now - 60, "plate", label="car", value="AA-123-BB", extra={"direction": "out", "stay_s": 3540}),
        _event(spot, now - 500, "occupied", label="car"),
        _event(spot, now - 100, "freed", label="car", value="6 min"),
    ]
    for e in events:
        assert store.add_area_event(e) == e.id
    got = store.get_area_event(events[1].id)
    assert got.value == "AA-123-BB" and got.track_id == 5 and got.extra == {"direction": "in"} and \
        abs(got.confidence - 0.91) < 1e-9 and got.clip_offset_s == -1.0
    assert [e.id for e in store.list_area_events(area_id=gate["id"])] == [e.id for e in events[3::-1]]
    assert len(store.list_area_events(kind=["occupied", "freed"])) == 2
    assert len(store.list_area_events(query="123")) == 2
    assert len(store.list_area_events(query="unreadable")) == 1
    assert len(store.list_area_events(since=now - 2000, until=now - 70)) == 3
    stats = store.area_stats(gate["id"], since=now - 86400)
    assert stats["total"] == 3 and stats["counts"] == {"plate": 3} and (stats["in"], stats["out"]) == (2, 1)
    assert sum(sum(h["counts"].values()) for h in stats["hours"]) == 3
    assert store.area_stats(spot["id"])["state"] == "free"
    n = store.export_area_events_csv(folder / "log.csv", area_id=gate["id"])
    rows = list(csv.reader((folder / "log.csv").open(encoding="utf-8-sig")))
    assert n == 4 and len(rows) == 5 and rows[0][0] == "Time" and rows[2][7] == "AA-123-BB"
    assert "AA-123-BB coming in at Barrier" in rows[2][5]
    # clips: an area event's clip is in use, and forgotten when the file goes
    store.set_area_event_clip(events[3].id, str(folder / "clips" / "c.mp4"), 7.5)
    refs = store.clip_refs()
    assert len(refs) == 1 and refs[0]["clip_path"].endswith("c.mp4") and not refs[0]["false_alarm"]
    store.clear_clip(refs[0]["clip_path"])
    assert store.get_area_event(events[3].id).clip_path == ""
    # retention: older than 30 days go with their pictures
    assert store.cleanup_area_events(30, now=now) == 1 and not old_pic.exists()
    assert store.delete_area_events(spot["id"]) == [] and store.list_area_events(area_id=spot["id"]) == []
    store.close()
    # migration in place: a database from before areas gets the table
    store = AlertStore(str(folder))
    assert len(store.list_area_events()) == 3
    store.close()


def test_plate_helpers():
    assert plate_distance("aa-123 bb", "AA123BB") == 0
    assert plate_distance("AA-123-BB", "AA-128-BB") == 1
    assert plate_distance("AA-123-BB", "ZZ-999-XX") > 1
    assert plate_distance("", "") > 1


# --- config -----------------------------------------------------------------------------------

def test_zones_migrate_to_watch_zone_areas():
    folder = OUT / "migrate"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    path = folder / "config.json"
    zones = [{"name": "Parking row", "points": [[0.1, 0.5], [0.6, 0.5], [0.6, 0.9], [0.1, 0.9]]},
             {"name": "", "points": [[0.7, 0.1], [0.9, 0.1], [0.9, 0.3]]}]
    path.write_text(json.dumps({"sites": [{"name": "Home", "host": "10.0.0.2", "id": "abcd1234",
                                           "cameras": [{"channel": 1, "name": "Yard", "zones": zones}]}],
                                "settings": {"clip_max_days": 7}}), encoding="utf-8")
    old_path = config.CONFIG_PATH
    config.CONFIG_PATH = path
    try:
        cfg = config.load_config()
        cam = cfg.sites[0].cameras[0]
        assert [a["task"] for a in cam.areas] == ["watch_zone", "watch_zone"]
        assert [a["name"] for a in cam.areas] == ["Parking row", "Zone 2"]
        assert cam.zones[0]["points"] == zones[0]["points"] and len(active_zones(cam)) == 2
        assert cfg.settings.area_log_days == 30
        cam.areas.append(new_area("count", "line", LINE))
        cam.areas[1]["enabled"] = False
        cfg.save()
        saved = json.loads(path.read_text(encoding="utf-8"))["sites"][0]["cameras"][0]
        assert "zones" not in saved and len(saved["areas"]) == 3
        cam2 = config.load_config().sites[0].cameras[0]
        assert [a["id"] for a in cam2.areas] == [a["id"] for a in cam.areas]  # loaded again: no duplicates
        assert len(active_zones(cam2)) == 1 and len(cam2.zones) == 2           # disabled zone not watched
        zs = Zones(active_zones(cam2), W, H)
        assert zs.at(0.3 * W, 0.7 * H) == "Parking row" and zs.at(0.8 * W, 0.15 * H) is None
        # writing zones (older code) keeps the other areas and the zone's id
        first = cam2.areas[0]["id"]
        cam2.zones = [{"name": "Row", "points": zones[0]["points"]}]
        assert [a["task"] for a in cam2.areas] == ["watch_zone", "count"] and cam2.areas[0]["id"] == first
        assert Camera(3, zones=zones).zones[1]["name"] == "Zone 2"
    finally:
        config.CONFIG_PATH = old_path


def test_new_area_defaults_and_normalize():
    a = new_area("no_parking", "polygon", SPOT)
    assert a["params"] == {"max_minutes": 5} and a["alert"] and a["objects"] == ["car", "truck", "bus", "motorcycle"]
    assert len(a["id"]) == 8 and a["schedule"]["mode"] == "always" and a["enabled"]
    b = config.normalize_area({"task": "bogus", "shape": "line", "points": [[2, -1], [0.5, 0.5], [0.1, 0.1]],
                               "params": "x", "schedule": None})
    assert b["task"] == "presence" and b["points"] == [[1.0, 0.0], [0.5, 0.5]] and b["schedule"]["mode"] == "always"
    assert set(config.TEMPLATES) == set(config.TEMPLATE_INFO)


# --- monitor --------------------------------------------------------------------------------

class FakeSource:
    instances: dict[int, "FakeSource"] = {}

    def __init__(self, url, fps, long_side=1280, gpu_decode=True):
        self.channel = int(url.split("channel=")[1].split("&")[0])
        self.item = None
        self.long_side = long_side
        self.backend, self.error, self.alive, self.size = "fake", None, True, (W, H)
        FakeSource.instances[self.channel] = self

    def start(self):
        pass

    def stop(self):
        pass

    def latest(self):
        return self.item

    def push(self, ts, img=None):
        frame = (img if img is not None else scene(1)).copy()
        frame[0, 0, 0] = self.channel
        self.item = (frame, ts)


class FakeDetector:
    backend = "fake"

    def __init__(self):
        self.dets: dict[int, np.ndarray] = {}

    def predict(self, frames, conf):
        return [self.dets.get(int(f[0, 0, 0]), np.zeros((0, 6), np.float32)) for f in frames]


class FakeBrain:
    def __init__(self, answer):
        self.answer = answer   # function(seed) -> bool
        self.calls = []
        self.user_stopped = False

    def status(self):
        return "ready"

    def start(self, user=False):
        pass

    def ask(self, image, phrases, mode="fast", timeout=60, max_side=896, upscale=False, seed=0):
        self.calls.append((image.shape, list(phrases), max_side, upscale, seed))
        h, w = image.shape[:2]
        if self.answer(seed):  # in the middle of the picture: on the subject of a verify crop
            return [{"label": phrases[0], "x1": 0.45 * w, "y1": 0.3 * h, "x2": 0.55 * w, "y2": 0.7 * h,
                     "score": None}]
        return []


class FakeReader:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def read(self, crops):
        self.calls.append(len(crops))
        text = self.texts.pop(0) if self.texts else None
        if not text:
            return None
        return {"text": text, "confidence": 0.9, "plate_crop": crops[0][:20, :60].copy()}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_monitor(tmp: str, area_list: list[dict], brain=None, mode="off"):
    events = OUT / tmp
    shutil.rmtree(events, ignore_errors=True)
    cam = Camera(1, "Yard", "Yard")
    cam.areas = [config.normalize_area(a) for a in area_list]
    site = Site(name="Test", host="127.0.0.1", cameras=[cam])
    cfg = Config(sites=[site], active_site_id=site.id)
    s = Settings(events_dir=str(events), clips_enabled=False, brain_mode=mode, rule_night_presence=False,
                 rule_loitering=False, rule_prowling=False, rule_car_moved=False, motion_gating=False)
    cfg.settings = s
    store = AlertStore(str(events))
    det = FakeDetector()
    clock = Clock(DAY)
    got = {"alerts": [], "areas": []}
    FakeSource.instances.clear()
    mon = Monitor(cfg, store, brain, on_alert=got["alerts"].append, detector=det, source_factory=FakeSource,
                  clock=clock, on_area_event=got["areas"].append)
    mon._setup()
    mon._running = True  # area_live answers only while monitoring
    return mon, store, det, clock, got


def feed(mon, det, clock, positions, step=0.2, cls=PERSON, img=None):
    """positions: list of boxes (or None = nothing detected), one per step."""
    src = FakeSource.instances[1]
    for box in positions:
        clock.t += step
        det.dets[1] = np.array([[*box, 0.9, cls]], np.float32) if box is not None else np.zeros((0, 6), np.float32)
        src.push(clock.t, img)
        mon.step()
    while mon.area_step():
        pass


def test_monitor_area_alert_and_event():
    linger = new_area("loitering", "polygon", rect(400, 200, 900, 700), name="Door", alert=True,
                      params={"seconds": 10})
    count = new_area("count", "polygon", rect(0, 0, 1280, 720), name="Whole yard", objects=["person"])
    mon, store, det, clock, got = make_monitor("m_alert", [linger, count])
    feed(mon, det, clock, [(600, 400, 680, 600)] * 75)
    alerts = store.list()
    assert [a.rule for a in alerts] == ["area_loitering"], [a.rule for a in alerts]
    a = alerts[0]
    assert a.title.startswith("Person lingering at Door for") and a.verdict == "unverified"
    assert Path(a.snapshot_path).exists() and a.incident_id
    inc = store.get_incident(a.incident_id)
    assert "lingering" in inc.title
    evs = store.list_area_events()
    assert sorted(e.kind for e in evs) == ["enter", "loitering"]
    loiter = next(e for e in evs if e.kind == "loitering")
    assert loiter.extra["alert_id"] == a.id and Path(loiter.snapshot_path).exists() and Path(loiter.crop_path).exists()
    assert "areas" in loiter.snapshot_path and [e.kind for e in got["areas"]].count("loitering") == 1
    live = mon.area_live(1)
    assert live[linger["id"]]["state"] == "present" and live[count["id"]]["count_in"] == 1
    assert mon.area_live(9) == {}
    mon._running = False


def test_monitor_area_alert_is_verified_by_the_brain():
    area = new_area("presence", "polygon", rect(400, 200, 900, 700), name="Yard", alert=True)
    brain = FakeBrain(lambda seed: True)
    mon, store, det, clock, got = make_monitor("m_verify", [area], brain=brain, mode="verify")
    feed(mon, det, clock, [(600, 400, 680, 600)] * 10)
    alerts = store.list()
    assert len(alerts) == 1 and alerts[0].verdict == "pending" and alerts[0].rule == "area_presence"
    assert mon.brain_step()
    assert store.get(alerts[0].id).verdict == "confirmed" and brain.calls[0][1][0] == "person"
    mon._running = False


def test_monitor_presence_once_across_detection_gap():
    """A person walking (120 px/s, 180 px tall) is not detected for 2 s and comes back under a
    new track id: still one presence event, one alert."""
    area = new_area("presence", "polygon", rect(40, 40, 1240, 700), name="Yard", alert=True)
    mon, store, det, clock, got = make_monitor("m_gap", [area])
    boxes = []
    for i in range(50):
        x = 200 + 24 * i
        boxes.append(None if 10 <= i < 20 else (x, 300, x + 60, 480))
    feed(mon, det, clock, boxes)
    evs = [e.kind for e in store.list_area_events()]
    assert evs == ["presence"], evs
    assert len(store.list()) == 1
    mon._running = False


def test_monitor_vehicle_alert_asks_the_brain_for_that_vehicle():
    area = new_area("presence", "polygon", rect(200, 100, 1100, 700), name="Drive", alert=True,
                    objects=["motorcycle"])
    brain = FakeBrain(lambda seed: True)
    mon, store, det, clock, got = make_monitor("m_moto", [area], brain=brain, mode="verify")
    feed(mon, det, clock, [(500, 350, 700, 650)] * 10, cls=3)
    alerts = store.list()
    assert len(alerts) == 1 and alerts[0].verdict == "pending"
    assert mon.brain_step()
    assert brain.calls[0][1][0] == "motorcycle" and store.get(alerts[0].id).verdict == "confirmed"
    mon._running = False


def test_monitor_stop_unloads_plates():
    class Reader(FakeReader):
        unloaded = 0

        def unload(self):
            Reader.unloaded += 1

    gate = new_area("log_plates", "line", [[0.5, 0.0], [0.5, 1.0]], name="Barrier")
    mon, store, det, clock, got = make_monitor("m_unload", [gate])
    mon._plate_reader, mon._plate_state = Reader([]), "ok"
    mon._running = True
    mon._threads = []
    mon.stop()
    assert Reader.unloaded == 1


def test_monitor_ask_ai_lowest_priority():
    area = new_area("ask_ai", "polygon", rect(450, 300, 760, 620), name="Barrier",
                    params={"question": "barrier arm is raised", "interval_s": 30})
    state = {"yes": True}
    brain = FakeBrain(lambda seed: state["yes"] if seed == 0 else state.get("second", state["yes"]))
    mon, store, det, clock, got = make_monitor("m_ask", [area], brain=brain, mode="verify")
    feed(mon, det, clock, [None])
    with mon._jobs_lock:
        mon._pending["x"] = object()   # an alert waits for the brain: no question now
    assert not mon.area_ai_step() and not brain.calls
    with mon._jobs_lock:
        mon._pending.clear()
    assert mon.area_ai_step() and not mon.area_ai_step()   # one question, then the rate limit
    assert brain.calls[0][1] == ["barrier arm is raised"] and brain.calls[0][2:4] == (896, True)
    while mon.area_step():
        pass
    assert [e.kind for e in store.list_area_events()] == ["answer_yes"]
    # the answer changes, but the second sample disagrees: nothing logged
    state.update(yes=False, second=True)
    feed(mon, det, clock, [None] * 160, img=with_package(scene(1)))
    mon._last_area_ask = -1e18
    assert mon.area_ai_step() and len(brain.calls) == 3
    while mon.area_step():
        pass
    assert [e.kind for e in store.list_area_events()] == ["answer_yes"]
    # both samples agree: logged
    state.update(second=False)
    feed(mon, det, clock, [None] * 160)
    mon._last_area_ask = -1e18
    assert mon.area_ai_step()
    while mon.area_step():
        pass
    assert [e.kind for e in store.list_area_events()] == ["answer_no", "answer_yes"]
    assert mon.area_live(1)[area["id"]]["state"] == "no"
    mon._running = False


def test_monitor_plates_in_out_known_and_unreadable():
    gate = new_area("log_plates", "line", [[0.5, 0.0], [0.5, 1.0]], name="Barrier",
                    params={"min_plate_px": 80, "known": ["BB-777-CC"]}, alert=True)
    mon, store, det, clock, got = make_monitor("m_plates", [gate])
    reader = FakeReader(["AA-123-BB", "AA-128-BB", "BB-777-CC", None])
    mon._plate_reader, mon._plate_state = reader, "ok"

    def drive(xs):
        feed(mon, det, clock, [(x - 150, 400, x + 150, 600) for x in xs], cls=CAR)
        feed(mon, det, clock, [None] * 70, cls=CAR)  # gone: the next car is a new track

    drive(range(1000, 300, -40))          # in
    drive(range(300, 1000, 40))           # out, plate misread by one character
    drive(range(1000, 300, -40))          # a resident
    drive(range(1000, 300, -40))          # unreadable
    evs = list(reversed(store.list_area_events(kind="plate")))
    assert [e.value for e in evs] == ["AA-123-BB", "AA-128-BB", "BB-777-CC", ""], [e.value for e in evs]
    assert [e.extra["direction"] for e in evs] == ["in", "out", "in", "in"]
    assert evs[1].extra["stay_s"] > 0 and evs[1].extra["in_event_id"] == evs[0].id
    assert evs[2].extra["known"] and evs[3].extra["note"] == "plate unreadable" and evs[3].confidence == 0.0
    assert all(1 <= n <= A.PLATE_CANDIDATES for n in reader.calls)
    assert all(Path(e.crop_path).exists() for e in evs)
    # alerts for strangers only (the resident's plate is known)
    assert len(store.list()) == 3 and all(a.rule == "area_log_plates" for a in store.list())
    # plates.py missing: logged as unreadable
    mon._plate_reader, mon._plate_state = None, "missing"
    drive(range(1000, 300, -40))
    last = store.list_area_events(kind="plate", limit=1)[0]
    assert last.value == "" and last.extra["note"] == "plate unreadable"
    mon._running = False


# --- plates: best moments --------------------------------------------------------------------

_BG = cv2.GaussianBlur(np.random.default_rng(5).integers(40, 120, (H, W, 3), dtype=np.uint8), (0, 0), 3)
_BODY = cv2.GaussianBlur(np.random.default_rng(6).integers(0, 255, (80, 140, 3), dtype=np.uint8), (0, 0), 1.5)


def car_frame(x: float, y: float) -> np.ndarray:
    """The textured 140 x 80 "car" with its feet (bottom centre) at x, y on a textured yard."""
    f = _BG.copy()
    x1, y1 = int(round(x - 70)), int(round(y - 80))
    f[y1:y1 + 80, max(0, x1):x1 + 140] = _BODY[:, max(0, -x1):min(140, W - x1)]
    return f


def drive_by(ev, area, xs: list[float], every: int = 2, y: float = 400, t0: float = DAY):
    """8 frames / s at x positions; every `every`-th one is a detection, the rest go to between()
    (switched on here: it is off by default)."""
    hits = []
    on, A.PLATE_BETWEEN = A.PLATE_BETWEEN, True
    try:
        for i, x in enumerate(xs):
            ts, frame = t0 + i / 8, car_frame(x, y)
            if i % every == 0:
                hits += ev.update([area], [car(x, y, w=140, h=80)], frame, ts)
            else:
                ev.between([area], frame, ts)
    finally:
        A.PLATE_BETWEEN = on
    return hits


def test_plate_moments_over_the_whole_approach():
    """Pictures from PLATE_APPROACH box heights before the line until as far past it, also from frames
    between detections; the crossing is read once the car has left that band."""
    gate = new_area("log_plates", "line", LINE, name="Barrier")
    ev = A.AreaEvaluator(1)
    xs = [1200 - 50 * i for i in range(23)]  # 400 px/s = 5 box heights/s, right to left
    hits = drive_by(ev, gate, xs)
    assert kinds(hits) == ["plate"], kinds(hits)
    hit = hits[0]
    cross_ts = hit.event.ts
    assert hit.event.extra["direction"] == "in"
    st = ev._states[gate["id"]]
    assert not next(iter(st.lines.values())).cands  # handed over with the hit
    cands = hit.plate_cands
    assert 3 <= len(cands) <= A.PLATE_CANDIDATES and len(hit.plate_crops) == len(cands)
    assert [c.score for c in cands] == sorted((c.score for c in cands), reverse=True)
    assert any(c.ts < cross_ts - 0.5 for c in cands) and any(c.ts >= cross_ts - 0.2 for c in cands)
    assert any(c.box[2] < 640 for c in cands)  # wholly past the line
    assert any(not c.detected for c in cands)
    times = sorted(c.ts for c in cands)
    assert all(b - a >= A.PLATE_GAP_S - 1e-6 for a, b in zip(times, times[1:]))
    band = A.PLATE_APPROACH * 80
    for c in cands:
        fx = (c.box[0] + c.box[2]) / 2
        assert abs(fx - 640) <= band + 1 and c.frame.shape == (H, W, 3)
    # read when the car is beyond the band, after the crossing was counted
    last_detect = DAY + (len(xs) - 1) // 2 * 2 / 8
    assert cross_ts < last_detect and ev.live(last_detect)[gate["id"]]["count_in"] == 1


def test_plate_moment_boxes_follow_a_braking_car():
    """Between detections the box is moved on by the car's speed and then found by a template search:
    a car that stops at the barrier is not placed where its old speed would have taken it."""
    gate = new_area("log_plates", "line", LINE, name="Barrier")
    ev = A.AreaEvaluator(1)
    xs = [1000 - 50 * i for i in range(7)] + [700] * 3   # 400 px/s, stops dead at x = 700 at frame 6
    gap, A.PLATE_GAP_S = A.PLATE_GAP_S, 0.0  # keep every frame
    try:
        drive_by(ev, gate, xs)
    finally:
        A.PLATE_GAP_S = gap
    lt = next(iter(ev._states[gate["id"]].lines.values()))
    placed = [c for c in lt.cands if not c.detected]
    assert placed
    for c in placed:
        i = round((c.ts - DAY) * 8)
        true_x1 = xs[i] - 70
        assert abs(c.box[0] - true_x1) <= 3, (i, c.box[0], true_x1)
    after_stop = [c for c in placed if round((c.ts - DAY) * 8) == 7]
    assert after_stop  # the old speed said x = 650: the template found it still at 700


def test_plate_still_car_only_near_the_line():
    """A car parked within the band but away from the line is not collected (memory, CPU); one waiting
    right at the line is."""
    gate = new_area("log_plates", "line", LINE, name="Barrier")
    ev = A.AreaEvaluator(1)
    drive_by(ev, gate, [1000] * 12)           # 360 px = 4.5 box heights from the line, standing
    assert not next(iter(ev._states[gate["id"]].lines.values())).cands
    ev = A.AreaEvaluator(1)
    drive_by(ev, gate, [700] * 12)            # 60 px from the line, waiting
    assert next(iter(ev._states[gate["id"]].lines.values())).cands


def test_monitor_plate_best_moment_is_the_picture():
    """The reader picks the picture with the best plate: its moment becomes the event's picture and
    best_ts (here the farthest, earliest one), and the plate width is logged."""
    class Picky(FakeReader):
        def read(self, crops):
            self.calls.append(len(crops))
            i = min(range(len(crops)), key=lambda k: crops[k].shape[1] * 1000 + k)
            return {"text": "AA-123-BB", "confidence": 0.9, "plate_crop": crops[i][:20, :60].copy(), "index": i,
                    "plate_px": 91}

    gate = new_area("log_plates", "line", [[0.5, 0.0], [0.5, 1.0]], name="Barrier")
    mon, store, det, clock, got = make_monitor("m_best", [gate])
    mon._plate_reader, mon._plate_state = Picky([]), "ok"
    # coming in from the right and growing: the first pictures are the narrowest
    feed(mon, det, clock, [(x - 100 - (1000 - x) // 10, 400, x + 100 + (1000 - x) // 10, 600)
                           for x in range(1000, 300, -40)], cls=CAR)
    feed(mon, det, clock, [None] * 20, cls=CAR)
    ev = store.list_area_events(kind="plate")[0]
    assert ev.value == "AA-123-BB" and ev.extra["plate_px"] == 91
    assert ev.extra["best_ts"] < ev.ts - 0.3, (ev.extra["best_ts"], ev.ts)
    assert Path(ev.snapshot_path).exists() and Path(ev.crop_path).exists()
    assert "best_offset_s" not in ev.extra  # clips are off
    mon._running = False


def test_monitor_full_resolution_for_plate_cameras():
    """Cameras with a switched-on plate line get native frames (up to plate_long_side, never below the
    detection size); the others keep the detection size. Adding or switching off the line restarts."""
    events = OUT / "m_res"
    shutil.rmtree(events, ignore_errors=True)
    gate = config.normalize_area(new_area("log_plates", "line", [[0.5, 0.0], [0.5, 1.0]], name="Barrier"))
    yard = config.normalize_area(new_area("count", "line", [[0.5, 0.0], [0.5, 1.0]], name="Yard"))
    cams = [Camera(1, "Gate", "Gate"), Camera(2, "Yard", "Yard")]
    cams[0].areas, cams[1].areas = [gate], [yard]
    site = Site(name="Test", host="127.0.0.1", cameras=cams)
    cfg = Config(sites=[site], active_site_id=site.id)
    cfg.settings = Settings(events_dir=str(events), clips_enabled=False, brain_mode="off", detect_size=1280)
    FakeSource.instances.clear()
    mon = Monitor(cfg, AlertStore(str(events)), None, detector=FakeDetector(), source_factory=FakeSource,
                  clock=Clock(DAY))
    mon._setup()
    assert FakeSource.instances[1].long_side == 1920 and FakeSource.instances[2].long_side == 1280
    assert not mon.needs_restart()
    gate["enabled"] = False
    assert mon.needs_restart()
    mon._setup()
    assert FakeSource.instances[1].long_side == 1280
    gate["enabled"] = True
    cfg.settings.plate_long_side = 960  # never below the detection size
    mon._setup()
    assert FakeSource.instances[1].long_side == 1280
    cfg.settings.plate_long_side = 2560
    cams[1].areas.append(config.normalize_area(new_area("log_plates", "line", [[0.2, 0.0], [0.2, 1.0]])))
    assert mon.needs_restart()
    mon._setup()
    assert FakeSource.instances[1].long_side == 2560 and FakeSource.instances[2].long_side == 2560


def test_monitor_cleanup_includes_area_log():
    area = new_area("presence", "polygon", rect(400, 200, 900, 700))
    mon, store, det, clock, got = make_monitor("m_cleanup", [area])
    store.add_area_event(_event(area, time.time() - 90 * 86400, "presence"))
    store.add_area_event(_event(area, time.time() - 60, "presence"))
    mon.cleanup_clips()
    assert len(store.list_area_events()) == 1
    mon._running = False


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    t0 = time.perf_counter()
    for name, fn in tests:
        try:
            t1 = time.perf_counter()
            fn()
            print(f"PASS {name} ({time.perf_counter() - t1:.1f} s)")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {time.perf_counter() - t0:.1f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
