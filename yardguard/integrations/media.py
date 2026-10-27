"""Pictures and words for messages, shared by every channel so they all say the same thing.

Wording rules: short, readable on a lock screen, no technical terms ("AI" is fine), local time,
camera names as the user typed them. Severity words: Critical / Warning / Info.
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from .. import config
from .model import INCIDENT_KINDS, SEVERITY_RANK, SEVERITY_WORDS, Event

log = logging.getLogger("yardguard")
NOT_CHECKED = "Not checked by AI"


# --- pictures -----------------------------------------------------------------------------

def read_image(path: str):
    """BGR image from a file (non-ASCII paths work); None when missing or unreadable."""
    if not path:
        return None
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except (OSError, ValueError, cv2.error):
        return None


def encode_jpeg(img, max_side: int = 1280, quality: int = 80, max_bytes: int = 5_000_000) -> bytes | None:
    """JPEG bytes of a BGR image: shrunk (never enlarged) to max_side, quality lowered (and then the
    size) until it fits max_bytes."""
    if img is None or getattr(img, "size", 0) == 0:
        return None
    h, w = img.shape[:2]
    if max_side and max(h, w) > max_side:
        f = max_side / max(h, w)
        img = cv2.resize(img, (max(1, round(w * f)), max(1, round(h * f))), interpolation=cv2.INTER_AREA)
    q = int(max(10, min(100, quality)))
    for _ in range(20):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if not ok:
            return None
        if not max_bytes or buf.size <= max_bytes:
            return buf.tobytes()
        if q > 40:
            q -= 10
        else:
            h, w = img.shape[:2]
            if max(h, w) < 64:
                return buf.tobytes() if buf.size <= max_bytes else None
            img = cv2.resize(img, (max(1, int(w * 0.75)), max(1, int(h * 0.75))), interpolation=cv2.INTER_AREA)
    return None


def prepare_jpeg(path: str, max_side: int = 1280, quality: int = 80, max_bytes: int = 5_000_000) -> bytes | None:
    """A picture file ready to upload: resized down only, re-encoded lowering the quality until it
    fits max_bytes; None when missing / unreadable."""
    return encode_jpeg(read_image(path), max_side, quality, max_bytes)


def demo_picture(folder: str | None = None) -> str:
    """yardguard/web/sample.jpg (shipped with phone access); if it is missing, a generated grey
    640x360 picture in `folder` (default: the app's .cache folder)."""
    shipped = config.ROOT / "yardguard" / "web" / "sample.jpg"
    if shipped.exists():
        return str(shipped)
    out = Path(folder) if folder else config.ROOT / ".cache"
    path = out / "integrations_sample.jpg"
    if path.exists():
        return str(path)
    try:
        out.mkdir(parents=True, exist_ok=True)
        img = np.zeros((360, 640, 3), np.uint8)
        grad = np.linspace(40, 90, 360, dtype=np.uint8)[:, None]
        img[:] = np.dstack([grad, grad + 10, grad])
        cv2.putText(img, "YardGuard test", (150, 195), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (235, 235, 235), 2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(buf.tobytes())
            tmp.replace(path)
    except OSError as exc:
        log.warning("Could not write the sample picture: %s", exc)
        return ""
    return str(path)


# --- words -------------------------------------------------------------------------------------

def clock(ts: float, now: float | None = None) -> str:
    """ "21:04" today, "Yesterday 21:04", "Mon 21:04" within a week, else "23 Oct 21:04" (local)."""
    t = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(time.time() if now is None else now).date()
    hm = t.strftime("%H:%M")
    if t.date() == today:
        return hm
    if t.date() == today - timedelta(days=1):
        return f"Yesterday {hm}"
    if today - timedelta(days=6) <= t.date() < today:
        return f"{t.strftime('%a')} {hm}"
    return f"{t.day} {t.strftime('%b')} {hm}"


def duration_text(seconds: float) -> str:
    """ "45 s", "12 min", "2 h 5 min", "3 days"."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400 * 2:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{s // 86400} days"


def severity_word(severity: str) -> str:
    return SEVERITY_WORDS.get(severity, "Info")


def headline(e: Event) -> str:
    """The first line: "Person near the car at night"."""
    return (e.title or "YardGuard").strip()


def subline(e: Event, now: float | None = None) -> str:
    """ "Front gate · 21:04 · Critical" (the level only where it matters)."""
    parts = []
    if e.camera and e.camera not in headline(e):
        parts.append(e.camera)
    parts.append(clock(e.ts, now))
    if e.kind in INCIDENT_KINDS or SEVERITY_RANK.get(e.severity, 0) > 0:
        parts.append(severity_word(e.severity))
    return " · ".join(parts)


def ai_line(e: Event) -> str:
    """ "AI: confirmed (hand on door handle)" / "Not checked by AI" / ""."""
    if e.ai_note == NOT_CHECKED:
        return NOT_CHECKED
    if e.verdict == "confirmed":
        return "AI: confirmed" + (f" ({e.ai_note})" if e.ai_note else "")
    if e.verdict == "rejected":
        return "AI: found nothing (dismissed)"
    if e.verdict == "pending" and e.kind in INCIDENT_KINDS:
        return "AI: checking..."
    return e.ai_note


def status_line(e: Event) -> str:
    """ "Handled by Alex (Telegram) at 21:10" / "Marked as a false alarm by ..." / "Reopened"."""
    if e.kind not in INCIDENT_KINDS or not (e.status_by or e.kind == "incident_status"):
        return ""
    ts = e.extra.get("status_ts") or e.ts
    by = f" by {e.status_by}" if e.status_by else ""
    at = f" at {datetime.fromtimestamp(ts).strftime('%H:%M')}"
    if e.status == "handled":
        return f"Handled{by}{at}"
    if e.status == "false_alarm":
        return f"Marked as a false alarm{by}{at}"
    if e.status == "open" and e.kind == "incident_status":
        return f"Reopened{by}{at}"
    return ""


def lines(e: Event, now: float | None = None) -> list[str]:
    """[headline, subline, text lines, ai line, "3 alerts", status, site]."""
    out = [headline(e), subline(e, now)]
    out += [ln.strip() for ln in (e.text or "").splitlines() if ln.strip()]
    ai = ai_line(e)
    if ai:
        out.append(ai)
    if e.kind in INCIDENT_KINDS and e.alert_count > 1:
        out.append(f"{e.alert_count} alerts")   # the camera is already in the headline or the subline
    st = status_line(e)
    if st:
        out.append(st)
    if e.site_name and e.extra.get("multi_site"):
        out.append(e.site_name)
    return out


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:max(0, n - 1)].rstrip() + "…"


def html_caption(e: Event, limit: int = 1024, now: float | None = None) -> str:
    """Telegram HTML caption: bold headline, every line escaped; never longer than limit (the
    text is shortened before escaping, so no entity or tag is ever cut)."""
    ls = lines(e, now)
    body = ls[1:]

    def build(head: str, rest: list[str]) -> str:
        return "\n".join([f"<b>{html.escape(head)}</b>"] + [html.escape(x) for x in rest])

    out = build(ls[0], body)
    while len(out) > limit and body:
        last = body[-1]
        over = len(out) - limit
        if len(last) > over + 8:
            body[-1] = _cut(last, len(last) - over - 8)   # escaping may grow it: leave some slack
        else:
            body.pop()
        out = build(ls[0], body)
    head = ls[0]
    while len(out) > limit and head:
        head = _cut(head, max(1, len(head) - (len(out) - limit) - 8))
        out = build(head, [])
        if len(head) <= 1:
            break
    return out[:limit] if len(out) > limit else out


def plain(e: Event, now: float | None = None) -> str:
    """Newline-joined lines (email text part, ntfy body, webhook "text")."""
    return "\n".join(lines(e, now))


def subject(e: Event, prefix: str = "YardGuard") -> str:
    """Email subject: "[YardGuard] Critical: Person near the car at night - Front gate 02:14"."""
    sev = f"{severity_word(e.severity)}: " if e.kind in INCIDENT_KINDS or e.severity != "info" else ""
    where = f" - {e.camera}" if e.camera and e.camera not in headline(e) else " -"
    return f"[{prefix}] {sev}{headline(e)}{where} {datetime.fromtimestamp(e.ts).strftime('%H:%M')}"


# --- summary -----------------------------------------------------------------------------------

def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def build_summary(store, cfg, since: float, now: float | None = None,
                  offline_cameras: list[str] | None = None) -> Event:
    """The daily summary as an Event (kind "summary"): text lines, extra stats and the picture of
    the most severe incident. offline_cameras: names of cameras without video right now
    (None = unknown, not mentioned)."""
    now = time.time() if now is None else now
    incidents = store.list_incidents(limit=1000, since=since) if store is not None else []
    real = [i for i in incidents if i.status != "false_alarm" and i.verdict != "rejected"]
    false_alarms = len(incidents) - len(real)
    crit = sum(1 for i in real if i.severity == "critical")
    handled = sum(1 for i in real if i.status == "handled")
    open_n = sum(1 for i in real if i.status == "open")
    parts = []
    if real:
        parts.append(_plural(len(real), "incident") + (f" ({crit} critical)" if crit else ""))
    else:
        parts.append("no incidents")
    if false_alarms:
        parts.append(_plural(false_alarms, "false alarm"))
    stats = {"incidents": len(real), "critical": crit, "false_alarms": false_alarms, "handled": handled,
             "open": open_n, "areas": {}, "unknown_plates": 0, "plates": 0}
    site = cfg.active_site() if cfg is not None else None
    area_lines = []
    if site is not None and store is not None:
        for cam in site.cameras:
            for a in cam.areas:
                if not a.get("enabled", True):
                    continue
                try:
                    st = store.area_stats(a["id"], since=since)
                except Exception:
                    continue
                if not st.get("total"):
                    continue
                stats["areas"][a["id"]] = {"name": a["name"], "total": st["total"], "counts": st.get("counts", {})}
                if a["task"] == "count":
                    objs = a.get("objects") or ["car"]
                    noun = objs[0] if len(objs) == 1 else "object"
                    n = st.get("in", 0) + st.get("out", 0) or st["total"]
                    area_lines.append(f"{_plural(n, noun)} counted at {a['name']}")
                elif a["task"] == "log_plates":
                    n = st["counts"].get("plate", 0)
                    stats["plates"] += n
                    try:
                        evs = store.list_area_events(area_id=a["id"], since=since, kind="plate", limit=5000)
                        stats["unknown_plates"] += sum(1 for ev in evs if not (ev.extra or {}).get("known"))
                    except Exception:
                        pass
    parts += area_lines
    if stats["unknown_plates"]:
        parts.append(_plural(stats["unknown_plates"], "unknown plate"))
    if offline_cameras is not None:
        parts.append("all cameras online" if not offline_cameras else
                     ("no video from " + ", ".join(offline_cameras)))
    span = "Last 24 hours" if now - since >= 23 * 3600 else f"Since {clock(since, now)}"
    text = f"{span}: " + ", ".join(parts) + "."
    detail = []
    if open_n:
        detail.append(f"{_plural(open_n, 'incident')} still open.")
    worst = max(real, key=lambda i: (SEVERITY_RANK.get(i.severity, 0), i.start_ts), default=None)
    snapshot = crop = ""
    if worst is not None:
        detail.append(f"Most serious: {worst.title.split(' · ')[0]} at {clock(worst.start_ts, now)}.")
        try:
            cover = store.get(worst.cover_alert_id) if worst.cover_alert_id else None
            if cover is not None:
                snapshot, crop = cover.snapshot_path or "", cover.crop_path or ""
        except Exception:
            pass
    stats["since"] = since
    stats["worst_incident_id"] = worst.id if worst is not None else ""
    return Event("summary", now, "Daily summary", text="\n".join([text] + detail),
                 severity="info", snapshot_path=snapshot, crop_path=crop, extra=stats,
                 site_id=getattr(site, "id", ""), site_name=getattr(site, "name", ""),
                 web_path="#/incidents")
