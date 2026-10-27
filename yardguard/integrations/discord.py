"""Alerts posted to a Discord channel through a channel webhook.

The webhook URL contains its own password (the part after the id), so it is stored encrypted and
never shown or logged: errors are scrubbed of the whole URL and of its path. One post per message:
an embed (title, lines, severity colour, time) with the picture attached as yardguard.jpg; the clip
follows as a second post when the user asked for clips and it fits Discord's upload limit.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import media
from .base import Channel, ChannelError, http
from .base import scrub as _scrub
from .model import CheckResult, Event, SendContext, SendResult

log = logging.getLogger("yardguard")
logging.getLogger("urllib3").setLevel(logging.WARNING)

URL_RE = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
MAX_UPLOAD = 8_000_000            # webhook uploads without a server boost
PICTURE_BYTES = 4_000_000
TIMEOUT_S = 20
CLIP_TIMEOUT_S = 90
COLORS = {"critical": 0xDC2626, "warning": 0xF59E0B, "info": 0x3B82F6}
DELETED = "This webhook was deleted in Discord. Create a new one and paste its URL."


def scrub(text: str, *secrets: str) -> str:
    """base.scrub without the empty strings."""
    return _scrub(text, *[s for s in secrets if s])


def clean_url(url: str) -> str:
    """Trims what people paste around a webhook URL (spaces, quotes, angle brackets)."""
    return (url or "").strip().strip("<>\"'").strip()


def valid_url(url: str) -> bool:
    """True for https://discord.com/api/webhooks/<id>/<token> (also discordapp.com, ptb./canary.
    are normalised by the caller); a query such as ?thread_id=... is allowed."""
    url = clean_url(url)
    base = url.split("?", 1)[0]
    base = re.sub(r"^https://(ptb\.|canary\.)", "https://", base)
    return bool(URL_RE.match(base))


def url_secrets(url: str) -> tuple[str, ...]:
    """Everything of a webhook URL that must never appear in an error: the URL, its path, the token."""
    try:
        p = urlsplit(url)
    except ValueError:
        return (url,)
    out = [url, p.path] + ([p.path + "?" + p.query] if p.query else [])
    token = p.path.rstrip("/").rsplit("/", 1)[-1]
    if len(token) >= 8:
        out.append(token)
    return tuple(s for s in out if s and s != "/")


def _with_wait(url: str) -> str:
    """The URL with ?wait=true, so Discord answers with the created message (and real errors)."""
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "wait"] + [("wait", "true")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def _error_result(exc: Exception, started: float, *secrets: str) -> SendResult:
    ms = int((time.monotonic() - started) * 1000)
    if isinstance(exc, ChannelError):
        return SendResult(ok=False, error=scrub(str(exc), *secrets), retry=bool(getattr(exc, "retry", False)),
                          retry_after=getattr(exc, "retry_after", None),
                          permanent=bool(getattr(exc, "permanent", False)), ms=ms)
    log.warning("Discord: unexpected error (%s)", type(exc).__name__)
    return SendResult(ok=False, error=scrub(f"Unexpected problem: {exc}", *secrets), retry=True, ms=ms)


def _raise_for(resp, *secrets: str) -> None:
    code = resp.status_code
    if 200 <= code < 300:
        return
    if code in (401, 404):
        raise ChannelError(DELETED, permanent=True, field="url")
    if code == 403:
        raise ChannelError("Discord does not allow this webhook to post (missing permission).", permanent=True,
                           field="url")
    if code == 408:
        raise ChannelError("Discord did not answer in time.", retry=True)
    detail = ""
    try:
        detail = str(resp.json().get("message", ""))[:120]
    except Exception:
        pass
    text = f"Discord did not accept the message (HTTP {code})"
    raise ChannelError(scrub(f"{text}: {detail}" if detail else text + ".", *secrets), permanent=True)


def embed_for(event: Event, web_base: str = "") -> dict:
    lines = [ln for ln in media.lines(event) if ln]
    title = (lines[0] if lines else event.title or "YardGuard")[:256]
    embed = {"title": title, "description": "\n".join(lines[1:])[:4000],
             "color": COLORS.get(event.severity, COLORS["info"]),
             "timestamp": datetime.fromtimestamp(event.ts or time.time(), timezone.utc).isoformat()}
    if web_base and event.web_path and web_base.startswith("https://"):
        embed["url"] = web_base.rstrip("/") + "/" + event.web_path.lstrip("/")
    return embed


class DiscordChannel(Channel):
    KIND = "discord"
    CAPS = {"pictures": True, "clip": True, "edit": False, "buttons": False, "two_way": False,
            "max_upload": MAX_UPLOAD, "updates": "drop"}
    SECRET_KEYS = ("url",)

    @classmethod
    def check(cls, params: dict, secrets: dict[str, str]) -> CheckResult:
        """Reads the webhook (GET answers its name); posts nothing."""
        url = clean_url(secrets.get("url", ""))
        if not url:
            return CheckResult(False, "Paste the webhook URL from Discord.", field="url")
        if not valid_url(url):
            return CheckResult(False, "This is not a Discord webhook URL. It starts with "
                                      "https://discord.com/api/webhooks/", field="url")
        hide = url_secrets(url)
        try:
            r = http("GET", url, secrets=hide, timeout=10)
            _raise_for(r, *hide)
            try:
                info = r.json()
            except ValueError:
                info = {}
            name = str(info.get("name") or "")[:80]
            data = {"webhook_name": name} if name else {}
            return CheckResult(True, f"Found the webhook \"{name}\"" if name else "Found the webhook", data=data)
        except ChannelError as e:
            return CheckResult(False, scrub(str(e), *hide), field="url")
        except Exception as e:
            return CheckResult(False, scrub(f"Could not check the webhook: {e}", *hide), field="url")

    def _post(self, url: str, payload: dict, file_name: str, data: bytes | None, content_type: str,
              timeout: float, hide: tuple[str, ...]):
        files = {"payload_json": (None, json.dumps(payload), "application/json")}
        if data is not None:
            files["files[0]"] = (file_name, data, content_type)
        return http("POST", _with_wait(url), secrets=hide, timeout=timeout, files=files)

    def send(self, event: Event, ctx: SendContext) -> SendResult:
        started = time.monotonic()
        hide: tuple[str, ...] = ()
        try:
            url = clean_url(self.conf.secret("url"))
            hide = url_secrets(url) if url else ()
            if ctx.edit:
                return SendResult(ok=True, note="Discord messages are not updated")
            if not url:
                raise ChannelError("The webhook URL is missing or cannot be read on this Windows account. "
                                   "Enter it again.", permanent=True, field="url")
            if urlsplit(url).scheme not in ("http", "https"):
                raise ChannelError("The webhook URL is not valid.", permanent=True, field="url")
            username = str(self.conf.params.get("username") or "YardGuard")[:80]
            embed = embed_for(event, ctx.web_base)
            jpeg = None
            if ctx.pictures != "none":
                path = event.image_path(ctx.pictures)
                if path:
                    jpeg = media.prepare_jpeg(path, 1280, 80, PICTURE_BYTES)
            payload = {"username": username, "embeds": [embed], "allowed_mentions": {"parse": []}}
            note = ""
            if jpeg:
                embed["image"] = {"url": "attachment://yardguard.jpg"}
                r = self._post(url, payload, "yardguard.jpg", jpeg, "image/jpeg", TIMEOUT_S, hide)
                if r.status_code == 413:
                    embed.pop("image", None)
                    note, jpeg = "The picture was too large for Discord; sent text only.", None
                else:
                    _raise_for(r, *hide)
            if not jpeg:
                r = self._post(url, payload, "", None, "", TIMEOUT_S, hide)
                _raise_for(r, *hide)
            ref = None
            try:
                mid = r.json().get("id")
                ref = {"message_id": str(mid)} if mid else None
            except Exception:
                pass
            if ctx.send_clip and event.clip_path:
                note = self._send_clip(url, username, event, hide) or note
            return SendResult(ok=True, ref=ref, ms=int((time.monotonic() - started) * 1000), note=note)
        except Exception as e:
            return _error_result(e, started, *hide)

    def _send_clip(self, url: str, username: str, event: Event, hide: tuple[str, ...]) -> str:
        """Second post with the clip. Returns a note when it was not sent (the alert itself was)."""
        try:
            size = os.path.getsize(event.clip_path)
        except OSError:
            return "The clip file was not found."
        if size > MAX_UPLOAD:
            return f"The clip is too large for Discord ({size / 1e6:.0f} MB)."
        try:
            with open(event.clip_path, "rb") as f:
                data = f.read()
            payload = {"username": username, "content": f"Clip: {media.headline(event)}"[:1900],
                       "allowed_mentions": {"parse": []}}
            r = self._post(url, payload, "yardguard.mp4", data, "video/mp4", CLIP_TIMEOUT_S, hide)
            if r.status_code == 413:
                return "The clip was too large for Discord."
            _raise_for(r, *hide)
            return ""
        except Exception as e:
            return scrub(f"The clip was not sent: {e}", *hide)

    def describe(self) -> str:
        name = str(self.conf.params.get("webhook_name") or "")
        return f"Webhook \"{name}\"" if name else "Discord webhook"
