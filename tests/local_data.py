"""Real test data that exists only on the developer's PC (camera snapshots, config.json, models).

Tests that need it call need_*() first: on any other PC (a fresh clone) they print SKIP and
exit 0 instead of failing. YARDGUARD_TEST_SNAPS points to another snapshots folder.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPS = Path(os.environ.get("YARDGUARD_TEST_SNAPS", str(ROOT / "events" / "snapshots")))
# The night snapshots the UI tests and brain probes use
SNAP_FILES = ("check_ch1_20261023_010006.jpg", "check_ch5_20261023_010046.jpg",
              "check_ch2_20261023_010028.jpg", "check_ch7_20261023_010056.jpg")
BRAIN_MODEL = ROOT / "models" / "LocateAnything-3B"


def skip(reason: str) -> None:
    print(f"SKIP: {reason}")
    sys.exit(0)


def need(*paths: Path | str, what: str = "local test data") -> None:
    for p in paths:
        if not Path(p).exists():
            skip(f"needs {what}, not found: {p}")


def need_snapshots(*names: str) -> None:
    """The real camera snapshots (set YARDGUARD_TEST_SNAPS to use another folder)."""
    need(*(SNAPS / n for n in (names or SNAP_FILES)), what="this PC's camera snapshots (YARDGUARD_TEST_SNAPS)")


def need_config() -> None:
    need(ROOT / "config.json", what="a configured YardGuard (config.json with cameras)")


def need_brain() -> None:
    need(BRAIN_MODEL / "config.json", ROOT / ".venv-brain", what="the LocateAnything add-on")
