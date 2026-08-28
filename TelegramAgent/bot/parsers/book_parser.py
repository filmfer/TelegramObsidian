"""Extract metadata (title, authors, year) and text from e-book files."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# All digital e-book formats supported by the bot
BOOK_EXTENSIONS = {
    ".pdf",
    ".epub",
    ".mobi",
    ".azw",
    ".azw3",
    ".azw4",
    ".djvu",
    ".fb2",
    ".lit",
}

_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")


def is_book_file(file_path: str) -> bool:
    """Return True if the file extension is a recognized e-book format."""
    return Path(file_path).suffix.lower() in BOOK_EXTENSIONS


def extract_book_metadata(file_path: str) -> Optional[Dict[str, Any]]:
    """
    Extract {title, authors, year, text} from an e-book.

    - PDF:        pypdf (pdf metadata + extracted text)
    - EPUB / FB2: ebooklib (OPF metadata + spine text)
    - MOBI/AZW/DJVU/LIT: best-effort text extraction; metadata falls back
      to parsing the first ~3000 characters.
    Returns None on total failure.
    """
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".pdf":
            return _extract_pdf(file_path)
        if ext in {".epub", ".fb2"}:
            return _extract_epub_fb2(file_path)
        # MOBI, AZW, AZW3, AZW4, DJVU, LIT — fallback heuristics
        return _extract_fallback(file_path)
    except Exception as e:
        logger.error(f"Failed to extract book metadata from {file_path}: {e}")
        return None


def _extract_pdf(file_path: str) -> Dict[str, Any]:
    """PDF: use document metadata for title/author/year, pypdf for text."""
    from pypdf import PdfReader

    reader = PdfReader(file_path)
    meta = reader.metadata or {}
    title = _clean(meta.get("/Title")) or Path(file_path).stem
    author = _clean(meta.get("/Author")) or ""
    year = _extract_year(_clean(meta.get("/CreationDate")) or "")

    text_pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(text_pages).strip()

    if not title or not author:
        head = text[:3000]
        if not title:
            title = _guess_title(head) or Path(file_path).stem
        if not author:
            author = _guess_author(head)
    if not year:
        year = _extract_year(text)

    return {
        "title": title,
        "authors": [a.strip() for a in author.split(",") if a.strip()],
        "year": year,
        "text": text,
    }


def _extract_epub_fb2(file_path: str) -> Dict[str, Any]:
    """EPUB / FB2: use ebooklib OPF metadata."""
    from ebooklib import epub, ITEM_DOCUMENT

    if str(file_path).lower().endswith(".fb2"):
        return _extract_fb2(file_path)

    book = epub.read_epub(file_path)
    title = _clean(book.get_metadata("DC", "title"))
    authors = [_clean(a) for a in book.get_metadata("DC", "creator")]
    year = ""
    for date in book.get_metadata("DC", "date"):
        y = _extract_year(_clean(date))
        if y:
            year = y
            break

    # Collect text from all spine documents
    text_parts = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            content = item.get_content().decode("utf-8", errors="ignore")
            plain = _strip_html(content)
            if plain.strip():
                text_parts.append(plain)
        except Exception as e:
            logger.debug(f"Skipping EPUB item: {e}")
    text = "\n\n".join(text_parts).strip()

    if not title:
        title = Path(file_path).stem
    if not year:
        year = _extract_year(text)
    if not authors:
        authors = _guess_author_list(text)

    return {
        "title": title,
        "authors": [a for a in authors if a],
        "year": year,
        "text": text,
    }


def _extract_fb2(file_path: str) -> Dict[str, Any]:
    """FB2 is XML; parse title/author/year from the XML structure."""
    # defusedxml prevents XXE / billion-laughs entity expansion attacks
    from defusedxml import ElementTree as ET

    tree = ET.parse(file_path)
    root = tree.getroot()
    ns = {"fb": "http://www.gribuser.ru/xml/fictionbook/2.0"}

    title_el = root.find(".//fb:title-info/fb:book-title", ns)
    title = _clean(title_el.text if title_el is not None else "") or Path(file_path).stem

    author_els = root.findall(".//fb:title-info/fb:author", ns)
    authors = []
    for a in author_els:
        first = _clean((a.findtext("fb:first-name", "", ns)))
        last = _clean((a.findtext("fb:last-name", "", ns)))
        name = f"{first} {last}".strip()
        if name:
            authors.append(name)

    year_el = root.find(".//fb:title-info/fb:date", ns)
    year = _extract_year(_clean(year_el.text if year_el is not None else ""))

    body_parts = []
    for body in root.findall(".//fb:body", ns):
        for sec in body.iter():
            if sec.tag.endswith("p") and sec.text:
                body_parts.append(sec.text.strip())
    text = "\n".join(body_parts).strip()

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "text": text,
    }


def _extract_fallback(file_path: str) -> Dict[str, Any]:
    """
    MOBI / AZW / AZW3 / AZW4 / DJVU / LIT.

    Full text decoding for these formats requires proprietary/OS-specific
    libraries, so we do a best-effort: try to read the file as text, and when
    that yields nothing usable return an empty text blob. The bot will still
    save the original file as an attachment to the note.
    """
    title = Path(file_path).stem
    text = _try_read_raw(file_path)
    authors = _guess_author_list(text)
    year = _extract_year(text)
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "text": text,
    }


# ---- helpers ----

def _clean(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    elif isinstance(value, (list, tuple)):
        return _clean(value[0]) if value else ""
    return ""


def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _extract_year(text: str) -> str:
    match = _YEAR_RE.search(text)
    return match.group(1) if match else ""


def _guess_title(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines[:8]:
        if 2 <= len(line) <= 120:
            return line
    return ""


def _guess_author(text: str) -> str:
    match = re.search(r"(?:by|author)\s*[:\-]?\s*([A-Z][\w.\-]+(?:\s+[A-Z][\w.\-]+){0,3})", text, re.I)
    return match.group(1) if match else ""


def _guess_author_list(text: str) -> list:
    author = _guess_author(text)
    return [a.strip() for a in author.split(",") if a.strip()]


def _try_read_raw(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()[:50000].strip()
    except Exception as e:
        logger.debug(f"Raw read failed for {file_path}: {e}")
        return ""


# ------------------------------------------------- book pipeline helpers ----

_TOC_LINE = re.compile(r"^\s*.{0,80}[.·…]{3,}\s*\d{1,4}\s*$")   # "Title ..... 12"
_PAGE_NUM = re.compile(r"^\s*\d{1,5}\s*$")
_CHAPTER = re.compile(r"^\s*(chapter|part|section)\b[ :.\-\d].{0,70}$", re.I | re.M)


def clean_book_text(text: str) -> str:
    """
    Strip junk that pollutes study notes: table-of-contents lines
    ("Chapter 3 .... 45"), bare page numbers, and collapsed whitespace.
    """
    if not text:
        return ""
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            kept.append("")
            continue
        if _TOC_LINE.match(s) or _PAGE_NUM.match(s):
            continue
        kept.append(line)
    cleaned = "\n".join(kept)
    # Collapse 3+ blank lines to one
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def split_into_chunks(text: str, max_chars: int = 24000) -> List[str]:
    """
    Split book text into chunks near `max_chars`, preferring paragraph and
    chapter boundaries. Chapter starts are recorded so the map step can be
    section-aware.
    """
    if len(text) <= max_chars:
        return [text] if text else []

    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            # Prefer a hard chapter boundary inside the window
            best = -1
            for m in _CHAPTER.finditer(text, start + max_chars // 2, end):
                best = m.start()
            if best == -1:
                # Fall back to the last double-newline (paragraph break)
                best = text.rfind("\n\n", start + max_chars // 2, end)
            if best > start:
                end = best
        chunks.append(text[start:end].strip())
        start = end

    return [c for c in chunks if c]


def count_chapters(text: str) -> int:
    """Approximate number of chapter headings — useful for progress display."""
    return len(_CHAPTER.findall(text)) or 1