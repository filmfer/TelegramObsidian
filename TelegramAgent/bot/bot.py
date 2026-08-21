"""Telegram → Gemini → Obsidian Knowledge Agent."""
import os
import logging
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from parsers.document_parser import parse_document, SUPPORTED_EXTENSIONS
from parsers.link_parser import parse_link
from parsers.book_parser import BOOK_EXTENSIONS, is_book_file, extract_book_metadata
from llm.analyzer import analyze_content
from storage.vault_writer import write_note_to_vault, derive_detail_level

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

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")

Path(VAULT_PATH).mkdir(parents=True, exist_ok=True)
ATTACHMENTS_DIR = Path(VAULT_PATH, "90_Attachments")
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

# Detail levels selectable via /command or caption
DETAIL_LEVELS = {"summarize", "detailed", "precise", "raw", "book"}

# Per-user cooldown (seconds) between document/link processing to prevent abuse
USER_COOLDOWN_SECONDS = 10
_user_last_request: dict = {}


# ---- Command handlers ----

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Obsidian Knowledge Agent ready.\n\n"
        "Send a document (PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML), a link, or an e-book "
        "(PDF/EPUB/MOBI/AZW/AZW3/AZW4/DJVU/FB2/LIT).\n\n"
        "Set detail level with /summarize, /detailed, /precise, /raw, or /book."
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


# ---- Message handlers ----

def _check_rate_limit(user_id: int) -> bool:
    """Return True if the user is allowed to proceed (cooldown elapsed)."""
    import time

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
        derive_detail_level(caption) if caption else context.user_data.get("detail_level", "detailed")
    )

    file_obj = await update.message.document.get_file()
    if not file_obj:
        await update.message.reply_text("❌ No valid document received.")
        return

    with tempfile.TemporaryDirectory() as tmp:
        file_name = update.message.document.file_name
        destination_path = Path(tmp) / file_name
        local_path = await file_obj.download_to_drive(custom_path=destination_path)
        #local_path = await file_obj.download_to_drive(tmp)
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
                update, book_meta, detail_level=detail_level,
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
        content_text,
        detail_level,
        source=f"telegram-doc::{Path(local_path).name}",
        source_type="document",
        attachment=attachment_rel,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else 0
    if not _check_rate_limit(user_id):
        await update.message.reply_text(
            f"⏳ Please wait {USER_COOLDOWN_SECONDS}s between requests."
        )
        return

    text = update.message.text.strip()
    if text.startswith("http://") or text.startswith("https://"):
        content = await parse_link(text)
        if not content:
            await update.message.reply_text("❌ Could not read the link.")
            return
        await analyze_and_save(
            update, content, "summarize", source=text, source_type="link", attachment=None
        )
    else:
        await update.message.reply_text("Send a document or a link to process.")


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

    authors = ", ".join(note_dict["book_authors"]) if note_dict["book_authors"] else "Unknown"
    year = note_dict["book_year"] or "Unknown"
    await update.message.reply_text(
        f"✅ Book saved to vault!\n📂 {note_path}\n\n"
        f"📚 {note_dict['book_title']}\n"
        f"✍️ {authors}\n📅 {year}\n"
        f"📎 Attachment: {note_dict['attachment'] or 'none'}"
    )


async def analyze_and_save(update, text, detail_level, source, source_type, attachment=None):
    source_url = source if source_type == "link" else ""
    note_dict = analyze_content(text, detail_level, source_url=source_url)
    if not note_dict:
        await update.message.reply_text("❌ AI analysis failed. Check logs.")
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


def main():
    """Start the Telegram bot."""
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    for cmd in ("summarize", "detailed", "precise", "raw", "book"):
        app.add_handler(CommandHandler(cmd, set_detail_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()