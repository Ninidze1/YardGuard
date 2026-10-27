"""Phone access security: one-time pairing codes, device tokens, rate limits and CSRF tokens.

Pure Python, no Qt, safe to use from any thread. Device tokens are long random strings kept only
in the phone's cookie; YardGuard stores just their SHA-256, so config.json never holds a usable
token.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections import deque

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L: easy to read and type
CODE_LEN = 8
MAX_ACTIVE_CODES = 5

PAIR_PER_MINUTE = 5            # pairing attempts per client per minute
PAIR_FAILS_LOCK = 10           # failed codes per client before its pairing is locked...
PAIR_LOCK_S = 15 * 60          # ...for this long
PAIR_GLOBAL_FAILS = 50         # failed codes from everyone within PAIR_LOCK_S lock pairing for all


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_device_id() -> str:
    return "d-" + uuid.uuid4().hex[:8]


def clean_code(text: str) -> str:
    """What the user typed or the link carried, as the bare code: upper case, no dashes or spaces."""
    return "".join(ch for ch in str(text or "").upper() if ch.isalnum())[:32]


def format_code(code: str) -> str:
    """"K7P29QXM" -> "K7P2-9QXM" (how it is shown on the PC)."""
    return f"{code[:4]}-{code[4:]}" if len(code) > 4 else code


def clean_name(text: str, fallback: str = "Phone") -> str:
    """A device name the user typed: printable, single line, at most 40 characters."""
    name = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())
    return name[:40] or fallback


class PairingCodes:
    """Short-lived, single-use pairing codes. Only their hashes are kept in memory."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}     # sha256(code) -> expiry

    def new(self, ttl: float = 300.0) -> str:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            while len(self._codes) >= MAX_ACTIVE_CODES:   # drop the oldest
                self._codes.pop(min(self._codes, key=self._codes.get))
            self._codes[hash_token(code)] = now + float(ttl)
        return code

    def use(self, code: str) -> bool:
        """True (and the code is gone) when it is a valid, unexpired code."""
        code = clean_code(code)
        if len(code) != CODE_LEN:
            return False
        key = hash_token(code)
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            for stored in list(self._codes):
                if hmac.compare_digest(stored, key):
                    del self._codes[stored]
                    return True
        return False

    def active(self) -> int:
        with self._lock:
            self._purge_locked(self._clock())
            return len(self._codes)

    def clear(self) -> None:
        with self._lock:
            self._codes.clear()

    def _purge_locked(self, now: float) -> None:
        for k in [k for k, exp in self._codes.items() if exp <= now]:
            del self._codes[k]


class PairLimiter:
    """Slows down code guessing: PAIR_PER_MINUTE tries per client per minute, a 15 minute lock
    for a client after PAIR_FAILS_LOCK wrong codes, and for everyone after PAIR_GLOBAL_FAILS."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._tries: dict[str, deque] = {}
        self._fails: dict[str, deque] = {}
        self._global: deque = deque()

    def check(self, key: str) -> str:
        """"" when this client may try now, else the reason ("slow" | "locked"). Counts the try."""
        now = self._clock()
        with self._lock:
            self._trim_locked(now)
            if len(self._global) >= PAIR_GLOBAL_FAILS or len(self._fails.get(key, ())) >= PAIR_FAILS_LOCK:
                return "locked"
            tries = self._tries.setdefault(key, deque())
            if len(tries) >= PAIR_PER_MINUTE:
                return "slow"
            tries.append(now)
            return ""

    def failed(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._fails.setdefault(key, deque()).append(now)
            self._global.append(now)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def _trim_locked(self, now: float) -> None:
        for store, window in ((self._tries, 60.0), (self._fails, PAIR_LOCK_S)):
            for k in list(store):
                q = store[k]
                while q and now - q[0] > window:
                    q.popleft()
                if not q:
                    del store[k]
        while self._global and now - self._global[0] > PAIR_LOCK_S:
            self._global.popleft()


class Csrf:
    """Per-device CSRF tokens: HMAC of the device id with a random key made at start. Pages
    fetch theirs from /api/status, so a restart only means one refetch."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)

    def token(self, device_id: str) -> str:
        return hmac.new(self._key, device_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def valid(self, device_id: str, value: str) -> bool:
        return bool(value) and hmac.compare_digest(self.token(device_id), str(value))


def find_device(devices: list[dict], token: str) -> dict | None:
    """The device whose stored hash matches this cookie token (constant-time compare)."""
    if not token or len(token) > 200:
        return None
    key = hash_token(token)
    found = None
    for d in devices:
        stored = str(d.get("token_sha256") or "")
        if stored and hmac.compare_digest(stored, key):
            found = d
    return found


def parse_cookie(header: str, name: str) -> str:
    """The value of one cookie from a Cookie header ("" when missing)."""
    for part in str(header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k == name:
            return v.strip().strip('"')
    return ""
