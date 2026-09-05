"""Audio/voice transcription via the free Groq Whisper API.

Telegram voice messages (.ogg/opus) are accepted directly by Groq —
no ffmpeg conversion needed. Long audio (> ~20 min of a 64kbps stream,
which exceeds Groq's 25MB per-request cap) is split into 15-minute
segments with ffmpeg, transcribed per segment, and re-joined.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq limit: 25 MB
# Safe per-request cap so even a small size spike stays under 25MB.
DEFAULT_MAX_GROQ_MB = int(os.getenv("AUDIO_MAX_GROQ_MB", "24"))
# Segment length for long files (seconds → 15 min by default).
DEFAULT_SEGMENT_SECONDS = int(os.getenv("AUDIO_SEGMENT_SECONDS", "900"))

# MIME types Groq's Whisper endpoint accepts without complaint. Anything
# else is pre-converted to MP3 before upload (see transcribe_audio).
_GROQ_SAFE_MIME_TYPES = frozenset({
    "audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4",
    "audio/x-m4a", "audio/webm", "audio/flac",
})

# File extensions that pair with the safe MIME types. Both checks must
# pass for the file to skip ffmpeg — OGG/Opus containers (.oga/.opus/.ogg)
# are intentionally excluded because Groq intermittently rejects them.
_GROQ_SAFE_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".webm", ".flac"})


class TranscriptionError(Exception):
    """Raised when audio transcription fails for a known reason."""


def _convert_to_mp3(src: str) -> Optional[str]:
    """Repackage any audio to MP3 (mono 64k) for Groq compatibility.

    Groq's whisper refuses some containers/codecs with HTTP 400 (e.g. some
    Opus/OGG variants). Returns the temp mp3 path, or None on failure
    (caller falls back to the raw file).
    """
    d = Path(src).with_name(f"{Path(src).stem}_groq.mp3")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", src,
                "-ac", "1", "-b:a", "64k", str(d),
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )
        logger.info("Converted %s → %s for Groq", Path(src).name, d.name)
        return str(d)
    except Exception as e:
        logger.warning("ffmpeg conversion failed (%s) — sending raw file", e)
        return None


async def transcribe_audio(file_path: str, language: Optional[str] = None) -> str:
    """
    Transcribe an audio file using Groq's hosted whisper-large-v3 (free tier).
    Returns the transcript text. Raises TranscriptionError on failure.

    Files in containers Groq may reject (`.oga`/`.opus`, or unknown MIME) are
    pre-converted to MP3 with ffmpeg and sent with the correct name/MIME.
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

    mime = mimetypes.guess_type(path.name)[0] or ""
    upload = path

    # --- Groq format gate -------------------------------------------------
    # Groq rejects some audio containers/codecs (notably certain Opus/OGG
    # variants, which is exactly what Telegram voice messages use) with an
    # HTTP 400. To be robust, we convert anything that is not *provably*
    # safe: both the MIME type AND the file extension must be in the
    # whitelists below, otherwise the file goes through ffmpeg first.
    #
    # NOTE: .oga/.opus/.ogg are deliberately NOT in _GROQ_SAFE_EXTENSIONS —
    # Telegram voice notes arrive as .oga, and "the upload succeeded last
    # time" is not a guarantee with Groq's codec handling. A failed ffmpeg
    # conversion falls back to sending the raw file (see _convert_to_mp3).
    if (
        mime not in _GROQ_SAFE_MIME_TYPES
        or Path(path.name).suffix.lower() not in _GROQ_SAFE_EXTENSIONS
    ):
        converted = _convert_to_mp3(str(path))
        if converted:
            upload = Path(converted)

    up_mime = mimetypes.guess_type(upload.name)[0] or "audio/mpeg"
    content = upload.read_bytes()

    data = {"model": WHISPER_MODEL, "response_format": "text"}
    if language:
        data["language"] = language[:2]

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (upload.name, content, up_mime)},
                data=data,
            )
        if r.status_code == 429:
            raise TranscriptionError("Groq rate limit hit — try again in a minute.")
        r.raise_for_status()
        transcript = (r.text or "").strip()
        if not transcript:
            raise TranscriptionError("Transcription came back empty.")
        logger.info(f"Transcribed {upload.name} ({content.__len__() // 1024}KB → {len(transcript)} chars)")
        return transcript
    except TranscriptionError:
        raise
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "")[:300]
        logger.error(f"Groq transcription HTTP error {e.response.status_code}: {body}")
        raise TranscriptionError(
            f"Groq API error {e.response.status_code}: {body}"
        )
    except Exception as e:
        logger.error(f"Groq transcription failed: {e}")
        raise TranscriptionError(f"Transcription failed: {e}")
    finally:
        # Clean up the temporary mp3 created for Groq compatibility.
        if upload != path:
            try:
                Path(upload).unlink(missing_ok=True)
            except OSError:
                pass


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


# ---- Long-audio support (split into segments, transcribe each, re-join) ----

try:
    from typing import Protocol

    class ProgressCallback(Protocol):
        """Called (index, total, message) as long transcription progresses."""

        def __call__(self, index: int, total: int, message: str) -> None: ...

except Exception:  # pragma: no cover
    pass


def audio_duration_seconds(file_path: str) -> float:
    """Return media duration in seconds via ffprobe (0.0 if it fails)."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            logger.warning(f"ffprobe failed for {file_path}: {out.stderr[-200:]}")
            return 0.0
        return float(out.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not read duration of {file_path}: {e}")
        return 0.0


def split_audio_segments(
    file_path: str,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    out_dir: Optional[str] = None,
) -> list:
    """
    Split an audio file into ~`segment_seconds`-long mp3 segments with ffmpeg.

    Returns a sorted list of segment paths. Raises RuntimeError on failure.
    """
    src = Path(file_path)
    out = Path(out_dir) if out_dir else src.parent
    out.mkdir(parents=True, exist_ok=True)
    pattern = str(out / "seg_%03d.mp3")

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-f", "segment", "-segment_time", str(segment_seconds),
        "-ar", "16000", "-ac", "1", "-b:a", "48k",
        pattern,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed on the host/container.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg segment split timed out.")
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-300:]
        raise RuntimeError(f"ffmpeg segment split failed: {tail}")

    segments = sorted(out.glob("seg_*.mp3"))
    if not segments:
        raise RuntimeError("ffmpeg produced no segments.")
    return [str(s) for s in segments]


def _fmt_ts(seconds: int) -> str:
    """'0:00:00' style timestamp for segment markers."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


async def _call_progress(progress_cb, index, total, message):
    """Invoke a progress callback, awaiting it if it returns a coroutine."""
    if progress_cb is None:
        return
    result = progress_cb(index, total, message)
    if asyncio.iscoroutine(result):
        await result


async def transcribe_audio_long(
    file_path: str,
    language: Optional[str] = None,
    progress_cb: Optional[Callable] = None,
) -> str:
    """
    Transcribe audio of ANY length (tested to ~2h YouTube audio).

    - Files that fit under Groq's 25MB cap → one `transcribe_audio` call.
    - Larger files → split into `AUDIO_SEGMENT_SECONDS` (default 15 min)
      segments via ffmpeg, transcribe each with Groq (falling back to
      local faster-whisper per segment), then re-join with [00:00–15:00]
      markers so the final note keeps chronological structure.
    """
    path = Path(file_path)
    size_mb = path.stat().st_size / (1024 * 1024)
    duration = audio_duration_seconds(str(path))

    # Small enough → single-shot fast path (behaviour unchanged).
    if size_mb <= DEFAULT_MAX_GROQ_MB:
        await _call_progress(progress_cb, 1, 1, "Transcribing audio…")
        return await transcribe_audio(str(path), language)

    # Long file → segment + transcribe + re-join.
    seg_seconds = DEFAULT_SEGMENT_SECONDS
    if duration:
        n_est = max(1, round(duration / seg_seconds))
        await _call_progress(
            progress_cb, 0, n_est,
            f"Splitting audio into ~{seg_seconds // 60}-min segments…",
        )

    logger.info(
        f"Long audio detected: {size_mb:.1f}MB, ~{duration / 60:.1f}min "
        f"— splitting into {seg_seconds // 60}-min segments."
    )
    segment_paths = split_audio_segments(str(path), seg_seconds)
    total = len(segment_paths)
    parts: list = []

    for i, seg in enumerate(segment_paths, 1):
        try:
            await _call_progress(
                progress_cb, i, total, f"Transcribing segment {i}/{total}…"
            )
            part = await transcribe_audio(seg, language)
        except TranscriptionError as e:
            logger.warning(f"Segment {i} Groq failed ({e}) — trying local Whisper.")
            try:
                part = await asyncio.to_thread(transcribe_audio_local, seg, language)
            except TranscriptionError as e2:
                logger.error(f"Segment {i} local transcription failed too: {e2}")
                parts.append(f"[{_fmt_ts((i - 1) * seg_seconds)}] (segment failed)")
                continue
        start_ts = _fmt_ts((i - 1) * seg_seconds)
        end_ts = _fmt_ts(i * seg_seconds)
        parts.append(f"[{start_ts}–{end_ts}]\n{part.strip()}")

    if not parts:
        raise TranscriptionError("No segments could be transcribed.")

    # Cleanup segments.
    for seg in segment_paths:
        try:
            Path(seg).unlink(missing_ok=True)
        except OSError:
            pass

    joined = "\n\n".join(parts)
    logger.info(
        f"Transcribed long audio {path.name}: {len(parts)} segments, "
        f"{len(joined)} chars total."
    )
    return joined