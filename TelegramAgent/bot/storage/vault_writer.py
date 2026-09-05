"""Vault persistence: write AI-generated notes safely to the Obsidian vault.

This is the ONLY module that touches the vault filesystem. Three invariants:

1. Path safety — every category/filename passes through _SAFE_CHARS/_TRAVERSAL
   sanitization, so a hostile LLM output can never escape the vault root
   (path-traversal defense).
2. Single source of truth — get_vault_root() resolves OBSIDIAN_VAULT_PATH
   (default ``/data/vault``, the docker-compose bind-mount contract).
3. Fail loudly — warn_if_not_mountpoint() logs CRITICAL at startup if the
   default root is not actually a mountpoint; notes would otherwise land in
   the container's ephemeral layer and vanish on the next rebuild while the
   bot still reports success.
"""
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Only allow safe folder/filename characters; blocks path traversal (../, etc.)
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9 _\-.]+")
_TRAVERSAL = re.compile(r"(\.\.|[/\\])")

# Single source of truth for the vault location.
# In the container this MUST match the docker-compose bind mount
# (`${OBSIDIAN_VAULT_HOST_PATH}:<VAULT_ROOT>`). Never fall back to a relative
# path: that would silently write notes into the container's ephemeral layer —
# the bot reports success while nothing reaches the host / Google Drive.
DEFAULT_VAULT_ROOT = "/data/vault"


def get_vault_root() -> str:
    """Resolve the vault root: OBSIDIAN_VAULT_PATH env var or the container default."""
    return os.getenv("OBSIDIAN_VAULT_PATH", DEFAULT_VAULT_ROOT)


def warn_if_not_mountpoint(vault_root: str) -> None:
    """Log CRITICAL if the default vault root is not a bind mount (silent-loss guard).

    Only applies to DEFAULT_VAULT_ROOT: a custom path is a deliberate local-dev
    choice, and /proc/mounts only exists on Linux (i.e. inside the container).
    """
    if vault_root != DEFAULT_VAULT_ROOT:
        return
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1 and parts[1] == vault_root:
                    return  # bind mount present — all good
        logger.critical(
            "VAULT MISCONFIGURED: %s is NOT a mountpoint — notes will be written "
            "to the container's ephemeral layer and lost on rebuild. Set "
            "OBSIDIAN_VAULT_PATH=%s in .env and verify the docker-compose bind "
            "mount (${OBSIDIAN_VAULT_HOST_PATH}:/data/vault).",
            vault_root,
            vault_root,
        )
    except FileNotFoundError:
        return  # not Linux / not containerized (local dev) — skip check
    except OSError as e:
        logger.warning("Could not verify vault mountpoint: %s", e)

# Single source of truth: maps an AI category keyword to a vault folder.
CATEGORY_MAP = {
    "travel": "Travel",
    "vacation": "Travel",
    "car": "Car",
    "mechanics": "Car",
    "finance": "Finance",
    "programming": "Programming",
    "ai": "AI",
    "religion": "Religion",
    "bible": "Religion",
    "politics": "Politics",
    "iot": "IoT",
    "books": "Books",
    "book": "Books",
    "database": "Database",
    "data-analysis": "Data-Analysis",
    "data_analysis": "Data-Analysis",
    "web-scraping": "Web-Scraping",
    "web_scraping": "Web-Scraping",
    "exercise": "Fitness",
    "diet": "Food",
    "food": "Food",
    "cooking": "Food",
    "hacking": "Hacking",
    "osint": "Hacking",
    "pentesting": "Hacking",
    "security": "Security",
    "cybersecurity": "Security",
    "technology": "Technology",
    "tech": "Technology",
    "linux": "Programming",
    "python": "Programming",
    "javascript": "Programming",
}

DEFAULT_DETAIL = "detailed"


def derive_detail_level(caption: Optional[str]) -> str:
    """Map a user caption / command to a detail level."""
    if not caption:
        return DEFAULT_DETAIL
    cl = caption.strip().lower()
    if "summar" in cl:
        return "summarize"
    if "detail" in cl:
        return "detailed"
    if "precise" in cl:
        return "precise"
    if "raw" in cl:
        return "raw"
    if "book" in cl:
        return "book"
    if "handwritten" in cl or "manuscrit" in cl or "escrito à mão" in cl or "à mão" in cl or "letra" in cl:
        return "handwritten"
    return DEFAULT_DETAIL


def _sanitize_component(value: str, fallback: str) -> str:
    """Strip path separators and unsafe chars; never allow '..' or '/'."""
    cleaned = _TRAVERSAL.sub("", value)
    cleaned = _SAFE_CHARS.sub("", cleaned).strip()
    return cleaned or fallback


def _normalize_categories(note: Dict[str, Any]) -> List[str]:
    """Resolve note categories (plural or singular) into vault folder names."""
    raw = note.get("categories") or [note.get("category", "Uncategorized")]
    if isinstance(raw, str):
        raw = [raw]
    mapped = []
    for c in raw:
        key = str(c).replace("_", "-").lower()
        folder = CATEGORY_MAP.get(key, str(c))
        mapped.append(_sanitize_component(folder, "Uncategorized"))
    return mapped


def write_note_to_vault(note: Dict[str, Any]) -> Optional[str]:
    """Write a Markdown note into the Obsidian vault under its primary category folder."""
    vault_root = get_vault_root()
    mapped = _normalize_categories(note)
    folder_name = mapped[0]
    folder_path = Path(vault_root) / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    title = note.get("title", "Untitled")
    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = _sanitize_component(title.lower().replace(" ", "-"), "untitled")[:50]
    filename = f"{date_str}-{slug}.md"
    file_path = folder_path / filename

    frontmatter = {
        "title": title,
        "source": note.get("source", ""),
        "source_type": note.get("source_type", ""),
        "date": date_str,
        "categories": mapped,
        "tags": note.get("tags", []),
        "detail_level": note.get("detail_level", DEFAULT_DETAIL),
        "attachment": note.get("attachment", ""),
        "attachments": note.get("attachments", []),
        "thumbnail": note.get("thumbnail", ""),
        "book_title": note.get("book_title", ""),
        "book_authors": note.get("book_authors", []),
        "book_year": note.get("book_year", ""),
    }
    frontmatter_yaml = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
    markdown_body = note.get("content", "")

    # Gallery: embed every attached image at the top of the note (Obsidian
    # resolves `![[filename]]` from anywhere in the vault).
    attachments = note.get("attachments") or []
    if attachments:
        gallery = "\n".join(
            f"![[{Path(a).name}]]" for a in attachments
        )
        markdown_body = f"{gallery}\n\n{markdown_body}"

    full_note = f"---\n{frontmatter_yaml}---\n\n{markdown_body}\n"

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(full_note)
        logger.info(f"Note written to: {file_path}")
        return str(file_path.relative_to(vault_root))
    except Exception as e:
        logger.error(f"Failed to write note: {e}")
        return None