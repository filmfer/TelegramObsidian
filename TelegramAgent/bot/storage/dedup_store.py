"""SQLite-backed deduplication + pending-queue store.

The database lives OUTSIDE the Obsidian vault (the vault is synced via
Google Drive / Git from several devices — a live .db file there would
corrupt easily). Default path: <bot dir>/data/agent.db, override with
the DEDUP_DB_PATH env var.

All functions are synchronous and open a fresh connection per call
(safe across threads). Call them from async code via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "agent.db"

# Tracking params stripped before hashing URLs so mirrored/campaigned
# links of the same page deduplicate to the same fingerprint.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "si", "fbclid", "gclid", "igshid", "ref", "ref_src", "ref_url",
    "spm", "scm", "share_url",
}

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    source_identifier TEXT,
    note_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_fp ON processed_items(fingerprint);

CREATE TABLE IF NOT EXISTS pending_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    content TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_chat ON pending_items(chat_id);
"""


def _db_path() -> Path:
    return Path(os.getenv("DEDUP_DB_PATH", str(_DEFAULT_DB)))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if missing. Idempotent."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    logger.info("Dedup store ready at %s", _db_path())


# ---- Fingerprint computation ----

def compute_url_fingerprint(url: str) -> str:
    """Stable sha256 for a URL: strip tracking params, normalize, hash.

    YouTube links collapse to the 11-char video id, so youtu.be/ID and
    youtube.com/watch?v=ID deduplicate to the same entry.
    """
    url = (url or "").strip()
    yt = _YT_ID_RE.search(url)
    if yt:
        return hashlib.sha256(f"youtube:{yt.group(1)}".encode()).hexdigest()

    parts = urlsplit(url)
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    clean = urlunsplit(("https", parts.netloc.lower(), path, urlencode(query_pairs), ""))
    return hashlib.sha256(clean.encode()).hexdigest()


def compute_file_fingerprint(file_path: str) -> str:
    """sha256 of the binary file content (filename-independent)."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_text_fingerprint(text: str) -> str:
    """sha256 of normalized text (collapse whitespace so tiny edits still match)."""
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha256(normalized.encode()).hexdigest()


# ---- Processed items CRUD ----

def check_duplicate(fingerprint: str) -> Optional[Dict[str, Any]]:
    """Return the existing record for this fingerprint, or None."""
    if not fingerprint:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT fingerprint, source_type, source_identifier, note_path, created_at "
            "FROM processed_items WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
    return dict(row) if row else None


def record_processed(
    fingerprint: str, source_type: str,
    source_identifier: str, note_path: str,
) -> None:
    """Insert a processed item (ignored silently if it already exists)."""
    if not fingerprint or not note_path:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_items "
                "(fingerprint, source_type, source_identifier, note_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fingerprint, source_type, source_identifier or "", note_path,
                 datetime.now().isoformat(timespec="seconds")),
            )
    except sqlite3.Error as e:
        logger.error(f"Dedup insert failed: {e}")


# ---- Pending queue (used by the /text and /voice commands) ----

def pending_add(chat_id: int, item_type: str, content: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO pending_items (chat_id, item_type, content, received_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, item_type, content, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid or 0


def pending_list(chat_id: int, item_type: Optional[str] = None) -> List[Dict[str, Any]]:
    query = (
        "SELECT id, item_type, content, received_at FROM pending_items "
        "WHERE chat_id = ?"
    )
    params: List[Any] = [chat_id]
    if item_type:
        query += " AND item_type = ?"
        params.append(item_type)
    query += " ORDER BY id"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def pending_clear(chat_id: int, item_type: Optional[str] = None) -> int:
    """Delete queued items; returns how many rows were removed."""
    query = "DELETE FROM pending_items WHERE chat_id = ?"
    params: List[Any] = [chat_id]
    if item_type:
        query += " AND item_type = ?"
        params.append(item_type)
    with _connect() as conn:
        cur = conn.execute(query, params)
        return cur.rowcount or 0


def pending_expire(ttl_hours: int) -> int:
    """Drop queued items older than ttl_hours; returns rows removed."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM pending_items WHERE received_at < datetime('now', ?)",
            (f"-{int(ttl_hours)} hours",),
        )
        return cur.rowcount or 0


# ---- Async wrappers (sqlite3 is blocking) ----

async def acheck_duplicate(fingerprint: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(check_duplicate, fingerprint)


async def arecord_processed(
    fingerprint: str, source_type: str,
    source_identifier: str, note_path: str,
) -> None:
    await asyncio.to_thread(
        record_processed, fingerprint, source_type, source_identifier, note_path
    )

