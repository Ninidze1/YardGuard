"""Tailscale helpers for using phone access away from home without router changes.

Only reads `tailscale status`, and runs `tailscale serve` when the user presses the button. Never
installs anything, never changes the firewall. Every call has a timeout and runs without a console
window.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("yardguard")

TIMEOUT_S = 5.0
SERVE_TIMEOUT_S = 20.0
DOWNLOAD_URL = "https://tailscale.com/download/windows"


def find_cli() -> str | None:
    """Path of tailscale.exe, or None when Tailscale is not installed."""
    found = shutil.which("tailscale")
    if found:
        return found
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            exe = Path(base) / "Tailscale" / "tailscale.exe"
            if exe.is_file():
                return str(exe)
    return None


def _run(cli: str, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.run([cli, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=flags, stdin=subprocess.DEVNULL)


def _short(text: str) -> str:
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return line[:200]


def status(cli: str | None = None) -> dict:
    """{"installed", "running", "backend_state", "dns_name", "online", "error"} in plain words.
    running: logged in and connected (BackendState "Running")."""
    out = {"installed": False, "running": False, "backend_state": "", "dns_name": "", "online": False, "error": ""}
    cli = cli or find_cli()
    if not cli:
        out["error"] = "Tailscale is not installed on this PC."
        return out
    out["installed"] = True
    try:
        res = _run(cli, ["status", "--json"], TIMEOUT_S)
    except subprocess.TimeoutExpired:
        out["error"] = "Tailscale did not answer. Is it running?"
        return out
    except OSError as exc:
        out["error"] = f"Tailscale could not be started ({exc.__class__.__name__})."
        return out
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data:
        out["error"] = "Tailscale is not running. Open Tailscale and sign in." if res.returncode else \
            "Tailscale gave an unexpected answer."
        return out
    me = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    out["backend_state"] = str(data.get("BackendState") or "")
    out["dns_name"] = str(me.get("DNSName") or "").rstrip(".")
    out["online"] = bool(me.get("Online"))
    out["running"] = out["backend_state"] == "Running"
    if not out["running"]:
        out["error"] = {"NeedsLogin": "Open Tailscale on this PC and sign in.",
                        "Stopped": "Tailscale is turned off. Open it and connect.",
                        "Starting": "Tailscale is still starting. Try again in a moment."}.get(
            out["backend_state"], "Tailscale is not connected. Open it and sign in.")
    elif not out["dns_name"]:
        out["error"] = "Tailscale has no name for this PC yet. Turn on MagicDNS in the Tailscale admin page."
    return out


def serve_on(port: int, cli: str | None = None) -> tuple[bool, str]:
    """Publish http://127.0.0.1:<port> as https://<this pc>.ts.net to your own Tailscale devices.
    Returns (True, url) or (False, plain-words error)."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, "The port is not a number."
    st = status(cli)
    if st["error"]:
        return False, st["error"]
    cli = cli or find_cli()
    try:
        res = _run(cli, ["serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"], SERVE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, "Tailscale did not answer in time. Try again."
    except OSError as exc:
        return False, f"Tailscale could not be started ({exc.__class__.__name__})."
    if res.returncode != 0:
        text = _short(res.stderr or res.stdout)
        if "https" in text.lower() and ("enable" in text.lower() or "not enabled" in text.lower()):
            return False, ("HTTPS certificates are off in your Tailscale account. Turn on HTTPS in the "
                           "Tailscale admin page (DNS settings), then try again.")
        if "access denied" in text.lower() or "permission" in text.lower():
            return False, "Tailscale refused the change. Make sure you are signed in as the PC's owner."
        log.info("tailscale serve failed (exit %s)", res.returncode)
        return False, "Tailscale could not turn on the secure link." + (f" ({text})" if text else "")
    return True, "https://" + st["dns_name"]


def serve_off(cli: str | None = None) -> tuple[bool, str]:
    cli = cli or find_cli()
    if not cli:
        return False, "Tailscale is not installed on this PC."
    try:
        res = _run(cli, ["serve", "--https=443", "off"], SERVE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, "Tailscale did not answer in time. Try again."
    except OSError as exc:
        return False, f"Tailscale could not be started ({exc.__class__.__name__})."
    if res.returncode != 0:
        return False, "Tailscale could not turn off the secure link." + (
            f" ({_short(res.stderr or res.stdout)})" if _short(res.stderr or res.stdout) else "")
    return True, ""
