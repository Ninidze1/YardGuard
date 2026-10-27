"""JSON for the phone web app: status, incidents, areas and their logs.

Plain functions over the AlertStore and the config (read-only), safe to call from the web server's
threads. File paths never leave this module: the phone only learns whether a picture or clip exists
and fetches it through /media/... by id.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

from ..config import APP_VERSION, TEMPLATE_INFO

STATE_TEXT = {"watching": "Watching", "idle": "Watching", "starting": "Starting", "offline": "No video",
              "off": "Not monitored"}
VEHICLE_LABELS = ("car", "truck", "bus", "motorcycle")


def _exists(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path)
    except (OSError, ValueError):
        return False


def midnight(now: float | None = None) -> float:
    d = datetime.fromtimestamp(time.time() if now is None else now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _site(cfg):
    try:
        return cfg.active_site()
    except Exception:
        return None


def camera_list(cfg, monitor) -> list[dict]:
    """Enabled cameras of the active site with their monitoring state and what they see."""
    site = _site(cfg)
    running = bool(getattr(monitor, "running", False))
    try:
        states = monitor.camera_status() if running else {}
    except Exception:
        states = {}
    out = []
    for cam in (site.enabled_cameras() if site else []):
        st = states.get(cam.channel) or {}
        state = str(st.get("state") or ("starting" if running and cam.monitor else "off"))
        objects = {str(k): int(v) for k, v in (st.get("objects") or {}).items() if int(v or 0) > 0}
        out.append({"channel": cam.channel, "name": cam.label, "state": state,
                    "state_text": STATE_TEXT.get(state, state.capitalize()), "monitored": bool(cam.monitor),
                    "objects": objects, "people": objects.get("person", 0),
                    "vehicles": sum(objects.get(k, 0) for k in VEHICLE_LABELS)})
    return out


def camera_name(cfg, channel) -> str:
    site = _site(cfg)
    cam = site.camera(int(channel)) if site and channel is not None else None
    return cam.label if cam else (f"Camera {channel}" if channel is not None else "")


def status(cfg, store, monitor, *, allow_actions: bool, csrf: str, device: dict | None = None,
           public_url: str = "", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    site = _site(cfg)
    running = bool(getattr(monitor, "running", False))
    message = str(getattr(monitor, "message", "") or "") if running else "Monitoring is off"
    counts, unread, newest = {}, 0, 0.0
    if store is not None:
        try:
            counts = store.incident_counts(since=now - 86400)
            unread = store.unread_incident_count()
            latest = store.list_incidents(limit=1)
            newest = latest[0].start_ts if latest else 0.0
        except Exception:
            pass
    return {"site": site.name if site else "", "sites": len(getattr(cfg, "sites", []) or []),
            "monitoring": {"running": running, "message": message},
            "cameras": camera_list(cfg, monitor), "counts": counts, "unread": unread, "newest_ts": newest,
            "allow_actions": bool(allow_actions), "csrf": csrf if allow_actions else "",
            "device": {"id": device.get("id", ""), "name": device.get("name", "")} if device else None,
            "public_url": public_url, "server_time": now, "version": APP_VERSION}


# -- incidents ------------------------------------------------------------------------

def _media_flags(obj) -> dict:
    clip = str(getattr(obj, "clip_path", "") or "")
    offset = getattr(obj, "clip_offset_s", None)
    offset = -1.0 if offset is None else float(offset)
    return {"has_snapshot": _exists(getattr(obj, "snapshot_path", "")), "has_crop": _exists(getattr(obj, "crop_path", "")),
            "has_clip": _exists(clip), "clip_pending": offset >= 0 and not clip, "clip_offset_s": offset}


def incident_json(inc, store) -> dict:
    cover = store.get(inc.cover_alert_id) if inc.cover_alert_id else None
    if cover is None:
        alerts = store.incident_alerts(inc.id)
        cover = alerts[-1] if alerts else None
    d = {"id": inc.id, "title": inc.title or "Incident", "severity": inc.severity, "verdict": inc.verdict,
         "status": inc.status, "start_ts": inc.start_ts, "end_ts": inc.end_ts, "cameras": list(inc.cameras),
         "channels": list(inc.channels), "alert_count": inc.alert_count, "unread": int(inc.unread or 0),
         "cover": cover.id if cover else ""}
    flags = _media_flags(cover) if cover else {"has_snapshot": False, "has_crop": False, "has_clip": False,
                                               "clip_pending": False, "clip_offset_s": -1.0}
    d.update(flags)
    return d


def incidents(store, *, before: float | None = None, limit: int = 30, status: str | None = None) -> dict:
    """Newest first; `before` pages backwards by start time. status None = all; "open" leaves out
    the ones the AI dismissed (like the desktop's open count)."""
    limit = max(1, min(100, int(limit)))
    n = limit + 1
    while True:
        rows = store.list_incidents(limit=n, status=status)
        picked = [i for i in rows if (before is None or i.start_ts < before)
                  and not (status == "open" and i.verdict == "rejected")]
        if len(picked) > limit or len(rows) < n or n >= 20000:
            break
        n = min(20000, n * 4)
    more = len(picked) > limit
    return {"items": [incident_json(i, store) for i in picked[:limit]], "more": more}


def alert_json(a) -> dict:
    d = {"id": a.id, "ts": a.ts, "title": a.title, "detail": a.detail, "severity": a.severity,
         "verdict": a.verdict, "camera": a.camera, "channel": a.channel, "rule": a.rule, "status": a.status,
         "brain_findings": [str(f) for f in (a.brain_findings or [])][:6]}
    d.update(_media_flags(a))
    return d


def incident_detail(store, incident_id: str) -> dict | None:
    inc = store.get_incident(incident_id)
    if inc is None:
        return None
    d = incident_json(inc, store)
    d["alerts"] = [alert_json(a) for a in store.incident_alerts(inc.id)]
    return d


# -- areas ----------------------------------------------------------------------------

def _headline(task: str, st: dict) -> str:
    counts, total = st.get("counts") or {}, int(st.get("total") or 0)
    state = st.get("state")
    if task == "count":
        if st.get("in") or st.get("out"):
            return f"{st.get('in', 0)} in · {st.get('out', 0)} out today"
        return f"{total} counted today" if total else "Nothing counted today"
    if task == "log_plates":
        n = counts.get("plate", 0)
        return f"{n} plate{'s' if n != 1 else ''} today" if n else "No plates today"
    if task == "parking_spot" and state in ("occupied", "free"):
        return "Occupied now" if state == "occupied" else "Free now"
    if task == "ask_ai" and state in ("yes", "no"):
        return f"Answer now: {state}"
    if task == "state_watch" and state in ("changed", "normal"):
        return "Changed now" if state == "changed" else "As usual"
    return f"{total} event{'s' if total != 1 else ''} today" if total else "Nothing today"


def areas(cfg, store, now: float | None = None) -> dict:
    site = _site(cfg)
    since = midnight(now)
    items = []
    for cam in sorted(site.cameras if site else [], key=lambda c: c.channel):
        for a in cam.areas:
            if not a.get("enabled", True):
                continue
            try:
                st = store.area_stats(a["id"], since=since) if store is not None else {}
            except Exception:
                st = {}
            task = a.get("task", "")
            items.append({"id": a["id"], "name": a.get("name", ""), "task": task,
                          "task_title": TEMPLATE_INFO.get(task, {}).get("title", task),
                          "channel": cam.channel, "camera": cam.label, "alert": bool(a.get("alert")),
                          "total": int(st.get("total") or 0), "counts": st.get("counts") or {},
                          "in": int(st.get("in") or 0), "out": int(st.get("out") or 0), "state": st.get("state"),
                          "last_ts": st.get("last_ts"), "last_value": st.get("last_value") or "",
                          "headline": _headline(task, st)})
    return {"items": items, "site": site.name if site else ""}


def find_area(cfg, area_id: str) -> tuple[object, dict] | None:
    site = _site(cfg)
    for cam in (site.cameras if site else []):
        for a in cam.areas:
            if a.get("id") == area_id:
                return cam, a
    return None


def area_event_json(ev, cfg) -> dict:
    from ..areas import KIND_TEXT, describe
    try:
        text = describe(ev)
    except Exception:
        text = KIND_TEXT.get(ev.kind, ev.kind)
    d = {"id": ev.id, "ts": ev.ts, "kind": ev.kind, "kind_text": KIND_TEXT.get(ev.kind, ev.kind), "text": text,
         "label": ev.label, "value": ev.value, "area_id": ev.area_id, "area_name": ev.area_name,
         "task": ev.task, "channel": ev.channel, "camera": camera_name(cfg, ev.channel),
         "direction": str((ev.extra or {}).get("direction", "") or "")}
    d.update(_media_flags(ev))
    return d


def area_events(cfg, store, *, area_id: str | None, before: float | None = None, limit: int = 50) -> dict:
    limit = max(1, min(200, int(limit)))
    rows = store.list_area_events(area_id=area_id, until=before, limit=limit + 1)
    return {"items": [area_event_json(e, cfg) for e in rows[:limit]], "more": len(rows) > limit}
