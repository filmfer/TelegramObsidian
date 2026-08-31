#!/usr/bin/env python3
"""Offline tests for parsers/x_thread.py (v1.8 — X/Twitter threads).

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_x_thread.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.x_thread import (  # noqa: E402
    build_thread_markdown,
    extract_external_links,
    extract_tweet_id,
    fetch_x_thread,
    is_x_url,
)

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("✅" if cond else "❌"), name)
    if not cond:
        failures += 1


# ---- URL parsing ----
check("x.com status URL", is_x_url("https://x.com/user/status/1234567890123"))
check("twitter.com status URL", is_x_url("https://twitter.com/user/status/1234567890123"))
check("mobile.twitter.com", is_x_url("https://mobile.twitter.com/user/statuses/1234567890123"))
check("non-X URL rejected", not is_x_url("https://example.com/status/123"))
check("id extracted (status)", extract_tweet_id("https://x.com/jack/status/20?foo=1") == "20")
check("id extracted (statuses)", extract_tweet_id("https://twitter.com/a/statuses/99999999999") == "99999999999")
check("id missing → None", extract_tweet_id("https://x.com/jack") is None)

# ---- canned payload: thread with self-replies + author links ----
PAYLOAD = {
    "tweet": {
        "url": "https://x.com/dev/status/100",
        "id": "100",
        "text": "A thread about web scraping 1/ Read my guide at https://example.com/guide",
        "author": {"screen_name": "dev", "name": "The Dev", "id": "42"},
        "created_timestamp": 1700000000,
        "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/img.jpg"}]},
        "thread": [
            {
                "id": "101",
                "text": "2/ More details. Also see https://example.com/tools",
                "author": {"screen_name": "dev", "name": "The Dev", "id": "42"},
            },
            {
                "id": "102",
                "text": "3/ Thanks for reading!",
                "author": {"screen_name": "dev", "name": "The Dev", "id": "42"},
            },
            {
                "id": "103",
                "text": "This is someone else's reply — must be excluded",
                "author": {"screen_name": "other", "name": "Other", "id": "77"},
            },
        ],
    }
}

links = extract_external_links(PAYLOAD)
check("Author links extracted in order", links == [
    "https://example.com/guide", "https://example.com/tools"])

md, og = build_thread_markdown(PAYLOAD, resources=[(links[0], "Guide content " * 10)])
check("Header has author", "The Dev (@dev)" in md)
check("Tweet text included", "web scraping 1/" in md)
check("Thread sections numbered", "## Thread 2/3" in md and "## Thread 3/3" in md)
check("Other users' replies excluded", "someone else's reply" not in md)
check("Linked resources section", "## 🔗 Linked resources" in md and "https://example.com/guide" in md)
check("Thumbnail from media", og == "https://pbs.twimg.com/img.jpg")
check("Source line", "Source: https://x.com/dev/status/100" in md)

# Long resource text is abridged
long_md, _ = build_thread_markdown(PAYLOAD, resources=[(links[0], "x" * 9000)])
check("Long resource truncated", "…_(truncated)_" in long_md)

# Single tweet (no thread, no media)
single = {"tweet": {"url": "https://x.com/a/status/200", "text": "just a thought",
                    "author": {"screen_name": "a", "name": "A", "id": "1"}}}
smd, sog = build_thread_markdown(single)
check("Single tweet renders", "just a thought" in smd and "Thread 2" not in smd)
check("Single tweet has no thumbnail", sog is None)

# Empty payload → (None, None)
check("Empty payload → None", build_thread_markdown({}) == (None, None))

# fetch_x_thread with invalid URL → (None, None) without network
check("fetch with non-status URL → None",
      asyncio.run(fetch_x_thread("https://x.com/elonmusk")) == (None, None))

print("\n🎉 ALL X-THREAD TESTS PASSED" if failures == 0 else f"\n💥 {failures} FAILURES")
sys.exit(1 if failures else 0)
