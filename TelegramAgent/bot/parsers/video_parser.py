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
    Fetch captions for a YouTube video (free, no key).
    Layer 1: youtube-transcript-api (fast, but blocked from datacenter IPs).
    Layer 2: yt-dlp subtitles download (different request path, bypasses
             the datacenter block for most videos).
    Returns plain text or None. Sync — call via asyncio.to_thread.
    """
    # Layer 1 — youtube-transcript-api
    text = _transcript_yt_api(video_id)
    if text:
        return text
    # Layer 2 — yt-dlp subtitles (works from VPS/datacenter IPs)
    text = _fetch_transcript_ytdlp(video_id)
    if text:
        logger.info(f"YouTube subtitles via yt-dlp for {video_id}: {len(text)} chars")
        return text
    logger.warning(f"YouTube transcript unavailable for {video_id}")
    return None


def _transcript_yt_api(video_id: str) -> Optional[str]:
    """youtube-transcript-api captions (English + Portuguese)."""
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
        return text or None
    except Exception as e:
        logger.warning(f"YouTube transcript-api failed for {video_id}: {e}")
        return None


def _fetch_transcript_ytdlp(video_id: str, proxy: Optional[str] = None) -> Optional[str]:
    """
    Layer 2 — download subtitles with yt-dlp (writesubtitles + writeautomaticsub,
    skip_download=True) and parse them out. Uses a different request path to
    YouTube than youtube-transcript-api, so it usually works when that is
    blocked from datacenter IPs. No duration limit (works for 90-min videos).
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        import yt_dlp

        ydl_opts = {"skip_download": True, "noplaylist": True}
        if proxy:
            ydl_opts["proxy"] = proxy
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as e:
        logger.warning(f"yt-dlp info for {video_id} failed: {e}")
        return None

    subtitles = info.get("subtitles") or {}
    auto_subs = info.get("automatic_captions") or {}
    full_map = {**subtitles, **auto_subs}
    track = None
    preferred = ("en", "en-US", "en-GB", "pt", "pt-BR")
    for p in preferred:
        if p in full_map:
            track = full_map[p][-1]  # last is usually vtt/srt
            break
    if track is None:
        for key in ("en", "pt"):
            if key in full_map:
                track = full_map[key][0]
                break
    if track is None and full_map:
        track = next(iter(full_map.values()))[0]

    url = track.get("url") if track else None
    if url:
        return _fetch_and_parse_vtt(url, proxy)
    return None


def _fetch_and_parse_vtt(url: str, proxy: Optional[str] = None) -> Optional[str]:
    """Download a VTT/SRT transcript URL and strip timestamps/tags."""
    import re

    try:
        proxies = {"http://": proxy, "https://": proxy} if proxy else None
        r = httpx.get(url, headers=UA, timeout=60, proxies=proxies)
        r.raise_for_status()
        raw = r.text
    except Exception as e:
        logger.warning(f"Could not fetch subtitle URL: {e}")
        return None

    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lstrip().isdigit():  # srt index
            continue
        if "-->" in line:  # timestamp cue
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or \
           line.startswith("Language:") or line.startswith("NOTE"):
            continue
        # strip inline VTT tags
        line = re.sub(r"<[^>]+>", "", line)
        line = line.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
        if line:
            lines.append(line)
    text = " ".join(lines).strip()
    return text or None


def download_youtube_audio(video_id: str, dest_mp3: str, proxy: Optional[str] = None) -> bool:
    """
    Layer 3 — download just the audio track of a video (best free fallback
    for videos with no subtitles). Returns True on success.
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        import yt_dlp

        opts = {
            "format": "bestaudio/best",
            "outtmpl": dest_mp3.replace(".mp3", ".%(ext)s"),
            "noplaylist": True,
        }
        if proxy:
            opts["proxy"] = proxy
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
        return Path(dest_mp3).is_file() and Path(dest_mp3).stat().st_size > 0
    except Exception as e:
        logger.warning(f"yt-dlp audio download failed for {video_id}: {e}")
        return False


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