"""Telegram → Gemini → Obsidian Knowledge Agent."""
import os
import logging
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from parsers.document_parser import parse_document, SUPPORTED_EXTENSIONS
from parsers.link_parser import parse_link
from llm.analyzer import analyze_content
from storage.vault_writer import write_note_to_vault, derive_detail_level

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "ObsidianVault")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")

Path(VAULT_PATH).mkdir(parents=True, exist_ok=True)
ATTACHMENTS_DIR = Path(VAULT_PATH, "90_Attachments")
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


# ---- Command handlers ----

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Obsidian Knowledge Agent ready.\n\n"
        "Send a document (PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML) or a link.\n\n"
        "Set detail level with /summarize, /detailed, /precise, or /raw."
    )


async def set_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    level = update.message.text.lstrip("/").strip().lower()
    if level in {"summarize", "detailed", "precise", "raw"}:
        context.user_data["detail_level"] = level
        await update.message.reply_text(f"✅ Detail level set to: {level}")
    else:
        await update.message.reply_text("❌ Unknown level. Use /summarize, /detailed, /precise, /raw.")


# ---- Message handlers ----

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption
    detail_level = derive_detail_level(caption) if caption else context.user_data.get("detail_level", "detailed")

    file_obj = await update.message.document.get_file()
    if not file_obj:
        await update.message.reply_text("❌ No valid document received.")
        return

    with tempfile.TemporaryDirectory() as tmp:
        local_path = await file_obj.download_to_drive(tmp)
        content_text = parse_document(local_path)
        attachment_rel = _save_attachment(local_path)

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
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    for cmd in ("summarize", "detailed", "precise", "raw"):
        app.add_handler(CommandHandler(cmd, set_detail_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
