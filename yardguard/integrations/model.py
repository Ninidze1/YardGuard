"""Plain data passed between the hub, the routing policy and the channels (no Qt, no I/O)."""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import asdict, dataclass, field, fields

EVENT_KINDS = ("incident_new", "incident_update", "incident_status", "area", "plate",
               "camera_offline", "camera_online", "monitoring", "health", "summary", "test")
INCIDENT_KINDS = ("incident_new", "incident_update", "incident_status")
SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}   # same as engine.SEVERITY_RANK
SEVERITY_WORDS = {"info": "Info", "warning": "Warning", "critical": "Critical"}
PICTURES = ("crop", "snapshot", "both", "none")
DELIVERY_STATUSES = ("queued", "waiting_ai", "held", "sending", "sent", "edited", "retrying", "failed",
                     "skipped", "expired", "dropped")
ACTION_KINDS = ("mark_incident", "set_monitoring", "snapshot", "status", "list_incidents", "snooze",
                "update_chat")
_PATHS = ("snapshot_path", "crop_path", "clip_path")


@dataclass
class Event:
    """Something worth telling the user about, in words every channel can show."""
    kind: str                          # EVENT_KINDS
    ts: float                          # when it happened (epoch; local time is used for display)
    title: str                         # one line, plain words: "Person near the car at night"
    text: str = ""                     # 0-3 short lines; plain text (channels escape for HTML/Markdown)
    severity: str = "info"             # info | warning | critical
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    site_id: str = ""
    site_name: str = ""
    channel: int | None = None         # camera channel, when the event is about one camera
    camera: str = ""                   # camera label(s), "Front gate, Driveway"
    incident_id: str = ""
    alert_id: str = ""                 # cover / newest alert
    alert_count: int = 0
    verdict: str = ""                  # confirmed | pending | rejected | unverified ("" = not applicable)
    ai_note: str = ""                  # "hand on door handle" (first brain finding), "" if none
    status: str = ""                   # incident status: open | handled | false_alarm
    status_by: str = ""                # "Alex (Telegram)" for incident_status
    labels: list[str] = field(default_factory=list)     # "person", "car" ... (from alert boxes)
    area_id: str = ""
    area_name: str = ""
    area_task: str = ""
    area_kind: str = ""                # AreaEvent.kind
    plate: str = ""                    # plate text for kind == "plate"
    plate_known: bool | None = None
    snapshot_path: str = ""            # full frame (with boxes)
    crop_path: str = ""                # close-up
    clip_path: str = ""                # mp4, only when complete
    clip_pending: bool = False         # clip_offset_s >= 0 and no path yet
    web_path: str = ""                 # "#/incident/<id>" etc., joined with the phone-access URL when known
    extra: dict = field(default_factory=dict)            # summary stats, offline duration, channels, ...
    escalated: bool = False            # incident_update that raised the severity (send as new message)

    def image_path(self, pictures: str) -> str:
        """The single picture for this event under a destination's pictures setting
        ("crop" | "snapshot" | "both" | "none"): crop falls back to snapshot and vice versa."""
        if pictures == "none":
            return ""
        if pictures == "snapshot":
            return self.snapshot_path or self.crop_path
        return self.crop_path or self.snapshot_path

    @property
    def channels(self) -> list[int]:
        """Every camera channel the event is about (incidents may span several)."""
        chans = self.extra.get("channels") if isinstance(self.extra, dict) else None
        if isinstance(chans, list) and chans:
            return [int(c) for c in chans if isinstance(c, (int, float)) or str(c).isdigit()]
        return [] if self.channel is None else [int(self.channel)]

    @property
    def has_clip(self) -> bool:
        return bool(self.clip_path)

    def to_json(self, include_paths: bool = False) -> dict:
        """Stable JSON form (webhook / MQTT / delivery log). Local file paths only with
        include_paths (the delivery log); otherwise just has_snapshot / has_crop / has_clip."""
        d = asdict(self)
        for name in _PATHS:
            d["has_" + name[:-5]] = bool(d[name])
            if not include_paths:
                d.pop(name)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Event":
        """Back from to_json (unknown keys ignored), e.g. to re-queue a row of the delivery log."""
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in names}
        data.setdefault("kind", "test")
        data.setdefault("ts", 0.0)
        data.setdefault("title", "")
        if not isinstance(data.get("labels"), list):
            data["labels"] = []
        if not isinstance(data.get("extra"), dict):
            data["extra"] = {}
        return cls(**data)

    def copy(self, **changes) -> "Event":
        """A copy with some fields changed (lists and dicts are copied too)."""
        d = asdict(self)
        d.update(changes)
        return Event(**d)


@dataclass
class SendContext:
    """What a channel needs besides the event; built per delivery by the hub."""
    delivery_id: str = ""
    pictures: str = "crop"           # crop | snapshot | both | none
    silent: bool = False             # quiet hours "silent" mode or low priority
    priority: int = 3                # 1..5 (ntfy scale), from severity + quiet hours
    thread_ref: dict | None = None   # what a previous send of THIS incident to THIS destination returned
    edit: bool = False               # True: update the message in thread_ref instead of sending a new one
    send_clip: bool = False          # attach/follow with the clip if the channel can
    web_base: str = ""               # "https://pc.tail1234.ts.net" or "" (no phone access URL)
    attempt: int = 1                 # 1-based


@dataclass
class SendResult:
    ok: bool
    ref: dict | None = None          # stored as thread_ref for later edits, e.g. {"messages": [{"chat": -100.., "id": 42}]}
    error: str = ""                  # plain words, already scrubbed, shown in the UI
    retry: bool = False              # temporary problem: retry with backoff
    retry_after: float | None = None  # server asked for this delay (429)
    permanent: bool = False          # config problem (401, 403, chat not found): mark destination "Problem"
    ms: int = 0
    note: str = ""                   # "picture too large, sent text only"


@dataclass
class CheckResult:
    ok: bool
    message: str                     # "Found @MyYardBot" / "Password not accepted..."
    field: str = ""                  # which setup field is wrong, for the UI to point at
    data: dict = dataclasses.field(default_factory=dict)   # e.g. {"bot_username": ..., "bot_name": ...}


@dataclass
class Action:
    """Something a remote user asked YardGuard to do (Telegram button, MQTT command, web app)."""
    kind: str                        # ACTION_KINDS
    args: dict = field(default_factory=dict)
    source: str = ""                 # "Telegram", "Home Assistant", "Phone"
    user: str = ""                   # "Alex" (display only, never used for auth)


@dataclass
class Decision:
    """What the routing policy decided for one event and one destination."""
    action: str                      # "send" | "edit" | "skip" | "wait_ai" | "hold" | "digest"
    reason: str = ""                 # plain words for the Activity log: "Below your minimum level (warning)"
    silent: bool = False
    priority: int = 3
    send_clip: bool = False          # also send the (newly completed) clip
    escalated: bool = False          # an update sent as a new message because the severity rose
    log: bool = True                 # False: routine skip, not worth a row in the Activity log


@dataclass
class Delivery:
    """One event on its way to one destination."""
    id: str
    dest_id: str
    event: Event
    decision: Decision
    created: float
    attempts: int = 0
    edit: bool = False
    send_clip: bool = False
    pictures: str = "crop"
    web_base: str = ""
    thread_ref: dict | None = None   # known message of this incident at queue time (the worker may know newer)
    due: float = 0.0                 # not before this time (retries, edit throttling)
    manual: bool = False             # retried by the user: never expires


def new_delivery_id() -> str:
    return "dl-" + uuid.uuid4().hex[:12]
