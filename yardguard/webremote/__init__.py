"""Phone access: YardGuard's cameras, incidents and areas in a phone's browser (off by default)."""
from __future__ import annotations

from .server import DEFAULTS, WebRemote, WebRemoteSignals, lan_ips, normalize_remote

__all__ = ["DEFAULTS", "WebRemote", "WebRemoteSignals", "lan_ips", "normalize_remote"]
