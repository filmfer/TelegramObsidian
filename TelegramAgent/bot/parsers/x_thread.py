"""X/Twitter thread parser (v1.8).

Authors often continue a publication in self-replies ("1/ 2/ 3/…"). This
module fetches the full author thread through the free fxtwitter API
(api.fxtwitter.com — no API key, JSON), collects external links posted by
the author inside the thread, fetches each one with the generic scraper,
and merges everything into ONE Markdown document that becomes a single
Obsidian note.

Replies from other users are intentionally ignored (commentary noise).
If fxtwitter is unreachable, the caller falls back to the generic scrape chain.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_FXTWITTER_API = "https://api.fxtwitter.com/i/status/{status_id}"
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com",
            "mobile.twitter.com", "m.twitter.com"}
_STATUS_RE = re.compile(r"/(?:status|statuses)/(\d{1,25})")
_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")
_SKIP_LINK_HOSTS = ("x.com", "twitter.com", "t.co", "fxtwitter.com",
                    "vxtwitter.com", "api.fxtwitter.com")

_LINK_ABRIDGE_CHARS = 4000


def is_x_url(url: str) -> bool:
    """True when the URL points at x.com / twitter.com."""
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname in _X_HOSTS
    except Exception:
        return False


def extract_tweet_id(url: str) -> Optional[str]:
    """Pull the numeric status id out of any x.com/twitter.com status URL."""
    match = _STATUS_RE.search(url or "")
    return match.group(1) if match else None


def _max_links() -> int:
    try:
        return max(0, int(os.getenv("X_THREAD_MAX_LINKS", "3")))
    except ValueError:
        return 3


def _link_allowed(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname not in _SKIP_LINK_HOSTS
    except Exception:
        return False


def extract_external_links(payload: Dict[str, Any]) -> List[str]:
    """Collect external http(s) links posted by the author in tweet + thread,
    in order, de-duplicated."""
    tweets: List[Dict[str, Any]] = [payload.get("tweet") or {}]
    tweets.extend(payload.get("tweet", {}).get("thread") or [])
    seen: set = set()
    links: List[str] = []
    for tw in tweets:
        text = str(tw.get("text") or "")
        for match in _URL_RE.findall(text):
            url = match.rstrip(".,;:!?")
            if url in seen or not _link_allowed(url):
                continue
            seen.add(url)
            links.append(url)
    return links


def _fmt_date(tweet: Dict[str, Any]) -> str:
    ts = tweet.get("created_timestamp")
    if isinstance(ts, (int, float)):
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    return str(tweet.get("created_at") or "").strip()


def _tweet_section(tweet: Dict[str, Any], heading: Optional[str]) -> List[str]:
    lines: List[str] = []
    if heading:
        lines.append(f"## {heading}")
        lines.append("")
    text = str(tweet.get("text") or "").strip()
    if text:
        lines.append(text)
        lines.append("")
    media = (tweet.get("media") or {}).get("all") or []
    for item in media:
        m_url = str(item.get("url") or "")
        m_type = str(item.get("type") or "media")
        if m_url:
            lines.append(f"![{m_type}]({m_url})")
    if media:
        lines.append("")
    return lines


def build_thread_markdown(
    payload: Dict[str, Any],
    resources: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Render the thread payload as one Markdown document.
    Returns (markdown, og_image_url) — (None, None) when the payload is empty.
    """
    tweet = payload.get("tweet") or {}
    text = str(tweet.get("text") or "").strip()
    if not tweet or not text:
        return None, None

    author = tweet.get("author") or {}
    handle = str(author.get("screen_name") or "unknown")
    name = str(author.get("name") or handle)
    tweet_url = str(tweet.get("url") or f"https://x.com/{handle}")

    og_image = None
    media = (tweet.get("media") or {}).get("all") or []
    if media:
        og_image = str(media[0].get("url") or "") or None

    thread = [
        t for t in (tweet.get("thread") or [])
        if isinstance(t, dict) and (t.get("author") or {}).get("id") == author.get("id")
    ]

    lines = [
        f"# 🧵 X/Twitter — {name} (@{handle})",
        "",
        f"_Source: {tweet_url} · {_fmt_date(tweet)}_",
        "",
    ]
    lines.extend(_tweet_section(tweet, None))

    for i, tw in enumerate(thread, 2):
        lines.extend(_tweet_section(tw, f"Thread {i}/{len(thread) + 1}"))

    if resources:
        lines.append("## 🔗 Linked resources")
        lines.append("")
        for r_url, r_text in resources:
            r_text = (r_text or "").strip()
            lines.append(f"### {r_url}")
            lines.append("")
            if r_text:
                if len(r_text) > _LINK_ABRIDGE_CHARS:
                    r_text = r_text[:_LINK_ABRIDGE_CHARS] + " …_(truncated)_"
                lines.append(r_text)
                lines.append("")
        lines.append(f"_Retrieved {len(resources)} linked resource(s) "
                     f"posted by the author._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n", og_image


async def fetch_x_thread(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch tweet + author thread + external author links via fxtwitter.
    Returns (markdown, og_image) or (None, None) on any failure — the caller
    then falls back to the generic scrape chain.
    """
    status_id = extract_tweet_id(url)
    if not status_id:
        return None, None

    try:
        # fxtwitter sits behind a bot filter: a browser-like User-Agent is required
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                _FXTWITTER_API.format(status_id=status_id), headers=headers
            )
        if resp.status_code != 200:
            logger.warning(f"fxtwitter returned {resp.status_code} for status {status_id}")
            return None, None
        payload = resp.json()
    except Exception as e:
        logger.warning(f"fxtwitter request failed for status {status_id}: {e}")
        return None, None

    tweet = payload.get("tweet") or {}
    if not tweet.get("text"):
        return None, None

    thread = tweet.get("thread") or []
    logger.info(
        f"X status {status_id} by @{(tweet.get('author') or {}).get('screen_name')}: "
        f"{len(thread)} self-reply thread part(s)"
    )

    # Fetch external links the author posted (capped)
    resources: List[Tuple[str, str]] = []
    from parsers.link_parser import parse_link  # local import avoids an import cycle
    for link in extract_external_links(payload)[: _max_links()]:
        try:
            link_text = await parse_link(link)
        except Exception as e:
            logger.warning(f"Could not fetch X-thread linked resource {link}: {e}")
            link_text = None
        if link_text:
            resources.append((link, link_text))

    return build_thread_markdown(payload, resources=resources)
