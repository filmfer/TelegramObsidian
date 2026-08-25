"""Video handling: platform-link transcripts (YouTube) + uploaded-video audio.

- YouTube links: free caption/transcript API + oEmbed metadata (no key needed)
- Uploaded videos: ffmpeg extracts the audio track for transcription.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_YT_PATTERNS = (
    r"(?:youtube\.com/watch\?(?:.*&)?v=)([\w\-]{11})",
    r"(?:youtu\.be/)([\w\-]{11})",
    r"(?:youtube\.com/shorts/)([\w\-]{11})",
)

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


def is_youtube_url(url: str) -> bool:
    return any(re.search(p, url) for p in _YT_PATTERNS)


def extract_youtube_id(url: str) -> Optional[str]:
    for p in _YT_PATTERNS:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def fetch_youtube_metadata(video_id: str) -> Dict[str, Any]:
    """Title + channel via YouTube's public oEmbed endpoint (no key)."""
    try:
        r = httpx.get(
            "https://www.youtube.com/oembed",
            params={
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "format": "json",
            },
            headers=UA,
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        return {"title": d.get("title", ""), "author": d.get("author_name", "")}
    except Exception as e:
        logger.warning(f"oEmbed metadata failed: {e}")
        return {}


def fetch_youtube_transcript(video_id: str) -> Optional[str]:
    """
    Fetch captions via youtube-transcript-api (free).
    Tries English + Portuguese. Returns plain text or None.
    Sync — call via asyncio.to_thread.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
        try:
            fetched = api.fetch(video_id, languages=["en", "en-US", "pt", "pt-BR"])
        except Exception:
            listing = api.list(video_id)
            codes = [t.language_code for t in listing]
            if not codes:
                return None
            fetched = api.fetch(video_id, languages=codes[:3])

        parts = [snip.text for snip in fetched]
        text = " ".join(parts).replace("\n", " ").strip()
        logger.info(f"YouTube transcript for {video_id}: {len(text)} chars")
        return text or None
    except Exception as e:
        logger.warning(f"YouTube transcript failed for {video_id}: {e}")
        return None


def extract_audio_track(video_path: str, out_mp3: str) -> str:
    """
    Extract the audio track from a video file using ffmpeg (sync — run in a thread).
    Returns path to the mp3. Raises RuntimeError on failure.
    """
    import subprocess

    cmd = [
        "ffmpeg", "-y", "-i", video_path, "-vn",
        "-acodec", "libmp3lame", "-b:a", "64k", out_mp3,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed on the host/container.")

    if proc.returncode != 0:
        tail = (proc.stderr or "")[-300:]
        raise RuntimeError(f"ffmpeg failed: {tail}")

    if Path(out_mp3).stat().st_size == 0:
        raise RuntimeError("ffmpeg produced an empty audio track.")
    return out_mp3