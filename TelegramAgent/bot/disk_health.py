"""Disk-space health checks (Task: warn when free space < 20%).

Pure stdlib (`shutil.disk_usage`) — no new dependency. All functions are
synchronous; call them from async code via asyncio.to_thread.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PCT = int(os.getenv("DISK_WARN_THRESHOLD_PCT", "20"))
# Seconds that must elapse between proactive alert messages (anti-spam).
DEFAULT_ALERT_MINUTES = int(os.getenv("DISK_ALERT_MINUTES", "60"))


def disk_usage(path: str) -> Optional[tuple]:
    """Return (total_bytes, used_bytes, free_bytes) or None on failure."""
    try:
        return shutil.disk_usage(path)
    except OSError as e:
        logger.warning(f"disk_usage failed for {path}: {e}")
        return None


def free_percent(path: str) -> Optional[float]:
    """Fraction (0.0–1.0) of free space at `path`, or None on failure."""
    u = disk_usage(path)
    if u is None or u.total == 0:
        return None
    return u.free / u.total


def low_disk(path: str, threshold_pct: Optional[float] = None) -> bool:
    """True when free space at `path` is below threshold (default 20%)."""
    if threshold_pct is None:
        threshold_pct = float(os.getenv("DISK_WARN_THRESHOLD_PCT", "20"))
    pct = free_percent(path)
    if pct is None:
        return False  # cannot measure -> don't nag
    return pct * 100 < threshold_pct


def format_disk(path: str) -> str:
    """Human-readable space summary, e.g. '12.4 GB free of 58.0 GB (21%)'."""
    u = disk_usage(path)
    if u is None:
        return ""
    total, used, free = u
    mb = 1024 * 1024
    return (
        f"{free / mb / 1024:.1f} GB free of {total / mb / 1024:.1f} GB "
        f"({free * 100 // total}%)"
    )


def disk_alert_text(path: str) -> str:
    """A ready-to-send warning message, or '' when space is fine."""
    if not low_disk(path):
        return ""
    text = format_disk(path)
    return (
        "⚠️ **Low disk space!**\n"
        f"{text}\n\n"
        "Consider freeing up space, increasing the disk, or swapping to a "
        "larger one, or the bot may fail to write new notes."
    )