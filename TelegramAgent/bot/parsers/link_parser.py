from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, urljoin

import httpx
from trafilatura import fetch_url, extract

logger = logging.getLogger(__name__)


async def parse_link(url: str) -> str | None:
    """
    Extract readable content from a URL / webpage.
    Returns plain text content, or None on failure.
    """
    # Basic URL validation
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning(f"Unsupported URL scheme: {url}")
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