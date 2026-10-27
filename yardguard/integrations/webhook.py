"""Each message as JSON to the user's own server (n8n, Node-RED, Home Assistant webhook, a script).

Body schema "yardguard.v1" (see build_payload). Headers: X-YardGuard-Event, X-YardGuard-Timestamp
and, when a signing secret is set, X-YardGuard-Signature: sha256=<hex HMAC-SHA256(secret,
"<timestamp>.<body>")> so the receiver can check the message really came from YardGuard and is
fresh. The URL may carry its own key, so it is stored encrypted and shown only as
https://host/***. 2xx = delivered, 408/429/5xx/network = try again, other 4xx = setup problem.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets as _secrets
import time
from datetime import datetime
from urllib.parse import urlsplit

from . import media
from .base import Channel, ChannelError, http
from .base import scrub as _scrub
from .model import CheckResult, Event, SendContext, SendResult

log = logging.getLogger("yardguard")
logging.getLogger("urllib3").setLevel(logging.WARNING)

SCHEMA = "yardguard.v1"
METHODS = ("POST", "PUT")
PICTURE_BYTES = 2_000_000
MAX_UPLOAD = 10_000_000


def _app_version() -> str:
    try:
        from ..config import APP_VERSION
        return APP_VERSION
    except Exception:
        return "0"


def scrub(text: str, *secrets: str) -> str:
    """base.scrub without the empty strings."""
    return _scrub(text, *[s for s in secrets if s])


def new_secret() -> str:
    """A signing secret for the "Generate" button (43 URL-safe characters)."""
    return _secrets.token_urlsafe(32)


def valid_url(url: str) -> bool:
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname)


def display_url(url: str) -> str:
    """"https://user:pw@hooks.example.com:8443/abc?key=1" -> "https://hooks.example.com:8443/***"."""
    try:
        p = urlsplit((url or "").strip())
        host = p.hostname or ""
        if not host:
            return "***"
        if ":" in host:
            host = f"[{host}]"
        port = f":{p.port}" if p.port else ""
    except ValueError:
        return "***"
    return f"{p.scheme}://{host}{port}/***"


def url_secrets(url: str) -> tuple[str, ...]:
    """The URL and the parts of it that may carry a key (path, query, user info)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return (url,)
    out = [url, p.path, p.query, p.password or "", p.username or ""]
    if p.query:
        out.append(p.path + "?" + p.query)
    return tuple(s for s in out if s and s != "/" and len(s) > 1)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The X-YardGuard-Signature value for a body sent at timestamp."""
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """For receivers written in Python (and the tests): constant-time signature check."""
    return hmac.compare_digest(sign(secret, timestamp, body), signature or "")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def build_payload(event: Event, *, image: bytes | None = None, web_base: str = "") -> dict:
    """The yardguard.v1 JSON object for an event. Keys are always present; null when they do not apply."""
    web_url = None
    if web_base and event.web_path:
        web_url = web_base.rstrip("/") + "/" + event.web_path.lstrip("/")
    return {
        "schema": SCHEMA,
        "event": event.kind,
        "id": event.id,
        "ts": float(event.ts),
        "time": _iso(event.ts),
        "site": {"id": event.site_id, "name": event.site_name} if (event.site_id or event.site_name) else None,
        "camera": ({"channel": event.channel, "name": event.camera}
                   if (event.channel is not None or event.camera) else None),
        "title": event.title,
        "text": media.plain(event),
        "severity": event.severity,
        "verdict": event.verdict or None,
        "ai_note": event.ai_note or None,
        "incident": ({"id": event.incident_id, "status": event.status or "open", "alert_count": event.alert_count,
                      "cameras": [c.strip() for c in event.camera.split(",") if c.strip()],
                      "status_by": event.status_by or None,
                      "has_clip": bool(event.clip_path), "clip_pending": bool(event.clip_pending)}
                     if event.incident_id else None),
        "labels": list(event.labels or []),
        "area": ({"id": event.area_id, "name": event.area_name, "task": event.area_task, "kind": event.area_kind}
                 if event.area_id else None),
        "plate": {"text": event.plate, "known": event.plate_known} if event.plate else None,
        "image": ({"content_type": "image/jpeg", "base64": base64.b64encode(image).decode("ascii")}
                  if image else None),
        "web_url": web_url,
        "extra": dict(event.extra or {}),
    }


def encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def sample_payload(include_image: bool = False) -> str:
    """Pretty JSON of a made-up incident for the setup dialog's preview (picture shortened)."""
    e = Event(kind="incident_new", ts=time.time(), title="Person at night · Front gate",
              severity="critical", site_id="home", site_name="Home", channel=1, camera="Front gate",
              incident_id="inc-1a2b3c4d5e6f", alert_id="a1b2c3d4", alert_count=1, verdict="confirmed",
              ai_note="person near the car", status="open", labels=["person"])
    p = build_payload(e, web_base="")
    if include_image:
        p["image"] = {"content_type": "image/jpeg", "base64": "/9j/4AAQSkZJRgABAQ...(about 200 KB)"}
    return json.dumps(p, ensure_ascii=False, indent=2)


def _test_event() -> Event:
    return Event(kind="test", ts=time.time(), title="YardGuard test",
                 text="If you can read this, alerts will arrive here.", severity="info")


def post(url: str, payload: dict, *, method: str = "POST", hmac_secret: str = "", timeout: float = 10) -> int:
    """Sends one payload. Returns the HTTP status (2xx); raises ChannelError with plain words."""
    hide = url_secrets(url) + ((hmac_secret,) if hmac_secret else ())
    if not valid_url(url):
        raise ChannelError("The webhook address must start with http:// or https://", permanent=True, field="url")
    body = encode(payload)
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": f"YardGuard/{_app_version()}",
               "X-YardGuard-Event": str(payload.get("event", "")), "X-YardGuard-Timestamp": stamp}
    if hmac_secret:
        headers["X-YardGuard-Signature"] = sign(hmac_secret, stamp, body)
    method = method.upper() if str(method).upper() in METHODS else "POST"
    r = http(method, url, secrets=hide, timeout=timeout, data=body, headers=headers, allow_redirects=False)
    code = r.status_code
    if 200 <= code < 300:
        return code
    if code in (408, 429) or code >= 500:
        after = None
        try:
            after = float(r.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            pass
        raise ChannelError(f"The server is busy (HTTP {code}). Trying again later.", retry=True, retry_after=after)
    if 300 <= code < 400:
        raise ChannelError(f"The server answered with a redirect (HTTP {code}). Enter the final address.",
                           permanent=True, field="url")
    if code in (401, 403):
        raise ChannelError(f"The server refused the message as not allowed (HTTP {code}). Check the address "
                           "and the signing secret.", permanent=True, field="url")
    if code == 404:
        raise ChannelError("The server says this address does not exist (HTTP 404).", permanent=True, field="url")
    raise ChannelError(f"The server refused the message (HTTP {code}).", permanent=True)


class WebhookChannel(Channel):
    KIND = "webhook"
    CAPS = {"pictures": True, "clip": False, "edit": False, "buttons": False, "two_way": False,
            "max_upload": MAX_UPLOAD, "updates": "each"}
    SECRET_KEYS = ("url", "hmac_secret")

    @classmethod
    def check(cls, params: dict, secrets: dict[str, str]) -> CheckResult:
        """Posts an event "test" payload (that is the only way to know the receiver accepts it)."""
        url = (secrets.get("url") or "").strip()
        key = secrets.get("hmac_secret") or ""
        hide = url_secrets(url) + ((key,) if key else ())
        if not url:
            return CheckResult(False, "Enter the address to send to.", field="url")
        if not valid_url(url):
            return CheckResult(False, "The address must start with http:// or https://", field="url")
        try:
            timeout = float(params.get("timeout_s") or 10)
            code = post(url, build_payload(_test_event()), method=params.get("method", "POST"),
                        hmac_secret=key, timeout=max(2.0, min(timeout, 60.0)))
            return CheckResult(True, f"{display_url(url)} accepted the test (HTTP {code})")
        except ChannelError as e:
            return CheckResult(False, scrub(str(e), *hide), field=getattr(e, "field", "") or "url")
        except Exception as e:
            return CheckResult(False, scrub(f"Could not send: {e}", *hide), field="url")

    def send(self, event: Event, ctx: SendContext) -> SendResult:
        started = time.monotonic()
        hide: tuple[str, ...] = ()
        try:
            url = (self.conf.secret("url") or "").strip()
            key = self.conf.secret("hmac_secret") if self.conf.has_secret("hmac_secret") else ""
            hide = url_secrets(url) + ((key,) if key else ())
            if ctx.edit:
                return SendResult(ok=True, note="Webhooks get each change as a new message")
            if not url:
                raise ChannelError("The webhook address is missing or cannot be read on this Windows account. "
                                   "Enter it again.", permanent=True, field="url")
            if self.conf.has_secret("hmac_secret") and not key:
                raise ChannelError("The signing secret cannot be read on this Windows account. Enter it again.",
                                   permanent=True, field="hmac_secret")
            params = self.conf.params
            image = None
            if params.get("include_image") == "base64" and ctx.pictures != "none":
                path = event.image_path(ctx.pictures)
                if path:
                    image = media.prepare_jpeg(path, 1280, 80, PICTURE_BYTES)
            try:
                timeout = max(2.0, min(float(params.get("timeout_s") or 10), 60.0))
            except (TypeError, ValueError):
                timeout = 10.0
            post(url, build_payload(event, image=image, web_base=ctx.web_base), method=params.get("method", "POST"),
                 hmac_secret=key, timeout=timeout)
            return SendResult(ok=True, ms=int((time.monotonic() - started) * 1000))
        except Exception as e:
            ms = int((time.monotonic() - started) * 1000)
            if not isinstance(e, ChannelError):
                log.warning("Webhook: unexpected error (%s)", type(e).__name__)
                e = ChannelError(f"Unexpected problem: {e}", retry=True)
            return SendResult(ok=False, error=scrub(str(e), *hide), retry=bool(getattr(e, "retry", False)),
                              retry_after=getattr(e, "retry_after", None),
                              permanent=bool(getattr(e, "permanent", False)), ms=ms)

    def describe(self) -> str:
        url = self.conf.secret("url") if self.conf.has_secret("url") else ""
        signed = " · signed" if self.conf.has_secret("hmac_secret") else ""
        return (display_url(url) + signed) if url else "No address yet"
