"""Integrations: alerts to Telegram, ntfy, email, Discord, webhooks and Home Assistant.

    from yardguard.integrations import IntegrationHub, CATALOG, GROUPS, new_integration

Imported lazily so that `import yardguard.integrations.base` (channels, tests) does not pull in Qt.
"""
from __future__ import annotations

__all__ = ["IntegrationHub", "IntegrationConf", "Channel", "CATALOG", "GROUPS", "new_integration",
           "normalize_integration", "load_channel_class", "Event", "Action", "SendResult", "CheckResult"]


def __getattr__(name: str):
    if name == "IntegrationHub":
        from .hub import IntegrationHub
        return IntegrationHub
    if name in ("IntegrationConf", "Channel", "CATALOG", "GROUPS", "new_integration", "normalize_integration",
                "load_channel_class"):
        from . import base
        return getattr(base, name)
    if name in ("Event", "Action", "SendResult", "CheckResult"):
        from . import model
        return getattr(model, name)
    raise AttributeError(name)
