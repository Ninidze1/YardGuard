"""Home Assistant over MQTT: YardGuard's cameras, alerts and a monitoring switch appear in Home Assistant.

Uses the optional pure-Python package paho-mqtt (>= 2). When it is missing the channel still loads:
check() answers "Needs the MQTT add-on" and install_addon() installs it on the user's click.

Topics (b = params.base_topic, n = Settings.install_id), all under b/n/:
  availability (online/offline, retained, LWT), monitoring/state + monitoring/set,
  cam/<ch>/online|person|vehicle|person_count (retained), cam/<ch>/alert (event entity, not retained),
  cam/<ch>/image (last alert picture), cam/<ch>/snapshot/press, events, incident (+ incident/set),
  area/<area_id>/event, area/<area_id>/plate.
Discovery: one device message <prefix>/device/yardguard_<n>/config (Home Assistant 2024.11+).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import random
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import APP_VERSION, ROOT
from .base import Channel, IntegrationConf, scrub
from .model import Action, CheckResult, Event, SendContext, SendResult

log = logging.getLogger("yardguard")

AVAILABLE = importlib.util.find_spec("paho") is not None
ADDON_MISSING = "Needs the MQTT add-on"
VEHICLES = ("car", "truck", "bus", "motorcycle")
EVENT_TYPES = ["critical", "warning", "info"]
INCIDENT_STATUSES = ("open", "handled", "false_alarm")
PUBLISH_TIMEOUT = 10.0
MAX_IMAGE_BYTES = 2_000_000       # retained pictures stay small: Home Assistant keeps them in memory
MAX_COMMAND_BYTES = 4096
SNAPSHOT_MIN_GAP_S = 2.0
STOP_TIMEOUT = 2.5
_SAFE_TOPIC = re.compile(r"[^A-Za-z0-9_\-/]")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def is_available() -> bool:
    """paho-mqtt importable now (re-checked after install_addon)."""
    global AVAILABLE
    importlib.invalidate_caches()
    AVAILABLE = importlib.util.find_spec("paho") is not None
    return AVAILABLE


def _mqtt():
    """The paho.mqtt.client module, or None when the add-on is missing."""
    try:
        return importlib.import_module("paho.mqtt.client")
    except Exception:   # ImportError, or a broken install
        return None


def install_addon(timeout: float = 600) -> tuple[bool, str]:
    """Install paho-mqtt into YardGuard's own Python (blocking; the UI runs it with run_async on the
    user's click). Returns (ok, plain-words message)."""
    runner = ROOT / "run.cmd"
    cmd = ([str(runner)] if runner.exists() and sys.platform == "win32" else [sys.executable])
    cmd += ["-m", "pip", "install", "--disable-pip-version-check", "paho-mqtt>=2,<3"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                              cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return False, "Installing the add-on took too long. Check the internet connection and try again."
    except OSError as e:
        return False, scrub(f"Could not start the installer: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        log.warning("MQTT add-on install failed: %s", scrub(tail[0]))
        return False, "The add-on could not be installed. Check the internet connection and try again."
    if not is_available():
        return False, "The add-on was installed but cannot be loaded. Restart YardGuard and try again."
    return True, "The MQTT add-on is installed."


# --- Topics and discovery (pure helpers, no network) --------------------------------------------

def clean_topic(text: str, default: str) -> str:
    """A topic prefix without wildcards, spaces or leading/trailing slashes."""
    t = _SAFE_TOPIC.sub("_", str(text or "").strip()).strip("/")
    t = re.sub(r"/{2,}", "/", t)
    return t or default


def clean_node(text: str, fallback: str = "") -> str:
    n = re.sub(r"[^a-z0-9]", "", str(text or "").lower())[:16]
    return n or re.sub(r"[^a-z0-9]", "", str(fallback or "").lower())[-8:] or "yardguard"


def parse_host(text: str) -> tuple[str, int | None]:
    """'mqtt://homeassistant.local:1883/' -> ('homeassistant.local', 1883). Port None when not given."""
    t = str(text or "").strip()
    t = re.sub(r"^[a-z]+://", "", t, flags=re.I).split("/")[0]
    if "@" in t:            # user:pass@host typed into the host field: never keep credentials here
        t = t.rsplit("@", 1)[1]
    if t.startswith("[") and "]" in t:      # [ipv6]:port
        host, _, rest = t[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif t.count(":") == 1:
        host, _, port = t.partition(":")
    else:
        host, port = t, ""
    try:
        p = int(port) if port else None
    except ValueError:
        p = None
    return host.strip(), p


class Topics:
    """Every topic of one YardGuard install under one base topic."""

    def __init__(self, base_topic: str, node: str, prefix: str = "homeassistant") -> None:
        self.node = node
        self.root = f"{clean_topic(base_topic, 'yardguard')}/{node}"
        self.prefix = clean_topic(prefix, "homeassistant")

    @property
    def availability(self) -> str:
        return f"{self.root}/availability"

    @property
    def monitoring_state(self) -> str:
        return f"{self.root}/monitoring/state"

    @property
    def monitoring_set(self) -> str:
        return f"{self.root}/monitoring/set"

    @property
    def events(self) -> str:
        return f"{self.root}/events"

    @property
    def incident(self) -> str:
        return f"{self.root}/incident"

    @property
    def incident_set(self) -> str:
        return f"{self.root}/incident/set"

    @property
    def discovery(self) -> str:
        return f"{self.prefix}/device/yardguard_{self.node}/config"

    @property
    def ha_status(self) -> str:
        return f"{self.prefix}/status"

    @property
    def snapshot_press_filter(self) -> str:
        return f"{self.root}/cam/+/snapshot/press"

    def cam(self, channel: int, what: str) -> str:
        return f"{self.root}/cam/{int(channel)}/{what}"

    def area(self, area_id: str, what: str) -> str:
        return f"{self.root}/area/{clean_topic(area_id, 'area')}/{what}"

    def subscriptions(self, control: bool) -> list[str]:
        subs = [self.snapshot_press_filter, self.ha_status]
        if control:
            subs += [self.monitoring_set, self.incident_set]
        return subs


def topic_list(params: dict, node: str) -> list[str]:
    """Readable topic overview for the setup dialog (camera 1 as the example)."""
    t = Topics(params.get("base_topic", "yardguard"), node, params.get("discovery_prefix", "homeassistant"))
    return [t.availability, t.monitoring_state, t.monitoring_set, t.cam(1, "online"), t.cam(1, "person"),
            t.cam(1, "vehicle"), t.cam(1, "person_count"), t.cam(1, "alert"), t.cam(1, "image"),
            t.cam(1, "snapshot/press"), t.events, t.incident, t.incident_set, f"{t.root}/area/<area>/plate"]


def components(node: str, topics: Topics, cameras: list[tuple[int, str]],
               plate_areas: list[tuple[str, str]], control: bool = True) -> dict[str, dict]:
    """Home Assistant device-discovery components: {component id: config}."""
    uid = f"yardguard_{node}"
    cmps: dict[str, dict] = {}
    if control:
        cmps["monitoring"] = {"p": "switch", "name": "Monitoring", "unique_id": f"{uid}_monitoring",
                              "state_topic": topics.monitoring_state, "command_topic": topics.monitoring_set,
                              "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:cctv"}
    else:
        # own id: a platform change must look like "remove the switch, add a sensor" to Home Assistant
        cmps["monitoring_running"] = {"p": "binary_sensor", "name": "Monitoring",
                                      "unique_id": f"{uid}_monitoring_running",
                              "state_topic": topics.monitoring_state, "payload_on": "ON", "payload_off": "OFF",
                              "device_class": "running", "icon": "mdi:cctv"}
    cmps["last_incident"] = {"p": "sensor", "name": "Last incident", "unique_id": f"{uid}_last_incident",
                             "state_topic": topics.incident, "value_template": "{{ value_json.title[:250] }}",
                             "json_attributes_topic": topics.incident, "icon": "mdi:shield-alert-outline"}
    for ch, label in cameras:
        c = f"cam{int(ch)}"
        cmps[f"{c}_online"] = {"p": "binary_sensor", "name": f"{label} online", "unique_id": f"{uid}_{c}_online",
                               "state_topic": topics.cam(ch, "online"), "payload_on": "ON", "payload_off": "OFF",
                               "device_class": "connectivity", "entity_category": "diagnostic"}
        cmps[f"{c}_person"] = {"p": "binary_sensor", "name": f"{label} person", "unique_id": f"{uid}_{c}_person",
                               "state_topic": topics.cam(ch, "person"), "payload_on": "ON", "payload_off": "OFF",
                               "device_class": "occupancy", "icon": "mdi:walk"}
        cmps[f"{c}_vehicle"] = {"p": "binary_sensor", "name": f"{label} vehicle", "unique_id": f"{uid}_{c}_vehicle",
                                "state_topic": topics.cam(ch, "vehicle"), "payload_on": "ON", "payload_off": "OFF",
                                "device_class": "occupancy", "icon": "mdi:car"}
        cmps[f"{c}_person_count"] = {"p": "sensor", "name": f"{label} people", "unique_id": f"{uid}_{c}_person_count",
                                     "state_topic": topics.cam(ch, "person_count"), "state_class": "measurement",
                                     "icon": "mdi:account-multiple"}
        cmps[f"{c}_image"] = {"p": "image", "name": f"{label} last alert", "unique_id": f"{uid}_{c}_image",
                              "image_topic": topics.cam(ch, "image"), "content_type": "image/jpeg"}
        cmps[f"{c}_alert"] = {"p": "event", "name": f"{label} alert", "unique_id": f"{uid}_{c}_alert",
                              "state_topic": topics.cam(ch, "alert"), "event_types": list(EVENT_TYPES),
                              "icon": "mdi:alarm-light-outline"}
        cmps[f"{c}_snapshot"] = {"p": "button", "name": f"{label} snapshot", "unique_id": f"{uid}_{c}_snapshot",
                                 "command_topic": topics.cam(ch, "snapshot/press"), "payload_press": "PRESS",
                                 "icon": "mdi:camera"}
    for area_id, name in plate_areas:
        a = f"area{clean_topic(area_id, 'area')}"
        cmps[f"{a}_last_plate"] = {"p": "sensor", "name": f"{name} last plate", "unique_id": f"{uid}_{a}_last_plate",
                                   "state_topic": topics.area(area_id, "plate"), "icon": "mdi:card-text-outline"}
    return cmps


def discovery_payload(node: str, topics: Topics, cmps: dict[str, dict],
                      gone: dict[str, str] | None = None) -> dict:
    """The retained device-discovery message. `gone`: {component id: platform} of components that
    existed before; they are sent with only their platform so Home Assistant removes them."""
    all_cmps = {cid: {"p": p} for cid, p in (gone or {}).items() if cid not in cmps}
    all_cmps.update(cmps)
    return {"dev": {"ids": [f"yardguard_{node}"], "name": "YardGuard", "mf": "YardGuard", "mdl": "YardGuard",
                    "sw": APP_VERSION},
            "o": {"name": "YardGuard", "sw": APP_VERSION},
            "avty_t": topics.availability, "qos": 1, "cmps": all_cmps}


def entity_list(cameras: list[tuple[int, str]], plate_areas: list[tuple[str, str]], control: bool = True) -> list[str]:
    """Plain-words list of what will appear in Home Assistant (setup dialog)."""
    out = ["Monitoring switch" if control else "Monitoring (on/off)", "Last incident"]
    for _ch, label in cameras:
        out.append(f"{label}: online, person, vehicle, people count, last alert picture, alert, snapshot button")
    for _id, name in plate_areas:
        out.append(f"{name}: last number plate")
    return out


# --- Payload builders -----------------------------------------------------------------------------

def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def event_payload(e: Event, web_base: str = "") -> dict:
    """Same shape as the webhook body (schema yardguard.v1) without the picture."""
    has = lambda p: bool(p) and Path(p).is_file()   # noqa: E731
    cams = [c.strip() for c in (e.camera or "").split(",") if c.strip()]
    return {
        "schema": "yardguard.v1", "event": e.kind, "id": e.id, "ts": e.ts, "time": _iso(e.ts),
        "site": {"id": e.site_id, "name": e.site_name},
        "camera": {"channel": e.channel, "name": e.camera},
        "title": e.title, "text": e.text, "severity": e.severity, "verdict": e.verdict, "ai_note": e.ai_note,
        "incident": ({"id": e.incident_id, "status": e.status or "open", "alert_count": e.alert_count,
                      "cameras": cams} if e.incident_id else None),
        "labels": list(e.labels or []),
        "area": ({"id": e.area_id, "name": e.area_name, "task": e.area_task, "kind": e.area_kind}
                 if e.area_id else None),
        "plate": {"text": e.plate, "known": e.plate_known} if e.plate else None,
        "has_snapshot": has(e.snapshot_path), "has_crop": has(e.crop_path), "has_clip": has(e.clip_path),
        "web_url": (web_base.rstrip("/") + "/" + e.web_path.lstrip("/")) if web_base and e.web_path else None,
    }


def area_event_payload(ev: Any, camera: str = "") -> dict:
    """JSON for b/n/area/<id>/event from an areas.AreaEvent (read through getattr: duck-typed)."""
    kind = str(getattr(ev, "kind", ""))
    try:
        from ..areas import KIND_TEXT
        kind_text = KIND_TEXT.get(kind, kind)
    except Exception:
        kind_text = kind
    g = lambda k, d="": getattr(ev, k, d)   # noqa: E731
    return {"id": g("id"), "ts": g("ts", 0.0), "time": _iso(g("ts", 0.0)), "kind": kind, "kind_text": kind_text,
            "area": {"id": g("area_id"), "name": g("area_name"), "task": g("task")},
            "camera": {"channel": g("channel", None), "name": camera},
            "label": g("label"), "value": g("value"), "site_id": g("site_id"),
            "has_snapshot": bool(g("snapshot_path")), "has_crop": bool(g("crop_path")),
            "has_clip": bool(g("clip_path"))}


def _image_path(e: Event, pictures: str) -> str:
    try:
        p = e.image_path(pictures)
        if p is not None:
            return p
    except Exception:
        pass
    if pictures == "none":
        return ""
    order = [e.snapshot_path, e.crop_path] if pictures == "snapshot" else [e.crop_path, e.snapshot_path]
    return next((p for p in order if p and Path(p).is_file()), "")


def _jpeg(path: str, max_bytes: int = MAX_IMAGE_BYTES) -> bytes | None:
    if not path:
        return None
    try:
        from .media import prepare_jpeg
    except Exception:   # media missing or broken: fall back to the file as it is
        prepare_jpeg = None
    if prepare_jpeg is not None:
        try:
            return prepare_jpeg(path, 1280, 80, max_bytes)
        except Exception:
            log.exception("Could not prepare the MQTT picture")
            return None
    try:   # media not available: send the file as it is when it is small enough
        data = Path(path).read_bytes()
        return data if len(data) <= max_bytes else None
    except OSError:
        return None


def _connack_text(rc: Any, host: str, port: int) -> tuple[str, str]:
    """(message, field) for a refused connection."""
    try:
        value = int(getattr(rc, "value", rc))
    except (TypeError, ValueError):
        value = -1
    if value in (4, 5, 134, 135):
        return "Wrong user name or password.", "password"
    if value in (2, 133):
        return "The broker refused YardGuard's client name.", ""
    if value in (3, 136):
        return f"The broker at {host}:{port} is not ready. Try again in a minute.", "host"
    return scrub(f"The broker at {host}:{port} refused the connection ({rc})."), ""


def _socket_text(e: BaseException, host: str, port: int, tls: bool) -> tuple[str, str]:
    if isinstance(e, socket.gaierror):
        return f"Could not find {host}. Check the broker address.", "host"
    if isinstance(e, ssl.SSLError):
        return ("Secure connection failed. Turn off 'Use TLS' or use the broker's TLS port (often 8883)."
                if tls else "Secure connection failed."), "tls"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"Could not reach the broker at {host}:{port} (no answer).", "host"
    return f"Could not reach the broker at {host}:{port}.", "host"


# --- The channel ------------------------------------------------------------------------------------

class MqttChannel(Channel):
    """Publishes YardGuard's state and alerts to an MQTT broker with Home Assistant discovery, and
    turns Home Assistant commands (monitoring switch, snapshot button, incident status) into actions."""

    KIND = "mqtt"
    CAPS = {"pictures": True, "clip": False, "edit": False, "buttons": False, "two_way": True,
            "max_upload": MAX_IMAGE_BYTES, "updates": "each"}
    SECRET_KEYS = ("password",)

    def __init__(self, conf: IntegrationConf, hub) -> None:
        super().__init__(conf, hub)
        self.conf = conf
        self.hub = hub
        self.status = "off"
        self.status_text = ""
        self._lock = threading.RLock()
        self._client = None
        self._connected = threading.Event()
        self._started = False
        self._gen = 0                              # bumps on every start: late callbacks of an old client are ignored
        self._topics: Topics | None = None
        self._states: dict[str, str] = {}          # retained state topics -> last payload (dedupe + replay)
        self._disc_json = ""                       # last discovery payload published
        self._timers: list[threading.Timer] = []
        self._snap_last: dict[int, float] = {}
        self._snap_busy: set[int] = set()
        self._last_incident = ""                   # newest incident id seen (for the retained incident topic)
        self._monitoring: bool | None = None
        self._fingerprint = ""
        self._applied: dict[str, bool] = {}        # discovery / control as last applied (see reconfigure)

    # -- config helpers ------------------------------------------------------------------------------
    @property
    def _ctx(self):
        return getattr(self.hub, "ctx", None)

    def _settings(self):
        cfg = getattr(self._ctx, "cfg", None)
        return getattr(cfg, "settings", None)

    def _node(self) -> str:
        return clean_node(getattr(self._settings(), "install_id", ""), self.conf.id)

    def _params(self) -> dict:
        return self.conf.params

    def _host_port(self) -> tuple[str, int]:
        p = self._params()
        host, port_in_host = parse_host(p.get("host", ""))
        try:
            port = int(p.get("port") or port_in_host or (8883 if p.get("tls") else 1883))
        except (TypeError, ValueError):
            port = 1883
        return host, port

    def _control(self) -> bool:
        return bool(self._params().get("allow_control", True))

    def _make_topics(self) -> Topics:
        p = self._params()
        return Topics(p.get("base_topic", "yardguard"), self._node(), p.get("discovery_prefix", "homeassistant"))

    def _cameras(self) -> list[tuple[int, str]]:
        frames = getattr(self.hub, "frames", None)
        try:
            if frames is not None:
                return [(int(ch), str(label)) for ch, label in frames.cameras()]
        except Exception:
            log.debug("FrameProvider.cameras failed", exc_info=True)
        cfg = getattr(self._ctx, "cfg", None)
        site = cfg.active_site() if cfg is not None else None
        return [(c.channel, c.label) for c in site.enabled_cameras()] if site else []

    def _plate_areas(self) -> list[tuple[str, str]]:
        cfg = getattr(self._ctx, "cfg", None)
        site = cfg.active_site() if cfg is not None else None
        out = []
        for cam in (site.enabled_cameras() if site else []):
            for a in cam.areas:
                if a.get("task") == "log_plates" and a.get("enabled", True):
                    out.append((str(a.get("id")), str(a.get("name") or "Number plates")))
        return out

    def _camera_label(self, channel) -> str:
        return next((label for ch, label in self._cameras() if ch == channel), f"Camera {channel}")

    def _fp(self) -> str:
        p = self._params()
        host, port = self._host_port()
        pw = self.conf.secret("password")
        # the password itself never leaves memory: only whether it changed matters
        return json.dumps([host, port, bool(p.get("tls")), p.get("username", ""), hash(pw), self._node(),
                           clean_topic(p.get("base_topic", ""), "yardguard"),
                           clean_topic(p.get("discovery_prefix", ""), "homeassistant")])

    def _set_status(self, status: str, text: str = "") -> None:
        """Through Channel.set_status (scrubs, tells the hub) when the base class has it."""
        base_set = getattr(super(), "set_status", None)
        if base_set is not None:
            base_set(status, scrub(text, self.conf.secret("password")) if text else "")
            return
        text = scrub(text, self.conf.secret("password")) if text else ""
        if (status, text) == (self.status, self.status_text):
            return
        self.status, self.status_text = status, text
        sig = getattr(self.hub, "destination_status", None)
        try:
            if sig is not None:
                sig.emit(self.conf.id)
        except Exception:
            log.debug("destination_status emit failed", exc_info=True)

    # -- client construction ------------------------------------------------------------------------
    @staticmethod
    def _new_client(mqtt, client_id: str, params: dict, password: str, will_topic: str = ""):
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                             protocol=mqtt.MQTTv311, clean_session=True)
        user = str(params.get("username") or "")
        if user or password:
            client.username_pw_set(user or None, password or None)
        if params.get("tls"):
            client.tls_set_context(ssl.create_default_context())
        if will_topic:
            client.will_set(will_topic, "offline", qos=1, retain=True)
        client.connect_timeout = 5.0
        client.reconnect_delay_set(1, 60)
        return client

    # -- lifecycle (GUI thread) -------------------------------------------------------------------------
    def start(self) -> None:
        mqtt = _mqtt()
        if mqtt is None:
            self._set_status("problem", "Needs the MQTT add-on. Open the setup to install it.")
            return
        host, port = self._host_port()
        if not host:
            self._set_status("problem", "No broker address. Open the setup and enter it.")
            return
        if self.conf.secret_unreadable("password"):
            self._set_status("problem", "The saved password cannot be read on this Windows account. Enter it again.")
            return
        with self._lock:
            if self._started:
                return
            self._started = True
            self._gen += 1
            gen = self._gen
            self._topics = self._make_topics()
            self._fingerprint = self._fp()
            self._applied = {"discovery": bool(self._params().get("discovery", True)), "control": self._control()}
            self._disc_json = ""
            self._connected.clear()
        ctx = self._ctx
        mon = getattr(ctx, "monitor", None)
        try:
            self._monitoring = bool(getattr(mon, "running", False)) if mon is not None else None
        except Exception:
            self._monitoring = None
        if self._monitoring is not None:
            self._remember(self._topics.monitoring_state, "ON" if self._monitoring else "OFF")
        client = self._new_client(mqtt, f"yardguard-{self._node()}-{uuid.uuid4().hex[:6]}", self._params(),
                                  self.conf.secret("password"), self._topics.availability)
        client.on_connect = lambda c, u, f, rc, pr=None: self._on_connect(gen, c, rc)
        client.on_connect_fail = lambda c, u: self._on_connect_fail(gen)
        client.on_disconnect = lambda c, u, f, rc, pr=None: self._on_disconnect(gen, rc)
        client.on_message = lambda c, u, msg: self._on_message(gen, msg)
        self._client = client
        self._set_status("connecting", "")
        try:
            client.connect_async(host, port, keepalive=30)
            client.loop_start()
            t = getattr(client, "_thread", None)
            if t is not None:   # name it like every YardGuard thread, so leak checks see it
                t.name = f"yg-mqtt-{self.conf.id}"
        except Exception as e:
            log.warning("MQTT start failed: %s", scrub(str(e), self.conf.secret("password")))
            self._set_status("problem", _socket_text(e, host, port, bool(self._params().get("tls")))[0])
        add = getattr(self.hub, "add_listener", None)
        if add is not None:
            try:
                add(self._on_hub_event)
            except Exception:
                log.debug("add_listener failed", exc_info=True)

    def stop(self) -> None:
        """Publishes offline, disconnects and ends the network thread (returns within ~3 s)."""
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._gen += 1
            client, self._client = self._client, None
            timers, self._timers = self._timers, []
        for t in timers:
            t.cancel()
        rm = getattr(self.hub, "remove_listener", None)
        if rm is not None:
            try:
                rm(self._on_hub_event)
            except Exception:
                pass
        if client is not None:
            deadline = time.monotonic() + STOP_TIMEOUT
            if self._connected.is_set() and self._topics is not None:
                try:
                    info = client.publish(self._topics.availability, "offline", qos=1, retain=True)
                    info.wait_for_publish(1.0)
                except Exception:
                    pass
            try:
                client.disconnect()
            except Exception:
                pass
            # loop_stop joins the network thread without a timeout; a connect attempt to an unreachable
            # host can hold it for the connect timeout, so join from a helper with our own deadline.
            stopper = threading.Thread(target=self._loop_stop, args=(client,), daemon=True,
                                       name=f"yg-mqtt-stop-{self.conf.id}")
            stopper.start()
            stopper.join(max(0.1, deadline - time.monotonic()))
        self._connected.clear()
        self._set_status("off", "")

    @staticmethod
    def _loop_stop(client) -> None:
        try:
            client.loop_stop()
        except Exception:
            pass

    def reconfigure(self, conf: IntegrationConf) -> None:
        """Connection changes reconnect; camera/area/discovery changes only re-publish discovery."""
        # The UI edits conf.params in place before calling this, so "before" comes from what was applied.
        old_topics, was_started = self._topics, self._started
        old_disc, old_control = self._applied.get("discovery", True), self._applied.get("control", True)
        self.conf = conf
        if not was_started:
            return
        self._applied = {"discovery": bool(self._params().get("discovery", True)), "control": self._control()}
        if self._fp() != self._fingerprint:
            if (old_topics is not None and old_disc and self._connected.is_set()
                    and old_topics.discovery != self._make_topics().discovery):
                self._clear_discovery(old_topics)      # the device moves to a new prefix: remove the old one
            self.stop()
            self.start()
            return
        if old_control != self._control():
            self._resubscribe(old_control)
        if old_disc and not self._params().get("discovery", True):
            self._clear_discovery(self._topics)
        self.refresh_discovery(force=True)

    def _clear_discovery(self, topics: Topics) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.publish(topics.discovery, b"", qos=1, retain=True).wait_for_publish(2.0)
        except Exception:
            pass
        self._disc_json = ""

    def _resubscribe(self, old_control: bool) -> None:
        client, t = self._client, self._topics
        if client is None or t is None or not self._connected.is_set():
            return
        try:
            if old_control and not self._control():
                client.unsubscribe([t.monitoring_set, t.incident_set])
            elif self._control():
                client.subscribe([(t.monitoring_set, 1), (t.incident_set, 1)])
        except Exception:
            log.debug("MQTT resubscribe failed", exc_info=True)

    # -- paho callbacks (network thread) --------------------------------------------------------------
    def _on_connect(self, gen: int, client, rc) -> None:
        if gen != self._gen:
            return
        failure = getattr(rc, "is_failure", None)
        if failure if failure is not None else bool(rc):
            host, port = self._host_port()
            self._connected.clear()
            self._set_status("problem", _connack_text(rc, host, port)[0])
            return
        self._connected.set()
        t = self._topics
        try:
            client.publish(t.availability, "online", qos=1, retain=True)
            client.subscribe([(s, 1) for s in t.subscriptions(self._control())])
        except Exception:
            log.debug("MQTT subscribe failed", exc_info=True)
        self._set_status("ok", "")
        self.refresh_discovery(force=True)
        self._replay_states()

    def _on_connect_fail(self, gen: int) -> None:
        if gen != self._gen:
            return
        host, port = self._host_port()
        self._connected.clear()
        try:   # paho does not hand over the exception: resolve once to tell "not found" from "not reachable"
            socket.getaddrinfo(host, port)
            text = f"Could not reach the broker at {host}:{port}. Trying again."
        except OSError:
            text = f"Could not find {host}. Check the broker address."
        self._set_status("problem", text)

    def _on_disconnect(self, gen: int, rc) -> None:
        if gen != self._gen:
            return
        was = self._connected.is_set()
        self._connected.clear()
        if was:
            host, port = self._host_port()
            self._set_status("connecting", f"Lost the connection to {host}:{port}. Trying again.")

    def _on_message(self, gen: int, msg) -> None:
        if gen != self._gen:
            return
        try:
            self._handle_message(msg.topic, bytes(msg.payload or b"")[:MAX_COMMAND_BYTES + 1])
        except Exception:
            log.exception("MQTT command failed")

    def _handle_message(self, topic: str, payload: bytes) -> None:
        t = self._topics
        if t is None or len(payload) > MAX_COMMAND_BYTES:
            return
        text = payload.decode("utf-8", "replace").strip()
        if topic == t.ha_status:
            if text.lower() == "online":
                self._schedule(random.uniform(1.0, 5.0), self._ha_online)
            return
        if topic == t.monitoring_set:
            if not self._control():
                return
            on = text.upper() in ("ON", "TRUE", "1")
            if not on and text.upper() not in ("OFF", "FALSE", "0"):
                return
            self._request(Action("set_monitoring", {"on": on}, "Home Assistant"), self._after_monitoring)
            return
        if topic == t.incident_set:
            if not self._control():
                return
            try:
                data = json.loads(text)
            except ValueError:
                return
            if not isinstance(data, dict):
                return
            inc, status = str(data.get("id", "")), str(data.get("status", ""))
            if _SAFE_ID.match(inc) and status in INCIDENT_STATUSES:
                self._request(Action("mark_incident", {"incident_id": inc, "status": status}, "Home Assistant"),
                              None)
            return
        m = re.fullmatch(re.escape(t.root) + r"/cam/(\d{1,4})/snapshot/press", topic)
        if m:
            self._snapshot(int(m.group(1)))

    def _request(self, action: Action, then: Callable[[dict], None] | None) -> None:
        req = getattr(self.hub, "request", None)
        if req is None:
            return
        try:
            fut = req(action)
        except Exception:
            log.exception("MQTT action %s failed", action.kind)
            return
        if then is not None:
            def done(f):
                try:
                    res = f.result()
                except Exception:
                    res = {"ok": False}
                try:
                    then(res if isinstance(res, dict) else {"ok": False})
                except Exception:
                    log.exception("MQTT action result failed")
            fut.add_done_callback(done)

    def _after_monitoring(self, res: dict) -> None:
        if not res.get("ok") and self._topics is not None:
            # refused: put Home Assistant's switch back to the real state
            if self._monitoring is not None:
                self._publish_state(self._topics.monitoring_state, "ON" if self._monitoring else "OFF", force=True)

    def _snapshot(self, channel: int) -> None:
        if channel not in {ch for ch, _ in self._cameras()}:
            return
        now = time.monotonic()
        with self._lock:
            if channel in self._snap_busy or now - self._snap_last.get(channel, -1e9) < SNAPSHOT_MIN_GAP_S:
                return
            self._snap_busy.add(channel)
            self._snap_last[channel] = now

        def publish(res: dict) -> None:
            with self._lock:
                self._snap_busy.discard(channel)
            jpeg = res.get("jpeg") if res.get("ok") else None
            client, t = self._client, self._topics
            if not jpeg or client is None or t is None or not self._connected.is_set():
                return
            try:
                client.publish(t.cam(channel, "image"), bytes(jpeg), qos=0,
                               retain=bool(self._params().get("retain_images", True)))
            except Exception:
                log.debug("MQTT snapshot publish failed", exc_info=True)

        if getattr(self.hub, "request", None) is None:
            with self._lock:
                self._snap_busy.discard(channel)
            return
        self._request(Action("snapshot", {"channel": channel, "max_side": 1280}, "Home Assistant"), publish)

    def _schedule(self, delay: float, fn: Callable[[], None]) -> None:
        with self._lock:
            if not self._started:
                return
            self._timers = [t for t in self._timers if t.is_alive()]
            timer = threading.Timer(delay, fn)
            timer.daemon = True
            timer.name = f"yg-mqtt-ha-{self.conf.id}"
            self._timers.append(timer)
        timer.start()

    def _ha_online(self) -> None:
        """Home Assistant restarted: send discovery and states again."""
        if not self._started:
            return
        self.refresh_discovery(force=True)
        self._replay_states()

    # -- discovery and states --------------------------------------------------------------------------
    def _kv(self):
        return getattr(self.hub, "log", None)

    def _load_known(self) -> dict:
        kv = self._kv()
        try:
            data = json.loads(kv.kv_get(f"mqtt_cmps_{self.conf.id}", "") or "{}") if kv is not None else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_known(self, data: dict) -> None:
        kv = self._kv()
        try:
            if kv is not None:
                kv.kv_set(f"mqtt_cmps_{self.conf.id}", json.dumps(data))
        except Exception:
            log.debug("MQTT kv save failed", exc_info=True)

    def _discovery_json(self, t: Topics) -> str:
        """Current discovery message. Components that existed before but not any more stay in it with
        only their platform (kept, up to 100), so Home Assistant removes them even if it was offline
        when they went away."""
        with self._lock:
            cmps = components(t.node, t, self._cameras(), self._plate_areas(), self._control())
            known = self._load_known()
            prev = {k: v for k, v in (known.get("cmps") or {}).items() if isinstance(v, str)}
            gone = {k: v for k, v in (known.get("gone") or {}).items() if isinstance(v, str)}
            for cid, plat in prev.items():
                if cid not in cmps:
                    gone[cid] = plat          # removed camera / area, or monitoring switch <-> sensor
            for cid in cmps:
                gone.pop(cid, None)
            gone = dict(list(gone.items())[-100:])
            self._save_known({"cmps": {k: v["p"] for k, v in cmps.items()}, "gone": gone})
            return json.dumps(discovery_payload(t.node, t, cmps, gone), ensure_ascii=False)

    def refresh_discovery(self, force: bool = False) -> bool:
        """Publish the device discovery message when it changed (or always with force). Any thread."""
        client, t = self._client, self._topics
        if client is None or t is None or not self._connected.is_set():
            return False
        if not self._params().get("discovery", True):
            return False
        with self._lock:
            payload = self._discovery_json(t)
            if payload == self._disc_json and not force:
                return False
            self._disc_json = payload
        try:
            client.publish(t.discovery, payload.encode("utf-8"), qos=1, retain=True)
        except Exception:
            log.debug("MQTT discovery publish failed", exc_info=True)
            return False
        return True

    def _remember(self, topic: str, payload: str) -> bool:
        """Store a retained state; False when it did not change."""
        with self._lock:
            if self._states.get(topic) == payload:
                return False
            self._states[topic] = payload
            return True

    def _publish_state(self, topic: str, payload: str, force: bool = False) -> None:
        changed = self._remember(topic, payload)
        if not (changed or force):
            return
        client = self._client
        if client is None or not self._connected.is_set():
            return       # replayed on (re)connect
        try:
            client.publish(topic, payload, qos=1, retain=True)
        except Exception:
            log.debug("MQTT state publish failed", exc_info=True)

    def _replay_states(self) -> None:
        client = self._client
        if client is None:
            return
        with self._lock:
            states = list(self._states.items())
        for topic, payload in states:
            try:
                client.publish(topic, payload, qos=1, retain=True)
            except Exception:
                break

    # -- hub feeds (GUI thread, must be quick) --------------------------------------------------------
    def _on_hub_event(self, kind: str, payload: Any) -> None:
        try:
            if kind == "status":
                self.publish_status(payload)
            elif kind == "monitoring":
                self.publish_monitoring(bool(payload))
            elif kind == "area_event":
                self.publish_area_event(payload)
            elif kind == "config":
                self.refresh_discovery()
        except Exception:
            log.exception("MQTT feed %s failed", kind)

    def publish_monitoring(self, running: bool) -> None:
        t = self._topics
        if t is None:
            return
        self._monitoring = running
        self._publish_state(t.monitoring_state, "ON" if running else "OFF")
        if not running:   # nothing is watched any more: occupancy would be stale
            for ch, _ in self._cameras():
                self._publish_state(t.cam(ch, "person"), "OFF")
                self._publish_state(t.cam(ch, "vehicle"), "OFF")
                self._publish_state(t.cam(ch, "person_count"), "0")

    def publish_status(self, status: Any) -> None:
        """Monitor.camera_status() dict -> online / person / vehicle / person_count per camera."""
        t = self._topics
        if t is None or not isinstance(status, dict):
            return
        for ch, st in status.items():
            try:
                ch = int(ch)
            except (TypeError, ValueError):
                continue
            if not isinstance(st, dict):
                continue
            state = st.get("state", "")
            if state in ("watching", "idle"):
                self._publish_state(t.cam(ch, "online"), "ON")
            elif state == "offline":
                self._publish_state(t.cam(ch, "online"), "OFF")
            objects = st.get("objects") or {}
            if not isinstance(objects, dict):
                objects = {}
            people = int(objects.get("person", 0) or 0)
            vehicles = sum(int(objects.get(v, 0) or 0) for v in VEHICLES)
            self._publish_state(t.cam(ch, "person"), "ON" if people > 0 else "OFF")
            self._publish_state(t.cam(ch, "vehicle"), "ON" if vehicles > 0 else "OFF")
            self._publish_state(t.cam(ch, "person_count"), str(people))

    def publish_area_event(self, ev: Any) -> None:
        t, client = self._topics, self._client
        area_id = str(getattr(ev, "area_id", "") or "")
        if t is None or not area_id:
            return
        if getattr(ev, "kind", "") == "plate" and getattr(ev, "value", ""):
            self._publish_state(t.area(area_id, "plate"), str(ev.value)[:64])
        if client is None or not self._connected.is_set():
            return
        body = area_event_payload(ev, self._camera_label(getattr(ev, "channel", None)))
        try:
            client.publish(t.area(area_id, "event"), json.dumps(body, ensure_ascii=False).encode("utf-8"), qos=0)
        except Exception:
            log.debug("MQTT area event publish failed", exc_info=True)

    # -- delivery (worker thread) ---------------------------------------------------------------------
    def _messages(self, event: Event, pictures: str, web_base: str, t: Topics,
                  retain_images: bool) -> tuple[list[tuple[str, bytes, int, bool]], str]:
        """(topic, payload, qos, retain) to publish for one event, plus a note."""
        body = event_payload(event, web_base)
        msgs: list[tuple[str, bytes, int, bool]] = []
        note = ""
        enc = lambda d: json.dumps(d, ensure_ascii=False).encode("utf-8")   # noqa: E731
        if event.kind in ("incident_new", "incident_update", "incident_status"):
            kind = {"incident_new": "new", "incident_update": "update", "incident_status": "status"}[event.kind]
            msgs.append((t.events, enc({"type": kind, "incident": body}), 1, False))
            with self._lock:
                newest = (event.kind == "incident_new" or not self._last_incident
                          or self._last_incident == event.incident_id)
                if event.kind == "incident_new" or not self._last_incident:
                    self._last_incident = event.incident_id
            if newest:
                msgs.append((t.incident, enc(body), 1, True))
            if event.channel is not None and (event.kind == "incident_new" or event.escalated):
                alert = {"event_type": event.severity if event.severity in EVENT_TYPES else "info",
                         "title": event.title, "incident_id": event.incident_id, "verdict": event.verdict,
                         "camera": event.camera}
                msgs.append((t.cam(event.channel, "alert"), enc(alert), 1, False))
            if event.channel is not None and event.kind != "incident_status" and pictures != "none":
                jpeg = _jpeg(_image_path(event, pictures))
                if jpeg:
                    msgs.append((t.cam(event.channel, "image"), jpeg, 1, retain_images))
                elif event.crop_path or event.snapshot_path:
                    note = "picture could not be read, sent without it"
        else:
            msgs.append((t.events, enc({"type": event.kind, "event": body}), 1, False))
        return msgs, note

    @staticmethod
    def _publish_all(client, msgs: list[tuple[str, bytes, int, bool]], timeout: float) -> str:
        """Publish and wait for the broker's acknowledgements. Returns "" or an error text."""
        mqtt = _mqtt()
        ok_rc = getattr(mqtt, "MQTT_ERR_SUCCESS", 0) if mqtt else 0
        infos = []
        for topic, payload, qos, retain in msgs:
            info = client.publish(topic, payload, qos=qos, retain=retain)
            if info.rc != ok_rc:
                return "Not connected to the broker."
            infos.append(info)
        deadline = time.monotonic() + timeout
        for info in infos:
            try:
                info.wait_for_publish(max(0.05, deadline - time.monotonic()))
            except (RuntimeError, ValueError):
                return "Not connected to the broker."
            if not info.is_published():
                return f"The broker did not confirm the message within {int(timeout)} s."
        return ""

    def send(self, event: Event, ctx: SendContext) -> SendResult:
        t0 = time.monotonic()
        try:
            if ctx.edit:
                return SendResult(ok=True, note="editing is not supported")
            client, t = self._client, self._topics
            host, port = self._host_port()
            if _mqtt() is None:
                return SendResult(ok=False, error="Needs the MQTT add-on.", permanent=True)
            if client is None or t is None or not self._connected.is_set():
                text = self.status_text if self.status == "problem" and self.status_text else \
                    f"Not connected to the broker at {host}:{port}."
                return SendResult(ok=False, error=text, retry=True)
            msgs, note = self._messages(event, ctx.pictures, ctx.web_base, t,
                                        bool(self._params().get("retain_images", True)))
            err = self._publish_all(client, msgs, PUBLISH_TIMEOUT)
            ms = int((time.monotonic() - t0) * 1000)
            if err:
                return SendResult(ok=False, error=err, retry=True, ms=ms)
            return SendResult(ok=True, ms=ms, note=note)
        except Exception as e:
            log.warning("MQTT send failed: %s", scrub(repr(e), self.conf.secret("password")))
            return SendResult(ok=False, error=scrub(f"Could not publish: {e}", self.conf.secret("password")),
                              retry=True, ms=int((time.monotonic() - t0) * 1000))

    # -- setup support -------------------------------------------------------------------------------
    @classmethod
    def _connect_once(cls, params: dict, secrets: dict, timeout: float = 5.0, will_topic: str = ""):
        """Blocking connect for check()/test(): (client or None, CheckResult). The caller disconnects."""
        mqtt = _mqtt()
        if mqtt is None:
            return None, CheckResult(False, ADDON_MISSING, field="addon")
        host, port_in_host = parse_host(params.get("host", ""))
        if not host:
            return None, CheckResult(False, "Enter the broker address, for example homeassistant.local.", field="host")
        try:
            port = int(params.get("port") or port_in_host or (8883 if params.get("tls") else 1883))
            if not 0 < port < 65536:
                raise ValueError
        except (TypeError, ValueError):
            return None, CheckResult(False, "The port must be a number from 1 to 65535.", field="port")
        password = str(secrets.get("password") or "")
        tls = bool(params.get("tls"))
        client = cls._new_client(mqtt, f"yardguard-check-{uuid.uuid4().hex[:8]}", params, password, will_topic)
        client.connect_timeout = timeout
        result: dict = {}
        done = threading.Event()

        def on_connect(c, u, f, rc, pr=None):
            result["rc"] = rc
            done.set()
        client.on_connect = on_connect
        try:
            client.connect(host, port, keepalive=15)
        except Exception as e:
            msg, fld = _socket_text(e, host, port, tls)
            return None, CheckResult(False, msg, field=fld)
        deadline = time.monotonic() + timeout
        try:
            while not done.is_set() and time.monotonic() < deadline:
                rc = client.loop(timeout=0.2)
                if rc != getattr(mqtt, "MQTT_ERR_SUCCESS", 0) and not done.is_set():
                    break
        except Exception as e:
            msg, fld = _socket_text(e, host, port, tls)
            return None, CheckResult(False, msg, field=fld)
        rc = result.get("rc")
        if rc is None:
            try:
                client.disconnect()
            except Exception:
                pass
            return None, CheckResult(False, f"The broker at {host}:{port} did not answer. Check the address and "
                                            "the port (1883, or 8883 with TLS).", field="host")
        failure = getattr(rc, "is_failure", None)
        if failure if failure is not None else bool(rc):
            msg, fld = _connack_text(rc, host, port)
            try:
                client.disconnect()
            except Exception:
                pass
            return None, CheckResult(False, msg, field=fld)
        return client, CheckResult(True, f"Connected to {host}:{port}.", data={"host": host, "port": port})

    @classmethod
    def check(cls, params: dict, secrets: dict[str, str]) -> CheckResult:
        """Log in to the broker and disconnect; sends nothing."""
        try:
            client, res = cls._connect_once(params, secrets)
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass
            return res
        except Exception as e:
            return CheckResult(False, scrub(f"Could not connect: {e}", str(secrets.get("password") or "")))

    def test(self) -> SendResult:
        """Publish discovery and a test event now (works before start(), through a short connection)."""
        t0 = time.monotonic()
        password = self.conf.secret("password")
        try:
            event = self.hub.sample_event()
            web_base = ""
            try:
                web_base = self.hub.web_base()
            except Exception:
                pass
            t = self._make_topics()
            msgs, note = self._messages(event, "none", web_base, t, False)
            client, t_own = self._client, self._topics
            if client is not None and t_own is not None and self._connected.is_set():
                self.refresh_discovery(force=True)
                err = self._publish_all(client, msgs, PUBLISH_TIMEOUT)
                ms = int((time.monotonic() - t0) * 1000)
                return SendResult(ok=not err, error=err, retry=bool(err), ms=ms, note=note)
            client, res = self._connect_once(self._params(), {"password": password}, will_topic="")
            if client is None:
                return SendResult(ok=False, error=res.message, permanent=res.field in ("password", "addon"),
                                  ms=int((time.monotonic() - t0) * 1000))
            client.loop_start()
            try:
                pre: list[tuple[str, bytes, int, bool]] = []
                if self._params().get("discovery", True):
                    pre.append((t.discovery, self._discovery_json(t).encode("utf-8"), 1, True))
                err = self._publish_all(client, pre + msgs, PUBLISH_TIMEOUT)
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass
                stopper = threading.Thread(target=self._loop_stop, args=(client,), daemon=True,
                                           name=f"yg-mqtt-stop-{self.conf.id}")
                stopper.start()
                stopper.join(STOP_TIMEOUT)
            ms = int((time.monotonic() - t0) * 1000)
            return SendResult(ok=not err, error=err, retry=bool(err), ms=ms, note=note)
        except Exception as e:
            return SendResult(ok=False, error=scrub(f"Could not publish: {e}", password),
                              ms=int((time.monotonic() - t0) * 1000))

    def describe(self) -> str:
        p = self._params()
        host, port = self._host_port()
        if not host:
            return "No broker set"
        parts = [f"{host}:{port}" + (" (TLS)" if p.get("tls") else "")]
        if p.get("username"):
            parts.append(f"user {p.get('username')}")
        parts.append("Home Assistant discovery on" if p.get("discovery", True) else "discovery off")
        return " · ".join(parts)


CHANNEL = MqttChannel
