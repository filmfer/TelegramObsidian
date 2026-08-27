"""Telegram → Gemini → Obsidian Knowledge Agent."""
import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

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
from parsers.link_parser import parse_link
from parsers.book_parser import (
    clean_book_text,
    extract_book_metadata,
    is_book_file,
    split_into_chunks,
)
from parsers.audio_parser import TranscriptionError, transcribe_audio
from parsers.search_parser import search_web
from parsers.video_parser import (
    extract_audio_track,
    extract_youtube_id,
    fetch_youtube_metadata,
    fetch_youtube_transcript,
    is_youtube_url,
)
from llm.analyzer import (
    BOOK_FINAL_PROMPT,
    BOOK_SECTION_PROMPT,
    RESEARCH_PROMPT,
    VIDEO_PROMPT,
    CATEGORIES,
    _parse_response,
    analyze_content,
)
from llm.provider import chat
from llm.provider import (
    AllProvidersFailedError,
    _free_score,
    get_catalog,
    get_current_model,
    list_available_models,
    set_current_model,
    validate_and_autoswitch,
)
from storage.vault_writer import derive_detail_level, write_note_to_vault
from storage.dedup_store import (
    acheck_duplicate,
    arecord_processed,
    compute_file_fingerprint,
    compute_text_fingerprint,
    compute_url_fingerprint,
    init_db,
)

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
init_db()  # dedup + pending-queue SQLite store (data/agent.db)

DETAIL_LEVELS = {"summarize", "detailed", "precise", "raw", "book"}
USER_COOLDOWN_SECONDS = 10
_user_last_request: dict = {}


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

WEEKLY_SECONDS = 7 * 24 * 60 * 60
MAX_MODEL_BUTTONS = 30


# ---- Command handlers ----

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Obsidian Knowledge Agent ready.\n\n"
        "Send me:\n"
        "• Documents (PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML)\n"
        "• E-books (EPUB/MOBI/AZW/DJVU/FB2/LIT) → deep study notes\n"
        "• Links (https://…)\n"
        "• Plain text thoughts — structured & categorized by AI\n"
        "• Voice messages 🎙️ — transcribed into notes\n\n"
        "/research <topic> — deep web research, cited sources\n"
        "Detail levels: /summarize /detailed /precise /raw /book\n"
        "LLM models: /models"
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
    now = time.monotonic()
    last = _user_last_request.get(user_id, 0)
    if now - last < USER_COOLDOWN_SECONDS:
        return False
    _user_last_request[user_id] = now
    return True


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else 0
    if not _check_rate_limit(user_id):
        await update.message.reply_text(
            f"⏳ Please wait {USER_COOLDOWN_SECONDS}s between uploads."
        )
        return

    caption = update.message.caption
    detail_level = (
        derive_detail_level(caption)
        if caption
        else context.user_data.get("detail_level", "detailed")
    )

    file_obj = await update.message.document.get_file()
    if not file_obj:
        await update.message.reply_text("❌ No valid document received.")
        return

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
    user_id = update.effective_user.id if update.effective_user else 0
    if not _check_rate_limit(user_id):
        await update.message.reply_text(
            f"⏳ Please wait {USER_COOLDOWN_SECONDS}s between requests."
        )
        return

    text = update.message.text.strip()

    if text.startswith(("http://", "https://")):
        if is_youtube_url(text):
            await _process_youtube(update, context, text)
            return
        content = await parse_link(text)
        if not content:
            await update.message.reply_text(
                "❌ Could not read the link (it may block bots).\n"
                "Tip: copy the page text and send it here instead — I'll turn it into a note."
            )
            return
        detail = context.user_data.get("detail_level") or "summarize"
        await analyze_and_save(
            update, context, content, detail,
            source=text, source_type="link", source_kind="document",
            fingerprint=compute_url_fingerprint(text),
            force=_wants_force(text),
        )
        return

    # --- Personal text note (#3): structure + categorize the user's thought ---
    detail = context.user_data.get("detail_level", "detailed")
    await analyze_and_save(
        update, context, text[:12000], detail,
        source="telegram-text::manual note",
        source_type="text",
        source_kind="text",
        fingerprint=compute_text_fingerprint(text[:12000]),
        force=_wants_force(text),
    )


# ---- Voice notes (#4) ----

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe Telegram voice/audio via free Groq Whisper, then note it."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not _check_rate_limit(user_id):
        await update.message.reply_text(
            f"⏳ Please wait {USER_COOLDOWN_SECONDS}s between requests."
        )
        return

    media = update.message.voice or update.message.audio
    status = await update.message.reply_text("🎙️ Transcribing audio…")

    with tempfile.TemporaryDirectory() as tmp:
        file_obj = await media.get_file()
        local_path = await file_obj.download_to_drive(tmp)
        fingerprint = await asyncio.to_thread(compute_file_fingerprint, local_path)
        try:
            transcript = await transcribe_audio(local_path)
        except TranscriptionError as e:
            await status.edit_text(f"❌ Transcription failed: {e}")
            return

    caption = (update.message.caption or "").strip()
    want_research = "research" in caption.lower()
    detail = derive_detail_level(caption) if caption else context.user_data.get("detail_level", "detailed")

    preview = transcript[:300] + ("…" if len(transcript) > 300 else "")
    await status.edit_text(f"🎙️ Transcribed:\n\n{preview}\n\n✍️ Creating your note…")

    if want_research:
        await _research_topic(update, context, transcript[:500], status)
        return

    await analyze_and_save(
        update, context, transcript, detail,
        source="telegram-voice::transcription",
        source_type="voice",
        source_kind="text",
        fingerprint=fingerprint,
        force=_wants_force(caption),
    )


# ---- Deep research (#2) ----

async def research_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/research <topic> — web search + synthesis into a cited study note."""
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text(
            "Usage: /research <topic>\nExample: /research best linux hardening practices 2026"
        )
        return
    status = await update.message.reply_text(f"🔎 Researching “{topic}”…")
    await _research_topic(update, context, topic, status)


async def _research_topic(update, context, topic, status):
    """Search web → scrape top sources → LLM synthesis → cited note."""
    fingerprint = compute_text_fingerprint(topic)
    if await _reject_duplicate(update, fingerprint, _wants_force(topic)):
        return
    results = await asyncio.to_thread(search_web, topic, 5)
    if not results:
        await status.edit_text("❌ No search results found for this topic.")
        return

    sources_payload = []
    for r in results[:4]:
        title = r.get("title") or r.get("url", "")[:60]
        try:
            await status.edit_text(f"🔎 Reading: {title[:60]}…")
            content = await parse_link(r["url"]) or ""
        except Exception:
            content = ""
        body = content[:5000] or f"(snippet) {r.get('snippet', '')}"
        sources_payload.append(f"### Source: {title}\nURL: {r['url']}\n\n{body}")

    prompt = RESEARCH_PROMPT.format(topic=topic, categories=CATEGORIES)
    payload = "\n\n---\n\n".join(sources_payload)

    await status.edit_text("🧠 Synthesizing research…")
    note_text = await chat(prompt, payload, max_tokens=6000)

    note_dict = _parse_response(note_text)
    if not note_dict:
        await status.edit_text("❌ Research synthesis failed.")
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
        await status.edit_text("❌ Could not write to Obsidian vault.")
        return
    await arecord_processed(fingerprint, "research", f"research::{topic}", note_path)
    await status.edit_text(
        f"🔎 Research saved!\n📂 {note_path}\n📝 {note_dict.get('title')}",
        disable_web_page_preview=True,
    )


# ---- Video handling (#5) ----

async def _process_youtube(update, context, url):
    """YouTube link → free caption transcript → knowledge note with video info."""
    video_id = extract_youtube_id(url) or ""
    detail = context.user_data.get("detail_level") or "summarize"
    fingerprint = compute_url_fingerprint(url)
    if await _reject_duplicate(update, fingerprint, _wants_force(url)):
        return
    status = await update.message.reply_text("🎬 Fetching video transcript…")

    meta, transcript = {}, None
    if video_id:
        meta = await asyncio.to_thread(fetch_youtube_metadata, video_id)
        transcript = await asyncio.to_thread(fetch_youtube_transcript, video_id)

    if not transcript:
        await status.edit_text(
            "❌ No transcript available for this video (captions may be disabled).\n"
            "Tip: upload the video file here and I'll transcribe the audio instead."
        )
        return

    title = meta.get("title") or f"Video {video_id}"
    author = meta.get("author", "")
    await status.edit_text(f"🧠 Summarizing “{title[:60]}”…")

    payload = (
        f"Video URL: {url}\nTitle: {title}\nChannel: {author}\n\n"
        f"Transcript:\n{transcript[:50000]}"
    )
    note_dict = _parse_response(await chat(VIDEO_PROMPT.format(categories=CATEGORIES), payload, max_tokens=6000))
    if not note_dict:
        await status.edit_text("❌ Video summarization failed.")
        return

    tags = note_dict.get("tags", [])
    if "video" not in tags:
        tags.insert(0, "video")
    note_dict.update(
        {
            "tags": tags,
            "source": url,
            "source_type": "video",
            "detail_level": detail,
            "content": f"🔗 {url}\n📺 {title}" + (f" — {author}" if author else "") + f"\n\n{note_dict.get('content','')}",
        }
    )

    note_path = write_note_to_vault(note_dict)
    if not note_path:
        await status.edit_text("❌ Could not write to Obsidian vault.")
        return
    await arecord_processed(fingerprint, "video", url, note_path)
    await status.edit_text(f"🎬 Video note saved!\n📂 {note_path}\n📝 {note_dict.get('title')}", disable_web_page_preview=True)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uploaded video → ffmpeg extracts audio → Groq Whisper → knowledge note."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not _check_rate_limit(user_id):
        await update.message.reply_text(f"⏳ Please wait {USER_COOLDOWN_SECONDS}s between requests.")
        return

    video = update.message.video or update.message.document
    size_mb = (video.file_size or 0) / (1024 * 1024)
    if size_mb > 20:
        await update.message.reply_text(
            f"❌ This video is {size_mb:.0f}MB — Telegram's Bot API only allows downloads up to 20MB.\n"
            "Tip: compress it, or send a platform link (YouTube etc.) instead."
        )
        return

    status = await update.message.reply_text("🎬 Downloading & extracting audio track…")
    with tempfile.TemporaryDirectory() as tmp:
        file_obj = await video.get_file()
        local_path = await file_obj.download_to_drive(tmp)
        fingerprint = await asyncio.to_thread(compute_file_fingerprint, local_path)
        if await _reject_duplicate(update, fingerprint, _wants_force(caption := (update.message.caption or "").strip())):
            return
        out_mp3 = str(Path(tmp) / "audio.mp3")
        try:
            await asyncio.to_thread(extract_audio_track, local_path, out_mp3)
        except RuntimeError as e:
            await status.edit_text(f"❌ {e}")
            return
        try:
            transcript = await transcribe_audio(out_mp3)
        except TranscriptionError as e:
            await status.edit_text(f"❌ Transcription failed: {e}")
            return

    caption = (update.message.caption or "").strip()
    detail = derive_detail_level(caption) if caption else context.user_data.get("detail_level", "detailed")

    await status.edit_text("🧠 Summarizing video content…")
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
    Map-reduce study-note pipeline:
      clean → split into section-sized chunks → extract knowledge per chunk
      (map) → merge into one comprehensive study note (reduce).
    Uses free-tier models; progress is edited into the status message.
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

        chunks = split_into_chunks(raw)[:40]  # safety cap for very large books
        total = len(chunks)
        logger.info(f"Book '{title}': {len(raw)} chars → {total} sections")

        extractions = []
        for i, chunk in enumerate(chunks, 1):
            prompt = BOOK_SECTION_PROMPT.format(
                section_num=i, total=total, book_title=title, authors=authors
            )
            extractions.append(f"## Section {i}\n{await chat(prompt, chunk, max_tokens=2500)}")
            if i % 2 == 0 or i == total:
                try:
                    await status.edit_text(
                        f"📖 Processing “{title}”\n🧠 Extracting knowledge — section {i}/{total}…"
                    )
                except Exception:
                    pass  # message may be identical; ignore edit errors
            await asyncio.sleep(0.5)  # be polite to free-tier rate limits

        await status.edit_text(f"📚 Merging {total} sections into your study note…")
        final_prompt = BOOK_FINAL_PROMPT.format(book_title=title, authors=authors)
        merged = "\n\n".join(extractions)
        final_note = await chat(final_prompt, merged[-100000:], max_tokens=8000)

        note_dict = _parse_response(final_note)
        if not note_dict or not note_dict.get("content"):
            await status.edit_text("❌ Book synthesis failed — please try again.")
            return

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
):
    """Run AI analysis and persist the resulting knowledge note."""
    if await _reject_duplicate(update, fingerprint, force):
        return
    source_url = source if source_type == "link" else ""
    if source_kind is None:
        source_kind = "document"

    try:
        note_dict = await analyze_content(
            text, detail_level, source_url=source_url, source_kind=source_kind
        )
    except AllProvidersFailedError as e:
        logger.error(f"All providers failed: {[a.get('error') for a in e.attempts]}")
        await _offer_model_switch(update, context)
        return
    except Exception as e:
        logger.error(f"AI analysis failed: {e}")
        await update.message.reply_text("❌ AI analysis failed. Check logs.")
        return

    if not note_dict:
        await update.message.reply_text("❌ AI analysis returned nothing. Check logs.")
        return

    note_dict["source"] = source
    note_dict["source_type"] = source_type
    note_dict["attachment"] = attachment

    note_path = write_note_to_vault(note_dict)
    if not note_path:
        await update.message.reply_text("❌ Could not write to Obsidian vault.")
        return

    if fingerprint:
        await arecord_processed(fingerprint, source_type, source, note_path)

    preview = (note_dict.get("content") or "")[:400]
    await update.message.reply_text(
        f"✅ Saved to vault!\n📂 {note_path}\n\n📝 {note_dict.get('title')}\n---\n{preview}",
        disable_web_page_preview=True,
    )


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


# ---- Weekly model health check ----

async def weekly_model_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs at startup + once a week: refresh catalog, auto-switch if needed."""
    report = await validate_and_autoswitch()
    chat_id = (context.job.data or {}).get("chat_id") or TELEGRAM_CHAT_ID
    if report and chat_id:
        try:
            await context.bot.send_message(
                chat_id=int(chat_id), text=f"🩺 Model check\n\n{report}"
            )
        except Exception as e:
            logger.error(f"Could not deliver model-check report: {e}")


async def post_init(application: Application):
    """Startup tasks: schedule the weekly job + run first check immediately."""
    if application.job_queue:
        application.job_queue.run_repeating(
            weekly_model_check_job,
            interval=WEEKLY_SECONDS,
            first=20,
            name="weekly_model_check",
            data={"chat_id": TELEGRAM_CHAT_ID},
        )
    try:
        report = await validate_and_autoswitch()
        if report and TELEGRAM_CHAT_ID:
            await application.bot.send_message(
                chat_id=int(TELEGRAM_CHAT_ID),
                text=f"🩺 Startup model check\n\n{report}",
            )
    except Exception as e:
        logger.warning(f"Startup model check failed: {e}")


def main():
    """Start the Telegram bot."""
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
    app.add_handler(CommandHandler("research", research_command))
    for cmd in ("summarize", "detailed", "precise", "raw", "book"):
        app.add_handler(CommandHandler(cmd, set_detail_command))
    app.add_handler(CallbackQueryHandler(model_choice_callback, pattern=r"^swm:\d+$"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(
        MessageHandler(filters.VIDEO | (filters.Document.VIDEO & filters.Document.ALL), handle_video)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started. Current LLM model: %s", get_current_model())
    app.run_polling()


if __name__ == "__main__":
    main()