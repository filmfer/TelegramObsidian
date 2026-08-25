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