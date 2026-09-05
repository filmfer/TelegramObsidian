"""Async web-page scraping for link notes (trafilatura + httpx).

Security-relevant module: every outbound fetch goes through the SSRF guard
(`_is_blocked` + _BLOCKED_NETWORKS) which resolves the hostname and rejects
private, loopback, link-local, and reserved IP ranges — otherwise a crafted
URL could make the bot probe the VPS's internal network (cloud metadata
endpoints, docker bridges, etc.).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
from trafilatura import extract

logger = logging.getLogger(__name__)

# Private / reserved networks that must never be fetched (SSRF protection)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local (AWS metadata 169.254.169.254)
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),       # multicast
    ipaddress.ip_network("240.0.0.0/4"),       # reserved
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def _is_blocked(host: str) -> bool:
    """Resolve a hostname and check whether any of its IPs is private/reserved."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        logger.warning(f"DNS resolution failed for host: {host}")
        return True
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _BLOCKED_NETWORKS):
            logger.warning(f"Blocked SSRF target: {host} -> {ip}")
            return True
    return False


async def parse_link(url: str) -> str | None:
    """Extract readable content from a public URL (no metadata)."""
    text, _ = await parse_link_with_meta(url)
    return text


async def parse_link_with_meta(url: str) -> tuple[str | None, str | None]:
    """
    Extract readable content + the page's og:image (thumbnail) URL.
    Layered fallback: httpx+headers → cloudscraper → Jina Reader.
    SSRF-safe: blocks private/reserved IP ranges.
    Returns (text, og_image_url) — og_image may be None even on success.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning(f"Unsupported URL scheme: {url}")
        return None, None
    if not parsed.hostname:
        logger.warning(f"Missing hostname: {url}")
        return None, None
    if _is_blocked(parsed.hostname):
        return None, None

    # Layer 1 + 2: fetch HTML, then extract main content
    html = await _fetch_httpx(url) or await _fetch_cloudscraper(url)
    if html:
        try:
            text = extract(html)
            if text and text.strip():
                logger.info(f"Scraped {url} ({len(text)} chars)")
                return text.strip(), _extract_og_image(html, url)
        except Exception as e:
            logger.error(f"trafilatura extraction failed for {url}: {e}")

    # Layer 3: Jina Reader — renders JS, bypasses most blocks, returns markdown
    md = await _fetch_jina(url)
    if md:
        return md.strip(), None
    return None, None


def _extract_og_image(html: str, base_url: str) -> str | None:
    """Pull og:image / twitter:image from HTML, resolving relative URLs."""
    try:
        from urllib.parse import urljoin

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for prop in ("og:image", "twitter:image", "twitter:image:src"):
            tag = soup.find("meta", attrs={"property": prop}) or soup.find(
                "meta", attrs={"name": prop}
            )
            if tag and tag.get("content"):
                return urljoin(base_url, tag["content"].strip())
    except Exception as e:
        logger.debug(f"og:image extraction failed: {e}")
    return None


async def download_thumbnail(img_url: str, dest, max_bytes: int = 5_000_000) -> bool:
    """
    Stream-download an image into `dest` with SSRF checks, a content-type
    guard and a hard size cap. Never raises; returns success as bool.
    """
    from pathlib import Path

    parsed = urlparse(img_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if _is_blocked(parsed.hostname):
        logger.warning(f"Blocked SSRF thumbnail target: {img_url}")
        return False

    dest = Path(dest)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            async with client.stream("GET", img_url) as resp:
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if not ctype.startswith("image/"):
                    logger.info(f"Thumbnail rejected (content-type={ctype})")
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                too_big = False
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            too_big = True
                            break
                        f.write(chunk)
        if too_big:
            logger.info(f"Thumbnail too large (> {max_bytes}B) — skipped")
            dest.unlink(missing_ok=True)
            return False
        ok = dest.is_file() and dest.stat().st_size > 0
        if not ok:
            dest.unlink(missing_ok=True)
        return ok
    except Exception as e:
        logger.info(f"Thumbnail download failed: {e}")
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return False


async def _fetch_httpx(url: str) -> str | None:
    """Plain HTTP fetch with realistic browser headers."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9,pt;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except Exception as e:
        logger.info(f"httpx fetch failed for {url}: {e}")
        return None


async def _fetch_cloudscraper(url: str) -> str | None:
    """Cloudflare-protected sites — cloudscraper solves JS challenges (sync)."""
    try:
        import cloudscraper

        def _get():
            s = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "desktop": True}
            )
            r = s.get(url, timeout=45)
            r.raise_for_status()
            return r.text

        html = await asyncio.to_thread(_get)
        logger.info(f"cloudscraper fetched {url} ({len(html)} chars)")
        return html
    except ImportError:
        logger.debug("cloudscraper not installed — skipping layer 2")
        return None
    except Exception as e:
        logger.info(f"cloudscraper failed for {url}: {e}")
        return None


async def _fetch_jina(url: str) -> str | None:
    """Jina Reader (r.jina.ai): renders JS and returns clean markdown."""
    try:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            r = await client.get(f"https://r.jina.ai/{url}")
            r.raise_for_status()
            text = r.text
            # Skip Google login-wall responses
            if "sign in to continue" in text.lower()[:500]:
                logger.info(f"Jina returned a login wall for {url}")
                return None
            logger.info(f"jina.ai fetched {url} ({len(text)} chars)")
            return text
    except Exception as e:
        logger.info(f"jina.ai failed for {url}: {e}")
        return None