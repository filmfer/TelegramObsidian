"""Handwritten-note ingestion (Portuguese from Portugal) — v1.10, DEV status.

Design goals (from user request):
- Accept one or more photographs of handwritten notes; language is EU-Portuguese.
- Transcribe the handwriting VERBATIM — never correct, rephrase or summarise the
  content. Only the note title + categories are inferred (META_JSON).
- Learn the user's handwriting via a small few-shot reference set (/learn):
  the most recent image+transcript pairs are injected into the vision prompt so
  the model generalises the user's letter shapes with zero fine-tuning cost.

Implementation notes:
- Primary: LLM vision (VISION_MODEL) with a pt-PT, verbatim, few-shot prompt.
- Fallback: Tesseract OCR with `por` language data (`tesseract-ocr-por` in the
  Docker image) — weaker on cursive, used only when the vision LLM is unavailable.
- Full output is structured as a normal Obsidian note: model returns markdown body
  + a final `META_JSON:` line, parsed with `_parse_response` from llm.analyzer.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image

logger = logging.getLogger(__name__)

# ---- config (env) ----
HANDWRITTEN_LANG = os.getenv("HANDWRITTEN_LANG", "pt-PT")
OCR_LANG = os.getenv("OCR_LANG", "por")          # tesseract language code
HANDWRITING_REF_DIR = Path(
    os.getenv("HANDWRITING_REF_DIR", "data/handwriting_ref")
)
HANDWRITING_REF_MAX = int(os.getenv("HANDWRITING_REF_MAX", "3"))
HANDWRITING_DEV_MODE = os.getenv("HANDWRITING_DEV_MODE", "true").strip().lower() in (
    "1", "true", "yes"
)

SYSTEM_HANDWRITING = (
    "You are a transcription engine for handwritten notes written in Portuguese "
    "from Portugal (pt-PT).\n"
    "Rules:\n"
    "1) TRANSCRIBE THE HANDWRITING VERBATIM. Do NOT correct spelling, grammar, "
    "punctuation or accents. Do NOT rephrase, summarise, add or remove content.\n"
    "2) Keep line breaks and list structure (use '-' for bullet lists).\n"
    "3) If a word is illegible, write [?] — never guess.\n"
    "4) Preserve dates, numbers, prices and names exactly as written.\n"
    "5) At the end add 'META_JSON:' with ONE rigorous line of JSON for the note "
    "metadata ONLY: {{\"title\": \"short pt-PT title\", \"category\": \"best fit\", "
    "\"categories\": [\"1-2 broad categories\"], \"tags\": [\"handwritten\", \"...\"]}}. "
    "The META_JSON line is metadata — NEVER modify the transcribed body for it."
)

PROMPT_HANDWRITING = (
    "Below are sample(s) of the author's handwriting with their correct "
    "transcriptions (few-shot). Use the SAME hand to disambiguate the new photo(s).\n\n"
    "{fewshot}\n"
    "Now transcribe the attached handwritten note(s) verbatim in European "
    "Portuguese, preserving every word as written. End with META_JSON."
)

def handwriting_ref_dir() -> Path:
    HANDWRITING_REF_DIR.mkdir(parents=True, exist_ok=True)
    return HANDWRITING_REF_DIR


def _load_recent_examples(max_examples: Optional[int] = None) -> List[Dict[str, str]]:
    """Return the most recent (image, transcript) pairs for few-shot prompting."""
    max_n = max_examples or HANDWRITING_REF_MAX
    ref_dir = HANDWRITING_REF_DIR
    if not ref_dir.exists():
        return []
    pairs: List[Dict[str, str]] = []
    for img in sorted(ref_dir.glob("*.jp*"))[-max_n:]:
        txt = img.with_suffix(".txt")
        if txt.is_file():
            pairs.append(
                {
                    "image": str(img),
                    "reference": txt.read_text(encoding="utf-8", errors="replace").strip(),
                }
            )
    return pairs


def save_handwriting_reference(image_path: str, transcript: str) -> Path:
    """Persist one (image, transcript) pair for the few-shot learning set."""
    ref_dir = handwriting_ref_dir()
    stem = Path(image_path).stem
    img_dest = ref_dir / f"{stem}.jpg"
    with Image.open(image_path) as im:
        im.thumbnail((1200, 1200))
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.save(img_dest, format="JPEG", quality=85)
    txt_path = img_dest.with_suffix(".txt")
    txt_path.write_text(transcript.strip(), encoding="utf-8")
    logger.info("Saved handwriting reference pair: %s", img_dest.name)
    return img_dest


def build_fewshot_section() -> str:
    """Render the few-shot examples block for the vision prompt ('' if none)."""
    examples = _load_recent_examples()
    if not examples:
        return "(No reference samples yet.)"
    parts = []
    for i, e in enumerate(examples, 1):
        parts.append(
            f"Sample {i} (image '{Path(e['image']).name}'):\n"
            f"Correct transcription (verbatim):\n{e['reference']}"
        )
    return "\n\n".join(parts)


def _ocr_fallback(image_paths: List[str]) -> str:
    """Local Tesseract OCR in `por` — last-chance extraction when no vision LLM."""
    import pytesseract

    parts = []
    for p in image_paths:
        try:
            with Image.open(p) as im:
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                text = (pytesseract.image_to_string(im, lang=OCR_LANG) or "").strip()
            if text:
                parts.append(f"## {Path(p).name}\n{text}")
        except Exception as e:
            logger.error("OCR failed on %s: %s", p, e)
    return "\n\n".join(parts)


async def transcribe_handwritten(
    image_paths: List[str],
    reference_text: str = "",
) -> Optional[str]:
    """Transcribe handwritten photo(s) verbatim (pt-PT), returning note-ready text.

    Returns None if nothing could be extracted at all (vision + OCR both failed).
    """
    from llm.provider import chat_vision  # lazy

    # Build few-shot section; allow an explicit current reference to override.
    fewshot = build_fewshot_section()
    if reference_text:
        fewshot = f"{fewshot}\n\n(Current sample — transcribe this as the reference above.)"

    try:
        data_uris = []
        for p in image_paths:
            from parsers.image_parser import image_to_base64_data_uri

            data_uris.append(image_to_base64_data_uri(p))
    except Exception as e:
        logger.error("Image prep failed for handwriting: %s", e)
        data_uris = []

    if data_uris:
        body = PROMPT_HANDWRITING.format(fewshot=fewshot)
        result = await chat_vision(SYSTEM_HANDWRITING, body, data_uris)
        if result and result[0].strip():
            logger.info(
                "Handwritten vision extraction: %d chars across %d image(s)",
                len(result[0]),
                len(image_paths),
            )
            return result[0]

    # ---- OCR fallback ----
    ocr = _ocr_fallback(image_paths)
    return ocr or None