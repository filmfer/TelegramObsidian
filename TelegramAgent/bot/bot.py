"""Telegram → Gemini → Obsidian Knowledge Agent."""
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

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
from parsers.book_parser import extract_book_metadata, is_book_file
from llm.analyzer import analyze_content
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

DETAIL_LEVELS = {"summarize", "detailed", "precise", "raw", "book"}
USER_COOLDOWN_SECONDS = 10
_user_last_request: dict = {}

WEEKLY_SECONDS = 7 * 24 * 60 * 60
MAX_MODEL_BUTTONS = 30


# ---- Command handlers ----

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Obsidian Knowledge Agent ready.\n\n"
        "Send me:\n"
        "• Documents (PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML)\n"
        "• E-books (EPUB/MOBI/AZW/DJVU/FB2/LIT) — or caption 'book'\n"
        "• Links (https://…)\n"
        "• Plain text thoughts — I'll structure & categorize them\n\n"
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
                book_meta,
                detail_level=detail_level,
                source=f"telegram-book::{Path(local_path).name}",
                attachment=attachment_rel,
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
        )
        return

    # --- Personal text note (#3): structure + categorize the user's thought ---
    detail = context.user_data.get("detail_level", "detailed")
    await analyze_and_save(
        update, context, text[:12000], detail,
        source="telegram-text::manual note",
        source_type="text",
        source_kind="text",
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


async def _save_book_note(update, book_meta, detail_level="book", source="", attachment=None):
    """Create a note that carries the extracted book metadata."""
    note_dict = {
        "title": book_meta.get("title", "Untitled Book"),
        "category": "books",
        "content": book_meta.get("text", "")[:20000] or "_(No extractable text.)_",
        "tags": ["book"],
        "source": source,
        "source_type": "book",
        "attachment": attachment,
        "detail_level": detail_level,
        "book_title": book_meta.get("title", ""),
        "book_authors": book_meta.get("authors", []),
        "book_year": book_meta.get("year", ""),
    }

    note_path = write_note_to_vault(note_dict)
    if not note_path:
        await update.message.reply_text("❌ Could not write to Obsidian vault.")
        return

    authors = ", ".join(note_dict["book_authors"]) or "Unknown"
    year = note_dict["book_year"] or "Unknown"
    await update.message.reply_text(
        f"✅ Book saved to vault!\n📂 {note_path}\n\n"
        f"📚 {note_dict['book_title']}\n"
        f"✍️ {authors}\n📅 {year}\n"
        f"📎 Attachment: {note_dict['attachment'] or 'none'}"
    )


async def analyze_and_save(
    update, context, text, detail_level, source, source_type,
    attachment=None, source_kind=None,
):
    """Run AI analysis and persist the resulting knowledge note."""
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
    for cmd in ("summarize", "detailed", "precise", "raw", "book"):
        app.add_handler(CommandHandler(cmd, set_detail_command))
    app.add_handler(CallbackQueryHandler(model_choice_callback, pattern=r"^swm:\d+$"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started. Current LLM model: %s", get_current_model())
    app.run_polling()


if __name__ == "__main__":
    main()