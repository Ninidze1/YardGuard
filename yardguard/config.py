"""Sites, cameras and app settings, stored in config.json next to the app.

Passwords are encrypted with Windows DPAPI: only this Windows user on this PC
can decrypt them, and they are never written in plain text.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import uuid
from ctypes import wintypes
from dataclasses import InitVar, asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("YARDGUARD_CONFIG", ROOT / "config.json"))
APP_VERSION = "0.2.0"

BRANDS = {
    "dahua": "Dahua (also Imou, Amcrest, Lorex)",
    "hikvision": "Hikvision (also HiLook, EZVIZ)",
    "custom": "Other brand (custom RTSP path)",
}
ROLES = ["Parking", "Entrance", "Yard", "Street", "Indoor", "Other"]
# Watch zone colours, shared by the zone editor and the live view so a zone looks the same in
# both. Warm / non-blue so zones are never confused with the blue car boxes.
ZONE_COLORS = ("#f59e0b", "#ec4899", "#14b8a6", "#f97316", "#a3e635", "#e879f9")


def zone_color(index: int) -> str:
    return ZONE_COLORS[index % len(ZONE_COLORS)]


def zone_color_bgr(index: int) -> tuple[int, int, int]:
    h = zone_color(index).lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


# --- Areas -----------------------------------------------------------------------
# An area is a polygon or a line the user drew on one camera, with a task ("what should happen
# here"). Stored in Camera.areas as plain dicts (see new_area for every key); yardguard.areas
# evaluates them while monitoring runs.
AREA_SHAPES = ("polygon", "line")
AREA_DIRECTIONS = ("both", "a_to_b", "b_to_a")  # a_to_b: from the left of A->B to its right
SCHEDULE_MODES = ("always", "night", "custom")
AREA_OBJECTS = ("person", "car", "truck", "bus", "motorcycle")

# task -> params with their defaults
TEMPLATES: dict[str, dict] = {
    "watch_zone": {},
    "count": {"objects": ["car"]},
    "log_plates": {"min_plate_px": 80, "known": []},
    "parking_spot": {"occupied_after_s": 20, "free_after_s": 45},
    "no_parking": {"max_minutes": 5},
    "loitering": {"seconds": 30},
    "presence": {},
    "line_cross": {},
    "left_object": {"seconds": 60, "min_change": 0.06},
    "state_watch": {"seconds": 120, "min_change": 0.12},
    "ask_ai": {"question": "barrier arm is raised", "interval_s": 30},
}
# task -> how the UI presents it: title, one-line example, shapes it works with, default objects,
# and whether it raises alerts by default
TEMPLATE_INFO: dict[str, dict] = {
    "watch_zone": {"title": "Watch zone", "example": "Night presence, lingering and prowling rules watch here",
                   "shapes": ["polygon"], "objects": ["person"], "alert": True},
    "count": {"title": "Count", "example": "How many cars came in and went out today",
              "shapes": ["line", "polygon"], "objects": ["car"], "alert": False},
    "log_plates": {"title": "Log number plates", "example": "Every car through the barrier with its plate and time",
                   "shapes": ["line"], "objects": ["car", "truck", "bus", "motorcycle"], "alert": False},
    "parking_spot": {"title": "Parking spot", "example": "Is my spot free, who parked there and for how long",
                     "shapes": ["polygon"], "objects": ["car", "truck", "bus", "motorcycle"], "alert": False},
    "no_parking": {"title": "No parking", "example": "A car blocking the gate for more than 5 minutes",
                   "shapes": ["polygon"], "objects": ["car", "truck", "bus", "motorcycle"], "alert": True},
    "loitering": {"title": "Lingering", "example": "Someone standing at the door for more than 30 s",
                  "shapes": ["polygon"], "objects": ["person"], "alert": True},
    "presence": {"title": "Presence", "example": "Anyone in the yard at night",
                 "shapes": ["polygon"], "objects": ["person"], "alert": True},
    "line_cross": {"title": "Line crossing", "example": "Someone climbing over the fence, a car going the wrong way",
                   "shapes": ["line", "polygon"], "objects": ["person"], "alert": True},
    "left_object": {"title": "Left or taken object", "example": "A package left at the door, or something taken",
                    "shapes": ["polygon"], "objects": [], "alert": False},
    "state_watch": {"title": "Changed and stayed changed", "example": "Gate or garage door left open",
                    "shapes": ["polygon"], "objects": [], "alert": True},
    "ask_ai": {"title": "Ask the AI", "example": "Is the barrier arm raised? Logged every time the answer changes",
               "shapes": ["polygon"], "objects": [], "alert": False},
}


def new_area_id() -> str:
    return uuid.uuid4().hex[:8]


def new_area(task: str, shape: str, points, name: str = "", **kw) -> dict:
    """A complete area dict with the template's defaults; kw overrides any key."""
    info = TEMPLATE_INFO.get(task, TEMPLATE_INFO["presence"])
    area = {"id": new_area_id(), "name": name or info["title"], "shape": shape,
            "points": [[float(x), float(y)] for x, y in points], "direction": "both", "task": task,
            "objects": list(info["objects"]), "schedule": {"mode": "always", "start": "22:00", "end": "06:00"},
            "params": dict(TEMPLATES.get(task, {})), "alert": bool(info["alert"]), "enabled": True, "color": 0}
    area.update(kw)
    return normalize_area(area)


def normalize_area(a: dict) -> dict:
    """Fill missing keys and fix wrong values in place (hand-edited or older config); returns a."""
    a["id"] = str(a.get("id") or new_area_id())
    a["task"] = a.get("task") if a.get("task") in TEMPLATES else "presence"
    a["shape"] = a.get("shape") if a.get("shape") in AREA_SHAPES else "polygon"
    pts = []
    for p in a.get("points") or []:
        try:
            pts.append([min(1.0, max(0.0, float(p[0]))), min(1.0, max(0.0, float(p[1])))])
        except (TypeError, ValueError, IndexError):
            continue
    a["points"] = pts[:2] if a["shape"] == "line" else pts
    a["name"] = str(a.get("name") or TEMPLATE_INFO[a["task"]]["title"])
    a["direction"] = a.get("direction") if a.get("direction") in AREA_DIRECTIONS else "both"
    objs = a.get("objects")
    a["objects"] = [str(o) for o in objs] if isinstance(objs, list) else list(TEMPLATE_INFO[a["task"]]["objects"])
    sched = a.get("schedule") if isinstance(a.get("schedule"), dict) else {}
    a["schedule"] = {"mode": sched.get("mode") if sched.get("mode") in SCHEDULE_MODES else "always",
                     "start": str(sched.get("start") or "22:00"), "end": str(sched.get("end") or "06:00")}
    params = a.get("params") if isinstance(a.get("params"), dict) else {}
    a["params"] = {**TEMPLATES[a["task"]], **params}
    a["alert"] = bool(a.get("alert", TEMPLATE_INFO[a["task"]]["alert"]))
    a["enabled"] = bool(a.get("enabled", True))
    try:
        a["color"] = int(a.get("color") or 0)
    except (TypeError, ValueError):
        a["color"] = 0
    return a


def area_valid(a: dict) -> bool:
    """Enough points for its shape (a line needs 2, a polygon 3)."""
    return len(a.get("points") or []) >= (2 if a.get("shape") == "line" else 3)

# key: (title, description, download size in MB)
MODELS = {
    "yolo11n": ("Fast", "Lowest GPU use. Good for many cameras at once.", 5.4),
    "yolo11s": ("Balanced", "Recommended starting point.", 18.4),
    "yolo11m": ("Accurate", "Catches more dark, small or very close cars.", 38.8),
    "yolo11l": ("Most accurate", "Best detection, highest GPU use.", 49.0),
}
DEFAULT_MODEL = "yolo11s"
CONFIG_VERSION = 4   # Settings.config_version: one-time migrations below run for older files
STREAMS = {"main": "Full quality (main stream): recommended", "sub": "Low bandwidth (sub stream)"}
THEMES = {"system": "Same as Windows", "light": "Light", "dark": "Dark"}


# --- Password encryption (Windows DPAPI) -------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def encrypt(text: str) -> str:
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _dpapi(base64.b64decode(token), False).decode("utf-8")
    except (OSError, ValueError):
        return ""  # encrypted by another Windows user / PC


def masked_url(url: str) -> str:
    return re.sub(r"(rtsp://[^:/@]*):[^@]*@", r"\1:***@", url)


# --- Data model ---------------------------------------------------------------

@dataclass
class Camera:
    channel: int
    name: str = ""
    role: str = "Other"
    enabled: bool = True
    # Older watch zones ({"name", "points"}): taken over as "watch_zone" areas on creation and not
    # stored any more; Camera.zones below reads and writes those areas.
    zones: InitVar[list[dict] | None] = None
    monitor: bool = True  # include this camera in live monitoring / alerts
    # Areas drawn on the picture (see new_area). Enabled watch_zone areas are where the night
    # presence / lingering / prowling / car-moved rules look; none = the whole picture.
    areas: list[dict] = field(default_factory=list)

    def __post_init__(self, zones: list[dict] | None) -> None:
        self.areas = [normalize_area(dict(a)) for a in (self.areas or []) if isinstance(a, dict)]
        if zones:
            known = {_points_key(a["points"]) for a in self.areas if a["task"] == "watch_zone"}
            for z in zones:
                if isinstance(z, dict) and _points_key(z.get("points") or []) not in known:
                    self.areas.append(_zone_area(z, len(self.areas)))

    @property
    def label(self) -> str:
        return self.name or f"Camera {self.channel}"

    def area(self, area_id: str) -> dict | None:
        return next((a for a in self.areas if a.get("id") == area_id), None)


def _points_key(points) -> tuple:
    try:
        return tuple((round(float(x), 4), round(float(y), 4)) for x, y in points)
    except (TypeError, ValueError):
        return ()


def _zone_area(z: dict, index: int) -> dict:
    return new_area("watch_zone", "polygon", z.get("points") or [], z.get("name") or f"Zone {index + 1}",
                    color=index)


def _get_zones(cam: Camera) -> list[dict]:
    """The watch_zone areas as older code knows them: {"name", "points"} (disabled ones too, so
    reading, editing and writing back keeps them; the rules use active_zones)."""
    return [{"name": a["name"], "points": a["points"]} for a in cam.areas if a["task"] == "watch_zone"]


def active_zones(cam) -> list[dict]:
    """Where the night presence / lingering / prowling / car-moved rules look: the enabled
    watch_zone areas. None = the whole picture."""
    return [{"name": a["name"], "points": a["points"]} for a in cam.areas
            if a["task"] == "watch_zone" and a["enabled"] and area_valid(a)]


def _set_zones(cam: Camera, zones: list[dict]) -> None:
    """Replace the watch_zone areas (other areas stay); a zone keeps its area's id and settings
    when its position in the list is unchanged."""
    old = [a for a in cam.areas if a["task"] == "watch_zone"]
    rest = [a for a in cam.areas if a["task"] != "watch_zone"]
    new = []
    for i, z in enumerate(zones or []):
        if i < len(old):
            a = old[i]
            a["name"], a["points"] = z.get("name") or a["name"], [list(p) for p in z.get("points") or []]
            new.append(normalize_area(a))
        else:
            new.append(_zone_area(z, len(rest) + i))
    cam.areas = new + rest


# A property next to the InitVar of the same name: Camera(zones=[...]) still works, cam.zones reads
# and writes the watch_zone areas, and asdict() / config.json only carry "areas".
Camera.zones = property(_get_zones, _set_zones)


@dataclass
class Site:
    name: str = "New site"
    host: str = ""
    port: int = 554
    brand: str = "dahua"
    username: str = ""
    password_enc: str = ""
    custom_path: str = "/cam/realmonitor?channel={channel}&subtype={subtype}"
    cameras: list[Camera] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def password(self) -> str:
        return decrypt(self.password_enc) if self.password_enc else ""

    @property
    def has_password(self) -> bool:
        return bool(self.password_enc)

    def set_password(self, value: str) -> None:
        self.password_enc = encrypt(value) if value else ""

    def stream_path(self, channel: int, main: bool = True) -> str:
        if self.brand == "hikvision":
            return f"/Streaming/Channels/{channel}0{1 if main else 2}"
        if self.brand == "dahua":
            return f"/cam/realmonitor?channel={channel}&subtype={0 if main else 1}"
        try:
            path = self.custom_path.format(channel=channel, subtype=0 if main else 1, stream=1 if main else 2)
        except (KeyError, ValueError, IndexError):
            path = self.custom_path
        return path if path.startswith("/") else "/" + path

    def rtsp_url(self, channel: int, main: bool = True, password: str | None = None) -> str:
        pw = self.password if password is None else password
        creds = f"{quote(self.username, safe='')}:{quote(pw, safe='')}@" if self.username else ""
        return f"rtsp://{creds}{self.host}:{self.port}{self.stream_path(channel, main)}"

    def enabled_cameras(self) -> list[Camera]:
        return sorted((c for c in self.cameras if c.enabled), key=lambda c: c.channel)

    def camera(self, channel: int) -> Camera | None:
        return next((c for c in self.cameras if c.channel == channel), None)


@dataclass
class Settings:
    events_dir: str = str(ROOT / "events")
    model: str = "yolo11s"
    confidence: float = 0.35
    stream: str = "main"
    theme: str = "system"
    show_detections: bool = True
    overlay_style: str = "boxes"     # live view AI overlay when shown: "boxes" | "corners"

    # Monitoring pipeline ("eyes")
    detect_fps: float = 5.0          # AI frames per second per camera (live view still shows full fps)
    detect_size: int = 1280          # detection resolution (long side); higher catches small/close/dark objects
    plate_long_side: int = 1920      # cameras with a number plate line deliver frames up to this big (plates
                                     # are legible from ~85 px; YOLO still detects at detect_size)
    gpu_decode: bool = True          # decode video on the NVIDIA GPU (NVDEC) when available
    motion_gating: bool = True       # skip AI when nothing moves (a slow keep-alive check still runs)
    monitoring_on: bool = False      # monitoring was switched on when the app last ran (restored at start)

    # Incident clips (a few seconds before and after each alert)
    clips_enabled: bool = True
    clip_fps: float = 8.0            # clip frames per second (cameras deliver at least this while clips are on)
    clip_max_gb: float = 5.0         # total size cap; the oldest clips go first
    clip_max_days: int = 30
    false_alarm_clip_days: int = 1   # clips of incidents marked as false alarms go sooner
    area_log_days: int = 30          # area events (counts, plates, ...) and their pictures kept this long

    # Rules
    night_start: str = "00:00"       # night hours for the "person near cars at night" rule
    night_end: str = "06:00"
    rule_night_presence: bool = True
    # When lingering / car to car / car moved may alert. In a busy residential yard people chat, wait and
    # load cars all day, so by default they only alert during night hours (areas have their own schedule).
    rules_when: str = "night"        # "night" | "always"
    rule_loitering: bool = True
    loiter_seconds: int = 120
    rule_prowling: bool = True       # one person visiting several cars
    prowl_cars: int = 3
    prowl_window_s: int = 180
    rule_car_moved: bool = True      # parked car moved / was hit
    alert_cooldown_s: int = 120      # same rule + camera + object: at most one alert per cooldown
    incident_gap_s: int = 300        # alerts of one site less than this apart form one incident

    # LocateAnything ("brain"): double-checks alerts. It first makes sure the person or car the alert
    # is about is really there, then looks for brain_prompts (suspicious behaviour) on that subject.
    brain_mode: str = "verify"       # "off" | "verify" (check suspicious moments)
    brain_generation: str = "fast"   # "fast" | "hybrid" | "slow"
    brain_prompts: list[str] = field(default_factory=lambda: [
        "person crouching next to a car",
        "hand on a car door handle",
        "open car door",
        "person lying under a car",
        "broken car window",
        "person looking into a car window",
    ])
    config_version: int = 0          # see CONFIG_VERSION

    # Integrations (yardguard.integrations): where alerts are sent, and phone access (yardguard.webremote)
    install_id: str = ""             # random 8 hex, made once by the migration (MQTT node id, web cookie name)
    integrations: list[dict] = field(default_factory=list)   # destinations; [] = nothing is sent
    notify: dict = field(default_factory=dict)    # {"snooze_until": epoch, 0 = not snoozed}
    remote: dict = field(default_factory=dict)    # phone access settings (webremote.normalize_remote)


@dataclass
class Config:
    sites: list[Site] = field(default_factory=list)
    active_site_id: str = ""
    settings: Settings = field(default_factory=Settings)

    def active_site(self) -> Site | None:
        site = next((s for s in self.sites if s.id == self.active_site_id), None)
        return site or (self.sites[0] if self.sites else None)

    def site(self, site_id: str) -> Site | None:
        return next((s for s in self.sites if s.id == site_id), None)

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


def _pick(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def load_config() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for s in data.get("sites", []):
            # "zones" (older files) is an InitVar, not a field: _pick would drop it
            cams = [Camera(**_pick(Camera, c), zones=c.get("zones")) for c in s.get("cameras", [])]
            cfg.sites.append(Site(**{**_pick(Site, s), "cameras": cams}))
        cfg.active_site_id = data.get("active_site_id", "")
        cfg.settings = Settings(**_pick(Settings, data.get("settings", {})))
    _migrate_settings(cfg.settings)
    _migrate_env(cfg)
    return cfg


def _migrate_settings(s: Settings) -> None:
    """Values older versions wrote that no longer exist: the "Detect everything" brain mode
    ("main") and LocateAnything as the main model ("locateanything") are gone; the brain only
    verifies alerts now, next to a YOLO model."""
    if s.brain_mode not in ("off", "verify"):
        s.brain_mode = "verify"
    if s.model not in MODELS:
        s.model = DEFAULT_MODEL
    if s.brain_generation not in ("fast", "hybrid", "slow"):
        s.brain_generation = "fast"
    if not isinstance(s.brain_prompts, list) or not all(isinstance(v, str) for v in s.brain_prompts):
        s.brain_prompts = Settings().brain_prompts
    try:
        s.area_log_days = max(1, int(s.area_log_days))
    except (TypeError, ValueError):
        s.area_log_days = Settings().area_log_days
    if not isinstance(s.config_version, int) or s.config_version < 2:
        # Benchmarked: "hybrid" was never more accurate than "fast", but slower and more prone to runaway
        # boxes; verification now asks several pictures per alert, so the slower mode adds up. Once only:
        # choosing Hybrid again afterwards is kept.
        if s.brain_generation == "hybrid":
            s.brain_generation = "fast"
    if not isinstance(s.config_version, int) or s.config_version < 3:
        # 30 s lingering was the old default and flooded a busy yard with alerts (34 in one day): once
        # only, the old default becomes the new one. A value the user chose is kept.
        if s.loiter_seconds == 30:
            s.loiter_seconds = Settings().loiter_seconds
    if s.rules_when not in ("night", "always"):
        s.rules_when = "night"
    # Integrations (every time, not only once): type checks only. Defaults of each destination are
    # filled by integrations.base.normalize_integration (importing it here would be an import cycle).
    if not isinstance(s.install_id, str) or not re.fullmatch(r"[0-9a-f]{8}", s.install_id):
        s.install_id = uuid.uuid4().hex[:8]
    if not isinstance(s.integrations, list):
        s.integrations = []
    s.integrations = [d for d in s.integrations if isinstance(d, dict) and isinstance(d.get("kind"), str)
                      and d.get("kind")]
    if not isinstance(s.notify, dict):
        s.notify = {}
    if not isinstance(s.remote, dict):
        s.remote = {}
    s.config_version = CONFIG_VERSION


def _migrate_env(cfg: Config) -> None:
    """Move a login from the old .env file into config.json, then delete .env."""
    env_path = ROOT / ".env"
    if not env_path.exists() or CONFIG_PATH != ROOT / "config.json":
        return
    values = {}
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    host = values.get("CAM_HOST", "")
    if host:
        site = next((s for s in cfg.sites if s.host == host), None)
        if site is None:
            channel = int(values.get("CAM_CHANNEL") or 1)
            site = Site(name="My site", host=host, port=int(values.get("CAM_PORT") or 554),
                        brand=values.get("CAM_BRAND", "dahua").lower(), cameras=[Camera(channel)])
            cfg.sites.append(site)
        if not site.username:
            site.username = values.get("CAM_USER", "")
        if not site.has_password and values.get("CAM_PASS"):
            site.set_password(values["CAM_PASS"])
        if not cfg.active_site_id:
            cfg.active_site_id = site.id
        cfg.save()
    env_path.unlink()
    (ROOT / ".env.example").unlink(missing_ok=True)
