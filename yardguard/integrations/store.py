"""Delivery log: what was sent where, why something was not, and message threads per incident.

SQLite at <events folder>/integrations.db (never alerts.db); safe to use from any thread. After
close() every call is a quiet no-op, so a worker finishing late never crashes.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .model import Decision, Delivery, Event

log = logging.getLogger("yardguard")

_COLS = ("id", "ts", "updated", "dest_id", "dest_kind", "dest_name", "event_id", "event_kind", "incident_id",
         "title", "status", "reason", "attempts", "next_try", "ms", "event_json")
FINAL = ("sent", "edited", "failed", "skipped", "expired", "dropped")
PENDING = ("queued", "retrying", "sending")
PROBLEMS = ("failed", "expired", "dropped")


class DeliveryLog:
    def __init__(self, folder: str) -> None:
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "integrations.db"
        self._lock = threading.Lock()
        self._closed = False
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, ts REAL, updated REAL, dest_id TEXT, "
                "dest_kind TEXT, dest_name TEXT, event_id TEXT, event_kind TEXT, incident_id TEXT, title TEXT, "
                "status TEXT, reason TEXT, attempts INTEGER, next_try REAL, ms INTEGER, event_json TEXT)")
            self._db.execute("CREATE TABLE IF NOT EXISTS threads (dest_id TEXT, incident_id TEXT, ref TEXT, "
                             "severity TEXT, updated REAL, PRIMARY KEY (dest_id, incident_id))")
            self._db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
            self._db.execute("CREATE INDEX IF NOT EXISTS deliveries_ts ON deliveries(ts)")
            self._db.execute("CREATE INDEX IF NOT EXISTS deliveries_status ON deliveries(status)")
            self._db.execute("CREATE INDEX IF NOT EXISTS deliveries_dest ON deliveries(dest_id, ts)")

    def _exec(self, sql: str, args=(), *, fetch: str = "", commit: bool = False):
        with self._lock:
            if self._closed:
                return None if fetch == "one" else []
            try:
                if commit:
                    with self._db:
                        cur = self._db.execute(sql, args)
                else:
                    cur = self._db.execute(sql, args)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
            except sqlite3.Error as exc:
                log.warning("Delivery log: %s", exc)
                return None if fetch == "one" else []

    # -- deliveries ------------------------------------------------------------------------------
    def add(self, d: Delivery, *, status: str = "queued", reason: str | None = None, dest_kind: str = "",
            dest_name: str = "") -> None:
        ev = d.event
        payload = ev.to_json(include_paths=True)
        payload["_delivery"] = {"edit": d.edit, "send_clip": d.send_clip, "pictures": d.pictures,
                                "silent": d.decision.silent, "priority": d.decision.priority}
        now = time.time()
        self._exec(f"INSERT OR REPLACE INTO deliveries ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
                   (d.id, d.created or now, now, d.dest_id, dest_kind, dest_name, ev.id, ev.kind, ev.incident_id,
                    ev.title, status, d.decision.reason if reason is None else reason, d.attempts, d.due or None, 0,
                    json.dumps(payload, ensure_ascii=False, default=str)), commit=True)

    def update(self, delivery_id: str, **fields) -> None:
        names = [k for k in fields if k in _COLS and k != "id"]
        if not names:
            return
        fields.setdefault("updated", time.time())
        if "updated" not in names:
            names.append("updated")
        self._exec(f"UPDATE deliveries SET {', '.join(n + '=?' for n in names)} WHERE id=?",
                   [fields[n] for n in names] + [delivery_id], commit=True)

    def list(self, limit: int = 300, problems_only: bool = False, dest_id: str | None = None) -> list[dict]:
        """Newest first; event_json left out (use get())."""
        where, args = [], []
        if problems_only:
            where.append(f"status IN ({', '.join('?' * len(PROBLEMS))})")
            args += list(PROBLEMS)
        if dest_id:
            where.append("dest_id=?")
            args.append(dest_id)
        cols = ", ".join(c for c in _COLS if c != "event_json")
        sql = f"SELECT {cols} FROM deliveries" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._exec(sql + " ORDER BY ts DESC, updated DESC LIMIT ?", args + [int(limit)], fetch="all")
        return [dict(r) for r in rows or []]

    def get(self, delivery_id: str) -> dict | None:
        row = self._exec("SELECT * FROM deliveries WHERE id=?", (delivery_id,), fetch="one")
        if row is None:
            return None
        d = dict(row)
        try:
            d["event"] = json.loads(d.get("event_json") or "{}")
        except ValueError:
            d["event"] = {}
        return d

    def pending(self, max_age_s: float) -> list[dict]:
        """queued / retrying / sending rows young enough to re-queue, oldest first, with "event"."""
        cutoff = time.time() - max_age_s
        rows = self._exec(f"SELECT id FROM deliveries WHERE status IN ({', '.join('?' * len(PENDING))}) AND ts>=? "
                          "ORDER BY ts", list(PENDING) + [cutoff], fetch="all")
        return [r for r in (self.get(row["id"]) for row in rows or []) if r is not None]

    def expire_stale(self, max_age_s: float) -> int:
        """Rows left queued / retrying / sending / waiting by an earlier run that are too old to send."""
        cutoff = time.time() - max_age_s
        statuses = list(PENDING) + ["waiting_ai", "held"]
        rows = self._exec(f"SELECT COUNT(*) FROM deliveries WHERE status IN ({', '.join('?' * len(statuses))}) "
                          "AND ts<?", statuses + [cutoff], fetch="one")
        n = int(rows[0]) if rows else 0
        if n:
            self._exec(f"UPDATE deliveries SET status='expired', reason=?, updated=? WHERE status IN "
                       f"({', '.join('?' * len(statuses))}) AND ts<?",
                       ["Not sent: YardGuard was closed", time.time()] + statuses + [cutoff], commit=True)
        return n

    def delivery_from_row(self, row: dict) -> Delivery | None:
        """Rebuild a Delivery from a get() / pending() row (for retry and re-queue)."""
        data = row.get("event") or {}
        if not data:
            return None
        meta = data.pop("_delivery", {}) if isinstance(data.get("_delivery"), dict) else {}
        ev = Event.from_json(data)
        dec = Decision("send", row.get("reason") or "", silent=bool(meta.get("silent")),
                       priority=int(meta.get("priority") or 3))
        return Delivery(id=row["id"], dest_id=row["dest_id"], event=ev, decision=dec, created=float(row["ts"] or 0),
                        attempts=0, edit=bool(meta.get("edit")), send_clip=bool(meta.get("send_clip")),
                        pictures=str(meta.get("pictures") or "crop"))

    # -- threads -------------------------------------------------------------------------------------
    def thread(self, dest_id: str, incident_id: str) -> dict | None:
        row = self._exec("SELECT * FROM threads WHERE dest_id=? AND incident_id=?", (dest_id, incident_id),
                         fetch="one")
        if row is None:
            return None
        d = dict(row)
        try:
            d["ref"] = json.loads(d["ref"]) if d["ref"] else None
        except ValueError:
            d["ref"] = None
        return d

    def set_thread(self, dest_id: str, incident_id: str, ref: dict | None, severity: str) -> None:
        self._exec("INSERT OR REPLACE INTO threads (dest_id, incident_id, ref, severity, updated) VALUES (?,?,?,?,?)",
                   (dest_id, incident_id, json.dumps(ref) if ref is not None else None, severity, time.time()),
                   commit=True)

    def threads_for(self, incident_id: str) -> list[dict]:
        rows = self._exec("SELECT dest_id FROM threads WHERE incident_id=?", (incident_id,), fetch="all")
        return [t for t in (self.thread(r["dest_id"], incident_id) for r in rows or []) if t]

    def recent_threads(self, since: float) -> list[dict]:
        """(dest_id, incident_id, severity, updated) of threads touched since `since`."""
        rows = self._exec("SELECT dest_id, incident_id, severity, updated FROM threads WHERE updated>=?",
                          (since,), fetch="all")
        return [dict(r) for r in rows or []]

    # -- key/value -------------------------------------------------------------------------------------
    def kv_get(self, key: str, default: str = "") -> str:
        row = self._exec("SELECT value FROM kv WHERE key=?", (key,), fetch="one")
        return row["value"] if row is not None and row["value"] is not None else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, str(value)), commit=True)

    # -- upkeep ------------------------------------------------------------------------------------------
    def stats(self, dest_id: str) -> dict:
        """{"last_ok": ts|None, "last_error": str, "queued": n} for a card's detail line."""
        ok = self._exec("SELECT MAX(updated) FROM deliveries WHERE dest_id=? AND status IN ('sent','edited')",
                        (dest_id,), fetch="one")
        err = self._exec("SELECT reason, updated FROM deliveries WHERE dest_id=? AND status IN ('failed','expired',"
                         "'dropped') ORDER BY updated DESC LIMIT 1", (dest_id,), fetch="one")
        q = self._exec(f"SELECT COUNT(*) FROM deliveries WHERE dest_id=? AND status IN "
                       f"('queued','retrying','sending','waiting_ai','held')", (dest_id,), fetch="one")
        last_ok = ok[0] if ok is not None else None
        last_error = ""
        if err is not None and (last_ok is None or (err["updated"] or 0) > last_ok):
            last_error = err["reason"] or ""
        return {"last_ok": last_ok, "last_error": last_error, "queued": int(q[0]) if q else 0}

    def cleanup(self, keep_days: int = 30, keep_rows: int = 5000) -> None:
        now = time.time()
        self._exec("DELETE FROM deliveries WHERE ts<?", (now - keep_days * 86400,), commit=True)
        self._exec("DELETE FROM deliveries WHERE status='skipped' AND ts<?", (now - 2 * 86400,), commit=True)
        self._exec("DELETE FROM deliveries WHERE id IN (SELECT id FROM deliveries ORDER BY ts DESC LIMIT -1 "
                   "OFFSET ?)", (int(keep_rows),), commit=True)
        self._exec("DELETE FROM threads WHERE updated<?", (now - keep_days * 86400,), commit=True)

    def clear(self) -> None:
        """Empty the Activity log (threads and kv stay, so messages can still be updated)."""
        self._exec("DELETE FROM deliveries", commit=True)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass

    @property
    def closed(self) -> bool:
        return self._closed
