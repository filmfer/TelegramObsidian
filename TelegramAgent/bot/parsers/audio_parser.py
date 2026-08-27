"""Audio/voice transcription via the free Groq Whisper API.

Telegram voice messages (.ogg/opus) are accepted directly by Groq —
no ffmpeg conversion needed.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq limit: 25 MB


class TranscriptionError(Exception):
    """Raised when audio transcription fails for a known reason."""


async def transcribe_audio(file_path: str, language: Optional[str] = None) -> str:
    """
    Transcribe an audio file using Groq's hosted whisper-large-v3 (free tier).
    Returns the transcript text. Raises TranscriptionError on failure.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise TranscriptionError(
            "GROQ_API_KEY is not set. Get a free key at https://console.groq.com "
            "and add it to .env to enable voice notes."
        )

    path = Path(file_path)
    size = path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        raise TranscriptionError(
            f"Audio too large ({size // (1024 * 1024)}MB > 25MB limit)."
        )
    if size == 0:
        raise TranscriptionError("Audio file is empty.")

    mime = mimetypes.guess_type(path.name)[0] or "audio/ogg"
    content = path.read_bytes()

    data = {"model": WHISPER_MODEL, "response_format": "text"}
    if language:
        data["language"] = language[:2]

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (path.name, content, mime)},
                data=data,
            )
        if r.status_code == 429:
            raise TranscriptionError("Groq rate limit hit — try again in a minute.")
        r.raise_for_status()
        transcript = (r.text or "").strip()
        if not transcript:
            raise TranscriptionError("Transcription came back empty.")
        logger.info(f"Transcribed {path.name} ({size // 1024}KB → {len(transcript)} chars)")
        return transcript
    except TranscriptionError:
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"Groq transcription HTTP error: {e.response.text[:300]}")
        raise TranscriptionError(f"Groq API error {e.response.status_code}.")
    except Exception as e:
        logger.error(f"Groq transcription failed: {e}")
        raise TranscriptionError(f"Transcription failed: {e}")


# ---- Local transcription (free, no size limit) ----

_LOCAL_WHISPER = None


def get_local_whisper_model(model_name: Optional[str] = None):
    """Lazily load faster-whisper (CPU). Caches the loaded model in-process."""
    global _LOCAL_WHISPER
    model_name = model_name or os.getenv("WHISPER_MODEL", "base")
    if _LOCAL_WHISPER is None:
        from faster_whisper import WhisperModel  # lazy import

        _LOCAL_WHISPER = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _LOCAL_WHISPER


def transcribe_audio_local(file_path: str, language: Optional[str] = None) -> str:
    """
    Transcribe audio locally with faster-whisper (int8 CPU).

    Designed for LONG files (e.g. 90-min YouTube audio) — there is no upload
    size cap and no API cost. Slower than Groq but fully free and private.
    Ramps through the file in a single pass.
    """
    model = get_local_whisper_model()
    lang = (language or os.getenv("WHISPER_LANGUAGE") or "en")[:2]
    try:
        segments, _info = model.transcribe(file_path, language=lang, beam_size=5)
        parts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
        text = " ".join(parts).strip()
        if not text:
            raise TranscriptionError("Local transcription came back empty.")
        logger.info(f"Local transcribed {Path(file_path).name}: {len(text)} chars")
        return text
    except TranscriptionError:
        raise
    except Exception as e:
        logger.error(f"Local Whisper transcription failed: {e}")
        raise TranscriptionError(f"Local transcription failed: {e}")