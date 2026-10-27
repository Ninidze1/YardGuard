"""Phone access: a small web server inside YardGuard that phones open in their browser.

Off until the user turns it on. Binds to 127.0.0.1 unless "Phones on my home Wi-Fi" is on. Every
route except the app's own files and pairing needs a paired device's cookie; actions (mark an
incident, switch monitoring) also need the "allow actions" setting, a CSRF header and a same-origin
request. Runs on daemon threads; nothing here touches Qt widgets or saves the config off the GUI
thread (saves are queued to it through WebRemoteSignals).
"""
from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from . import api
from .auth import (Csrf, PairingCodes, PairLimiter, clean_name, find_device, format_code, hash_token,
                   new_device_id, new_token, parse_cookie)
from .live import LiveHub

log = logging.getLogger("yardguard")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_PORT = 8765
DEFAULTS = {"enabled": False, "port": DEFAULT_PORT, "lan": False, "allow_actions": False, "public_url": "",
            "devices": [], "live_fps": 2, "live_max_side": 960}

# request path -> (file in web/, content type, cache seconds); nothing else is ever read from disk
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8", 0),
    "/index.html": ("index.html", "text/html; charset=utf-8", 0),
    "/app.js": ("app.js", "text/javascript; charset=utf-8", 0),
    "/app.css": ("app.css", "text/css; charset=utf-8", 0),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", 0),
    "/icon-192.png": ("icon-192.png", "image/png", 86400),
    "/icon-512.png": ("icon-512.png", "image/png", 86400),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png", 86400),
    "/favicon.ico": ("icon-192.png", "image/png", 86400),
}
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; media-src 'self'; "
                                "frame-ancestors 'none'"),
)
ID_RE = r"[A-Za-z0-9_-]{1,64}"
MAX_BODY = 4096
MAX_CONNECTIONS = 64
LIVE_MAX_S = 600.0           # a live stream ends after 10 min; the page offers "Tap to keep watching"
LONG_POLL_MAX_S = 25.0
ACTION_TIMEOUT_S = 10.0
SEEN_SAVE_EVERY_S = 60.0
CONNECTED_WINDOW_S = 120.0
THUMB_CACHE = 50
CHANGE_RING = 500
INCIDENT_STATUSES = ("open", "handled", "false_alarm")


def normalize_remote(d) -> dict:
    """Fill missing keys and fix wrong values of Settings.remote in place; returns d."""
    if not isinstance(d, dict):
        d = {}
    for key, value in DEFAULTS.items():
        d.setdefault(key, copy.deepcopy(value))
    for key in ("enabled", "lan", "allow_actions"):
        d[key] = d[key] is True
    for key, lo, hi in (("port", 1024, 65535), ("live_fps", 0.5, 10.0), ("live_max_side", 320, 1920)):
        try:
            value = float(d[key])
            d[key] = value if lo <= value <= hi else DEFAULTS[key]
        except (TypeError, ValueError):
            d[key] = DEFAULTS[key]
    d["port"], d["live_max_side"] = int(d["port"]), int(d["live_max_side"])
    url = str(d.get("public_url") or "").strip().rstrip("/")
    d["public_url"] = url if url.startswith("https://") else ""
    devices = []
    for dev in (d["devices"] if isinstance(d["devices"], list) else []):
        if isinstance(dev, dict) and re.fullmatch(r"[0-9a-f]{64}", str(dev.get("token_sha256") or "")):
            devices.append({"id": str(dev.get("id") or new_device_id()), "name": clean_name(dev.get("name")),
                            "token_sha256": dev["token_sha256"], "created": float(dev.get("created") or 0.0),
                            "last_seen": float(dev.get("last_seen") or 0.0), "last_ip": str(dev.get("last_ip") or "")})
    d["devices"] = devices
    return d


def lan_ips() -> list[str]:
    """This PC's IPv4 addresses on the local network, the one with the default route first."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # UDP: nothing is sent
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if (addr.is_loopback or addr.is_link_local or addr.is_unspecified
                or addr in ipaddress.IPv4Network("100.64.0.0/10")):   # Tailscale addresses are not "home Wi-Fi"
            continue
        if ip not in out:
            out.append(ip)
    return out


def _bind_error(exc: OSError, port: int) -> str:
    code = getattr(exc, "winerror", None) or exc.errno
    if code in (10048, 98, 48):
        return f"Port {port} is used by another program. Choose another port."
    if code in (10013, 13):
        return f"Windows did not allow YardGuard to use port {port}. Choose another port."
    if code in (10049, 99):
        return "This PC has no network address to listen on."
    return f"Phone access could not start ({exc.__class__.__name__})."


def _shrink_jpeg(path: str, max_side: int) -> bytes | None:
    """Thumbnail: media.prepare_jpeg when the integrations core is there, else the same steps here."""
    try:
        from ..integrations.media import prepare_jpeg
    except ImportError:
        prepare_jpeg = None
    if prepare_jpeg is not None:
        try:
            return prepare_jpeg(path, max_side=max_side, quality=75, max_bytes=1_000_000)
        except Exception:
            log.debug("prepare_jpeg failed, using the fallback", exc_info=True)
    try:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return None
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return buf.tobytes() if ok else None


def inside(path: str, base) -> Path | None:
    """The resolved file when `path` is an existing file inside folder `base`, else None."""
    if not path or base is None:
        return None
    try:
        p = Path(path).resolve(strict=True)
        b = Path(base).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not p.is_file() or p == b or not p.is_relative_to(b):
        return None
    return p


def parse_range(header: str, size: int) -> tuple[int, int] | None | bool:
    """(start, end) inclusive for a "bytes=..." header; None = no/unsupported header (send all);
    False = unsatisfiable (416). Only the first range of a list is used."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if size <= 0:
        return False
    if not m.group(1):  # suffix: last N bytes
        n = int(m.group(2))
        if n <= 0:
            return False
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return False
    return start, min(end, size - 1)


class ChangeFeed:
    """Ring buffer of recent changes for the phones' long poll: {seq, type, id}."""

    def __init__(self, size: int = CHANGE_RING) -> None:
        self._cond = threading.Condition()
        self._items: deque = deque(maxlen=size)
        self._seq = 0
        self._closed = False

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def push(self, kind: str, item_id: str = "") -> None:
        with self._cond:
            self._seq += 1
            self._items.append({"seq": self._seq, "type": kind, "id": str(item_id or "")})
            self._cond.notify_all()

    def wait(self, since: int, timeout: float) -> dict:
        """Changes newer than `since`, waiting up to timeout s for one. reset: the phone's seq is
        unknown (YardGuard restarted): reload everything."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            if since > self._seq:
                return {"seq": self._seq, "items": [], "reset": True}
            while self._seq <= since and not self._closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            items = [i for i in self._items if i["seq"] > since]
            oldest = self._items[0]["seq"] if self._items else self._seq + 1
            return {"seq": self._seq, "items": items, "reset": since + 1 < oldest and since < self._seq}

    def open(self) -> None:
        with self._cond:
            self._closed = False

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class WebRemoteSignals(QObject):
    """Qt side of phone access (lives on the GUI thread). devices_changed / status_changed are for
    the UI; the private slots receive the app's events and do config saves on the GUI thread."""

    devices_changed = Signal()
    status_changed = Signal()
    _save = Signal()

    def __init__(self, owner: "WebRemote") -> None:
        super().__init__()
        self._owner = owner
        self._last_status = 0.0
        self._save.connect(self._do_save)

    @Slot()
    def _do_save(self) -> None:
        self._owner._save_now()

    @Slot(object)
    def on_incident(self, inc) -> None:
        self._owner.feed.push("incident", getattr(inc, "id", ""))

    @Slot(object)
    def on_area_event(self, ev) -> None:
        self._owner.feed.push("area_event", getattr(ev, "id", ""))

    @Slot(object)
    def on_status(self, _status) -> None:
        now = time.monotonic()
        if now - self._last_status >= 3.0:   # monitor status arrives often: phones refresh at most every 3 s
            self._last_status = now
            self._owner.feed.push("status")

    @Slot()
    def on_incidents_changed(self) -> None:
        self._owner.feed.push("incident")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False   # on Windows it would let us share a port another program uses

    def __init__(self, addr, handler, remote: "WebRemote") -> None:
        self.remote = remote
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(addr, handler, bind_and_activate=False)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        try:
            self.server_bind()
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def server_bind(self) -> None:
        """Like HTTPServer.server_bind without its reverse DNS lookup (slow on some networks)."""
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):   # too many open connections: drop this one
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        log.debug("Phone access connection error", exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    server_version = "YardGuard"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 60

    # -- plumbing -----------------------------------------------------------------------
    @property
    def remote(self) -> "WebRemote":
        return self.server.remote

    def log_message(self, fmt, *args) -> None:   # no request lines in the log (privacy)
        pass

    def send_error(self, code, message=None, explain=None) -> None:
        """Parse errors of the base class: plain JSON, never echoing the request."""
        try:
            self._json(int(code), {"error": "bad_request"})
        except OSError:
            pass
        self.close_connection = True

    def end_headers(self) -> None:
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        super().end_headers()

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data, headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", headers)

    def _err(self, code: int, error: str, message: str = "") -> None:
        self._json(code, {"error": error, "message": message} if message else {"error": error})

    def _query(self) -> dict:
        q = parse_qs(urlsplit(self.path).query, keep_blank_values=False, max_num_fields=20)
        return {k: v[0] for k, v in q.items() if v}

    def _peer(self) -> str:
        return str(self.client_address[0]) if self.client_address else ""

    def _from_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self._peer().split("%")[0]).is_loopback
        except ValueError:
            return False

    def client_key(self) -> str:
        user = self.headers.get("Tailscale-User-Login", "")
        if user and self._from_loopback():
            return "ts:" + user[:200]
        return "ip:" + self._peer()

    def _host(self) -> str:
        return str(self.headers.get("Host", "") or "").strip().lower()

    def _host_ok(self) -> bool:
        """Only names this PC really has (stops DNS-rebinding pages from reaching the server)."""
        host = self._host()
        if not host:
            return False
        if host.startswith("["):
            name = host[1:host.find("]")]
        elif host.count(":") == 1:
            name = host.split(":")[0]
        else:
            name = host
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            pass
        return name in self.remote.host_names() or name.endswith(".ts.net")

    def _same_origin(self, required: bool) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return not required
        try:
            netloc = urlsplit(origin).netloc.lower()
        except ValueError:
            return False
        return bool(netloc) and netloc == self._host()

    def _read_body(self) -> bool:
        """Read a POST body up front (so keep-alive stays in sync); False when it is too large."""
        self._raw = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return False
        if length:
            self._raw = self.rfile.read(length)
        return True

    def _body(self) -> dict | None:
        """The JSON object of the request (JSON content type only: plain cross-site forms cannot send it)."""
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json" or not getattr(self, "_raw", b""):
            return None
        try:
            data = json.loads(self._raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _device(self) -> dict | None:
        token = parse_cookie(self.headers.get("Cookie", ""), self.remote.cookie_name)
        dev = self.remote.authenticate(token, self._peer())
        return dev

    def _secure(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https" and self._from_loopback()

    # -- dispatch -----------------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            self._raw = b""
            if self.command == "POST" and not self._read_body():
                self._err(413, "too_large")
                return
            if not self._host_ok():
                self._err(403, "host")
                return
            path = urlsplit(self.path).path
            if self.command in ("GET", "HEAD") and path in STATIC:
                self._static(path)
                return
            if self.command == "POST" and path == "/api/pair":
                self._pair()
                return
            route = self._route(path)
            if route is None:
                self._err(404, "not_found")
                return
            dev = self._device()
            if dev is None:
                self._err(401, "pair")
                return
            route(dev)
        except (ConnectionError, TimeoutError, socket.timeout):
            self.close_connection = True
        except Exception:
            log.exception("Phone access request failed")
            self.close_connection = True
            try:
                self._err(500, "server", "Something went wrong in YardGuard.")
            except OSError:
                pass

    def _route(self, path: str):
        get = self.command in ("GET", "HEAD")
        post = self.command == "POST"
        m = None
        if get and path == "/api/status":
            return self._status
        if get and path == "/api/incidents":
            return self._incidents
        if get and (m := re.fullmatch(rf"/api/incidents/({ID_RE})", path)):
            return lambda dev, i=m.group(1): self._incident(dev, i)
        if post and (m := re.fullmatch(rf"/api/incidents/({ID_RE})/mark", path)):
            return lambda dev, i=m.group(1): self._mark(dev, i)
        if post and path == "/api/monitoring":
            return self._monitoring
        if post and path == "/api/logout":
            return self._logout
        if get and path == "/api/areas":
            return self._areas
        if get and path == "/api/area_events":
            return self._area_events
        if get and path == "/api/changes":
            return self._changes
        if get and (m := re.fullmatch(rf"/media/(alert|area)/({ID_RE})/(snapshot|crop|clip)", path)):
            return lambda dev, k=m.group(1), i=m.group(2), w=m.group(3): self._media(dev, k, i, w)
        if get and (m := re.fullmatch(r"/live/(\d{1,4})\.(mjpg|jpg)", path)):
            return lambda dev, ch=int(m.group(1)), f=m.group(2): self._live(dev, ch, f)
        return None

    # -- static -------------------------------------------------------------------------
    def _static(self, path: str) -> None:
        name, ctype, max_age = STATIC[path]
        try:
            body = (WEB_DIR / name).read_bytes()
        except OSError:
            self._err(404, "not_found")
            return
        self._send(200, body, ctype, cache=f"public, max-age={max_age}" if max_age else "no-cache")

    # -- pairing ------------------------------------------------------------------------
    def _pair(self) -> None:
        if not self._same_origin(required=False):
            self._err(403, "origin")
            return
        key = self.client_key()
        why = self.remote.limiter.check(key)
        if why:
            self._err(429, why, "Too many wrong codes. Wait 15 minutes, then make a new code on the PC."
                      if why == "locked" else "Too many tries. Wait a minute and try again.")
            return
        body = self._body()
        if body is None:
            self._err(400, "bad_request")
            return
        if not self.remote.codes.use(str(body.get("code") or "")):
            self.remote.limiter.failed(key)
            self._err(403, "code", "This code is wrong or has expired. Make a new one on the PC.")
            return
        self.remote.limiter.succeeded(key)
        token, dev = self.remote.add_device(body.get("name"), self._peer())
        cookie = (f"{self.remote.cookie_name}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"
                  + ("; Secure" if self._secure() else ""))
        self._json(200, {"ok": True, "device": {"id": dev["id"], "name": dev["name"]}}, {"Set-Cookie": cookie})

    def _logout(self, dev: dict) -> None:
        if not self._same_origin(required=False):
            self._err(403, "origin")
            return
        self.remote.remove_device(dev["id"])
        self._json(200, {"ok": True}, {"Set-Cookie": f"{self.remote.cookie_name}=; HttpOnly; SameSite=Strict; "
                                                     "Path=/; Max-Age=0"})

    # -- JSON ---------------------------------------------------------------------------
    def _status(self, dev: dict) -> None:
        r = self.remote
        self._json(200, api.status(r.ctx.cfg, r.store, getattr(r.ctx, "monitor", None),
                                   allow_actions=r.allow_actions, csrf=r.csrf.token(dev["id"]), device=dev,
                                   public_url=r.remote["public_url"]))

    @staticmethod
    def _float(value, default=None):
        try:
            v = float(value)
            return v if v == v and abs(v) < 1e12 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int(value, default: int, lo: int, hi: int) -> int:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return max(lo, min(hi, int(v)))

    def _incidents(self, dev: dict) -> None:
        q = self._query()
        status = "open" if q.get("status") == "open" else None
        store = self.remote.store
        if store is None:
            self._json(200, {"items": [], "more": False})
            return
        self._json(200, api.incidents(store, before=self._float(q.get("before")),
                                      limit=self._int(q.get("limit"), 30, 1, 100), status=status))

    def _incident(self, dev: dict, incident_id: str) -> None:
        store = self.remote.store
        data = api.incident_detail(store, incident_id) if store is not None else None
        if data is None:
            self._err(404, "not_found")
            return
        self._json(200, data)

    def _areas(self, dev: dict) -> None:
        self._json(200, api.areas(self.remote.ctx.cfg, self.remote.store))

    def _area_events(self, dev: dict) -> None:
        q = self._query()
        area_id = q.get("area") or None
        if area_id is not None and not re.fullmatch(ID_RE, area_id):
            self._err(404, "not_found")
            return
        store = self.remote.store
        if store is None:
            self._json(200, {"items": [], "more": False})
            return
        self._json(200, api.area_events(self.remote.ctx.cfg, store, area_id=area_id,
                                        before=self._float(q.get("before")),
                                        limit=self._int(q.get("limit"), 50, 1, 200)))

    def _changes(self, dev: dict) -> None:
        q = self._query()
        since = self._int(q.get("since"), 0, 0, 2**53)
        wait = min(LONG_POLL_MAX_S, max(0.0, self._float(q.get("wait"), LONG_POLL_MAX_S)))
        self._json(200, self.remote.feed.wait(since, wait))

    # -- actions ------------------------------------------------------------------------
    def _action_allowed(self, dev: dict) -> bool:
        if not self.remote.allow_actions:
            self._err(403, "actions_off", "Control from phones is turned off in YardGuard.")
            return False
        if not self._same_origin(required=True):
            self._err(403, "origin")
            return False
        if not self.remote.csrf.valid(dev["id"], self.headers.get("X-YG-CSRF", "")):
            self._err(403, "csrf")
            return False
        return True

    def _run_action(self, kind: str, args: dict, dev: dict) -> None:
        result = self.remote.request_action(kind, args, dev.get("name", ""))
        if "error" in result:
            self._json(result.pop("http", 503), result)
        elif result.get("ok") is False:
            self._err(409, "failed", str(result.get("message") or "That did not work. Try again."))
        else:
            self._json(200, result)

    def _mark(self, dev: dict, incident_id: str) -> None:
        if not self._action_allowed(dev):
            return
        body = self._body()
        status = str((body or {}).get("status") or "")
        if status not in INCIDENT_STATUSES:
            self._err(400, "bad_request")
            return
        store = self.remote.store
        if store is None or store.get_incident(incident_id) is None:
            self._err(404, "not_found")
            return
        self._run_action("mark_incident", {"incident_id": incident_id, "status": status}, dev)
        self.remote.feed.push("incident", incident_id)

    def _monitoring(self, dev: dict) -> None:
        if not self._action_allowed(dev):
            return
        body = self._body()
        if body is None or not isinstance(body.get("on"), bool):
            self._err(400, "bad_request")
            return
        self._run_action("set_monitoring", {"on": body["on"]}, dev)
        self.remote.feed.push("status")

    # -- media --------------------------------------------------------------------------
    def _media(self, dev: dict, kind: str, item_id: str, what: str) -> None:
        store = self.remote.store
        if store is None:
            self._err(404, "not_found")
            return
        item = store.get(item_id) if kind == "alert" else store.get_area_event(item_id)
        path = inside(str(getattr(item, f"{what}_path", "") or ""), getattr(store, "dir", None)) if item else None
        if path is None:
            self._err(404, "not_found")
            return
        if what == "clip":
            self._file_ranged(path, "video/mp4")
            return
        w = self._query().get("w")
        if w:
            side = self._int(w, 480, 64, 1600)
            body = self.remote.thumbnail(path, side)
            if body is None:
                self._err(404, "not_found")
                return
            self._send(200, body, "image/jpeg", cache="private, max-age=3600")
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._err(404, "not_found")
            return
        self._send(200, body, "image/jpeg", cache="private, max-age=3600")

    def _file_ranged(self, path: Path, ctype: str) -> None:
        try:
            f = path.open("rb")
        except OSError:
            self._err(404, "not_found")
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            rng = parse_range(self.headers.get("Range", ""), size)
            if rng is False:
                self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
                return
            start, end = (0, size - 1) if rng is None else rng
            length = max(0, end - start + 1)
            self.send_response(206 if rng is not None else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=3600")
            if rng is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            left = length
            while left > 0 and not self.remote.stopping:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
            if left > 0:
                self.close_connection = True

    # -- live ---------------------------------------------------------------------------
    def _live(self, dev: dict, channel: int, fmt: str) -> None:
        r = self.remote
        if channel not in {c["channel"] for c in api.camera_list(r.ctx.cfg, None)}:
            self._err(404, "not_found")
            return
        if not r.has_frames():
            self._err(503, "nopicture", "Live pictures are not available.")
            return
        if fmt == "jpg":
            enc = r.live.encoder(channel)
            jpeg = enc.snapshot(8.0) if enc is not None else None
            if jpeg is None:
                self._err(503, "nopicture", "No picture from this camera right now.")
                return
            self._send(200, jpeg, "image/jpeg")
            return
        enc = r.live.acquire(channel)
        if enc is None:
            self._err(503, "busy", "Too many people are watching. Try again later.")
            return
        r.track_stream(self.connection, True)
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ygframe")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            seq = 0
            started = time.monotonic()
            next_check = started + 5.0
            while not r.stopping and time.monotonic() - started < r.live_max_s:
                got = enc.wait_frame(seq, 1.0)
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + 5.0
                    if not r.device_valid(dev["id"]):
                        break
                if got is None:
                    if not enc.alive:
                        break
                    continue
                seq, jpeg = got
                self.wfile.write(b"--ygframe\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg)
                                 + jpeg + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"--ygframe--\r\n")
        finally:
            r.live.release(enc)
            r.track_stream(self.connection, False)


class WebRemote:
    """Phone access server. Created on the GUI thread with the app context and the IntegrationHub
    (for actions and live frames); nothing listens until start() with remote.enabled on."""

    def __init__(self, ctx, hub) -> None:
        self.ctx = ctx
        self.hub = hub
        self.changed = None            # optional callable(), called from any thread after device/status changes
        self.running = False
        self.error = ""
        self.stopping = False
        self.live_max_s = LIVE_MAX_S
        self.codes = PairingCodes()
        self.limiter = PairLimiter()
        self.csrf = Csrf()
        self.feed = ChangeFeed()
        self.signals = WebRemoteSignals(self)
        self._lock = threading.RLock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._bound: tuple[str, int] | None = None
        self._devices: list[dict] = [dict(d) for d in self.remote["devices"]]
        self._seen_saved: dict[str, float] = {}
        self._streams: set = set()
        self._thumbs: OrderedDict = OrderedDict()
        self.live = LiveHub(self._frame, lambda: self.remote["live_fps"], lambda: self.remote["live_max_side"])
        self._connect()

    # -- config -------------------------------------------------------------------------
    @property
    def remote(self) -> dict:
        """Settings.remote, normalised the first time a (new) dict is seen."""
        s = self.ctx.cfg.settings
        cur = getattr(s, "remote", None)
        if not isinstance(cur, dict) or id(cur) != getattr(self, "_normalized", None):
            cur = normalize_remote(cur if isinstance(cur, dict) else {})
            s.remote = cur
            self._normalized = id(cur)
        return cur

    @property
    def allow_actions(self) -> bool:
        return bool(self.remote.get("allow_actions"))

    @property
    def cookie_name(self) -> str:
        iid = re.sub(r"[^A-Za-z0-9]", "", str(getattr(self.ctx.cfg.settings, "install_id", "") or ""))[:16]
        return f"yg_{iid or 'remote'}"

    @property
    def store(self):
        return getattr(self.ctx, "alert_store", None)

    def _connect(self) -> None:
        bridge = getattr(self.ctx, "alert_bridge", None)
        sig = self.signals
        if bridge is not None:
            bridge.incident_added.connect(sig.on_incident)
            bridge.incident_updated.connect(sig.on_incident)
            bridge.area_event_added.connect(sig.on_area_event)
            bridge.status_changed.connect(sig.on_status)
        changed = getattr(self.hub, "incidents_changed", None)
        if changed is not None and hasattr(changed, "connect"):
            changed.connect(sig.on_incidents_changed)

    # -- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        """Listen when phone access is on (no-op otherwise or when already running)."""
        rem = self.remote
        if not rem["enabled"] or self.running:
            return
        host = "0.0.0.0" if rem["lan"] else "127.0.0.1"
        port = rem["port"]
        self.stopping = False
        self.feed.open()
        self.live.open()
        try:
            server = _Server((host, port), _Handler, self)
        except OSError as exc:
            self.error = _bind_error(exc, port)
            log.warning("Phone access could not start on port %s: %s", port, exc.__class__.__name__)
            self._notify_status()
            return
        self._server = server
        self._bound = (host, port)
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="yg-web", daemon=True)
        self._thread.start()
        self.running, self.error = True, ""
        log.info("Phone access is on (%s, port %s)", "home network" if rem["lan"] else "this PC only", port)
        self._notify_status()

    def stop(self) -> None:
        """Stop listening and end open streams; returns within about 2 s."""
        self.stopping = True
        self.feed.close()
        server, thread = self._server, self._thread
        self._server, self._thread, self._bound = None, None, None
        with self._lock:
            streams = list(self._streams)
        for conn in streams:   # unblock live streams waiting on slow phones
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.live.close(1.0)
        if server is not None:
            try:
                server.shutdown()
            finally:
                server.server_close()
        if thread is not None:
            thread.join(1.0)
        was = self.running
        self.running = False
        if was:
            log.info("Phone access is off")
            self._notify_status()

    def restart(self) -> None:
        self.stop()
        self.error = ""
        self.start()

    def apply_settings(self) -> None:
        """After the UI changed Settings.remote: start, stop or rebind as needed (GUI thread)."""
        rem = normalize_remote(self.remote)
        want = ("0.0.0.0" if rem["lan"] else "127.0.0.1", rem["port"])
        if not rem["enabled"]:
            self.stop()
            self.error = ""
            self._notify_status()
        elif not self.running or self._bound != want:
            self.restart()

    def _notify_status(self) -> None:
        try:
            self.signals.status_changed.emit()
        except RuntimeError:
            pass
        self._call_changed()

    def _call_changed(self) -> None:
        if callable(self.changed):
            try:
                self.changed()
            except Exception:
                log.debug("Phone access change hook failed", exc_info=True)

    # -- addresses ----------------------------------------------------------------------
    def urls(self) -> list[str]:
        rem = self.remote
        out = []
        if rem["public_url"]:
            out.append(rem["public_url"])
        if rem["lan"]:
            out += [f"http://{ip}:{rem['port']}" for ip in lan_ips()]
        out.append(f"http://127.0.0.1:{rem['port']}")
        return out

    def host_names(self) -> set[str]:
        names = {"localhost"}
        try:
            hn = socket.gethostname().lower()
            names |= {hn, hn + ".local", hn + ".lan", hn + ".home"}
        except OSError:
            pass
        pub = self.remote["public_url"]
        if pub:
            names.add((urlsplit(pub).hostname or "").lower())
        return names

    def set_public_url(self, url: str) -> None:
        """Remember (or forget with "") the Tailscale https address; GUI thread."""
        url = str(url or "").strip().rstrip("/")
        self.remote["public_url"] = url if url.startswith("https://") else ""
        self._save_now()
        self._notify_status()

    # -- pairing and devices --------------------------------------------------------------
    def new_pairing(self, ttl: float = 300) -> tuple[str, str]:
        """A one-time code (shown as "K7P2-9QXM") and the link with it in the fragment."""
        code = self.codes.new(ttl)
        return format_code(code), f"{self.pair_base()}/#pair={code}"

    def cancel_pairing(self) -> None:
        self.codes.clear()

    def pair_base(self) -> str:
        rem = self.remote
        if rem["public_url"]:
            return rem["public_url"]
        if rem["lan"]:
            ips = lan_ips()
            if ips:
                return f"http://{ips[0]}:{rem['port']}"
        return f"http://127.0.0.1:{rem['port']}"

    def add_device(self, name, ip: str) -> tuple[str, dict]:
        """Any thread: a new device with a fresh token (returned once, never stored)."""
        token = new_token()
        now = time.time()
        dev = {"id": new_device_id(), "name": clean_name(name), "token_sha256": hash_token(token),
               "created": now, "last_seen": now, "last_ip": str(ip)[:64]}
        with self._lock:
            self._devices.append(dev)
            self._seen_saved[dev["id"]] = time.monotonic()
        log.info("Phone access: a phone was paired")
        self._request_save()
        self._devices_changed()
        return token, dict(dev)

    def devices(self) -> list[dict]:
        """Paired devices (no token hashes), newest first."""
        with self._lock:
            out = [{k: v for k, v in d.items() if k != "token_sha256"} for d in self._devices]
        return sorted(out, key=lambda d: d.get("created", 0.0), reverse=True)

    def remove_device(self, device_id: str) -> None:
        with self._lock:
            before = len(self._devices)
            self._devices = [d for d in self._devices if d["id"] != device_id]
            removed = len(self._devices) != before
            self._seen_saved.pop(device_id, None)
        if removed:
            log.info("Phone access: a phone was removed")
            self._request_save()
            self._devices_changed()

    def rename_device(self, device_id: str, name: str) -> None:
        with self._lock:
            for d in self._devices:
                if d["id"] == device_id:
                    d["name"] = clean_name(name, d["name"])
        self._request_save()
        self._devices_changed()

    def device_valid(self, device_id: str) -> bool:
        with self._lock:
            return any(d["id"] == device_id for d in self._devices)

    def authenticate(self, token: str, ip: str) -> dict | None:
        """The device for this cookie token (and remembers it was seen), else None."""
        with self._lock:
            dev = find_device(self._devices, token)
            if dev is None:
                return None
            now = time.time()
            dev["last_seen"], dev["last_ip"] = now, str(ip)[:64]
            due = time.monotonic() - self._seen_saved.get(dev["id"], -1e9) >= SEEN_SAVE_EVERY_S
            if due:
                self._seen_saved[dev["id"]] = time.monotonic()
            result = dict(dev)
        if due:
            self._request_save()
        return result

    def connected_count(self) -> int:
        """Devices that used phone access in the last 2 minutes."""
        now = time.time()
        with self._lock:
            return sum(1 for d in self._devices if now - d.get("last_seen", 0.0) <= CONNECTED_WINDOW_S)

    def _devices_changed(self) -> None:
        try:
            self.signals.devices_changed.emit()
        except RuntimeError:
            pass
        self._call_changed()

    def _request_save(self) -> None:
        try:
            self.signals._save.emit()   # queued to the GUI thread when emitted elsewhere
        except RuntimeError:
            pass

    def _save_now(self) -> None:
        """GUI thread: copy the devices into the config and save it."""
        with self._lock:
            self.remote["devices"] = [dict(d) for d in self._devices]
        try:
            self.ctx.cfg.save()
        except Exception as exc:
            log.warning("Saving phone access settings failed: %s", exc.__class__.__name__)

    # -- helpers for the handler ----------------------------------------------------------
    def notify_change(self, kind: str, item_id: str = "") -> None:
        """Any thread: tell open phone pages that something changed ("incident", "area_event", "status")."""
        self.feed.push(kind, item_id)

    def _frame(self, channel: int):
        frames = getattr(self.hub, "frames", None)
        return frames.latest(channel) if frames is not None else None

    def has_frames(self) -> bool:
        return getattr(self.hub, "frames", None) is not None

    def track_stream(self, conn, add: bool) -> None:
        with self._lock:
            (self._streams.add if add else self._streams.discard)(conn)

    def thumbnail(self, path: Path, side: int) -> bytes | None:
        try:
            st = path.stat()
        except OSError:
            return None
        key = (str(path), st.st_mtime_ns, st.st_size, side)
        with self._lock:
            hit = self._thumbs.get(key)
            if hit is not None:
                self._thumbs.move_to_end(key)
                return hit
        body = _shrink_jpeg(str(path), side)
        if body is not None:
            with self._lock:
                self._thumbs[key] = body
                while len(self._thumbs) > THUMB_CACHE:
                    self._thumbs.popitem(last=False)
        return body

    def request_action(self, kind: str, args: dict, user: str) -> dict:
        """Run an action through the hub (GUI thread) and wait for its answer (any thread)."""
        request = getattr(self.hub, "request", None)
        if request is None:
            return {"error": "unavailable", "message": "Not available right now.", "http": 503}
        try:
            from ..integrations.model import Action
        except ImportError:
            return {"error": "unavailable", "message": "Not available right now.", "http": 503}
        try:
            fut = request(Action(kind, dict(args), "Phone", user=str(user or "")))
            result = fut.result(timeout=ACTION_TIMEOUT_S)
        except TimeoutError:
            return {"error": "timeout", "message": "YardGuard did not answer in time. Try again.", "http": 504}
        except Exception as exc:
            log.warning("Phone action %s failed: %s", kind, exc.__class__.__name__)
            return {"error": "failed", "message": "That did not work. Try again.", "http": 500}
        if not isinstance(result, dict):
            result = {"ok": bool(result)}
        return {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool, type(None), list, dict))}
