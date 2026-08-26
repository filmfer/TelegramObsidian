# 📚 Telegram → Gemini → Obsidian Knowledge Agent

<img width="1100" height="614" alt="image" src="https://github.com/user-attachments/assets/245adcc2-d7bd-43f2-a638-9cd945880deb" />

> **Turn Telegram into your personal knowledge capture pipeline.** Send documents, links, and e-books to a bot — get clean, AI-categorized Markdown notes in your Obsidian vault, synced across all your devices.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0.svg?logo=telegram&logoColor=white)](https://telegram.org/)
[![Gemini](https://img.shields.io/badge/Gemini-AI-4285F4.svg?logo=google&logoColor=white)](https://ai.google.dev/)
[![Obsidian](https://img.shields.io/badge/Obsidian-Vault-7C3AED.svg?logo=obsidian&logoColor=white)](https://obsidian.md/)

---

## 🧭 What is this?

This is a **self-hosted Telegram bot** that acts as a bridge between your chat and your **Obsidian knowledge base**. You send it a file or a link, and it:

1. **Extracts** the content (PDF, DOCX, XLSX, TXT, JSON, MD, CSV, EML, or a webpage)
2. **Analyzes** it with **Google Gemini** — classifies it into your categories, generates a title, tags, and a structured summary
3. **Writes** a clean Markdown note with YAML frontmatter into your Obsidian vault
4. **Syncs** it to all your devices via Google Drive + Git (no conflicts, no data loss)

It's like having a **personal research assistant** that files everything for you.

---

## ✨ Features

### 📄 Document Ingestion
| Format | Extension |
|---|---|
| PDF | `.pdf` |
| Word | `.docx` |
| Excel | `.xlsx` |
| Plain text | `.txt`, `.md`, `.json`, `.csv` |
| Email | `.eml` |

### 🔗 Link Scraping
Paste any `https://` URL — the bot fetches the page, extracts the main content, and summarizes it. **SSRF-protected** (blocks private/reserved IP ranges).

### 📖 E-Book Support
| Format | Extension |
|---|---|
| PDF | `.pdf` |
| EPUB | `.epub` |
| Kindle | `.mobi`, `.azw`, `.azw3`, `.azw4` |
| Other | `.djvu`, `.fb2`, `.lit` |

The `book` detail level extracts **title, author(s), publish year** and attaches the original file to the note.

### 🎚️ Detail Levels
Control how deep the AI goes — via caption keyword or `/command`:

| Level | Output |
|---|---|
| `/summarize` | 3–8 bullet points |
| `/detailed` | Full structured Markdown with subheadings |
| `/precise` | Exact data, numbers, quotes, specs |
| `/raw` | Original text verbatim |
| `/book` | Book metadata + full text + attachment |

### 🗂️ Multi-Label Categorization
Gemini classifies each note into one or more of your folders: `Travel`, `Car`, `Finance`, `Programming`, `AI`, `Religion`, `Politics`, `IoT`, `Database`, `Food`, `Books`… New categories are easy to add.

### 🔄 Multi-Device Sync (Zero Data Loss)
| Device | Sync Method |
|---|---|
| 🖥️ MacBook | Google Drive for Desktop |
| 🖥️ Windows PC (personal) | Google Drive for Desktop |
| 🖥️ Windows PC (work) | Google Drive for Desktop |
| 📱 Android / iOS | DriveSync / FolderSync |
| 🔁 All devices | **Obsidian Git plugin** (auto-commit + push) |

Git inside the vault prevents silent overwrite conflicts — two devices editing the same file produce a Git-detectable conflict instead of Google Drive's "file (1)" duplication.

### 🛡️ Security-First Design
- 🔐 Secrets only via `.env` — never hardcoded, never in the Docker image
- 🚫 **SSRF protection** — blocks private/reserved IP ranges in the link parser
- 🧱 **Path traversal protection** — vault writer sanitizes folder/file names
- 🧬 **XXE-safe XML** — `defusedxml` for FB2 parsing
- ⏱️ **Rate limiting** — per-user cooldown + Telegram `AIORateLimiter`
- 🚪 **No exposed ports** — outbound HTTPS polling only
- 👤 **Non-root Docker** — runs as `appuser`

---

## 🏗️ Architecture

```
Telegram ──▶ python-telegram-bot (polling)
                │
                ├── parsers/document_parser.py   (PDF/DOCX/XLSX/TXT/JSON/MD/CSV/EML)
                ├── parsers/link_parser.py       (web scraping, SSRF-safe)
                ├── parsers/book_parser.py       (e-book metadata + text)
                │
                ▼
            llm/analyzer.py  ──▶ Google Gemini (classify + summarize)
                │
                ▼
            storage/vault_writer.py  ──▶ ObsidianVault/ (on Google Drive)
```

---

## 🚀 Quick Start

### Local (5 minutes)

```bash
cd TelegramAgent/bot
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
cp .env.example .env             # fill in TELEGRAM_BOT_TOKEN + GEMINI_API_KEY
export $(grep -v '^#' .env | xargs)
python bot.py
```

### Docker (Oracle Free VPS)

```bash
git clone <your-repo-url> telegram-agent && cd telegram-agent/TelegramAgent/bot
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f telegram-agent
```

### Test (no API key needed)

```bash
cd TelegramAgent/bot
OBSIDIAN_VAULT_PATH=../../ObsidianVault .venv/bin/python -m tests.test_pipeline
```

---

## 📖 Usage

| Command | Action |
|---|---|
| `/start` | Help message |
| `/summarize` `/detailed` `/precise` `/raw` `/book` | Set default detail level |
| *(send doc + caption)* | Process at that detail level |
| *(send e-book)* | Auto-detected → book note with metadata + attachment |
| *(paste URL)* | Scrape + summarize the page |

### Example book note

```yaml
---
title: "The Pragmatic Programmer"
source_type: book
book_title: "The Pragmatic Programmer"
book_authors: [Andrew Hunt, David Thomas]
book_year: "1999"
attachment: 90_Attachments/the-pragmatic-programmer.epub
detail_level: book
---
```

---

## 📁 Vault Structure

```
ObsidianVault/
├── .obsidian/              # Obsidian config (Git plugin, Dataview, QuickAdd)
├── 00_Inbox/               # Uncategorized notes
├── 10_Categories/          # One folder per AI category
│   ├── Travel/
│   ├── Car/
│   ├── Finance/
│   ├── Programming/
│   ├── AI/
│   ├── Religion/
│   ├── Books/
│   └── ...
├── 90_Attachments/         # Original documents (PDFs, e-books, etc.)
└── 99_Templates/           # Note templates
```

---

## 🧰 Tech Stack

| Component | Technology |
|---|---|
| Bot framework | `python-telegram-bot` v21 (async) |
| AI | Google Gemini (`google-genai`) |
| PDF | `pypdf` |
| DOCX | `python-docx` |
| XLSX | `openpyxl` |
| Web scraping | `trafilatura` + `httpx` |
| E-books | `ebooklib` |
| XML safety | `defusedxml` |
| Deployment | Docker + docker-compose |

---

## 👤 Author

**Filipe Fernandes**
📧 filmfer@gmail.com

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🙏 Acknowledgements

- [Obsidian](https://obsidian.md/) — the knowledge base that makes this worthwhile
- [Google Gemini](https://ai.google.dev/) — the AI brain
- [python-telegram-bot](https://python-telegram-bot.org/) — the bot framework
- [trafilatura](https://trafilatura.readthedocs.io/) — web content extraction
