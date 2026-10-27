"""IntegrationHub: turns YardGuard events into messages for the user's destinations, and remote
commands (Telegram buttons, Home Assistant, the phone web app) into actions.

Threads:
- GUI thread: signal intake, Event building (AlertStore reads), policy decisions, timers, actions.
- One worker thread per enabled destination ("yg-notify-<id>") with a bounded queue: it calls
  channel.send() one delivery at a time, retries with backoff (5 s, 30 s, 2 min, 5 min, 10 min),
  expires messages older than 30 min and writes the outcome to the delivery log. Results come
  back to the GUI thread through a queued signal.
- Channels may run threads of their own (Telegram poller, MQTT loop); they reach the app only
  through request(), whose actions run on the GUI thread.
"""
from __future__ import annotations

import concurrent.futures
import heapq
import itertools
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from .. import config
from . import media, policy
from .base import Channel, IntegrationConf, load_channel_class, normalize_integration, scrub
from .frames import FrameProvider
from .model import (INCIDENT_KINDS, SEVERITY_RANK, Action, Decision, Delivery, Event, SendContext, SendResult,
                    new_delivery_id)
from .policy import DestState
from .store import DeliveryLog

log = logging.getLogger("yardguard")

BACKOFF = [5, 30, 120, 300, 600]   # seconds before attempt 2, 3, ...
MAX_ATTEMPTS = 6
EXPIRE_S = 1800.0                  # messages older than this are not sent any more
QUEUE_MAX = 200
AI_WAIT_S = 60.0                   # "AI-confirmed only": wait this long for the verdict
REQUEUE_MAX_AGE_S = 600.0          # unfinished deliveries of the last run younger than this are sent at start
PERMANENT_GAP_S = 300.0            # after a config problem: at most one try per 5 min
PROBLEM_AFTER = 3                  # failed attempts in a row before problem() is emitted
HEALTH_GAP_S = 600.0               # not running longer than this -> "YardGuard is back"
HEALTH_DELAY_MS = 20_000           # ...said 20 s after start, so the network is up
STARTUP_QUIET_S = 30.0             # monitoring switched on right after start is not announced
SUMMARY_LATE_S = 3600.0            # a daily summary missed by more than this waits for the next day
MONITORING_SOURCE_S = 120.0        # a remote on/off request explains a change within this long
VEHICLES = ("car", "truck", "bus", "motorcycle")


class _Worker:
    """Delivery thread of one destination."""

    def __init__(self, hub: "IntegrationHub", dest_id: str, channel: Channel) -> None:
        self.hub, self.dest_id, self.channel = hub, dest_id, channel
        self._cond = threading.Condition()
        self._items: deque = deque()          # Delivery, or ("edit", incident_id) markers
        self._heap: list = []                 # (due monotonic, seq, item)
        self._edits: dict[str, Delivery] = {}  # incident -> newest edit waiting (coalesced)
        self._seq = itertools.count()
        self._stop = False
        self.refs: dict[str, dict] = {}       # incident -> message ref, known on this thread at once
        self.pending_new: dict[str, Delivery] = {}  # incident -> first message waiting to be retried
        self.last_edit: dict[str, float] = {}
        self.blocked_until = 0.0
        self.thread = threading.Thread(target=self._run, name=f"yg-notify-{dest_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def join(self, timeout: float) -> None:
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=max(0.0, timeout))

    def size(self) -> int:
        with self._cond:
            return len(self._items) + len(self._heap)

    def put(self, d: Delivery) -> None:
        replaced = dropped = None
        with self._cond:
            inc = d.event.incident_id
            if d.edit and inc:
                old = self._edits.get(inc)
                self._edits[inc] = d
                if old is not None:
                    replaced = old         # its queue slot now carries the newer state
                else:
                    self._items.append(("edit", inc))
            else:
                if len(self._items) + len(self._heap) >= QUEUE_MAX:
                    dropped = self._drop_oldest_locked()
                self._items.append(d)
            self._cond.notify()
        if replaced is not None:
            self.hub.log.update(replaced.id, status="skipped", reason="Replaced by a newer update")
        if dropped is not None:
            self.hub.log.update(dropped.id, status="dropped", reason="Dropped (too many messages waiting)")
            self.hub._result.emit({"dest_id": self.dest_id, "delivery": dropped, "status": "dropped",
                                   "result": SendResult(False, error="Dropped (too many messages waiting)")})

    def _resolve(self, item) -> Delivery | None:
        return self._edits.get(item[1]) if isinstance(item, tuple) else item

    def _drop_oldest_locked(self) -> Delivery | None:
        """Remove the oldest waiting non-critical delivery (or the oldest of all)."""
        pick = None
        for item in self._items:
            d = self._resolve(item)
            if d is not None and d.event.severity != "critical":
                pick = item
                break
        if pick is None and self._items:
            pick = self._items[0]
        if pick is None:
            return None
        self._items.remove(pick)
        d = self._resolve(pick)
        if isinstance(pick, tuple):
            self._edits.pop(pick[1], None)
        return d

    def _next(self) -> Delivery | None:
        """The next delivery to process, waiting as needed; None when stopping."""
        with self._cond:
            while True:
                if self._stop:
                    return None
                now = time.monotonic()
                item = None
                if self._heap and self._heap[0][0] <= now:
                    item = heapq.heappop(self._heap)[2]
                elif self._items:
                    item = self._items.popleft()
                if item is None:
                    timeout = min(1.0, self._heap[0][0] - now) if self._heap else 1.0
                    self._cond.wait(max(0.01, timeout))
                    continue
                if isinstance(item, tuple):
                    inc = item[1]
                    d = self._edits.get(inc)
                    if d is None:
                        continue
                    last = self.last_edit.get(inc, -1e18)
                    if now - last < policy.EDIT_THROTTLE_S:
                        heapq.heappush(self._heap, (last + policy.EDIT_THROTTLE_S, next(self._seq), item))
                        continue
                    self._edits.pop(inc, None)
                    return d
                return item

    def _run(self) -> None:
        while True:
            d = self._next()
            if d is None:
                return
            try:
                self._process(d)
            except Exception:  # a bug here must not kill the destination
                log.exception("Delivery to %s failed unexpectedly", self.dest_id)

    def _requeue(self, d: Delivery, due: float) -> None:
        with self._cond:
            heapq.heappush(self._heap, (due, next(self._seq), d))
            self._cond.notify()

    def _forget_pending(self, inc: str, d: Delivery) -> None:
        if inc and self.pending_new.get(inc) is d:
            del self.pending_new[inc]

    def _process(self, d: Delivery) -> None:
        hub, ch = self.hub, self.channel
        logdb = hub.log
        ev = d.event
        inc = ev.incident_id if ev.kind in INCIDENT_KINDS else ""
        if not d.manual and ev.kind != "test" and hub.clock() - ev.ts > EXPIRE_S:
            self._forget_pending(inc, d)
            logdb.update(d.id, status="expired", reason="Expired (older than 30 min)", next_try=None)
            hub._result.emit({"dest_id": self.dest_id, "delivery": d, "status": "expired",
                              "result": SendResult(False, error="Expired (older than 30 min)")})
            return
        now_m = time.monotonic()
        if self.blocked_until > now_m:
            if inc and not d.edit:
                self.pending_new[inc] = d
            self._requeue(d, self.blocked_until)
            logdb.update(d.id, status="retrying", next_try=time.time() + (self.blocked_until - now_m),
                         reason=hub.problem_text(self.dest_id) or "Waiting: the last try had a problem")
            return
        ref = (self.refs.get(inc) if inc else None) or d.thread_ref
        first = self.pending_new.get(inc) if inc else None
        if d.edit and not ref and first is not None and first is not d:
            # The first message of this incident is still waiting to be retried: it goes out with
            # this newer state instead of this update becoming a second message.
            first.event = ev.copy(kind=first.event.kind, id=first.event.id)
            logdb.update(d.id, status="skipped", reason="Merged into the first message (still waiting to send)",
                         next_try=None)
            return
        edit = bool(d.edit and ref)
        ctx = SendContext(delivery_id=d.id, pictures=d.pictures, silent=d.decision.silent,
                          priority=d.decision.priority, thread_ref=ref, edit=edit, send_clip=d.send_clip,
                          web_base=d.web_base, attempt=d.attempts + 1)
        d.attempts += 1
        logdb.update(d.id, status="sending", attempts=d.attempts)
        t0 = time.perf_counter()
        try:
            res = ch.send(ev, ctx)
        except Exception as exc:
            res = SendResult(False, error=f"Sending failed: {type(exc).__name__}", retry=True)
            log.warning("Channel %s raised %s while sending", self.dest_id, ch.scrub(str(exc)))
        if not isinstance(res, SendResult):
            res = SendResult(False, error="The connection gave no answer", retry=True)
        res.ms = res.ms or int((time.perf_counter() - t0) * 1000)
        res.error = scrub(res.error, *ch.secret_values()) if res.error else ""
        res.note = scrub(res.note, *ch.secret_values()) if res.note else ""
        if res.ok:
            self.blocked_until = 0.0
            self._forget_pending(inc, d)
            if inc:
                if res.ref is not None and (edit or inc not in self.refs):
                    self.refs[inc] = res.ref
                elif inc not in self.refs and ref is not None:
                    self.refs[inc] = ref
                if edit:
                    self.last_edit[inc] = time.monotonic()
                old = logdb.thread(self.dest_id, inc)
                sev = ev.severity
                if old and SEVERITY_RANK.get(old.get("severity") or "info", 0) > SEVERITY_RANK.get(sev, 0):
                    sev = old["severity"]
                logdb.set_thread(self.dest_id, inc, self.refs.get(inc), sev)
            status = "edited" if edit else "sent"
            reason = "; ".join(x for x in (d.decision.reason, res.note) if x)
            logdb.update(d.id, status=status, reason=reason, ms=res.ms, next_try=None)
        else:
            if res.permanent:
                self.blocked_until = time.monotonic() + PERMANENT_GAP_S
            if (res.retry or res.permanent) and d.attempts < MAX_ATTEMPTS:
                backoff = hub_backoff()
                delay = float(backoff[min(d.attempts - 1, len(backoff) - 1)])
                if res.retry_after:
                    delay = max(delay, float(res.retry_after))
                due = time.monotonic() + delay
                if res.permanent:
                    due = max(due, self.blocked_until)
                if inc and not edit and not d.edit:
                    self.pending_new[inc] = d
                self._requeue(d, due)
                status = "retrying"
                logdb.update(d.id, status=status, reason=res.error or "Will try again",
                             next_try=time.time() + (due - time.monotonic()))
            else:
                status = "failed"
                self._forget_pending(inc, d)   # later updates may then start a message of their own
                logdb.update(d.id, status=status, reason=res.error or "Could not send", next_try=None)
        hub._result.emit({"dest_id": self.dest_id, "delivery": d, "status": status, "result": res, "edit": edit,
                          "ref": self.refs.get(inc) if inc else None})


def hub_backoff() -> list:
    """The retry delays (read at use time so tests can shorten them)."""
    return BACKOFF


class IntegrationHub(QObject):
    """Turns YardGuard events into messages for the user's destinations, and remote commands
    into actions. Lives on the GUI thread; delivery runs on one worker thread per destination."""

    deliveries_changed = Signal()                 # Activity tab / card detail lines refresh (throttled 500 ms)
    destination_status = Signal(str)              # destination id whose status/status_text changed
    incidents_changed = Signal()                  # an incident was marked from outside the desktop UI
    action_requested = Signal(object, object)     # internal: (Action, Future) queued to the GUI thread
    problem = Signal(str, str)                    # (destination id, plain text) after 3 failures in a row
    snooze_changed = Signal(float)                # new snooze_until (0 = not snoozed)
    _result = Signal(object)                      # internal: worker results, queued to the GUI thread
    _status_sig = Signal(str)                     # internal: channel status changes from any thread

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.clock: Callable[[], float] = time.time
        self.health_delay_ms = HEALTH_DELAY_MS
        s = ctx.cfg.settings
        if not isinstance(getattr(s, "integrations", None), list):
            s.integrations = []
        for d in s.integrations:
            normalize_integration(d)
        if not isinstance(getattr(s, "notify", None), dict):
            s.notify = {}
        self.frames = FrameProvider(ctx)
        self.log = self._open_log()
        self._channels: dict[str, Channel] = {}
        self._workers: dict[str, _Worker] = {}
        self._states: dict[str, DestState] = {}
        self._waiting: dict[tuple[str, str], dict] = {}      # (dest, incident) -> {"deadline", "event", "row"}
        self._fail_count: dict[str, int] = {}
        self._problem_sent: set[str] = set()
        self._problem_text: dict[str, str] = {}
        self._listeners: list[Callable[[str, object], None]] = []
        self._handlers: dict[str, Callable | None] = {"set_monitoring": None, "open_incident": None}
        self._cams: dict[int, dict] = {}       # offline tracker: channel -> {"since", "seen", "sent", "name"}
        self._told_status: dict[str, str] = {}  # incident -> status the destinations were last told
        self._monitoring_request: tuple | None = None   # (on, source, user, ts)
        self._last_running: bool | None = None
        self._started = False
        self._shut = False
        self._started_at = 0.0
        self._last_cleanup = 0.0

        self.action_requested.connect(self._run_action, Qt.QueuedConnection)
        self._result.connect(self._on_result, Qt.QueuedConnection)
        self._status_sig.connect(self.destination_status.emit, Qt.QueuedConnection)
        b = ctx.alert_bridge
        b.incident_added.connect(self._on_incident_added)
        b.incident_updated.connect(self._on_incident_updated)
        b.area_event_added.connect(self._on_area_event)
        b.status_changed.connect(self._on_status)
        cfg_changed = getattr(ctx, "config_changed", None)
        if cfg_changed is not None:
            cfg_changed.connect(self._on_config_changed)

        self._changed_timer = QTimer(self)
        self._changed_timer.setSingleShot(True)
        self._changed_timer.setInterval(500)
        self._changed_timer.timeout.connect(self.deliveries_changed.emit)
        self._t_fast = QTimer(self)
        self._t_fast.setInterval(1000)
        self._t_fast.timeout.connect(self._tick_fast)
        self._t_slow = QTimer(self)
        self._t_slow.setInterval(60_000)
        self._t_slow.timeout.connect(self._tick_slow)
        self._t_mon = QTimer(self)
        self._t_mon.setInterval(2000)
        self._t_mon.timeout.connect(self._poll_monitoring)

    # -- lifecycle ---------------------------------------------------------------------------------
    def _events_dir(self) -> str:
        store = getattr(self.ctx, "alert_store", None)
        if store is not None and getattr(store, "dir", None):
            return str(store.dir)
        return str(self.ctx.cfg.settings.events_dir)

    def _open_log(self) -> DeliveryLog:
        try:
            return DeliveryLog(self._events_dir())
        except Exception as exc:  # missing drive, broken file
            log.error("Delivery log at %s failed (%s): using the app folder", self._events_dir(), exc)
            return DeliveryLog(str(config.ROOT / "events"))

    def start(self) -> None:
        """Build channels for enabled destinations, start workers and timers, re-queue young
        unfinished deliveries of the last run and check for downtime."""
        if self._started or self._shut:
            return
        self._started = True
        now = self.clock()
        self._started_at = now
        for conf in self.destinations():
            if conf.enabled:
                self._start_dest(conf)
        self.log.expire_stale(REQUEUE_MAX_AGE_S)
        for row in self.log.pending(REQUEUE_MAX_AGE_S):
            d = self.log.delivery_from_row(row)
            worker = self._workers.get(row.get("dest_id") or "")
            if d is None or worker is None:
                self.log.update(row["id"], status="expired", reason="The connection was removed or turned off")
                continue
            self._fill_delivery(d)
            self.log.update(d.id, status="queued", reason="Sent after a restart")
            worker.put(d)
        try:
            hb = float(self.log.kv_get("heartbeat") or 0)
        except ValueError:
            hb = 0.0
        if hb and now - hb > HEALTH_GAP_S:
            ev = Event("health", now, "YardGuard is back",
                       text=f"It was not running from {media.clock(hb, now)} to {media.clock(now, now)}.",
                       extra={"down_from": hb, "down_to": now})
            QTimer.singleShot(max(0, int(self.health_delay_ms)), lambda ev=ev: self._shut or self._route(ev))
        self.log.kv_set("heartbeat", repr(now))
        mon = getattr(self.ctx, "monitor", None)
        self._last_running = bool(mon is not None and mon.running)
        self._t_fast.start()
        self._t_slow.start()
        self._t_mon.start()
        self._cleanup()
        self._changed()

    def shutdown(self, timeout: float = 4.0) -> None:
        """Stop timers, workers and channels; queued deliveries stay in the log ("queued") and are
        sent at the next start if still young. Idempotent."""
        if self._shut:
            return
        self._shut = True
        end = time.monotonic() + max(0.5, timeout)
        for t in (self._t_fast, self._t_slow, self._t_mon, self._changed_timer):
            t.stop()
        if self._started:
            self.log.kv_set("heartbeat", repr(self.clock()))
        b = self.ctx.alert_bridge
        for sig, slot in ((b.incident_added, self._on_incident_added), (b.incident_updated, self._on_incident_updated),
                          (b.area_event_added, self._on_area_event), (b.status_changed, self._on_status)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        workers = list(self._workers.values())
        for w in workers:
            w.stop()
        self._stop_channels(list(self._channels.values()), end)
        for w in workers:
            w.join(end - time.monotonic())
            if w.thread.is_alive():   # stuck in a slow send: it is a daemon thread and ends with the app
                log.warning("Integration %s did not stop in time", w.dest_id)
        self._workers.clear()
        self._channels.clear()
        self.frames.close()
        self.log.close()

    def _stop_channels(self, channels: list[Channel], end: float) -> None:
        threads = []
        for ch in channels:
            t = threading.Thread(target=self._safe_stop, args=(ch,), name=f"yg-stop-{ch.conf.id}", daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=max(0.0, end - time.monotonic()))

    @staticmethod
    def _safe_stop(ch: Channel) -> None:
        try:
            ch.stop()
        except Exception as exc:
            log.warning("Stopping %s failed: %s", ch.conf.id, ch.scrub(str(exc)))

    def _start_dest(self, conf: IntegrationConf) -> None:
        if conf.id in self._channels or self._shut:
            return
        cls = load_channel_class(conf.kind)
        if cls is None:
            self._problem_text[conf.id] = "Needs an add-on that is not installed"
            self.notify_status(conf.id)
            return
        try:
            ch = cls(conf, self)
        except Exception as exc:
            log.warning("Could not create %s: %s", conf.id, scrub(str(exc), *conf.secret_values()))
            return
        self._channels[conf.id] = ch
        self._states.setdefault(conf.id, DestState())
        worker = _Worker(self, conf.id, ch)
        self._workers[conf.id] = worker
        worker.start()
        try:
            ch.start()
        except Exception as exc:
            ch.set_status("problem", f"Could not start: {ch.scrub(str(exc))}")
        self.notify_status(conf.id)

    def _stop_dest(self, dest_id: str, wait: float = 3.0) -> None:
        ch = self._channels.pop(dest_id, None)
        w = self._workers.pop(dest_id, None)
        if w is not None:
            w.stop()
        if ch is not None:
            self._stop_channels([ch], time.monotonic() + wait)
        if w is not None:
            w.join(1.0)
        for key in [k for k in self._waiting if k[0] == dest_id]:
            entry = self._waiting.pop(key)
            self.log.update(entry["row"], status="skipped", reason="The connection was turned off")

    def set_handlers(self, *, set_monitoring: Callable[[bool], None] | None = None,
                     open_incident: Callable[[str], None] | None = None) -> None:
        if set_monitoring is not None:
            self._handlers["set_monitoring"] = set_monitoring
        if open_incident is not None:
            self._handlers["open_incident"] = open_incident

    def open_incident(self, incident_id: str) -> bool:
        """Show an incident in the desktop app (Activity tab "Open incident")."""
        fn = self._handlers.get("open_incident")
        if fn is None:
            return False
        fn(incident_id)
        return True

    # -- config API (GUI thread) -------------------------------------------------------------------
    def destinations(self) -> list[IntegrationConf]:
        return [IntegrationConf(d) for d in self.ctx.cfg.settings.integrations]

    def _conf(self, dest_id: str) -> IntegrationConf | None:
        return next((c for c in self.destinations() if c.id == dest_id), None)

    def channel(self, dest_id: str) -> Channel | None:
        return self._channels.get(dest_id)

    def problem_text(self, dest_id: str) -> str:
        return self._problem_text.get(dest_id, "")

    def _save(self) -> None:
        try:
            self.ctx.cfg.save()
        except OSError as exc:
            log.error("Could not save the settings: %s", exc)

    def add_destination(self, d: dict) -> IntegrationConf:
        """Append a destination (from base.new_integration + setup), save it, start it if enabled."""
        normalize_integration(d)
        items = self.ctx.cfg.settings.integrations
        if any(x.get("id") == d["id"] for x in items):
            raise ValueError("A destination with this id exists")
        items.append(d)
        self._save()
        conf = IntegrationConf(d)
        if conf.enabled and self._started:
            self._start_dest(conf)
        self._emit_listeners("config", None)
        self.notify_status(conf.id)
        self._changed()
        return conf

    def update_destination(self, dest_id: str) -> None:
        """After the UI changed conf.params / routes / secrets / name: save and reconfigure."""
        conf = self._conf(dest_id)
        if conf is None:
            return
        normalize_integration(conf.d)
        self._save()
        ch = self._channels.get(dest_id)
        if ch is not None and conf.enabled:
            try:
                ch.reconfigure(conf)
            except Exception as exc:
                ch.set_status("problem", f"Could not apply the change: {ch.scrub(str(exc))}")
            self._problem_text.pop(dest_id, None)
        elif conf.enabled and self._started:
            self._start_dest(conf)
        elif not conf.enabled:
            self._stop_dest(dest_id)
        self._emit_listeners("config", None)
        self.notify_status(dest_id)

    def set_enabled(self, dest_id: str, on: bool) -> None:
        conf = self._conf(dest_id)
        if conf is None:
            return
        conf.enabled = on
        self._save()
        if on and self._started:
            self._start_dest(conf)
        elif not on:
            self._stop_dest(dest_id)
        self._emit_listeners("config", None)
        self.notify_status(dest_id)

    def remove_destination(self, dest_id: str) -> None:
        """Stop it and delete its config entry (secrets with it); its log rows stay."""
        self._stop_dest(dest_id)
        items = self.ctx.cfg.settings.integrations
        items[:] = [d for d in items if d.get("id") != dest_id]
        self._states.pop(dest_id, None)
        self._problem_text.pop(dest_id, None)
        self._problem_sent.discard(dest_id)
        self._fail_count.pop(dest_id, None)
        self._save()
        self._emit_listeners("config", None)
        self.notify_status(dest_id)
        self._changed()

    def notify_status(self, dest_id: str) -> None:
        """A destination's status changed (any thread): destination_status on the GUI thread."""
        if not self._shut:
            self._status_sig.emit(dest_id)

    def send_test(self, dest_id: str, on_done: Callable[[SendResult], None]) -> None:
        """Send a sample message now (off the GUI thread); on_done(SendResult) on the GUI thread."""
        from ..qt_util import run_async
        conf = self._conf(dest_id)
        if conf is None:
            on_done(SendResult(False, error="This connection was removed."))
            return
        ch = self._channels.get(dest_id)
        if ch is None:
            cls = load_channel_class(conf.kind)
            if cls is None:
                on_done(SendResult(False, error="This connection needs an add-on that is not installed."))
                return
            ch = cls(conf, self)
        row = Delivery(new_delivery_id(), dest_id, Event("test", self.clock(), "Test message"),
                       Decision("send", "Test message"), self.clock())

        def done(res) -> None:
            if not isinstance(res, SendResult):
                res = SendResult(False, error=ch.scrub(str(res)) or "Could not send")
            res.error = ch.scrub(res.error) if res.error else ""
            self.log.add(row, status="sent" if res.ok else "failed", reason=res.note if res.ok else res.error,
                         dest_kind=conf.kind, dest_name=conf.name)
            self.log.update(row.id, ms=res.ms)
            if res.ok and ch.status == "problem" and ch.status_text == self._problem_text.get(dest_id):
                ch.set_status("ok")
            self._changed()
            on_done(res)

        run_async(ch.test, on_done=done)

    def snooze(self, until: float) -> None:
        """Pause messages (critical ones still go out) until `until`; 0 = resume. Saved."""
        until = max(0.0, float(until or 0))
        self.ctx.cfg.settings.notify["snooze_until"] = until if until > self.clock() else 0.0
        self._save()
        self.snooze_changed.emit(self.snoozed_until())

    def snoozed_until(self) -> float:
        try:
            until = float(self.ctx.cfg.settings.notify.get("snooze_until") or 0)
        except (TypeError, ValueError):
            return 0.0
        return until if until > self.clock() else 0.0

    def web_base(self) -> str:
        """The phone-access https address when phone access is on, else ""."""
        remote = getattr(self.ctx.cfg.settings, "remote", None) or {}
        url = str(remote.get("public_url") or "").strip()
        return url.rstrip("/") if remote.get("enabled") and url.startswith(("https://", "http://")) else ""

    def retry(self, delivery_id: str) -> bool:
        """Queue a failed / expired / dropped row again (it does not expire this time)."""
        row = self.log.get(delivery_id)
        if row is None or row.get("status") not in ("failed", "expired", "dropped"):
            return False
        worker = self._workers.get(row.get("dest_id") or "")
        d = self.log.delivery_from_row(row)
        if worker is None or d is None:
            return False
        d.manual = True
        self._fill_delivery(d)
        self.log.update(d.id, status="queued", reason="Sending again", attempts=0, next_try=None)
        worker.put(d)
        self._changed()
        return True

    def _fill_delivery(self, d: Delivery) -> None:
        conf = self._conf(d.dest_id)
        if conf is not None:
            d.pictures = conf.routes.get("pictures", d.pictures)
        d.web_base = self.web_base()
        inc = d.event.incident_id
        if inc and d.event.kind in INCIDENT_KINDS:
            t = self.log.thread(d.dest_id, inc)
            d.thread_ref = t["ref"] if t else None

    def add_listener(self, fn: Callable[[str, object], None]) -> None:
        """fn(kind, payload) on the GUI thread: ("status", camera_status dict), ("area_event",
        AreaEvent), ("monitoring", bool), ("config", None). Must be quick."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit_listeners(self, kind: str, payload) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, payload)
            except Exception:
                log.exception("Integration listener failed")

    def events_dir_changed(self) -> None:
        """Reopen the delivery log in the (new) events folder."""
        new = Path(self._events_dir())
        if self.log.closed is False and Path(self.log.dir) == new:
            return
        old, self.log = self.log, self._open_log()
        old.close()
        self._changed()

    # -- sample ------------------------------------------------------------------------------------
    def sample_event(self) -> Event:
        """A "YardGuard test" event with the newest incident picture, else the demo picture."""
        now = self.clock()
        snapshot = crop = ""
        camera = ""
        store = getattr(self.ctx, "alert_store", None)
        try:
            for inc in (store.list_incidents(limit=20) if store is not None else []):
                cover = store.get(inc.cover_alert_id) if inc.cover_alert_id else None
                if cover is None:
                    continue
                s = cover.snapshot_path if cover.snapshot_path and Path(cover.snapshot_path).exists() else ""
                c = cover.crop_path if cover.crop_path and Path(cover.crop_path).exists() else ""
                if s or c:
                    snapshot, crop, camera = s, c, cover.camera
                    break
        except Exception:
            pass
        if not (snapshot or crop):
            snapshot = media.demo_picture(str(Path(self._events_dir())))
        if not camera:
            cams = self.frames.cameras()
            camera = cams[0][1] if cams else ""
        site = self.ctx.cfg.active_site()
        return Event("test", now, "YardGuard test", text="If you can read this, alerts will arrive here.",
                     severity="info", camera=camera, snapshot_path=snapshot, crop_path=crop,
                     site_id=getattr(site, "id", ""), site_name=getattr(site, "name", ""), web_path="#/incidents")

    # -- event building ------------------------------------------------------------------------------
    def _site_extra(self, site_id: str) -> tuple[str, bool]:
        cfg = self.ctx.cfg
        site = cfg.site(site_id) if site_id else None
        return (site.name if site is not None else ""), len(cfg.sites) > 1

    def _incident_event(self, inc, kind: str) -> Event:
        from ..engine import short_title
        store = self.ctx.alert_store
        cover = None
        try:
            cover = store.get(inc.cover_alert_id) if inc.cover_alert_id else None
            if cover is None:
                alerts = store.incident_alerts(inc.id)
                cover = alerts[-1] if alerts else None
        except Exception:
            cover = None
        labels: list[str] = []
        if cover is not None:
            for b in sorted(cover.boxes or [], key=lambda b: b.get("role") != "subject"):
                lab = str(b.get("label") or "")
                if lab and lab not in labels:
                    labels.append(lab)
        ai_note = ""
        if inc.verdict == "confirmed" and cover is not None and cover.brain_findings:
            ai_note = str(cover.brain_findings[0])
        site_name, multi = self._site_extra(inc.site_id)
        title = short_title(cover) if cover is not None else (inc.title or "Incident").split(" · ")[0]
        return Event(
            kind, self.clock() if kind != "incident_new" else (cover.ts if cover is not None else inc.start_ts),
            title, severity=inc.severity, site_id=inc.site_id, site_name=site_name,
            channel=inc.channels[0] if inc.channels else None, camera=", ".join(inc.cameras),
            incident_id=inc.id, alert_id=cover.id if cover is not None else "", alert_count=inc.alert_count,
            verdict=inc.verdict, ai_note=ai_note, status=inc.status, labels=labels,
            snapshot_path=cover.snapshot_path if cover is not None else "",
            crop_path=cover.crop_path if cover is not None else "",
            clip_path=cover.clip_path if cover is not None else "",
            clip_pending=bool(cover is not None and cover.clip_offset_s >= 0 and not cover.clip_path),
            web_path=f"#/incident/{inc.id}",
            extra={"channels": list(inc.channels), "start_ts": inc.start_ts, "end_ts": inc.end_ts,
                   "multi_site": multi, "clip_offset_s": cover.clip_offset_s if cover is not None else -1.0})

    def _area_event(self, ev) -> Event | None:
        from ..areas import ALERT_KINDS, KIND_TEXT
        cfg = self.ctx.cfg
        site = cfg.site(ev.site_id) or cfg.active_site()
        cam = site.camera(ev.channel) if site is not None else None
        area = cam.area(ev.area_id) if cam is not None else None
        site_name, multi = self._site_extra(getattr(site, "id", ""))
        camera = cam.label if cam is not None else f"Camera {ev.channel}"
        common = dict(site_id=ev.site_id, site_name=site_name, channel=ev.channel, camera=camera,
                      area_id=ev.area_id, area_name=ev.area_name, area_task=ev.task, area_kind=ev.kind,
                      snapshot_path=ev.snapshot_path or "", crop_path=ev.crop_path or "",
                      clip_path=ev.clip_path or "", labels=[ev.label] if ev.label else [],
                      web_path=f"#/area/{ev.area_id}")
        extra = {"area_event_id": ev.id, "value": ev.value, "multi_site": multi,
                 "alerting": bool(area and area.get("alert")), "channels": [ev.channel]}
        if ev.kind == "plate":
            if isinstance(ev.extra, dict) and "known" in ev.extra:
                known = bool(ev.extra.get("known"))
            else:
                known = policy.plate_is_known(ev.value, ((area or {}).get("params") or {}).get("known"))
            title = f"{'Known' if known else 'Unknown'} plate {ev.value} at {ev.area_name}"
            return Event("plate", ev.ts, title, severity="info" if known else "warning", plate=ev.value,
                         plate_known=known, extra=extra, **common)
        title = ""
        try:
            from ..areas import describe
            title = describe(ev)    # "Car blocking Gate for 6 min"
        except Exception:
            title = ""
        if not title:
            title = f"{KIND_TEXT.get(ev.kind, ev.kind.replace('_', ' ').capitalize())} at {ev.area_name}"
        return Event("area", ev.ts, title, severity="warning" if ev.kind in ALERT_KINDS else "info",
                     extra=extra, **common)

    # -- intake (GUI thread) ----------------------------------------------------------------------------
    def _on_incident_added(self, inc) -> None:
        if self._shut or not self._started:
            return
        try:
            self._route(self._incident_event(inc, "incident_new"))
        except Exception:
            log.exception("Integrations: incident intake failed")

    def _on_incident_updated(self, inc) -> None:
        if self._shut or not self._started:
            return
        try:
            self._route(self._incident_event(inc, "incident_update"))
        except Exception:
            log.exception("Integrations: incident update intake failed")

    def _on_area_event(self, ev) -> None:
        if self._shut:
            return
        self._emit_listeners("area_event", ev)
        if not self._started or getattr(ev, "value", "") == "restored":
            return
        try:
            event = self._area_event(ev)
            if event is not None:
                self._route(event)
        except Exception:
            log.exception("Integrations: area event intake failed")

    def _on_status(self, status) -> None:
        if self._shut:
            return
        self._emit_listeners("status", status)
        if not self._started or not isinstance(status, dict):
            return
        mon = getattr(self.ctx, "monitor", None)
        if mon is None or not mon.running:
            return
        now = self.clock()
        for ch, st in status.items():
            try:
                ch = int(ch)
            except (TypeError, ValueError):
                continue
            st = st if isinstance(st, dict) else {}
            tr = self._cams.setdefault(ch, {"since": None, "seen": False, "sent": set(), "name": ""})
            tr["name"] = st.get("name") or tr["name"] or self.frames.camera_name(ch)
            state = st.get("state")
            if state == "offline":
                if tr["seen"] and tr["since"] is None:
                    tr["since"] = now
            elif state in ("watching", "idle"):
                if tr["since"] is not None and tr["sent"]:
                    dur = now - tr["since"]
                    ev = Event("camera_online", now, f"{tr['name']} is back (offline {media.duration_text(dur)})",
                               channel=ch, camera=tr["name"], extra={"offline_s": dur, "since": tr["since"],
                                                                      "channels": [ch]})
                    self._route(ev, only=set(tr["sent"]))
                tr["since"] = None
                tr["sent"] = set()
                tr["seen"] = True

    def _on_config_changed(self) -> None:
        if self._shut:
            return
        self.frames.reset()
        for conf in self.destinations():
            ch = self._channels.get(conf.id)
            if ch is not None and conf.kind == "mqtt":
                try:
                    ch.reconfigure(conf)
                except Exception as exc:
                    ch.set_status("problem", ch.scrub(str(exc)))
        self._emit_listeners("config", None)

    # -- routing ----------------------------------------------------------------------------------------
    def _state(self, dest_id: str) -> DestState:
        return self._states.setdefault(dest_id, DestState())

    def _load_thread(self, dest_id: str, state: DestState, incident_id: str) -> None:
        if not incident_id or incident_id in state.incidents:
            return
        t = self.log.thread(dest_id, incident_id)
        if t is not None:
            state.incidents[incident_id] = {"severity": t.get("severity") or "info", "ref": t.get("ref"),
                                            "last_edit": 0.0}

    def _route(self, ev: Event, only: set | None = None, exclude: set | None = None) -> None:
        for conf in self.destinations():
            if (only is not None and conf.id not in only) or (exclude and conf.id in exclude):
                continue
            try:
                self._route_one(conf, ev)
            except Exception:
                log.exception("Integrations: routing to %s failed", conf.id)

    def _route_one(self, conf: IntegrationConf, ev: Event, row_id: str | None = None) -> Decision | None:
        ch, worker = self._channels.get(conf.id), self._workers.get(conf.id)
        if not conf.enabled or ch is None or worker is None:
            return None
        state = self._state(conf.id)
        inc = ev.incident_id if ev.kind in INCIDENT_KINDS else ""
        self._load_thread(conf.id, state, inc)
        if ev.kind == "incident_update" and inc and inc not in state.incidents:
            ev = ev.copy(kind="incident_new")      # never told here: this is its first message
        waiting = self._waiting.get((conf.id, inc)) if inc else None
        now = self.clock()
        dec = policy.decide(ev, conf.routes, state, now, self.ctx.cfg.settings, caps=ch.CAPS, enabled=conf.enabled)
        if waiting is not None and dec.action != "wait_ai":
            self._waiting.pop((conf.id, inc), None)
            row_id = row_id or waiting["row"]
        self._apply(conf, worker, state, ev, dec, now, row_id)
        return dec

    def _apply(self, conf: IntegrationConf, worker: _Worker, state: DestState, ev: Event, dec: Decision,
               now: float, row_id: str | None = None) -> None:
        inc = ev.incident_id if ev.kind in INCIDENT_KINDS else ""
        a = dec.action
        if a in ("send", "edit"):
            edit = a == "edit"
            if dec.escalated:
                ev = ev.copy(escalated=True)
            info = state.incidents.get(inc) if inc else None
            d = Delivery(row_id or new_delivery_id(), conf.id, ev, dec, now, edit=edit, send_clip=dec.send_clip,
                         pictures=conf.routes.get("pictures", "crop"), web_base=self.web_base(),
                         thread_ref=(info or {}).get("ref"))
            state.note_send(ev, now, counted=not edit and ev.kind in ("incident_new", "incident_update", "area",
                                                                       "plate"))
            if inc and dec.send_clip:
                state.incidents[inc]["clip_sent"] = True
            if inc:
                self._told_status[inc] = ev.status
            self.log.add(d, status="queued", dest_kind=conf.kind, dest_name=conf.name)
            worker.put(d)
        elif a == "wait_ai":
            key = (conf.id, inc)
            entry = self._waiting.get(key)
            if entry is None:
                entry = {"deadline": now + AI_WAIT_S, "event": ev, "row": row_id or new_delivery_id()}
                self._waiting[key] = entry
                self.log.add(Delivery(entry["row"], conf.id, ev, dec, now), status="waiting_ai",
                             dest_kind=conf.kind, dest_name=conf.name)
            else:
                entry["event"] = ev
        elif a in ("hold", "digest"):
            if a == "digest" and not state.digest:
                state.digest_first = now
            (state.held if a == "hold" else state.digest).append(ev)
            self.log.add(Delivery(row_id or new_delivery_id(), conf.id, ev, dec, now), status="held",
                         dest_kind=conf.kind, dest_name=conf.name)
        else:
            if row_id:
                self.log.update(row_id, status="skipped", reason=dec.reason)
            elif dec.log:
                self.log.add(Delivery(new_delivery_id(), conf.id, ev, dec, now), status="skipped",
                             dest_kind=conf.kind, dest_name=conf.name)
            else:
                return
        self._changed()

    def _enqueue_direct(self, conf: IntegrationConf, ev: Event, reason: str, counted: bool = True) -> None:
        """Send an event the hub already decided on (digests), bypassing the policy."""
        worker = self._workers.get(conf.id)
        if worker is None:
            return
        state = self._state(conf.id)
        now = self.clock()
        quiet = conf.routes.get("quiet", {})
        silent = policy.in_quiet_hours(quiet, now) and quiet.get("mode") == "silent"
        dec = Decision("send", reason, silent=silent, priority=policy.priority_for(ev, silent))
        if counted:
            state.sent_times.append(now)
        d = Delivery(new_delivery_id(), conf.id, ev, dec, now, pictures=conf.routes.get("pictures", "crop"),
                     web_base=self.web_base())
        self.log.add(d, status="queued", dest_kind=conf.kind, dest_name=conf.name)
        worker.put(d)
        self._changed()

    @staticmethod
    def _digest_event(events: list[Event], now: float, held: bool) -> Event:
        n = len(events)
        cams = []
        for e in events:
            for c in (e.camera or "").split(", "):
                if c and c not in cams:
                    cams.append(c)
        since = min(e.ts for e in events)
        where = f" on {len(cams)} camera{'s' if len(cams) != 1 else ''}" if cams else ""
        if held:
            title = f"While quiet hours were on: {n} alert{'s' if n != 1 else ''}{where}"
        else:
            title = f"{n} more alert{'s' if n != 1 else ''}{where} since {datetime.fromtimestamp(since):%H:%M}"
        newest = next((e for e in reversed(events) if e.crop_path or e.snapshot_path), events[-1])
        worst = max(events, key=lambda e: SEVERITY_RANK.get(e.severity, 0))
        titles = []
        for e in reversed(events):
            line = f"{datetime.fromtimestamp(e.ts):%H:%M} {e.title}"
            if line not in titles:
                titles.append(line)
        text = "\n".join(titles[:3] + ([f"and {len(titles) - 3} more"] if len(titles) > 3 else []))
        return Event("summary", now, title, text=text, severity=worst.severity, camera=", ".join(cams),
                     snapshot_path=newest.snapshot_path, crop_path=newest.crop_path, web_path="#/incidents",
                     extra={"digest": True, "held": held, "count": n,
                            "incident_ids": sorted({e.incident_id for e in events if e.incident_id})})

    # -- results (GUI thread) ------------------------------------------------------------------------------
    def _on_result(self, info: dict) -> None:
        if self._shut:
            return
        dest_id = info.get("dest_id", "")
        d: Delivery = info.get("delivery")
        res: SendResult = info.get("result")
        status = info.get("status")
        state = self._states.get(dest_id)
        ch = self._channels.get(dest_id)
        inc = d.event.incident_id if d is not None and d.event.kind in INCIDENT_KINDS else ""
        if status in ("sent", "edited"):
            self._fail_count[dest_id] = 0
            self._problem_sent.discard(dest_id)
            if state is not None and inc:
                entry = state.incidents.setdefault(inc, {"severity": d.event.severity, "last_edit": 0.0})
                if info.get("ref") is not None:
                    entry["ref"] = info["ref"]
                if status == "edited":
                    entry["last_edit"] = self.clock()
            old = self._problem_text.pop(dest_id, None)
            if ch is not None and ch.status == "problem" and (old is None or ch.status_text == old):
                ch.set_status("ok")
        elif status in ("retrying", "failed"):
            if state is not None and inc and d.send_clip and status == "failed":
                state.incidents.get(inc, {}).pop("clip_sent", None)
            n = self._fail_count.get(dest_id, 0) + 1
            self._fail_count[dest_id] = n
            text = res.error if res is not None and res.error else "Could not send"
            if res is not None and res.permanent and ch is not None:
                self._problem_text[dest_id] = ch.scrub(text)
                ch.set_status("problem", text)
            permanent = bool(res is not None and res.permanent)
            if (n >= PROBLEM_AFTER or permanent) and dest_id not in self._problem_sent:
                self._problem_sent.add(dest_id)
                conf = self._conf(dest_id)
                name = conf.name if conf is not None else "A connection"
                self.problem.emit(dest_id, f"{name}: {text}")
        self._changed()

    def _changed(self) -> None:
        if not self._shut and not self._changed_timer.isActive():
            self._changed_timer.start()

    # -- timers -------------------------------------------------------------------------------------------
    def _tick_fast(self) -> None:
        if self._shut:
            return
        now = self.clock()
        try:
            self._release_waiting(now)
            self._check_digests(now)
            self._check_offline(now)
        except Exception:
            log.exception("Integrations: timer failed")

    def _release_waiting(self, now: float) -> None:
        for key, entry in list(self._waiting.items()):
            if entry["deadline"] > now:
                continue
            self._waiting.pop(key, None)
            conf = self._conf(key[0])
            if conf is None:
                continue
            if conf.routes.get("incidents", {}).get("send_unchecked", True):
                ev = entry["event"].copy(verdict="unverified", ai_note=media.NOT_CHECKED)
                self._route_one(conf, ev, row_id=entry["row"])
            else:
                self.log.update(entry["row"], status="skipped", reason="The AI did not answer in time")
                self._changed()

    def _check_digests(self, now: float) -> None:
        for conf in self.destinations():
            state = self._states.get(conf.id)
            if state is None or conf.id not in self._workers:
                continue
            rate = conf.routes.get("rate", {})
            if state.digest:
                room = state.recent_sends(now) < int(rate.get("max_per_10min", 10) or 10)
                cool = float(rate.get("cooldown_s", 60) or 0)
                if room and now - state.last_digest >= policy.DIGEST_EVERY_S and now - state.digest_first >= cool:
                    events, state.digest = state.digest, []
                    state.last_digest = now
                    self._enqueue_direct(conf, self._digest_event(events, now, held=False), "Combined message")
            if state.held and not policy.in_quiet_hours(conf.routes.get("quiet", {}), now):
                events, state.held = state.held, []
                self._enqueue_direct(conf, self._digest_event(events, now, held=True), "Quiet hours ended",
                                     counted=False)

    def _check_offline(self, now: float) -> None:
        mon = getattr(self.ctx, "monitor", None)
        if mon is None or not mon.running:
            return
        for ch, tr in self._cams.items():
            if tr["since"] is None:
                continue
            dur = now - tr["since"]
            for conf in self.destinations():
                if not conf.enabled or conf.id in tr["sent"] or conf.id not in self._workers:
                    continue
                off = conf.routes.get("offline", {})
                if not off.get("enabled") or dur < float(off.get("minutes", 5)) * 60:
                    continue
                tr["sent"].add(conf.id)
                ev = Event("camera_offline", now, f"Camera {tr['name']} stopped sending video",
                           text=f"No video since {media.clock(tr['since'], now)}. You will get a message when it is back.",
                           severity="warning", channel=ch, camera=tr["name"],
                           extra={"offline_s": dur, "since": tr["since"], "channels": [ch]})
                self._route_one(conf, ev)

    def _tick_slow(self) -> None:
        if self._shut:
            return
        now = self.clock()
        try:
            self.log.kv_set("heartbeat", repr(now))
            if Path(self._events_dir()) != Path(self.log.dir):
                self.events_dir_changed()
            self._check_summaries(now)
            if now - self._last_cleanup > 86400:
                self._cleanup()
        except Exception:
            log.exception("Integrations: minute timer failed")

    def _cleanup(self) -> None:
        self._last_cleanup = self.clock()
        self.log.cleanup()

    def offline_cameras(self) -> list[str] | None:
        """Names of cameras without video right now; None when monitoring is off (unknown)."""
        mon = getattr(self.ctx, "monitor", None)
        if mon is None or not mon.running:
            return None
        return [tr["name"] for tr in self._cams.values() if tr["since"] is not None]

    def _check_summaries(self, now: float) -> None:
        t = datetime.fromtimestamp(now)
        today = t.strftime("%Y-%m-%d")
        summary = None
        for conf in self.destinations():
            s = conf.routes.get("summary", {})
            if not conf.enabled or not s.get("enabled") or conf.id not in self._workers:
                continue
            try:
                hh, mm = (int(x) for x in str(s.get("time", "08:00")).split(":"))
            except ValueError:
                hh, mm = 8, 0
            target = t.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
            key = f"summary_{conf.id}"
            if not (0 <= now - target < SUMMARY_LATE_S) or self.log.kv_get(key) == today:
                continue
            if summary is None:
                summary = media.build_summary(self.ctx.alert_store, self.ctx.cfg, since=now - 86400, now=now,
                                              offline_cameras=self.offline_cameras())
            self.log.kv_set(key, today)
            self._route_one(conf, summary)

    def _poll_monitoring(self) -> None:
        if self._shut:
            return
        mon = getattr(self.ctx, "monitor", None)
        running = bool(mon is not None and mon.running)
        if running == self._last_running:
            return
        self._last_running = running
        self._cams.clear()
        self._emit_listeners("monitoring", running)
        now = self.clock()
        if now - self._started_at < STARTUP_QUIET_S:
            return
        text = "" if running else "Nothing is being watched until it is turned on again."
        req = self._monitoring_request
        if req is not None and req[0] == running and now - req[3] <= MONITORING_SOURCE_S:
            who = f" by {req[2]}" if req[2] else ""
            text = (f"Turned {'on' if running else 'off'} from {req[1]}{who}. " + text).strip()
        self._monitoring_request = None
        ev = Event("monitoring", now, f"Monitoring is {'on' if running else 'off'}", text=text,
                   extra={"running": running})
        self._route(ev)

    # -- desktop status sync (P2) -------------------------------------------------------------------------------
    def desktop_incidents_changed(self) -> None:
        """Slot for the Incidents page: incidents marked on this PC update their messages."""
        if self._shut or not self._started:
            return
        store = getattr(self.ctx, "alert_store", None)
        if store is None:
            return
        for inc_id, told in list(self._told_status.items())[-100:]:
            try:
                inc = store.get_incident(inc_id)
            except Exception:
                continue
            if inc is None or inc.status == told:
                continue
            self._send_status(inc, "YardGuard on this PC")

    def _send_status(self, inc, by: str, exclude: set | None = None) -> None:
        self._told_status[inc.id] = inc.status
        ev = self._incident_event(inc, "incident_status")
        ev.status_by = by
        ev.extra["status_ts"] = self.clock()
        have = {t["dest_id"] for t in self.log.threads_for(inc.id)}
        have |= {dest for dest, st in self._states.items() if inc.id in st.incidents}
        self._route(ev, only=have, exclude=exclude)

    # -- remote actions -------------------------------------------------------------------------------------------
    def request(self, action: Action) -> concurrent.futures.Future:
        """Thread-safe: run the action on the GUI thread; the future gets a result dict."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if self._shut:
            fut.set_result({"ok": False, "message": "YardGuard is closing"})
            return fut
        self.action_requested.emit(action, fut)
        return fut

    @staticmethod
    def _resolve(fut, result: dict) -> None:
        if not fut.done():
            try:
                fut.set_result(result)
            except concurrent.futures.InvalidStateError:
                pass

    def _run_action(self, action: Action, fut) -> None:
        if self._shut:
            self._resolve(fut, {"ok": False, "message": "YardGuard is closing"})
            return
        try:
            fn = getattr(self, f"_act_{action.kind}", None)
            if fn is None:
                self._resolve(fut, {"ok": False, "message": "Not available"})
                return
            result = fn(action, fut)
            if result is not None:
                self._resolve(fut, result)
        except Exception as exc:
            log.exception("Action %s failed", action.kind)
            self._resolve(fut, {"ok": False, "message": f"That did not work ({type(exc).__name__})."})

    def _act_mark_incident(self, action: Action, fut) -> dict:
        inc_id = str(action.args.get("incident_id") or "")
        status = str(action.args.get("status") or "")
        store = self.ctx.alert_store
        inc = store.get_incident(inc_id) if inc_id else None
        if inc is None:
            return {"ok": False, "title": "", "status": "", "message": "This incident no longer exists."}
        if status not in ("open", "handled", "false_alarm"):
            return {"ok": False, "title": inc.title, "status": inc.status, "message": "Unknown status"}
        store.mark_incident(inc_id, status)
        inc = store.get_incident(inc_id) or inc
        self.incidents_changed.emit()
        by = f"{action.user} ({action.source})" if action.user else action.source
        exclude = {action.args["dest_id"]} if action.args.get("dest_id") else None
        self._send_status(inc, by, exclude=exclude)
        return {"ok": True, "title": inc.title, "status": inc.status}

    def _act_set_monitoring(self, action: Action, fut) -> dict:
        on = bool(action.args.get("on"))
        handler = self._handlers.get("set_monitoring")
        mon = getattr(self.ctx, "monitor", None)
        running = bool(mon is not None and mon.running)
        if handler is None:
            return {"ok": False, "running": running, "message": "Not available"}
        if on == running:
            return {"ok": True, "running": running, "message": f"Monitoring is already {'on' if on else 'off'}"}
        self._monitoring_request = (on, action.source or "a remote", action.user, self.clock())
        handler(on)
        return {"ok": True, "running": bool(mon is not None and mon.running),
                "message": "Monitoring is starting" if on else "Monitoring is stopping"}

    def _act_snapshot(self, action: Action, fut) -> None:
        from ..qt_util import run_async
        try:
            ch = int(action.args.get("channel"))
        except (TypeError, ValueError):
            self._resolve(fut, {"ok": False, "jpeg": b"", "camera": "", "message": "Which camera?"})
            return None
        max_side = int(action.args.get("max_side") or 1280)
        name = self.frames.camera_name(ch)
        if ch not in {c for c, _ in self.frames.cameras()}:
            self._resolve(fut, {"ok": False, "jpeg": b"", "camera": name, "message": f"There is no camera {ch}."})
            return None

        def done(jpeg) -> None:
            if isinstance(jpeg, (bytes, bytearray)) and jpeg:
                self._resolve(fut, {"ok": True, "jpeg": bytes(jpeg), "camera": name})
            else:
                self._resolve(fut, {"ok": False, "jpeg": b"", "camera": name,
                                    "message": f"No picture from {name} right now."})

        run_async(self.frames.latest_jpeg, ch, max_side, on_done=done)
        return None

    def _act_status(self, action: Action, fut) -> dict:
        mon = getattr(self.ctx, "monitor", None)
        running = bool(mon is not None and mon.running)
        try:
            status = mon.camera_status() if running else {}
        except Exception:
            status = {}
        cams = []
        for ch, name in self.frames.cameras():
            st = status.get(ch) or {}
            cams.append({"channel": ch, "name": name,
                         "state": st.get("state", "starting" if running else "off"),
                         "objects": dict(st.get("objects") or {})})
        store = getattr(self.ctx, "alert_store", None)
        try:
            n_open = store.open_incident_count() if store is not None else 0
        except Exception:
            n_open = 0
        site = self.ctx.cfg.active_site()
        message = (getattr(mon, "message", "") or "Monitoring is on") if running else "Monitoring is off"
        return {"ok": True, "running": running, "message": message, "cameras": cams, "open": n_open,
                "snoozed_until": self.snoozed_until(), "site": getattr(site, "name", "")}

    def _act_list_incidents(self, action: Action, fut) -> dict:
        store = self.ctx.alert_store
        limit = int(action.args.get("limit") or 5)
        items = [i for i in store.list_incidents(limit=50, status="open") if i.verdict != "rejected"][:limit]
        return {"ok": True, "items": [{"id": i.id, "title": i.title, "severity": i.severity, "start_ts": i.start_ts,
                                       "cameras": list(i.cameras), "status": i.status} for i in items]}

    def _act_snooze(self, action: Action, fut) -> dict:
        try:
            until = float(action.args.get("until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        self.snooze(until)
        return {"ok": True, "until": self.snoozed_until()}

    def _act_update_chat(self, action: Action, fut) -> dict:
        conf = self._conf(str(action.args.get("dest_id") or ""))
        if conf is None:
            return {"ok": False, "message": "Unknown connection"}
        old, new = action.args.get("old_id"), action.args.get("new_id")
        changed = False
        for chat in conf.params.get("chats", []):
            if str(chat.get("id")) == str(old):
                chat["id"] = int(new) if str(new).lstrip("-").isdigit() else new
                if chat.get("type") == "group":
                    chat["type"] = "supergroup"
                changed = True
        if changed:
            self._save()
            self.notify_status(conf.id)
        return {"ok": changed}
