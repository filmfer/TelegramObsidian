"""AI analysis: turns shared content into structured Obsidian knowledge notes.

Uses the multi-provider layer (llm/provider.py) with automatic fallback.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from llm.provider import AllProvidersFailedError, chat

logger = logging.getLogger(__name__)

CATEGORIES = (
    "travel, car, mechanics, finance, programming, ai, religion, bible, "
    "politics, iot, database, data_analysis, web_scraping, exercise, diet, "
    "food, cooking, books, uncategorized"
)

# --------------------------------------------------------------- prompts ---

DETAIL_SPECS = {
    "summarize": (
        "Produce ONLY the Overview and Key Concepts sections. Keep it brief "
        "(max ~150 words total). This is a quick-reference note."
    ),
    "detailed": (
        "Produce ALL sections of the knowledge template below, fully fleshed "
        "out. This is the default deep-study format."
    ),
    "precise": (
        "Focus on exact facts: numbers, dates, names, commands, formulas. "
        "Preserve them verbatim; skip narrative filler."
    ),
}

KNOWLEDGE_TEMPLATE = """You are an expert study-notes author building a personal knowledge base in Obsidian.

Write the note in this exact Markdown structure:

## 📌 Overview
(2-4 sentences: what this is and why it matters)

## 🔑 Key Concepts
(bulleted list of the main ideas, each explained in 1-2 sentences)

## 📊 Facts & Data
(all concrete facts: numbers, dates, names, versions, commands, formulas)

## 💡 Insights & Implications
(what this means, why it matters, connections to broader topics)

## ❓ Open Questions
(what is unclear or worth researching further)

Rules:
- Use ## headers exactly as shown above.
- Skip a section ONLY if there is truly nothing for it.
- Never invent facts that are not in the source material.
"""

PERSONAL_NOTE_EXTRA = """The user is sharing their OWN raw thought or idea written \
casually. Transform it into a clean structured note WITHOUT changing their \
meaning or inventing facts. Where their idea connects to known concepts, you \
may add helpful context clearly marked as "(context: ...)". Short thoughts \
get short sections — do not pad."""


def build_prompt(kind: str, source_kind: str) -> str:
    """System prompt for the requested detail kind and source kind."""
    detail = DETAIL_SPECS.get(kind, DETAIL_SPECS["detailed"])
    extra = PERSONAL_NOTE_EXTRA if source_kind == "text" else ""
    return f"{KNOWLEDGE_TEMPLATE}\n{detail}\n{extra}".strip()


METADATA_ONLY_PROMPT = """Read the content and reply ONLY with raw JSON (no fences):
{{"title": "concise English title", "category": "one of: __CATS__", "tags": ["t1","t2","t3"]}}
Valid categories: __CATS__"""


# ------------------------------------------------------------ main entry ---

async def analyze_content(
    content: str,
    detail: str,
    source_url: str = "",
    source_kind: str = "document",
) -> Optional[Dict[str, Any]]:
    """
    Transform shared content into a structured knowledge-note dict.
    Returns {title, category, content, tags, detail_level} or None on failure.

    Raises AllProvidersFailedError when every provider/model fails, so the
    bot can trigger the model-selection flow in Telegram.
    """
    if not content or not content.strip():
        logger.warning("analyze_content called with empty content")
        return None

    # 'raw' level: keep text verbatim; ask AI only for cheap metadata.
    if detail == "raw":
        meta = await _extract_metadata(content)
        if meta is None:
            return None
        meta["content"] = content
        meta["detail_level"] = detail
        return meta

    prompt = build_prompt(detail, source_kind)
    payload = f"Source URL: {source_url}\n\n---\n\n{content}" if source_url else content

    note_text = await chat(prompt, payload, max_tokens=8192)
    note_dict = _parse_response(note_text)
    if note_dict is None:
        return None

    note_dict.setdefault("content", "")
    note_dict["detail_level"] = detail
    return note_dict


async def _extract_metadata(content: str) -> Optional[Dict[str, Any]]:
    """Cheap metadata-only extraction (title/category/tags)."""
    try:
        raw = await chat(
            METADATA_ONLY_PROMPT.replace("__CATS__", CATEGORIES),
            content[:6000],
            max_tokens=200,
        )
        return _parse_response(raw)
    except AllProvidersFailedError:
        raise
    except Exception as e:
        logger.error(f"Metadata extraction failed: {e}")
        return None


# -------------------------------------------------------------- parsing ----

def _parse_response(note_text: str) -> Optional[Dict[str, Any]]:
    """
    Split the LLM reply into Markdown body + trailing META_JSON line.
    Tolerates missing/invalid META_JSON and stray code fences.
    """
    body = note_text.strip()
    note_dict: Dict[str, Any] = {}

    marker = "META_JSON:"
    if marker in body:
        body, meta_part = body.rsplit(marker, 1)
        body = body.strip()
        try:
            parsed = json.loads(meta_part.strip().strip("`"))
            if isinstance(parsed, dict):
                note_dict.update(parsed)
        except json.JSONDecodeError:
            logger.warning("Could not parse META_JSON line from model reply")

    note_dict["content"] = body
    note_dict.setdefault("title", "Untitled")
    note_dict.setdefault("category", "uncategorized")
    note_dict.setdefault("tags", [])
    if isinstance(note_dict["tags"], str):
        note_dict["tags"] = [
            t.strip() for t in note_dict["tags"].split(",") if t.strip()
        ]
    return note_dict