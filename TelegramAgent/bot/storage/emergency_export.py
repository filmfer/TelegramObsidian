"""Emergency vault mirroring — safety net independent of the rclone sync chain.

Rationale (v1.12, learned from the Sep-2026 outage):
  - Notes are written to /data/vault (a host bind-mount). If that mount is
    misconfigured (wrong host dir) or the rclone↔Drive link breaks, notes
    accumulate in a place Google Drive never sees — while the bot reports OK.
  - This module keeps an always-current SECOND copy of every note under the
    persistent ``agent-data`` Docker volume (/app/data/emergency_export/),
    which survives rebuilds and is totally outside the rclone/mount chain.
  - /export forces a full mirror on demand; /vaultsync reports mount health
    and can suggest the exact host commands to realign the chain.

Security: no secrets are ever involved here (only vault notes + attachments).
The implementation below reuses the same path sanitization rules as
vault_writer so a malicious filename can never escape the export dir.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Only safe folder/filename characters; blocks path traversal (../, etc.).
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9 _\-.]+")
_TRAVERSAL = re.compile(r"(\.\.|[/\\])")

# Dirs inside the vault that must NEVER be mirrored (obsidian/git internals).
_EXCLUDED_DIRS = frozenset({".obsidian", ".git", ".trash"})

# Dirs whose presence inside the vault signals a sync/security hazard.
HAZARDOUS_VAULT_DIRS = frozenset({"_Backups", "_backups", "backup", "secrets"})


def export_root() -> Path:
    """Resolve the emergency-export root (persistent agent-data volume)."""
    return Path(os.getenv("EMERGENCY_EXPORT_DIR", "data/emergency_export"))


def _guard_component(value: str, fallback: str) -> str:
    """Strip path separators/unsafe chars; never allow '..' or '/' (mirror of vault_writer)."""
    cleaned = _TRAVERSAL.sub("", value)
    cleaned = _SAFE_CHARS.sub("", cleaned).strip()
    return cleaned or fallback


def mirror_note(note_rel: str) -> Optional[str]:
    """Copy a just-written note (vault-relative path) into the emergency export.

    Returns the export-relative path, or None on failure (always logged).
    """
    vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "/data/vault")
    note_rel = note_rel.replace("\\", "/")
    # Reject traversal/absolute attempts defensively.
def export_all() -> dict:
    """Mirror every note + attachment from the vault into the export root.

    Returns {"files": n, "bytes": b, "errors": [..]}. Never raises: individual
    copy failures are logged and counted.
    """
    vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "/data/vault")
    root = Path(vault_root)
    dest_root = export_root()
    files = 0
    total_bytes = 0
    errors: List[str] = []
    if not root.is_dir():
        return {"files": 0, "bytes": 0, "errors": [f"vault missing: {root}"]}

    for f in root.rglob("*"):
        # Skip vault-internal directories.
        if any(part in _EXCLUDED_DIRS for part in f.relative_to(root).parts):
            continue
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        try:
            safe_rel = Path(*[_guard_component(p, "item") for p in rel.parts])
            dest = dest_root / safe_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            files += 1
            total_bytes += f.stat().st_size
        except Exception as e:
            logger.error(f"export_all: failed to copy {f}: {e}", exc_info=True)
            errors.append(str(f))

    logger.info("Emergency export complete: %d files, %d bytes, %d errors",
                files, total_bytes, len(errors))
    return {"files": files, "bytes": total_bytes, "errors": errors}


def scan_hazardous_notes(vault_root: Optional[str] = None) -> List[str]:
    """List vault dirs that signal a sync/security hazard (e.g. _Backups).

    Used by /vaultsync to warn that secrets (.env, rclone.conf) may be
    inside the Drive-synced vault — a credential-exposure risk.
    """
    root = Path(vault_root or os.getenv("OBSIDIAN_VAULT_PATH", "/data/vault"))
    found: List[str] = []
    if not root.is_dir():
        return found
    for child in root.iterdir():
        if child.is_dir() and child.name in HAZARDOUS_VAULT_DIRS:
            found.append(str(child.relative_to(root)))
    return found


def note_write_lag(days: int = 1) -> int:
    """Count notes written within the last N days. 0 = no recent writes."""
    vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "/data/vault")
    cutoff = time.time() - 86400 * max(0, days)
    n = 0
    for f in Path(vault_root).rglob("*.md"):
        if any(part in _EXCLUDED_DIRS for part in f.relative_to(Path(vault_root)).parts):
            continue
        try:
            if f.stat().st_mtime > cutoff:
                n += 1
        except Exception:
            continue
    return n
    if note_rel.startswith("/") or note_rel.startswith("..") or "//" in note_rel:
        logger.warning("Refusing to mirror unsafe note path: %s", note_rel)
        return None
    src = Path(vault_root) / note_rel
    if not src.is_file():
        logger.debug("mirror_note: source missing, skipping %s", note_rel)
        return None
    # Mirror the whole relative tree (category folders) inside the export root.
    parts = [p for p in Path(note_rel).parts if p not in ("", ".")]
    safe_parts = [_guard_component(p, f"item{i}") for i, p in enumerate(parts)]
    dest = export_root().joinpath(*safe_parts)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)  # copy2 preserves mtime (useful for dashboards)
        logger.info("Emergency mirror: %s -> %s", note_rel, dest.relative_to(export_root()))
        return str(dest.relative_to(export_root()))
    except Exception as e:
        logger.error(f"Emergency mirror failed for {note_rel}: {e}", exc_info=True)
        return None