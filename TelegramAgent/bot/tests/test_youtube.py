#!/usr/bin/env python3
"""Tests for the YouTube caption fallback chain (yt-dlp Layer 2 & 3).

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_youtube.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from parsers.video_parser import (  # noqa: E402
    _fetch_and_parse_vtt,
    extract_youtube_id,
    is_youtube_url,
)

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("✅" if cond else "❌"), name)
    if not cond:
        failures += 1


# URL parsing & id extraction
check("youtu.be/shorts/watch forms all detected",
      all(is_youtube_url(u) for u in (
          "https://youtube.com/watch?v=dQw4w9WgXcQ",
          "https://youtu.be/dQw4w9WgXcQ",
          "https://youtube.com/shorts/dQw4w9WgXcQ",
      )))
check("id extraction stable", extract_youtube_id(
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s") == "dQw4w9WgXcQ")
check("non-YouTube URL rejected", not is_youtube_url("https://example.com"))


# --- VTT parsing (unit, no network — monkeypatch httpx.get) ---
raw = (
    "WEBVTT\n\nKind: captions\nLanguage: en\n\n"
    "00:00:00.000 --> 00:00:03.000\n"
    "Hello <c>world</c> this is &amp; a test\n\n"
    "00:00:03.000 --> 00:00:06.000\n"
    "Second line here\n"
)


def fake_get(url, headers=None, timeout=None, proxies=None):
    class FakeResp:
        def raise_for_status(self):
            pass

        @property
        def text(self):
            return raw

    return FakeResp()


orig = httpx.get
httpx.get = fake_get
try:
    out = _fetch_and_parse_vtt("https://example.com/sub.vtt")
finally:
    httpx.get = orig
check("VTT strips timestamps/tags/entities",
      out == "Hello world this is & a test Second line here")

print("\n🎉 ALL YOUTUBE TESTS PASSED" if failures == 0 else f"\n💥 {failures} FAILURES")
sys.exit(1 if failures else 0)