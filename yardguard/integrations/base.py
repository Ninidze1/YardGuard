"""Channel base class, destination config wrapper, and helpers every channel uses.

A destination is a plain dict in Settings.integrations (see new_integration); IntegrationConf
wraps that same dict and decrypts its secrets on demand. Secrets are stored only as
config.encrypt() output and never logged: scrub() every error text before it is shown or logged.
"""
from __future__ import annotations

import importlib
import logging
import re
import secrets as _secrets
import string
import time
import uuid
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from .. import config
from . import policy
from .model import CheckResult, Event, SendContext, SendResult

if TYPE_CHECKING:  # pragma: no cover
    import requests

    from .hub import IntegrationHub

log = logging.getLogger("yardguard")
logging.getLogger("urllib3").setLevel(logging.WARNING)

CATALOG: dict[str, dict] = {
    # kind: module, title, icon, group, blurb - the UI's catalogue reads ONLY this
    "telegram": {"module": "telegram", "title": "Telegram", "icon": "send", "group": "phone",
                 "blurb": "Get a message with the picture on your phone, with buttons. Free, about 2 minutes to set up."},
    "ntfy": {"module": "ntfy", "title": "Phone push (ntfy)", "icon": "bell", "group": "phone",
             "blurb": "A push notification with the picture through the free ntfy app. No account needed."},
    "email": {"module": "emailer", "title": "Email", "icon": "mail", "group": "phone",
              "blurb": "An email with the picture. Good for the daily summary."},
    "discord": {"module": "discord", "title": "Discord", "icon": "message", "group": "home",
                "blurb": "Post alerts with the picture to a Discord channel."},
    "mqtt": {"module": "mqtt_ha", "title": "Home Assistant (MQTT)", "icon": "house", "group": "home",
             "blurb": "YardGuard's cameras, alerts and a monitoring switch appear in Home Assistant."},
    "webhook": {"module": "webhook", "title": "Webhook", "icon": "webhook", "group": "home",
                "blurb": "Send each alert as JSON to your own server, n8n or Node-RED."},
}
GROUPS = (("phone", "On your phone"), ("home", "Smart home and other apps"))
ID_PREFIX = {"telegram": "tg", "ntfy": "ntfy", "email": "mail", "discord": "dc", "mqtt": "mqtt", "webhook": "wh"}
SECRET_KEYS = {"telegram": ("token",), "ntfy": ("token",), "email": ("password",), "discord": ("url",),
               "webhook": ("url", "hmac_secret"), "mqtt": ("password",)}


def random_topic() -> str:
    """ntfy topic: long and random, because anyone who knows it can read the messages."""
    alphabet = string.ascii_lowercase + string.digits
    return "yardguard-" + "".join(_secrets.choice(alphabet) for _ in range(20))


def default_params(kind: str) -> dict:
    """Kind-specific, never secret settings with their defaults (SPEC 2.2)."""
    if kind == "telegram":
        return {"api_base": "https://api.telegram.org", "bot_username": "", "bot_name": "", "chats": [],
                "allow_control": True, "protect_content": False}
    if kind == "ntfy":
        return {"server": "https://ntfy.sh", "topic": "", "click_url": True}
    if kind == "email":
        return {"preset": "gmail", "host": "smtp.gmail.com", "port": 587, "security": "starttls", "username": "",
                "sender": "", "to": [], "subject_prefix": "YardGuard"}
    if kind == "discord":
        return {"username": "YardGuard"}
    if kind == "webhook":
        return {"method": "POST", "include_image": "none", "timeout_s": 10}
    if kind == "mqtt":
        return {"host": "", "port": 1883, "tls": False, "username": "", "discovery": True,
                "discovery_prefix": "homeassistant", "base_topic": "yardguard", "retain_images": True,
                "allow_control": True}
    return {}


def new_integration(kind: str, name: str = "") -> dict:
    """A complete destination dict with defaults and the "Only important" routes. Not saved."""
    d = {"id": f"{ID_PREFIX.get(kind, kind[:4] or 'x')}-{uuid.uuid4().hex[:8]}", "kind": kind,
         "name": name or CATALOG.get(kind, {}).get("title", kind.capitalize()), "enabled": True,
         "created": time.time(), "secrets": {}, "params": default_params(kind),
         "routes": policy.default_routes(kind)}
    return normalize_integration(d)


def normalize_integration(d: dict) -> dict:
    """Fill missing params / routes keys and fix wrong types in place; returns d."""
    kind = str(d.get("kind") or "")
    d["kind"] = kind
    if not d.get("id") or not isinstance(d.get("id"), str):
        d["id"] = f"{ID_PREFIX.get(kind, kind[:4] or 'x')}-{uuid.uuid4().hex[:8]}"
    if not isinstance(d.get("name"), str) or not d["name"].strip():
        d["name"] = CATALOG.get(kind, {}).get("title", kind.capitalize() or "Destination")
    d["enabled"] = bool(d.get("enabled", True))
    try:
        d["created"] = float(d.get("created") or time.time())
    except (TypeError, ValueError):
        d["created"] = time.time()
    sec = d.get("secrets") if isinstance(d.get("secrets"), dict) else {}
    d["secrets"] = {str(k): v for k, v in sec.items() if isinstance(v, str) and v}
    params = d.get("params") if isinstance(d.get("params"), dict) else {}
    for k, v in default_params(kind).items():
        params.setdefault(k, v)
    if kind == "ntfy" and not params.get("topic"):
        params["topic"] = random_topic()
    if kind == "telegram":
        chats = params.get("chats") if isinstance(params.get("chats"), list) else []
        params["chats"] = [c for c in chats if isinstance(c, dict) and "id" in c]
    if kind == "email" and isinstance(params.get("to"), str):
        params["to"] = [t.strip() for t in params["to"].split(",") if t.strip()]
    d["params"] = params
    d["routes"] = policy.normalize_routes(d.get("routes"), kind)
    return d


class IntegrationConf:
    """A destination dict from Settings.integrations, with secret helpers. Wraps the SAME dict
    (changes are visible to config.save())."""

    def __init__(self, d: dict) -> None:
        self.d = d

    id = property(lambda self: str(self.d.get("id", "")))
    kind = property(lambda self: str(self.d.get("kind", "")))

    @property
    def name(self) -> str:
        return str(self.d.get("name") or CATALOG.get(self.kind, {}).get("title", self.kind))

    @name.setter
    def name(self, value: str) -> None:
        self.d["name"] = str(value).strip() or self.name

    @property
    def enabled(self) -> bool:
        return bool(self.d.get("enabled", True))

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.d["enabled"] = bool(value)

    @property
    def params(self) -> dict:
        return self.d.setdefault("params", {})

    @property
    def routes(self) -> dict:
        return self.d.setdefault("routes", policy.default_routes(self.kind))

    @property
    def title(self) -> str:
        """The catalogue title of the kind ("Telegram")."""
        return CATALOG.get(self.kind, {}).get("title", self.kind)

    def secret(self, key: str) -> str:
        """Decrypted secret; "" if missing or unreadable."""
        token = self.d.get("secrets", {}).get(key)
        return config.decrypt(token) if token else ""

    def set_secret(self, key: str, value: str) -> None:
        """Encrypt and store; "" removes the key."""
        sec = self.d.setdefault("secrets", {})
        if value:
            sec[key] = config.encrypt(value)
        else:
            sec.pop(key, None)

    def has_secret(self, key: str) -> bool:
        return bool(self.d.get("secrets", {}).get(key))

    def secret_unreadable(self, key: str) -> bool:
        """Stored, but decrypt() gave "" (saved by another Windows user or on another PC)."""
        return self.has_secret(key) and not self.secret(key)

    def secret_values(self) -> tuple[str, ...]:
        """Every readable secret of this destination, for scrub()."""
        return tuple(v for v in (self.secret(k) for k in self.d.get("secrets", {})) if v)

    def __eq__(self, other) -> bool:
        return isinstance(other, IntegrationConf) and other.d is self.d

    def __hash__(self) -> int:
        return id(self.d)

    def __repr__(self) -> str:  # never shows secrets
        return f"IntegrationConf({self.kind}, {self.id}, {self.name!r})"


# --- channel classes ---------------------------------------------------------------------

_TEST_CHANNELS: dict[str, type] = {}


def register_test_channel(kind: str, cls: type | None) -> None:
    """Tests only: make load_channel_class(kind) return cls (None removes it). Also used for kinds
    that are not in CATALOG."""
    if cls is None:
        _TEST_CHANNELS.pop(kind, None)
    else:
        _TEST_CHANNELS[kind] = cls


def load_channel_class(kind: str) -> type["Channel"] | None:
    """The Channel class for a kind, imported on first use; None (and a log line) when the module
    is missing or broken, so an optional package never breaks the app."""
    if kind in _TEST_CHANNELS:
        return _TEST_CHANNELS[kind]
    info = CATALOG.get(kind)
    if info is None:
        return None
    try:
        module = importlib.import_module(f"{__package__}.{info['module']}")
    except Exception as exc:
        log.warning("Integration %s is not available: %s", kind, scrub(str(exc)))
        return None
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Channel) and value is not Channel and value.KIND == kind:
            return value
    log.warning("Integration %s: no channel class found", kind)
    return None


# --- secrets in texts -------------------------------------------------------------------

_BOT_TOKEN = re.compile(r"bot\d{3,12}:[A-Za-z0-9_-]{10,}")
_TOKEN_LIKE = re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{30,}\b")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@'\"]+@")
_QUERY = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s'\"?]*|/[^\s'\"?]*)\?[^\s'\")]*")
_DISCORD = re.compile(r"(?i)(/api/webhooks/)\d+/[\w-]+")
_AUTH = re.compile(r"(?i)\b(authorization|password|passwd|pwd|token|secret|api_?key)(\s*[:=]\s*)([^\s,;'\"]+)"
                   r"|\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=-]{8,})")


def scrub(text, *secrets: str) -> str:
    """The text without secrets: each given secret (also URL-encoded), Telegram bot tokens,
    URL user:password@, query strings, Discord webhook keys and "token=..." pairs become "***".
    Truncated to 300 characters."""
    s = "" if text is None else str(text)
    for sec in sorted((x for x in secrets if x and len(x) >= 3), key=len, reverse=True):
        for form in {sec, quote(sec, safe=""), quote(sec)}:
            s = s.replace(form, "***")
    s = _BOT_TOKEN.sub("bot***", s)
    s = _TOKEN_LIKE.sub("***", s)
    s = _USERINFO.sub(r"\1***@", s)
    s = _DISCORD.sub(r"\1***", s)
    s = _QUERY.sub(r"\1?***", s)
    s = _AUTH.sub(lambda m: (m.group(1) + m.group(2) if m.group(1) else m.group(4) + m.group(5)) + "***", s)
    s = " ".join(s.split())
    return s if len(s) <= 300 else s[:299] + "…"


def mask(secret: str, keep: int = 3) -> str:
    """For display: "123456:****XYZ" (Telegram tokens keep the bot number), else "****XYZ"."""
    secret = secret or ""
    if not secret:
        return ""
    if len(secret) <= keep * 2 + 2:
        return "****"
    m = re.match(r"^(\d{3,12}):", secret)
    head = m.group(1) + ":" if m else ""
    return f"{head}****{secret[-keep:]}"


def url_for_display(url: str) -> str:
    """https://host/*** - a URL that may carry a key, shown without it."""
    try:
        parts = urlsplit(url or "")
        if parts.scheme and parts.hostname:
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme}://{parts.hostname}{port}/***"
    except ValueError:
        pass
    return "***" if url else ""


class ChannelError(Exception):
    """A delivery or check problem in plain words (already scrubbed)."""

    def __init__(self, message: str, *, retry: bool = False, permanent: bool = False,
                 retry_after: float | None = None, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.retry, self.permanent, self.retry_after, self.field = retry, permanent, retry_after, field

    def result(self, ms: int = 0) -> SendResult:
        return SendResult(ok=False, error=self.message, retry=self.retry, retry_after=self.retry_after,
                          permanent=self.permanent, ms=ms)


def _retry_after(resp) -> float | None:
    value = resp.headers.get("Retry-After") if resp is not None else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    try:
        data = resp.json()
        ra = (data.get("parameters") or {}).get("retry_after") or data.get("retry_after")
        return float(ra) if ra is not None else None
    except Exception:
        return None


def http(method: str, url: str, *, secrets: tuple[str, ...] = (), timeout: float = 15,
         session: "requests.Session | None" = None, **kw) -> "requests.Response":
    """requests call that never leaks secrets: connection errors / timeouts become
    ChannelError(retry=True) with scrubbed plain-words text; 429 -> ChannelError with
    retry_after (Retry-After header or JSON parameters.retry_after); 5xx -> ChannelError(retry);
    other responses (2xx-4xx) are returned. Never logs the URL."""
    import requests

    try:
        host = urlsplit(url).hostname or "the server"
    except ValueError:
        host = "the server"
    try:
        resp = (session or requests).request(method, url, timeout=timeout, **kw)
    except requests.exceptions.SSLError:
        raise ChannelError(f"Secure connection to {host} failed.", retry=True) from None
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout):
        raise ChannelError(f"The server did not answer in {timeout:g} s.", retry=True) from None
    except requests.exceptions.ConnectionError:
        raise ChannelError(f"Could not connect to {host}. Check the internet connection.", retry=True) from None
    except requests.exceptions.InvalidURL:
        raise ChannelError("The address is not valid.", permanent=True, field="url") from None
    except requests.exceptions.RequestException as exc:
        raise ChannelError(scrub(f"Request failed: {type(exc).__name__}", *secrets), retry=True) from None
    if resp.status_code == 429:
        ra = _retry_after(resp)
        raise ChannelError("Too many messages; the service asked to wait"
                           + (f" {ra:g} s." if ra else "."), retry=True, retry_after=ra)
    if resp.status_code >= 500:
        raise ChannelError(f"The server had a problem (HTTP {resp.status_code}). Trying again later.", retry=True)
    return resp


# --- the channel base class -----------------------------------------------------------------

def test_context(conf: "IntegrationConf | None" = None, web_base: str = "") -> SendContext:
    """SendContext for a test message: the destination's pictures setting, normal priority."""
    pictures = (conf.routes.get("pictures") if conf is not None else None) or "crop"
    return SendContext(delivery_id="test-" + uuid.uuid4().hex[:8], pictures=pictures, silent=False, priority=3,
                       thread_ref=None, edit=False, send_clip=False, web_base=web_base, attempt=1)


class Channel:
    """One destination's sender. send() runs on the destination's worker thread, one call at a
    time, and never raises; lifecycle calls run on the GUI thread and must not block."""
    KIND: str = ""
    CAPS: dict = {"pictures": True, "clip": False, "edit": False, "buttons": False,
                  "two_way": False, "max_upload": 10_000_000, "updates": "drop"}
    SECRET_KEYS: tuple[str, ...] = ()

    def __init__(self, conf: IntegrationConf, hub: "IntegrationHub | None") -> None:
        self.conf = conf
        self.hub = hub
        self.status = "ok" if conf.enabled else "off"     # ok | connecting | problem | off
        self.status_text = ""

    # lifecycle
    def start(self) -> None:
        """Start background parts (pollers, connections). Default: nothing."""

    def stop(self) -> None:
        """Stop background parts; must return within 3 s."""

    def reconfigure(self, conf: IntegrationConf) -> None:
        self.stop()
        self.conf = conf
        self.start()

    # delivery
    def send(self, event: Event, ctx: SendContext) -> SendResult:
        raise NotImplementedError

    # setup support
    @classmethod
    def check(cls, params: dict, secrets: dict[str, str]) -> CheckResult:
        return CheckResult(True, "OK")

    def test(self) -> SendResult:
        """Send a sample message right now, bypassing the routes."""
        try:
            event = self.hub.sample_event() if self.hub is not None else sample_event_fallback()
            web = self.hub.web_base() if self.hub is not None else ""
            return self.send(event, test_context(self.conf, web))
        except Exception as exc:  # send() should not raise; a test must never crash the UI
            return SendResult(False, error=scrub(str(exc), *self.secret_values()))

    # status
    def describe(self) -> str:
        return CATALOG.get(self.KIND, {}).get("title", self.KIND)

    def set_status(self, status: str, text: str = "") -> None:
        """Change status / status_text and tell the hub (the UI refreshes the card)."""
        text = scrub(text, *self.secret_values()) if text else ""
        if (status, text) == (self.status, self.status_text):
            return
        self.status, self.status_text = status, text
        if self.hub is not None:
            notify = getattr(self.hub, "notify_status", None)
            if notify:
                notify(self.conf.id)

    def secret_values(self) -> tuple[str, ...]:
        return self.conf.secret_values() if self.conf is not None else ()

    def scrub(self, text) -> str:
        """scrub() with this destination's secrets."""
        return scrub(text, *self.secret_values())


def sample_event_fallback() -> Event:
    from . import media
    now = time.time()
    return Event("test", now, "YardGuard test", text="If you can read this, alerts will arrive here.",
                 severity="info", camera="Front gate", snapshot_path=media.demo_picture())
