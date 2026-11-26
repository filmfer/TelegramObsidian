"""Chapter summarisation sized for free-tier models.

* A chapter longer than BOOK_CHUNK_CHARS is summarised in parts (no more
  silent truncation at 24k chars).
* If every model rejects a part as too large, the part is halved at a
  paragraph break and retried (up to MAX_SPLIT_DEPTH times).
* Rate limits are ridden out inside chat() via ``max_wait``.
"""
from __future__ import annotations

import os
import re
from typing import Callable, List

from llm.provider import AllProvidersFailedError, chat
from parsers.book_parser import split_into_chunks


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# ~12k chars ≈ 3.4k tokens: with 1.8k output it fits Groq's ~6k tokens/min free tier.
BOOK_CHUNK_CHARS = _env_int("BOOK_CHUNK_CHARS", 12000)
BOOK_CHAPTER_MAX_TOKENS = _env_int("BOOK_CHAPTER_MAX_TOKENS", 1800)
BOOK_MAX_WAIT = float(_env_int("BOOK_MAX_WAIT_SECONDS", 180))
MAX_SPLIT_DEPTH = 3
MIN_SPLIT_CHARS = 2000

_KEYWORD_LINE = re.compile(r"^\s*KEYWORD:.*$", re.MULTILINE)


def strip_keyword_lines(text: str) -> str:
    return _KEYWORD_LINE.sub("", text or "").strip()


def halve(text: str) -> List[str]:
    """Split near the middle, preferring a paragraph, then a line break."""
    mid = len(text) // 2
    lo = len(text) // 4
    cut = text.rfind("\n\n", lo, mid + lo)
    if cut <= 0:
        cut = text.rfind("\n", lo, mid + lo)
    if cut <= 0:
        cut = mid
    return [text[:cut].strip(), text[cut:].strip()]


async def summarize_chunk(prompt: str, text: str, max_tokens: int = BOOK_CHAPTER_MAX_TOKENS,
                          max_wait: float = BOOK_MAX_WAIT, _depth: int = 0) -> str:
    """One LLM summary; auto-halves the input when every model finds it too large."""
    try:
        out, _meta = await chat(prompt, text, max_tokens=max_tokens, max_wait=max_wait)
        return strip_keyword_lines(out)
    except AllProvidersFailedError as e:
        if not e.too_large or _depth >= MAX_SPLIT_DEPTH or len(text) < 2 * MIN_SPLIT_CHARS:
            raise
        halves = [h for h in halve(text) if h]
        outs = [await summarize_chunk(prompt, h, max_tokens, max_wait, _depth + 1) for h in halves]
        return "\n\n".join(outs)


async def summarize_chapter(make_prompt: Callable[[int, int], str], text: str,
                            chunk_chars: int = BOOK_CHUNK_CHARS) -> str:
    """Summarise a whole chapter; ``make_prompt(part, parts)`` builds each part's prompt."""
    parts = split_into_chunks(text, max_chars=chunk_chars) or [text]
    if len(parts) == 1:
        return await summarize_chunk(make_prompt(1, 1), parts[0])
    outs = []
    for k, part in enumerate(parts, 1):
        summary = await summarize_chunk(make_prompt(k, len(parts)), part)
        outs.append(f"#### Part {k}/{len(parts)}\n\n{summary}")
    return "\n\n".join(outs)
