"""Routing policy: which event goes to which destination, now, later or never.

Pure functions (no Qt, no I/O). The hub keeps one DestState per destination and calls decide()
for every event; the reasons end up in the Activity log in plain words.
"""
from __future__ import annotations

import copy
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .model import INCIDENT_KINDS, SEVERITY_RANK, Decision, Event

# area kinds that are worth a message by default (same as areas.ALERT_KINDS; kept here so the
# policy stays importable without numpy / cv2)
try:
    from ..areas import ALERT_KINDS as AREA_ALERT_KINDS
except Exception:  # pragma: no cover - areas.py needs numpy/cv2, always present in the app
    AREA_ALERT_KINDS = {"enter", "cross_in", "cross_out", "occupied", "blocked", "loitering", "presence", "plate",
                        "appeared", "removed", "changed", "answer_yes"}

CAP_WINDOW_S = 600.0         # max_per_10min window
EDIT_THROTTLE_S = 5.0        # at most one edit of one message this often (the hub coalesces)
DIGEST_EVERY_S = 600.0       # at most one "more alerts" digest per destination this often
PRESETS = ("important", "everything", "custom")
LEVELS = ("info", "warning", "critical")
QUIET_MODES = ("silent", "hold", "drop")
UPDATE_MODES = ("edit", "each", "quiet")
PLATE_MODES = ("off", "unknown", "known", "all")
AREA_MODES = ("instant", "summary", "off")


def default_routes(kind: str = "") -> dict:
    """The "Only important" preset (email: both pictures and the daily summary)."""
    r = {
        "preset": "important",
        "incidents": {"enabled": True, "min_severity": "warning", "confirmed_only": True, "send_unchecked": True,
                      "updates": "edit", "cameras": []},
        "areas": {},
        "plates": "off",
        "offline": {"enabled": True, "minutes": 5, "back": True},
        "health": {"enabled": True},
        "summary": {"enabled": False, "time": "08:00"},
        "pictures": "crop",
        "clip": False,
        "quiet": {"enabled": False, "start": "23:00", "end": "07:00", "days": [0, 1, 2, 3, 4, 5, 6],
                  "mode": "silent", "critical_through": True},
        "rate": {"cooldown_s": 60, "max_per_10min": 10},
    }
    if kind == "email":
        r["pictures"] = "both"
        r["summary"]["enabled"] = True
    return r


def _merge(dst: dict, defaults: dict) -> dict:
    for k, v in defaults.items():
        if k not in dst or (isinstance(v, dict) and not isinstance(dst.get(k), dict)):
            dst[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and k != "areas":
            _merge(dst[k], v)
    return dst


def _hhmm(text, default: str) -> str:
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (ValueError, AttributeError):
        pass
    return default


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_routes(routes, kind: str = "") -> dict:
    """Fill missing keys and fix wrong values in place (hand-edited or older config); returns routes."""
    if not isinstance(routes, dict):
        routes = {}
    d = default_routes(kind)
    _merge(routes, d)
    if routes["preset"] not in PRESETS:
        routes["preset"] = "custom"
    inc = routes["incidents"]
    inc["enabled"] = bool(inc.get("enabled"))
    if inc.get("min_severity") not in LEVELS:
        inc["min_severity"] = "warning"
    inc["confirmed_only"] = bool(inc.get("confirmed_only"))
    inc["send_unchecked"] = bool(inc.get("send_unchecked"))
    if inc.get("updates") not in UPDATE_MODES:
        inc["updates"] = "edit"
    cams = inc.get("cameras")
    inc["cameras"] = [int(c) for c in cams if str(c).lstrip("-").isdigit()] if isinstance(cams, list) else []
    areas = routes.get("areas") if isinstance(routes.get("areas"), dict) else {}
    clean = {}
    for area_id, rule in areas.items():
        if isinstance(rule, dict):
            kinds = rule.get("kinds") if isinstance(rule.get("kinds"), list) else []
            clean[str(area_id)] = {"kinds": [str(k) for k in kinds],
                                   "mode": rule.get("mode") if rule.get("mode") in AREA_MODES else "off"}
    routes["areas"] = clean
    if routes.get("plates") not in PLATE_MODES:
        routes["plates"] = "off"
    off = routes["offline"]
    off["enabled"], off["back"] = bool(off.get("enabled")), bool(off.get("back"))
    off["minutes"] = _int(off.get("minutes"), 5, 1, 24 * 60)
    routes["health"]["enabled"] = bool(routes["health"].get("enabled"))
    routes["summary"]["enabled"] = bool(routes["summary"].get("enabled"))
    routes["summary"]["time"] = _hhmm(routes["summary"].get("time"), "08:00")
    if routes.get("pictures") not in ("crop", "snapshot", "both", "none"):
        routes["pictures"] = d["pictures"]
    routes["clip"] = bool(routes.get("clip"))
    q = routes["quiet"]
    q["enabled"], q["critical_through"] = bool(q.get("enabled")), bool(q.get("critical_through"))
    q["start"], q["end"] = _hhmm(q.get("start"), "23:00"), _hhmm(q.get("end"), "07:00")
    days = q.get("days") if isinstance(q.get("days"), list) else list(range(7))
    q["days"] = sorted({int(x) for x in days if str(x).isdigit() and 0 <= int(x) <= 6})
    if q.get("mode") not in QUIET_MODES:
        q["mode"] = "silent"
    rate = routes["rate"]
    rate["cooldown_s"] = _int(rate.get("cooldown_s"), 60, 0, 24 * 3600)
    rate["max_per_10min"] = _int(rate.get("max_per_10min"), 10, 1, 1000)
    return routes


def _active_areas(cfg) -> list[tuple[object, dict]]:
    site = cfg.active_site() if cfg is not None and hasattr(cfg, "active_site") else None
    if site is None:
        return []
    return [(cam, a) for cam in site.cameras for a in cam.areas]


def apply_preset(routes: dict, preset: str, cfg=None) -> dict:
    """Set the preset's values in place ("custom" only changes the name); returns routes."""
    if preset not in PRESETS:
        return routes
    routes["preset"] = preset
    if preset == "custom":
        return routes
    kind_defaults = default_routes()
    inc = routes.setdefault("incidents", {})
    if preset == "important":
        inc.update({k: copy.deepcopy(v) for k, v in kind_defaults["incidents"].items()})
        routes["plates"] = "off"
        routes["areas"] = {}
    else:   # everything
        inc.update({"enabled": True, "min_severity": "info", "confirmed_only": False, "send_unchecked": True,
                    "cameras": []})
        inc.setdefault("updates", "edit")
        routes["plates"] = "all"
        routes["areas"] = {a["id"]: {"kinds": [], "mode": "instant"} for _cam, a in _active_areas(cfg)
                           if not a.get("alert")}
        routes.setdefault("summary", {})
        routes["summary"].update({"enabled": True, "time": routes["summary"].get("time") or "08:00"})
    routes.setdefault("offline", {}).update({"enabled": True})
    routes.setdefault("health", {}).update({"enabled": True})
    return routes


def explain(routes: dict, cfg=None) -> list[str]:
    """A few plain lines saying what this destination gets, for the UI."""
    out = []
    inc = routes.get("incidents", {})
    if inc.get("enabled"):
        level = {"critical": "Critical incidents", "warning": "Critical and warning incidents",
                 "info": "Every incident"}.get(inc.get("min_severity"), "Incidents")
        cams = inc.get("cameras") or []
        line = level + (f" on {len(cams)} camera{'s' if len(cams) != 1 else ''}" if cams else "")
        brain_on = getattr(getattr(cfg, "settings", None), "brain_mode", "verify") == "verify"
        if inc.get("confirmed_only") and brain_on:
            line += ", after the AI confirms them"
        out.append(line)
    else:
        out.append("No incidents")
    n_areas = sum(1 for r in (routes.get("areas") or {}).values() if r.get("mode") == "instant")
    if n_areas:
        out.append(f"Events of {n_areas} area{'s' if n_areas != 1 else ''}")
    plates = routes.get("plates", "off")
    if plates != "off":
        out.append({"unknown": "Unknown number plates", "known": "Known number plates",
                    "all": "Every number plate"}[plates])
    if routes.get("offline", {}).get("enabled"):
        out.append(f"Cameras without video for {routes['offline'].get('minutes', 5)} min")
    if routes.get("summary", {}).get("enabled"):
        out.append(f"Daily summary at {routes['summary'].get('time', '08:00')}")
    q = routes.get("quiet", {})
    if q.get("enabled"):
        what = {"silent": "sent silently", "hold": f"held until {q.get('end')}",
                "drop": "nothing sent"}.get(q.get("mode"), "sent silently")
        out.append(f"Quiet hours {q.get('start')}-{q.get('end')}: {what}")
    return out


# --- time helpers ------------------------------------------------------------------

def _minutes(text: str) -> int:
    h, m = _hhmm(text, "00:00").split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(quiet: dict, now: float) -> bool:
    """Local time; a range may cross midnight (23:00-07:00); days = weekday (Mon=0) of the START
    of the range, so Friday 23:00 - Saturday 07:00 counts as Friday."""
    if not quiet or not quiet.get("enabled"):
        return False
    start, end = _minutes(quiet.get("start", "23:00")), _minutes(quiet.get("end", "07:00"))
    if start == end:
        return False
    days = quiet.get("days")
    days = set(range(7)) if not isinstance(days, list) else {int(d) for d in days}
    t = datetime.fromtimestamp(now)
    m = t.hour * 60 + t.minute
    if start < end:
        return start <= m < end and t.weekday() in days
    if m >= start:
        return t.weekday() in days
    if m < end:
        return (t - timedelta(days=1)).weekday() in days
    return False


def quiet_end(quiet: dict, now: float) -> float:
    """When the current quiet period ends (epoch), or now when not in quiet hours."""
    if not in_quiet_hours(quiet, now):
        return now
    end = _minutes(quiet.get("end", "07:00"))
    t = datetime.fromtimestamp(now)
    e = t.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if e <= t:
        e += timedelta(days=1)
    return e.timestamp()


def priority_for(event: Event, silent: bool) -> int:
    """ntfy-style 1..5: critical 5, warning 4, info 3; silent -> 2."""
    if silent:
        return 2
    return {"critical": 5, "warning": 4}.get(event.severity, 3)


def clock_text(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# --- per-destination state ---------------------------------------------------------------

@dataclass
class DestState:
    """Per-destination memory kept by the hub (in memory; threads persisted in DeliveryLog)."""
    sent_times: deque = field(default_factory=lambda: deque(maxlen=1000))   # for max_per_10min
    last_by_key: dict = field(default_factory=dict)    # "cam:<channel>:<kind>" -> last send ts (cooldown)
    incidents: dict = field(default_factory=dict)      # incident_id -> {"severity", "ref", "last_edit", "clip_sent"}
    held: list = field(default_factory=list)           # quiet-hours "hold": sent as one digest at the end
    digest: list = field(default_factory=list)         # cooldown / cap overflow: sent as one digest later
    digest_first: float = 0.0                          # when the first waiting digest item came in
    last_digest: float = 0.0
    area_summary: Counter = field(default_factory=Counter)   # area events kept for the daily summary
    problem_until: float = 0.0

    def recent_sends(self, now: float) -> int:
        while self.sent_times and now - self.sent_times[0] > CAP_WINDOW_S:
            self.sent_times.popleft()
        return len(self.sent_times)

    def note_send(self, event: Event, now: float, counted: bool = True) -> None:
        """Remember a message that is going out (cooldown key, cap, incident severity)."""
        if counted:
            self.sent_times.append(now)
            key = cooldown_key(event)
            if key:
                self.last_by_key[key] = now
        if event.incident_id and event.kind in INCIDENT_KINDS:
            info = self.incidents.setdefault(event.incident_id, {"ref": None, "last_edit": 0.0})
            if SEVERITY_RANK.get(event.severity, 0) >= SEVERITY_RANK.get(info.get("severity", "info"), 0):
                info["severity"] = event.severity
            info.setdefault("severity", event.severity)


def cooldown_key(event: Event) -> str:
    kind = {"incident_new": "incident", "incident_update": "incident", "area": "area", "plate": "plate"}.get(event.kind)
    if not kind:
        return ""
    chans = event.channels
    return f"cam:{chans[0] if chans else '-'}:{kind}"


def plate_is_known(plate: str, known) -> bool:
    """True when the plate is in the list allowing one misread character (engine.plate_distance)."""
    from ..engine import plate_distance
    for k in known or []:
        text = k.get("plate", "") if isinstance(k, dict) else str(k)
        if text and plate_distance(text, plate) <= 1:
            return True
    return False


# --- the decision ----------------------------------------------------------------------

def _settings_value(settings, name: str, default):
    return getattr(settings, name, default) if settings is not None else default


def decide(event: Event, routes: dict, state: DestState, now: float, settings=None, *,
           caps: dict | None = None, enabled: bool = True) -> Decision:
    """What to do with this event for one destination. First matching rule wins (SPEC 2.5).
    caps: the channel's CAPS (edit support); enabled: the destination's switch."""
    caps = caps or {}
    if event.kind == "test":
        return Decision("send", "Test message", priority=3)
    if not enabled:
        return Decision("skip", "This connection is turned off", log=False)
    notify = _settings_value(settings, "notify", {}) or {}
    try:
        snooze = float(notify.get("snooze_until") or 0)
    except (TypeError, ValueError):
        snooze = 0.0
    # status changes only update messages already sent (silently), so a pause does not hold them
    if snooze > now and event.severity != "critical" and event.kind != "incident_status":
        return Decision("skip", f"Messages paused until {clock_text(snooze)}")

    kind = event.kind
    inc_rules = routes.get("incidents", {})
    sent = state.incidents.get(event.incident_id) if event.incident_id else None
    can_edit = bool(caps.get("edit"))
    clip_ready = bool(routes.get("clip") and caps.get("clip") and event.clip_path
                      and not (sent or {}).get("clip_sent"))

    # 2. kind filters
    if kind in INCIDENT_KINDS:
        if not inc_rules.get("enabled", True):
            return Decision("skip", "Incidents are turned off here", log=kind == "incident_new")
        cams = inc_rules.get("cameras") or []
        if cams and not set(cams) & set(event.channels):
            return Decision("skip", "Not one of the cameras chosen here", log=kind == "incident_new")
        if kind == "incident_status":
            if not sent:
                return Decision("skip", "Nothing was sent here about this incident", log=False)
            if can_edit:
                return Decision("edit", "Status changed", silent=True, priority=2)
            if caps.get("updates") == "each":
                return Decision("send", "Status changed", silent=True, priority=2)
            return Decision("skip", "Status changes are only shown where the message can be updated", log=False)
        rank = SEVERITY_RANK.get(event.severity, 0)
        min_sev = inc_rules.get("min_severity", "warning")
        if rank < SEVERITY_RANK.get(min_sev, 1) and not sent:
            return Decision("skip", f"Below your minimum level ({min_sev})", log=kind == "incident_new")
    elif kind == "area":
        if event.extra.get("alerting"):
            return Decision("skip", "Already sent as an incident", log=False)
        rule = (routes.get("areas") or {}).get(event.area_id)
        if not rule or rule.get("mode") == "off":
            return Decision("skip", "Not chosen for this area", log=False)
        allowed = rule.get("kinds") or AREA_ALERT_KINDS
        if event.area_kind not in allowed:
            return Decision("skip", "This kind of area event is not chosen here", log=False)
        if rule.get("mode") == "summary":
            state.area_summary[event.area_id] += 1
            return Decision("skip", "Kept for the daily summary", log=False)
    elif kind == "plate":
        mode = routes.get("plates", "off")
        if mode == "off":
            return Decision("skip", "Number plates are not sent here", log=False)
        if mode == "unknown" and event.plate_known:
            return Decision("skip", "Known plate (only unknown plates are sent here)", log=False)
        if mode == "known" and not event.plate_known:
            return Decision("skip", "Unknown plate (only known plates are sent here)", log=False)
    elif kind in ("camera_offline", "camera_online"):
        off = routes.get("offline", {})
        if not off.get("enabled") or (kind == "camera_online" and not off.get("back")):
            return Decision("skip", "Camera problems are not sent here", log=False)
    elif kind in ("monitoring", "health"):
        if not routes.get("health", {}).get("enabled"):
            return Decision("skip", "Status messages are not sent here", log=False)
    elif kind == "summary":
        if not event.extra.get("digest") and not routes.get("summary", {}).get("enabled"):
            return Decision("skip", "The daily summary is off here", log=False)

    # 3. AI verdict
    if kind in ("incident_new", "incident_update"):
        if event.verdict == "rejected":
            if sent and can_edit:
                return Decision("edit", "The AI found nothing (dismissed)", silent=True, priority=2)
            return Decision("skip", "The AI found nothing (dismissed)", log=not sent)
        brain_on = _settings_value(settings, "brain_mode", "verify") == "verify"
        if (event.verdict == "pending" and inc_rules.get("confirmed_only") and brain_on and not sent):
            return Decision("wait_ai", "Waiting for the AI to check it")

    # 4. incident updates
    escalated = False
    if kind == "incident_update" and sent:
        sent_rank = SEVERITY_RANK.get(sent.get("severity", "info"), 0)
        escalated = SEVERITY_RANK.get(event.severity, 0) > sent_rank
        mode = inc_rules.get("updates", "edit")
        if not escalated:
            if mode == "edit" and caps.get("updates") == "each" and not can_edit:
                mode = "each"
            if mode == "quiet":
                if clip_ready:
                    return Decision("send", "The clip is ready", silent=True, priority=2, send_clip=True)
                return Decision("skip", "Already told you about this incident", log=False)
            if mode == "edit":
                if can_edit:
                    return Decision("edit", "Updated the message", silent=True, priority=2, send_clip=clip_ready)
                if clip_ready:
                    return Decision("send", "The clip is ready", silent=True, priority=2, send_clip=True)
                return Decision("skip", "Already told you about this incident", log=False)
        # each / escalation: a new message, subject to the cap and quiet hours below
    is_update_send = kind == "incident_update" and bool(sent)

    # 5. cooldown (not for follow-ups of an incident already sent, nor for critical incidents)
    rate = routes.get("rate", {})
    counted = kind in ("incident_new", "incident_update", "area", "plate")
    critical_incident = event.severity == "critical" and kind in INCIDENT_KINDS
    if counted and not is_update_send and not critical_incident:
        key = cooldown_key(event)
        cool = float(rate.get("cooldown_s", 60) or 0)
        last = state.last_by_key.get(key)
        if key and cool > 0 and last is not None and now - last < cool:
            return Decision("digest", "Too soon after the last message from this camera")
    # 6. cap; critical incidents get through (with their buttons) up to twice the cap, which only
    #    stops a runaway flood
    cap = int(rate.get("max_per_10min", 10) or 10)
    if counted and state.recent_sends(now) >= (cap * 2 if critical_incident else cap):
        return Decision("digest", "Too many messages in 10 minutes; combined into one later")
    # 7. quiet hours
    silent = False
    quiet = routes.get("quiet", {})
    if in_quiet_hours(quiet, now) and not (event.severity == "critical" and quiet.get("critical_through", True)):
        mode = quiet.get("mode", "silent")
        if mode == "hold":
            return Decision("hold", f"Quiet hours: held until {quiet.get('end', '07:00')}")
        if mode == "drop":
            return Decision("skip", "Quiet hours")
        silent = True
    # 8. send
    reason = "Severity went up" if escalated else ("Quiet hours: sent silently" if silent else "")
    return Decision("send", reason, silent=silent, priority=priority_for(event, silent),
                    send_clip=clip_ready or bool(kind == "incident_new" and routes.get("clip") and caps.get("clip")
                                                 and event.clip_path),
                    escalated=escalated)
