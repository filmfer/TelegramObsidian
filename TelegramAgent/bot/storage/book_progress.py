"""Per-book checkpoint of finished chapter summaries (resume after failure).

A 60-chapter book makes 60+ LLM calls; when every free model is exhausted
halfway, the finished chapters are kept here so re-sending the same file
continues where it stopped instead of starting over (and re-spending quota).

Lives in data/ (the Docker volume), never inside the synced vault.
Files are written atomically (tmp + replace) so a crash can't corrupt them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "book_progress"


def _dir() -> Path:
    return Path(os.getenv("BOOK_PROGRESS_DIR", str(_DEFAULT_DIR)))


def progress_key(fingerprint: str, title: str, n_chars: int) -> str:
    """Stable id for one book: its file fingerprint, else title + text length."""
    basis = fingerprint or f"{title}|{n_chars}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _path(key: str) -> Path:
    return _dir() / f"{key}.json"


def load_progress(key: str) -> Dict[int, str]:
    """Finished chapter summaries {chapter_number: summary}; {} if none/corrupt."""
    p = _path(key)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {int(k): str(v) for k, v in data.get("chapters", {}).items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable book checkpoint %s: %s", p, e)
        return {}


def save_progress(key: str, chapters: Dict[int, str]) -> None:
    p = _path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"chapters": {str(k): v for k, v in chapters.items()}},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:  # checkpointing is best-effort; processing continues
        logger.error("Could not save book checkpoint %s: %s", p, e)


def clear_progress(key: str) -> None:
    try:
        _path(key).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not delete book checkpoint %s: %s", key, e)
