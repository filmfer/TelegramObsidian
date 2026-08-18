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
    vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "ObsidianVault")
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
        "book_title": note.get("book_title", ""),
        "book_authors": note.get("book_authors", []),
        "book_year": note.get("book_year", ""),
    }
    frontmatter_yaml = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
    markdown_body = note.get("content", "")
    full_note = f"---\n{frontmatter_yaml}---\n\n{markdown_body}\n"

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(full_note)
        logger.info(f"Note written to: {file_path}")
        return str(file_path.relative_to(vault_root))
    except Exception as e:
        logger.error(f"Failed to write note: {e}")
        return None