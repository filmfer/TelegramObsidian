"""DuckDuckGo web search for the /research deep-search pipeline.

Uses the `ddgs` package (no API key required).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def search_web(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Search the web via DuckDuckGo. Returns [{title, url, snippet}].
    Sync (ddgs is blocking) — callers should wrap in asyncio.to_thread.
    """
    try:
        from ddgs import DDGS

        out: List[Dict[str, Any]] = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                out.append(
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", "") or r.get("url", ""),
                        "snippet": r.get("body", "") or r.get("snippet", ""),
                    }
                )
        logger.info(f"DuckDuckGo search for '{query}': {len(out)} results")
        return out
    except Exception as e:
        logger.error(f"DuckDuckGo search failed for '{query}': {e}")
        return []