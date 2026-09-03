"""Telegram → Gemini → Obsidian Knowledge Agent."""
import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from parsers.document_parser import parse_document
from parsers.link_parser import parse_link, parse_link_with_meta, download_thumbnail
from parsers.x_thread import fetch_x_thread, is_x_url
from parsers.book_parser import (
    book_to_markdown,
    clean_book_text,
    extract_book_metadata,
    extract_chapters,
    is_book_file,
    safe_book_filename,
    split_into_chunks,
)
from parsers.audio_parser import (
    TranscriptionError,
    transcribe_audio,
    transcribe_audio_long,
    transcribe_audio_local,
)
from parsers.search_parser import search_web
from parsers.image_parser import ocr_image, vision_extract
from parsers.handwriting_parser import (
    save_handwriting_reference,
    transcribe_handwritten,
)
from parsers.video_parser import (
    download_youtube_audio,
    extract_audio_track,
    extract_youtube_id,
    fetch_youtube_metadata,
    fetch_youtube_transcript,
    is_youtube_url,
)
from llm.analyzer import (
    BOOK_SYNTHESIS_PROMPT,
    CHAPTER_PROMPT,
    RESEARCH_PROMPT,
    VIDEO_PROMPT,
    CATEGORIES,
    _parse_response,
    analyze_content,
)
from llm.provider import chat
from llm.provider import (
    AllProvidersFailedError,
    ProviderRateLimitError,
    _free_score,
    get_catalog,
    get_current_model,
    list_available_models,
    set_current_model,
    validate_and_autoswitch,
)
from storage.vault_writer import derive_detail_level, write_note_to_vault
from storage.vault_organizer import apply_merge, build_merge_plan, make_keyword_suggester
from storage.dashboard import write_dashboard
from storage.dedup_store import (
    acheck_duplicate,
    arecord_processed,
    compute_file_fingerprint,
    compute_text_fingerprint,
    compute_url_fingerprint,
    init_db,
    pending_add,
    pending_clear,
    pending_delete,
    pending_expire,
    pending_list,
)
from notifications import (
    StatusMessage,
    on_error,
    run_with_deadline,
    setup_error_logging,
)
from disk_health import (
    DEFAULT_ALERT_MINUTES,
    disk_alert_text,
    format_disk,
    free_percent,
    low_disk,
)

DISK_JOB_HOURS = 6  # proactive re-check interval

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# Silence noisy INFO logs from HTTP libraries (they spam every 10s polling loop)
for _noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "ObsidianVault")
# Chat used for proactive alerts (weekly model checks). Optional.
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")

Path(VAULT_PATH).mkdir(parents=True, exist_ok=True)
ATTACHMENTS_DIR = Path(VAULT_PATH, "90_Attachments")
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR = Path(VAULT_PATH, "90_Attachments", "thumbnails")
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
init_db()  # dedup + pending-queue SQLite store (data/agent.db)

DETAIL_LEVELS = {"summarize", "detailed", "precise", "raw", "book", "handwritten"}
# Book pipeline tuning
BOOK_MAX_CHAPTERS = int(os.getenv("BOOK_MAX_CHAPTERS", "60"))
BOOK_FULLTEXT = os.getenv("BOOK_FULLTEXT", "true").strip().lower() in ("1", "true", "yes")
# Recent-notes dashboard (silent weekly refresh — no Telegram messages)
try:
    DASHBOARD_DAYS = max(1, int(os.getenv("DASHBOARD_DAYS", "7")))
except ValueError:
    DASHBOARD_DAYS = 7
USER_COOLDOWN_SECONDS = 10
_user_last_request: dict = {}
_user_last_warning: dict = {}  # suppress repeated rate-limit warnings per user

# Telegram Bot API hard limit for getFile() downloads
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # 20 MB

# /text + /voice pending queue (Task 4) — survives restarts via SQLite
STAGING_DIR = Path(os.getenv("STAGING_DIR", "data/staging"))
PENDING_QUEUE_TTL_HOURS = int(os.getenv("PENDING_QUEUE_TTL_HOURS", "24"))
# Disk-warning cooldown (Task: low-disk alerts)
_last_disk_alert: float = 0.0
DISK_ALERT_SECONDS = DEFAULT_ALERT_MINUTES * 60


def _wants_force(text: Optional[str]) -> bool:
    """True when the user appended '--force' to a caption/message."""
    return bool(text) and "--force" in text.lower()


async def _reject_duplicate(update: Update, fingerprint: str, force: bool) -> bool:
    """Return True (and notify the user) if this content was already saved."""
    if not fingerprint or force:
        return False
    dup = await acheck_duplicate(fingerprint)
    if not dup:
        return False
    await update.message.reply_text(
        "⚠️ This content was already saved:\n"
        f"📂 {dup['note_path']} ({dup['created_at']})\n\n"
        "Send it again with `--force` in the caption to create it anyway."
    )
    return True


async def _fetch_thumbnail(img_url: str, slug: str) -> str:
    """Download a thumbnail into 90_Attachments/thumbnails/; vault-rel path or ''."""
    if not img_url:
        return ""
    dest = THUMBNAILS_DIR / f"{slug[:60]}.jpg"
    if await download_thumbnail(img_url, dest):
        try:
            return str(dest.relative_to(VAULT_PATH))
        except ValueError:
            return ""
    return ""

MAX_MODEL_BUTTONS = 30


# ---- Command handlers ----

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Obsidian Knowledge Agent ready.\n\n"
        "Send me:\n"
        "• Documents (PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML)\n"
        "• E-books (EPUB/MOBI/AZW/DJVU/FB2/LIT) → deep study notes\n"
        "• Links (https://…)\n"
        "• Photos/screenshots 🖼️ — caption /image for detailed analysis\n"
        "• Plain text thoughts — queued, then /text creates the note\n"
        "• Voice messages 🎙️ — queued, then /voice transcribes & notes\n\n"
        "/image — (caption on a photo/album) detailed analysis + all\n"
        "        original images embedded as a gallery in the note\n"
        "/text — build a note from queued text messages\n"
        "/voice · /audio — transcribe queued audio into one note; used as a\n"
        "        caption on a voice message or audio file it transcribes\n"
        "        immediately (same behaviour for both)\n"
        "/queue — see what's waiting\n"
        "/research <topic> — deep web research, cited sources\n"
        "Detail levels: /summarize /detailed /precise /raw /book\n"
                "LLM models: /models\n"
        "/disk — check vault disk space\n"
        "/organize preview — tidy sparse categories into broad ones (plan only)\n"
        "/organize — apply the proposed category merges (asks confirmation)\n"
        "/dashboard — rebuild the \"Recent Notes\" note (newest per category)\n\n"
        "Duplicates are detected automatically — send with '--force' to override."
    )


async def set_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    level = update.message.text.lstrip("/").strip().lower()
    if level in DETAIL_LEVELS:
        context.user_data["detail_level"] = level
        await update.message.reply_text(f"✅ Detail level set to: {level}")
    else:
        await update.message.reply_text(
            "❌ Unknown level. Use /summarize, /detailed, /precise, /raw, or /book."
        )


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List available LLM models as inline buttons; tap to switch."""
    await update.message.reply_text("🔍 Fetching available models…")
    catalog = await list_available_models()
    if not catalog:
        await update.message.reply_text(
            "❌ No providers reachable. Check GEMINI_API_KEY / GROQ_API_KEY in .env"
        )
        return
    markup, count = _build_model_keyboard(context, catalog)
    await update.message.reply_text(
        f"🧠 Current model: {get_current_model()}\n"
        f"Available models: {count}\nTap one to switch:",
        reply_markup=markup,
    )


def _build_model_keyboard(context: ContextTypes.DEFAULT_TYPE, catalog: dict):
    """Build inline keyboard from a {provider: [models]} catalog."""
    current = get_current_model()
    flat = sorted(
        {m for models in catalog.values() for m in models},
        key=lambda m: (m != current, -_free_score(m), m),
    )[:MAX_MODEL_BUTTONS]
    context.bot_data["model_choices"] = flat
    rows = [
        [InlineKeyboardButton(f"{'✅ ' if m == current else ''}{m}", callback_data=f"swm:{i}")]
        for i, m in enumerate(flat)
    ]
    return InlineKeyboardMarkup(rows), len(flat)


async def model_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choices = context.bot_data.get("model_choices", [])
    try:
        model = choices[int(query.data.split(":", 1)[1])]
    except (IndexError, ValueError):
        await query.answer("⚠️ List expired — run /models again.", show_alert=True)
        return
    set_current_model(model)
    await query.edit_message_text(f"✅ Model switched to:\n{model}")


# ---- Message handlers ----

def _check_rate_limit(user_id: int) -> bool:
    """Return True if the user is allowed to proceed (cooldown elapsed)."""
    last = _user_last_request.get(user_id)
    if last is None:  # first request ever — always allowed
        _user_last_request[user_id] = time.monotonic()
        return True
    now = time.monotonic()
    if now - last < USER_COOLDOWN_SECONDS:
        return False
    _user_last_request[user_id] = now
    return True


def _too_large(media) -> bool:
    """True if a Telegram media object exceeds the Bot API download limit."""
    size = getattr(media, "file_size", None) or 0
    return size > TELEGRAM_DOWNLOAD_LIMIT


def _too_large_text(media) -> str:
    size_mb = (getattr(media, "file_size", None) or 0) / (1024 * 1024)
    return (
        f"❌ This file is {size_mb:.0f}MB — Telegram's Bot API only allows "
        "bots to download files up to 20MB.\n"
        "Tip: compress it, split it, or send a platform link (YouTube etc.) instead."
    )


async def _rate_limited_reply(update: Update, user_id: int) -> bool:
    """Cooldown gate. Warns at most once per cooldown window per user.

    Returns True when the request must be dropped (still in cooldown).
    """
    if _check_rate_limit(user_id):
        return False
    now = time.monotonic()
    last_warn = _user_last_warning.get(user_id)
    if last_warn is None or now - last_warn >= USER_COOLDOWN_SECONDS:
        _user_last_warning[user_id] = now
        try:
            await update.message.reply_text(
                f"⏳ Please wait {USER_COOLDOWN_SECONDS}s between uploads."
            )
        except Exception as e:
            logger.warning("Could not send rate-limit warning: %s", e)
    return True


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document if update.message else None
    logger.info(
        "Received DOCUMENT: mime=%s size=%s media_group=%s",
        getattr(doc, "mime_type", None), getattr(doc, "file_size", None),
        getattr(update.message, "media_group_id", None),
    )
    user_id = update.effective_user.id if update.effective_user else 0
    if await _rate_limited_reply(update, user_id):
        return

    document = update.message.document
    if _too_large(document):
        logger.info(
            "Rejected oversized document: %.1fMB",
            (document.file_size or 0) / (1024 * 1024),
        )
        await update.message.reply_text(_too_large_text(document))
        return

    caption = update.message.caption
    detail_level = (
        derive_detail_level(caption)
        if caption
        else context.user_data.get("detail_level", "detailed")
    )

    # --- MIME routing: audio/image files sent as documents go to the right pipeline ---
    mime = (update.message.document.mime_type or "").lower()
    if mime.startswith("audio/"):
        await _queue_document_as_voice(update, context)
        return
    if mime.startswith("image/"):
        status = StatusMessage(await update.message.reply_text("🖼️ Reading image…"))
        await run_with_deadline(
            status, _document_image_job(update, context, update.message.document, status)
        )
        return

    file_obj = await update.message.document.get_file()
    if not file_obj:
        await update.message.reply_text("❌ No valid document received.")
        return

    status = StatusMessage(await update.message.reply_text("🔄 Processing document…"))
    await run_with_deadline(
        status, _document_job(update, file_obj, caption, detail_level, status)
    )


async def _queue_document_as_voice(update, context):
    """Audio files sent as documents join the /voice queue (same as voice notes)."""
    media = update.message.document
    if _too_large(media):
        logger.info(
            "Rejected oversized audio document: %.1fMB",
            (media.file_size or 0) / (1024 * 1024),
        )
        await update.message.reply_text(_too_large_text(media))
        return
    caption = (update.message.caption or "").strip()
    if "research" in caption.lower():
        status = StatusMessage(await update.message.reply_text("🎙️ Transcribing audio…"))
        await run_with_deadline(status, _voice_research_job(update, context, media, status))
        return
    chat_id = update.effective_chat.id
    staging = STAGING_DIR / str(chat_id)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        local_path = await _download_telegram_file(media, staging, what="audio document")
    except Exception as e:
        logger.error("Could not download audio document: %s", e, exc_info=True)
        await update.message.reply_text(_download_error_text("audio", e))
        return
    # Caption /audio → transcribe & create the note immediately (like /image).
    if caption.lower().lstrip("/").startswith(("audio", "voice")):
        status = StatusMessage(await update.message.reply_text("🎙️ Transcribing audio…"))
        item = {
            "id": 0,
            "content": str(local_path),
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        await run_with_deadline(status, _voice_queue_job(update, context, [item], status))
        return
    n = await asyncio.to_thread(pending_add, chat_id, "voice", str(local_path))
    await update.message.reply_text(
        f"🎙️ Audio queued ({n} in the queue).\n"
        "Keep sending more, or /voice to transcribe and create the note."
    )


async def _document_image_job(update, context, document, status):
    """Image sent as a document → download → vision/OCR → note."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            local_path = await _download_telegram_file(
                document, tmp, what="image document"
            )
        except Exception as e:
            logger.error("Image document download failed: %s", e, exc_info=True)
            await status.fail(_download_error_text("image", e))
            return
        try:
            fingerprint = await asyncio.to_thread(compute_file_fingerprint, local_path)
            force = _wants_force(update.message.caption or "")
            if await _reject_duplicate(update, fingerprint, force):
                return
            attachment_rel = _save_attachment(local_path)
            detail = (
                derive_detail_level(update.message.caption)
                if update.message.caption
                else "detailed"
            )
            extracted = await vision_extract([str(local_path)])
        except Exception as e:
            logger.error("Image processing failed: %s", e, exc_info=True)
            await status.fail(
                "The vision model could not process this image "
                "(unsupported format, too large, or provider error)."
            )
            return
        if not extracted:
            await status.fail("Could not read any content from this image.")
            return
        prev_model = get_current_model()
        attachments = [attachment_rel] if attachment_rel else []
        await status.update("🧠 Interpreting image content…")
        result = await analyze_and_save(
            update,
            context,
            extracted,
            detail,
            source=f"telegram-image::{Path(local_path).name}",
            source_type="image",
            source_kind="image",
            attachment=attachment_rel,
            attachments=attachments,
            fingerprint=fingerprint,
            force=force,
        )
        if get_current_model() != prev_model:
            set_current_model(prev_model)
            logger.info("Restored previous text model after image note: %s", prev_model)


async def _document_job(update, file_obj, caption, detail_level, status):
    """Download → dedup-check → parse/book-route → analyze (under deadline)."""
    with tempfile.TemporaryDirectory() as tmp:
        local_path = await file_obj.download_to_drive(tmp)
        fingerprint = await asyncio.to_thread(compute_file_fingerprint, local_path)
        force = _wants_force(caption)
        if await _reject_duplicate(update, fingerprint, force):
            return
        attachment_rel = _save_attachment(local_path)

        # --- E-BOOK ROUTE ---
        if detail_level == "book" or is_book_file(local_path):
            book_meta = extract_book_metadata(local_path)
            if not book_meta:
                await update.message.reply_text(
                    "❌ Could not extract book metadata. The file is still saved as an attachment."
                )
                return
            book_meta["path"] = local_path  # needed for chapter extraction
            await _save_book_note(
                update,
                context,
                book_meta,
                detail_level=detail_level,
                source=f"telegram-book::{Path(local_path).name}",
                attachment=attachment_rel,
                fingerprint=fingerprint,
                force=force,
            )
            return

        # --- STANDARD DOCUMENT ROUTE ---
        content_text = parse_document(local_path)

    if not content_text:
        await update.message.reply_text("❌ Could not parse document content.")
        return

    await analyze_and_save(
        update,
        context,
        content_text,
        detail_level,
        source=f"telegram-doc::{Path(local_path).name}",
        source_type="document",
        source_kind="document",
        attachment=attachment_rel,
        fingerprint=fingerprint,
        force=force,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """URLs are scraped+summarized; plain text becomes a personal knowledge note."""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id if update.effective_user else 0
    # Only URL processing is rate-limited; plain text queueing is cheap and must
    # never drop user content mid-burst (that breaks /text batching).
    if text.startswith(("http://", "https://")) and await _rate_limited_reply(
        update, user_id
    ):
        return

    # --- /learn two-step: photo with /learn, then plain text = reference ---
    if context.user_data.get("_hw_pending_ref"):
        img_path = context.user_data.pop("_hw_pending_ref", None)
        token = context.user_data.pop("_hw_pending_token", None)
        try:
            saved = await asyncio.to_thread(
                save_handwriting_reference, img_path, text
            )
        except Exception as e:
            logger.error("Could not save handwriting reference: %s", e)
            await update.message.reply_text(
                "❌ Could not save the reference. Try /learn again."
            )
            return
        # clean up temp image regardless
        import os as _os

        if img_path and _os.path.isfile(img_path):
            try:
                _os.remove(img_path)
            except Exception:
                pass
        await update.message.reply_text(
            f"✅ Handwriting sample saved! The bot will use your handwriting\n"
            f"to transcribe future notes more accurately ({saved.name}).\n"
            "Send future notes with caption 'handwritten' (or /handwritten)."
        )
        return

    if text.startswith(("http://", "https://")):
        if is_youtube_url(text):
            await _process_youtube(update, context, text)
            return
        status = StatusMessage(await update.message.reply_text("🔄 Reading link…"))
        await run_with_deadline(status, _link_job(update, context, text, status))
        return

    # --- Queue plain text for /text (Task 4): nothing is lost, user batches ---
    n = await asyncio.to_thread(pending_add, update.effective_chat.id, "text", text)
    await update.message.reply_text(
        f"📝 Text queued ({n} in the queue).\n"
        "Keep sending messages to accumulate, or /text to create the note now."
    )


async def learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Two-step handwriting trainer: /learn (with photo) → text with correct transcript."""
    photo = None
    if update.message and update.message.photo:
        photo = update.message.photo[-1]
    caption = (update.message.caption or "").strip() if update.message else ""
    if not photo:
        await update.message.reply_text(
            "✍️ To teach me your handwriting, send a PHOTO of a handwritten note "
            "with the caption /learn.\n"
            "Then, in the NEXT message, write the correct verbatim text.\n"
            "I'll use it to recognize your writing better on future notes."
        )
        return
    # Save the photo to staging for the upcoming text step
    staging = STAGING_DIR / "hw_learn"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        file_obj = await photo.get_file()
        hw_path = await file_obj.download_to_drive(staging)
    except Exception as e:
        logger.error(f"/learn photo download failed: {e}")
        await update.message.reply_text("❌ Could not download the photo. Try again.")
        return
    context.user_data["_hw_pending_ref"] = str(hw_path)
    await update.message.reply_text(
        "✅ Photo received. Now send the CORRECT verbatim text of this note "
        "as your next message (no command).\n"
        "I'll store it as a handwriting reference sample."
    )


async def text_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Combine every queued text message into one structured note."""
    chat_id = update.effective_chat.id
    await asyncio.to_thread(pending_expire, PENDING_QUEUE_TTL_HOURS)
    items = await asyncio.to_thread(pending_list, chat_id, "text")
    if not items:
        await update.message.reply_text(
            "📭 No queued text messages. Send me plain text first, then /text."
        )
        return

    combined = "\n\n---\n\n".join(
        f"[{it['received_at']}]\n{it['content']}" for it in items
    )
    detail = context.user_data.get("detail_level", "detailed")
    status = StatusMessage(
        await update.message.reply_text(f"📝 Creating your note from {len(items)} messages…")
    )
    logger.info(
        "Queue[text] chat=%s: processing %d item(s) (ids=%s)",
        chat_id, len(items), [it["id"] for it in items],
    )
    ids = [it["id"] for it in items]
    try:
        result = await run_with_deadline(
            status, _text_note_job(update, context, combined[:12000], detail, status)
        )
        if result is TIMEOUT:
            logger.warning("Queue[text] chat=%s: timed out — items kept for retry", chat_id)
            return
        # Success (True) or duplicate ("duplicate") → items are fully handled.
        deleted = await asyncio.to_thread(pending_delete, ids)
        logger.info(
            "Queue[text] chat=%s: cleared %d item(s) after outcome=%r",
            chat_id, deleted, result,
        )
    except Exception as e:
        logger.error(
            "Queue[text] chat=%s: processing failed — items kept for retry",
            chat_id, exc_info=True,
        )
        # analyze_and_save already reported handled provider errors to the user;
        # anything else gets a generic failure note here (never silent).
        if not getattr(e, "handled", False):
            await status.fail(f"Could not create the note: {e}")


async def voice_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe every queued audio, merge, and create one note."""
    chat_id = update.effective_chat.id
    await asyncio.to_thread(pending_expire, PENDING_QUEUE_TTL_HOURS)
    items = await asyncio.to_thread(pending_list, chat_id, "voice")
    if not items:
        await update.message.reply_text(
            "📭 No queued audio messages. Send me a voice note first, then /voice."
        )
        return

    status = StatusMessage(
        await update.message.reply_text(f"🎙️ Transcribing {len(items)} audio(s)…")
    )
    logger.info(
        "Queue[voice] chat=%s: processing %d item(s) (ids=%s)",
        chat_id, len(items), [it["id"] for it in items],
    )
    ids = [it["id"] for it in items]
    try:
        result = await run_with_deadline(status, _voice_queue_job(update, context, items, status))
        if result is TIMEOUT:
            logger.warning("Queue[voice] chat=%s: timed out — items kept for retry", chat_id)
            return
        # Success (True) or duplicate ("duplicate") → items are fully handled.
        deleted = await asyncio.to_thread(pending_delete, ids)
        logger.info(
            "Queue[voice] chat=%s: cleared %d item(s) after outcome=%r",
            chat_id, deleted, result,
        )
    except Exception as e:
        logger.error(
            "Queue[voice] chat=%s: processing failed — items kept for retry",
            chat_id, exc_info=True,
        )
        if not getattr(e, "handled", False):
            await status.fail(f"Could not create the note: {e}")

async def _voice_queue_job(update, context, items, status):
    """Transcribe queued audio files sequentially, then build a merged note."""
    transcripts = []
    for i, item in enumerate(items, 1):
        path = item["content"]
        if not Path(path).is_file():
            transcripts.append(f"[{item['received_at']}]\n(staging file missing — skipped)")
            continue
        try:
            await status.update(f"🎙️ Transcribing audio {i}/{len(items)}…")
            transcripts.append(f"[{item['received_at']}]\n{await transcribe_audio(path)}")
        except TranscriptionError as e:
            logger.error(f"Queued audio {i} transcription failed: {e}")
            transcripts.append(f"[{item['received_at']}]\n(transcription failed: {e})")

    combined = "\n\n---\n\n".join(transcripts)[:12000]
    detail = context.user_data.get("detail_level", "detailed")
    await status.update("✍️ Creating your note…")
    result = await analyze_and_save(
        update, context, combined, detail,
        source="telegram-voice::queued batch",
        source_type="voice",
        source_kind="text",
        fingerprint=compute_text_fingerprint(combined),
    )

    # Cleanup staging files ONLY if success
    if result:
        for item in items:
            try:
                Path(item["content"]).unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Could not delete staging file: %s", e)
    return result


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show how many items are waiting in the /text and /voice queues."""
    chat_id = update.effective_chat.id
    await asyncio.to_thread(pending_expire, PENDING_QUEUE_TTL_HOURS)
    texts = await asyncio.to_thread(pending_list, chat_id, "text")
    voices = await asyncio.to_thread(pending_list, chat_id, "voice")
    await update.message.reply_text(
        f"📋 Queued for this chat:\n"
        f"📝 Text: {len(texts)} → /text to process\n"
        f"🎙️ Audio: {len(voices)} → /voice to process\n\n"
        "Items expire after "
        f"{PENDING_QUEUE_TTL_HOURS}h."
    )


# ---- /organize (Task 3) ----

async def organize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Merge sparse category folders. '/organize preview' only shows the plan."""
    preview = bool(context.args) and context.args[0].lower() == "preview"
    status = StatusMessage(await update.message.reply_text("🧹 Analyzing vault categories…"))
    try:
        plan = await asyncio.to_thread(
            build_merge_plan, VAULT_PATH, make_keyword_suggester()
        )
    except Exception as e:
        logger.exception("Failed to build organize plan")
        await status.fail(
            "Could not read the vault (this often happens when the rclone "
            "mount / Google Drive is momentarily unavailable).\n"
            "Check logs/bot.log and try again in a moment."
        )
        return
    if not plan:
        await status.update(
            "✅ Vault is tidy — no merge candidates.\n"
            "Tune config/category_taxonomy.yaml (protected/manual/threshold) if needed."
        )
        return
    lines = [f"• {f} → {t} ({c} notes)" for f, t, c in plan]
    text = "🧹 Proposed merges:\n" + "\n".join(lines)
    if preview:
        await status.update(text + "\n\n(Preview only — nothing was moved. Run /organize to apply.)")
        return

    context.user_data["organize_plan"] = plan
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data="org:yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="org:no"),
    ]])
    try:
        await status.message.edit_text(
            text + "\n\nApply these merges?", reply_markup=markup
        )
    except Exception as e:
        logger.warning(f"Could not show organize keyboard: {e}")
        context.user_data.pop("organize_plan", None)
        await status.update(text + "\n\n(could not show confirmation buttons — cancelled)")


async def organize_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply or cancel the stored merge plan."""
    query = update.callback_query
    await query.answer()
    plan = context.user_data.get("organize_plan")
    if not plan:
        await query.edit_message_text("⌛ The proposal expired — run /organize again.")
        return
    if query.data == "org:no":
        context.user_data.pop("organize_plan", None)
        await query.edit_message_text("❌ Cancelled — nothing was moved.")
        return

    await query.edit_message_text("🧹 Moving notes…")
    moved = await asyncio.to_thread(apply_merge, VAULT_PATH, plan)
    context.user_data.pop("organize_plan", None)
    await query.edit_message_text(
        f"✅ Organized! {moved} notes moved.\n"
        "Frontmatter updated, old categories kept as tags; a git commit was written."
    )


async def _link_job(update, context, url, status):
    """Scrape a public URL and turn it into a knowledge note (under deadline)."""
    content, og_image = None, None
    source_type = "link"
    # X/Twitter first: fxtwitter thread (author self-replies + author's links)
    if is_x_url(url):
        try:
            content, og_image = await fetch_x_thread(url)
            if content:
                source_type = "x-thread"
        except Exception as e:
            logger.warning(f"X thread fetch failed for {url}: {e} — generic scrape fallback")
            content, og_image = None, None
    if not content:
        content, og_image = await parse_link_with_meta(url)
    if not content:
        await status.fail(
            "Could not read the link (it may block bots).\n"
            "Tip: copy the page text and send it here instead — I'll turn it into a note."
        )
        return
    thumb_rel = await _fetch_thumbnail(
        og_image, hashlib.sha1(url.encode()).hexdigest()[:16]
    )
    detail = context.user_data.get("detail_level") or "summarize"
    await analyze_and_save(
        update, context, content, detail,
        source=url, source_type=source_type, source_kind="document",
        fingerprint=compute_url_fingerprint(url),
        force=_wants_force(url),
        thumbnail=thumb_rel,
    )


async def _text_note_job(update, context, text, detail, status):
    """Turn a plain-text thought into a structured, categorized note."""
    return await analyze_and_save(
        update, context, text[:12000], detail,
        source="telegram-text::manual note",
        source_type="text",
        source_kind="text",
        fingerprint=compute_text_fingerprint(text[:12000]),
        force=_wants_force(text),
    )


# ---- Voice notes (#4) ----

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice/audio is queued for /voice; captions with 'research' run instantly."""
    logger.info("Received VOICE/AUDIO update")
    user_id = update.effective_user.id if update.effective_user else 0
    if await _rate_limited_reply(update, user_id):
        return

    media = update.message.voice or update.message.audio
    if not media:
        await update.message.reply_text("❌ No audio received.")
        return
    if _too_large(media):
        logger.info(
            "Rejected oversized audio: %.1fMB",
            (media.file_size or 0) / (1024 * 1024),
        )
        await update.message.reply_text(_too_large_text(media))
        return
    caption = (update.message.caption or "").strip()

    # Instant research flow kept for backward compatibility
    if "research" in caption.lower():
        status = StatusMessage(await update.message.reply_text("🎙️ Transcribing audio…"))
        await run_with_deadline(status, _voice_research_job(update, context, media, status))
        return

    # --- Queue for /voice (Task 4): nothing is lost, user batches ---
    chat_id = update.effective_chat.id
    staging = STAGING_DIR / str(chat_id)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        local_path = await _download_telegram_file(media, staging, what="audio")
    except Exception as e:
        logger.error("Could not download queued audio: %s", e, exc_info=True)
        await update.message.reply_text(_download_error_text("audio", e))
        return

    # Caption /audio or /voice → transcribe & create the note immediately
    # (unified with /image-style captions; voice note and audio file alike).
    if caption.lower().lstrip("/").startswith(("audio", "voice")):
        status = StatusMessage(await update.message.reply_text("🎙️ Transcribing audio…"))
        item = {
            "id": 0,
            "content": str(local_path),
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        await run_with_deadline(
            status, _voice_queue_job(update, context, [item], status)
        )
        return

    n = await asyncio.to_thread(pending_add, chat_id, "voice", str(local_path))
    await update.message.reply_text(
        f"🎙️ Audio queued ({n} in the queue).\n"
        "Keep sending more, or /voice to transcribe and create the note."
    )


async def _voice_research_job(update, context, media, status):
    """Download → transcribe → deep research (kept for 'research' captions)."""
    with tempfile.TemporaryDirectory() as tmp:
        file_obj = await media.get_file()
        local_path = await file_obj.download_to_drive(tmp)
        try:
            transcript = await transcribe_audio(local_path)
        except TranscriptionError as e:
            await status.fail(f"Transcription failed: {e}")
            return

    preview = transcript[:300] + ("…" if len(transcript) > 300 else "")
    await status.update(f"🎙️ Transcribed:\n\n{preview}\n\n🔎 Researching…")
    await _research_topic(update, context, transcript[:500], status)


# ---- Deep research (#2) ----

async def research_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/research <topic> — web search + synthesis into a cited study note."""
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text(
            "Usage: /research <topic>\nExample: /research best linux hardening practices 2026"
        )
        return
    status = StatusMessage(await update.message.reply_text(f"🔎 Researching “{topic}”…"))
    try:
        await run_with_deadline(status, _research_topic(update, context, topic, status))
    except ProviderRateLimitError as e:
        logger.error(f"Rate Limit or Quota Exceeded: {e}")
        await status.fail(await _quota_error_text(e))
    except AllProvidersFailedError as e:
        logger.error(f"All providers failed: {[a.get('error') for a in e.attempts]}")
        await _offer_model_switch(update, context)
    except Exception as e:
        logger.error(f"Research command failed: {e}")
        await status.fail(f"Research failed: {e}")


async def _research_topic(update, context, topic, status):
    """Search web → scrape top sources → LLM synthesis → cited note.
    `status` is a notifications.StatusMessage."""
    fingerprint = compute_text_fingerprint(topic)
    if await _reject_duplicate(update, fingerprint, _wants_force(topic)):
        return
    results = await asyncio.to_thread(search_web, topic, 5)
    if not results:
        await status.fail("No search results found for this topic.")
        return

    sources_payload = []
    for r in results[:4]:
        title = r.get("title") or r.get("url", "")[:60]
        try:
            await status.update(f"🔎 Reading: {title[:60]}…")
            content = await parse_link(r["url"]) or ""
        except Exception:
            content = ""
        body = content[:5000] or f"(snippet) {r.get('snippet', '')}"
        sources_payload.append(f"### Source: {title}\nURL: {r['url']}\n\n{body}")

    prompt = RESEARCH_PROMPT.format(topic=topic, categories=CATEGORIES)
    payload = "\n\n---\n\n".join(sources_payload)

    await status.update("🧠 Synthesizing research…")
    try:
        note_text, _ = await chat(prompt, payload, max_tokens=6000)
    except ProviderRateLimitError as e:
        logger.error(f"Rate Limit or Quota Exceeded during research: {e}")
        await status.fail(await _quota_error_text(e))
        return
    except AllProvidersFailedError as e:
        logger.error(f"All providers failed during research: {[a.get('error') for a in e.attempts]}")
        await status.fail("All AI providers failed — run /models to check.")
        return

    note_dict = _parse_response(note_text)
    if not note_dict:
        await status.fail("Research synthesis failed.")
        return

    tags = note_dict.get("tags", [])
    if "research" not in tags:
        tags.insert(0, "research")
    note_dict["tags"] = tags
    note_dict["source"] = f"research::{topic}"
    note_dict["source_type"] = "research"
    note_dict["detail_level"] = "detailed"

    note_path = write_note_to_vault(note_dict)
    if not note_path:
        await status.fail("Could not write to Obsidian vault.")
        return
    await arecord_processed(fingerprint, "research", f"research::{topic}", note_path)
    await status.update(
        f"🔎 Research saved!\n📂 {note_path}\n📝 {note_dict.get('title')}"
    )


# ---- Video handling (#5) ----

async def _process_youtube(update, context, url):
    """YouTube link → free caption transcript → knowledge note with video info."""
    video_id = extract_youtube_id(url) or ""
    detail = context.user_data.get("detail_level") or "summarize"
    fingerprint = compute_url_fingerprint(url)
    if await _reject_duplicate(update, fingerprint, _wants_force(url)):
        return
    status = StatusMessage(await update.message.reply_text("🎬 Fetching video transcript…"))
    await run_with_deadline(
        status, _youtube_job(update, context, url, video_id, detail, fingerprint, status)
    )


async def _youtube_job(update, context, url, video_id, detail, fingerprint, status):
    """Fetch transcript → summarize → write note (under deadline).
    Free caption fallback chain: youtube-transcript-api → yt-dlp subtitles
    → (last resort) download audio + local faster-whisper transcription.
    """
    meta, transcript = {}, None
    if video_id:
        meta = await asyncio.to_thread(fetch_youtube_metadata, video_id)
        await status.update("🎬 Fetching video transcript…")
        transcript = await asyncio.to_thread(fetch_youtube_transcript, video_id)

    # Layer 3 — no captions at all: pull the audio and transcribe it (free)
    if not transcript and video_id:
        await status.update("🎬 No captions found — transcribing audio…")
        transcript = await _yt_audio_fallback(video_id)

    if not transcript:
        await status.fail(
            "Could not extract any transcript/captions for this video.\n"
            "Tip: upload the video file here and I'll transcribe the audio instead."
        )
        return

    title = meta.get("title") or f"Video {video_id}"
    author = meta.get("author", "")
    await status.update(f"🧠 Summarizing “{title[:60]}”…")

    # Predictable YouTube thumbnail — download best-effort (Task 9)
    thumb_rel = ""
    if video_id:
        thumb_rel = await _fetch_thumbnail(
            f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg", video_id
        )

    payload = (
        f"Video URL: {url}\nTitle: {title}\nChannel: {author}\n\n"
        f"Transcript:\n{transcript[:50000]}"
    )
    try:
        note_text, _ = await chat(VIDEO_PROMPT.format(categories=CATEGORIES), payload, max_tokens=6000)
    except ProviderRateLimitError as e:
        logger.error(f"Rate Limit or Quota Exceeded on video summarization: {e}")
        await status.fail(await _quota_error_text(e))
        return
    except AllProvidersFailedError as e:
        logger.error(f"All providers failed: {[a.get('error') for a in e.attempts]}")
        await status.fail("All AI providers failed — run /models to check.")
        return
    note_dict = _parse_response(note_text)
    if not note_dict:
        await status.fail("Video summarization failed.")
        return

    tags = note_dict.get("tags", [])
    if "video" not in tags:
        tags.insert(0, "video")
    body = note_dict.get("content", "")
    if thumb_rel:
        body = f"![[{thumb_rel}]]\n\n{body}"
    note_dict.update(
        {
            "tags": tags,
            "source": url,
            "source_type": "video",
            "detail_level": detail,
            "thumbnail": thumb_rel,
            "content": f"🔗 {url}\n📺 {title}" + (f" — {author}" if author else "") + f"\n\n{body}",
        }
    )

    note_path = write_note_to_vault(note_dict)
    if not note_path:
        await status.fail("Could not write to Obsidian vault.")
        return
    await arecord_processed(fingerprint, "video", url, note_path)
    await status.update(f"🎬 Video note saved!\n📂 {note_path}\n📝 {note_dict.get('title')}")


async def _yt_audio_fallback(video_id: str) -> Optional[str]:
    """Layer 3: download audio (yt-dlp) + transcribe ANY length.

    `transcribe_audio_long` handles the split: files ≤ ~24MB go to Groq in
    one call, larger files are segmented (15-min, `AUDIO_SEGMENT_SECONDS`)
    with ffmpeg, transcribed per segment (Groq → local fallback) and merged.
    """
    import tempfile

    proxy = os.getenv("YOUTUBE_PROXY_URL") or None
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = str(Path(tmp) / "audio.mp3")
        ok = await asyncio.to_thread(download_youtube_audio, video_id, mp3, proxy)
        if not ok:
            logger.warning(f"yt-dlp audio download failed for {video_id}")
            return None
        # yt-dlp may keep the native container extension (m4a/webm/opus)
        for ext in (".mp3", ".m4a", ".webm", ".opus", ".oga", ".mkv"):
            p = Path(tmp) / f"audio{ext}"
            if p.is_file():
                mp3 = str(p)
                break
        try:
            return await transcribe_audio_long(mp3)
        except TranscriptionError as e:
            logger.error(f"Long-audio transcription failed for {video_id}: {e}")
            return None


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uploaded video → ffmpeg extracts audio → Groq Whisper → knowledge note."""
    user_id = update.effective_user.id if update.effective_user else 0
    if await _rate_limited_reply(update, user_id):
        return

    video = update.message.video or update.message.document
    if not video:
        await update.message.reply_text("❌ No video received.")
        return
    size_mb = (video.file_size or 0) / (1024 * 1024)
    if size_mb > 20:
        await update.message.reply_text(
            f"❌ This video is {size_mb:.0f}MB — Telegram's Bot API only allows downloads up to 20MB.\n"
            "Tip: compress it, or send a platform link (YouTube etc.) instead."
        )
        return

    status = StatusMessage(await update.message.reply_text("🎬 Downloading & extracting audio track…"))
    await run_with_deadline(status, _video_job(update, context, video, status))


async def _video_job(update, context, video, status):
    """Download → ffmpeg audio → Whisper → summarize (under deadline)."""
    with tempfile.TemporaryDirectory() as tmp:
        file_obj = await video.get_file()
        local_path = await file_obj.download_to_drive(tmp)
        fingerprint = await asyncio.to_thread(compute_file_fingerprint, local_path)
        caption = (update.message.caption or "").strip()
        if await _reject_duplicate(update, fingerprint, _wants_force(caption)):
            return
        out_mp3 = str(Path(tmp) / "audio.mp3")
        try:
            await asyncio.to_thread(extract_audio_track, local_path, out_mp3)
        except RuntimeError as e:
            await status.fail(str(e))
            return
        try:
            transcript = await transcribe_audio_long(
                out_mp3,
                progress_cb=status.update,
            )
        except TranscriptionError as e:
            await status.fail(f"Transcription failed: {e}")
            return

    detail = derive_detail_level(caption) if caption else context.user_data.get("detail_level", "detailed")

    await status.update("🧠 Summarizing video content…")
    await analyze_and_save(
        update, context, transcript[:40000], detail,
        source=f"telegram-video::{Path(local_path).name}",
        source_type="video",
        source_kind="document",
    )


# ---- Helpers ----

def _save_attachment(local_path: str):
    """Copy original document into the vault's Attachments folder."""
    try:
        dest = ATTACHMENTS_DIR / Path(local_path).name
        shutil.copy2(local_path, dest)
        return str(dest.relative_to(VAULT_PATH))
    except Exception as e:
        logger.error(f"Could not store attachment: {e}")
        return None


def _save_attachments(local_paths) -> list:
    """Copy every original file into Attachments; returns vault-relative paths."""
    out = []
    for p in local_paths:
        rel = _save_attachment(p)
        if rel:
            out.append(rel)
    return out


async def _save_book_note(update, context, book_meta, detail_level="book", source="",
                          attachment=None, fingerprint: str = "", force: bool = False):
    """Launch background deep-processing of an e-book (map-reduce over sections)."""
    title = book_meta.get("title") or "Untitled Book"
    status = await update.message.reply_text(
        f"📖 Starting deep processing of “{title}”…\n"
        f"This runs in the background — I'll keep you updated here."
    )
    asyncio.create_task(
        _process_book_task(update, book_meta, detail_level, source, attachment,
                           status, fingerprint, force)
    )


async def _process_book_task(update, book_meta, detail_level, source, attachment, status,
                             fingerprint: str = "", force: bool = False):
    """
    Chapter-aware study-note pipeline:
      extract real chapters → detailed per-chapter summary (map, one LLM call
      per chapter) → cheap synthesis from digests → assemble note locally
      (no giant merge call, no truncation) → save full-text Markdown to vault.
    """
    title = book_meta.get("title") or "Untitled Book"
    authors = ", ".join(book_meta.get("authors", [])) or "Unknown"
    if await _reject_duplicate(update, fingerprint, force):
        return
    try:
        raw = clean_book_text(book_meta.get("text", ""))
        if len(raw) < 500:
            await status.edit_text(
                "❌ Not enough extractable text in this book to build a note.\n"
                "The original file is still saved in 90_Attachments/."
            )
            return

        local_path = book_meta.get("path") or ""
        chapters = (
            extract_chapters(local_path, max_chapters=BOOK_MAX_CHAPTERS)
            if local_path
            else []
        )
        if not chapters:
            # Fallback: no detectable chapter structure → part-sized chunks
            chapters = [
                {"title": f"Part {i}", "text": c}
                for i, c in enumerate(split_into_chunks(raw), 1)
            ]
        total = len(chapters)
        logger.info(f"Book '{title}': {len(raw)} chars → {total} chapters/parts")

        chapter_notes = []
        for i, ch in enumerate(chapters, 1):
            ch_title = ch.get("title") or f"Part {i}"
            prompt = CHAPTER_PROMPT.format(
                chapter_num=i, total=total, chapter_title=ch_title,
                book_title=title, authors=authors,
            )
            section_text, _ = await chat(prompt, ch["text"][:24000], max_tokens=2500)
            chapter_notes.append({"title": ch_title, "summary": section_text})
            if i % 2 == 0 or i == total:
                try:
                    await status.edit_text(
                        f"📖 Processing “{title}”\n"
                        f"🧠 Summarizing chapter {i}/{total} — {ch_title[:40]}…"
                    )
                except Exception:
                    pass  # message may be identical; ignore edit errors
            await asyncio.sleep(0.5)  # be polite to free-tier rate limits

        # Full-text Markdown copy of the book inside the vault (90_Attachments/BookTexts)
        fulltext_name = ""
        if BOOK_FULLTEXT and local_path:
            try:
                fulltext_name = safe_book_filename(title) + ".md"
                fulltext_dir = ATTACHMENTS_DIR / "BookTexts"
                fulltext_dir.mkdir(parents=True, exist_ok=True)
                (fulltext_dir / fulltext_name).write_text(
                    book_to_markdown(book_meta, chapters), encoding="utf-8"
                )
                logger.info(f"Full-text markdown saved: {fulltext_dir / fulltext_name}")
            except Exception as e:
                logger.error(f"Failed to write full-text markdown: {e}")
                fulltext_name = ""

        await status.edit_text(f"📚 Synthesizing {total} chapter summaries…")
        digest = "\n".join(
            f"Chapter {i} — {n['title']}: {n['summary'][:300].replace(chr(10), ' ')}"
            for i, n in enumerate(chapter_notes, 1)
        )
        synth_prompt = BOOK_SYNTHESIS_PROMPT.format(book_title=title, authors=authors)
        synth_text, _ = await chat(synth_prompt, digest, max_tokens=2000)

        note_dict = _parse_response(synth_text)
        if not note_dict or not note_dict.get("content"):
            note_dict = {
                "title": f"{title} — Study Notes",
                "category": "books",
                "content": synth_text,
            }

        # Assemble the final note locally: synthesis + one section per chapter
        body = note_dict["content"].rstrip()
        body += "\n\n## 📖 Chapter-by-Chapter\n"
        for i, n in enumerate(chapter_notes, 1):
            summary = n["summary"]
            if "KEYWORD:" in summary:  # strip the keyword footer line
                summary = summary.split("KEYWORD:", 1)[0]
            body += f"\n### {i}. {n['title']}\n\n{summary.strip()}\n"
        if fulltext_name:
            link = fulltext_name.removesuffix(".md")
            body += (
                f"\n## 📚 Full Text\n"
                f"The complete book converted to Markdown: [[{link}]]\n"
            )
        note_dict["content"] = body

        note_dict.update(
            {
                "source": source,
                "source_type": "book",
                "attachment": attachment,
                "detail_level": detail_level,
                "book_title": title,
                "book_authors": book_meta.get("authors", []),
                "book_year": book_meta.get("year", ""),
            }
        )

        # Task 2: link the book into the Obsidian graph via topic hashtags
        related = [
            str(t).replace(" ", "-") for t in note_dict.get("tags", [])
            if t and str(t).lower() not in ("book", "books")
        ][:5]
        if related:
            note_dict["content"] += (
                "\n\n## 🧭 Related Topics\n"
                + " ".join(f"#{t}" for t in related)
            )

        note_path = write_note_to_vault(note_dict)
        if not note_path:
            await status.edit_text("❌ Could not write to Obsidian vault.")
            return
        if fingerprint:
            await arecord_processed(fingerprint, "book", source, note_path)

        words = len((note_dict.get("content") or "").split())
        await status.edit_text(
            f"✅ Study notes ready!\n📂 {note_path}\n\n"
            f"📚 {title}\n✍️ {authors}\n"
            f"📝 ~{words} words of distilled knowledge\n"
            f"📎 Attachment: {attachment or 'none'}",
            disable_web_page_preview=True,
        )
    except ProviderRateLimitError as e:
        logger.error(f"Rate Limit or Quota Exceeded: {e}")
        try:
            await status.edit_text(await _quota_error_text(e))
        except Exception:
            pass
    except AllProvidersFailedError as e:
        logger.error(f"Book pipeline failed — providers exhausted: {e.attempts}")
        try:
            await update.message.reply_text(
                "🚨 Book processing failed — all LLM models are unavailable.\n"
                "Run /models to switch to a working model, then re-send the file."
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Book pipeline error: {e}")
        try:
            await status.edit_text(f"❌ Book processing failed: {str(e)[:200]}")
        except Exception:
            pass


async def analyze_and_save(
    update, context, text, detail_level, source, source_type,
    attachment=None, source_kind=None,
    fingerprint: str = "", force: bool = False,
    thumbnail: str = "",
    attachments: Optional[List[str]] = None,
):
    """Run AI analysis and persist the resulting knowledge note.

    Returns the string "duplicate" when the dedup gate rejected the content
    (already saved, no --force), True on success; raises on hard failures.
    """
    if await _reject_duplicate(update, fingerprint, force):
        return "duplicate"
    source_url = source if source_type == "link" else ""
    if source_kind is None:
        source_kind = "document"

    try:
        note_dict = await analyze_content(
            text, detail_level, source_url=source_url, source_kind=source_kind
        )
    except ProviderRateLimitError as e:
        logger.error(f"Rate Limit or Quota Exceeded: {e}")
        await update.message.reply_text(await _quota_error_text(e))
        e.handled = True
        raise e
    except AllProvidersFailedError as e:
        logger.error(f"All providers failed: {[a.get('error') for a in e.attempts]}")
        await _offer_model_switch(update, context)
        e.handled = True
        raise e
    except Exception as e:
        logger.error(f"AI analysis failed: {e}")
        await update.message.reply_text(f"❌ AI analysis failed. Error: {e}")
        e.handled = True
        raise e

    if not note_dict:
        await update.message.reply_text("❌ AI analysis returned nothing. Check logs.")
        raise ValueError("AI analysis returned nothing")

    note_dict["source"] = source
    note_dict["source_type"] = source_type
    note_dict["attachment"] = attachment
    if attachments:
        note_dict["attachments"] = list(attachments)
    if thumbnail:
        note_dict["thumbnail"] = thumbnail
        note_dict["content"] = f"![[{thumbnail}]]\n\n{note_dict.get('content', '')}"

    note_path = write_note_to_vault(note_dict)
    if not note_path:
        await update.message.reply_text("❌ Could not write to Obsidian vault.")
        raise OSError("Could not write to vault")

    if fingerprint:
        await arecord_processed(fingerprint, source_type, source, note_path)

    preview = (note_dict.get("content") or "")[:400]
    msg = f"✅ Saved to vault!\n📂 {note_path}\n\n📝 {note_dict.get('title')}\n---\n{preview}"
    meta = note_dict.get("_meta_info", {})
    if meta:
        model_used = meta.get("model", "unknown")
        usage = meta.get("usage")
        if usage:
            msg += f"\n\n🤖 Modelo: {model_used} | Tokens: {usage.get('total_tokens', 0)} ({usage.get('prompt_tokens', 0)} in, {usage.get('completion_tokens', 0)} out)"
        else:
            msg += f"\n\n🤖 Modelo: {model_used} (Tokens não reportados)"
    
    if low_disk(VAULT_PATH):
        msg += f"\n\n⚠️ Disk space low: {format_disk(VAULT_PATH)}"
    await update.message.reply_text(
        msg,
        disable_web_page_preview=True,
    )
    return True


async def _offer_model_switch(update, context):
    """All providers failed — show which models ARE available right now."""
    try:
        catalog = await list_available_models()
    except Exception as e:
        logger.error(f"Could not fetch models for fallback UI: {e}")
        catalog = get_catalog()

    if not catalog:
        await update.message.reply_text(
            "🚨 AI analysis failed: no LLM provider is reachable.\n"
            "Check your API keys (GEMINI_API_KEY / GROQ_API_KEY) in .env."
        )
        return

    markup, count = _build_model_keyboard(context, catalog)
    await update.message.reply_text(
        f"🚨 AI analysis failed — configured models are unavailable.\n"
        f"{count} working models found. Tap one to switch and retry:",
        reply_markup=markup,
    )


# ---- Model check (only on user request or when quota is exhausted) ----

async def _quota_error_text(e: Exception) -> str:
    """
    Error text for quota exhaustion. Only NOW do we run the auto-switch check —
    never proactively, so no unsolicited "model check" messages are sent.
    """
    provider = getattr(e, "provider", "unknown")
    text = (
        f"⚠️ Quota excedida ou Rate Limit atingido no provider {provider}. "
        f"Tente novamente mais tarde.\nDetalhes: {e}"
    )
    try:
        report = await validate_and_autoswitch()
    except Exception as ex:
        logger.warning(f"Post-quota autoswitch check failed: {ex}")
        report = None
    if report:
        text += f"\n\n🩺 {report}"
    return text


# ---- Disk-space monitoring (Task: warn when free space < 20%) ----

async def disk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual /disk — report current vault disk usage."""
    pct = await asyncio.to_thread(free_percent, VAULT_PATH)
    if pct is None:
        await update.message.reply_text("Could not read disk stats.")
        return
    text = format_disk(VAULT_PATH)
    if pct * 100 < float(os.getenv("DISK_WARN_THRESHOLD_PCT", "20")):
        await update.message.reply_text(f"Low disk!\n{text}")
    else:
        await update.message.reply_text(f"Disk OK\n{text}")


async def disk_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every DISK_JOB_HOURS: proactively alert if disk is low (anti-spam)."""
    await _maybe_send_disk_alert(context.bot)


async def _maybe_send_disk_alert(bot):
    """Send one proactive alert if disk is low AND the anti-spam cooldown elapsed."""
    global _last_disk_alert
    if not low_disk(VAULT_PATH):
        return
    now = time.time()
    if now - _last_disk_alert < DEFAULT_ALERT_MINUTES * 60:
        return  # too soon since last alert
    _last_disk_alert = now
    text = disk_alert_text(VAULT_PATH)
    chat_id = TELEGRAM_CHAT_ID
    if chat_id:
        try:
            await bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as e:
            logger.error(f"Could not send disk alert: {e}")


# ---- Recent-notes dashboard (vault note — silent, no Telegram messages) ----

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rebuild Recent Notes.md at the vault root (newest per category)."""
    path = await asyncio.to_thread(write_dashboard, VAULT_PATH)
    if path:
        await update.message.reply_text(
            f"📊 Dashboard updated (last {DASHBOARD_DAYS} day(s)):\n"
            f"📄 {path.name} — open it in Obsidian to see the newest notes per category."
        )
    else:
        await update.message.reply_text(
            f"No notes created in the last {DASHBOARD_DAYS} day(s) — nothing to show yet."
        )


async def dashboard_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    """Weekly silent refresh — only rewrites the vault note, never messages."""
    try:
        path = await asyncio.to_thread(write_dashboard, VAULT_PATH)
        if path:
            logger.info(f"Dashboard auto-refreshed: {path}")
    except Exception as e:
        logger.warning(f"Dashboard refresh failed: {e}")


async def post_init(application: Application):
    """Startup tasks: schedule the disk job only. Model checks never run
    proactively — they happen on /models (user request) or when quota runs out."""
    if application.job_queue:
        application.job_queue.run_repeating(
            disk_check_job,
            interval=DISK_JOB_HOURS * 3600,
            first=45,
            name="disk_check",
            data={"chat_id": TELEGRAM_CHAT_ID},
        )
        application.job_queue.run_repeating(
            dashboard_refresh_job,
            interval=7 * 24 * 60 * 60,
            first=90,
            name="dashboard_refresh",
        )

    # Initial disk check (sends an alert immediately if already low).
    await _maybe_send_disk_alert(application.bot)


def main():
    """Start the Telegram bot."""
    setup_error_logging()
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(CommandHandler("disk", disk_command))
    app.add_handler(CommandHandler("voice", voice_note_command))
    app.add_handler(CommandHandler("audio", voice_note_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("organize", organize_command))
    app.add_handler(CommandHandler("dashboard", dashboard_command))
    app.add_handler(CallbackQueryHandler(organize_callback, pattern=r"^org:(yes|no)$"))
    for cmd in ("summarize", "detailed", "precise", "raw", "book", "handwritten"):
        app.add_handler(CommandHandler(cmd, set_detail_command))
    app.add_handler(CommandHandler("learn", learn_command))
    app.add_handler(CallbackQueryHandler(model_choice_callback, pattern=r"^swm:\d+$"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(
        MessageHandler(filters.VIDEO | (filters.Document.VIDEO & filters.Document.ALL), handle_video)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Catch-all: any content type no handler claimed gets an answer (Task 8)
    app.add_handler(MessageHandler(filters.ALL, handle_unsupported))
    app.add_error_handler(on_error)
    logger.info("Bot started. Current LLM model: %s", get_current_model())
    app.run_polling()


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        "Received UNSUPPORTED update: %s",
        (update.message.to_dict() if update.message else "?"),
    )
    """Reply to content types the bot can't process yet (stickers, GIFs…)."""
    if update.message and not update.message.text:
        await update.message.reply_text(
            "🤔 I can't process this content type yet.\n"
            "Supported: images/albums, documents (PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML), links, "
            "e-books, voice/audio, video files and video links."
        )


# ---- Photo / album ingestion (v1.9) ----

ALBUM_HOLD_SECONDS = 3
_album_buffers: dict = {}


def _wants_image_analysis(caption: str) -> bool:
    """/image caption command → detailed analysis like the link flow."""
    return bool(caption) and caption.strip().lower().startswith("/image")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo/screenshot → LLM vision (extract text+diagrams) → OCR fallback → note."""
    logger.info(
        "Received PHOTO: media_group=%s sizes=%s",
        update.message.media_group_id if update.message else None,
        [p.file_size for p in (update.message.photo or [])],
    )
    media_group_id = update.message.media_group_id
    if media_group_id:
        # Album members are aggregated into ONE event — no per-photo rate limit,
        # otherwise every photo after the first spams the cooldown warning.
        photos = update.message.photo or []
        if not photos:
            return
        await _buffer_album_photo(update, context, photos[-1], media_group_id)
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if await _rate_limited_reply(update, user_id):
        return

    photos = update.message.photo or []
    if not photos:
        await update.message.reply_text("❌ No image received.")
        return
    photo = photos[-1]  # largest available size

    status = StatusMessage(await update.message.reply_text("🖼️ Reading image…"))
    await run_with_deadline(
        status,
        _photo_job(update, context, [update.message], status),
    )


async def _buffer_album_photo(update, context, photo, media_group_id):
    """Collect album photos; process the whole group after a short hold."""
    buf = _album_buffers.setdefault(
        media_group_id,
        {"messages": [], "caption": "", "task": None, "context": context},
    )
    buf["messages"].append(update.message)
    # Keep the full Update (has effective_chat / .message.reply_text) — the
    # album job must reply with a real Update, not a bare Message.
    if "first_update" not in buf:
        buf["first_update"] = update
    if update.message.caption:
        buf["caption"] = update.message.caption
    logger.info(
        "Album %s buffered: %d image(s) so far", media_group_id, len(buf["messages"])
    )
    if buf["task"] is None or buf["task"].done():
        try:
            buf["task"] = asyncio.create_task(_process_album(media_group_id))
            # Safety net: a task that dies unobserved must at least scream in
            # the logs (it cannot reach the user if the loop already collapsed).
            buf["task"].add_done_callback(_log_task_exception)
        except Exception as e:
            logger.error("Could not start album task: %s", e, exc_info=True)


def _log_task_exception(task) -> None:
    """done_callback: surface exceptions that would otherwise be swallowed."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Album background task crashed: %s", exc, exc_info=exc)


async def _process_album(media_group_id):
    await asyncio.sleep(ALBUM_HOLD_SECONDS)
    buf = _album_buffers.pop(media_group_id, None)
    if not buf or not buf["messages"]:
        return
    update = buf.get("first_update") or buf["messages"][0]
    if not hasattr(update, "effective_chat"):
        # Legacy buffer without the full Update — rebuild the chat reply target.
        logger.error("Album %s has no first_update; cannot reply", media_group_id)
        return
    logger.info(
        "Album %s processing started (%d images, caption=%r)",
        media_group_id, len(buf["messages"]), buf["caption"][:40],
    )
    status = None
    try:
        status = StatusMessage(
            await update.effective_chat.send_message(
                f"🖼️ Reading album ({len(buf['messages'])} images)…"
            )
        )
        await run_with_deadline(
            status,
            _photo_job(update, buf.get("context"), buf["messages"], status, caption=buf["caption"]),
        )
    except Exception as e:
        # create_task swallows exceptions silently — the user would get NO
        # response at all. Always report what happened (real cause when the
        # error was not already surfaced downstream).
        logger.error("Album %s processing failed: %s", media_group_id, e, exc_info=True)
        if not getattr(e, "handled", False):
            try:
                if status is None:
                    # even the initial status message failed — reply directly
                    await update.effective_chat.send_message(
                        f"❌ Album processing failed: {e}"
                    )
                else:
                    await status.fail(
                        f"Album processing failed: {e}\n"
                        "Try sending the images again (or one by one)."
                    )
            except Exception:
                logger.error("Could not deliver album failure message", exc_info=True)
    finally:
        _album_buffers.pop(media_group_id, None)


class PhotoDownloadError(Exception):
    """Raised when a Telegram media download fails after retries."""


class FileTooBigError(PhotoDownloadError):
    """Media exceeds the Telegram Bot API 20MB download limit."""


async def _download_telegram_file(media, dest_dir: str, what: str = "file") -> str:
    """Robustly download a Telegram File to dest_dir.

    - Pre-checks file_size against the Bot API 20MB limit (raises FileTooBigError).
    - Retries once on transient network errors, then falls back to
      download_to_memory + manual write.
    Returns the local file path. Raises PhotoDownloadError with the real
    exception chained, so callers can show an honest error message.
    """
    size = getattr(media, "file_size", None) or 0
    if size > TELEGRAM_DOWNLOAD_LIMIT:
        raise FileTooBigError(
            f"{size / (1024 * 1024):.0f}MB exceeds the 20MB Bot API limit"
        )
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            file_obj = await media.get_file()
            return await file_obj.download_to_drive(dest_dir)
        except Exception as e:
            last_exc = e
            logger.warning(
                "%s download attempt %d/2 failed: %s [%s]",
                what, attempt, e, type(e).__name__,
            )
            await asyncio.sleep(1)
    # Last resort: in-memory download (survives odd drive-write failures)
    try:
        file_obj = await media.get_file()
        buf = await file_obj.download_as_bytearray()
        dest = Path(dest_dir) / f"{what.replace(' ', '_')}.bin"
        dest.write_bytes(bytes(buf))
        logger.info("%s downloaded via memory fallback (%d bytes)", what, len(buf))
        return str(dest)
    except Exception as e:
        last_exc = e
    logger.error("%s download failed for good: %s [%s]",
                 what, last_exc, type(last_exc).__name__, exc_info=True)
    raise PhotoDownloadError(
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def _download_error_text(what: str, err: Exception) -> str:
    """Honest, actionable download-failure message (no wrong 20MB blame)."""
    if isinstance(err, FileTooBigError):
        return (
            f"❌ This {what} is too big for the Telegram Bot API "
            "(downloads are capped at 20MB). Compress it or send a link instead."
        )
    return (
        f"❌ Could not download the {what} from Telegram.\n"
        f"Real error: {err}\n"
        "It may be a temporary network issue — try again in a moment."
    )


async def _photo_job(update, context, messages, status, caption: str = ""):
    """Download N photos → fingerprint → vision/OCR extraction → analyze_and_save."""
    with tempfile.TemporaryDirectory() as tmp:
        paths: list = []
        for i, msg in enumerate(messages, 1):
            photo = msg.photo[-1] if msg.photo else None
            if photo is None:
                continue
            try:
                p = await _download_telegram_file(
                    photo, tmp, what=f"photo {i} of {len(messages)}"
                )
            except Exception as e:
                logger.error(
                    "Photo %d/%d download failed (media_group=%s): %s",
                    i, len(messages),
                    getattr(msg, "media_group_id", None), e, exc_info=True,
                )
                await status.fail(_download_error_text(f"photo {i}/{len(messages)}", e))
                return
            paths.append(str(p))
        if not paths:
            await status.fail("No image data received.")
            return

        # Combined fingerprint so re-sending the same album is deduped.
        fps = [
            await asyncio.to_thread(compute_file_fingerprint, p) for p in paths
        ]
        fingerprint = (
            fps[0]
            if len(fps) == 1
            else f"image-album::{hashlib.sha256(','.join(fps).encode()).hexdigest()}"
        )
        if not caption and messages:
            caption = messages[0].caption or ""
        force = _wants_force(caption)
        # /image caption command → force the detailed-analysis flow (like links)
        image_cmd = _wants_image_analysis(caption)
        if await _reject_duplicate(update, fingerprint, force):
            return

        attachment_rel = _save_attachment(paths[0]) if len(paths) == 1 else None
        detail = derive_detail_level(caption) if caption else "detailed"
        if image_cmd:
            detail = "detailed"

        # --- HANDWRITTEN route (v1.10, DEV): verbatim transcription, no summarization ---
        if detail == "handwritten":
            await _handwritten_job(update, context, paths, status, fingerprint, force)
            return

        # Snapshot the active TEXT model — /image uses vision models (free chain
        # → paid Gemini) and must leave the user's model choice untouched.
        prev_model = get_current_model()
        try:
            extracted = await vision_extract(paths, detailed=image_cmd)
        except Exception as e:
            logger.error("Vision extraction failed for %d image(s): %s",
                         len(paths), e, exc_info=True)
            await status.fail(
                "The vision model could not process these images "
                "(unsupported format, too large, or provider error)."
            )
            return
        if not extracted:
            await status.fail("Could not read any content from these images.")
            return
        source_name = Path(paths[0]).name
        source_label = "photograph" if len(paths) == 1 else f"album of {len(paths)} photos"
        # Attach ALL originals to the note (gallery embeds in Obsidian)
        attachment_rel = None
        attachments = _save_attachments(paths)

    await status.update("🧠 Interpreting image content…")
    result = await analyze_and_save(
        update,
        context,
        extracted,
        detail,
        source=f"telegram-{source_label}::{source_name}",
        source_type="image",
        source_kind="image",
        attachment=attachments[0] if attachments else None,
        attachments=attachments,
        fingerprint=fingerprint,
        force=force,
    )
    # Restore the model that was active before the /image vision chain ran
    if get_current_model() != prev_model:
        set_current_model(prev_model)
        logger.info("Restored previous text model after /image: %s", prev_model)


async def _handwritten_job(update, context, paths, status, fingerprint, force):
    """Handwritten photos → verbatim transcription (pt-PT) → direct note, no summarization."""
    await status.update("✍️ Transcribing handwritten note (pt-PT)…")
    try:
        text = await transcribe_handwritten(paths)
    except Exception as e:
        logger.error(f"Handwritten transcription failed: {e}")
        await status.fail(f"Handwritten transcription failed: {e}")
        return
    if not text:
        await status.fail("Could not read the handwriting. Ensure the photo is well lit.")
        return

    # Reuse the META_JSON parser: the model outputs body + META_JSON line.
    note_dict = _parse_response(text)
    body = note_dict.get("content") or text if note_dict else text
    # Strip any META_JSON remnants from body display
    import re as _re

    if body:
        body = _re.sub(r"\n?META_JSON:\s*\{.*\}\s*$", "", body, flags=_re.S).strip()
    if not note_dict:
        note_dict = {
            "title": "Handwritten Note",
            "category": "uncategorized",
            "tags": ["handwritten"],
        }
    note_dict.update(
        {
            "content": body or note_dict.get("content", text),
            "source": f"telegram-handwritten::{Path(paths[0]).name}",
            "source_type": "handwritten",
            "detail_level": "handwritten",
            "tags": list(dict.fromkeys(["handwritten"] + note_dict.get("tags", []))),
        }
    )
    note_path = write_note_to_vault(note_dict)
    if not note_path:
        await status.fail("Could not write to Obsidian vault.")
        return
    await arecord_processed(fingerprint, "handwritten", note_dict["source"], note_path)
    await status.update(
        f"✍️ Handwritten note saved!\n📂 {note_path}\n📝 {note_dict.get('title')}\n\n"
        "⚠️ Feature in development — verify the transcription made sense."
    )


if __name__ == "__main__":
    main()