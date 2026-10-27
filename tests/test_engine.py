"""Deterministic tests for yardguard.engine: synthetic boxes, a fake clock, fake camera,
detector and brain. Run with the main venv:  .venv\\Scripts\\python.exe tests\\test_engine.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "engine"
os.environ.setdefault("YARDGUARD_CONFIG", str(OUT / "config.json"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from yardguard import engine  # noqa: E402
from yardguard.config import Camera, Config, Settings, Site  # noqa: E402
from yardguard.engine import (Alert, AlertStore, CameraPipeline, Monitor, Tracker, iou, is_night,  # noqa: E402
                              point_in_polygon)

PERSON, CAR, TRUCK = 0, 2, 7
W, H = 1280, 720
NIGHT = datetime(2026, 10, 23, 1, 0, 0).timestamp()
DAY = datetime(2026, 10, 23, 14, 0, 0).timestamp()
LEFT_HALF = [{"name": "Parking", "points": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]}]


def det(x1, y1, x2, y2, cls, score=0.9):
    return [x1, y1, x2, y2, score, cls]


def dets(*rows):
    return np.array(rows, np.float32).reshape(-1, 6)


def settings(**kw) -> Settings:
    s = Settings(events_dir=str(OUT / "events"), clips_enabled=False)  # clips: tests/test_clips.py
    s.night_start, s.night_end = "00:00", "06:00"
    s.rules_when = "always"  # these tests check the rule logic itself; the schedule has its own test
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def run(pipe: CameraPipeline, frames, s, zones=None):
    """frames: iterable of (ts, dets). Returns all hits."""
    hits = []
    for ts, d in frames:
        hits += pipe.process(d, ts, (W, H), zones, s)
    return hits


def rules(hits) -> list[str]:
    return [h.rule for h in hits]


# --- geometry / time -------------------------------------------------------------

def test_geometry():
    assert abs(iou([0, 0, 10, 10], [5, 0, 15, 10]) - 1 / 3) < 1e-6
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert point_in_polygon(5, 5, square) and not point_in_polygon(15, 5, square)
    tri = [(0, 0), (10, 0), (0, 10)]
    assert point_in_polygon(2, 2, tri) and not point_in_polygon(8, 8, tri)
    assert engine.is_near([100, 0, 120, 50], [130, 0, 230, 50])  # gap 10 < 0.5 * 100
    assert not engine.is_near([0, 0, 20, 50], [130, 0, 230, 50])  # gap 110


def test_night_window_across_midnight():
    def at(h, m):
        return datetime(2026, 10, 23, h, m).timestamp()
    assert is_night(at(23, 30), "23:00", "06:00")
    assert is_night(at(0, 0), "23:00", "06:00")
    assert is_night(at(5, 59), "23:00", "06:00")
    assert not is_night(at(6, 0), "23:00", "06:00")
    assert not is_night(at(12, 0), "23:00", "06:00")
    assert not is_night(at(22, 59), "23:00", "06:00")
    assert is_night(at(3, 0), "00:00", "06:00") and not is_night(at(7, 0), "00:00", "06:00")
    assert is_night(at(12, 0), "08:00", "08:00")  # same start and end = all day
    assert is_night(at(1, 0), "bad", "06:00")      # broken text falls back to defaults


# --- tracker ----------------------------------------------------------------------

def test_tracker_stable_ids():
    tr = Tracker()
    t0 = 1000.0
    for i in range(20):  # person walking 25 px per 0.2 s, a car standing still
        tr.update(dets(det(100 + 25 * i, 300, 160 + 25 * i, 460, PERSON), det(800, 400, 1000, 500, CAR)), t0 + 0.2 * i)
    ids = {t.group: t.id for t in tr.tracks}
    assert len(tr.tracks) == 2, tr.tracks
    # one missed second: same id; car flips to truck: same vehicle track
    tr.update(dets(det(800, 400, 1000, 500, TRUCK)), t0 + 4.2)
    tr.update(dets(det(100 + 25 * 26, 300, 160 + 25 * 26, 460, PERSON), det(800, 400, 1000, 500, CAR)), t0 + 5.2)
    assert {t.group: t.id for t in tr.tracks} == ids
    # person gone for longer than the max age: a new id
    tr.update(dets(det(800, 400, 1000, 500, CAR)), t0 + 9.0)
    tr.update(dets(det(900 - 400, 300, 960 - 400, 460, PERSON), det(800, 400, 1000, 500, CAR)), t0 + 9.2)
    person = next(t for t in tr.tracks if t.group == "person")
    assert person.id != ids["person"] and not person.confirmed
    n = len(tr.tracks)
    tr.update(dets(det(10, 10, 50, 50, 16), det(800, 400, 1000, 500, CAR)), t0 + 9.4)  # class 16 (dog): ignored
    assert len(tr.tracks) == n


def test_tracker_two_people_cross():
    tr = Tracker()
    for i in range(15):  # two people walking towards each other on different rows
        tr.update(dets(det(100 + 40 * i, 200, 160 + 40 * i, 360, PERSON),
                       det(1000 - 40 * i, 400, 1060 - 40 * i, 560, PERSON)), 50 + 0.2 * i)
    a = min(tr.tracks, key=lambda t: t.box[1])
    assert len(tr.tracks) == 2 and a.id == 1 and a.box[0] > 600  # the top one kept id 1 to the end


# --- rules ------------------------------------------------------------------------

def walk(t0, n, step, box, cls=PERSON, extra=()):
    """n detections every `step` s of a fixed box (plus extra rows)."""
    return [(t0 + i * step, dets(det(*box, cls), *extra)) for i in range(n)]


def test_zone_filtering_and_night_presence():
    s = settings()
    pipe = CameraPipeline(1, "Yard", "Parking")
    hits = run(pipe, walk(NIGHT, 20, 0.2, (900, 300, 960, 460)), s, LEFT_HALF)  # right half: outside
    assert hits == [], hits
    pipe = CameraPipeline(1, "Yard", "Parking")
    early = run(pipe, walk(NIGHT, 10, 0.2, (200, 300, 260, 460)), s, LEFT_HALF)  # 1.8 s: too short
    assert early == []
    late = run(pipe, walk(NIGHT + 2.0, 5, 0.2, (200, 300, 260, 460)), s, LEFT_HALF)
    assert rules(late) == ["night_presence"], rules(late)
    assert late[0].severity == "warning" and "Parking" in late[0].detail and late[0].night
    # feet decide: a box mostly in the zone but standing outside does not count
    pipe = CameraPipeline(1)
    hits = run(pipe, walk(NIGHT, 20, 0.2, (500, 300, 700, 460)), s, LEFT_HALF)  # foot x = 600 > 640? no: 600 < 640
    assert rules(hits) == ["night_presence"]
    pipe = CameraPipeline(1)
    hits = run(pipe, walk(NIGHT, 20, 0.2, (560, 300, 800, 460)), s, LEFT_HALF)  # foot x = 680: outside
    assert hits == []
    # no zones = whole picture
    pipe = CameraPipeline(1)
    hits = run(pipe, walk(NIGHT, 20, 0.2, (900, 300, 960, 460)), s, [])
    assert rules(hits) == ["night_presence"] and "whole view" in hits[0].detail


def test_daytime_has_no_night_presence():
    s = settings()
    pipe = CameraPipeline(1)
    assert run(pipe, walk(DAY, 40, 0.2, (200, 300, 260, 460)), s, LEFT_HALF) == []
    s = settings(night_start="23:00", night_end="06:00", rule_night_presence=True)
    pipe = CameraPipeline(1)
    assert rules(run(pipe, walk(datetime(2026, 10, 23, 23, 30).timestamp(), 20, 0.2, (200, 300, 260, 460)), s)) \
        == ["night_presence"]
    s = settings(rule_night_presence=False)
    pipe = CameraPipeline(1)
    assert run(pipe, walk(NIGHT, 20, 0.2, (200, 300, 260, 460)), s) == []


def test_loitering():
    s = settings(loiter_seconds=30, rule_night_presence=False)
    pipe = CameraPipeline(1)
    inside, outside = (200, 300, 260, 460), (700, 300, 760, 460)
    frames = walk(DAY, 50, 0.5, inside)                     # 0 .. 24.5 s in the zone
    frames += walk(DAY + 25, 4, 0.5, outside)               # 2 s outside: grace keeps the timer
    frames += walk(DAY + 27, 5, 0.5, inside)                # 27 .. 29 s
    assert run(pipe, frames, s, LEFT_HALF) == []
    hits = run(pipe, walk(DAY + 29.5, 3, 0.5, inside), s, LEFT_HALF)
    assert rules(hits) == ["loitering"] and hits[0].severity == "info", hits
    assert "30 s" in hits[0].detail
    # leaving for longer than the grace resets the timer
    s2 = settings(loiter_seconds=10, rule_night_presence=False)
    pipe = CameraPipeline(1)
    frames = walk(DAY, 16, 0.5, inside) + walk(DAY + 8, 10, 0.5, outside) + walk(DAY + 13, 16, 0.5, inside)
    assert run(pipe, frames, s2, LEFT_HALF) == []
    # at night it is a warning
    pipe = CameraPipeline(1)
    hits = run(pipe, walk(NIGHT, 25, 0.5, inside), s2, LEFT_HALF)
    assert rules(hits) == ["loitering"] and hits[0].severity == "warning"


CARS = [(100, 400, 300, 520), (400, 400, 600, 520), (700, 400, 900, 520), (1000, 400, 1200, 520)]


def parked(extra_cars=CARS):
    return [det(*c, CAR) for c in extra_cars]


def test_prowling():
    s = settings(rule_night_presence=False, rule_loitering=False, prowl_cars=3, prowl_window_s=180)
    pipe = CameraPipeline(5, "Street side", "Parking")
    t = NIGHT
    frames = [(t + i, dets(*parked())) for i in range(12)]  # cars standing still for 11 s
    t += 12
    x = CARS[0][2] + 10
    for car in CARS[:3]:  # walks to each car, stops 2.4 s next to it
        target = car[2] + 10
        while x < target:
            x = min(target, x + 30)
            frames.append((t, dets(det(x, 380, x + 50, 520, PERSON), *parked())))
            t += 0.2
        frames += [(t + i * 0.3, dets(det(x, 380, x + 50, 520, PERSON), *parked())) for i in range(9)]
        t += 2.7
    hits = run(pipe, frames, s)
    assert rules(hits) == ["prowling"], rules(hits)
    assert hits[0].severity == "warning" and "3 different" in hits[0].detail
    assert len(hits[0].context) >= 1


def test_walking_past_cars_is_not_prowling():
    s = settings(rule_night_presence=False, rule_loitering=False, prowl_cars=3)
    pipe = CameraPipeline(5)
    frames = [(NIGHT + i, dets(*parked())) for i in range(12)]
    t = NIGHT + 12
    for i in range(50):  # normal walking pace (~0.8 body heights / s) along the whole row
        frames.append((t + i * 0.2, dets(det(100 + 22 * i, 380, 150 + 22 * i, 520, PERSON), *parked())))
    assert run(pipe, frames, s) == []


def test_standing_between_cars_is_not_prowling():
    # people chatting or loading between two parked cars are near both at once (11:17-11:29 alerts)
    s = settings(rule_night_presence=False, rule_loitering=False, prowl_cars=2)
    pipe = CameraPipeline(5)
    frames = [(NIGHT + i, dets(*parked())) for i in range(12)]
    frames += [(NIGHT + 12 + i * 0.2, dets(det(320, 380, 370, 520, PERSON), *parked())) for i in range(50)]
    assert run(pipe, frames, s) == []


def test_prowling_ignores_driving_cars():
    s = settings(rule_night_presence=False, rule_loitering=False, prowl_cars=2)
    pipe = CameraPipeline(5)
    frames = []
    for i in range(40):  # cars keep moving: never "parked"
        moving = [det(c[0] + 15 * i, c[1], c[2] + 15 * i, c[3], CAR) for c in CARS[:2]]
        frames.append((NIGHT + i * 0.3, dets(det(320 + 15 * i, 380, 370 + 15 * i, 520, PERSON), *moving)))
    assert run(pipe, frames, s) == []


def _park(pipe, s, t0, box=(400, 400, 600, 520), seconds=305, extra=()):
    frames = [(t0 + i, dets(det(box[0] + (i % 3) - 1, box[1], box[2] + (i % 2), box[3], CAR), *extra))
              for i in range(seconds)]  # +-1 px jitter like a real detector
    return run(pipe, frames, s)


def only_cars(**kw):
    return settings(rule_night_presence=False, rule_loitering=False, rule_prowling=False, **kw)


def test_car_moved():
    s = only_cars()
    pipe = CameraPipeline(1, "Yard", "Parking")
    assert _park(pipe, s, DAY) == []
    assert len(pipe.parked) == 1
    t = DAY + 305
    # a person stands right next to the car, then the car shifts by 30% of its width
    frames = [(t + i * 0.5, dets(det(400, 400, 600, 520, CAR), det(580, 360, 630, 515, PERSON))) for i in range(4)]
    frames += [(t + 2 + i * 0.5, dets(det(460, 400, 660, 520, CAR))) for i in range(6)]
    hits = run(pipe, frames, s)
    assert rules(hits) == ["car_moved"], rules(hits)
    h = hits[0]
    assert h.title == "Parked car moved" and "person" in h.detail and h.severity == "info"
    assert pipe.parked == []


def test_parked_car_jitter_and_occlusion_are_quiet():
    s = only_cars()
    pipe = CameraPipeline(1)
    _park(pipe, s, NIGHT)
    t = NIGHT + 305
    # 10 minutes of jitter
    frames = [(t + i, dets(det(400 + (i % 5) - 2, 400 + (i % 3), 600 - (i % 4), 520, CAR))) for i in range(600)]
    # a person stands in front: the car box gets cut in half (shape change, not a move)
    t += 600
    frames += [(t + i * 0.5, dets(det(400, 400, 500, 520, CAR), det(480, 380, 560, 540, PERSON))) for i in range(20)]
    assert run(pipe, frames, s) == []
    # a van parks in front and hides it completely for 2 minutes, then leaves
    t += 10
    frames = [(t + i, dets(det(380, 380, 620, 540, TRUCK))) for i in range(120)]
    frames += [(t + 120 + i, dets(det(400, 400, 600, 520, CAR))) for i in range(5)]
    hits = run(pipe, frames, s)
    assert hits == [], [h.title for h in hits]


def _noisy_night_car(t0, seconds, box=(400, 400, 600, 520), rng=None, step=0.25):
    """A parked car as YOLO reports it on noisy night video: boxes jitter by up to +-12 px, the
    score swings around the 0.4 certainty, a quarter of frames miss it, and a shifted second box
    sometimes appears on top of it."""
    rng = rng or np.random.default_rng(1)
    frames = []
    for i in range(int(seconds / step)):
        rows = []
        if rng.random() > 0.25:
            j = rng.integers(-12, 13, 4)
            rows.append(det(box[0] + j[0], box[1] + j[1], box[2] + j[2], box[3] + j[3], CAR,
                            score=float(rng.uniform(0.15, 0.7))))
            if rng.random() < 0.15:
                rows.append(det(box[0] + 20, box[1] + 8, box[2] + 22, box[3] + 6, CAR, score=0.45))
        frames.append((t0 + i * step, dets(*rows)))
    return frames


def test_daytime_lingering_is_quiet_unless_always():
    # A busy yard: people stand by the cars for minutes in the afternoon. By default no alert;
    # with rules_when="always" the same scene alerts; at night it alerts either way.
    frames = [(t, dets(det(300, 380, 350, 520, PERSON))) for t in np.arange(0, 150, 0.5)]
    for when, t0, expect in (("night", DAY, []), ("always", DAY, ["loitering"]), ("night", NIGHT, ["loitering"])):
        s = settings(rule_night_presence=False, rule_prowling=False, rule_car_moved=False,
                     loiter_seconds=120, rules_when=when)
        pipe = CameraPipeline(1, "Yard", "Parking")
        got = rules(run(pipe, [(t0 + t, d) for t, d in frames], s))
        assert got == expect, (when, t0 == NIGHT, got)


def test_old_lingering_default_migrates_once():
    from yardguard.config import _migrate_settings
    old = Settings(loiter_seconds=30, config_version=2)
    _migrate_settings(old)
    assert old.loiter_seconds == 120 and old.rules_when == "night"
    chosen = Settings(loiter_seconds=45, config_version=2)
    _migrate_settings(chosen)
    assert chosen.loiter_seconds == 45
    again = Settings(loiter_seconds=30, config_version=3)  # set to 30 on purpose after the migration
    _migrate_settings(again)
    assert again.loiter_seconds == 30


def test_noisy_night_parked_car_is_one_steady_track():
    s = only_cars(confidence=0.4)
    pipe = CameraPipeline(1)
    run(pipe, _noisy_night_car(NIGHT, 60), s)
    vehicles = [t for t in pipe.tracker.tracks if t.group == "vehicle"]
    assert len(vehicles) == 1, f"{len(vehicles)} tracks for one car"
    assert next(pipe.tracker._ids) <= 3, "the noisy car kept being re-created as new objects"
    v = vehicles[0]
    assert np.abs(v.box - np.array([400, 400, 600, 520])).max() < 10, v.box
    assert v.still_for(NIGHT + 60) > 30, "jitter kept resetting the car's stillness"


def test_noisy_night_parked_car_raises_no_alerts():
    s = settings(confidence=0.4)  # all rules on, whole frame watched, at night
    pipe = CameraPipeline(1, "Yard", "Parking")
    hits = run(pipe, _noisy_night_car(NIGHT, 20 * 60), s)  # 20 minutes, parked rule armed after 5
    assert hits == [], [h.title for h in hits]
    assert len(pipe.parked) == 1, "the noisy car never counted as parked"


def test_weak_detections_never_start_objects():
    s = settings(confidence=0.4)
    pipe = CameraPipeline(1, "Yard", "Parking")
    # a ghost "person" at 0.2-0.35 for a whole minute at night: must not exist, must not alert
    frames = [(NIGHT + i * 0.25, dets(det(700, 300, 760, 460, PERSON, score=0.2 + 0.15 * (i % 2))))
              for i in range(240)]
    assert run(pipe, frames, s) == []
    assert pipe.tracker.tracks == []


def test_parked_car_gone_after_contact():
    s = only_cars()
    pipe = CameraPipeline(1)
    _park(pipe, s, NIGHT)
    t = NIGHT + 305
    # another vehicle touches the parked one, then the parked car is no longer detected
    frames = [(t + i * 0.5, dets(det(400, 400, 600, 520, CAR), det(560, 410, 800, 530, CAR))) for i in range(4)]
    frames += [(t + 2 + i, dets(det(900, 410, 1140, 530, CAR))) for i in range(40)]
    hits = run(pipe, frames, s)
    assert rules(hits) == ["car_moved"], rules(hits)
    assert "another vehicle" in hits[0].title and hits[0].severity == "warning"


def test_parked_car_leaving_without_contact_is_quiet():
    s = only_cars()
    pipe = CameraPipeline(1)
    _park(pipe, s, DAY)
    frames = [(DAY + 305 + i, dets()) for i in range(60)]
    assert run(pipe, frames, s) == [] and pipe.parked == []


def test_cooldown():
    s = settings(alert_cooldown_s=120, rule_loitering=False)
    pipe = CameraPipeline(1)
    box = (200, 300, 260, 460)
    hits = run(pipe, walk(NIGHT, 300, 0.5, box), s)  # 150 s in view: one alert, then one after the cooldown
    assert rules(hits) == ["night_presence", "night_presence"], rules(hits)
    assert hits[1].key == hits[0].key
    # ID switch: track lost for 4 s, comes back as a new id at the same place -> no new alert
    pipe = CameraPipeline(1)
    hits = run(pipe, walk(NIGHT, 10, 0.5, box), s)
    hits += run(pipe, [(NIGHT + 9 + i * 0.5, dets()) for i in range(1)], s)
    hits += run(pipe, walk(NIGHT + 13, 10, 0.5, box), s)
    assert rules(hits) == ["night_presence"], rules(hits)
    # a second person elsewhere is a separate alert
    hits = run(pipe, walk(NIGHT + 18, 10, 0.5, (900, 300, 960, 460), extra=[det(*box, PERSON)]), s)
    assert rules(hits) == ["night_presence"]


# --- monitor with fakes -------------------------------------------------------------

class FakeSource:
    instances: dict[int, "FakeSource"] = {}

    def __init__(self, url, fps, long_side=1280, gpu_decode=True):
        self.channel = int(url.split("channel=")[1].split("&")[0])
        self.item = None
        self.started = self.stopped = False
        self.backend, self.error, self.alive, self.size = "fake", None, True, (W, H)
        FakeSource.instances[self.channel] = self

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def latest(self):
        return self.item

    def push(self, ts, moving=True):
        frame = np.full((H, W, 3), 60, np.uint8)
        if moving:
            frame[:, :, 1] = np.uint8(int(ts * 37) % 200)
        frame[0, 0, 0] = self.channel  # lets FakeDetector know which camera it sees
        self.item = (frame, ts)


class FakeDetector:
    backend = "fake"

    def __init__(self):
        self.dets: dict[int, np.ndarray] = {}
        self.calls = 0
        self.batch_sizes = []

    def predict(self, frames, conf):
        self.calls += 1
        self.batch_sizes.append(len(frames))
        return [self.dets.get(int(f[0, 0, 0]), dets()) for f in frames]


class FakeBrain:
    """answer: list of boxes, or a function(image, phrases, seed) -> boxes. Boxes are in pixels of
    the picture asked about (the verify crop); only asked phrases are answered, as the real worker."""

    def __init__(self, state="ready", answer=None):
        self.state = state
        self.answer = answer or []
        self.calls = []
        self.started = 0
        self.user_stopped = False

    def status(self):
        return self.state

    def start(self, user=False):
        self.started += 1
        if user:
            self.user_stopped = False

    def stop(self, user=False):
        self.state = "stopped"
        if user:
            self.user_stopped = True

    def ask(self, image, phrases, mode="fast", timeout=60, max_side=896, upscale=False, seed=0):
        self.calls.append((image.shape, list(phrases), mode, max_side, upscale, seed))
        boxes = self.answer(image, phrases, seed) if callable(self.answer) else self.answer
        return [dict(b) for b in boxes if b["label"] in phrases]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_monitor(tmp: str, brain=None, mode="verify", **kw):
    events = OUT / tmp
    shutil.rmtree(events, ignore_errors=True)
    site = Site(name="Test", host="127.0.0.1", cameras=[Camera(1, "Yard parking", "Parking", zones=LEFT_HALF),
                                                       Camera(5, "Street", "Parking"),
                                                       Camera(7, "Hallway", "Indoor", monitor=False)])
    cfg = Config(sites=[site], active_site_id=site.id)
    cfg.settings = settings(brain_mode=mode, **kw)
    cfg.settings.events_dir = str(events)
    store = AlertStore(str(events))
    det_ = FakeDetector()
    clock = Clock(NIGHT)
    got = {"added": [], "updated": []}
    mon = Monitor(cfg, store, brain, on_alert=got["added"].append, on_alert_update=got["updated"].append,
                  detector=det_, source_factory=FakeSource, clock=clock)
    FakeSource.instances.clear()
    mon._setup()
    return mon, store, det_, clock, got


def feed(mon, det_, clock, channel, box, seconds, step=0.2, cls=PERSON):
    src = FakeSource.instances[channel]
    for i in range(int(seconds / step)):
        clock.t += step
        det_.dets[channel] = dets(det(*box, cls))
        src.push(clock.t)
        mon.step()


def test_monitor_cameras_and_batching():
    mon, store, det_, clock, _ = make_monitor("m_batch")
    assert sorted(FakeSource.instances) == [1, 5]  # camera 7 is not monitored
    for ch in (1, 5):
        FakeSource.instances[ch].push(clock.t + 0.1)
    clock.t += 0.1
    assert mon.step() == 2 and det_.batch_sizes == [2]  # one batched call for both cameras
    assert mon.step() == 0  # no new frames: nothing to do
    st = mon.camera_status()
    assert st[1]["state"] == "watching" and st[1]["backend"] == "fake" and st[5]["fps"] > 0
    clock.t += 20  # frames are now old
    mon.step()
    assert mon.camera_status()[1]["state"] == "offline"


def test_motion_gating_keepalive():
    mon, store, det_, clock, _ = make_monitor("m_gate", motion_gating=True)
    src = FakeSource.instances[1]
    FakeSource.instances[5].item = None
    for i in range(50):  # 10 s of a still picture at 5 fps
        clock.t += 0.2
        src.push(clock.t, moving=False)
        mon.step()
    # first frame + keep-alive every 4 s, not 50 calls
    assert 2 <= det_.calls <= 4, det_.calls
    assert mon.camera_status()[1]["state"] == "idle"
    # a person was detected: keep detecting every frame even without motion
    det_.dets[1] = dets(det(200, 300, 260, 460, PERSON))
    calls = det_.calls
    clock.t += 4.1
    src.push(clock.t, moving=False)
    mon.step()
    for i in range(10):
        clock.t += 0.2
        src.push(clock.t, moving=False)
        mon.step()
    assert det_.calls - calls == 11, det_.calls - calls


PERSON_BOX = (200, 300, 260, 460)   # the person the verify tests feed (inside LEFT_HALF, 160 px tall)


def brain_box(label, box, subject=PERSON_BOX):
    """A brain answer box given in FRAME pixels, as the fake brain returns it: in pixels of the verify
    crop around the subject (the real Brain.ask answers in pixels of the picture it was given)."""
    x0, y0, _, _ = engine.verify_region(subject, W, H)
    return {"label": label, "x1": box[0] - x0, "y1": box[1] - y0, "x2": box[2] - x0, "y2": box[3] - y0,
            "score": None}


def verify_run(tmp, answer, seconds=3, box=PERSON_BOX, **kw):
    """Night presence of a person standing at `box`, then one brain check."""
    brain = FakeBrain("ready", answer)
    mon, store, det_, clock, got = make_monitor(tmp, brain, **kw)
    feed(mon, det_, clock, 1, box, seconds)
    assert mon.brain_step()
    return mon, store, brain, got


def test_verdict_confirmed():
    hand = (235, 360, 255, 385)   # an open door right next to the person (a thing, not a pose)
    answer = [brain_box("person", PERSON_BOX), brain_box("open car door", hand)]
    mon, store, brain, got = verify_run("m_confirm", answer)
    assert len(got["added"]) == 1
    a = got["added"][0]
    assert a.rule == "night_presence" and a.camera == "Yard parking" and a.boxes[0]["role"] == "subject"
    assert Path(a.snapshot_path).exists() and Path(a.crop_path).exists() and "alerts" in a.snapshot_path
    assert not Path(engine.raw_path(a.snapshot_path)).exists()  # the RAW copy goes once checked
    assert not mon.brain_step()
    # first picture: person + benign twins + the first 5 behaviours, then the 6th; later: only what was seen
    prompts = list(mon.cfg.settings.brain_prompts)
    asked = [c[1] for c in brain.calls]
    assert asked[0] == ["person", "person walking", "person standing", *prompts[:5]], asked[0]
    assert asked[1] == ["person", "person walking", "person standing", *prompts[5:]]
    assert asked[2] == ["person", "person walking", "person standing", "open car door"]
    assert len(asked) == 3  # 2 of 2 pictures agree: no third one needed
    assert all(c[2] == "fast" and c[3] == 896 and c[4] for c in brain.calls)  # zoomed crop, fast mode
    assert brain.calls[0][0][0] < H  # a crop, not the whole frame
    b = store.get(a.id)
    assert b.verdict == "confirmed" and b.severity == "critical", (b.verdict, b.severity)
    assert b.brain_findings == ["A real person is there (2 of 2 pictures)",
                                "Open car door (seen in 2 of 2 pictures)"], b.brain_findings
    box = next(x for x in b.boxes if x["role"] == "brain")
    assert abs(box["x1"] - hand[0]) < 1 and abs(box["y1"] - hand[1]) < 1  # back in snapshot pixels
    assert got["updated"] and got["updated"][-1].verdict == "confirmed"


def test_daytime_behaviour_is_reported_not_escalated():
    hand = (235, 360, 255, 385)
    brain = FakeBrain("ready", [brain_box("person", PERSON_BOX), brain_box("hand on a car door handle", hand)])
    mon, store, det_, clock, got = make_monitor("m_day", brain, loiter_seconds=5, rule_night_presence=False)
    clock.t = DAY
    feed(mon, det_, clock, 1, PERSON_BOX, 7)
    assert [a.rule for a in got["added"]] == ["loitering"] and got["added"][0].severity == "info"
    mon.brain_step()
    b = store.get(got["added"][0].id)
    assert b.verdict == "confirmed" and b.severity == "info" and "hand on a car door handle" in b.brain_findings[1]


def test_pose_phrases_are_hints_never_critical():
    # On 43 real alerts the model "saw" person-pose phrases on people doing nothing suspicious:
    # at night they are listed as a hint, but the alert keeps its own severity and gets no brain box.
    answer = [brain_box("person", PERSON_BOX), brain_box("person looking into a car window", PERSON_BOX),
              brain_box("hand on a car door handle", (235, 360, 255, 385))]
    mon, store, brain, got = verify_run("m_pose", answer)
    b = store.get(got["added"][0].id)
    assert b.verdict == "confirmed" and b.severity == "warning", (b.verdict, b.severity)
    assert any(f.startswith("Maybe: ") for f in b.brain_findings), b.brain_findings
    assert not [x for x in b.boxes if x["role"] == "brain"]
    assert engine.is_pose_phrase("person crouching next to a car") and engine.is_pose_phrase("a man at the gate")
    assert not engine.is_pose_phrase("open car door") and not engine.is_pose_phrase("broken car window")


def test_verdict_real_person_without_behaviour():
    # the recall fix: a real person who does nothing listed is confirmed (not "rejected") at the
    # rule's own severity
    mon, store, brain, got = verify_run("m_plain", [brain_box("person", PERSON_BOX),
                                                    brain_box("person standing", PERSON_BOX)])
    b = store.get(got["added"][0].id)
    assert b.verdict == "confirmed" and b.severity == "warning" and len(b.brain_findings) == 1
    assert not [x for x in b.boxes if x["role"] == "brain"]


def test_verdict_rejected_unverified_and_dedupe():
    brain = FakeBrain("ready", [])
    mon, store, det_, clock, got = make_monitor("m_reject", brain, loiter_seconds=5)
    feed(mon, det_, clock, 1, PERSON_BOX, 6)  # night presence + loitering, same person
    assert sorted(a.rule for a in got["added"]) == ["loitering", "night_presence"]
    assert mon._jobs.qsize() == 1  # both alerts wait on one brain check
    mon.brain_step()
    # nobody there in the first two pictures: no third needed
    assert len({c[5] for c in brain.calls}) <= 2 and len(brain.calls) == 3, brain.calls
    assert all(store.get(a.id).verdict == "rejected" for a in got["added"])
    assert all(store.get(a.id).severity == "warning" for a in got["added"])
    assert store.get(got["added"][0].id).brain_findings[0].startswith("No person found")

    brain = FakeBrain("not_installed")
    mon, store, det_, clock, got = make_monitor("m_unverified", brain)
    feed(mon, det_, clock, 1, PERSON_BOX, 3)
    mon.brain_step()
    assert store.get(got["added"][0].id).verdict == "unverified" and not brain.calls

    mon, store, det_, clock, got = make_monitor("m_off", FakeBrain(), mode="off")
    feed(mon, det_, clock, 1, PERSON_BOX, 3)
    assert got["added"][0].verdict == "unverified" and mon._jobs.qsize() == 0
    assert not Path(engine.raw_path(got["added"][0].snapshot_path)).exists()  # nothing kept to verify


def test_verdict_person_elsewhere_is_rejected():
    # the brain finds a person, but not where the alert is: the detector's "person" was a post or shadow
    mon, store, brain, got = verify_run("m_elsewhere", [brain_box("person", (560, 300, 620, 460))])
    assert store.get(got["added"][0].id).verdict == "rejected"


def test_behaviour_off_subject_or_same_as_benign_is_no_evidence():
    # 05:22 and 11:03 in the audit: "crouching" boxed on a standing man (the same box as "person
    # standing"), "broken car window" on another car far away
    answer = [brain_box("person", PERSON_BOX), brain_box("person standing", PERSON_BOX),
              brain_box("person crouching next to a car", (201, 301, 259, 459)),
              brain_box("broken car window", (900, 380, 980, 430))]
    mon, store, brain, got = verify_run("m_contrast", answer)
    b = store.get(got["added"][0].id)
    assert b.verdict == "confirmed" and b.severity == "warning" and len(b.brain_findings) == 1, b.brain_findings


def test_behaviour_needs_two_pictures():
    # seen in the first picture only (sampling noise): no escalation
    def answer(image, phrases, seed):
        boxes = [brain_box("person", PERSON_BOX)]
        if len(phrases) > 4:  # only the first picture asks all behaviours
            boxes.append(brain_box("open car door", (265, 380, 300, 450)))
        return boxes

    mon, store, brain, got = verify_run("m_votes", answer)
    b = store.get(got["added"][0].id)
    assert b.verdict == "confirmed" and b.severity == "warning", (b.severity, b.brain_findings)
    assert len(brain.calls) == 4  # 2 calls for picture 1, then pictures 2 and 3 for the open vote


def test_too_small_subject_is_unverified():
    small = (300, 300, 312, 330)  # 30 px tall in a 720 px frame
    mon, store, brain, got = verify_run("m_small", [brain_box("person", small, small)], box=small)
    b = store.get(got["added"][0].id)
    assert b.verdict == "unverified" and "Too small" in b.brain_findings[0] and not brain.calls


def test_single_picture_votes_with_seeds():
    brain = FakeBrain("ready", [brain_box("person", PERSON_BOX)])
    mon, store, det_, clock, got = make_monitor("m_seeds", brain)
    feed(mon, det_, clock, 1, PERSON_BOX, 2.5)  # the alert is (nearly) the last frame: no later pictures
    mon.brain_step()
    seeds = [c[5] for c in brain.calls if len(c[1]) == 3]
    assert seeds == [1], brain.calls  # the second vote is another sample of the same picture
    assert store.get(got["added"][0].id).verdict == "confirmed"


def test_car_moved_unchanged_picture_is_rejected():
    brain = FakeBrain("ready", [])
    mon, store, det_, clock, got = make_monitor("m_car", brain)
    car = (400, 400, 600, 520)
    frame = np.full((H, W, 3), 60, np.uint8)

    def job(key, unchanged, gone=False):
        a = Alert(key, "s", 1, "Yard", NIGHT, "car_moved", "Parked car moved", "d", "warning")
        store.add(a)
        mon._queue_verify(key, (0, 0, NIGHT), a, frame.copy(), frame, car, "car", channel=1, ts=NIGHT,
                          gone=gone, unchanged=unchanged, collect=False)
        mon.brain_step()
        return store.get(key)

    b = job("same", 0.97)  # two overlapping parked cars swapped boxes: the picture did not change
    assert b.verdict == "rejected" and "did not move" in b.brain_findings[0] and not brain.calls
    brain.answer = [brain_box("car", (405, 398, 602, 521), car)]
    assert job("moved", 0.4).verdict == "confirmed" and brain.calls[0][1] == ["car"]
    assert job("gone_but_there", 0.4, gone=True).verdict == "rejected"
    brain.answer = []
    assert job("gone", None, gone=True).verdict == "confirmed"


def test_similarity_of_parked_car_pictures():
    rng = np.random.default_rng(3)
    cv2 = engine.cv2
    scene = cv2.resize(rng.integers(0, 255, (18, 32, 3)).astype(np.uint8), (W, H), interpolation=cv2.INTER_CUBIC)
    noisy = np.clip(scene.astype(int) + rng.integers(-12, 12, scene.shape), 0, 255).astype(np.uint8)
    darker = (scene * 0.7).astype(np.uint8)
    moved = np.roll(scene, 60, axis=1)
    car = (400, 400, 600, 520)
    a = engine.small_grey(scene)
    assert engine.similarity(a, engine.small_grey(noisy), car, W) > engine.STILL_NCC   # sensor noise
    assert engine.similarity(a, engine.small_grey(darker), car, W) > engine.STILL_NCC  # light changed
    assert engine.similarity(a, engine.small_grey(moved), car, W) < engine.STILL_NCC   # something moved
    flat = np.full((H, W, 3), 30, np.uint8)
    assert engine.similarity(engine.small_grey(flat), engine.small_grey(flat), car, W) is None  # no texture: unknown


def test_verify_geometry_helpers():
    x1, y1, x2, y2 = engine.verify_region((600, 300, 620, 340), W, H)  # a far person
    assert y2 - y1 == round(engine.VERIFY_MIN_H * H) and x1 <= 600 and x2 >= 620 and x1 >= 0 and x2 <= W
    assert abs((x2 - x1) / (y2 - y1) - engine.VERIFY_ASPECT) < 0.02
    assert engine.verify_region((0, 0, 1280, 720), W, H) == (0, 0, W, H)
    assert engine.too_small((0, 0, 10, 30), H) and engine.too_small((10, 0, 30, 70), H)  # top edge, small
    assert not engine.too_small((10, 100, 40, 200), H) and not engine.too_small((10, 0, 60, 200), H)
    s = (200, 300, 260, 460)
    assert engine.on_subject((205, 310, 255, 450), s) and not engine.on_subject((300, 300, 360, 460), s)
    assert engine.on_subject((300, 380, 340, 440), s, near=True)  # a car door next to the person
    assert engine.about_person("hand on a car door handle") and not engine.about_person("open car door")
    assert engine._behaviours(["Person", "open  car door", "open car door", "person walking", " "]) == \
        ["open car door"]


def test_brain_worker_strict_labels():
    import importlib
    out = sys.stdout
    try:
        bw = importlib.import_module("yardguard.brain_worker")  # it redirects stdout for its protocol
    finally:
        sys.stdout = out
    box = "<box><100><200><300><400></box>"
    phrases = ["person lying under a car", "person"]
    assert [b["label"] for b in bw.parse_answer(f"<ref>person</ref>{box}", phrases)] == ["person"]
    # a shorter label never becomes the behaviour that was asked
    assert bw.parse_answer(f"<ref>person</ref>{box}", ["person lying under a car"]) == []
    assert [b["label"] for b in bw.parse_answer(f"<ref>The person.</ref>{box}", phrases)] == ["person"]
    assert bw.parse_answer(f"<ref>a dog</ref>{box}", phrases) == []
    assert bw.parse_answer(box, phrases) == []                       # no label, two phrases: unknown
    assert [b["label"] for b in bw.parse_answer(box, ["car"])] == ["car"]
    assert bw.parse_answer("<ref>person</ref><box>None</box>", phrases) == []
    assert bw._match_label("Person Lying Under A Car", phrases) == "person lying under a car"
    assert bw._match_label("person lying", phrases) is None


def test_pending_check_survives_restart():
    brain = FakeBrain("ready", [brain_box("person", PERSON_BOX)])
    mon, store, det_, clock, got = make_monitor("m_requeue", brain)
    feed(mon, det_, clock, 1, PERSON_BOX, 3)
    a = got["added"][0]
    assert Path(engine.raw_path(a.snapshot_path)).exists()
    mon._running = True
    mon.stop()  # monitoring stopped before the brain got to it
    assert store.get(a.id).verdict == "unverified" and Path(engine.raw_path(a.snapshot_path)).exists()
    mon2 = Monitor(mon.cfg, store, brain, detector=FakeDetector(), source_factory=FakeSource, clock=clock)
    assert mon2._requeue_pending() == [a.id] and store.get(a.id).verdict == "pending"
    assert mon2.brain_step()
    assert store.get(a.id).verdict == "confirmed" and not Path(engine.raw_path(a.snapshot_path)).exists()


def test_verify_priority_night_parking_first():
    brain = FakeBrain("ready", [])
    mon, store, det_, clock, got = make_monitor("m_prio", brain)
    pic = np.zeros((10, 10, 3), np.uint8)
    mon._queue_verify("a", (1, 1, 5.0), Alert("a", "s", 7, "x", 5.0, "loitering", "t", "d", "info"),
                      pic, pic, (0, 0, 5, 5))
    mon._queue_verify("b", (0, 0, 9.0), Alert("b", "s", 1, "x", 9.0, "night_presence", "t", "d", "warning"),
                      pic, pic, (0, 0, 5, 5))
    assert mon._jobs.get_nowait().key == "b"


def test_config_migrates_old_modes():
    import json
    from yardguard import config
    path = OUT / "migrate.json"
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"settings": {"brain_mode": "main", "brain_main_interval_s": 10,
                                             "brain_main_prompts": ["person"], "model": "yolo11m"}}), encoding="utf-8")
    old = config.CONFIG_PATH
    config.CONFIG_PATH = path
    try:
        s = config.load_config().settings
        assert s.brain_mode == "verify" and s.model == "yolo11m" and not hasattr(s, "brain_main_interval_s")
        path.write_text(json.dumps({"settings": {"model": "gone-model", "brain_mode": "off"}}), encoding="utf-8")
        s = config.load_config().settings
        assert s.model == config.DEFAULT_MODEL and s.brain_mode == "off"
        # LocateAnything as the main model is gone: back to a YOLO model, its object list dropped
        path.write_text(json.dumps({"settings": {"model": "locateanything", "la_objects": ["van"],
                                                 "brain_generation": "hybrid"}}), encoding="utf-8")
        s = config.load_config().settings
        assert s.model == "yolo11s" and list(config.MODELS) == ["yolo11n", "yolo11s", "yolo11m", "yolo11l"]
        assert not hasattr(s, "la_objects") and s.brain_mode == "verify"
        # benchmarked: "hybrid" was never better than "fast" -> moved once; a later choice is kept
        assert s.brain_generation == "fast" and s.config_version == config.CONFIG_VERSION
        path.write_text(json.dumps({"settings": {"brain_generation": "hybrid",
                                                 "config_version": config.CONFIG_VERSION}}), encoding="utf-8")
        assert config.load_config().settings.brain_generation == "hybrid"
    finally:
        config.CONFIG_PATH = old


def test_yolo_groups_and_camera_status():
    assert [engine._group(x) for x in ("person", "car", "motorcycle", "bus", "truck")] == \
        ["person", "vehicle", "vehicle", "vehicle", "vehicle"]
    mon, store, det_, clock, got = make_monitor("m_status", FakeBrain("ready"))
    assert not mon.gpu_decode() and mon.verifying()  # the brain verifies: NVDEC would cost its memory
    feed(mon, det_, clock, 1, PERSON_BOX, 1)
    st = mon.camera_status()[1]
    assert st["detector"] == "fake" and st["state"] == "watching" and "la" not in st


def test_threads_start_stop():
    brain = FakeBrain("stopped")
    mon, store, det_, clock, got = make_monitor("m_threads", brain)
    mon._clock = time.time
    mon.start()  # _setup runs again: new FakeSources
    assert mon.running and brain.started == 1
    for _ in range(10):
        for src in FakeSource.instances.values():
            src.push(time.time())
        time.sleep(0.05)
    t0 = time.time()
    mon.stop()
    assert not mon.running and time.time() - t0 < 3
    assert all(s.stopped for s in FakeSource.instances.values())
    assert det_.calls > 0
    mon.start()
    mon.stop()


def test_user_stop_keeps_brain_stopped():
    brain = FakeBrain("ready", [{"label": "open car door", "x1": 1, "y1": 2, "x2": 3, "y2": 4}])
    mon, store, det_, clock, got = make_monitor("m_user_stop", brain)
    brain.stop(user=True)  # the user pressed "Stop AI brain" while monitoring runs
    mon._clock = time.time
    mon.start()
    try:
        assert brain.started == 0  # a (re)start of monitoring does not bring it back
        mon.restart()
        assert brain.started == 0
        brain.state = "error"      # nor does the restart-after-error path
        mon.restart()
        assert brain.started == 0
        assert not mon.verifying()
    finally:
        mon.stop()
    # monitoring keeps working; its alerts are unverified at once, nothing waits for the brain
    mon, store, det_, clock, got = make_monitor("m_user_stop2", brain)
    feed(mon, det_, clock, 1, (200, 300, 260, 460), 3)
    assert [a.verdict for a in got["added"]] == ["unverified"] and mon._jobs.qsize() == 0
    assert not brain.calls
    # Start by the user lifts it
    brain.start(user=True)
    brain.state = "ready"
    assert mon.verifying()


def test_pictures_follow_store_when_events_dir_missing():
    # the app falls back to a local store when the configured folder (unplugged drive) fails
    mon, store, det_, clock, got = make_monitor("m_fallback")
    mon.cfg.settings.events_dir = r"Q:\missing_drive\events"
    feed(mon, det_, clock, 1, (200, 300, 260, 460), 3)
    assert got["added"], "alert was dropped"
    snap = Path(got["added"][0].snapshot_path)
    assert snap.exists() and store.dir in snap.parents
    store.close()


def test_brain_restarted_after_error():
    brain = FakeBrain("error")
    mon, store, det_, clock, got = make_monitor("m_brain_err", brain)
    mon._clock = time.time
    mon.start()
    try:
        assert brain.started == 1
    finally:
        mon.stop()
        store.close()


def test_pipeline_state_is_locked():
    import threading
    s = settings()
    pipe = CameraPipeline(1)
    errors = []

    def brain_side():
        try:
            for i in range(3000):
                hit = engine.RuleHit("brain_detect", f"b{i}", "t", "d", "warning",
                                     (np.array([i % 900, 0, i % 900 + 40, 80], np.float32), "person"),
                                     [], True, f"b{i}")
                pipe.cooldown_ok(hit, NIGHT + i * 0.01, 0.5)
                pipe.recent_rule("night_presence", NIGHT + i * 0.01, 60)
        except Exception as exc:  # deque / dict mutated during iteration
            errors.append(exc)

    t = threading.Thread(target=brain_side)
    t.start()
    ts = NIGHT
    while t.is_alive():
        ts += 0.2
        x = 200 + int(ts * 7) % 400
        pipe.process(dets(det(x, 300, x + 60, 460, PERSON)), ts, (W, H), None, s)
        pipe.counts()
    t.join()
    assert not errors, errors


def test_detector_load_dropped_after_stop():
    import threading
    mon, store, det_, clock, got = make_monitor("m_detload")
    mon._detector, mon._fixed_detector = None, False
    stop = threading.Event()
    loaded = []
    real = engine._default_detector

    def slow_load(s, progress=None):
        stop.set()  # monitor stopped while the model was loading
        loaded.append(1)
        return FakeDetector()

    engine._default_detector = slow_load
    try:
        assert not mon._ensure_detector(stop)
        assert loaded and mon._detector is None
        assert mon._ensure_detector(threading.Event()) and mon._detector is not None
    finally:
        engine._default_detector = real
        store.close()


def test_alert_store_round_trip():
    folder = OUT / "store"
    shutil.rmtree(folder, ignore_errors=True)
    store = AlertStore(str(folder))
    pic = folder / "a.jpg"
    pic.write_bytes(b"x")
    a = Alert("id1", "site", 1, "Yard ·parking…", 100.0, "loitering", "Person lingering", "Stayed 30 s.",
              "info", str(pic), "", [{"label": "person", "x1": 1, "y1": 2, "x2": 3, "y2": 4}], "pending", [], "new")
    store.add(a)
    store.add(Alert("id2", "site", 5, "Street", 200.0, "prowling", "t", "d", "warning", verdict="rejected"))
    store.add(Alert("id3", "site", 5, "Street", 300.0, "car_moved", "t", "d", "info"))
    assert store.get("id1") == a
    assert [x.id for x in store.list()] == ["id3", "id2", "id1"]
    assert [x.id for x in store.list(channel=5)] == ["id3", "id2"]
    assert [x.id for x in store.list(verdict="rejected")] == ["id2"]
    assert [x.id for x in store.list(limit=1)] == ["id3"]
    assert store.unread_count() == 2  # the rejected one is not counted
    store.mark("id1", "seen")
    a.verdict, a.brain_findings, a.severity = "confirmed", ["open car door"], "critical"
    store.update(a)  # still has status "new": must not undo the user's mark
    b = store.get("id1")
    assert b.status == "seen" and b.verdict == "confirmed" and b.brain_findings == ["open car door"]
    assert [x.id for x in store.list(status="seen")] == ["id1"]
    assert store.resolve_pending() == 1 and store.get("id3").verdict == "unverified"
    store.delete("id1")
    assert store.get("id1") is None and not pic.exists()
    store.close()
    store = AlertStore(str(folder))  # reopen: data persisted
    assert store.unread_count() == 1 and store.get("id3").rule == "car_moved"
    store.close()


def test_crop_region():
    assert engine.crop_region([[600, 300, 640, 380]], W, H) == (460, 180, 780, 500)  # min 320 px
    x1, y1, x2, y2 = engine.crop_region([[0, 0, 50, 50]], W, H)
    assert (x1, y1) == (0, 0) and x2 - x1 == 320  # shifted back inside the frame
    x1, y1, x2, y2 = engine.crop_region([[100, 100, 1200, 700]], W, H)
    assert (x1, y1, x2, y2) == (0, 0, W, H)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    t0 = time.perf_counter()
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {time.perf_counter() - t0:.1f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
