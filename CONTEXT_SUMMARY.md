# CONTEXT SUMMARY — Telegram → Gemini → Obsidian Agent

## Status: IN PROGRESS (book feature partially implemented)

## 1. Primary Request & Intent
Build a Telegram bot agent that:
- Receives via Telegram: documents (PDF, DOCX, XLSX, TXT, JSON, MD, CSV), links, emails (.eml).
- **NEW** (last user request): a `book` category/detail level that extracts book metadata (title, author/authors, publish year) from e-books, attaches the file to the Obsidian note, and supports e-book extensions (PDF, EPUB, MOBI, AZW, AZW3, AZW4, DJVU, FB2, LIT).
- Analyzes content with Gemini (Pro Gemini API) → multi-label categorization (travel, car, finance, programming, AI, religion, bible, politics, IoT, database, data analysis, web scraping, exercise, diet, food, cooking, books…).
- Supports detail levels via caption: `summarize`, `detailed`, `precise`, `raw` (+ new `book`).
- Writes structured Markdown notes (YAML frontmatter) into an Obsidian vault stored in Google Drive.
- Multi-device sync strategy: Google Drive mount (Mac/Windows) + DriveSync (Android) + Git repo inside the vault + Obsidian Git plugin (no conflicts/data loss).

## 2. Key Technical Concepts
- Python 3.9 locally (uses `from __future__ import annotations` for `str | None`); Docker uses python:3.12-slim.
- `python-telegram-bot` v21 (async), Google GenAI SDK (`google-genai`), pypdf, python-docx, openpyxl, trafilatura, httpx, beautifulsoup4, PyYAML, ebooklib, lxml.
- Security: secrets via `.env` (never hardcoded), non-root Docker user, input validation, no string-concatenated SQL.
- Vault structure: `00_Inbox/`, `10_Categories/<Category>/`, `90_Attachments/`, `99_Templates/`.
- Git repo initialized in `ObsidianVault/` with 4 commits; `.obsidian/app.json`, `core.json`, `community-plugins.json` (obsidian-git, dataview, quickadd), note template.

## 3. Files (current state)
Working dir: `/Users/filmfer/Prog/TelegramObsidian`
- `ObsidianVault/.obsidian/app.json` — app preferences (show frontmatter, line numbers).
- `ObsidianVault/.obsidian/core.json` — enabled core plugins (templates, tag panel, backlinks, search, file recovery, outliner).
- `ObsidianVault/.obsidian/community-plugins.json` — recommends obsidian-git, dataview, quickadd.
- `ObsidianVault/99_Templates/Note Template.md` — template with YAML frontmatter placeholders.
- `ObsidianVault/.gitignore`, `README.md` — created; git-committed.
- `TelegramAgent/bot/bot.py` (152 lines) — Telegram bot orchestrator (start/summarize/detailed/precise/raw commands, handle_document, handle_text, _save_attachment, analyze_and_save, main). NOT YET updated for book routing.
- `TelegramAgent/bot/parsers/document_parser.py` — PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML extraction; `from __future__ import annotations` added.
- `TelegramAgent/bot/parsers/link_parser.py` — async web scraping via trafilatura (httpx); future annotations added.
- `TelegramAgent/bot/parsers/book_parser.py` (NEW) — `BOOK_EXTENSIONS`, `extract_book_metadata(file_path)` returning {title, authors, year, text}; PDF via pypdf, EPUB/FB2 via ebooklib, fallbacks for MOBI/AZW/DJVU.
- `TelegramAgent/bot/llm/analyzer.py` — `analyze_content(content, detail, source_url)` calling Gemini; returns {title, category, content, tags, detail_level}. Fixed `//` comment → `#`.
- `TelegramAgent/bot/storage/vault_writer.py` — `write_note_to_vault(note)`, `derive_detail_level(caption)`, `CATEGORY_MAP`; **rewritten WITH book support** (Books category, derive_detail_level detects "book", frontmatter includes book_title/book_authors/book_year). (Last confirmed on disk via write_to_file success, but earlier edits had reverted — needs verification.)
- `TelegramAgent/bot/requirements.txt` — added ebooklib==0.18.3 + lxml==5.3.0 (edit may not have persisted — needs verification).
- `TelegramAgent/bot/.env.example`, `.gitignore`, `Dockerfile`, `docker-compose.yml`, `README.md` (deployment docs).
- `TelegramAgent/bot/tests/test_pipeline.py` — end-to-end test (parse → analyze [mocked] → write_note). PASSED.

## 4. Problems Solved
- Python 3.9 `str | None` runtime error → added `from __future__ import annotations`.
- Invalid `//` JS-style comment in analyzer → replaced with `#`.
- `replace_in_file` repeatedly failed for large blocks / reverted files → switched to `write_to_file` for full-file rewrites.
- Vault sync conflict risk → Git repo inside vault as source of truth.
- Local venv at `TelegramAgent/bot/.venv` (Python 3.9) for isolated testing.

## 5. Pending / To Be Verified & Completed
- [ ] VERIFY vault_writer.py currently has book fields (last edit reverted earlier).
- [ ] VERIFY requirements.txt has ebooklib + lxml lines.
- [ ] Install ebooklib/lxml in venv, run py_compile + import book_parser.
- [ ] REWRITE bot.py to: import book_parser; add `/book` command; detect book files by extension OR detail_level=="book"; call extract_book_metadata; merge book metadata into note_dict; pass book routing. (Currently bot.py is the 152-line non-book version.)
- [ ] Add book test to tests/test_pipeline.py (e.g., mock extract_book_metadata + write a Books note).
- [ ] Re-run end-to-end test.
- [ ] Update README.md with /book command + e-book extension list + Books category.
- [ ] Final commit.

## 6. Current Work (left off)
Implementing the `book` upload/detail level: created `parsers/book_parser.py`, rewrote `vault_writer.py` to include book category + book_title/book_authors/book_year frontmatter fields + derive_detail_level "book" detection. About to wire book routing into bot.py (imports, `/book` command, handle_document book detection, analyze_and_save book handling).

## 7. Next Steps (in priority)
1. Verify vault_writer.py & requirements.txt on-disk state via grep.
2. Rewrite bot.py to integrate the book flow (cleanest path to avoid replace_in_file failures).
3. Install ebooklib/lxml in venv, compile + test.
4. Update README + final e2e test + commit.

## 8. How to run tests
```
cd TelegramAgent/bot
.venv/bin/python -m py_compile bot.py parsers/*.py llm/analyzer.py storage/vault_writer.py tests/test_pipeline.py
OBSIDIAN_VAULT_PATH=../../ObsidianVault .venv/bin/python -m tests.test_pipeline
```
## 9. How to run the bot
```
cp .env.example .env   # fill TELEGRAM_BOT_TOKEN, GEMINI_API_KEY
.venv/bin/python bot.py   # (OBSIDIAN_VAULT_PATH + keys in env)
# or: docker compose up -d --build
```
