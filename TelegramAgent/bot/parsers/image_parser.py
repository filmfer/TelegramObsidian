"""Image ingestion: LLM vision extraction (schemas, bullets, diagrams) + OCR fallback.

Pipeline (called from bot.py):
1. ``prepare_image_bytes`` — Pillow: convert to RGB, downscale to ≤1600px, JPEG bytes.
2. ``vision_extract(images)`` — tries an LLM with vision support (VISION_MODEL) via
   ``chat_vision``; if that fails (no key / provider error) falls back to local
   Tesseract OCR (already installed in the Docker image, zero tokens).
3. The extracted text is fed to the regular ``analyze_and_save`` pipeline, so the
   image content gets the same categorisation / detail-level handling as documents.

Albums (multiple photos in one Telegram message) are handled upstream in bot.py —
here we just accept a list of image paths.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import List, Optional

from PIL import Image

logger = logging.getLogger(__name__)

VISION_MAX_DIMENSION = 1600  # enough for slides / diagrams, small for token cost
VISION_STORE_FALLBACK_TO_OCR = True


def prepare_image_bytes(
    path: str, max_dimension: int = VISION_MAX_DIMENSION
) -> bytes:
    """Open an image, normalise and return JPEG bytes (base64-ready for LLM APIs)."""
    with Image.open(path) as im:
        im.thumbnail((max_dimension, max_dimension))
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def image_to_base64_data_uri(path: str) -> str:
    """Return a data:image/jpeg;base64,… URI suitable for OpenAI-style vision APIs."""
    data = prepare_image_bytes(path)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def ocr_image(path: str) -> str:
    """Local Tesseract OCR — offline, no tokens. Returns '' on failure."""
    try:
        import pytesseract

        with Image.open(path) as im:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            text = pytesseract.image_to_string(im)
        text = (text or "").strip()
        if text:
            logger.info("Tesseract OCR extracted %d chars from %s", len(text), path)
        return text
    except Exception as e:
        logger.error("Tesseract OCR failed on %s: %s", path, e)
        return ""


async def vision_extract(
    image_paths: List[str],
    prompt_topic: str = "image",
) -> Optional[str]:
    """Best-effort transcription of one or more images into structured text.

    - Tries ``chat_vision`` (LLM capable of image input, VISION_MODEL) first.
    - On failure/empty response falls back to Tesseract OCR (each image merged).
    Returns combined text ('' if everything failed) — callers surface a
    meaningful error if even OCR produced nothing.
    """
    from llm.provider import chat_vision  # lazy: avoids import cycles

    if image_paths:
        try:
            data_uris = [image_to_base64_data_uri(p) for p in image_paths]
        except Exception as e:
            logger.error("Image prep failed: %s", e)
            data_uris = []

        if data_uris:
            system = (
                "You extract ALL readable information from the provided image/s. "
                "Images often contain diagrams, boxes, bullet lists and schemas. "
                "Transcribe every piece of text, then add a short section describing "
                "any diagrams/relationships you can infer. Preserve bullet structure "
                "with '-' lines. Be exhaustive, do not paraphrase away details."
            )
            prompt = (
                f"Extract the complete content from this {prompt_topic} as structured "
                "Markdown. Keep the labels of boxes/diagrams and their grouping."
            )
            result = await chat_vision(system, prompt, data_uris)
            text = (result or "").strip() if result else ""
            if text:
                logger.info(
                    "Vision LLM extracted %d chars across %d image(s)",
                    len(text),
                    len(image_paths),
                )
                return text

    # ---- Fallback: local OCR (offline, free) ----
    if VISION_STORE_FALLBACK_TO_OCR:
        parts = []
        for p in image_paths:
            part = ocr_image(p)
            if part:
                parts.append(f"## {Path(p).name}\n{part}")
        if parts:
            logger.info("OCR fallback produced %d chars", sum(len(x) for x in parts))
            return "\n\n".join(parts)

    return ""