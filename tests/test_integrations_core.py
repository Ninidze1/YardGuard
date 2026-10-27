"""Tests of the integrations core: config additions, routing policy, IntegrationHub, delivery log,
FrameProvider, media texts / pictures and QR codes. Local fakes only (tests/fakes_integrations.py).

Run:  run.cmd tests\\test_integrations_core.py
Writes only under .cache\\tests\\integrations\\core\\ (config, events folders, pictures).
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "integrations" / "core"
if OUT.exists():
    shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True, exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from yardguard import config  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"

from fakes_integrations import (FakeCtx, FakeHttpSink, FakeSource, make_alert, make_incident,  # noqa: E402
                                write_jpeg, yg_threads)
from yardguard.areas import AreaEvent  # noqa: E402
from yardguard.integrations import base, frames as frames_mod, hub as hub_mod, media, policy, qr  # noqa: E402
from yardguard.integrations.base import (Channel, ChannelError, IntegrationConf, new_integration,  # noqa: E402
                                         normalize_integration, register_test_channel, scrub)
from yardguard.integrations.hub import IntegrationHub  # noqa: E402
from yardguard.integrations.model import Action, Decision, Delivery, Event, SendResult  # noqa: E402
from yardguard.integrations.policy import DestState, decide, in_quiet_hours  # noqa: E402
from yardguard.integrations.store import DeliveryLog  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
TOKEN = "7000000001:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ"
PASSED: list[str] = []
FAILED: list[str] = []

# capture every log line, to prove that no secret ever reaches the log
LOG_BUF = io.StringIO()
_h = logging.StreamHandler(LOG_BUF)
_h.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_h)
logging.getLogger().setLevel(logging.DEBUG)


def check(name: str, cond, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name} {detail}".strip())
        print("FAIL", name, detail)


def pump(ms: int = 50) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.01)
    app.processEvents()
    return bool(pred())


# --- recording channels (test only, registered through base.register_test_channel) -------------------------

class RecordingChannel(Channel):
    KIND = "rec"
    CAPS = {"pictures": True, "clip": True, "edit": True, "buttons": True, "two_way": False,
            "max_upload": 10_000_000, "updates": "edit"}
    SECRET_KEYS = ("token",)

    def __init__(self, conf, hub):
        super().__init__(conf, hub)
        self.sent: list = []
        self.plan: deque = deque()
        self.gate: threading.Event | None = None
        self.lock = threading.Lock()
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def send(self, event, ctx):
        if self.gate is not None:
            self.gate.wait(10)
        with self.lock:
            self.sent.append((event, ctx))
            n = len(self.sent)
            step = self.plan.popleft() if self.plan else None
        if isinstance(step, Exception):
            raise step
        if isinstance(step, SendResult):
            return step
        if ctx.edit:   # like Telegram: an edit keeps the message, so no new ref
            return SendResult(True)
        return SendResult(True, ref={"messages": [{"chat": 1, "id": n}]})

    def describe(self):
        return "Recording channel"


class PlainChannel(RecordingChannel):
    KIND = "rec2"
    CAPS = {"pictures": True, "clip": False, "edit": False, "buttons": False, "two_way": False,
            "max_upload": 10_000_000, "updates": "drop"}


register_test_channel("rec", RecordingChannel)
register_test_channel("rec2", PlainChannel)


def make_hub(name: str, kinds=("rec",), tweak=None, start=True, running=False):
    ctx = FakeCtx(str(OUT / name))
    ctx.monitor.running = running
    for kind in kinds:
        d = new_integration(kind, name=f"{kind} dest")
        IntegrationConf(d).set_secret("token", TOKEN)
        if tweak:
            tweak(d)
        ctx.cfg.settings.integrations.append(d)
    hub = IntegrationHub(ctx)
    hub.health_delay_ms = 0
    if start:
        hub.start()
        pump(20)
    return ctx, hub


def dest(hub, i: int = 0):
    conf = hub.destinations()[i]
    return conf, hub.channel(conf.id)


def rows(hub, **kw):
    return hub.log.list(**kw)


def close(ctx, hub):
    hub.shutdown(timeout=2)
    pump(20)
    ctx.close()


def alert(ctx, **kw):
    kw.setdefault("site_id", ctx.cfg.active_site_id)
    return make_alert(ctx.alert_store, **kw)


# =====================================================================================================
# 1. config, secrets, scrub, http helper
# =====================================================================================================

def test_config():
    from yardguard.config import load_config
    from fakes_integrations import make_config
    cfg = make_config(str(OUT / "cfg_events"))
    d = new_integration("telegram", "My bot")
    conf = IntegrationConf(d)
    conf.set_secret("token", TOKEN)
    conf.params["chats"] = [{"id": 123, "title": "Alex", "type": "private", "enabled": True}]
    cfg.settings.integrations = [d, "junk", {"no_kind": 1}]
    cfg.settings.notify = {"snooze_until": 0.0}
    cfg.settings.remote = {"enabled": False, "port": 8765}
    cfg.save()
    raw = config.CONFIG_PATH.read_text(encoding="utf-8")
    check("config: token never stored in plain text", TOKEN not in raw and "AAHdq" not in raw)
    loaded = load_config()
    s = loaded.settings
    check("config: version 4", s.config_version == 4 == config.CONFIG_VERSION)
    check("config: junk integrations dropped", len(s.integrations) == 1 and s.integrations[0]["id"] == d["id"])
    check("config: secret round trip", IntegrationConf(s.integrations[0]).secret("token") == TOKEN)
    check("config: params round trip", s.integrations[0]["params"]["chats"][0]["id"] == 123)
    check("config: notify/remote dicts", s.notify == {"snooze_until": 0.0} and s.remote["port"] == 8765)
    check("config: install_id 8 hex", len(s.install_id) == 8 and int(s.install_id, 16) >= 0)
    first = s.install_id
    loaded.save()
    check("config: install_id stays", load_config().settings.install_id == first)
    data = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    data["settings"].pop("install_id")
    data["settings"]["integrations"] = "broken"
    data["settings"]["notify"] = []
    config.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
    again = load_config().settings
    check("config: install_id created when missing", len(again.install_id) == 8)
    check("config: wrong types repaired", again.integrations == [] and again.notify == {})
    # secret helpers
    c2 = IntegrationConf({"kind": "email", "secrets": {"password": "bm90IGRwYXBp"}})
    check("conf: unreadable secret detected", c2.secret_unreadable("password") and c2.secret("password") == "")
    conf.set_secret("token", "")
    check("conf: empty secret removes key", not conf.has_secret("token"))
    check("conf: repr has no secret", "AAHdq" not in repr(conf))
    # normalize
    n = normalize_integration({"kind": "ntfy", "routes": {"quiet": {"start": "25:99", "mode": "bad"}}})
    check("normalize: ntfy random topic", n["params"]["topic"].startswith("yardguard-") and len(n["params"]["topic"]) == 30)
    check("normalize: bad quiet values fixed", n["routes"]["quiet"]["start"] == "23:00" and n["routes"]["quiet"]["mode"] == "silent")
    check("normalize: id prefix", n["id"].startswith("ntfy-"))
    e = new_integration("email")
    check("defaults: email both pictures + summary", e["routes"]["pictures"] == "both" and e["routes"]["summary"]["enabled"])
    check("catalog: 6 kinds, 2 groups", len(base.CATALOG) == 6 and [g for g, _ in base.GROUPS] == ["phone", "home"])
    check("load_channel_class: unknown kind None", base.load_channel_class("nope") is None)
    check("load_channel_class: test registry", base.load_channel_class("rec") is RecordingChannel)


def test_scrub_and_http():
    # a real requests exception text with the token in the URL path
    s = socket_free_port()
    try:
        requests.get(f"http://127.0.0.1:{s}/bot{TOKEN}/getMe?chat_id=5&token=abc", timeout=2)
        text = ""
    except requests.RequestException as exc:
        text = str(exc)
    check("scrub: raw requests error contains the token (precondition)", TOKEN.split(":")[1] in text)
    clean = scrub(text, TOKEN)
    check("scrub: token removed from requests error", TOKEN not in clean and TOKEN.split(":")[1] not in clean, clean)
    check("scrub: bot token pattern without the secret given", TOKEN not in scrub(text))
    check("scrub: query string removed", "chat_id=5" not in scrub("GET https://x.y/a?chat_id=5&key=1"))
    check("scrub: userinfo removed", "secretpw" not in scrub("rtsp://admin:secretpw@10.0.0.1:554/x"))
    check("scrub: discord key removed",
          "abcDEF" not in scrub("https://discord.com/api/webhooks/123456/abcDEF-ghi_jkl failed"))
    check("scrub: key=value removed", "hunter2" not in scrub("login failed password=hunter2"))
    check("scrub: plain words kept", scrub("The bot token is no longer valid") == "The bot token is no longer valid")
    check("scrub: url-encoded secret", "p%40ss" not in scrub("x p%40ss y", "p@ss"))
    check("scrub: 300 chars", len(scrub("x" * 1000)) == 300)
    check("mask: telegram style", base.mask(TOKEN) == "7000000001:****awZ")
    check("mask: short", base.mask("abc") == "****" and base.mask("") == "")
    check("url_for_display hides path", base.url_for_display("https://hooks.example.com/abc/def?x=1")
          == "https://hooks.example.com/***")
    # http(): connection error / 429 / 5xx / timeout, never leaking the token
    try:
        base.http("GET", f"http://127.0.0.1:{s}/bot{TOKEN}/getMe", secrets=(TOKEN,), timeout=2)
        err = None
    except ChannelError as exc:
        err = exc
    check("http: connection error -> ChannelError(retry)", err is not None and err.retry and TOKEN not in err.message)
    sink = FakeHttpSink()
    try:
        sink.respond(429, {"ok": False, "parameters": {"retry_after": 7}})
        try:
            base.http("POST", sink.url + "/x", timeout=3)
            err = None
        except ChannelError as exc:
            err = exc
        check("http: 429 retry_after from JSON", err is not None and err.retry and err.retry_after == 7)
        sink.respond(429, b"", headers={"Retry-After": "3"})
        try:
            base.http("POST", sink.url + "/x", timeout=3)
        except ChannelError as exc:
            err = exc
        check("http: 429 Retry-After header", err.retry_after == 3)
        sink.respond(502, b"bad gateway")
        try:
            base.http("POST", sink.url + "/x", timeout=3)
        except ChannelError as exc:
            err = exc
        check("http: 5xx -> retry", err.retry and "502" in err.message)
        sink.respond(404, b"nope")
        r = base.http("GET", sink.url + "/x", timeout=3)
        check("http: 4xx returned to the caller", r.status_code == 404)
        sink.delay_s = 1.0
        try:
            base.http("GET", sink.url + f"/bot{TOKEN}/x", timeout=0.3, secrets=(TOKEN,))
            err = None
        except ChannelError as exc:
            err = exc
        check("http: timeout -> plain words", err is not None and err.retry and "did not answer" in err.message)
        sink.delay_s = 0
    finally:
        sink.close()


def socket_free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =====================================================================================================
# 2. policy
# =====================================================================================================

def ts_at(hour: int, minute: int, weekday: int | None = None) -> float:
    base_day = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is not None:
        base_day += timedelta(days=(weekday - base_day.weekday()) % 7)
    return base_day.timestamp()


def settings(**kw):
    s = SimpleNamespace(notify={}, brain_mode="verify")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


EDIT_CAPS = RecordingChannel.CAPS
PLAIN_CAPS = PlainChannel.CAPS


def inc_event(kind="incident_new", severity="warning", verdict="confirmed", channel=1, inc="inc-1", ts=None, **kw):
    return Event(kind, ts or time.time(), "Person near cars at night", severity=severity, verdict=verdict,
                 channel=channel, camera="Front gate", incident_id=inc, extra={"channels": [channel]}, **kw)


def test_policy():
    R = policy.default_routes
    now = time.time()
    st = DestState()
    S = settings()
    d = decide(inc_event(severity="info"), R(), st, now, S, caps=EDIT_CAPS)
    check("policy: below minimum skipped", d.action == "skip" and "minimum" in d.reason, d.reason)
    d = decide(inc_event(), R(), st, now, S, caps=EDIT_CAPS)
    check("policy: warning confirmed -> send priority 4", d.action == "send" and d.priority == 4 and not d.silent)
    r = R()
    r["incidents"]["cameras"] = [2]
    check("policy: camera filter", decide(inc_event(channel=1), r, DestState(), now, S).action == "skip")
    check("policy: camera filter match", decide(inc_event(channel=2), r, DestState(), now, S).action == "send")
    d = decide(inc_event(verdict="rejected"), R(), DestState(), now, S, caps=EDIT_CAPS)
    check("policy: AI rejected skipped", d.action == "skip" and "dismissed" in d.reason)
    d = decide(inc_event(verdict="pending"), R(), DestState(), now, S, caps=EDIT_CAPS)
    check("policy: pending + confirmed_only -> wait_ai", d.action == "wait_ai")
    d = decide(inc_event(verdict="pending"), R(), DestState(), now, settings(brain_mode="off"))
    check("policy: brain off -> no wait", d.action == "send")
    d = decide(inc_event(verdict="unverified"), R(), DestState(), now, S)
    check("policy: after the AI timeout (unverified) -> send", d.action == "send")
    # updates
    st = DestState()
    st.note_send(inc_event(), now)
    upd = inc_event(kind="incident_update")
    check("policy: update -> edit (edit channel)", decide(upd, R(), st, now, S, caps=EDIT_CAPS).action == "edit")
    d = decide(upd, R(), st, now, S, caps=PLAIN_CAPS)
    check("policy: update -> skip (no edit)", d.action == "skip" and "Already" in d.reason)
    r = R()
    r["incidents"]["updates"] = "each"
    check("policy: updates each -> send", decide(upd, r, st, now, S, caps=PLAIN_CAPS).action == "send")
    r["incidents"]["updates"] = "quiet"
    check("policy: updates quiet -> skip", decide(upd, r, st, now, S, caps=EDIT_CAPS).action == "skip")
    esc = inc_event(kind="incident_update", severity="critical")
    d = decide(esc, R(), st, now, S, caps=EDIT_CAPS)
    check("policy: escalation -> new message", d.action == "send" and d.escalated and d.priority == 5)
    mqtt_caps = dict(PLAIN_CAPS, updates="each")
    check("policy: 'each' channel (MQTT) gets updates", decide(upd, R(), st, now, S, caps=mqtt_caps).action == "send")
    r = R()
    r["clip"] = True
    clip_upd = inc_event(kind="incident_update", clip_path="x.mp4")
    d = decide(clip_upd, r, st, now, S, caps=EDIT_CAPS)
    check("policy: clip ready -> edit + send_clip", d.action == "edit" and d.send_clip)
    d = decide(clip_upd, r, st, now, S, caps=dict(PLAIN_CAPS, clip=True))
    check("policy: clip ready, no edit -> send clip", d.action == "send" and d.send_clip)
    never = inc_event(kind="incident_update", inc="inc-new")
    check("policy: update of an incident never sent = new", decide(never, R(), st, now + 120, S).action == "send")
    # incident_status
    stat = inc_event(kind="incident_status", status="handled")
    check("policy: status -> edit where it was sent", decide(stat, R(), st, now, S, caps=EDIT_CAPS).action == "edit")
    check("policy: status not sent -> skip", decide(stat, R(), DestState(), now, S, caps=EDIT_CAPS).action == "skip")
    check("policy: status on plain channel -> skip", decide(stat, R(), st, now, S, caps=PLAIN_CAPS).action == "skip")
    # cooldown + cap
    st = DestState()
    st.note_send(inc_event(inc="a"), now)
    d = decide(inc_event(inc="b"), R(), st, now + 10, S)
    check("policy: cooldown -> digest", d.action == "digest" and "Too soon" in d.reason)
    check("policy: other camera not in cooldown", decide(inc_event(inc="c", channel=2), R(), st, now + 10, S).action == "send")
    check("policy: critical bypasses cooldown",
          decide(inc_event(inc="d", severity="critical"), R(), st, now + 10, S).action == "send")
    check("policy: after cooldown -> send", decide(inc_event(inc="e"), R(), st, now + 61, S).action == "send")
    st = DestState()
    for i in range(10):
        st.note_send(inc_event(inc=f"x{i}", channel=10 + i), now + i)
    d = decide(inc_event(inc="y0", channel=97), R(), st, now + 20, S)
    check("policy: cap -> digest", d.action == "digest" and "10 minutes" in d.reason)
    d = decide(inc_event(inc="y", channel=99, severity="critical"), R(), st, now + 20, S)
    check("policy: critical incident gets through the cap", d.action == "send", f"{d.action} {d.reason}")
    flood = DestState()
    for i in range(20):
        flood.note_send(inc_event(inc=f"f{i}", channel=10 + i, severity="critical"), now + i)
    d = decide(inc_event(inc="y2", channel=99, severity="critical"), R(), flood, now + 30, S)
    check("policy: critical capped only at twice the cap", d.action == "digest", d.action)
    check("policy: cap frees after 10 min", decide(inc_event(inc="z", channel=98), R(), st, now + 700, S).action == "send")
    # quiet hours
    q = {"enabled": True, "start": "23:00", "end": "07:00", "days": list(range(7)), "mode": "silent",
         "critical_through": True}
    check("quiet: 23:30 in", in_quiet_hours(q, ts_at(23, 30)))
    check("quiet: 06:59 in", in_quiet_hours(q, ts_at(6, 59)))
    check("quiet: 07:00 out", not in_quiet_hours(q, ts_at(7, 0)))
    check("quiet: 22:59 out", not in_quiet_hours(q, ts_at(22, 59)))
    check("quiet: disabled", not in_quiet_hours(dict(q, enabled=False), ts_at(23, 30)))
    fri = dict(q, days=[4])
    check("quiet: Friday 23:30 (Friday only)", in_quiet_hours(fri, ts_at(23, 30, weekday=4)))
    check("quiet: Saturday 06:00 counts as Friday night", in_quiet_hours(fri, ts_at(6, 0, weekday=5)))
    check("quiet: Saturday 23:30 not", not in_quiet_hours(fri, ts_at(23, 30, weekday=5)))
    check("quiet: Friday 06:00 belongs to Thursday", not in_quiet_hours(fri, ts_at(6, 0, weekday=4)))
    day = dict(q, start="09:00", end="17:00")
    check("quiet: same-day range", in_quiet_hours(day, ts_at(12, 0)) and not in_quiet_hours(day, ts_at(18, 0)))
    check("quiet: end time", abs(policy.quiet_end(q, ts_at(23, 30)) - (ts_at(7, 0) + 86400)) < 2)
    night = ts_at(23, 30)
    r = R()
    r["quiet"] = dict(q)
    d = decide(inc_event(ts=night), r, DestState(), night, S)
    check("quiet: silent -> send silently priority 2", d.action == "send" and d.silent and d.priority == 2)
    d = decide(inc_event(severity="critical", ts=night), r, DestState(), night, S)
    check("quiet: critical gets through loud", d.action == "send" and not d.silent and d.priority == 5)
    r["quiet"]["critical_through"] = False
    check("quiet: critical_through off -> silent", decide(inc_event(severity="critical"), r, DestState(), night, S).silent)
    r["quiet"].update(mode="hold", critical_through=True)
    check("quiet: hold", decide(inc_event(), r, DestState(), night, S).action == "hold")
    r["quiet"]["mode"] = "drop"
    check("quiet: drop", decide(inc_event(), r, DestState(), night, S).action == "skip")
    # snooze
    sn = settings(notify={"snooze_until": now + 3600})
    d = decide(inc_event(), R(), DestState(), now, sn)
    check("snooze: warning skipped with time", d.action == "skip" and "paused until" in d.reason)
    check("snooze: critical gets through", decide(inc_event(severity="critical"), R(), DestState(), now, sn).action == "send")
    check("snooze: expired snooze ignored", decide(inc_event(), R(), DestState(), now + 4000, sn).action == "send")
    st_sn = DestState()
    st_sn.note_send(inc_event(inc="sn1"), now - 60)
    d = decide(inc_event(kind="incident_status", inc="sn1", status="handled"), R(), st_sn, now, sn, caps=EDIT_CAPS)
    check("snooze: status change still updates the sent message", d.action == "edit" and d.silent, d.reason)
    # test bypass
    t = Event("test", now, "YardGuard test")
    check("test: bypasses disabled + snooze", decide(t, R(), DestState(), now, sn, enabled=False).action == "send")
    check("disabled: skipped quietly", not decide(inc_event(), R(), DestState(), now, S, enabled=False).log)
    # areas
    area = Event("area", now, "Car blocking Gate", severity="warning", area_id="a1", area_kind="blocked", channel=1,
                 extra={"channels": [1]})
    check("areas: not chosen -> skip", decide(area, R(), DestState(), now, S).action == "skip")
    r = R()
    r["areas"] = {"a1": {"kinds": [], "mode": "instant"}}
    check("areas: instant + alert kind -> send", decide(area, r, DestState(), now, S).action == "send")
    counted = Event("area", now, "Car in at Gate", area_id="a1", area_kind="exit", channel=1)
    check("areas: [] kinds = alert kinds only", decide(counted, r, DestState(), now, S).action == "skip")
    r["areas"]["a1"]["kinds"] = ["exit"]
    check("areas: explicit kinds", decide(counted, r, DestState(), now, S).action == "send"
          and decide(area, r, DestState(), now, S).action == "skip")
    r["areas"]["a1"] = {"kinds": [], "mode": "summary"}
    st = DestState()
    check("areas: summary mode -> kept", decide(area, r, st, now, S).action == "skip" and st.area_summary["a1"] == 1)
    r["areas"]["a1"]["mode"] = "instant"
    alerting = area.copy(extra={"alerting": True, "channels": [1]})
    check("areas: 'Alert me' areas not doubled", decide(alerting, r, DestState(), now, S).action == "skip")
    # plates
    known = Event("plate", now, "Known plate", plate="AB123CD", plate_known=True, severity="info", channel=1)
    unknown = known.copy(plate_known=False, severity="warning")
    for mode, k_ok, u_ok in (("off", False, False), ("unknown", False, True), ("known", True, False),
                             ("all", True, True)):
        r = R()
        r["plates"] = mode
        check(f"plates: {mode}", (decide(known, r, DestState(), now, S).action == "send") == k_ok
              and (decide(unknown, r, DestState(), now, S).action == "send") == u_ok)
    check("plates: one misread char is known", policy.plate_is_known("AB-123-CE", ["ab123cd"]))
    check("plates: two misread chars unknown", not policy.plate_is_known("AB-128-CE", ["ab123cd"]))
    check("plates: dict entries", policy.plate_is_known("AB123CD", [{"plate": "AB123CD", "name": "Dad"}]))
    # offline / health / summary switches
    off = Event("camera_offline", now, "Camera Backyard stopped sending video", severity="warning", channel=1)
    check("offline: default on", decide(off, R(), DestState(), now, S).action == "send")
    r = R()
    r["offline"]["back"] = False
    check("offline: back switch", decide(off.copy(kind="camera_online"), r, DestState(), now, S).action == "skip")
    r["health"]["enabled"] = False
    check("health: switch", decide(Event("health", now, "x"), r, DestState(), now, S).action == "skip")
    check("summary: off by default", decide(Event("summary", now, "x"), R(), DestState(), now, S).action == "skip")
    check("summary: email default on", decide(Event("summary", now, "x"), R("email"), DestState(), now, S).action == "send")
    # presets / explain
    from fakes_integrations import make_config
    from yardguard.config import new_area
    cfg = make_config(str(OUT / "preset_events"))
    cfg.active_site().cameras[0].areas.append(new_area("count", "line", [(0, 0), (1, 1)], "Gate"))
    cfg.active_site().cameras[0].areas.append(new_area("presence", "polygon", [(0, 0), (1, 0), (1, 1)], "Yard"))
    r = policy.apply_preset(R(), "everything", cfg)
    check("preset everything", r["incidents"]["min_severity"] == "info" and r["plates"] == "all"
          and r["summary"]["enabled"] and len(r["areas"]) == 1)
    r = policy.apply_preset(r, "important", cfg)
    check("preset important", r["incidents"]["min_severity"] == "warning" and r["areas"] == {} and r["plates"] == "off")
    lines_ = policy.explain(policy.apply_preset(R(), "everything", cfg), cfg)
    check("explain lines", lines_ and lines_[0].startswith("Every incident") and any("Daily summary" in x for x in lines_))


# =====================================================================================================
# 3. hub
# =====================================================================================================

def test_hub_basic():
    ctx, hub = make_hub("hub_basic")
    conf, ch = dest(hub)
    check("hub: channel started", ch is not None and ch.started)
    check("hub: worker thread", f"yg-notify-{conf.id}" in yg_threads())
    a = alert(ctx, verdict="confirmed", severity="warning")
    inc = ctx.raise_alert(a)
    check("hub: incident_added -> 1 send", wait_until(lambda: len(ch.sent) == 1))
    ev, sctx = ch.sent[0]
    check("hub: event fields", ev.kind == "incident_new" and ev.incident_id == inc.id and ev.camera == "Front gate"
          and ev.crop_path and ev.snapshot_path and ev.labels[0] == "person" and ev.ai_note == "hand on door handle",
          repr(ev))
    check("hub: title without camera suffix", ev.title == "Person near cars at night", ev.title)
    check("hub: context", sctx.pictures == "crop" and not sctx.edit and sctx.priority == 4 and sctx.attempt == 1)
    check("hub: sent row", wait_until(lambda: rows(hub)[0]["status"] == "sent"))
    check("hub: thread stored", (hub.log.thread(conf.id, inc.id) or {}).get("ref") == {"messages": [{"chat": 1, "id": 1}]})
    # three quick updates -> coalesced edits
    old = policy.EDIT_THROTTLE_S
    policy.EDIT_THROTTLE_S = 0.6
    try:
        for i in range(3):
            ctx.raise_alert(alert(ctx, verdict="confirmed", severity="warning", ts=time.time() + i + 1))
        check("hub: updates arrive as edits", wait_until(lambda: len(ch.sent) >= 2, 3))
        pump(1500)
        edits = [s for e, s in ch.sent if s.edit]
        check("hub: 3 updates coalesced into <= 2 edits", 1 <= len(edits) <= 2, str(len(edits)))
        check("hub: last edit shows the newest state", ch.sent[-1][0].alert_count == 4, str(ch.sent[-1][0].alert_count))
        check("hub: edit refers to the first message", ch.sent[-1][1].thread_ref == {"messages": [{"chat": 1, "id": 1}]})
        check("hub: no extra new messages", sum(1 for e, s in ch.sent if not s.edit) == 1)
    finally:
        policy.EDIT_THROTTLE_S = old
    check("hub: deliveries_changed fires", wait_until(lambda: True))
    close(ctx, hub)
    check("hub: shutdown leaves no yg threads", not yg_threads(), str(yg_threads()))
    check("hub: channel stopped", ch.stopped)


def test_hub_ai_wait():
    ctx, hub = make_hub("hub_ai")
    conf, ch = dest(hub)
    a = alert(ctx, verdict="pending", severity="warning")
    inc = ctx.raise_alert(a)
    pump(200)
    check("ai: pending incident waits", not ch.sent and rows(hub)[0]["status"] == "waiting_ai")
    row_id = rows(hub)[0]["id"]
    a.verdict = "confirmed"
    a.brain_findings = ["hand on door handle"]
    ctx.update_alert(a)
    check("ai: confirmed -> sent", wait_until(lambda: len(ch.sent) == 1))
    check("ai: sent as the first message", ch.sent[0][0].kind == "incident_new" and ch.sent[0][0].verdict == "confirmed")
    check("ai: same log row", wait_until(lambda: hub.log.get(row_id)["status"] == "sent"))
    # timeout: send anyway with "not checked by AI"
    t0 = time.time()
    hub.clock = lambda: t0
    b = alert(ctx, verdict="pending", severity="warning", channel=2, camera="Driveway")
    ctx.alert_store.add(b, gap_s=-1)
    ctx.alert_bridge.incident_added.emit(ctx.alert_store.get_incident(b.incident_id))
    pump(100)
    check("ai: second waits", len(ch.sent) == 1)
    hub.clock = lambda: t0 + 61
    hub._tick_fast()
    check("ai: timeout -> sent unchecked", wait_until(lambda: len(ch.sent) == 2))
    check("ai: note says not checked", ch.sent[-1][0].ai_note == media.NOT_CHECKED
          and media.ai_line(ch.sent[-1][0]) == "Not checked by AI")
    # send_unchecked off -> skipped
    conf.routes["incidents"]["send_unchecked"] = False
    c = alert(ctx, verdict="pending", severity="warning", channel=1, ts=time.time() + 5)
    ctx.alert_store.add(c, gap_s=-1)
    hub.clock = lambda: t0 + 100
    ctx.alert_bridge.incident_added.emit(ctx.alert_store.get_incident(c.incident_id))
    pump(50)
    hub.clock = lambda: t0 + 200
    hub._tick_fast()
    pump(100)
    check("ai: send_unchecked off -> skipped", len(ch.sent) == 2
          and any(r["status"] == "skipped" and "did not answer" in (r["reason"] or "") for r in rows(hub)))
    # rejected while waiting -> skipped
    d = alert(ctx, verdict="pending", severity="warning", channel=2, camera="Driveway", ts=time.time() + 10)
    ctx.alert_store.add(d, gap_s=-1)
    ctx.alert_bridge.incident_added.emit(ctx.alert_store.get_incident(d.incident_id))
    pump(50)
    d.verdict = "rejected"
    ctx.update_alert(d)
    pump(100)
    check("ai: rejected -> skipped", len(ch.sent) == 2 and any("dismissed" in (r["reason"] or "") for r in rows(hub)))
    close(ctx, hub)


def test_hub_retry_expiry_drop_problem():
    old_backoff = list(hub_mod.BACKOFF)
    hub_mod.BACKOFF[:] = [0.05] * 5
    try:
        ctx, hub = make_hub("hub_retry", tweak=lambda d: d["routes"]["rate"].update(cooldown_s=0))
        conf, ch = dest(hub)
        problems = []
        hub.problem.connect(lambda i, t: problems.append((i, t)))
        ch.plan.extend([SendResult(False, error="Server busy", retry=True),
                        SendResult(False, error="Server busy", retry=True, retry_after=0.1)])
        ctx.raise_alert(alert(ctx))
        check("retry: sent on the third attempt", wait_until(lambda: len(ch.sent) == 3, 4))
        check("retry: row sent with 3 attempts", wait_until(lambda: rows(hub)[0]["status"] == "sent")
              and rows(hub)[0]["attempts"] == 3, str(rows(hub)[0]))
        check("retry: attempt numbers", [s.attempt for _, s in ch.sent] == [1, 2, 3])
        check("retry: no problem after 2 failures", not problems)
        # failing for good: 6 attempts then failed; problem after 3 failures in a row
        ch.plan.extend([SendResult(False, error=f"Server busy {TOKEN}", retry=True)] * 6)
        ctx.raise_alert(alert(ctx, channel=2, camera="Driveway", ts=time.time() + 400), gap_s=-1)
        check("retry: gives up after 6", wait_until(lambda: any(r["status"] == "failed" for r in rows(hub)), 5))
        check("retry: 6 attempts", len(ch.sent) == 9, str(len(ch.sent)))
        check("problem: emitted once", wait_until(lambda: len(problems) == 1) and problems[0][0] == conf.id)
        check("problem: no token in the text", TOKEN not in problems[0][1] and "AAHdq" not in problems[0][1])
        check("problem: no token in the log rows", all(TOKEN not in (r["reason"] or "") for r in rows(hub)))
        # manual retry of the failed row
        failed = next(r for r in rows(hub) if r["status"] == "failed")
        check("retry(): re-queued", hub.retry(failed["id"]))
        check("retry(): sent", wait_until(lambda: hub.log.get(failed["id"])["status"] == "sent"))
        check("retry(): not twice", not hub.retry(failed["id"]))
        # exceptions inside send() are caught and scrubbed
        ch.plan.append(RuntimeError(f"boom with {TOKEN}"))
        hub._route(Event("test", time.time(), "x"))
        check("raise: retried after an exception", wait_until(lambda: len(ch.sent) >= 12, 3))
        # expiry: an alert older than 30 minutes is not sent
        n = len(ch.sent)
        ctx.raise_alert(alert(ctx, ts=time.time() - 2000), gap_s=-1)
        check("expiry: old event expired", wait_until(lambda: any(r["status"] == "expired" for r in rows(hub))))
        check("expiry: not sent", len(ch.sent) == n)
        # permanent failure: problem at once, destination status "problem"
        ctx2, hub2 = make_hub("hub_perm")
        conf2, ch2 = dest(hub2)
        probs2 = []
        hub2.problem.connect(lambda i, t: probs2.append(t))
        ch2.plan.append(SendResult(False, error="The bot token is no longer valid", permanent=True))
        hub2._route(Event("test", time.time(), "x"))
        check("permanent: channel status problem", wait_until(lambda: ch2.status == "problem")
              and "no longer valid" in ch2.status_text)
        check("permanent: retry row waits 5 min", wait_until(lambda: rows(hub2)[0]["status"] == "retrying"))
        hub2._route(Event("test", time.time(), "y"))
        pump(300)
        check("permanent: no hammering (1 try per 5 min)", len(ch2.sent) == 1)
        close(ctx2, hub2)
        # queue full: oldest non-critical dropped
        old_max = hub_mod.QUEUE_MAX
        hub_mod.QUEUE_MAX = 3
        try:
            ch.gate = threading.Event()
            for i in range(6):
                hub._route(Event("test", time.time(), f"t{i}", severity="critical" if i == 1 else "info"))
            pump(200)
            dropped = [r for r in rows(hub) if r["status"] == "dropped"]
            check("queue: overflow dropped", len(dropped) >= 2, str(len(dropped)))
            check("queue: critical kept", all(r["title"] != "t1" for r in dropped))
        finally:
            hub_mod.QUEUE_MAX = old_max
            ch.gate.set()
            ch.gate = None
        close(ctx, hub)
        check("log: no token anywhere in the log output", TOKEN not in LOG_BUF.getvalue()
              and TOKEN.split(":")[1] not in LOG_BUF.getvalue())
    finally:
        hub_mod.BACKOFF[:] = old_backoff


def test_hub_restart_requeue():
    ctx, hub = make_hub("hub_restart")
    conf, ch = dest(hub)
    ch.gate = threading.Event()
    hub._route(Event("test", time.time(), "in flight"))
    hub._route(Event("test", time.time(), "waiting"))
    pump(150)
    t0 = time.monotonic()
    hub.shutdown(timeout=0.5)
    check("shutdown: bounded with a stuck send", time.monotonic() - t0 < 2.0)
    ch.gate.set()
    statuses = {r["title"]: r["status"] for r in DeliveryLog(str(Path(ctx.cfg.settings.events_dir))).list()}
    check("shutdown: queued rows stay queued", statuses.get("waiting") == "queued", str(statuses))
    log2 = DeliveryLog(ctx.cfg.settings.events_dir)
    old = Delivery("dl-old", conf.id, Event("test", time.time() - 3000, "too old"), Decision("send", ""),
                   time.time() - 3000)
    log2.add(old, status="queued")
    log2.update("dl-old", ts=time.time() - 3000)
    log2.close()
    hub2 = IntegrationHub(ctx)
    hub2.start()
    ch2 = hub2.channel(conf.id)
    check("restart: young queued row sent", wait_until(lambda: any(e.title == "waiting" for e, _ in ch2.sent)))
    check("restart: old row expired", wait_until(lambda: hub2.log.get("dl-old")["status"] == "expired"))
    check("restart: old row not sent", not any(e.title == "too old" for e, _ in ch2.sent))
    hub2.shutdown()
    pump(50)
    ctx.close()
    check("restart: no threads", not yg_threads(), str(yg_threads()))


def test_hub_offline_health_summary():
    t = {"now": time.time()}
    ctx = FakeCtx(str(OUT / "hub_offline"))
    ctx.monitor.running = True
    d = new_integration("rec", "rec")
    d["routes"]["summary"].update(enabled=True, time="08:00")
    ctx.cfg.settings.integrations.append(d)
    # heartbeat gap -> health event
    pre = DeliveryLog(ctx.cfg.settings.events_dir)
    pre.kv_set("heartbeat", repr(time.time() - 3 * 3600))
    pre.close()
    hub = IntegrationHub(ctx)
    hub.health_delay_ms = 0
    hub.clock = lambda: t["now"]
    hub.start()
    conf, ch = dest(hub)
    check("health: back after a gap", wait_until(lambda: any(e.kind == "health" for e, _ in ch.sent)))
    hev = next(e for e, _ in ch.sent if e.kind == "health")
    check("health: text", hev.title == "YardGuard is back" and "not running from" in hev.text, hev.text)
    check("health: heartbeat updated", float(hub.log.kv_get("heartbeat")) >= t["now"] - 1)
    ch.sent.clear()
    # offline debounce
    hub._on_status({1: {"state": "watching", "name": "Front gate"}, 2: {"state": "watching", "name": "Driveway"}})
    t0 = t["now"]
    hub._on_status({1: {"state": "offline", "name": "Front gate"}, 2: {"state": "watching", "name": "Driveway"}})
    t["now"] = t0 + 240
    hub._tick_fast()
    pump(100)
    check("offline: not before 5 min", not ch.sent)
    t["now"] = t0 + 301
    hub._tick_fast()
    check("offline: notice after 5 min", wait_until(lambda: len(ch.sent) == 1))
    e = ch.sent[0][0]
    check("offline: wording", e.kind == "camera_offline" and e.title == "Camera Front gate stopped sending video"
          and e.severity == "warning", e.title)
    t["now"] = t0 + 400
    hub._tick_fast()
    pump(100)
    check("offline: only once", len(ch.sent) == 1)
    t["now"] = t0 + 1020
    hub._on_status({1: {"state": "watching", "name": "Front gate"}})
    check("offline: back notice", wait_until(lambda: len(ch.sent) == 2))
    check("offline: back wording", ch.sent[1][0].title == "Front gate is back (offline 17 min)", ch.sent[1][0].title)
    # never-seen cameras and stopped monitoring give no notice
    hub._on_status({3: {"state": "offline", "name": "New cam"}})
    t["now"] += 1000
    hub._tick_fast()
    pump(100)
    check("offline: camera never seen online -> no notice", len(ch.sent) == 2)
    # summary at the configured minute, once a day
    ch.sent.clear()
    a, inc = make_incident(ctx.alert_store, "critical", "confirmed", site_id=ctx.cfg.active_site_id)
    make_incident(ctx.alert_store, "warning", "rejected", site_id=ctx.cfg.active_site_id, ts=time.time() + 1)
    eight = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
    t["now"] = eight - 60
    hub._tick_slow()
    pump(100)
    check("summary: not before the time", not ch.sent)
    t["now"] = eight + 30
    hub._tick_slow()
    check("summary: sent at 08:00", wait_until(lambda: len(ch.sent) == 1))
    s = ch.sent[0][0]
    check("summary: kind + text", s.kind == "summary" and "1 incident (1 critical)" in s.text
          and "1 false alarm" in s.text, s.text)
    check("summary: picture of the worst incident", s.crop_path == a.crop_path)
    t["now"] = eight + 90
    hub._tick_slow()
    pump(100)
    check("summary: only once a day", len(ch.sent) == 1)
    # monitoring switched off from a remote -> monitoring event with who did it
    calls = []
    hub.set_handlers(set_monitoring=lambda on: calls.append(on))
    t["now"] = hub._started_at + 5000      # well after the start-up quiet time
    fut = hub.request(Action("set_monitoring", {"on": False}, "Telegram", "Alex"))
    check("action: set_monitoring", wait_until(fut.done) and fut.result()["ok"] and calls == [False])
    ctx.monitor.running = False
    hub._poll_monitoring()
    check("monitoring: event", wait_until(lambda: any(e.kind == "monitoring" for e, _ in ch.sent)))
    m = next(e for e, _ in ch.sent if e.kind == "monitoring")
    check("monitoring: wording", m.title == "Monitoring is off" and "from Telegram by Alex" in m.text, m.text)
    close(ctx, hub)


def test_hub_actions_and_misc():
    ctx, hub = make_hub("hub_actions", kinds=("rec", "rec2"), running=True)
    (c1, ch1), (c2, ch2) = dest(hub, 0), dest(hub, 1)
    old_throttle = policy.EDIT_THROTTLE_S
    policy.EDIT_THROTTLE_S = 0.3
    changed = []
    hub.incidents_changed.connect(lambda: changed.append(1))
    a = alert(ctx)
    inc = ctx.raise_alert(a)
    check("multi: both destinations", wait_until(lambda: len(ch1.sent) == 1 and len(ch2.sent) == 1))
    pump(100)
    # mark_incident from another thread (like a Telegram poller)
    box = {}

    def remote():
        f = hub.request(Action("mark_incident", {"incident_id": inc.id, "status": "handled", "dest_id": c2.id},
                               "Telegram", "Alex"))
        box["result"] = f.result(timeout=5)

    th = threading.Thread(target=remote)
    th.start()
    check("action: mark from a thread", wait_until(lambda: "result" in box, 5) and box["result"]["ok"])
    th.join(2)
    check("action: store updated", ctx.alert_store.get_incident(inc.id).status == "handled")
    check("action: incidents_changed", changed == [1])
    check("action: status edit to the other destination", wait_until(lambda: len(ch1.sent) == 2))
    st_ev, st_ctx = ch1.sent[-1]
    check("action: status event", st_ev.kind == "incident_status" and st_ctx.edit and st_ev.status == "handled"
          and media.status_line(st_ev).startswith("Handled by Alex (Telegram) at"), media.status_line(st_ev))
    check("action: requester not re-sent", len(ch2.sent) == 1)
    r = hub.request(Action("mark_incident", {"incident_id": "inc-nope", "status": "handled"}))
    check("action: unknown incident", wait_until(r.done) and not r.result()["ok"])
    # desktop change -> status edit (P2)
    ctx.alert_store.mark_incident(inc.id, "open")
    hub.desktop_incidents_changed()
    check("desktop: reopen edits the message", wait_until(lambda: len(ch1.sent) == 3)
          and media.status_line(ch1.sent[-1][0]).startswith("Reopened by YardGuard on this PC"))
    # snapshot from the monitor's frames
    r = hub.request(Action("snapshot", {"channel": 1, "max_side": 640}, "Telegram"))
    check("action: snapshot", wait_until(r.done, 5) and r.result()["ok"])
    img = cv2.imdecode(np.frombuffer(r.result()["jpeg"], np.uint8), cv2.IMREAD_COLOR)
    check("action: snapshot size", img is not None and max(img.shape[:2]) == 640 and r.result()["camera"] == "Front gate")
    r = hub.request(Action("snapshot", {"channel": 9}))
    check("action: snapshot unknown camera", wait_until(r.done) and not r.result()["ok"])
    r = hub.request(Action("status", {}, "Telegram"))
    check("action: status", wait_until(r.done) and r.result()["running"] and len(r.result()["cameras"]) == 2
          and r.result()["cameras"][0]["state"] == "watching" and r.result()["open"] == 1, str(r.result()))
    r = hub.request(Action("list_incidents", {}))
    check("action: list_incidents", wait_until(r.done) and r.result()["items"][0]["id"] == inc.id)
    until = time.time() + 3600
    r = hub.request(Action("snooze", {"until": until}))
    check("action: snooze", wait_until(r.done) and abs(r.result()["until"] - until) < 1
          and abs(hub.snoozed_until() - until) < 1)
    saved = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    check("snooze: saved", abs(saved["settings"]["notify"]["snooze_until"] - until) < 1)
    hub.snooze(0)
    check("snooze: resume", hub.snoozed_until() == 0)
    r = hub.request(Action("set_monitoring", {"on": True}))
    check("action: no handler -> not available", wait_until(r.done) and r.result()["message"] == "Not available")
    c1.params["chats"] = [{"id": -100, "title": "Family", "type": "group", "enabled": True}]
    r = hub.request(Action("update_chat", {"dest_id": c1.id, "old_id": -100, "new_id": -1001234}))
    check("action: update_chat", wait_until(r.done) and r.result()["ok"] and c1.params["chats"][0]["id"] == -1001234
          and c1.params["chats"][0]["type"] == "supergroup")
    r = hub.request(Action("explode", {}))
    check("action: unknown kind", wait_until(r.done) and r.result() == {"ok": False, "message": "Not available"})
    # area + plate events
    ctx.cfg.active_site().cameras[0].areas = []
    from yardguard.config import new_area
    gate = new_area("log_plates", "line", [(0, 0), (1, 1)], "Gate barrier", params={"known": ["AA111AA"],
                                                                                   "min_plate_px": 80})
    gate["alert"] = False
    ctx.cfg.active_site().cameras[0].areas.append(gate)
    c1.routes["plates"] = "all"
    c1.routes["rate"]["cooldown_s"] = 0
    n = len(ch1.sent)
    pic = write_jpeg(OUT / "hub_actions" / "plate.jpg", text="plate")
    ev = AreaEvent(id="ae1", site_id=ctx.cfg.active_site_id, channel=1, area_id=gate["id"], area_name="Gate barrier",
                   task="log_plates", ts=time.time(), kind="plate", label="car", value="AB-123-CD", crop_path=pic)
    feed = []
    hub.add_listener(lambda k, p: feed.append(k))
    ctx.alert_bridge.area_event_added.emit(ev)
    check("plate: sent", wait_until(lambda: len(ch1.sent) == n + 1))
    pe = ch1.sent[-1][0]
    check("plate: wording", pe.kind == "plate" and pe.title == "Unknown plate AB-123-CD at Gate barrier"
          and pe.severity == "warning" and pe.plate_known is False, pe.title)
    check("plate: other destination (plates off) skipped", len(ch2.sent) == 1)
    check("listener: area_event fed", feed == ["area_event"])
    ev2 = AreaEvent(id="ae2", site_id=ctx.cfg.active_site_id, channel=1, area_id=gate["id"], area_name="Gate barrier",
                    task="log_plates", ts=time.time(), kind="plate", label="car", value="AA-111-AB")
    ctx.alert_bridge.area_event_added.emit(ev2)
    check("plate: known (one char off)", wait_until(lambda: len(ch1.sent) == n + 2) and ch1.sent[-1][0].plate_known)
    ctx.alert_bridge.area_event_added.emit(AreaEvent(id="ae3", site_id="", channel=1, area_id=gate["id"],
                                                     area_name="Gate", task="state_watch", ts=time.time(),
                                                     kind="changed", value="restored"))
    pump(100)
    check("area: restored ignored", len(ch1.sent) == n + 2)
    # send_test + log
    results = []
    hub.send_test(c2.id, results.append)
    check("send_test: callback", wait_until(lambda: results, 5) and results[0].ok)
    te = ch2.sent[-1][0]
    check("send_test: sample event", te.kind == "test" and te.title == "YardGuard test" and (te.crop_path or te.snapshot_path))
    check("send_test: logged", any(r["event_kind"] == "test" and r["status"] == "sent" for r in rows(hub)))
    # digest after cooldown
    c2.routes["rate"]["cooldown_s"] = 60
    t = {"now": time.time()}
    hub.clock = lambda: t["now"]
    n2 = len(ch2.sent)
    ctx.raise_alert(alert(ctx, channel=2, camera="Driveway", ts=time.time() + 600), gap_s=-1)
    check("digest: first sent", wait_until(lambda: len(ch2.sent) == n2 + 1))
    ctx.raise_alert(alert(ctx, channel=2, camera="Driveway", ts=time.time() + 900), gap_s=-1)
    pump(100)
    check("digest: second held", len(ch2.sent) == n2 + 1 and any(r["status"] == "held" for r in rows(hub)))
    t["now"] += 120
    hub._tick_fast()
    check("digest: combined message", wait_until(lambda: len(ch2.sent) == n2 + 2))
    dg = ch2.sent[-1][0]
    check("digest: wording", dg.extra.get("digest") and dg.title.startswith("1 more alert on 1 camera since"), dg.title)
    # events folder change
    from yardguard.engine import AlertStore
    old_dir = Path(hub.log.dir)
    ctx.alert_store.close()
    ctx.alert_store = AlertStore(str(OUT / "hub_actions" / "events2"))
    hub.events_dir_changed()
    check("events dir: log reopened", Path(hub.log.dir) == OUT / "hub_actions" / "events2"
          and (OUT / "hub_actions" / "events2" / "integrations.db").exists() and old_dir != Path(hub.log.dir))
    # enable / remove
    hub.set_enabled(c2.id, False)
    check("set_enabled off: worker stopped", f"yg-notify-{c2.id}" not in yg_threads() and hub.channel(c2.id) is None)
    hub.set_enabled(c2.id, True)
    check("set_enabled on: running again", wait_until(lambda: f"yg-notify-{c2.id}" in yg_threads()))
    added = hub.add_destination(new_integration("rec", "third"))
    check("add_destination: started", hub.channel(added.id) is not None and len(hub.destinations()) == 3)
    hub.remove_destination(added.id)
    check("remove_destination: gone", hub.channel(added.id) is None and len(hub.destinations()) == 2
          and added.id not in config.CONFIG_PATH.read_text(encoding="utf-8"))
    check("web_base: off by default", hub.web_base() == "")
    ctx.cfg.settings.remote = {"enabled": True, "public_url": "https://pc.tail1234.ts.net/"}
    check("web_base: public url", hub.web_base() == "https://pc.tail1234.ts.net")
    policy.EDIT_THROTTLE_S = old_throttle
    close(ctx, hub)
    r = hub.request(Action("status", {}))
    check("shutdown: request answers closing", r.done() and not r.result()["ok"])
    hub.shutdown()
    check("shutdown: idempotent, no threads", not yg_threads(), str(yg_threads()))


# =====================================================================================================
# 4. FrameProvider
# =====================================================================================================

def test_frames():
    ctx = FakeCtx(str(OUT / "frames"))
    made = []

    def factory(url, fps, long_side, gpu):
        src = FakeSource(url, fps, long_side, gpu)
        made.append((url, fps, long_side, gpu, src))
        return src

    fp = frames_mod.FrameProvider(ctx, source_factory=factory)
    fp.idle_stop_s = 0.3
    ctx.monitor.running = True
    f = fp.latest(1)
    check("frames: monitor path", f is not None and f.shape[:2] == (720, 1280) and not made)
    ctx.monitor._cams[0].source.fresh = False
    check("frames: stale monitor frame -> None, no second stream", fp.latest(1) is None and not made)
    ctx.monitor.running = False
    f = fp.latest(2)
    check("frames: own source opened", f is not None and len(made) == 1)
    url, fps, side, gpu, _src = made[0]
    check("frames: sub stream, 2 fps, 960, CPU", "subtype=1" in url and "channel=2" in url and fps == 2
          and side == 960 and gpu is False)
    check("frames: reused", fp.latest(2) is not None and len(made) == 1 and fp.own_channels() == [2])
    jpg = fp.latest_jpeg(2, max_side=320)
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    check("frames: jpeg", img is not None and max(img.shape[:2]) == 320)
    check("frames: idle stop", wait_until(lambda: not fp.own_channels(), 3) and made[0][4].stopped)
    check("frames: reaper thread ends", wait_until(lambda: "yg-frames" not in yg_threads(), 3))
    check("frames: unknown camera", fp.latest(9) is None)
    check("frames: cameras", fp.cameras() == [(1, "Front gate"), (2, "Driveway")])
    slow = []

    def slow_factory(url, fps, long_side, gpu):
        s = FakeSource(url)
        s.delay_s = 99
        slow.append(s)
        return s

    fp2 = frames_mod.FrameProvider(ctx, source_factory=slow_factory)
    t0 = time.monotonic()
    check("frames: no picture -> None after the wait", fp2.latest(1, wait=0.3) is None and time.monotonic() - t0 < 1.5)
    fp2.close()
    fp.close()
    check("frames: close stops everything", slow[0].stopped and not yg_threads(), str(yg_threads()))
    ctx.close()


# =====================================================================================================
# 5. media + QR
# =====================================================================================================

def test_media():
    big = write_jpeg(OUT / "media" / "big.jpg", (3000, 4000), text="big")
    data = media.prepare_jpeg(big)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    check("media: long side 1280", img.shape[:2] == (960, 1280))
    noisy = np.random.default_rng(1).integers(0, 255, (1500, 2000, 3), dtype=np.uint8)
    p = OUT / "media" / "Двор" / "снимок.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", noisy, [cv2.IMWRITE_JPEG_QUALITY, 95])
    p.write_bytes(buf.tobytes())
    small = media.prepare_jpeg(str(p), max_bytes=60_000)
    check("media: non-ASCII path + byte cap", small is not None and len(small) <= 60_000, str(len(small or b"")))
    check("media: never enlarges", max(cv2.imdecode(np.frombuffer(media.prepare_jpeg(
        write_jpeg(OUT / "media" / "tiny.jpg", (100, 80))), np.uint8), cv2.IMREAD_COLOR).shape[:2]) == 100)
    check("media: missing file -> None", media.prepare_jpeg(str(OUT / "nope.jpg")) is None and media.prepare_jpeg("") is None)
    demo = media.demo_picture(str(OUT / "media"))
    check("media: demo picture", Path(demo).exists() and media.prepare_jpeg(demo) is not None)
    now = time.time()
    e = Event("incident_new", now, "Person <script> & co", severity="critical", camera="Front gate",
              verdict="confirmed", ai_note="hand on door handle", alert_count=3, incident_id="inc-1")
    cap = media.html_caption(e)
    check("media: caption escaped and bold", cap.startswith("<b>Person &lt;script&gt; &amp; co</b>") and "<script>" not in cap)
    check("media: subline", media.subline(e, now) == f"Front gate · {datetime.fromtimestamp(now):%H:%M} · Critical")
    check("media: ai line", media.ai_line(e) == "AI: confirmed (hand on door handle)")
    check("media: alerts line (camera said once)", "\n3 alerts" in media.plain(e)
          and media.plain(e).count("Front gate") == 1)
    e_cam = e.copy(title="Person near cars at night · Front gate")
    check("media: camera not repeated after headline", media.plain(e_cam).count("Front gate") == 1, media.plain(e_cam))
    long_ev = e.copy(text="&<>" * 600)
    cap = media.html_caption(long_ev)
    check("media: caption <= 1024", len(cap) <= 1024, str(len(cap)))
    import re
    tail = cap[-8:]
    check("media: no cut entity", not re.search(r"&[a-z]*$", tail), tail)
    check("media: tiny limit", len(media.html_caption(long_ev, limit=40)) <= 40)
    check("media: clock today", media.clock(now, now) == datetime.fromtimestamp(now).strftime("%H:%M"))
    check("media: clock yesterday", media.clock(now - 86400, now).startswith("Yesterday "))
    check("media: clock weekday", media.clock(now - 3 * 86400, now)[:3] == datetime.fromtimestamp(now - 3 * 86400).strftime("%a"))
    check("media: clock old date", media.clock(now - 30 * 86400, now)[0].isdigit())
    check("media: durations", media.duration_text(45) == "45 s" and media.duration_text(720) == "12 min"
          and media.duration_text(7500) == "2 h 5 min")
    check("media: subject", media.subject(e).startswith("[YardGuard] Critical: Person <script> & co - Front gate "))
    st = e.copy(kind="incident_status", status="false_alarm", status_by="Alex (Telegram)",
                extra={"status_ts": now})
    check("media: false alarm status line", media.status_line(st).startswith("Marked as a false alarm by Alex"))
    test_ev = Event("test", now, "YardGuard test", text="If you can read this, alerts will arrive here.",
                    camera="Front gate")
    check("media: test wording", media.plain(test_ev).splitlines()[1].startswith("Front gate · "))
    # summary text
    ctx = FakeCtx(str(OUT / "media_summary"))
    from yardguard.config import new_area
    cnt = new_area("count", "line", [(0, 0), (1, 1)], "Gate")
    ctx.cfg.active_site().cameras[0].areas.append(cnt)
    s = ctx.alert_store
    sid = ctx.cfg.active_site_id
    make_incident(s, "critical", "confirmed", site_id=sid)
    make_incident(s, "warning", "confirmed", site_id=sid, ts=time.time() + 1)
    _, fa = make_incident(s, "warning", "confirmed", site_id=sid, ts=time.time() + 2)
    s.mark_incident(fa.id, "false_alarm")
    for i in range(3):
        s.add_area_event(AreaEvent(id=f"c{i}", site_id=sid, channel=1, area_id=cnt["id"], area_name="Gate",
                                   task="count", ts=time.time(), kind="cross_in", label="car"))
    ev = media.build_summary(s, ctx.cfg, since=time.time() - 86400, offline_cameras=[])
    check("summary: text", ev.text.startswith("Last 24 hours: 2 incidents (1 critical), 1 false alarm, 3 cars counted "
                                              "at Gate, all cameras online."), ev.text)
    check("summary: stats", ev.extra["incidents"] == 2 and ev.extra["critical"] == 1 and ev.extra["false_alarms"] == 1)
    check("summary: picture", bool(ev.crop_path))
    empty = media.build_summary(None, None, since=time.time() - 86400)
    check("summary: empty", "no incidents" in empty.text)
    ctx.close()
    # QR
    m = qr.matrix("https://pc.tail1234.ts.net/#pair=K7P29QXM")
    check("qr: matrix", m is not None and len(m) == len(m[0]) >= 21)
    img = qr.to_qimage("https://example.com", scale=4)
    check("qr: qimage", img is not None and not img.isNull() and img.width() == (len(qr.matrix("https://example.com")) + 4) * 4)
    svg = qr.to_svg("https://example.com")
    check("qr: svg", svg is not None and "<svg" in svg)
    saved = qr.AVAILABLE
    qr.AVAILABLE = False
    try:
        check("qr: graceful without segno", qr.matrix("x") is None and qr.to_qimage("x") is None and qr.to_svg("x") is None)
    finally:
        qr.AVAILABLE = saved


def test_store():
    lg = DeliveryLog(str(OUT / "store"))
    ev = Event("incident_new", time.time(), "Person", incident_id="inc-1", crop_path="C:/x/crop.jpg")
    d = Delivery("dl-1", "tg-1", ev, Decision("send", "because"), time.time(), edit=False, pictures="both")
    lg.add(d, dest_kind="telegram", dest_name="Telegram")
    row = lg.get("dl-1")
    check("store: add/get", row["status"] == "queued" and row["reason"] == "because" and row["event"]["crop_path"])
    back = lg.delivery_from_row(row)
    check("store: delivery from row", back.event.crop_path == ev.crop_path and back.pictures == "both"
          and back.event.incident_id == "inc-1")
    lg.update("dl-1", status="failed", reason="Server busy")
    check("store: problems filter", len(lg.list(problems_only=True)) == 1 and len(lg.list(dest_id="other")) == 0)
    check("store: stats", lg.stats("tg-1")["last_error"] == "Server busy")
    lg.set_thread("tg-1", "inc-1", {"messages": [{"id": 5}]}, "warning")
    check("store: thread", lg.thread("tg-1", "inc-1")["ref"] == {"messages": [{"id": 5}]}
          and len(lg.threads_for("inc-1")) == 1)
    lg.kv_set("k", "v")
    check("store: kv", lg.kv_get("k") == "v" and lg.kv_get("missing", "d") == "d")
    sk = Delivery("dl-2", "tg-1", ev, Decision("skip", "x"), time.time() - 3 * 86400)
    lg.add(sk, status="skipped")
    lg.cleanup()
    check("store: cleanup drops old skipped rows", lg.get("dl-2") is None and lg.get("dl-1") is not None)
    js = ev.to_json()
    check("event: to_json hides paths", "crop_path" not in js and js["has_crop"] is True and js["has_clip"] is False)
    check("event: from_json round trip", Event.from_json(ev.to_json(include_paths=True)) == ev)
    check("event: image_path fallbacks", ev.image_path("snapshot") == ev.crop_path and ev.image_path("none") == "")
    lg.clear()
    check("store: clear", lg.list() == [])
    lg.close()
    lg.add(d)   # after close: quiet no-op
    check("store: calls after close are harmless", lg.list() == [] and lg.get("dl-1") is None)


def test_worker_merge_into_waiting_first():
    """An update arriving while the first message of its incident waits for a retry is merged into it
    (one message with the newer state), not sent as a second message."""
    old_backoff = list(hub_mod.BACKOFF)
    hub_mod.BACKOFF[:] = [0.3] * 5

    class Log:
        def __init__(self):
            self.rows = {}

        def update(self, did, **kw):
            self.rows.setdefault(did, {}).update(kw)

        def thread(self, *a):
            return None

        def set_thread(self, *a):
            pass

    class FakeHub:
        log = Log()
        _result = SimpleNamespace(emit=lambda *a: None)
        clock = staticmethod(time.time)

        def problem_text(self, d):
            return ""

    class Ch:
        def __init__(self):
            self.calls = []

        def send(self, ev, ctx):
            self.calls.append((ev.kind, ctx.edit, ev.verdict))
            if len(self.calls) == 1:
                return SendResult(False, error="net", retry=True)
            return SendResult(True, ref={"messages": [{"chat": 1, "id": len(self.calls)}]})

        def scrub(self, t):
            return t

        def secret_values(self):
            return ()

    fh, ch = FakeHub(), Ch()
    w = hub_mod._Worker(fh, "tg", ch)
    w.start()
    try:
        ev = Event("incident_new", time.time(), "x", incident_id="m1", severity="warning", verdict="pending")
        w.put(Delivery("d1", "tg", ev, Decision("send"), time.time()))
        wait_until(lambda: len(ch.calls) == 1, 2)
        w.put(Delivery("d2", "tg", ev.copy(kind="incident_update", verdict="confirmed"), Decision("edit"),
                       time.time(), edit=True))
        wait_until(lambda: len(ch.calls) >= 2, 3)
        time.sleep(0.4)
        check("merge: one message for the incident", [c[:2] for c in ch.calls] == [("incident_new", False)] * 2,
              str(ch.calls))
        check("merge: the retry carries the newer state", ch.calls[-1][2] == "confirmed", str(ch.calls))
        check("merge: update row skipped as merged", "Merged" in (fh.log.rows.get("d2", {}).get("reason") or ""))
        w.put(Delivery("d3", "tg", ev.copy(kind="incident_update", verdict="confirmed", ai_note="y"),
                       Decision("edit"), time.time(), edit=True))
        wait_until(lambda: len(ch.calls) >= 3, 8)
        check("merge: later updates edit the message", ch.calls[-1][:2] == ("incident_update", True), str(ch.calls))
    finally:
        w.stop()
        w.join(2)
        hub_mod.BACKOFF[:] = old_backoff


def main() -> int:
    tests = [test_config, test_scrub_and_http, test_policy, test_store, test_media, test_frames, test_hub_basic,
             test_hub_ai_wait, test_hub_retry_expiry_drop_problem, test_hub_restart_requeue,
             test_hub_offline_health_summary, test_hub_actions_and_misc, test_worker_merge_into_waiting_first]
    t_all = time.monotonic()
    for fn in tests:
        t0 = time.monotonic()
        try:
            fn()
        except Exception:
            FAILED.append(f"{fn.__name__} crashed")
            traceback.print_exc()
        print(f"{fn.__name__}: {time.monotonic() - t0:.1f} s")
    pump(100)
    check("end: no yg threads left", not yg_threads(), str(yg_threads()))
    check("end: no token in the log output", TOKEN not in LOG_BUF.getvalue())
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed in {time.monotonic() - t_all:.1f} s")
    for name in PASSED:
        print("PASS", name)
    for name in FAILED:
        print("FAIL", name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
