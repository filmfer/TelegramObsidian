# Telegram → Gemini → Obsidian Knowledge Agent

Self-hosted Telegram bot: send documents, links, e-books, voice notes and
video — get clean, AI-categorized **knowledge notes** in your Obsidian vault,
synced across all your devices.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

> This is the **operator manual** for the bot package. For the project
> showcase, see the [root README](../../README.md).

---

## Module map

```
bot.py                     orchestrator: handlers, queues, deadlines, dedup
llm/provider.py            litellm multi-provider fallback chain + /models + weekly health check
llm/analyzer.py            prompts (knowledge/research/video/book) + META_JSON parsing
parsers/document_parser.py PDF · DOCX · XLSX · TXT · MD · JSON · CSV · EML
parsers/link_parser.py     3-layer scraper (headers → cloudscraper → jina.ai), SSRF-safe, og:image
parsers/book_parser.py     e-book metadata + text (PDF/EPUB/MOBI/AZW/AZW3/AZW4/DJVU/FB2/LIT)
parsers/audio_parser.py    Groq Whisper transcription
parsers/video_parser.py    YouTube metadata/transcript; uploaded video → ffmpeg audio
parsers/search_parser.py   DuckDuckGo search for /research
storage/vault_writer.py    sanitized note writing + YAML frontmatter + CATEGORY_MAP
storage/dedup_store.py     SQLite fingerprints (dedup) + /text · /voice pending queue
storage/vault_organizer.py /organize merge engine
notifications.py           editable status messages, task deadlines, global error handler
config/category_taxonomy.yaml   protected folders, manual merges, threshold
data/agent.db              SQLite state (dedup + queues) — outside the vault on purpose
```

---

## Features

| Input | What happens |
|---|---|
| 📄 Documents: `PDF · DOCX · XLSX · TXT · MD · JSON · CSV · EML` | Full-text extraction → knowledge note |
| 🔗 Links (`https://…`) | 3-layer scraping chain, SSRF-safe, `og:image` thumbnail |
| 🎬 YouTube / video links | Caption transcript → summary note with video info + thumbnail |
| 🎥 Uploaded videos (≤20MB) | ffmpeg extracts audio → Whisper → summary note |
| 📖 E-books: `EPUB · MOBI · AZW/AZW3/AZW4 · DJVU · FB2 · LIT · PDF` | TOC/index stripped → map-reduce over sections → deep study note (background, live progress) |
| 📧 Email files (`.eml`) | Parsed → knowledge note |
| 💭 Plain text | Queued → `/text` merges into one structured note |
| 🗣️ Voice notes / audio | Queued → `/voice` transcribes (free Whisper) into one note; caption `research` = instant deep search |
| 🔎 `/research <topic>` | DuckDuckGo → scrape top sources → cited synthesis note |

### Detail levels (caption or command)

| Command | Output |
|---|---|
| `/summarize` | Quick-reference overview |
| `/detailed` ⭐ | Full study note: Overview · Key Concepts · Facts & Data · Insights · Open Questions |
| `/precise` | Exact data extraction — every number preserved |
| `/raw` | Verbatim text, metadata only |
| `/book` | Deep book mode (map-reduce, background) |

### Commands

| Command | Action |
|---|---|
| `/text` | Turn every queued text message into **one** note |
| `/voice` | Transcribe every queued audio into **one** note |
| `/queue` | See what's waiting (items expire after `PENDING_QUEUE_TTL_HOURS`, default 24h) |
| `/research <topic>` | Deep web research with cited sources |
| `/models` | List working LLM models, tap to switch instantly |
| `/organize preview` | Show which sparse category folders would be merged |
| `/organize` | Propose merges → confirm via inline keyboard → applies with a git commit |
| `/start` · `/help` | Full usage guide |

### Never lose work

- **Dedup** — same link/file/text twice is caught by SQLite fingerprints
  (URLs normalized, tracking params stripped, YouTube collapses to video id).
  Override per-send with `--force` in the caption/message.
- **Queues survive restarts** — `/text` and `/voice` items live in SQLite.
- **10-minute hard cap** — every heavy task has a deadline with a
  "still working" checkpoint; runaway jobs are cancelled cleanly.
- **Global error handler** — any unhandled exception is logged (rotating
  `logs/bot.log`) and you get a friendly message, never silence.

---

## Multi-provider LLM

Configured in `.env`; the bot probes providers at startup and **weekly**
(auto-switching if a model dies), and you can always switch live:

```ini
LLM_MODEL=gemini/gemini-flash-latest
LLM_FALLBACKS=groq/openai/gpt-oss-120b,groq/llama-3.1-8b-instant,gemini/gemini-pro-latest
```

| Provider | Key | Free tier |
|---|---|---|
| Google Gemini (default) | `GEMINI_API_KEY` | generous free tier |
| Groq | `GROQ_API_KEY` | free Llama + **free Whisper** (audio) |
| OpenRouter | `OPENROUTER_API_KEY` | models with `:free` suffix |
| Ollama (local) | `OLLAMA_HOST` | fully local, no key |

`/models` shows every currently-reachable model with a tap-to-switch menu;
when all providers fail mid-task the bot offers that menu automatically.

---

## Deploy on a headless VPS (Docker)

```bash
# 1. Clone
git clone https://github.com/filmfer/TelegramObsidian.git && cd TelegramObsidian/TelegramAgent/bot

# 2. Create .env with real secrets (never committed)
cp .env.example .env && nano .env

# 3. Mount the Google-Drive vault (headless rclone OAuth)
sudo bash setup_rclone.sh    # installs rclone+fuse, headless OAuth, systemd mount

# 4. Build & run
docker compose up -d --build

# 5. Logs / state
docker compose logs -f --tail 50
docker compose exec telegram-agent ls -la /app/data   # agent.db (dedup + queues)
```

> **Persistence:** the dedup/queue DB lives in the `agent-data` Docker volume
> (`/app/data` inside the container) — it survives rebuilds. The vault itself
> is bind-mounted from `OBSIDIAN_VAULT_HOST_PATH` (your rclone mount).

After a `git pull`, update with:

```bash
cd TelegramAgent/bot && docker compose up -d --build
```

---

## Multi-device sync (zero data loss)

| Device | Sync |
|---|---|
| 🖥️ MacBook / Windows PCs | Google Drive for Desktop |
| 🖥️ Headless VPS | rclone mount (`setup_rclone.sh`) |
| 📱 Android | DriveSync / FolderSync |
| 🔁 Versioning | Git repo inside the vault + Obsidian Git plugin |

---

## Security

- Secrets only via `.env`; `.dockerignore` keeps them out of the image
- SSRF protection on all outbound scraping **and** thumbnail downloads
  (private/reserved IP ranges blocked, DNS-resolved)
- Path-traversal sanitization on every vault write and category name
- `defusedxml` for untrusted XML (e-books, emails)
- Per-user cooldown + Telegram `AIORateLimiter`
- Non-root container user, no inbound ports (outbound polling only)

---

## Environment vars (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **required** |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `OPENROUTER_API_KEY` / `OLLAMA_HOST` | — | LLM providers (at least one) |
| `LLM_MODEL` | `gemini/gemini-flash-latest` | preferred model (litellm format) |
| `LLM_FALLBACKS` | see example | comma-separated fallback chain |
| `TELEGRAM_CHAT_ID` | — | chat for proactive alerts (weekly model check) |
| `OBSIDIAN_VAULT_PATH` | `/data/vault` | vault path **inside** the container |
| `OBSIDIAN_VAULT_HOST_PATH` | `/srv/obsidian-vault` | host path bind-mounted into the container |
| `DEDUP_DB_PATH` | `data/agent.db` | SQLite dedup + queue store |
| `PENDING_QUEUE_TTL_HOURS` | `24` | expiry for `/text`/`/voice` queue items |
| `TASK_TIMEOUT_SECONDS` | `600` | hard cap per task |
| `TASK_WARN_SECONDS` | `100` | "still working" checkpoint |
| `STAGING_DIR` | `data/staging` | queued audio staging area |
| `CATEGORY_TAXONOMY_PATH` | `config/category_taxonomy.yaml` | `/organize` rules |
| `LOG_DIR` | `logs` | rotating log files |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `litellm.NotFoundError` / dead model errors | Run `/models` — the bot lists every reachable model; tap to switch. The weekly check also auto-heals. |
| Notes land in `uncategorized/` | The model's `META_JSON` was missing → the parser derives metadata; if it persists, switch models via `/models`. |
| Bot says "already saved" for new content | It's the dedup store. Add `--force` to your message, or clear the DB: `docker compose exec telegram-agent rm /app/data/agent.db` then restart. |
| `/organize` finds no candidates | All folders are at/above `threshold` or protected — tune `config/category_taxonomy.yaml`. |
| Voice note gets no answer | Check `GROQ_API_KEY` (Whisper); transcription errors are always reported. |
| rclone mount down | `systemctl status rclone-gdrive --no-pager` · `sudo rclone lsd gdrive:` |

---

## License

MIT — see [LICENSE](../../LICENSE).
