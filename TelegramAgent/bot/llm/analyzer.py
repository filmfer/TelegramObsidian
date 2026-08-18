from __future__ import annotations

import logging
import os
import json
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Gemini / Google GenAI SDK expected to be installed.
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
DEFAULT_TEMPERATURE = 0.4

# System prompt instructs the model to categorize + return specific JSON.
SYSTEM_PROMPT = """
You are an AI assistant that converts documents into structured Obsidian notes.
Given the text content and a requested detail_level ({detail_level}), produce a JSON object (valid, raw JSON only, no markdown fences) with EXACTLY these fields:
{{"
  "title": "A concise title for the note in English",
  "category": "one of: travel, car, mechanics, finance, programming, ai, religion, bible, politics, iot, database, data_analysis, web_scraping, exercise, diet, food, cooking, uncategorized ',
  "content": "A well-structured Markdown summary, in US English, adapted to the requested detail level:
    - summarize: 3-8 bullet points
    - detailed: full UTF-8 Markdown with subheadings, bullet points, key facts, quotes, links,
    - precise: all data, numbers, names, URLs, quotes, exact specs preserved
    - raw: the original document text verbatim
  ",
  "tags": ["3-7 lower-case english tag words, separated by commas"],
  "source_url": "leave empty if none / or provide the source url string"
}}
Only output raw JSON. No explanations. No code fences.
"""


def analyze_content(content: str, detail: str, source_url: str = "") -> Optional[Dict[str, Any]]:
    """
    Send content to Gemini and structure output for Obsidian storage.

    Returns a note dict (see write_note_to_vault) or None on failure.
    """
    if not genai:
        logger.error("The 'google-genai' package is not installed.")
        return None

    # Create Gemini client
    client = genai.Client(api_key=GEMINI_API_KEY)

    # Prepare the prompt using detail on the fly
    prompt = SYSTEM_PROMPT.format(detail_level=detail)

    # If there is a URL, include it in the content block
    if source_url:
        combined = f"Source URL: {source_url}\n\n---\n\n{content}"
    else:
        combined = content

    # Generate the response with Gemini
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                prompt,
                combined,
            ],
        )
        # Response should be raw JSON
        raw_response = response.text.strip()
        # Remove any code fences if present
        if raw_response.startswith("```"):
            # Remove the first line and the last line (Could be used)
            lines = raw_response.split("\n")
            raw_response = "\n".join(lines[1:])

        note_dict = json.loads(raw_response)

        # Basic validation and cleanup
        if "title" not in note_dict:
            note_dict["title"] = "Untitled"
        if "category" not in note_dict:
            note_dict["category"] = "uncategorized"
        if "summary" not in note_dict:
            note_dict["summary"] = ""
        if "tags" not in note_dict:
            note_dict["tags"] = []

        note_dict["detail_level"] = detail

        return note_dict
    except Exception as e:
        logger.error(f"Gemini analysis failed: {e}")
        return None