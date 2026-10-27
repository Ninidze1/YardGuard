"""Shared local fakes for the integrations tests (nothing here talks to a real service).

- FakeTelegram: a Telegram Bot API emulation over http.server (getUpdates long poll, send*, edit*,
  error injection: 401 / 403 / 409 / 429 retry_after / 400 migrate_to_chat_id / not modified).
- FakeHttpSink: records any HTTP request and answers with programmable statuses (ntfy, Discord,
  webhook).
- FakeSmtp: minimal SMTP server (EHLO, AUTH PLAIN/LOGIN, MAIL, RCPT, DATA, QUIT; no TLS).
- FakeMqttBroker: minimal MQTT 3.1.1 broker (CONNECT with user/password check, PUBLISH QoS 0/1/2,
  SUBSCRIBE with wildcards, retained messages, PINGREQ, DISCONNECT, last will on a dropped socket).
- FakeCtx: an AppContext-like object (real AlertBridge from gui.py, real AlertStore in a test
  folder, a monitor stub with fake camera sources), plus make_config / make_incident helpers.

Every server binds 127.0.0.1 on an OS-chosen port and runs on daemon threads named "fake-*".
Import this module only AFTER setting YARDGUARD_CONFIG to a test path.
"""
from __future__ import annotations

import base64
import itertools
import json
import socket
import socketserver
import struct
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np


class _QuietHTTPServer(ThreadingHTTPServer):
    """No traceback when a client gives up (timeouts are part of the tests)."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def _serve(server, name: str) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, name=name, daemon=True)
    t.start()
    return t


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, dict]:
    """(fields {name: str}, files {name: {"filename", "content_type", "data"}}) of a multipart body."""
    fields, files = {}, {}
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1].strip('"')
    if not boundary:
        return fields, files
    delim = b"--" + boundary.encode("latin-1")
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        head, _, data = chunk.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        params = {}
        for item in disp.split(";")[1:]:
            if "=" in item:
                k, v = item.strip().split("=", 1)
                params[k.lower()] = v.strip('"')
        name = params.get("name", "")
        if "filename" in params:
            files[name] = {"filename": params["filename"], "content_type": headers.get("content-type", ""),
                           "data": data}
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


# --- Telegram ------------------------------------------------------------------------------------

class FakeTelegram:
    """Bot API at http://127.0.0.1:<port>/bot<token>/<method>. Records every call in .calls:
    {"method", "token", "params", "files", "ts"}. Inject updates with push_update / push_message /
    push_callback; inject errors with fail(method, code, description, parameters, times)."""

    BOT = {"id": 7000000001, "is_bot": True, "first_name": "Yard Bot", "username": "MyYardBot"}

    def __init__(self, token: str = "7000000001:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ") -> None:
        self.token = token
        self.calls: list[dict] = []
        self.updates: list[dict] = []
        self._update_id = itertools.count(1000)
        self._msg_id = itertools.count(1)
        self._cond = threading.Condition()
        self._errors: dict[str, deque] = {}
        self.not_modified = False        # edit* answer 400 "message is not modified"
        self.max_poll_s = 30.0
        self._closed = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def _handle(self):
                parts = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                status, payload = fake._dispatch(parts.path, parts.query, self.headers.get("Content-Type", ""), body)
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = _QuietHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = _serve(self.server, "fake-telegram")

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=3)

    # -- injection --
    def fail(self, method: str, code: int, description: str = "", parameters: dict | None = None,
             times: int = 1) -> None:
        """The next `times` calls of `method` ("*" = any) answer with this error."""
        q = self._errors.setdefault(method, deque())
        for _ in range(times):
            q.append((code, description or {401: "Unauthorized", 403: "Forbidden: bot was blocked by the user",
                                             409: "Conflict: terminated by other getUpdates request",
                                             429: "Too Many Requests: retry after 1"}.get(code, "Bad Request"),
                      parameters))

    def push_update(self, update: dict) -> dict:
        with self._cond:
            update = dict(update)
            update["update_id"] = next(self._update_id)
            self.updates.append(update)
            self._cond.notify_all()
        return update

    def push_message(self, chat_id: int, text: str, chat_type: str = "private", first_name: str = "Alex",
                     title: str = "", date: float | None = None) -> dict:
        chat = {"id": chat_id, "type": chat_type}
        if chat_type == "private":
            chat["first_name"] = first_name
        else:
            chat["title"] = title or "Family"
        msg = {"message_id": next(self._msg_id), "date": int(date if date is not None else time.time()),
               "chat": chat, "from": {"id": abs(chat_id) % 10_000_000 + 1, "is_bot": False, "first_name": first_name},
               "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return self.push_update({"message": msg})

    def push_callback(self, chat_id: int, data: str, message_id: int, first_name: str = "Alex",
                      date: float | None = None) -> dict:
        msg = {"message_id": message_id, "date": int(date if date is not None else time.time()),
               "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}}
        cq = {"id": uuid.uuid4().hex[:16], "from": {"id": 42, "is_bot": False, "first_name": first_name},
              "message": msg, "chat_instance": "1", "data": data}
        return self.push_update({"callback_query": cq})

    # -- inspection --
    def calls_of(self, method: str) -> list[dict]:
        return [c for c in list(self.calls) if c["method"] == method]

    def wait_for(self, method: str, n: int = 1, timeout: float = 5.0) -> list[dict]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = self.calls_of(method)
            if len(got) >= n:
                return got
            time.sleep(0.02)
        return self.calls_of(method)

    # -- the API --
    def _params(self, query: str, ctype: str, body: bytes) -> tuple[dict, dict]:
        params: dict = {k: v[-1] for k, v in parse_qs(query).items()}
        files: dict = {}
        if body:
            if ctype.startswith("application/json"):
                try:
                    params.update(json.loads(body.decode("utf-8")))
                except ValueError:
                    pass
            elif ctype.startswith("multipart/form-data"):
                f, files = parse_multipart(body, ctype)
                params.update(f)
            else:
                params.update({k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace")).items()})
        for k, v in list(params.items()):
            if isinstance(v, str) and v[:1] in "[{":
                try:
                    params[k] = json.loads(v)
                except ValueError:
                    pass
        return params, files

    def _dispatch(self, path: str, query: str, ctype: str, body: bytes) -> tuple[int, dict]:
        segs = path.strip("/").split("/")
        if len(segs) != 2 or not segs[0].startswith("bot"):
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}
        token, method = segs[0][3:], segs[1]
        params, files = self._params(query, ctype, body)
        self.calls.append({"method": method, "token": token, "params": params, "files": files, "ts": time.time()})
        if token != self.token:
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        for key in (method, "*"):
            q = self._errors.get(key)
            if q:
                code, desc, parameters = q.popleft()
                out = {"ok": False, "error_code": code, "description": desc}
                if parameters:
                    out["parameters"] = parameters
                elif code == 429:
                    out["parameters"] = {"retry_after": 1}
                return code, out
        fn = getattr(self, "_m_" + method, None)
        if fn is None:
            return 404, {"ok": False, "error_code": 404, "description": "Not Found: method not found"}
        result = fn(params, files)
        if isinstance(result, tuple):
            return result
        return 200, {"ok": True, "result": result}

    def _message(self, params: dict, **extra) -> dict:
        chat_id = params.get("chat_id")
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            pass
        msg = {"message_id": next(self._msg_id), "date": int(time.time()), "chat": {"id": chat_id}}
        msg.update(extra)
        return msg

    def _m_getMe(self, p, f):
        return dict(self.BOT)

    def _m_deleteWebhook(self, p, f):
        return True

    def _m_setMyCommands(self, p, f):
        return True

    def _m_answerCallbackQuery(self, p, f):
        return True

    def _m_getUpdates(self, p, f):
        offset = int(p.get("offset") or 0)
        timeout = min(float(p.get("timeout") or 0), self.max_poll_s)
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                if offset < 0:
                    out = self.updates[offset:]
                else:
                    out = [u for u in self.updates if u["update_id"] >= offset]
                if out or self._closed:
                    return out
                left = end - time.monotonic()
                if left <= 0:
                    return []
                self._cond.wait(min(left, 0.2))

    def _m_sendMessage(self, p, f):
        return self._message(p, text=p.get("text", ""))

    def _m_sendPhoto(self, p, f):
        return self._message(p, caption=p.get("caption", ""), photo=[{"file_id": "ph" + uuid.uuid4().hex[:6]}])

    def _m_sendVideo(self, p, f):
        return self._message(p, caption=p.get("caption", ""), video={"file_id": "vd" + uuid.uuid4().hex[:6]})

    def _m_sendMediaGroup(self, p, f):
        media = p.get("media") or []
        return [self._message(p, caption=m.get("caption", "") if isinstance(m, dict) else "") for m in media]

    def _edit(self, p, **extra):
        if self.not_modified:
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}
        try:
            mid = int(p.get("message_id"))
        except (TypeError, ValueError):
            mid = 0
        msg = self._message(p, **extra)
        msg["message_id"] = mid
        return msg

    def _m_editMessageCaption(self, p, f):
        return self._edit(p, caption=p.get("caption", ""))

    def _m_editMessageText(self, p, f):
        return self._edit(p, text=p.get("text", ""))

    def _m_editMessageMedia(self, p, f):
        return self._edit(p)

    def _m_editMessageReplyMarkup(self, p, f):
        return self._edit(p)


# --- generic HTTP sink ----------------------------------------------------------------------------------

class FakeHttpSink:
    """Records every request: {"method", "path", "query", "headers", "body"}; answers with the queued
    responses (respond()) or the default 200 {"ok": true}."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._responses: deque = deque()
        self.default = (200, {"Content-Type": "application/json"}, b'{"ok": true}')
        self.delay_s = 0.0
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                parts = urlsplit(self.path)
                sink.requests.append({"method": self.command, "path": parts.path, "query": parts.query,
                                      "headers": {k: v for k, v in self.headers.items()}, "body": body,
                                      "ts": time.time()})
                if sink.delay_s:
                    time.sleep(sink.delay_s)
                status, headers, data = sink._responses.popleft() if sink._responses else sink.default
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)
                except OSError:
                    pass

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle

        self.server = _QuietHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = _serve(self.server, "fake-http-sink")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def respond(self, status: int, body=b"", headers: dict | None = None, times: int = 1) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        elif isinstance(body, str):
            body = body.encode()
        for _ in range(times):
            self._responses.append((status, headers or {}, body))

    def wait_for(self, n: int = 1, timeout: float = 5.0) -> list[dict]:
        end = time.monotonic() + timeout
        while time.monotonic() < end and len(self.requests) < n:
            time.sleep(0.02)
        return list(self.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=3)


# --- SMTP ---------------------------------------------------------------------------------------------------

class FakeSmtp:
    """SMTP on 127.0.0.1 (no TLS: tests use security "none"). users: {username: password} (None =
    any login accepted); fail_auth: always 535; reject_rcpt: addresses answered with 550.
    .messages: [{"from", "to", "data" (bytes), "user"}]."""

    def __init__(self, users: dict | None = None) -> None:
        self.users = users
        self.fail_auth = False
        self.reject_rcpt: set[str] = set()
        self.messages: list[dict] = []
        self.logins: list[str] = []
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def _send(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode())
                self.wfile.flush()

            def _read(self) -> str | None:
                raw = self.rfile.readline(65536)
                if not raw:
                    return None
                return raw.decode("utf-8", "replace").rstrip("\r\n")

            def _auth(self, user: str, password: str) -> bool:
                if fake.fail_auth:
                    return False
                if fake.users is None:
                    return True
                return fake.users.get(user) == password

            def handle(self):
                self._send("220 fake.smtp ESMTP ready")
                user, mail_from, rcpts = "", "", []
                while True:
                    line = self._read()
                    if line is None:
                        return
                    cmd = line.split(" ", 1)[0].upper()
                    arg = line[len(cmd):].strip()
                    if cmd == "EHLO":
                        self.wfile.write(b"250-fake.smtp\r\n250-AUTH PLAIN LOGIN\r\n250-SIZE 35000000\r\n250 8BITMIME\r\n")
                        self.wfile.flush()
                    elif cmd == "HELO":
                        self._send("250 fake.smtp")
                    elif cmd == "AUTH":
                        mech, _, rest = arg.partition(" ")
                        mech = mech.upper()
                        if mech == "PLAIN":
                            if not rest:
                                self._send("334 ")
                                rest = self._read() or ""
                            try:
                                _, u, p = base64.b64decode(rest).decode("utf-8").split("\0")
                            except Exception:
                                self._send("501 5.5.2 Bad encoding")
                                continue
                        elif mech == "LOGIN":
                            if rest:
                                u = base64.b64decode(rest).decode("utf-8", "replace")
                            else:
                                self._send("334 VXNlcm5hbWU6")
                                u = base64.b64decode(self._read() or "").decode("utf-8", "replace")
                            self._send("334 UGFzc3dvcmQ6")
                            p = base64.b64decode(self._read() or "").decode("utf-8", "replace")
                        else:
                            self._send("504 5.5.4 Unrecognized authentication type")
                            continue
                        if self._auth(u, p):
                            user = u
                            fake.logins.append(u)
                            self._send("235 2.7.0 Authentication successful")
                        else:
                            self._send("535 5.7.8 Username and Password not accepted")
                    elif cmd == "MAIL":
                        mail_from, rcpts = arg.split(":", 1)[-1].strip().strip("<>").split(">")[0], []
                        self._send("250 2.1.0 OK")
                    elif cmd == "RCPT":
                        addr = arg.split(":", 1)[-1].strip().strip("<>").split(">")[0]
                        if addr in fake.reject_rcpt:
                            self._send("550 5.1.1 No such user")
                        else:
                            rcpts.append(addr)
                            self._send("250 2.1.5 OK")
                    elif cmd == "DATA":
                        self._send("354 End data with <CR><LF>.<CR><LF>")
                        chunks = []
                        while True:
                            raw = self.rfile.readline(1 << 20)
                            if not raw or raw in (b".\r\n", b".\n"):
                                break
                            if raw.startswith(b".."):
                                raw = raw[1:]
                            chunks.append(raw)
                        fake.messages.append({"from": mail_from, "to": list(rcpts), "data": b"".join(chunks),
                                              "user": user})
                        self._send("250 2.0.0 OK queued")
                    elif cmd == "RSET":
                        mail_from, rcpts = "", []
                        self._send("250 2.0.0 OK")
                    elif cmd == "NOOP":
                        self._send("250 2.0.0 OK")
                    elif cmd == "QUIT":
                        self._send("221 2.0.0 Bye")
                        return
                    else:
                        self._send("502 5.5.2 Command not implemented")

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self._thread = _serve(self.server, "fake-smtp")

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=3)


# --- MQTT ---------------------------------------------------------------------------------------------------

def topic_matches(filt: str, topic: str) -> bool:
    """MQTT wildcard match (+ one level, # the rest)."""
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


def _enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


class _MqttClient:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.client_id = ""
        self.subs: dict[str, int] = {}
        self.will: tuple | None = None
        self.clean = False
        self.lock = threading.Lock()
        self._pid = itertools.count(1)

    def send(self, data: bytes) -> None:
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass


class FakeMqttBroker:
    """MQTT 3.1.1 broker on 127.0.0.1. users: {username: password} (None = anonymous allowed).
    .published: [{"topic", "payload" (bytes), "retain", "qos", "client"}] (will messages too, with
    client "will:<id>"); .retained: {topic: payload}; .connects: [{"client_id", "username", "rc"}]."""

    def __init__(self, users: dict | None = None) -> None:
        self.users = users
        self.published: list[dict] = []
        self.retained: dict[str, bytes] = {}
        self.connects: list[dict] = []
        self.clients: list[_MqttClient] = []
        self._lock = threading.Lock()
        broker = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                broker._serve_client(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self._thread = _serve(self.server, "fake-mqtt")

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self) -> None:
        for c in list(self.clients):
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=3)

    # -- inspection / injection --
    def messages(self, topic_filter: str = "#") -> list[dict]:
        return [m for m in list(self.published) if topic_matches(topic_filter, m["topic"])]

    def wait_for(self, topic_filter: str, n: int = 1, timeout: float = 5.0, payload: bytes | None = None) -> list[dict]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = [m for m in self.messages(topic_filter) if payload is None or m["payload"] == payload]
            if len(got) >= n:
                return got
            time.sleep(0.02)
        return [m for m in self.messages(topic_filter) if payload is None or m["payload"] == payload]

    def publish(self, topic: str, payload, retain: bool = False) -> None:
        """Inject a message as if another client published it (e.g. homeassistant/status online)."""
        data = payload.encode() if isinstance(payload, str) else bytes(payload)
        self._route(topic, data, retain, 0, "broker")

    def kill_client(self, client_id: str) -> None:
        """Drop a client's socket without DISCONNECT: its last will is published."""
        for c in list(self.clients):
            if c.client_id == client_id:
                try:
                    c.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def connected_ids(self) -> list[str]:
        return [c.client_id for c in list(self.clients)]

    # -- protocol --
    def _route(self, topic: str, payload: bytes, retain: bool, qos: int, client: str) -> None:
        with self._lock:
            self.published.append({"topic": topic, "payload": payload, "retain": retain, "qos": qos, "client": client})
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            targets = [c for c in self.clients if any(topic_matches(f, topic) for f in c.subs)]
        pkt = self._publish_packet(topic, payload, retain=False)
        for c in targets:
            c.send(pkt)

    @staticmethod
    def _publish_packet(topic: str, payload: bytes, retain: bool) -> bytes:
        body = _enc_str(topic) + payload
        return bytes([0x30 | (1 if retain else 0)]) + _enc_len(len(body)) + body

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _read_packet(self, sock) -> tuple[int, int, bytes] | None:
        first = self._recv_exact(sock, 1)
        if first is None:
            return None
        mult, length = 1, 0
        while True:
            b = self._recv_exact(sock, 1)
            if b is None:
                return None
            length += (b[0] & 0x7F) * mult
            if not b[0] & 0x80:
                break
            mult *= 128
        body = self._recv_exact(sock, length) if length else b""
        if body is None:
            return None
        return first[0] >> 4, first[0] & 0x0F, body

    @staticmethod
    def _str(body: bytes, i: int) -> tuple[str, int]:
        n = struct.unpack("!H", body[i:i + 2])[0]
        return body[i + 2:i + 2 + n].decode("utf-8", "replace"), i + 2 + n

    @staticmethod
    def _bytes(body: bytes, i: int) -> tuple[bytes, int]:
        n = struct.unpack("!H", body[i:i + 2])[0]
        return body[i + 2:i + 2 + n], i + 2 + n

    def _serve_client(self, sock: socket.socket) -> None:
        c = _MqttClient(sock)
        graceful = False
        connected = False
        try:
            while True:
                pkt = self._read_packet(sock)
                if pkt is None:
                    break
                ptype, flags, body = pkt
                if ptype == 1:      # CONNECT
                    _proto, i = self._str(body, 0)
                    level, cflags = body[i], body[i + 1]
                    i += 4          # level, flags, keepalive
                    c.client_id, i = self._str(body, i)
                    if cflags & 0x04:
                        wt, i = self._str(body, i)
                        wm, i = self._bytes(body, i)
                        c.will = (wt, wm, bool(cflags & 0x20), (cflags >> 3) & 0x03)
                    user = pw = None
                    if cflags & 0x80:
                        user, i = self._str(body, i)
                    if cflags & 0x40:
                        pwb, i = self._bytes(body, i)
                        pw = pwb.decode("utf-8", "replace")
                    rc = 0
                    if level not in (3, 4):
                        rc = 1
                    elif self.users is not None and (user is None or self.users.get(user) != pw):
                        rc = 4 if user is not None else 5
                    self.connects.append({"client_id": c.client_id, "username": user, "rc": rc})
                    c.send(bytes([0x20, 2, 0, rc]))
                    if rc:
                        break
                    connected = True
                    with self._lock:
                        self.clients.append(c)
                elif ptype == 3:    # PUBLISH
                    qos, retain = (flags >> 1) & 0x03, bool(flags & 0x01)
                    topic, i = self._str(body, 0)
                    pid = None
                    if qos:
                        pid = struct.unpack("!H", body[i:i + 2])[0]
                        i += 2
                    self._route(topic, body[i:], retain, qos, c.client_id)
                    if qos == 1:
                        c.send(bytes([0x40, 2]) + struct.pack("!H", pid))
                    elif qos == 2:
                        c.send(bytes([0x50, 2]) + struct.pack("!H", pid))
                elif ptype == 6:    # PUBREL
                    c.send(bytes([0x70, 2]) + body[:2])
                elif ptype in (4, 5, 7):   # PUBACK / PUBREC / PUBCOMP from the client
                    if ptype == 5:
                        c.send(bytes([0x62, 2]) + body[:2])
                elif ptype == 8:    # SUBSCRIBE
                    pid = body[:2]
                    i, granted, filters = 2, [], []
                    while i < len(body):
                        f, i = self._str(body, i)
                        q = body[i]
                        i += 1
                        c.subs[f] = q
                        granted.append(min(q, 1))
                        filters.append(f)
                    c.send(bytes([0x90]) + _enc_len(2 + len(granted)) + pid + bytes(granted))
                    with self._lock:
                        retained = [(t, p) for t, p in self.retained.items() if any(topic_matches(f, t) for f in filters)]
                    for t, p in retained:
                        c.send(self._publish_packet(t, p, retain=True))
                elif ptype == 10:   # UNSUBSCRIBE
                    pid = body[:2]
                    i = 2
                    while i < len(body):
                        f, i = self._str(body, i)
                        c.subs.pop(f, None)
                    c.send(bytes([0xB0, 2]) + pid)
                elif ptype == 12:   # PINGREQ
                    c.send(bytes([0xD0, 0]))
                elif ptype == 14:   # DISCONNECT
                    graceful = True
                    break
        finally:
            with self._lock:
                if c in self.clients:
                    self.clients.remove(c)
            if connected and not graceful and c.will is not None:
                wt, wm, wr, wq = c.will
                self._route(wt, wm, wr, wq, "will:" + c.client_id)
            try:
                sock.close()
            except OSError:
                pass


# --- app context ------------------------------------------------------------------------------------------------

class FakeSource:
    """A camera source like stream.FrameSource: latest() -> (frame, time.time())."""

    def __init__(self, url: str = "", fps: float = 2, long_side: int = 960, gpu_decode: bool = False,
                 color=(60, 120, 60), size=(720, 1280)) -> None:
        self.url, self.fps, self.long_side = url, fps, long_side
        self.started = self.stopped = False
        self.frame = np.zeros((size[0], size[1], 3), np.uint8)
        self.frame[:] = color
        cv2.putText(self.frame, "fake", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 4)
        self.fresh = True
        self.delay_s = 0.0
        self._t0 = time.monotonic()

    def start(self) -> None:
        self.started = True
        self._t0 = time.monotonic()

    def stop(self) -> None:
        self.stopped = True

    def latest(self):
        if not self.started or time.monotonic() - self._t0 < self.delay_s:
            return None
        return self.frame, time.time() if self.fresh else time.time() - 3600


class FakeCam:
    def __init__(self, channel: int, name: str) -> None:
        self.channel, self.name = channel, name
        self.source = FakeSource()
        self.source.start()
        self.state = "watching"
        self.objects: dict = {}


class FakeMonitor:
    """Monitor stub: running, message, camera_status(), _cams with fake sources."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.running = False
        self.message = "Watching 2 cameras"
        site = cfg.active_site()
        self._cams = [FakeCam(c.channel, c.label) for c in site.enabled_cameras()] if site else []

    def camera_status(self) -> dict:
        return {c.channel: {"state": c.state, "fps": 5.0, "objects": dict(c.objects), "name": c.name, "error": None}
                for c in self._cams}


class FakeBrain:
    def status(self):
        return "running"


def make_config(events_dir: str, cameras=((1, "Front gate"), (2, "Driveway")), sites: int = 1):
    """A synthetic Config (never the real one): one site with the given cameras."""
    from yardguard import config
    from yardguard.config import Camera, Config, Site
    cfg = Config()
    for n in range(sites):
        site = Site(name="Home" if n == 0 else f"Site {n + 1}", host="192.0.2.10", username="admin",
                    cameras=[Camera(ch, name) for ch, name in cameras])
        cfg.sites.append(site)
    cfg.active_site_id = cfg.sites[0].id
    cfg.settings.events_dir = str(events_dir)
    cfg.settings.brain_mode = "verify"
    config._migrate_settings(cfg.settings)
    return cfg


def _qobject_base():
    from PySide6.QtCore import QObject, Signal

    class _Base(QObject):
        config_changed = Signal()

    return _Base


_CtxBase = None


def FakeCtx(folder: str, cameras=((1, "Front gate"), (2, "Driveway")), sites: int = 1):
    """An AppContext-like object for tests (a QApplication must exist): .cfg, .alert_bridge (the real
    gui.AlertBridge), .alert_store (real AlertStore in <folder>/events), .monitor (FakeMonitor),
    .brain, .ui (QSettings in the folder), .save(), .config_changed, and helpers raise_alert(alert)
    / update_alert(alert) that emit the bridge signals like AppContext.make_monitor does."""
    global _CtxBase
    from PySide6.QtCore import QSettings

    from yardguard import config
    from yardguard.engine import AlertStore
    from yardguard.gui import AlertBridge
    assert config.CONFIG_PATH != config.ROOT / "config.json", "tests must never use the real config.json"
    if _CtxBase is None:
        _CtxBase = _qobject_base()

    class _Ctx(_CtxBase):
        def __init__(self):
            super().__init__()
            folder_p = Path(folder)
            folder_p.mkdir(parents=True, exist_ok=True)
            self.cfg = make_config(str(folder_p / "events"), cameras, sites)
            self.alert_bridge = AlertBridge()
            self.alert_store = AlertStore(self.cfg.settings.events_dir)
            self.monitor = FakeMonitor(self.cfg)
            self.brain = FakeBrain()
            self.ui = QSettings(str(folder_p / "ui.ini"), QSettings.IniFormat)
            self.saves = 0

        def save(self):
            self.cfg.save()
            self.saves += 1
            self.config_changed.emit()

        def raise_alert(self, alert, gap_s=None):
            """store.add + the bridge signals (incident_added for the first alert of an incident)."""
            self.alert_store.add(alert, gap_s=gap_s)
            b = self.alert_bridge
            b.alert_added.emit(alert)
            inc = self.alert_store.get_incident(alert.incident_id)
            if inc is not None:
                (b.incident_added if inc.alert_count == 1 else b.incident_updated).emit(inc)
            return inc

        def update_alert(self, alert):
            self.alert_store.update(alert)
            b = self.alert_bridge
            b.alert_updated.emit(alert)
            inc = self.alert_store.get_incident(alert.incident_id)
            if inc is not None:
                b.incident_updated.emit(inc)
            return inc

        def close(self):
            self.alert_store.close()

    return _Ctx()


def write_jpeg(path: Path, size=(360, 640), color=(40, 90, 160), text: str = "") -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img = np.zeros((size[0], size[1], 3), np.uint8)
    img[:] = color
    if text:
        cv2.putText(img, text, (20, size[0] // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    path.write_bytes(buf.tobytes())
    return str(path)


def make_alert(store, severity: str = "warning", verdict: str = "confirmed", channel: int = 1,
               camera: str = "Front gate", ts: float | None = None, rule: str = "night_presence",
               title: str = "Person near cars at night", site_id: str = "", findings=None, clip: bool = False):
    """An engine.Alert with generated snapshot / crop JPEGs in the store's folder (not saved yet)."""
    from yardguard.engine import Alert
    aid = uuid.uuid4().hex[:12]
    folder = Path(store.dir) / "test_pics"
    snap = write_jpeg(folder / f"{aid}_snap.jpg", (720, 1280), text=f"{camera} snapshot")
    crop = write_jpeg(folder / f"{aid}_crop.jpg", (400, 300), (90, 60, 40), text="crop")
    clip_path = ""
    if clip:
        clip_path = str(folder / f"{aid}.mp4")
        Path(clip_path).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    return Alert(id=aid, site_id=site_id, channel=channel, camera=camera, ts=time.time() if ts is None else ts,
                 rule=rule, title=title, detail="", severity=severity, snapshot_path=snap, crop_path=crop,
                 boxes=[{"label": "car", "x1": 0, "y1": 0, "x2": 10, "y2": 10, "role": "context"},
                        {"label": "person", "x1": 0, "y1": 0, "x2": 10, "y2": 10, "role": "subject"}],
                 verdict=verdict, brain_findings=list(findings or (["hand on door handle"] if verdict == "confirmed"
                                                                   else [])),
                 clip_path=clip_path, clip_offset_s=2.0 if clip else -1.0)


def make_incident(store, severity: str = "warning", verdict: str = "confirmed", **kw):
    """Add one alert as a NEW incident; returns (alert, incident)."""
    a = make_alert(store, severity, verdict, **kw)
    store.add(a, gap_s=-1)
    return a, store.get_incident(a.incident_id)


def yg_threads() -> list[str]:
    """Names of live threads started by YardGuard integrations ("yg-*")."""
    return [t.name for t in threading.enumerate() if t.name.startswith("yg-") and t.is_alive()]
