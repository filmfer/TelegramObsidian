import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# Categories that map to top-level Obsidian folders.
# The AI classifier returns category names; this maps them to vault directories.
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
    "database": "Database",
    "data-analysis": "Data-Analysis",
    "web-scraping": "Web-Scraping",
    "exercise": "Fitness",
    "diet": "Food",
    "cooking": "Food",
    # Add more mappings here as new categories emerge
}

DEFAULT_DETAIL = "detailed"


def derive_detail_level(caption: Optional[str]) -> str:
    """Map user caption to a detail level."""
    if not caption:
        return DEFAULT_DETAIL
    caption_lower = caption.strip().lower()

    if "summar" in caption_lower:
        return "summarize"
    if "detail" in caption_lower:
        return "detailed"
    if "precise" in caption_lower:
        return "precise"
    if "raw" in caption_lower:
        return "raw"
    return DEFAULT_DETAIL


def write_note_to_vault(note: Dict[str, Any]) -> Optional[str]:
    """
    Write a Markdown note into the Obsidian vault using the category folder.
    Returns the relative path of the note inside the vault, or None on failure.
    """
    vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "ObsidianVault")
    category = note.get("category", "Uncategorized")
    folder_name = CATEGORY_MAP.get(category, "Uncategorized")
    folder_path = Path(vault_root) / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    title = note.get("title", "Untitled")
    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = title.lower().replace(" ", "-")[:50]
    filename = f"{date_str}-{slug}.md"
    file_path = folder_path / filename

    # Build YAML frontmatter
    frontmatter = {
        "title": title,
        "source": note.get("source", ""),
        "source_type": note.get("source_type", ""),
        "date": date_str,
        "categories": [folder_name],
        "tags": note.get("tags", []),
        "detail_level": note.get("detail_level", DEFAULT_DETAIL),
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