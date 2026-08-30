#!/usr/bin/env python3
"""One-off migration: pre-populate the dedup DB from existing vault notes.

Scans every note under the vault, extracts the YAML frontmatter, and
records a fingerprint for each note so old content won't be re-processed:
  - link/video notes  -> fingerprint of the normalized source URL
  - document/book notes with an attachment -> sha256 of the attachment file
  - text/voice notes  -> fingerprint of the note body text

Run once from the bot directory:
    .venv/bin/python scripts/backfill_dedup_db.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make bot package importable when run from TelegramAgent/bot
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from storage.dedup_store import (  # noqa: E402
    check_duplicate,
    compute_file_fingerprint,
    compute_text_fingerprint,
    compute_url_fingerprint,
    init_db,
    record_processed,
)


def parse_frontmatter(note_path: Path):
    """Return (frontmatter_dict, body) or (None, "")."""
    try:
        text = note_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, ""
    if not text.startswith("---"):
        return None, ""
    end = text.find("\n---", 3)
    if end == -1:
        return None, ""
    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return None, ""
    return fm, text[end + 4:].strip()


def main() -> int:
    vault = Path(os.getenv("OBSIDIAN_VAULT_PATH", "ObsidianVault"))
    if not vault.is_dir():
        print(f"Vault not found: {vault} — set OBSIDIAN_VAULT_PATH.")
        return 1

    init_db()
    notes = sorted(vault.glob("**/*.md"))
    # Skip templates and the organizer log
    notes = [n for n in notes if "99_Templates" not in n.parts]

    added = skipped = nofp = 0
    for note in notes:
        fm, body = parse_frontmatter(note)
        if not fm:
            skipped += 1
            continue

        rel_path = str(note.relative_to(vault))
        source = str(fm.get("source") or "")
        source_type = str(fm.get("source_type") or "")
        attachment = str(fm.get("attachment") or "")

        fingerprint = ""
        if source.startswith(("http://", "https://")):
            fingerprint = compute_url_fingerprint(source)
        elif attachment and source_type in ("document", "book"):
            att_file = vault / attachment
            if att_file.is_file():
                fingerprint = compute_file_fingerprint(str(att_file))
        if not fingerprint and source_type in ("text", "voice", "video"):
            fingerprint = compute_text_fingerprint(body)

        if not fingerprint:
            nofp += 1
            continue

        record_processed(
            fingerprint, source_type or "unknown", source or attachment, rel_path
        )
        rec = check_duplicate(fingerprint)
        if rec and rec["note_path"] == rel_path:
            added += 1
        else:
            skipped += 1

    print(f"Backfill complete: {added} recorded, {skipped} skipped/duplicate, "
          f"{nofp} without computable fingerprint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
