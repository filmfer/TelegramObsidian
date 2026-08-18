# Telegram → Gemini → Obsidian Knowledge Agent

A Telegram bot that receives **documents**, **links**, and **emails**, analyzes them with **Gemini**, and writes categorized Markdown notes into your **Obsidian vault** (stored in Google Drive).

---

## Features

- **Input via Telegram**
  - Documents: `PDF, DOCX, XLSX, TXT, JSON, MD, CSV, EML`
  - Links: paste `https://...`
  - Emails: send a `.eml` file (or forward; dedicated IMAP inbox later)
- **Detail levels** (caption keyword or command):
  `/summarize` · `/detailed` · `/precise` · `/raw`  (default `detailed`)
- **Multi-label AI categorization** into your folders: Travel, Car, Finance, Programming, AI, Religion, Politics, IoT, Database, Food…
- **Multi-device sync**: Google Drive + Git inside the vault (no conflicts, no data loss)

---

## Local setup (Mac / Windows)

```bash
cd TelegramAgent/bot
python3 -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows
pip install -r requirements.txt
cp .env.example .env       # then fill in TELEGRAM_BOT_TOKEN, GEMINI_API_KEY
```

---

## Run locally

```bash
export $(grep -v '^#' .env | xargs)
python bot.py
```

---

## Deploy on Oracle Free VPS (# docker)

```bash
# 1. On your Mac/Windows, mount Google Drive locally (Drive for Desktop)
# 2. Copy the vault folder into the VPS (rsync/scp) OR mount Drive as a volume
# 3. Build & run
docker compose up -d --build
```

`docker-compose.yml` mounts the vault volume and reads `.env`.
**Tip**: bind-mount your Google-Drive vault directly:

```yaml
volumes:
  - /path/to/google-drive/ObsidianVault:/data/vault
```

---

## Vault sync (multi-device)

| Device | How |
|---|---|
| MacBook Air M2 | Google Drive for Desktop |
| Windows PC (personal) | Google Drive for Desktop |
| Windows PC (work) | Google Drive for Desktop |
| Android / iOS | **DriveSync** or **FolderSync** (two-way sync of the vault folder) |
| All devices | **Obsidian Git plugin** — install, set auto-push every 5 min |

Git inside the vault prevents silent overwrite conflicts.

---

## Commands

| Command | Action |
|---|---|
| `/start` | Help message |
| `/summarize` `/detailed` `/precise` `/raw` | Set default detail level |
| *(send doc + caption)* | Detail level from caption, e.g. "summarize" |
| *(paste URL)* | Scrapes + summarizes the page |

---

## Security

- `.env` never committed (add `.env` to `.gitignore`).
- Rate-limited Telegram polling.
- All DB calls use parameterized queries (none here yet; reserved).
- No hardcoded keys — `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY` only from env.

---

## Environment vars (`.env`)

```ini
TELEGRAM_BOT_TOKEN=xxxx:token
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-2.5-pro
OBSIDIAN_VAULT_PATH=/data/vault
```
