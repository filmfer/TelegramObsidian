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
    """
    Extract readable content from a public URL.
    Layered fallback: httpx+headers → cloudscraper → Jina Reader.
    SSRF-safe: blocks private/reserved IP ranges.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning(f"Unsupported URL scheme: {url}")
        return None
    if not parsed.hostname:
        logger.warning(f"Missing hostname: {url}")
        return None
    if _is_blocked(parsed.hostname):
        return None

    # Layer 1 + 2: fetch HTML, then extract main content
    html = await _fetch_httpx(url) or await _fetch_cloudscraper(url)
    if html:
        try:
            text = extract(html)
            if text and text.strip():
                logger.info(f"Scraped {url} ({len(text)} chars)")
                return text.strip()
        except Exception as e:
            logger.error(f"trafilatura extraction failed for {url}: {e}")

    # Layer 3: Jina Reader — renders JS, bypasses most blocks, returns markdown
    md = await _fetch_jina(url)
    if md:
        return md.strip()
    return None


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