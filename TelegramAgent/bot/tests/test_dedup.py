#!/usr/bin/env python3
"""Tests for storage/dedup_store.py (Phase 1 — duplicate detection).

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_dedup.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.dedup_store import (  # noqa: E402
    check_duplicate,
    compute_file_fingerprint,
    compute_text_fingerprint,
    compute_url_fingerprint,
    init_db,
    pending_add,
    pending_clear,
    pending_list,
    record_processed,
)

TMP = tempfile.mkdtemp(prefix="dedup-test-")
os.environ["DEDUP_DB_PATH"] = str(Path(TMP) / "test.db")
init_db()

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("✅" if cond else "❌"), name)
    if not cond:
        failures += 1


# --- URL fingerprints ---
fp1 = compute_url_fingerprint(
    "https://example.com/article?utm_source=telegram&utm_medium=bot&id=42"
)
fp2 = compute_url_fingerprint("http://EXAMPLE.com/article/?utm_campaign=x&id=42")
fp3 = compute_url_fingerprint("https://example.com/article?si=abc&fbclid=z&id=42")
check("URL fingerprint ignores scheme/host case/trailing slash", fp1 == fp2)
check("URL fingerprint ignores tracking params", fp1 == fp3)

yt1 = compute_url_fingerprint("https://youtu.be/dQw4w9WgXcQ?si=xyz")
yt2 = compute_url_fingerprint("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s")
yt3 = compute_url_fingerprint("https://www.youtube.com/shorts/dQw4w9WgXcQ")
check("YouTube URLs collapse to video-id fingerprint", yt1 == yt2 == yt3)
check("YouTube fp differs from other URLs", yt1 != fp1)

# --- File fingerprints ---
f1 = Path(TMP) / "a.pdf"
f2 = Path(TMP) / "renamed.pdf"
f1.write_bytes(b"same-bytes")
f2.write_bytes(b"same-bytes")
check("File fingerprint is filename-independent",
      compute_file_fingerprint(str(f1)) == compute_file_fingerprint(str(f2)))
f3 = Path(TMP) / "b.pdf"
f3.write_bytes(b"other-bytes")
check("Different bytes -> different fingerprint",
      compute_file_fingerprint(str(f1)) != compute_file_fingerprint(str(f3)))

# --- Text fingerprints ---
t1 = compute_text_fingerprint("Hello   world\n\nthis is  a test")
t2 = compute_text_fingerprint("Hello world this is a test")
check("Text fingerprint normalizes whitespace", t1 == t2)

# --- Check / record cycle ---
dup = check_duplicate(fp1)
check("Unknown fingerprint returns None", dup is None)
record_processed(fp1, "link", "https://example.com/article", "Programming/2026-01-01-x.md")
dup = check_duplicate(fp1)
check("Recorded fingerprint is found", dup is not None and dup["note_path"].endswith("x.md"))
record_processed(fp1, "link", "again", "Other/note.md")
dup = check_duplicate(fp1)
check("Re-record is ignored (first wins)", dup["note_path"].endswith("x.md"))

# --- Pending queue ---
n = pending_add(123, "text", "note one")
pending_add(123, "text", "note two")
pending_add(456, "text", "other chat")
items = pending_list(123, "text")
check("Pending queue scoped per chat", len(items) == 2 and items[0]["content"] == "note one")
check("pending_clear removes only the chat's items", pending_clear(123, "text") == 2)
check("Queue empty after clear", pending_list(123, "text") == [])

print("\n🎉 ALL DEDUP TESTS PASSED" if failures == 0 else f"\n💥 {failures} FAILURES")
sys.exit(1 if failures else 0)
