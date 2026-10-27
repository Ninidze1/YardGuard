"""Live monitoring: YOLO watches every camera, rules turn tracks into alerts,
LocateAnything double-checks the suspicious moments.

Verification (Monitor._verify) works per rule, in the spirit of NVIDIA VSS alert verification:
1. Existence: is the alert's subject (the person, or the car for car_moved) really there? The
   brain is asked for "person" / "car" in a zoomed crop of RAW pictures (drawn boxes confuse it);
   only boxes ON the subject count. No -> "rejected" (a detector false positive: a post, a shadow).
   Yes -> "confirmed" (a real person / car), at the rule's own severity.
2. Behaviour: the user's suspicious phrases are asked in the same call next to benign twins
   ("person walking", "person standing"). A phrase counts only on the subject and only when no
   benign twin boxed the same thing (then the model grounded just "person" and the action words
   carried no evidence). Seen in at least two of the pictures it is reported, and at night the alert
   becomes critical (by day owners open doors and load cars all the time).
The pictures are the alert frame plus later frames of the same track (VOTE_FRAMES, about a second
apart), or extra samples of one picture with other seeds. Subjects too small to judge stay
"unverified". A parked car that "moved" while its picture did not change is rejected outright.

Threads (no Qt here; callbacks run on these threads, the GUI must hop to its own):
- inference: every tick takes the newest frame of each camera, skips cameras where
  nothing moves (with a slow keep-alive), and runs ONE batched FastDetector call;
  per-camera tracking and rules follow on the same thread, which also hands later
  pictures of a subject to its waiting verify job.
  The same pass feeds each camera's areas (areas.py: counting lines, parking spots, ...).
- brain: verifies alerts from a priority queue (night + parking first); asks the ask_ai areas'
  questions only while no alert waits.
- areas: finishes area events (number plates, pictures, alerts, clips) and saves them.
- capture: hands every new frame to the clip recorder (clips.py), which keeps a few seconds
  before each alert and writes short clips on threads of its own; cleans up clips hourly.
FrameSource objects keep their own reader threads, so no extra capture threads are needed.
"""
from __future__ import annotations

import csv
import itertools
import json
import logging
import queue
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .areas import (ALERT_KINDS, KIND_TEXT, AreaEvaluator, AreaEvent, AreaHit, area_rect, describe, draw_area,
                    reads_plates)
from .config import active_zones
from .overlay import OneEuro
from .video import CLASSES

log = logging.getLogger("yardguard")

VEHICLES = {"car", "motorcycle", "bus", "truck"}
# What the brain is asked for to confirm an alert about this object (YOLO calls a van a truck or a car)
VERIFY_NOUNS = {"truck": ["truck", "car"], "bus": ["bus", "truck"]}

# Tracking
PERSON_MAX_AGE_S = 3.0       # a person not seen this long is gone
VEHICLE_MAX_AGE_S = 12.0     # longer than the motion keep-alive, so parked cars survive gating
MIN_HITS = 2                 # detections before a track counts (filters one-frame flickers)
LOW_SCORE = 0.10             # weak detections down to this may keep a known object alive (ByteTrack idea)
WEAK_TRUST = 0.5             # weak detections move a smoothed box half as much
TWIN_IOU = 0.6               # two tracks this much on top of each other are one object
PARKED_GRACE_STILL_S = 10.0  # a vehicle still this long stays visible through short detection gaps...
PARKED_GRACE_S = 5.0         # ...of up to this long (noisy night frames)
# Rules
PRESENCE_MIN_S = 2.0         # night presence: person tracked at least this long
ZONE_GRACE_S = 3.0           # leaving a zone this briefly does not reset the loiter timer
PROWL_DWELL_S = 1.5          # time next to a car before it counts as "visited"
PROWL_CAR_STILL_S = 10.0     # only cars that are not driving count for prowling
PROWL_MAX_SPEED = 0.35       # person heights per second; walking is ~0.8, standing/peering ~0
PARK_S = 300.0               # a car still this long is "parked" and watched for moves
STILL_IOU = 0.85             # parked box overlap below this = the car shifted
SHIFT_CONFIRM_S = 2.0        # shift must last this long (filters box jitter)
MISSING_CONFIRM_S = 30.0     # parked car undetected this long (and not hidden) = gone
CONTACT_WINDOW_S = 60.0      # contact this recent explains / flags a move
# Pipeline
KEEPALIVE_S = 4.0            # detection still runs this often without motion
STALE_S = 10.0               # no new frame for this long = camera offline
MOTION_FRACTION = 0.002      # share of changed pixels in the small frame that counts as motion
BRAIN_TIMEOUT_S = 60.0
BRAIN_WAIT_START_S = 120.0   # verify jobs wait this long for a starting brain
# Verification (see the module docstring)
VERIFY_SIDE = 896            # long side of the picture the brain sees: crops are zoomed up to this
VERIFY_MIN_H = 0.4           # verify crop: at least this share of the frame height...
VERIFY_CONTEXT = 3.0         # ...and this many subject heights, so nearby cars are in it
VERIFY_ASPECT = 1.6          # crop width : height
MIN_SUBJECT_H = 40 / 720     # subjects lower than this share of the frame are too small to judge
VOTE_FRAMES = 3              # pictures per verdict: the alert frame and later ones of the same track
VOTE_GAP_S = 0.6             # later pictures at least this far apart
VOTE_WAIT_S = 2.5            # the brain waits at most this long (after the alert) for them
VOTE_MIN = 2                 # behaviour must be seen in this many pictures to raise the severity
ON_SUBJECT_IOU = 0.3         # a brain box is on the subject with this overlap...
ON_SUBJECT_GROW = 0.25       # ...or with its centre inside the subject box grown by this much
NEAR_SUBJECT_GROW = 1.0      # things ("open car door") may be up to a subject size away
SAME_BOX_IOU = 0.6           # a behaviour box this close to a benign twin's box is no evidence
BEHAVIOUR_PER_CALL = 5       # with "person" and the benign twins: 8 phrases, the model stops at ~8-10
BENIGN = ("person walking", "person standing")
# Phrases about what a PERSON is doing ("person looking into a car window", "hand on a door handle")
# are grounded on the person's own box whatever they are doing: on 43 real alerts LocateAnything
# "saw" them in 2-3 of 3 pictures on 5 people doing nothing suspicious. They are reported as a hint
# and never raise the severity; only phrases about a thing ("open car door", "broken car window")
# can make a night alert critical.
POSE_WORDS = ("person", "someone", "somebody", "man", "woman", "people", "hand", "hands", "child")


def is_pose_phrase(phrase: str) -> bool:
    words = phrase.lower().split()
    return bool(words) and (words[0] in POSE_WORDS or (words[0] in ("a", "an", "the") and len(words) > 1
                                                        and words[1] in POSE_WORDS))
# car_moved: the parked car's picture this similar before/after = nothing moved. On the quiet night
# clips the same place 4 s / 2 min apart scored >= 0.95 in 90-99 % of cases; content shifted by 15 % of a
# car width scored below it in 99-100 % (rejecting a real move is the costlier mistake).
STILL_NCC = 0.95
HISTORY_EVERY_S = 2.0        # small grey frames kept per camera for that comparison...
HISTORY_S = 180.0            # ...over this long
HISTORY_WIDTH = 320
AREA_ASK_GAP_S = 3.0         # ask_ai areas: at most one brain question this often, over all cameras
PEOPLE_WORDS = {"person", "people", "man", "men", "woman", "women", "child", "someone", "hand", "hands",
                "intruder", "thief"}


# --- Alerts and storage --------------------------------------------------------

@dataclass
class Alert:
    id: str
    site_id: str
    channel: int
    camera: str
    ts: float
    rule: str              # night_presence | loitering | prowling | car_moved (older versions: brain_detect)
    title: str
    detail: str
    severity: str          # info | warning | critical
    snapshot_path: str = ""
    crop_path: str = ""
    boxes: list[dict] = field(default_factory=list)  # {"label", "x1", "y1", "x2", "y2", "role"} snapshot pixels
    verdict: str = "pending"  # pending | confirmed | rejected | unverified
    brain_findings: list[str] = field(default_factory=list)
    status: str = "new"    # new | seen | false_alarm
    incident_id: str = ""  # set by AlertStore.add(): the incident this alert belongs to
    clip_path: str = ""    # preview clip, set once the file is complete (alerts close together share one)
    clip_offset_s: float = -1.0  # where the alert happens in the clip; -1 = no clip (>= 0 and no path: saving)


@dataclass
class Incident:
    """Related alerts of one site (one real event, possibly on several cameras) told as one story.
    Everything except status is derived from its alerts and kept up to date by AlertStore."""
    id: str
    site_id: str
    start_ts: float
    end_ts: float
    status: str = "open"          # open | handled | false_alarm
    severity: str = "info"        # highest severity of its alerts
    verdict: str = "unverified"   # confirmed | pending | rejected | unverified (see incident_verdict)
    title: str = ""
    channels: list[int] = field(default_factory=list)   # in order of first appearance
    cameras: list[str] = field(default_factory=list)    # names, same order as channels
    alert_count: int = 0
    cover_alert_id: str = ""
    unread: int = 0               # alerts still "new"; counted on read, not stored

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)


INCIDENT_GAP_S = 300.0
INCIDENT_STATUSES = ("open", "handled", "false_alarm")
DAYTIME_QUIET_RULES = ("loitering", "prowling", "car_moved")  # alert only at night unless rules_when="always"
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

_FIELDS = [f.name for f in fields(Alert)]
_JSON_FIELDS = {"boxes", "brain_findings"}
_SQL_TYPES = {"channel": "INTEGER", "ts": "REAL", "clip_offset_s": "REAL"}
_INC_FIELDS = [f.name for f in fields(Incident) if f.name != "unread"]
_INC_JSON = {"channels", "cameras"}
_INC_TYPES = {"start_ts": "REAL", "end_ts": "REAL", "alert_count": "INTEGER"}
_AE_FIELDS = [f.name for f in fields(AreaEvent)]
_AE_TYPES = {"channel": "INTEGER", "ts": "REAL", "confidence": "REAL", "track_id": "INTEGER", "clip_offset_s": "REAL"}
_AE_TEXT = ("site_id", "area_id", "area_name", "task", "kind", "label", "value", "snapshot_path", "crop_path",
            "clip_path")


def incident_verdict(verdicts) -> str:
    """confirmed if any alert is, else pending if any is still checked, else rejected if the AI
    dismissed all of them, else unverified."""
    v = set(verdicts)
    if "confirmed" in v:
        return "confirmed"
    if "pending" in v:
        return "pending"
    if v and v == {"rejected"}:
        return "rejected"
    return "unverified"


def short_title(a: Alert) -> str:
    """What happened in a few plain words, for incident titles."""
    near_cars = "car" in a.title.lower()
    if a.rule == "night_presence":
        return "Person near cars at night" if near_cars else "Person at night"
    if a.rule == "loitering":
        return "Person lingering near cars" if near_cars else "Person lingering"
    if a.rule == "prowling":
        return "Person going from car to car"
    return a.title or a.rule.replace("_", " ").capitalize()


def _cover(alerts: list[Alert]) -> Alert:
    """The alert that best shows the incident: most severe, AI-confirmed first, then the earliest."""
    return max(alerts, key=lambda a: (SEVERITY_RANK.get(a.severity, 0), a.verdict == "confirmed", -a.ts))


def incident_title(alerts: list[Alert]) -> str:
    cover = _cover(alerts)
    others = len({a.channel for a in alerts}) - 1
    extra = f" +{others} camera{'s' if others > 1 else ''}" if others > 0 else ""
    return f"{short_title(cover)} · {cover.camera}{extra}"


class AlertStore:
    """Alerts and the incidents grouping them, in SQLite at <events_dir>/alerts.db; safe to use
    from any thread."""

    def __init__(self, events_dir: str, incident_gap_s: float = INCIDENT_GAP_S) -> None:
        self.dir = Path(events_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "alerts.db"
        self.incident_gap_s = float(incident_gap_s)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._ensure_table("alerts", _FIELDS, _SQL_TYPES)
            self._ensure_table("incidents", _INC_FIELDS, _INC_TYPES)
            self._db.execute("CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts)")
            self._db.execute("CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status)")
            self._db.execute("CREATE INDEX IF NOT EXISTS alerts_incident ON alerts(incident_id)")
            self._db.execute("CREATE INDEX IF NOT EXISTS incidents_start ON incidents(start_ts)")
            self._ensure_table("area_events", _AE_FIELDS, _AE_TYPES)
            self._db.execute("CREATE INDEX IF NOT EXISTS area_events_area ON area_events(area_id, ts)")
            self._db.execute("CREATE INDEX IF NOT EXISTS area_events_ts ON area_events(ts)")
            self._backfill_locked()

    def _ensure_table(self, table: str, names: list[str], types: dict) -> None:
        cols = ", ".join(f"{n} {types.get(n, 'TEXT')}" + (" PRIMARY KEY" if n == "id" else "") for n in names)
        self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols})")
        have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
        for name in names:  # columns added in newer versions
            if name not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {types.get(name, 'TEXT')}")

    @staticmethod
    def _row(a: Alert) -> list:
        d = asdict(a)
        return [json.dumps(d[n], ensure_ascii=False) if n in _JSON_FIELDS else d[n] for n in _FIELDS]

    @staticmethod
    def _alert(row: sqlite3.Row) -> Alert:
        d = {n: row[n] for n in _FIELDS}
        for n in _JSON_FIELDS:
            d[n] = json.loads(d[n] or "[]")
        for n in ("snapshot_path", "crop_path", "detail", "camera", "incident_id", "clip_path"):
            d[n] = d[n] or ""
        d["clip_offset_s"] = -1.0 if d["clip_offset_s"] is None else float(d["clip_offset_s"])
        return Alert(**d)

    @staticmethod
    def _incident(row: sqlite3.Row) -> Incident:
        d = {n: row[n] for n in _INC_FIELDS}
        for n in _INC_JSON:
            d[n] = json.loads(d[n] or "[]")
        d["unread"] = int(row["unread"]) if "unread" in row.keys() else 0
        return Incident(**d)

    def add(self, a: Alert, gap_s: float | None = None) -> None:
        """Save the alert and put it in an incident: the newest open incident of its site when
        the alert is less than gap_s (default: incident_gap_s) from it, else a new one.
        Sets a.incident_id."""
        gap = self.incident_gap_s if gap_s is None else float(gap_s)
        with self._lock, self._db:
            old = self._db.execute("SELECT incident_id FROM alerts WHERE id=?", (a.id,)).fetchone()
            if old is not None and old["incident_id"]:
                a.incident_id = old["incident_id"]  # saved again: stays where it was
            elif not (a.incident_id and self._db.execute(
                    "SELECT 1 FROM incidents WHERE id=?", (a.incident_id,)).fetchone()):
                a.incident_id = self._group_locked(a, gap)
            self._db.execute(f"INSERT OR REPLACE INTO alerts ({', '.join(_FIELDS)}) "
                             f"VALUES ({', '.join('?' * len(_FIELDS))})", self._row(a))
            self._recompute_locked(a.incident_id)

    def _group_locked(self, a: Alert, gap: float) -> str:
        # Only open incidents absorb alerts: once handled, the next alert is a new story.
        row = self._db.execute("SELECT id, start_ts, end_ts FROM incidents WHERE site_id=? AND status='open' "
                               "ORDER BY end_ts DESC LIMIT 1", (a.site_id,)).fetchone()
        if row is not None and row["start_ts"] - gap <= a.ts <= row["end_ts"] + gap:
            return row["id"]
        return self._new_incident_locked(a)

    def _new_incident_locked(self, a: Alert, status: str = "open") -> str:
        inc = Incident(id=f"inc-{uuid.uuid4().hex[:12]}", site_id=a.site_id, start_ts=a.ts, end_ts=a.ts,
                       status=status)
        d = asdict(inc)
        values = [json.dumps(d[n], ensure_ascii=False) if n in _INC_JSON else d[n] for n in _INC_FIELDS]
        self._db.execute(f"INSERT INTO incidents ({', '.join(_INC_FIELDS)}) "
                         f"VALUES ({', '.join('?' * len(_INC_FIELDS))})", values)
        return inc.id

    def _recompute_locked(self, incident_id: str) -> None:
        """Derive everything but the status from the incident's alerts; drop it when it has none."""
        if not incident_id:
            return
        rows = self._db.execute("SELECT * FROM alerts WHERE incident_id=? ORDER BY ts", (incident_id,)).fetchall()
        if not rows:
            self._db.execute("DELETE FROM incidents WHERE id=?", (incident_id,))
            return
        alerts = [self._alert(r) for r in rows]
        cams: dict[int, str] = {}
        for a in alerts:
            cams.setdefault(a.channel, a.camera)
        cover = _cover(alerts)
        self._db.execute(
            "UPDATE incidents SET start_ts=?, end_ts=?, severity=?, verdict=?, title=?, channels=?, cameras=?, "
            "alert_count=?, cover_alert_id=? WHERE id=?",
            (alerts[0].ts, alerts[-1].ts, cover.severity, incident_verdict(a.verdict for a in alerts),
             incident_title(alerts), json.dumps(list(cams)), json.dumps(list(cams.values()), ensure_ascii=False),
             len(alerts), cover.id, incident_id))

    def _backfill_locked(self) -> None:
        """Alerts saved before incidents existed: group them by time, oldest first. Their status
        follows what the user did: any still new -> open, all false alarms -> false_alarm, else handled."""
        rows = self._db.execute("SELECT * FROM alerts WHERE incident_id IS NULL OR incident_id='' "
                                "ORDER BY ts").fetchall()
        if not rows:
            return
        current: dict[str, tuple[str, float]] = {}   # site -> (incident id, end ts)
        members: dict[str, list[Alert]] = {}
        for r in rows:
            a = self._alert(r)
            cur = current.get(a.site_id)
            inc_id = cur[0] if cur and a.ts - cur[1] <= self.incident_gap_s else self._new_incident_locked(a)
            current[a.site_id] = (inc_id, a.ts)
            members.setdefault(inc_id, []).append(a)
            self._db.execute("UPDATE alerts SET incident_id=? WHERE id=?", (inc_id, a.id))
        for inc_id, alerts in members.items():
            statuses = {a.status for a in alerts}
            status = "open" if "new" in statuses else "false_alarm" if statuses == {"false_alarm"} else "handled"
            self._db.execute("UPDATE incidents SET status=? WHERE id=?", (status, inc_id))
            self._recompute_locked(inc_id)
        log.info("Grouped %d older alerts into %d incidents", len(rows), len(members))

    def update(self, a: Alert) -> None:
        """Save everything except status: the user may have marked the alert while the brain
        was checking it, and mark() is the only way to change status. Refreshes its incident.
        An empty clip_path is not written: a copy saved before its clip was finished must not
        drop the clip (clear_clip() is the way to remove it)."""
        names = [n for n in _FIELDS if n not in ("id", "status", "incident_id")
                 and not (n == "clip_path" and not a.clip_path)]
        values = dict(zip(_FIELDS, self._row(a)))
        with self._lock, self._db:
            self._db.execute(f"UPDATE alerts SET {', '.join(n + '=?' for n in names)} WHERE id=?",
                             [values[n] for n in names] + [a.id])
            row = self._db.execute("SELECT incident_id FROM alerts WHERE id=?", (a.id,)).fetchone()
            if row is not None and row["incident_id"]:
                a.incident_id = row["incident_id"]
                self._recompute_locked(a.incident_id)

    def list(self, limit: int = 200, status: str | None = None, verdict: str | None = None,
             channel: int | None = None) -> list[Alert]:
        where, args = [], []
        for col, value in (("status", status), ("verdict", verdict), ("channel", channel)):
            if value is not None:
                where.append(f"{col}=?")
                args.append(value)
        sql = "SELECT * FROM alerts" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY ts DESC LIMIT ?", args + [int(limit)]).fetchall()
        return [self._alert(r) for r in rows]

    def get(self, alert_id: str) -> Alert | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return self._alert(row) if row else None

    def mark(self, alert_id: str, status: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE alerts SET status=? WHERE id=?", (status, alert_id))

    def delete(self, alert_id: str) -> list[str]:
        """Remove the alert, its two pictures and its clip unless another alert shares the clip
        (its incident follows, or goes when empty). Returns the files Windows refused to delete."""
        a = self.get(alert_id)
        with self._lock, self._db:
            self._db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
            if a is not None:
                self._recompute_locked(a.incident_id)
            clips = self._unused_clips_locked([a.clip_path] if a else [])
        return _unlink(([a.snapshot_path, a.crop_path, raw_path(a.snapshot_path)] if a else []) + clips)

    def unread_count(self) -> int:
        """New alerts the brain did not dismiss (rejected ones are shown greyed, not counted)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM alerts WHERE status='new' AND verdict!='rejected'").fetchone()
        return int(row[0])

    def resolve_pending(self, verdict: str = "unverified", keep=()) -> int:
        """Alerts left 'pending' by a crash or stop will never be checked: say so. keep: ids of
        pending alerts that are checked again (see Monitor._requeue_pending)."""
        keep = set(keep)
        with self._lock, self._db:
            rows = self._db.execute("SELECT id, incident_id FROM alerts WHERE verdict='pending'").fetchall()
            rows = [r for r in rows if r[0] not in keep]
            for r in rows:
                self._db.execute("UPDATE alerts SET verdict=? WHERE id=?", (verdict, r[0]))
            for inc_id in {r[1] for r in rows}:
                self._recompute_locked(inc_id)
            return len(rows)

    # -- incidents --------------------------------------------------------------------

    _INC_SELECT = ("SELECT i.*, (SELECT COUNT(*) FROM alerts a WHERE a.incident_id=i.id AND a.status='new') "
                   "AS unread FROM incidents i")

    def list_incidents(self, limit: int = 200, status: str | None = None,
                       since: float | None = None) -> list[Incident]:
        """Newest first (by start time)."""
        where, args = [], []
        if status is not None:
            where.append("i.status=?")
            args.append(status)
        if since is not None:
            where.append("i.end_ts>=?")
            args.append(float(since))
        sql = self._INC_SELECT + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY i.start_ts DESC LIMIT ?", args + [int(limit)]).fetchall()
        return [self._incident(r) for r in rows]

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._lock:
            row = self._db.execute(self._INC_SELECT + " WHERE i.id=?", (incident_id,)).fetchone()
        return self._incident(row) if row else None

    def incident_alerts(self, incident_id: str) -> list[Alert]:
        """The incident's alerts, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM alerts WHERE incident_id=? ORDER BY ts",
                                    (incident_id,)).fetchall()
        return [self._alert(r) for r in rows]

    def mark_incident(self, incident_id: str, status: str) -> None:
        """handled -> its alerts become seen; false_alarm -> they become false alarms;
        open (undo) -> false alarms go back to seen."""
        if status not in INCIDENT_STATUSES:
            raise ValueError(f"Unknown incident status {status!r}")
        alert_sql = {"handled": "UPDATE alerts SET status='seen' WHERE incident_id=?",
                     "false_alarm": "UPDATE alerts SET status='false_alarm' WHERE incident_id=?",
                     "open": "UPDATE alerts SET status='seen' WHERE incident_id=? AND status='false_alarm'"}
        with self._lock, self._db:
            self._db.execute("UPDATE incidents SET status=? WHERE id=?", (status, incident_id))
            self._db.execute(alert_sql[status], (incident_id,))

    def delete_incident(self, incident_id: str) -> list[str]:
        """Remove the incident, its alerts and their pictures. Returns the picture files that could
        not be deleted (Windows refuses while a file is open); the records are gone either way."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT snapshot_path, crop_path, clip_path FROM alerts WHERE incident_id=?",
                                    (incident_id,)).fetchall()
            self._db.execute("DELETE FROM alerts WHERE incident_id=?", (incident_id,))
            self._db.execute("DELETE FROM incidents WHERE id=?", (incident_id,))
            clips = self._unused_clips_locked([r["clip_path"] for r in rows])
        return _unlink([p for r in rows for p in (r["snapshot_path"], r["crop_path"], raw_path(r["snapshot_path"]))]
                       + clips)

    # -- clips ------------------------------------------------------------------------

    def _unused_clips_locked(self, paths) -> list[str]:
        """Of these clip files, the ones no remaining alert or area event refers to."""
        return [p for p in dict.fromkeys(p for p in paths if p)
                if self._db.execute("SELECT 1 FROM alerts WHERE clip_path=? LIMIT 1", (p,)).fetchone() is None
                and self._db.execute("SELECT 1 FROM area_events WHERE clip_path=? LIMIT 1", (p,)).fetchone() is None]

    def clip_refs(self) -> list[dict]:
        """Every clip file in use, for cleanup: {"clip_path", "ts" (its newest alert or area event),
        "false_alarm" (all its incidents are false alarms), "keep" (an open or AI-confirmed incident)}."""
        with self._lock:
            rows = self._db.execute(
                "SELECT a.clip_path AS clip_path, MAX(a.ts) AS ts, "
                "MIN(COALESCE(i.status, '') = 'false_alarm' OR a.status = 'false_alarm') AS false_alarm, "
                "MAX(COALESCE(i.status, '') = 'open' OR a.verdict = 'confirmed' "
                "    OR COALESCE(i.verdict, '') = 'confirmed') AS keep "
                "FROM alerts a LEFT JOIN incidents i ON i.id = a.incident_id "
                "WHERE a.clip_path IS NOT NULL AND a.clip_path != '' GROUP BY a.clip_path").fetchall()
            area_rows = self._db.execute(
                "SELECT clip_path, MAX(ts) AS ts FROM area_events WHERE clip_path IS NOT NULL AND clip_path != '' "
                "GROUP BY clip_path").fetchall()
        refs = {r["clip_path"]: {"clip_path": r["clip_path"], "ts": float(r["ts"]),
                                 "false_alarm": bool(r["false_alarm"]), "keep": bool(r["keep"])} for r in rows}
        for r in area_rows:  # an area event's clip is no false alarm; it goes by age (and the size cap)
            ref = refs.setdefault(r["clip_path"], {"clip_path": r["clip_path"], "ts": float(r["ts"]),
                                                   "false_alarm": False, "keep": False})
            ref["ts"], ref["false_alarm"] = max(ref["ts"], float(r["ts"])), False
        return list(refs.values())

    def clear_clip(self, clip_path: str) -> list[str]:
        """The clip file was deleted: its alerts and area events no longer have a clip. Returns the
        alert ids."""
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute("SELECT id FROM alerts WHERE clip_path=?", (clip_path,))]
            self._db.execute("UPDATE alerts SET clip_path='', clip_offset_s=-1 WHERE clip_path=?", (clip_path,))
            self._db.execute("UPDATE area_events SET clip_path='', clip_offset_s=-1 WHERE clip_path=?", (clip_path,))
        return ids

    # -- area events --------------------------------------------------------------------
    @staticmethod
    def _ae_row(e: AreaEvent) -> list:
        d = asdict(e)
        d["extra"] = json.dumps(d["extra"] or {}, ensure_ascii=False)
        return [d[n] for n in _AE_FIELDS]

    @staticmethod
    def _area_event(row: sqlite3.Row) -> AreaEvent:
        d = {n: row[n] for n in _AE_FIELDS}
        try:
            d["extra"] = json.loads(d["extra"] or "{}")
        except ValueError:
            d["extra"] = {}
        for n in _AE_TEXT:
            d[n] = d[n] or ""
        d["confidence"] = float(d["confidence"] or 0.0)
        d["clip_offset_s"] = -1.0 if d["clip_offset_s"] is None else float(d["clip_offset_s"])
        d["track_id"] = None if d["track_id"] is None else int(d["track_id"])
        return AreaEvent(**d)

    def add_area_event(self, e: AreaEvent) -> str:
        with self._lock, self._db:
            self._db.execute(f"INSERT OR REPLACE INTO area_events ({', '.join(_AE_FIELDS)}) "
                             f"VALUES ({', '.join('?' * len(_AE_FIELDS))})", self._ae_row(e))
        return e.id

    def get_area_event(self, event_id: str) -> AreaEvent | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM area_events WHERE id=?", (event_id,)).fetchone()
        return self._area_event(row) if row else None

    def set_area_event_clip(self, event_id: str, clip_path: str, offset_s: float) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE area_events SET clip_path=?, clip_offset_s=? WHERE id=?",
                             (clip_path, float(offset_s), event_id))

    @staticmethod
    def _ae_where(area_id=None, channel=None, since=None, until=None, kind=None, query=None) -> tuple[str, list]:
        where, args = [], []
        for col, value in (("area_id", area_id), ("channel", channel)):
            if value is not None:
                where.append(f"{col}=?")
                args.append(value)
        if since is not None:
            where.append("ts>=?")
            args.append(float(since))
        if until is not None:
            where.append("ts<?")
            args.append(float(until))
        if kind:
            kinds = [kind] if isinstance(kind, str) else list(kind)
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args += kinds
        if query and str(query).strip():
            like = f"%{str(query).strip()}%"
            where.append("(label LIKE ? OR value LIKE ? OR area_name LIKE ? OR kind LIKE ? OR extra LIKE ?)")
            args += [like] * 5
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def list_area_events(self, area_id: str | None = None, channel: int | None = None, since: float | None = None,
                         until: float | None = None, kind=None, query: str | None = None,
                         limit: int = 500) -> list[AreaEvent]:
        """Newest first. kind: one kind or a list; query: text in the label, value (plate), area name."""
        where, args = self._ae_where(area_id, channel, since, until, kind, query)
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM area_events{where} ORDER BY ts DESC LIMIT ?",
                                    args + [int(limit)]).fetchall()
        return [self._area_event(r) for r in rows]

    def area_stats(self, area_id: str, since: float | None = None) -> dict:
        """{"total", "counts": {kind: n}, "in", "out" (crossings / entries / plates by direction),
        "hours": [{"ts": hour start, "counts": {kind: n}}] (local hours, oldest first),
        "state", "last_kind", "last_value", "last_ts" (the newest event of the area, any time)}."""
        where, args = self._ae_where(area_id=area_id, since=since)
        with self._lock:
            rows = self._db.execute(f"SELECT kind, ts, value, extra FROM area_events{where} ORDER BY ts",
                                    args).fetchall()
            last = self._db.execute("SELECT kind, value, ts FROM area_events WHERE area_id=? "
                                    "ORDER BY ts DESC LIMIT 1", (area_id,)).fetchone()
        counts: Counter = Counter()
        hours: dict[float, Counter] = {}
        n_in = n_out = 0
        for r in rows:
            counts[r["kind"]] += 1
            hour = datetime.fromtimestamp(r["ts"]).replace(minute=0, second=0, microsecond=0).timestamp()
            hours.setdefault(hour, Counter())[r["kind"]] += 1
            direction = ""
            if r["kind"] == "plate":
                try:
                    direction = json.loads(r["extra"] or "{}").get("direction", "")
                except (ValueError, AttributeError):
                    pass
            if r["kind"] in ("cross_in", "enter") or direction == "in":
                n_in += 1
            elif r["kind"] in ("cross_out", "exit") or direction == "out":
                n_out += 1
        state = None
        if last is not None:
            state = {"occupied": "occupied", "freed": "free", "answer_yes": "yes", "answer_no": "no",
                     "blocked": "blocked", "appeared": "object", "removed": "taken"}.get(last["kind"], last["kind"])
            if last["kind"] == "changed":
                state = "normal" if last["value"] == "restored" else "changed"
        return {"total": len(rows), "counts": dict(counts), "in": n_in, "out": n_out,
                "hours": [{"ts": h, "counts": dict(c)} for h, c in sorted(hours.items())],
                "state": state, "last_kind": last["kind"] if last else None,
                "last_value": last["value"] if last else None, "last_ts": float(last["ts"]) if last else None}

    def export_area_events_csv(self, path, **filters) -> int:
        """Write the matching events (list_area_events filters, oldest first) as CSV for Excel;
        returns how many rows were written."""
        filters.setdefault("limit", 1_000_000)
        events = list(reversed(self.list_area_events(**filters)))
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Time", "Camera", "Area", "Task", "Event", "What happened", "Object", "Value",
                        "Direction", "Confidence", "Track", "Picture", "Clip"])
            for e in events:
                w.writerow([datetime.fromtimestamp(e.ts).strftime("%Y-%m-%d %H:%M:%S"), e.channel, e.area_name,
                            e.task, KIND_TEXT.get(e.kind, e.kind), describe(e), e.label, e.value,
                            (e.extra or {}).get("direction", ""), f"{e.confidence:.2f}",
                            "" if e.track_id is None else e.track_id, e.snapshot_path, e.clip_path])
        return len(events)

    def _drop_area_events_locked(self, where: str, args: list) -> list[str]:
        rows = self._db.execute(f"SELECT snapshot_path, crop_path, clip_path FROM area_events{where}",
                                args).fetchall()
        self._db.execute(f"DELETE FROM area_events{where}", args)
        clips = self._unused_clips_locked([r["clip_path"] for r in rows])
        return [p for r in rows for p in (r["snapshot_path"], r["crop_path"])] + clips

    def delete_area_events(self, area_id: str) -> list[str]:
        """Remove the area's log with its pictures (and clips nothing else uses). Returns the files
        Windows refused to delete."""
        with self._lock, self._db:
            files = self._drop_area_events_locked(" WHERE area_id=?", [area_id])
        return _unlink(files)

    def cleanup_area_events(self, days: float, now: float | None = None) -> int:
        """Area events older than `days` go with their pictures; returns how many."""
        cutoff = (time.time() if now is None else now) - float(days) * 86400
        with self._lock, self._db:
            n = int(self._db.execute("SELECT COUNT(*) FROM area_events WHERE ts<?", (cutoff,)).fetchone()[0])
            files = self._drop_area_events_locked(" WHERE ts<?", [cutoff]) if n else []
        _unlink(files)
        folder = self.dir / "areas"
        for day in (sorted(folder.iterdir()) if folder.exists() else []):
            if day.is_dir():
                try:
                    day.rmdir()  # only empty day folders
                except OSError:
                    pass
        if n:
            log.info("Area log cleanup: %d events older than %g days removed", n, days)
        return n

    def open_incident_count(self) -> int:
        """Open incidents, not counting those the AI dismissed (they wait under "Dismissed by AI")."""
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM incidents WHERE status='open' AND verdict!='rejected'").fetchone()[0])

    def unread_incident_count(self) -> int:
        """Open incidents with alerts nobody looked at yet (AI-dismissed alerts do not count,
        like unread_count)."""
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM incidents i WHERE i.status='open' AND EXISTS (SELECT 1 FROM alerts a "
                "WHERE a.incident_id=i.id AND a.status='new' AND a.verdict!='rejected')").fetchone()[0])

    def incident_counts(self, since: float | None = None) -> dict[str, int]:
        """Number of incidents per status (optionally only those still going on after `since`)."""
        sql, args = "SELECT status, COUNT(*) FROM incidents", []
        if since is not None:
            sql, args = sql + " WHERE end_ts>=?", [float(since)]
        with self._lock:
            rows = self._db.execute(sql + " GROUP BY status", args).fetchall()
            dismissed = self._db.execute(
                "SELECT COUNT(*) FROM incidents WHERE status='open' AND verdict='rejected'"
                + (" AND end_ts>=?" if since is not None else ""), args).fetchone()[0]
        counts = {s: 0 for s in INCIDENT_STATUSES}
        counts.update({r[0]: int(r[1]) for r in rows})
        # "dismissed": open incidents the AI found nothing in; they are not counted as open
        counts["dismissed"] = int(dismissed)
        counts["open"] -= counts["dismissed"]
        return counts

    def close(self) -> None:
        with self._lock:
            self._db.close()


def plate_key(text: str) -> str:
    """A plate for comparing: letters and digits only, upper case ("aa-123 bb" -> "AA123BB")."""
    return re.sub(r"[^0-9A-Z]", "", str(text).upper())


def plate_distance(a: str, b: str) -> int:
    """Edit distance of two plates (after plate_key): 1 = one character misread."""
    a, b = plate_key(a), plate_key(b)
    if not a or not b:
        return max(len(a), len(b)) or 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def raw_path(snapshot_path: str) -> str:
    """The un-annotated picture kept next to an alert while it waits for the brain, so the check
    survives a restart of monitoring ("" when the alert has no snapshot)."""
    return str(Path(snapshot_path).with_name(Path(snapshot_path).stem + "_raw.jpg")) if snapshot_path else ""


def _unlink(paths: list[str]) -> list[str]:
    """Delete picture files; returns the ones Windows refused (e.g. a thumbnail still has it open)."""
    left = []
    for p in paths:
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                log.warning("Could not delete %s", p)
                left.append(p)
    return left


# --- Geometry and time -----------------------------------------------------------

def iou(a, b) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return float(inter / union) if union > 0 else 0.0


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def foot(b) -> tuple[float, float]:
    """Bottom centre of a box: where feet / wheels touch the ground."""
    return (b[0] + b[2]) / 2, b[3]


def is_near(person, car, factor: float = 0.5) -> bool:
    """Person box within factor x car width (and height) of the car box."""
    gap_x = max(0.0, max(person[0], car[0]) - min(person[2], car[2]))
    gap_y = max(0.0, max(person[1], car[1]) - min(person[3], car[3]))
    return gap_x <= factor * (car[2] - car[0]) and gap_y <= factor * (car[3] - car[1])


def touches(other, car) -> bool:
    """Something overlaps the car and stands at its depth (feet near the car's bottom
    edge), so a person walking in front of the car closer to the camera does not count."""
    if _inter(other, car) <= 0:
        return False
    h = car[3] - car[1]
    return car[1] + 0.4 * h <= other[3] <= car[3] + 0.2 * h


def point_in_polygon(x: float, y: float, pts) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _minutes(text: str, default: int) -> int:
    try:
        h, m = str(text).strip().split(":")
        return (int(h) % 24) * 60 + int(m) % 60
    except ValueError:
        return default


def is_night(ts: float, start: str, end: str) -> bool:
    """True when local time of ts is inside [start, end); handles windows across midnight.
    start == end means all day."""
    t = datetime.fromtimestamp(ts)
    m = t.hour * 60 + t.minute
    s, e = _minutes(start, 0), _minutes(end, 360)
    if s == e:
        return True
    return s <= m < e if s < e else (m >= s or m < e)


class Zones:
    """Watch zones of one camera in pixels. No zones = the whole picture."""

    WHOLE = "the whole view"

    def __init__(self, zones: list[dict] | None, w: int, h: int) -> None:
        self.polys = []
        for i, z in enumerate(zones or []):
            pts = [(float(x) * w, float(y) * h) for x, y in z.get("points", [])]
            if len(pts) >= 3:
                self.polys.append((z.get("name") or f"Zone {i + 1}", pts))

    def at(self, x: float, y: float) -> str | None:
        if not self.polys:
            return self.WHOLE
        return next((name for name, pts in self.polys if point_in_polygon(x, y, pts)), None)


# --- Tracking ----------------------------------------------------------------------

@dataclass
class Track:
    id: int
    group: str                 # "person" | "vehicle"
    box: np.ndarray
    first_seen: float
    last_seen: float
    hits: int = 1
    labels: Counter = field(default_factory=Counter)
    vel: np.ndarray = field(default_factory=lambda: np.zeros(2, np.float32))  # centre px / s
    # zone state (people)
    zone: str | None = None
    zone_since: float | None = None
    zone_last_in: float = 0.0
    # prowling (people): car key -> first / last time next to it
    near_since: dict = field(default_factory=dict)
    visits: dict = field(default_factory=dict)
    # stillness (vehicles)
    anchor: np.ndarray | None = None
    anchor_since: float = 0.0
    anchor_off_since: float | None = None   # box away from the anchor since (must persist to count)
    # box smoothing: rules see a steady box, not detector jitter
    filt: OneEuro | None = None

    @property
    def label(self) -> str:
        return self.labels.most_common(1)[0][0] if self.labels else self.group

    @property
    def confirmed(self) -> bool:
        return self.hits >= MIN_HITS

    def still_for(self, ts: float) -> float:
        return ts - self.anchor_since if self.anchor is not None else 0.0


def _group(label: str) -> str:
    """YOLO labels: person or vehicle."""
    return "vehicle" if label in VEHICLES else "person"


class Tracker:
    """Greedy IoU tracker with a centroid fallback and constant-velocity prediction.
    Deterministic, cheap, and good enough at 2-10 detections per second. Car and truck
    share one group because YOLO often flips between them on the same vehicle.

    Noise-proofing for night video (same approach as the live overlay):
    - two-tier association: detections at or above `high` (the user's certainty) are matched
      first and may start new tracks; weaker ones (>= LOW_SCORE) only keep existing tracks alive;
    - a new track is not started on top of an existing one (a shifted second box), and twin
      tracks that drift onto the same object are merged;
    - boxes are smoothed with a One Euro filter, so stillness and shift rules see a steady box."""

    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self._ids = itertools.count(1)

    def update(self, dets: np.ndarray, ts: float, high: float = 0.0) -> list[Track]:
        """dets: (N, 6) x1 y1 x2 y2 score class (class looked up in CLASSES).
        Returns tracks that expired this update."""
        dets = np.asarray(dets, np.float32).reshape(-1, 6)
        keep = [int(c) in CLASSES for c in dets[:, 5]] if len(dets) else []
        dets = dets[keep] if len(dets) else dets
        labels = [CLASSES[int(c)] for c in dets[:, 5]]
        if len(dets):
            keep = dets[:, 4] >= min(LOW_SCORE, high)
            dets, labels = dets[keep], [lb for lb, k in zip(labels, keep) if k]
        strong = [di for di in range(len(dets)) if dets[di][4] >= high]
        weak = [di for di in range(len(dets)) if dets[di][4] < high]
        used_t, used_d = set(), set()
        for group, trust in ((strong, 1.0), (weak, WEAK_TRUST)):
            for _, _, ti, di in sorted(self._pairs(group, used_t, dets, labels, ts)):
                if ti in used_t or di in used_d:
                    continue
                used_t.add(ti)
                used_d.add(di)
                self._update(self.tracks[ti], dets[di], labels[di], ts, trust)
        for di in strong:
            if di in used_d or self._covers_existing(_group(labels[di]), dets[di]):
                continue
            d = dets[di]
            t = Track(next(self._ids), _group(labels[di]), d[:4].copy(), ts, ts)
            t.filt = OneEuro(t.box, ts)
            t.labels[labels[di]] += 1
            if t.group == "vehicle":
                t.anchor, t.anchor_since = t.box.copy(), ts
            self.tracks.append(t)
        self._merge_twins()
        gone = [t for t in self.tracks if self._expired(t, ts)]
        self.tracks = [t for t in self.tracks if not self._expired(t, ts)]
        return gone

    def _pairs(self, group: list[int], used_t: set, dets, labels, ts: float) -> list[tuple]:
        pairs = []
        for ti, t in enumerate(self.tracks):
            if ti in used_t:
                continue
            pred = self._predict(t, ts)
            tw, th = t.box[2] - t.box[0], t.box[3] - t.box[1]
            size = max(tw, th, 1.0)
            reach = 0.75
            for di in group:
                d = dets[di]
                if _group(labels[di]) != t.group:
                    continue
                ov = iou(pred, d)
                dist = float(np.hypot((d[0] + d[2] - pred[0] - pred[2]) / 2,
                                      (d[1] + d[3] - pred[1] - pred[3]) / 2)) / size
                ratio = _area(d) / max(_area(t.box), 1.0)
                if ov >= 0.15 or (dist <= reach and 0.33 <= ratio <= 3.0):
                    pairs.append((-ov, dist, ti, di))
        return pairs

    def _covers_existing(self, group: str, d: np.ndarray) -> bool:
        """A confident box mostly on top of a known object is a second box for it, not a new one."""
        for t in self.tracks:
            if t.group == group and (iou(t.box, d) >= 0.45
                                     or _inter(t.box, d) >= 0.8 * min(_area(t.box), _area(d))):
                return True
        return False

    def _merge_twins(self) -> None:
        drop = set()
        for i, a in enumerate(self.tracks):
            for b in self.tracks[i + 1:]:
                if (a.group == b.group and id(a) not in drop and id(b) not in drop
                        and iou(a.box, b.box) >= TWIN_IOU):
                    drop.add(id(b) if (a.hits, -a.first_seen) >= (b.hits, -b.first_seen) else id(a))
        if drop:
            self.tracks = [t for t in self.tracks if id(t) not in drop]

    @staticmethod
    def _max_age(t: Track) -> float:
        return VEHICLE_MAX_AGE_S if t.group == "vehicle" else PERSON_MAX_AGE_S

    def _expired(self, t: Track, ts: float) -> bool:
        return ts - t.last_seen > self._max_age(t)

    @staticmethod
    def _predict(t: Track, ts: float) -> np.ndarray:
        dt = min(ts - t.last_seen, 1.0)
        dx, dy = t.vel * dt
        return t.box + np.array([dx, dy, dx, dy], np.float32)

    @staticmethod
    def _update(t: Track, d: np.ndarray, label: str, ts: float, trust: float = 1.0) -> None:
        dt = ts - t.last_seen
        if dt > 0:
            moved = np.array([(d[0] + d[2] - t.box[0] - t.box[2]) / 2,
                              (d[1] + d[3] - t.box[1] - t.box[3]) / 2], np.float32) / dt
            t.vel = 0.5 * t.vel + 0.5 * moved
        if t.filt is None:
            t.filt = OneEuro(t.box.astype(float), t.last_seen)
        t.box = t.filt(d[:4].astype(float), ts, trust).astype(np.float32)
        t.last_seen = ts
        t.hits += 1
        t.labels[label] += 1
        if t.group != "vehicle":
            return
        if t.anchor is None:
            t.anchor, t.anchor_since = t.box.copy(), ts
        elif iou(t.box, t.anchor) >= STILL_IOU:
            t.anchor_off_since = None
        else:
            # One noisy frame is not a move: the vehicle only stops counting as still once
            # its box has stayed away from the anchor for SHIFT_CONFIRM_S.
            t.anchor_off_since = t.anchor_off_since or ts
            if ts - t.anchor_off_since >= SHIFT_CONFIRM_S:
                t.anchor, t.anchor_since, t.anchor_off_since = t.box.copy(), ts, None


# --- Rules -----------------------------------------------------------------------

@dataclass
class RuleHit:
    rule: str
    key: str                       # identity for the cooldown (track / parked slot)
    title: str
    detail: str
    severity: str
    subject: tuple                 # (box, label)
    context: list = field(default_factory=list)  # [(box, label)] nearby cars / people
    night: bool = False
    track_key: str = ""            # brain de-duplication key
    track_id: int | None = None    # the subject's track (people), for later pictures of it
    since: float = 0.0             # car_moved: when the change began
    gone: bool = False             # car_moved: the car disappeared (subject = where it was parked)
    kind: str = ""                 # what the brain looks for: "person" | "car" ("" = by rule)
    verify: bool = True            # False: nothing for the brain to check (an area's look changed)


@dataclass
class ParkedCar:
    id: int
    box: np.ndarray
    since: float
    track_id: int
    last_seen: float
    shift_since: float | None = None
    missing_since: float | None = None
    contact_ts: float = 0.0
    contact_kind: str = ""          # "person" | "vehicle"
    contact_box: np.ndarray | None = None


def _clock_text(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _duration_text(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    return f"{seconds // 60} min" + (f" {seconds % 60} s" if seconds % 60 and seconds < 600 else "")


class CameraPipeline:
    """Tracking + rules for one camera. Pure logic driven by timestamps, so tests can
    feed synthetic boxes with a fake clock."""

    def __init__(self, channel: int, name: str = "", role: str = "Other") -> None:
        self.channel = channel
        self.name = name or f"Camera {channel}"
        self.role = role
        self.tracker = Tracker()
        self.parked: list[ParkedCar] = []
        self._slot_ids = itertools.count(1)
        self._fired: dict[tuple, float] = {}
        self._recent: dict[str, deque] = {}
        self.last_ts = 0.0
        # Inference, brain-scan and GUI-status threads all touch the tracks and cooldown state.
        self._lock = threading.RLock()

    # -- public ------------------------------------------------------------------
    def process(self, dets, ts: float, size: tuple[int, int], zones: list[dict] | None, s) -> list[RuleHit]:
        """Feed one detection result; returns the rule hits that passed the cooldown."""
        with self._lock:
            return self._process(dets, ts, size, zones, s)

    def _process(self, dets, ts: float, size: tuple[int, int], zones: list[dict] | None, s) -> list[RuleHit]:
        self.last_ts = ts
        self.tracker.update(dets, ts, high=float(getattr(s, "confidence", 0.0)))
        zs = Zones(zones, *size)
        night = is_night(ts, s.night_start, s.night_end)
        hits: list[RuleHit] = []
        if s.rule_car_moved:
            hits += self._car_rules(ts, zs, night)
        else:
            self.parked.clear()
        hits += self._person_rules(ts, zs, night, s)
        # The rules still run by day (parked cars get armed, linger timers keep counting) but only alert
        # at night unless the user chose "always".
        if not night and getattr(s, "rules_when", "night") != "always":
            hits = [h for h in hits if h.rule not in DAYTIME_QUIET_RULES]
        return [h for h in hits if self.cooldown_ok(h, ts, s.alert_cooldown_s)]

    def visible(self, ts: float | None = None) -> list[Track]:
        """Tracks seen in this frame, plus parked vehicles missed for a few noisy frames."""
        with self._lock:
            ts = self.last_ts if ts is None else ts
            return [t for t in self.tracker.tracks if t.confirmed and (
                t.last_seen == ts or (t.group == "vehicle" and t.still_for(ts) >= PARKED_GRACE_STILL_S
                                      and ts - t.last_seen <= PARKED_GRACE_S))]

    def has_people(self) -> bool:
        with self._lock:
            return any(t.group == "person" for t in self.tracker.tracks)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(t.label for t in self.visible()))

    def recent_rule(self, rule: str, ts: float, cooldown: float) -> bool:
        """True if `rule` fired on this camera within the cooldown before `ts`."""
        with self._lock:
            return any(ts - when < cooldown for when, _ in self._recent.get(rule, ()))

    def cooldown_ok(self, hit: RuleHit, ts: float, cooldown: float) -> bool:
        """At most one alert per (rule, object) per cooldown. A new track id right next to
        a recent alert of the same rule is treated as the same object (ID switch)."""
        with self._lock:
            return self._cooldown_ok(hit, ts, cooldown)

    def _cooldown_ok(self, hit: RuleHit, ts: float, cooldown: float) -> bool:
        key = (hit.rule, hit.key)
        if ts - self._fired.get(key, -1e18) < cooldown:
            return False
        recent = self._recent.setdefault(hit.rule, deque(maxlen=20))
        box = hit.subject[0]
        if hit.subject[1] == "person":  # parked cars have stable slot ids, people may switch ids
            for when, other in recent:
                if ts - when < cooldown and (iou(box, other) > 0.1 or self._close(box, other)):
                    return False
        self._fired[key] = ts
        recent.append((ts, np.array(box, np.float32)))
        if len(self._fired) > 500:  # forget objects long gone
            self._fired = {k: v for k, v in self._fired.items() if ts - v < cooldown}
        return True

    @staticmethod
    def _close(a, b) -> bool:
        (ax, ay), (bx, by) = foot(a), foot(b)
        return float(np.hypot(ax - bx, ay - by)) < max(a[3] - a[1], b[3] - b[1])

    # -- people --------------------------------------------------------------------
    def _car_key(self, car: Track) -> str:
        slot = next((p for p in self.parked if p.track_id == car.id), None)
        return f"s{slot.id}" if slot else f"t{car.id}"

    def _person_rules(self, ts: float, zs: Zones, night: bool, s) -> list[RuleHit]:
        hits = []
        vis = self.visible(ts)
        cars = [c for c in vis if c.group == "vehicle" and c.still_for(ts) >= PROWL_CAR_STILL_S]
        for p in vis:
            if p.group != "person":
                continue
            zone = zs.at(*foot(p.box))
            if zone is not None:
                if p.zone_since is None:
                    p.zone_since = ts
                p.zone, p.zone_last_in = zone, ts
            elif p.zone_since is not None and ts - p.zone_last_in > ZONE_GRACE_S:
                p.zone, p.zone_since = None, None
            in_zone = zone is not None
            where = p.zone or zs.WHOLE
            near = [c for c in cars if is_near(p.box, c.box)]
            context = [(c.box.copy(), c.label) for c in near]
            subject = (p.box.copy(), "person")
            tkey = f"t{p.id}"
            parking = self.role == "Parking" or bool(cars)

            if s.rule_night_presence and night and in_zone and ts - p.first_seen >= PRESENCE_MIN_S:
                title = "Person near parked cars at night" if parking else "Person seen at night"
                hits.append(RuleHit("night_presence", tkey, title, f"In {where} at {_clock_text(ts)}.",
                                    "warning", subject, context, night, tkey))
            if s.rule_loitering and p.zone_since is not None and ts - p.zone_since >= s.loiter_seconds:
                stayed = _duration_text(ts - p.zone_since)
                hits.append(RuleHit("loitering", tkey, "Person lingering" + (" near cars" if near else ""),
                                    f"Stayed {stayed} in {where}.", "warning" if night else "info",
                                    subject, context, night, tkey))
            if s.rule_prowling and (in_zone or p.zone_since is not None):
                hit = self._prowl(p, near, ts, s, night, where, subject, context, tkey)
                if hit:
                    hits.append(hit)
            for h in hits:
                if h.track_key == tkey:
                    h.track_id = p.id
        return hits

    def _prowl(self, p: Track, near: list[Track], ts, s, night, where, subject, context, tkey):
        # Only stopping next to a car counts: walking along a row of parked cars passes
        # within half a car width of each one for several seconds.
        height = max(float(p.box[3] - p.box[1]), 1.0)
        walking = float(np.hypot(*p.vel)) / height > PROWL_MAX_SPEED
        # One car at a time, the closest: someone standing between parked cars (chatting, loading) is
        # near several at once without going from car to car.
        (px, py) = foot(p.box)
        closest = min(near, key=lambda c: float(np.hypot(px - (c.box[0] + c.box[2]) / 2, py - c.box[3])),
                      default=None)
        keys = set() if walking or closest is None else {self._car_key(closest)}
        for k in keys:
            first = p.near_since.setdefault(k, ts)
            if ts - first >= PROWL_DWELL_S:
                p.visits[k] = ts
        for k in [k for k in p.near_since if k not in keys]:
            del p.near_since[k]
        p.visits = {k: v for k, v in p.visits.items() if ts - v <= s.prowl_window_s}
        if len(p.visits) < max(2, s.prowl_cars):
            return None
        return RuleHit("prowling", tkey, "Person going from car to car",
                       f"Was close to {len(p.visits)} different parked cars within "
                       f"{_duration_text(s.prowl_window_s)} in {where}.",
                       "warning" if night else "info", subject, context, night, tkey)

    # -- parked cars -----------------------------------------------------------------
    def _car_rules(self, ts: float, zs: Zones, night: bool) -> list[RuleHit]:
        vis = self.visible(ts)
        vehicles = [v for v in vis if v.group == "vehicle"]
        people = [v for v in vis if v.group == "person"]
        for v in vehicles:
            if (v.still_for(ts) >= PARK_S and zs.at(*foot(v.box)) is not None
                    and not any(iou(v.anchor, p.box) > 0.5 for p in self.parked)):
                self.parked.append(ParkedCar(next(self._slot_ids), v.anchor.copy(), v.anchor_since, v.id, ts))
                log.debug("Camera %s: car %s parked", self.channel, v.id)
        hits, keep = [], []
        for slot in self.parked:
            hit, alive = self._watch_slot(slot, vehicles, people, ts, night)
            if hit:
                hits.append(hit)
            if alive:
                keep.append(slot)
        self.parked = keep
        return hits

    def _watch_slot(self, slot: ParkedCar, vehicles, people, ts, night):
        best, best_iou = None, 0.0
        for v in vehicles:
            ov = iou(v.box, slot.box)
            if ov > best_iou:
                best, best_iou = v, ov
        for o in people + [v for v in vehicles if v is not best]:
            contact = touches(o.box, slot.box) if o.group == "person" else iou(o.box, slot.box) > 0.05
            if contact:
                slot.contact_ts, slot.contact_kind, slot.contact_box = ts, o.group, o.box.copy()

        if best is not None and best_iou >= STILL_IOU:
            slot.last_seen, slot.track_id = ts, best.id
            slot.shift_since = slot.missing_since = None
            return None, True
        if best is not None and best_iou >= 0.2:
            slot.missing_since = None
            w = slot.box[2] - slot.box[0]
            ratio = _area(best.box) / max(_area(slot.box), 1.0)
            (bx, by), (sx, sy) = foot(best.box), foot(slot.box)
            if 0.75 <= ratio <= 1.33 and np.hypot(bx - sx, by - sy) >= 0.08 * w:
                slot.shift_since = slot.shift_since or ts
                if ts - slot.shift_since >= SHIFT_CONFIRM_S:
                    return self._moved_hit(slot, best.box, ts, night, gone=False), False
            # otherwise the box changed shape: partly hidden, not moved
            return None, True
        # no matching vehicle
        slot.missing_since = slot.missing_since or ts
        hidden = any(_inter(o.box, slot.box) > 0.3 * _area(slot.box) for o in vehicles + people)
        if ts - slot.missing_since < MISSING_CONFIRM_S or hidden:
            return None, True
        if self._contact_kind(slot, slot.missing_since):
            return self._moved_hit(slot, slot.box, ts, night, gone=True), False
        log.info("Camera %s: parked car %s left", self.channel, slot.id)
        return None, False

    @staticmethod
    def _contact_kind(slot: ParkedCar, since: float) -> str:
        """'person' / 'vehicle' if something touched the car shortly before or during the change."""
        return slot.contact_kind if slot.contact_ts and slot.contact_ts >= since - CONTACT_WINDOW_S else ""

    def _moved_hit(self, slot: ParkedCar, box, ts, night, gone: bool) -> RuleHit:
        kind = self._contact_kind(slot, slot.missing_since or slot.shift_since or ts)
        parked = _duration_text(ts - slot.since)
        if kind == "vehicle":
            title = "Parked car gone after another vehicle touched it" if gone else "Parked car may have been hit"
            severity = "warning"
        elif gone:
            title = "Parked car gone (possible tampering)" if night else "Parked car left"
            severity = "warning" if night else "info"
        else:
            title = "Parked car moved at night" if night else "Parked car moved"
            severity = "warning" if night else "info"
        detail = f"Parked for {parked}, then {'disappeared' if gone else 'moved'} at {_clock_text(ts)}."
        if kind:
            detail += f" A {kind} was next to it just before."
        context = [(slot.box.copy(), "parked here")]
        if kind and slot.contact_box is not None:
            context.append((slot.contact_box.copy(), kind))
        return RuleHit("car_moved", f"s{slot.id}", title, detail, severity, (np.array(box, np.float32), "car"),
                       context, night, f"s{slot.id}", since=(slot.missing_since if gone else slot.shift_since) or ts,
                       gone=gone)


# --- Pictures ------------------------------------------------------------------------

_COLORS = {"subject": (40, 40, 230), "context": (0, 165, 255), "brain": (220, 60, 200), "other": (200, 200, 200)}


def box_dict(box, label: str, role: str) -> dict:
    x1, y1, x2, y2 = (round(float(v), 1) for v in box[:4])
    return {"label": label, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "role": role}


def draw_boxes(img: np.ndarray, boxes: list[dict]) -> np.ndarray:
    """Draw boxes in place; subject thick red, context orange, brain magenta."""
    scale = max(img.shape[:2]) / 1280
    for b in boxes:
        color = _COLORS.get(b.get("role", "other"), _COLORS["other"])
        thick = max(1, round((3 if b.get("role") == "subject" else 2) * scale))
        p1, p2 = (int(b["x1"]), int(b["y1"])), (int(b["x2"]), int(b["y2"]))
        cv2.rectangle(img, p1, p2, color, thick, cv2.LINE_AA)
        text = str(b.get("label", ""))[:40].encode("ascii", "replace").decode()
        if text:
            fs = 0.55 * max(scale, 0.6)
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
            y = max(p1[1], th + 8)
            ink = (30, 30, 30) if sum(color) > 500 else (255, 255, 255)  # dark text on light labels
            cv2.rectangle(img, (p1[0], y - th - 8), (p1[0] + tw + 8, y), color, -1)
            cv2.putText(img, text, (p1[0] + 4, y - 5), cv2.FONT_HERSHEY_SIMPLEX, fs, ink, 1, cv2.LINE_AA)
    return img


def crop_region(boxes, w: int, h: int, pad: float = 0.3, min_side: int = 320) -> tuple[int, int, int, int]:
    """Region around the boxes with some margin, at least min_side, inside the frame."""
    arr = np.array([b[:4] for b in boxes], np.float32)
    x1, y1 = arr[:, 0].min(), arr[:, 1].min()
    x2, y2 = arr[:, 2].max(), arr[:, 3].max()
    bw, bh = x2 - x1, y2 - y1
    x1, x2 = x1 - pad * bw, x2 + pad * bw
    y1, y2 = y1 - pad * bh, y2 + pad * bh
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half_w = max(x2 - x1, min(min_side, w)) / 2
    half_h = max(y2 - y1, min(min_side, h)) / 2
    x1, x2 = cx - half_w, cx + half_w
    y1, y2 = cy - half_h, cy + half_h
    dx = -x1 if x1 < 0 else (w - x2 if x2 > w else 0)  # shift back inside instead of shrinking
    dy = -y1 if y1 < 0 else (h - y2 if y2 > h else 0)
    x1, x2, y1, y2 = x1 + dx, x2 + dx, y1 + dy, y2 + dy
    return int(max(0, x1)), int(max(0, y1)), int(min(w, x2)), int(min(h, y2))


def _write_jpg(path: Path, img: np.ndarray) -> None:
    ok, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise OSError("Could not encode the picture")
    path.write_bytes(data.tobytes())  # works with non-English folder names, unlike cv2.imwrite


def _read_jpg(path) -> np.ndarray | None:
    try:
        data = np.fromfile(str(path), np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


# --- Verification helpers ------------------------------------------------------------

def _xyxy(b: dict) -> tuple[float, float, float, float]:
    return float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"])


def verify_region(box, w: int, h: int) -> tuple[int, int, int, int]:
    """The crop the brain sees for one subject: the subject with room around it (nearby cars), at
    least VERIFY_MIN_H of the frame high and VERIFY_ASPECT wide, inside the frame. Zoomed up to
    VERIFY_SIDE, a far person becomes big enough to judge; in the whole frame they are a few pixels."""
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    ch = min(float(h), max(VERIFY_MIN_H * h, VERIFY_CONTEXT * (y2 - y1)))
    cw = min(float(w), max(VERIFY_ASPECT * ch, 2.0 * (x2 - x1)))
    left = min(max(0.0, (x1 + x2 - cw) / 2), w - cw)
    top = min(max(0.0, (y1 + y2 - ch) / 2), h - ch)
    return int(left), int(top), int(round(left + cw)), int(round(top + ch))


def too_small(box, frame_h: int) -> bool:
    """Too few pixels to judge: a far person, or a small box cut by the top edge (a head beyond
    the fence, or a spot at y=0)."""
    bh = float(box[3] - box[1])
    least = MIN_SUBJECT_H * frame_h
    return bh < least or (box[1] <= 1 and bh < 2 * least)


def about_person(phrase: str) -> bool:
    """Behaviour of a person ("person crouching...", "hand on a door handle"): its box is the person,
    so it must lie on the subject. Other phrases ("open car door") may be next to the subject."""
    return bool(set(re.findall(r"[a-z]+", phrase.lower())) & PEOPLE_WORDS)


def on_subject(box, subject, near: bool = False) -> bool:
    """A brain box belongs to the alert's subject: enough overlap, or its centre inside the subject
    box grown a little (near=True: grown by a whole subject size, for things next to a person)."""
    if iou(box, subject) >= ON_SUBJECT_IOU:
        return True
    sw, sh = float(subject[2] - subject[0]), float(subject[3] - subject[1])
    gx, gy = (max(sw, sh), max(sw, sh)) if near else (ON_SUBJECT_GROW * sw, ON_SUBJECT_GROW * sh)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return subject[0] - gx <= cx <= subject[2] + gx and subject[1] - gy <= cy <= subject[3] + gy


def _behaviours(prompts) -> list[str]:
    """The user's suspicious phrases, tidied: no duplicates, and not the words the check itself asks."""
    seen, out = {"person", "car", *BENIGN}, []
    for p in prompts:
        p = " ".join(str(p).split())
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def judge_answer(found: list[dict], subject, noun: str, behaviours: list[str],
                 benign=BENIGN) -> tuple[bool, dict[str, tuple]]:
    """One brain answer (boxes in frame pixels) -> (subject seen, {behaviour: its box}).
    The subject is seen when a box of the noun (or of a benign twin, which also says "a person")
    lies on it; a car needs a real overlap, since parked cars stand next to each other. A behaviour
    counts on (or, for things, next to) the subject, unless a benign twin boxed the same thing."""
    seen = False
    nouns = VERIFY_NOUNS.get(noun, [noun])
    twins = [_xyxy(b) for b in found if b.get("label") in benign]
    passed: dict[str, tuple] = {}
    for b in found:
        label, xy = str(b.get("label", "")), _xyxy(b)
        if label in nouns or label in benign:
            seen = seen or (iou(xy, subject) >= 0.5 if noun in ("car", "truck") else on_subject(xy, subject))
        elif label in behaviours and label not in passed:
            if on_subject(xy, subject, near=not about_person(label)) and \
                    not any(iou(xy, t) >= SAME_BOX_IOU for t in twins):
                passed[label] = xy
    return seen, passed


def small_grey(frame: np.ndarray) -> np.ndarray:
    """Small blurred grey copy of a frame, for comparing a parked car's picture over time."""
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (HISTORY_WIDTH, max(1, round(h * HISTORY_WIDTH / w))), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (3, 3), 0).astype(np.float32)


def similarity(a: np.ndarray, b: np.ndarray, box, frame_w: int) -> float | None:
    """Normalised cross-correlation of two small_grey pictures inside box (frame pixels): about 1.0
    when nothing changed there (brightness changes do not count), lower when something moved."""
    if a.shape != b.shape:
        return None
    k = a.shape[1] / float(frame_w)
    x1, y1 = max(0, int(box[0] * k)), max(0, int(box[1] * k))
    x2, y2 = min(a.shape[1], int(np.ceil(box[2] * k))), min(a.shape[0], int(np.ceil(box[3] * k)))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    pa, pb = a[y1:y2, x1:x2], b[y1:y2, x1:x2]
    pa, pb = pa - pa.mean(), pb - pb.mean()
    norm = float(np.sqrt((pa * pa).sum() * (pb * pb).sum()))
    return float((pa * pb).sum() / norm) if norm > 1e-6 else None


# --- Fallbacks while stream.py / accel.py are missing --------------------------------

class _CvFrameSource:
    """Minimal OpenCV reader with the FrameSource interface (used only if stream.py is absent)."""

    def __init__(self, url: str, fps: float, long_side: int = 1280, gpu_decode: bool = True) -> None:
        self.url, self.fps, self.long_side = url, fps, long_side
        self._item = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.size = None
        self.backend = "cpu"

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="yg-cvsource", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def latest(self):
        return self._item

    def _run(self) -> None:
        from .video import open_capture
        while not self._stop.is_set():
            cap = open_capture(self.url)
            last = 0.0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    self._error = "No video from the camera"
                    break
                self._error = None
                now = time.time()
                if now - last < 1 / max(self.fps, 0.5):
                    continue
                last = now
                h, w = frame.shape[:2]
                k = self.long_side / max(h, w)
                if k < 1:
                    frame = cv2.resize(frame, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
                self.size = (frame.shape[1], frame.shape[0])
                self._item = (frame, now)
            cap.release()
            self._stop.wait(3)


class _UltralyticsDetector:
    """Batched YOLO with the FastDetector interface (used only if accel.py is absent)."""

    def __init__(self, model_name: str, imgsz: int) -> None:
        import torch
        from ultralytics import YOLO

        from .video import ensure_model
        self._cuda = torch.cuda.is_available()
        self.backend = "pytorch" if self._cuda else "cpu"
        self._model = YOLO(str(ensure_model(model_name)))
        self._imgsz = imgsz

    def predict(self, frames: list, conf: float) -> list[np.ndarray]:
        results = self._model.predict(frames, classes=list(CLASSES), conf=conf, imgsz=self._imgsz,
                                      device=0 if self._cuda else "cpu", verbose=False)
        out = []
        for r in results:
            b = r.boxes
            out.append(np.concatenate([b.xyxy.cpu().numpy(), b.conf.cpu().numpy()[:, None],
                                       b.cls.cpu().numpy()[:, None]], axis=1).astype(np.float32)
                       if len(b) else np.zeros((0, 6), np.float32))
        return out


def _default_source_factory(url: str, fps: float, long_side: int, gpu_decode: bool):
    try:
        from .stream import FrameSource
    except ImportError:
        log.warning("stream.py not available: using the OpenCV frame reader")
        return _CvFrameSource(url, fps, long_side, gpu_decode)
    return FrameSource(url, fps, long_side=long_side, gpu_decode=gpu_decode)


_DETECTOR_LOAD_LOCK = threading.Lock()


def _free_gpu_cache() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _default_detector(s, progress=None):
    try:
        from .accel import load_fast_detector
    except ImportError:
        log.warning("accel.py not available: using plain Ultralytics YOLO")
        return _UltralyticsDetector(s.model, s.detect_size)
    return load_fast_detector(s.model, s.detect_size, progress=progress)


# --- Monitor ---------------------------------------------------------------------------

class _Motion:
    """Cheap motion check: small blurred grey frame against a slow running background."""

    def __init__(self) -> None:
        self.bg: np.ndarray | None = None
        self.level = 0.0

    def update(self, frame: np.ndarray) -> bool:
        small = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA),
                                              cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)
        if self.bg is None or self.bg.shape != small.shape:
            self.bg = small
            return True
        self.level = float((cv2.absdiff(small, self.bg) > 18).mean())
        cv2.accumulateWeighted(small, self.bg, 0.15)
        return self.level >= MOTION_FRACTION


@dataclass
class _Cam:
    channel: int
    name: str
    role: str
    source: object
    pipeline: CameraPipeline
    motion: _Motion = field(default_factory=_Motion)
    state: str = "starting"
    error: str | None = None
    last_frame_ts: float = 0.0
    last_detect: float = -1e18
    next_detect: float = -1e18     # detection schedule: keeps detect_fps when the camera sends more frames
    last_buffered: float = -1e18   # newest frame handed to the clip recorder
    last_between: float = -1e18    # newest frame between detections searched for plates
    detect_times: deque = field(default_factory=lambda: deque(maxlen=64))
    # small grey frames every HISTORY_EVERY_S: what a parked car looked like before it "moved"
    history: deque = field(default_factory=lambda: deque(maxlen=int(HISTORY_S / HISTORY_EVERY_S) + 1))
    areas: AreaEvaluator | None = None


@dataclass(order=True)
class _Job:
    priority: tuple
    seq: int
    key: str = field(compare=False)
    items: list = field(compare=False)    # [(Alert, annotated snapshot image)]
    channel: int = field(compare=False)
    kind: str = field(compare=False)      # "person" | "car": what the alert is about
    views: list = field(compare=False)    # [(RAW frame, subject box in it, frame ts)], the alert frame first
    deadline: float = field(compare=False)        # wait this long for a starting brain (monitor clock)
    collect_until: float = field(compare=False)   # time.monotonic(): later pictures are added until then
    track_id: int | None = field(default=None, compare=False)
    gone: bool = field(default=False, compare=False)        # car_moved: the car disappeared
    unchanged: float | None = field(default=None, compare=False)  # car_moved: picture similarity before/after
    night: bool = field(default=True, compare=False)        # behaviour raises the severity only at night


class Monitor:
    """Watches the monitored cameras of the active site and raises alerts."""

    def __init__(self, cfg, store: AlertStore, brain=None, on_alert=None, on_alert_update=None,
                 on_status=None, *, detector=None, source_factory=None, clock=time.time,
                 on_area_event=None) -> None:
        self.cfg = cfg
        self.store = store
        self.brain = brain
        self.on_alert, self.on_alert_update, self.on_status = on_alert, on_alert_update, on_status
        self.on_area_event = on_area_event   # on_area_event(AreaEvent), on the areas / brain thread
        self._area_jobs: queue.Queue = queue.Queue()   # (camera, frame, [AreaHit]) to finish and save
        self._plate_reader = None
        self._plate_state = ""      # "" not tried | "ok" | "missing"
        self._plate_lock = threading.Lock()
        self._last_area_ask = -1e18
        self._ask_turn = 0
        self._detector = detector
        self._fixed_detector = detector is not None
        self._source_factory = source_factory or _default_source_factory
        self._clock = clock
        self._cams: list[_Cam] = []
        self._stop = threading.Event()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._pending: dict[str, _Job] = {}
        self._collecting: list[_Job] = []   # jobs that still take later pictures of their subject
        self._jobs_lock = threading.Lock()
        self._seq = itertools.count()
        self._sig: tuple | None = None
        self._site_id = ""
        self.message = ""           # overall state in plain words, for the UI
        self._detector_error: str | None = None
        self._next_load = 0.0
        self._clips = None          # ClipRecorder while clips are enabled
        self._last_cleanup = -1e18

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def detector_backend(self) -> str:
        return getattr(self._detector, "backend", "") if self._detector else ""

    def start(self) -> None:
        """Non-blocking: opens the cameras and starts the threads; the detector loads on the
        inference thread (a first model load can take several seconds)."""
        if self._running:
            return
        self._running = True
        # A fresh event per run: a thread that outlived stop() (stuck in a long brain call)
        # keeps its own, already set, event and exits instead of joining the new run.
        self._stop = stop = threading.Event()
        self._setup()
        kept = self._requeue_pending()
        n = self.store.resolve_pending(keep=kept)
        if n:
            log.info("Marked %d old pending alerts as unverified", n)
        self._threads = [threading.Thread(target=self._inference_loop, args=(stop,), name="yg-inference", daemon=True),
                         threading.Thread(target=self._brain_loop, args=(stop,), name="yg-brain", daemon=True),
                         threading.Thread(target=self._capture_loop, args=(stop,), name="yg-capture", daemon=True),
                         threading.Thread(target=self._areas_loop, args=(stop,), name="yg-areas", daemon=True)]
        for t in self._threads:
            t.start()
        if self.verifying():
            try:
                status = self.brain.status()
                if status == "error":  # a crashed worker is restarted, as the Brain contract allows
                    log.warning("Restarting the brain after an error: %s", getattr(self.brain, "last_error", None))
                if status in ("stopped", "error"):
                    self.brain.start()
            except Exception:
                log.exception("Could not start the brain")
        log.info("Monitoring started: %d cameras", len(self._cams))

    def stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        for t in self._threads:
            t.join(10)
            if t.is_alive():
                log.warning("Thread %s did not stop in time", t.name)
        self._threads = []
        self._running = False
        for _ in range(200):  # area events found just before the stop are still saved
            if not self.area_step():
                break
        if self._clips is not None:  # running clips are finished with the frames they have
            self._clips.stop()
        for cam in self._cams:
            try:
                cam.source.stop()
            except Exception:
                log.exception("Could not stop camera %s", cam.channel)
        while True:  # alerts still waiting for the brain are checked when monitoring starts again
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            self._finish(job, "unverified", keep_raw=True)
        with self._jobs_lock:
            self._pending.clear()
            self._collecting.clear()
        if not self._fixed_detector:
            self._detector = None  # frees GPU memory once the threads are gone
        self._unload_plates()
        self.message = "Monitoring is off"
        log.info("Monitoring stopped")

    def restart(self) -> None:
        self.stop()
        self.start()

    def _signature(self) -> tuple:
        s = self.cfg.settings
        site = self.cfg.active_site()
        cams = [c for c in site.enabled_cameras() if c.monitor] if site else []
        chans = tuple(c.channel for c in cams)
        sizes = tuple(self.source_size(c) for c in cams)  # a plate line added / removed: new frame size
        return (site.id if site else "", site.host if site else "", chans, s.stream, s.detect_fps,
                s.detect_size, self.gpu_decode(), s.model, bool(s.clips_enabled), self.source_fps(), sizes)

    def source_size(self, camcfg) -> int:
        """Long side of a camera's frames: the detection size, or up to plate_long_side (the native
        1080p of the main stream) on cameras with a number plate line. FrameSource never upscales,
        detection resizes to detect_size itself and boxes stay in frame pixels."""
        s = self.cfg.settings
        if reads_plates(getattr(camcfg, "areas", None)):
            return max(int(s.detect_size), int(getattr(s, "plate_long_side", 0) or 0))
        return int(s.detect_size)

    def source_fps(self) -> float:
        """Cameras deliver frames at the clip rate when clips are on; detection keeps its own
        rate (see step())."""
        s = self.cfg.settings
        fps = float(s.detect_fps)
        return max(fps, float(s.clip_fps)) if s.clips_enabled else fps

    def gpu_decode(self) -> bool:
        """NVDEC costs ~210 MB of GPU memory per camera and saves very little CPU; with the brain
        (~9 GB) loaded on a 12 GB card that is the difference between fitting and not."""
        s = self.cfg.settings
        return bool(s.gpu_decode) and (self.brain is None or s.brain_mode == "off")

    def verifying(self) -> bool:
        """Alerts get a second look from the brain: verify mode, and the user did not stop the
        brain (then alerts are "unverified" until they start it again)."""
        return (self.brain is not None and self.cfg.settings.brain_mode != "off"
                and not getattr(self.brain, "user_stopped", False))

    def needs_restart(self) -> bool:
        """True when a setting that cannot change live (cameras, stream, fps, model...) changed."""
        return self._sig is not None and self._sig != self._signature()

    def _setup(self) -> None:
        s = self.cfg.settings
        site = self.cfg.active_site()
        self._sig = self._signature()
        self._site_id = site.id if site else ""
        self._cams = []
        if self._clips is not None:
            self._clips.stop()
        self._clips = None
        if not site:
            self.message = "Add a site with cameras to start monitoring"
            return
        if s.clips_enabled:
            from .clips import ClipRecorder
            self._clips = ClipRecorder(self.store.dir, s.clip_fps, on_done=self._clip_done)
        for c in site.enabled_cameras():
            if not c.monitor:
                continue
            src = self._source_factory(site.rtsp_url(c.channel, main=s.stream == "main"),
                                       self.source_fps(), self.source_size(c), self.gpu_decode())
            src.start()
            self._cams.append(_Cam(c.channel, c.label, c.role, src, CameraPipeline(c.channel, c.label, c.role),
                                   areas=AreaEvaluator(c.channel, c.label, site.id, seed=self._counts_today)))
        self.message = "Starting cameras" if self._cams else "No cameras are set to be monitored"

    # -- status ---------------------------------------------------------------------------
    def camera_status(self) -> dict[int, dict]:
        now = self._clock()
        out = {}
        for cam in self._cams:
            times = list(cam.detect_times)  # copy: appended on another thread
            recent = [t for t in times if now - t <= 5]
            fps = round(len(recent) / 5, 1) if recent else 0.0
            out[cam.channel] = {
                "state": cam.state, "fps": fps, "objects": cam.pipeline.counts() if cam.state != "offline" else {},
                "backend": getattr(cam.source, "backend", ""), "error": cam.error,
                "name": cam.name, "detector": self.detector_backend, "motion": round(cam.motion.level, 4),
            }
        return out

    def summary(self) -> dict:
        return {"running": self.running, "message": self.message, "detector": self.detector_backend,
                "cameras": len(self._cams), "watching": sum(c.state in ("watching", "idle") for c in self._cams),
                "brain_queue": self._jobs.qsize()}

    def _emit_status(self) -> None:
        if self.on_status:
            try:
                self.on_status(self.camera_status())
            except Exception:
                log.exception("on_status callback failed")

    # -- inference --------------------------------------------------------------------------
    def _ensure_detector(self, stop: threading.Event | None = None) -> bool:
        if self._detector is not None:
            return True
        now = self._clock()
        if now < self._next_load:
            return False
        self.message = "Preparing the AI (the first start can take a few minutes)"
        self._emit_status()
        try:
            # One load at a time: an inference thread that outlived stop() may still be loading,
            # and two copies of the model next to the brain would not fit on the GPU.
            with _DETECTOR_LOAD_LOCK:
                if self._detector is not None:
                    return True
                if stop is not None and stop.is_set():
                    return False
                detector = _default_detector(self.cfg.settings)
                if stop is not None and stop.is_set():  # stopped while loading: do not keep it
                    del detector
                    _free_gpu_cache()
                    return False
                self._detector = detector
        except Exception as exc:
            log.exception("Could not load the detector")
            self._detector_error = f"The AI model could not start: {exc}"
            self.message = self._detector_error
            self._next_load = now + 30
            for cam in self._cams:
                cam.error = self._detector_error
            return False
        self._detector_error = None
        self.message = f"Watching {len(self._cams)} cameras"
        log.info("Detector ready (%s)", self.detector_backend)
        return True

    def step(self, now: float | None = None, stop: threading.Event | None = None) -> int:
        """One inference pass over all cameras; returns how many frames were analysed."""
        now = self._clock() if now is None else now
        s = self.cfg.settings
        if not self._ensure_detector(stop):
            return 0
        batch = []
        for cam in self._cams:
            src = cam.source
            item = src.latest()
            if item is None:
                cam.error = getattr(src, "error", None)
                cam.state = "offline" if cam.error else "starting"
                continue
            frame, ts = item
            if now - ts > STALE_S:
                cam.state = "offline"
                cam.error = getattr(src, "error", None) or f"No new video for {int(now - ts)} s"
                continue
            cam.error = None
            # A schedule, not "time since the last check": cameras may send frames faster than
            # detect_fps (for clips), and on that finer frame grid a minimum gap alone would land
            # on every other frame and lower the rate.
            period = 1.0 / max(0.5, float(s.detect_fps))
            if ts <= cam.last_frame_ts or now < cam.next_detect - 0.1 * period:
                if ts > cam.last_frame_ts and ts > cam.last_between:  # a frame detection skips
                    cam.last_between = ts
                    self._areas_between(cam, frame, ts)
                continue
            cam.last_frame_ts = ts
            cam.next_detect = cam.next_detect + period if now - cam.next_detect < period else now + period
            moving = cam.motion.update(frame)
            if (s.motion_gating and not moving and not cam.pipeline.has_people()
                    and now - cam.last_detect < KEEPALIVE_S):
                cam.state = "idle"
                continue
            batch.append((cam, frame, ts))
        if not batch:
            return 0
        # Low threshold on purpose: weak hits keep known objects alive on noisy night frames;
        # the tracker only lets hits above the user's certainty start new objects.
        results = self._detector.predict([f for _, f, _ in batch], min(LOW_SCORE, s.confidence))
        for (cam, frame, ts), dets in zip(batch, results):
            cam.last_detect = now
            cam.detect_times.append(now)
            cam.state = "watching"
            camcfg = self._camera_cfg(cam.channel)
            h, w = frame.shape[:2]
            hits = cam.pipeline.process(dets, ts, (w, h), active_zones(camcfg) if camcfg else [], s)
            self._remember(cam, frame, ts)
            self._areas_step(cam, camcfg, frame, ts)
            for hit in hits:
                try:
                    self._raise(cam, frame, ts, hit)
                except Exception:
                    log.exception("Could not save an alert")
            self._add_views(cam, frame, ts)
        return len(batch)

    @staticmethod
    def _remember(cam: _Cam, frame: np.ndarray, ts: float) -> None:
        if not cam.history or ts - cam.history[-1][0] >= HISTORY_EVERY_S:
            cam.history.append((ts, small_grey(frame)))

    def _camera_cfg(self, channel: int):
        site = self.cfg.site(self._site_id)
        return site.camera(channel) if site else None

    def _inference_loop(self, stop: threading.Event) -> None:
        last_status = 0.0
        next_t = time.monotonic()
        while not stop.is_set():
            try:
                self.step(stop=stop)
            except Exception as exc:
                log.exception("Detection failed")
                self.message = f"Detection error: {exc}"
                stop.wait(1)
            if time.monotonic() - last_status >= 1.0:
                last_status = time.monotonic()
                self._emit_status()
            # Ticking at twice the detection rate catches each camera's new frame soon after it
            # arrives (cameras are not in phase); the per-camera interval in step() caps the rate.
            next_t += 0.5 / max(0.5, float(self.cfg.settings.detect_fps))
            delay = next_t - time.monotonic()
            if delay < 0:
                next_t, delay = time.monotonic(), 0.0
            stop.wait(delay)

    # -- clips -----------------------------------------------------------------------------------
    def capture(self, now: float | None = None) -> int:
        """Hand every new camera frame to the clip recorder, also the frames detection skips;
        finish clips whose camera went quiet. Returns how many frames were buffered."""
        rec = self._clips
        if rec is None:
            return 0
        n = 0
        for cam in self._cams:
            item = cam.source.latest()
            if item is None or item[1] <= cam.last_buffered:
                continue
            frame, ts = item
            cam.last_buffered = ts
            n += rec.add_frame(cam.channel, frame, ts)
        rec.tick(self._clock() if now is None else now)
        return n

    def _capture_loop(self, stop: threading.Event) -> None:
        from .clips import CLEANUP_EVERY_S
        while not stop.is_set():
            try:
                self.capture()
            except Exception:
                log.exception("Clip buffering failed")
                stop.wait(1)
            if time.time() - self._last_cleanup >= CLEANUP_EVERY_S:  # at start, then hourly
                self._last_cleanup = time.time()
                threading.Thread(target=self.cleanup_clips, name="yg-clip-cleanup", daemon=True).start()
            # Polling several times per frame interval catches each frame of every camera.
            stop.wait(0.25 / max(1.0, self.source_fps()) if self._clips is not None else 1.0)

    def cleanup_clips(self):
        """Apply the area log and clip storage limits now; returns clips.CleanupResult (None on error)."""
        from .clips import cleanup
        try:
            self.store.cleanup_area_events(self.cfg.settings.area_log_days)
        except Exception:
            log.exception("Area log cleanup failed")
        try:
            return cleanup(self.store, self.cfg.settings)
        except Exception:
            log.exception("Clip cleanup failed")
            return None

    def _start_clip(self, alert: Alert) -> None:
        if self._clips is not None:
            try:
                self._clips.on_alert(alert)
            except Exception:
                log.exception("Could not start a clip for %s", alert.id)

    def _clip_done(self, writer) -> None:
        """A clip file is complete (or failed): tell its alerts, on the writer's thread."""
        kept = False
        for alert in writer.alerts:
            if isinstance(alert, AreaEvent):  # an area event's clip (log_plates, or next to its alert)
                if writer.ok:
                    try:
                        self.store.set_area_event_clip(alert.id, str(writer.path), alert.clip_offset_s)
                        kept = kept or self.store.get_area_event(alert.id) is not None
                    except Exception:
                        log.exception("Could not save the clip of %s", alert.id)
                continue
            if writer.ok:
                alert.clip_path = str(writer.path)
            else:
                alert.clip_offset_s = -1.0
            try:
                self.store.update(alert)
                fresh = self.store.get(alert.id)
            except Exception:  # store closed (events folder switched while finishing)
                log.exception("Could not save the clip of %s", alert.id)
                continue
            if fresh is not None:
                kept = True
                self._callback(self.on_alert_update, fresh)
        if writer.ok and not kept:  # every alert was deleted while recording
            _unlink([str(writer.path)])

    # -- alerts ----------------------------------------------------------------------------------
    def _raise(self, cam: _Cam, frame: np.ndarray, ts: float, hit: RuleHit) -> Alert:
        s = self.cfg.settings
        boxes = [box_dict(hit.subject[0], hit.subject[1], "subject")]
        boxes += [box_dict(b, label, "context") for b, label in hit.context]
        others = [box_dict(t.box, t.label, "other") for t in cam.pipeline.visible(ts)
                  if not any(iou(t.box, (b["x1"], b["y1"], b["x2"], b["y2"])) > 0.9 for b in boxes)]
        stamp = datetime.fromtimestamp(ts)
        verify = self.verifying() and hit.verify
        alert = Alert(id=f"{stamp:%Y%m%d-%H%M%S}-{cam.channel}-{uuid.uuid4().hex[:6]}", site_id=self._site_id,
                      channel=cam.channel, camera=cam.name, ts=ts, rule=hit.rule, title=hit.title,
                      detail=hit.detail, severity=hit.severity, boxes=boxes,
                      verdict="pending" if verify else "unverified")
        annotated = draw_boxes(frame.copy(), others + boxes[::-1])  # subject drawn last, on top
        h, w = frame.shape[:2]
        region = crop_region([hit.subject[0]] + [b for b, _ in hit.context], w, h)
        self._save_pictures(alert, annotated, region, stamp)
        if verify:  # the RAW picture: the brain must not see drawn boxes, and it survives a restart
            try:
                _write_jpg(Path(raw_path(alert.snapshot_path)), frame)
            except OSError:
                log.warning("Could not save the picture to verify for %s", alert.id)
        self._start_clip(alert)
        self.store.add(alert, gap_s=s.incident_gap_s)
        log.info("Alert %s: %s on %s (%s)", alert.id, alert.rule, cam.name, alert.severity)
        self._callback(self.on_alert, alert)
        if verify:
            night = 0 if hit.night else 1
            parking = 0 if cam.role == "Parking" else 1
            kind = hit.kind or ("car" if hit.rule == "car_moved" else "person")
            self._queue_verify(f"{cam.channel}:{hit.track_key}", (night, parking, ts), alert, annotated,
                               frame.copy(), hit.subject[0], kind, channel=cam.channel, ts=ts,
                               track_id=hit.track_id, gone=hit.gone, night=hit.night,
                               unchanged=self._unchanged(cam, frame, hit) if hit.rule == "car_moved" else None)
        return alert

    @staticmethod
    def _unchanged(cam: _Cam, frame: np.ndarray, hit: RuleHit) -> float | None:
        """car_moved: how alike the parked car's place looks now and just before the change began
        (None when there is no picture from before). Two parked cars overlapping in the picture can
        swap boxes: then the rule sees a "move" while the picture shows nothing new."""
        before = next((g for t, g in reversed(cam.history) if t <= hit.since - HISTORY_EVERY_S), None)
        if before is None:
            return None
        area = [hit.subject[0]] + [b for b, label in hit.context if label == "parked here"]
        arr = np.array([b[:4] for b in area], np.float32)
        box = (arr[:, 0].min(), arr[:, 1].min(), arr[:, 2].max(), arr[:, 3].max())
        return similarity(before, small_grey(frame), box, frame.shape[1])

    def _save_pictures(self, alert: Alert, annotated: np.ndarray, region, stamp: datetime) -> None:
        # The store's folder, not the setting: the app falls back to a local folder when the
        # configured one (e.g. an unplugged drive) cannot be opened.
        folder = self.store.dir / "alerts" / f"{stamp:%Y-%m-%d}"
        folder.mkdir(parents=True, exist_ok=True)
        snap, crop = folder / f"{alert.id}.jpg", folder / f"{alert.id}_crop.jpg"
        x1, y1, x2, y2 = region
        _write_jpg(snap, annotated)
        _write_jpg(crop, annotated[y1:y2, x1:x2])
        alert.snapshot_path, alert.crop_path = str(snap), str(crop)

    def _callback(self, fn, alert: Alert) -> None:
        if fn:
            try:
                fn(alert)
            except Exception:
                log.exception("Alert callback failed")

    # -- areas ------------------------------------------------------------------------------------
    def area_live(self, channel: int) -> dict[str, dict]:
        """What each area of the camera shows now (see AreaEvaluator.live); {} while not monitored.
        Cheap and thread-safe: the GUI may call it every frame."""
        cam = next((c for c in self._cams if c.channel == channel), None)
        if cam is None or cam.areas is None or not self._running:
            return {}
        return cam.areas.live(self._clock())

    def reset_area_reference(self, channel: int, area_id: str) -> bool:
        """The area's current look is its normal one (left_object / state_watch). False when the
        camera is not monitored or the area has not been seen yet."""
        cam = next((c for c in self._cams if c.channel == channel), None)
        if cam is None or cam.areas is None or not self._running:
            return False
        return cam.areas.reset_reference(area_id)

    def _counts_today(self, area_id: str) -> tuple[int, int]:
        """Today's in / out counts from the log, so a restart does not reset the Live view."""
        midnight = datetime.fromtimestamp(self._clock()).replace(hour=0, minute=0, second=0, microsecond=0)
        stats = self.store.area_stats(area_id, since=midnight.timestamp())
        return stats["in"], stats["out"]

    def _areas_step(self, cam: _Cam, camcfg, frame: np.ndarray, ts: float) -> None:
        """Inference thread: evaluate the camera's areas on this detection; events are finished
        (plates, pictures, alerts) on the areas thread so detection never waits for them."""
        if cam.areas is None:
            return
        # a copy of the list: the Live view may add or delete areas meanwhile
        areas = [a for a in list(camcfg.areas) if a.get("task") != "watch_zone"] if camcfg else []
        if not areas:
            cam.areas.reset()  # the last area was deleted: nothing left to show
            return
        s = self.cfg.settings
        # Only tracks seen now (and parked cars briefly missed): a track the tracker still keeps with
        # a frozen box would hide that the object vanished, so a new id for it could not take over.
        tracks = cam.pipeline.visible(ts)
        try:
            hits = cam.areas.update(areas, tracks, frame, ts, (s.night_start, s.night_end))
        except Exception:
            log.exception("Areas of camera %s failed", cam.channel)
            return
        if hits:
            self._area_jobs.put((cam, frame.copy(), hits))

    def _areas_between(self, cam: _Cam, frame: np.ndarray, ts: float) -> None:
        """Inference thread, a frame between two detections: plate lines keep the moving vehicles'
        pictures (AreaEvaluator.between); nothing to do on cameras without one."""
        camcfg = self._camera_cfg(cam.channel)
        areas = list(camcfg.areas) if camcfg else []
        if cam.areas is None or not reads_plates(areas):
            return
        try:
            cam.areas.between(areas, frame, ts)
        except Exception:
            log.exception("Plate pictures of camera %s failed", cam.channel)

    def area_step(self, timeout: float = 0.0) -> bool:
        """Finish one batch of area events (see _areas_step). Returns True if there was one."""
        try:
            cam, frame, hits = self._area_jobs.get(timeout=timeout) if timeout else self._area_jobs.get_nowait()
        except queue.Empty:
            return False
        for hit in hits:
            try:
                self._finish_area_hit(cam, frame, hit)
            except Exception:
                log.exception("Could not save the area event %s", hit.event.kind)
        return True

    def _areas_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.area_step(timeout=0.5)
            except Exception:
                log.exception("Area step failed")
                stop.wait(1)

    def _finish_area_hit(self, cam: _Cam, frame: np.ndarray, hit: AreaHit) -> AreaEvent:
        ev, area = hit.event, hit.area
        frame = hit.frame if hit.frame is not None else frame
        plate_crop, best = self._read_plate(hit) if ev.kind == "plate" else (None, None)
        if best is not None:  # the moment the plate was best seen: the event's (and alert's) picture
            frame, hit.box = best.frame, best.box
            ev.extra["best_ts"] = round(best.ts, 3)
        self._save_area_pictures(ev, frame, hit, plate_crop)
        alert = None
        if area.get("alert") and ev.kind in ALERT_KINDS and ev.value != "restored" \
                and not (ev.kind == "plate" and ev.extra.get("known")):
            alert = self._area_alert(cam, frame, hit)
            if alert is not None:
                ev.extra["alert_id"] = alert.id
        # Only events that raise an alert get a clip. Logging (every passing car's plate, counts) keeps
        # the close-up and the whole picture: a clip per passing car is not worth the storage.
        if self._clips is not None and alert is not None:
            try:  # shares the alert's clip (same camera, same moment) or starts one
                self._clips.on_alert(ev)
            except Exception:
                log.exception("Could not start a clip for %s", ev.id)
        if best is not None and ev.clip_offset_s >= 0:  # the clip player opens at the best moment
            ev.extra["best_offset_s"] = round(max(0.0, ev.clip_offset_s + best.ts - ev.ts), 3)
        self.store.add_area_event(ev)
        log.info("Area event %s: %s", ev.id, describe(ev))
        if self.on_area_event:
            try:
                self.on_area_event(ev)
            except Exception:
                log.exception("Area event callback failed")
        return ev

    # plates
    def _plates(self):
        """The PlateReader (plates.py), loaded on first use; None when it is not available."""
        with self._plate_lock:
            if self._plate_state == "":
                try:
                    from .plates import PlateReader
                    self._plate_reader, self._plate_state = PlateReader(), "ok"
                except Exception:
                    log.warning("Number plate reading is not available", exc_info=True)
                    self._plate_state = "missing"
            return self._plate_reader

    @staticmethod
    def _plate_models_ok() -> bool:
        """Never resume a model download the user paused or cancelled in Settings."""
        try:
            from .plates import auto_download_ok
            return auto_download_ok()
        except Exception:
            return False

    def _unload_plates(self) -> None:
        """Frees the plate models (GPU memory the AI brain may need); loaded again on the next plate."""
        with self._plate_lock:
            reader = self._plate_reader
            if reader is not None and hasattr(reader, "unload"):
                try:
                    reader.unload()
                except Exception:
                    log.exception("Could not unload the plate reader")

    def _read_plate(self, hit: AreaHit):
        """Best-of-N plate reading of the vehicle's pictures; fills the event. Returns (the plate
        picture or None, the vehicle's best moment: an areas.PlateCandidate or None)."""
        ev, params = hit.event, hit.area.get("params") or {}
        cands = hit.plate_cands
        result = None
        reader = self._plates() if hit.plate_crops else None
        first = (hit.plate_crops[0] if hit.plate_crops else None), (cands[0] if cands else None)
        if reader is not None and not getattr(reader, "loaded", True) and not self._plate_models_ok():
            ev.value, ev.confidence = "", 0.0
            ev.extra["note"] = "plate model not downloaded"
            return first
        if reader is not None:
            try:
                with self._plate_lock:  # one reading at a time (GPU)
                    result = reader.read(hit.plate_crops)
            except Exception:
                log.exception("Plate reading failed")
        index = (result or {}).get("index")
        best = cands[index] if isinstance(index, int) and 0 <= index < len(cands) else first[1]
        crop = (result or {}).get("plate_crop")
        crop = crop if isinstance(crop, np.ndarray) and crop.size else first[0]
        if (result or {}).get("plate_px"):
            ev.extra["plate_px"] = int(result["plate_px"])
        text = str((result or {}).get("text") or "").strip()
        if not text:
            ev.value, ev.confidence = "", 0.0
            ev.extra["note"] = "plate unreadable"
            return crop, best
        ev.value, ev.confidence = text, round(float(result.get("confidence") or 0.0), 3)
        known = [k for k in params.get("known") or [] if plate_distance(k, text) <= 1]
        ev.extra["known"] = bool(known)
        if ev.extra.get("direction") == "out":  # how long it stayed: the last time it came in
            for prev in self.store.list_area_events(area_id=ev.area_id, kind="plate", until=ev.ts, limit=300):
                if prev.extra.get("direction") == "in" and prev.value and plate_distance(prev.value, text) <= 1:
                    ev.extra["stay_s"] = round(ev.ts - prev.ts, 1)
                    ev.extra["in_event_id"] = prev.id
                    break
                if prev.extra.get("direction") == "out" and prev.value and plate_distance(prev.value, text) <= 1:
                    break  # it went out since: that visit is over
        return crop, best

    def _save_area_pictures(self, ev: AreaEvent, frame: np.ndarray, hit: AreaHit, plate_crop) -> None:
        """The event's picture (the area drawn, the subject boxed) and a close-up: the plate, the
        subject, or the area itself."""
        stamp = datetime.fromtimestamp(ev.ts)
        folder = self.store.dir / "areas" / f"{stamp:%Y-%m-%d}"
        folder.mkdir(parents=True, exist_ok=True)
        h, w = frame.shape[:2]
        annotated = draw_area(frame.copy(), hit.area)
        if hit.box is not None:
            draw_boxes(annotated, [box_dict(hit.box, ev.value if ev.kind == "plate" and ev.value else ev.label,
                                            "subject")])
        if plate_crop is not None:
            close = plate_crop
        elif hit.box is not None:
            x1, y1, x2, y2 = crop_region([hit.box], w, h, min_side=240)
            close = annotated[y1:y2, x1:x2]
        else:
            x1, y1, x2, y2 = area_rect(hit.area, w, h, pad=0.1)
            close = frame[y1:y2, x1:x2]
        snap, crop = folder / f"{ev.id}.jpg", folder / f"{ev.id}_crop.jpg"
        k = 1280 / max(h, w)
        if k < 1:
            annotated = cv2.resize(annotated, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
        _write_jpg(snap, annotated)
        ev.snapshot_path = str(snap)
        if close is not None and close.size:
            _write_jpg(crop, close)
            ev.crop_path = str(crop)

    def _area_alert(self, cam: _Cam, frame: np.ndarray, hit: AreaHit) -> Alert | None:
        """Raise the area's event as an alert (rule "area_<task>"), so it joins Incidents with a clip.
        Events about a person or a vehicle are checked by the brain like the other alerts."""
        ev, area = hit.event, hit.area
        h, w = frame.shape[:2]
        box = hit.box if hit.box is not None else area_rect(area, w, h)
        label = ev.label or "area"
        # the brain is asked for the object itself: a motorcycle is "no car" to it
        group = ev.label if ev.label in VEHICLES else "person"
        about = ev.track_id if ev.track_id is not None else ev.kind
        detail = f"{KIND_TEXT.get(ev.kind, ev.kind)} in the area “{area['name']}” at {_clock_text(ev.ts)}."
        if ev.kind == "plate" and not ev.value:
            detail += " The number plate could not be read."
        rule_hit = RuleHit(f"area_{ev.task}", f"{area['id']}:{about}", describe(ev), detail, "warning",
                           (np.array(box, np.float32), label), [], hit.night, f"a{area['id']}:{about}",
                           track_id=ev.track_id, kind=group, verify=hit.box is not None and bool(ev.label))
        if not cam.pipeline.cooldown_ok(rule_hit, ev.ts, self.cfg.settings.alert_cooldown_s):
            return None
        return self._raise(cam, frame, ev.ts, rule_hit)

    # ask_ai
    def area_ai_step(self) -> bool:
        """Ask the brain one due ask_ai question, at the lowest priority: only while no alert waits
        for it, at most every AREA_ASK_GAP_S. Returns True if a question was asked."""
        due = [(c, c.areas) for c in self._cams if c.areas is not None and c.areas.pending_asks()]
        if not due:
            return False
        # Asked whenever the brain runs, whatever "How to use it" says: that setting is about alerts.
        if self.brain is None or getattr(self.brain, "user_stopped", False) or not self._brain_ready():
            for _, ev in due:  # nobody to ask: they are tried again later
                while (ask := ev.take_ask()) is not None:
                    ev.ai_answer(ask, None)
            return False
        with self._jobs_lock:
            busy = bool(self._pending) or not self._jobs.empty()
        if busy or time.monotonic() - self._last_area_ask < AREA_ASK_GAP_S:
            return False
        cam, ev = due[self._ask_turn % len(due)]  # cameras take turns
        self._ask_turn += 1
        ask = ev.take_ask()
        if ask is None:
            return False
        self._last_area_ask = time.monotonic()
        yes, confidence = self._ask_area(cam, ask)
        hits = ev.ai_answer(ask, yes, confidence)
        if hits:
            self._area_jobs.put((cam, ask["frame"], hits))
        return True

    def _ask_area(self, cam: _Cam, ask: dict) -> tuple[bool | None, float]:
        """yes / no for the area's question. A change of answer is double-checked with a second
        sample: one odd answer does not flip the log."""
        question = ask["question"]
        if not question:
            return None, 0.0
        s = self.cfg.settings
        try:
            def sample(seed: int) -> bool:
                found = self.brain.ask(ask["crop"], [question], mode=s.brain_generation, timeout=BRAIN_TIMEOUT_S,
                                       max_side=VERIFY_SIDE, upscale=True, seed=seed)
                return any(b.get("label") == question for b in found)
            yes = sample(0)
            before = cam.areas.current_answer(ask["area"]["id"])
            if before is not None and yes != before and sample(1) != yes:
                return before, 0.5  # the samples disagree: keep the answer we had
            log.info("Area %s asked %r -> %s", ask["area"].get("name"), question, "yes" if yes else "no")
            return yes, 1.0
        except Exception as exc:  # BrainError: not ready, timeout or crash
            log.warning("Area question failed: %s", exc)
            return None, 0.0

    # -- brain ----------------------------------------------------------------------------------
    def _queue_verify(self, key: str, priority: tuple, alert: Alert, annotated, frame: np.ndarray, subject,
                      kind: str = "person", *, channel: int = 0, ts: float = 0.0, track_id: int | None = None,
                      gone: bool = False, unchanged: float | None = None, night: bool = True,
                      collect: bool = True) -> None:
        """frame: the RAW frame of the alert; subject: the box (in it) the alert is about.
        collect: later pictures of the subject are added while the job waits (see _add_views)."""
        with self._jobs_lock:
            job = self._pending.get(key)
            if job is not None:  # same person / car already waiting: one check answers both
                job.items.append((alert, annotated))
                return
            job = _Job(priority, next(self._seq), key, [(alert, annotated)], channel, kind,
                       [(frame, np.array(subject[:4], np.float32), ts)], self._clock() + BRAIN_WAIT_START_S,
                       time.monotonic() + (VOTE_WAIT_S if collect else 0.0), track_id, gone, unchanged, night)
            self._pending[key] = job
            if collect:
                self._collecting.append(job)
        self._jobs.put(job)

    def _add_views(self, cam: _Cam, frame: np.ndarray, ts: float) -> None:
        """Later pictures of the subjects whose alerts wait for the brain: the same track, at least
        VOTE_GAP_S apart (a parked car stays where it is). Inference thread."""
        with self._jobs_lock:
            if not self._collecting:
                return
            now = time.monotonic()
            self._collecting = [j for j in self._collecting if len(j.views) < VOTE_FRAMES and now <= j.collect_until]
            jobs = [j for j in self._collecting if j.channel == cam.channel and ts - j.views[-1][2] >= VOTE_GAP_S]
        for job in jobs:
            if job.track_id is None:
                box = job.views[0][1]
            else:
                track = next((t for t in cam.pipeline.visible(ts) if t.id == job.track_id and t.last_seen == ts), None)
                if track is None:
                    continue
                box = track.box.copy()
            job.views.append((frame.copy(), np.array(box, np.float32), ts))

    def _requeue_pending(self) -> list[str]:
        """Alerts a stop or crash left without a verdict, whose RAW picture is still there, are
        checked again (with that one picture). Returns their ids."""
        if not self.verifying():
            return []
        kept = []
        for a in self.store.list(limit=50, verdict="pending") + self.store.list(limit=50, verdict="unverified"):
            raw = raw_path(a.snapshot_path)
            if not raw or not Path(raw).exists():
                continue
            subject = next((b for b in a.boxes if b.get("role") == "subject"), None)
            frame = _read_jpg(raw)
            if subject is None or frame is None or self._clock() - a.ts > BRAIN_WAIT_START_S * 30:
                _unlink([raw])  # too old to matter now
                continue
            annotated = _read_jpg(a.snapshot_path)
            a.verdict = "pending"
            self.store.update(a)
            self._callback(self.on_alert_update, a)
            kind = "car" if a.rule == "car_moved" else "person"
            self._queue_verify(f"re:{a.id}", (1, 1, a.ts), a, annotated if annotated is not None else frame.copy(),
                               frame, _xyxy(subject), kind, channel=a.channel, ts=a.ts,
                               gone="disappeared" in (a.detail or ""), collect=False,
                               night=is_night(a.ts, self.cfg.settings.night_start, self.cfg.settings.night_end))
            kept.append(a.id)
        if kept:
            log.info("Checking %d alerts again that were waiting for the AI brain", len(kept))
        return kept

    def _brain_ready(self, wait_until: float = 0.0) -> bool:
        if self.brain is None:
            return False
        status = self.brain.status()
        while status in ("starting", "installing") and self._clock() < wait_until and not self._stop.is_set():
            self._stop.wait(1)
            status = self.brain.status()
        return status in ("ready", "busy")

    def brain_step(self, timeout: float = 0.0, wait_views: bool = False) -> bool:
        """Handle one verify job. Returns True if something was done. wait_views: first wait (up to
        VOTE_WAIT_S after the alert) for later pictures of the subject; the brain thread does."""
        try:
            job = self._jobs.get(timeout=timeout) if timeout else self._jobs.get_nowait()
        except queue.Empty:
            return False
        stop = self._stop
        while wait_views and len(job.views) < VOTE_FRAMES and time.monotonic() < job.collect_until \
                and not stop.is_set():
            stop.wait(0.1)
        with self._jobs_lock:
            self._pending.pop(job.key, None)
            if job in self._collecting:
                self._collecting.remove(job)
        if wait_views and stop.is_set():  # monitoring stops: checked when it starts again
            self._finish(job, "unverified", keep_raw=True)
            return True
        self._verify(job)
        return True

    def _brain_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                # ask_ai areas only when no alert waits: they never delay a verification
                if not self.brain_step(timeout=0.5, wait_views=True):
                    self.area_ai_step()
            except Exception:
                log.exception("Brain step failed")
                stop.wait(2)

    def _verify(self, job: _Job) -> None:
        if not self.verifying():
            self._finish(job, "unverified")
            return
        views = list(job.views)
        frame, subject, _ = views[0]
        if job.kind == "car" and job.unchanged is not None and job.unchanged >= STILL_NCC:
            log.info("Brain %s: parked car picture unchanged (similarity %.2f)", job.key, job.unchanged)
            self._finish(job, "rejected", ["The parked car did not move: its picture is the same as before"])
            return
        if too_small(subject, frame.shape[0]):
            self._finish(job, "unverified", ["Too small or too far away for the AI brain to judge"])
            return
        if not self._brain_ready(job.deadline):
            self._finish(job, "unverified")
            return
        try:
            self._finish(job, *self._judge(job, views))
        except Exception as exc:  # BrainError: not ready, timeout or crash
            log.warning("Brain check failed for %s: %s", job.key, exc)
            self._finish(job, "unverified")

    def _judge(self, job: _Job, views: list) -> tuple:
        """Ask the brain about up to VOTE_FRAMES pictures of the subject (later frames of its track,
        else other samples of the newest one) and vote: (verdict, findings, brain boxes, escalate).
        Stops as soon as more pictures cannot change the outcome."""
        s = self.cfg.settings
        noun = job.kind
        vehicle = noun in VEHICLES
        nouns = VERIFY_NOUNS.get(noun, [noun])
        prompts = [] if vehicle else _behaviours(s.brain_prompts)
        benign = [] if vehicle else list(BENIGN)
        chunks = [prompts[i:i + BEHAVIOUR_PER_CALL] for i in range(0, len(prompts), BEHAVIOUR_PER_CALL)] or [[]]
        plan = [(v, 0) for v in views[:VOTE_FRAMES]]
        plan += [(views[-1], k) for k in range(1, VOTE_FRAMES - len(plan) + 1)]  # new seeds, same picture
        yes = no = 0
        votes: Counter = Counter()
        where: dict[str, tuple] = {}
        for k, ((frame, subject, _), seed) in enumerate(plan):
            asked = chunks if k == 0 else [[p for p in prompts if votes[p]]]
            seen = False
            for chunk in asked:
                found = self._ask(job, frame, subject, nouns + benign + chunk, seed)
                hit, passed = judge_answer(found, subject, noun, chunk, benign)
                seen = seen or hit
                for p, box in passed.items():
                    votes[p] += 1
                    where.setdefault(p, box)
            yes, no = yes + seen, no + (not seen)
            left = len(plan) - k - 1
            if no > yes + left:
                break  # cannot be confirmed any more
            undecided = [p for p in votes if votes[p] < VOTE_MIN <= votes[p] + left]
            if yes >= no + left and not undecided:
                break
        n = yes + no
        if job.gone:  # where the car was parked: rejected when the brain still sees it there
            return (("rejected", [f"The car is still there ({yes} of {n} pictures)"], [], False) if yes > no
                    else ("confirmed", ["The parked car is no longer there"], [], False))
        if not yes or no > yes:
            return "rejected", [f"No {noun} found where the alert was ({no} of {n} pictures)"], [], False
        strong = [p for p in prompts if votes[p] >= VOTE_MIN]
        weak = [p for p in prompts if 0 < votes[p] < VOTE_MIN]
        if weak:
            log.info("Brain %s: not enough evidence for %s", job.key, {p: votes[p] for p in weak})
        things = [p for p in strong if not is_pose_phrase(p)]
        poses = [p for p in strong if is_pose_phrase(p)]
        findings = [f"A real {noun} is there ({yes} of {n} pictures)"]
        findings += [f"{p[:1].upper() + p[1:]} (seen in {votes[p]} of {n} pictures)" for p in things]
        findings += [f"Maybe: {p} (the AI cannot judge poses reliably)" for p in poses]
        boxes = [box_dict(where[p], p, "brain") for p in things]
        # By day, owners open doors and load cars all the time: the behaviour is reported, but only at
        # night, and only for things the model can really see, does it make the alert critical.
        return "confirmed", findings, boxes, bool(things) and job.night

    def _ask(self, job: _Job, frame: np.ndarray, subject, phrases: list[str], seed: int) -> list[dict]:
        """One brain call on the zoomed crop around the subject; boxes come back in frame pixels."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = verify_region(subject, w, h)
        found = self.brain.ask(frame[y1:y2, x1:x2], phrases, mode=self.cfg.settings.brain_generation,
                               timeout=BRAIN_TIMEOUT_S, max_side=VERIFY_SIDE, upscale=True, seed=seed)
        raw = " ".join(str((getattr(self.brain, "last_answer", None) or {}).get("raw", "")).split())
        log.info("Brain %s (seed %d) asked %s -> %s", job.key, seed, phrases, raw[:600])
        return [{**b, "x1": b["x1"] + x1, "y1": b["y1"] + y1, "x2": b["x2"] + x1, "y2": b["y2"] + y1} for b in found]

    def _finish(self, job: _Job, verdict: str, findings: list[str] | None = None,
                brain_boxes: list[dict] | None = None, escalate: bool = False, keep_raw: bool = False) -> None:
        """Save the verdict on every alert of the job. Only behaviour seen in several pictures
        (escalate) raises the severity to critical; its boxes are drawn on the first alert's picture."""
        for i, (alert, annotated) in enumerate(job.items):
            alert.verdict = verdict
            alert.brain_findings = list(findings or [])
            if escalate:
                alert.severity = "critical"
            if i == 0 and brain_boxes and annotated is not None:
                alert.boxes = alert.boxes + brain_boxes
                try:
                    _write_jpg(Path(alert.snapshot_path), draw_boxes(annotated.copy(), brain_boxes))
                except (OSError, ValueError):
                    log.exception("Could not update the alert picture")
            self.store.update(alert)
            if not keep_raw:
                _unlink([raw_path(alert.snapshot_path)])
            self._callback(self.on_alert_update, self.store.get(alert.id) or alert)
            log.info("Alert %s verdict: %s %s", alert.id, verdict, alert.brain_findings)
