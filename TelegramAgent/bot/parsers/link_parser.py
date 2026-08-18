from __future__ import annotations

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
    Returns plain text content, or None on failure.
    SSRF-safe: blocks private/reserved IP ranges.
    """
    # Basic URL validation
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning(f"Unsupported URL scheme: {url}")
        return None
    if not parsed.hostname:
        logger.warning(f"Missing hostname: {url}")
        return None

    # SSRF protection: refuse private / reserved targets
    if _is_blocked(parsed.hostname):
        return None

    # Fetch the HTML
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            html_content = response.text
    except Exception as e:
        logger.error(f"Failed to fetch URL: {e}")
        return None

    # Use trafilatura to extract main content from HTML
    try:
        extracted_text = extract(html_content)
        if not extracted_text:
            logger.warning("No extractable content found on page.")
            return None
        return extracted_text.strip()
    except Exception as e:
        logger.error(f"Failed to extract content: {e}")
        return None