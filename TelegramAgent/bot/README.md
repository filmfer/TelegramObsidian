# Telegram → Gemini → Obsidian Knowledge Agent

A Telegram bot that receives **documents**, **links**, and **e-books**, analyzes them with **Google Gemini**, and writes categorized Markdown notes into your **Obsidian vault** (stored in Google Drive).

---

## Features

- **Input via Telegram**
  - Documents: `PDF, DOCX, XLSX, TXT, JSON, MD, CSV, EML`
  - Links: paste `https://...`
  - E-mails: send a `.eml` file
  - **E-books**: `PDF, EPUB, MOBI, AZW, AZW3, AZW4, DJVU, FB2, LIT`
    - `book` detail level extracts **title / author(s) / publish year**
      and attaches the original file to the note
- **Detail levels** (caption keyword or command):
  `/summarize` · `/detailed` · `/precise` · `/raw` · `/book`
  (default `detailed`)
- **Multi-label AI categorization** into folders: Travel, Car, Finance, Programming, AI, Religion, Politics, IoT, Database, Food, **Books**…
- **Multi-device sync**: Google Drive + Git inside the vault (no conflicts, no data loss)

---

## Architecture

```
Telegram → python-telegram-bot (polling) → parsers (docs/links/books)
       → Gemini (google-genai) → vault_writer → ObsidianVault/ (on Google Drive)
```

- No inbound ports exposed — the bot uses **outbound HTTPS polling** (minimal attack surface).
- Secrets are injected via `.env` only; `.dockerignore` prevents them from entering the image.
- SSRF protection blocks private/reserved IP ranges in the link parser.
- XML parsing uses `defusedxml` (XXE/billion-laughs safe).
- Path traversal is prevented in the vault writer (folder/file sanitization).
- Per-user request cooldown + Telegram `AIORateLimiter` prevent flooding.

---

## Setup (Local dev — 5 min)

### 1. Prerequisites

- **Python 3.9+** (local dev uses your system Python)
- **Google Gemini API key** — get one at https://aistudio.google.com/apikey
- **Telegram Bot Token** — create a bot via [@BotFather](https://t.me/BotFather) → `/newbot`

### 2. Install

```bash
cd TelegramAgent/bot
python3 -m venv .venv

# macOS / Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env
```

### 3. Configure `.env`

```ini
# --- Telegram ---
TELEGRAM_BOT_TOKEN=123456:ABC-your-token

# --- Gemini / LLM ---
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-pro

# --- Obsidian Vault ---
# Local dev: point at your synced vault folder
OBSIDIAN_VAULT_PATH=/path/to/GoogleDrive/ObsidianVault
```

> ⚠️ **Never commit `.env`.** It is already in `.gitignore` and `.dockerignore`.

### 4. Run locally

```bash
export $(grep -v '^#' .env | xargs)   # load env vars (macOS/Linux)
# Windows PowerShell: Get-Content .env | ForEach-Object { if ($_ -match '^(\w+)=(.*)$') { Set-Item Env:$matches[1] $matches[2] } }
python bot.py
```

### 5. Test the whole pipeline (no API key needed)

```bash
cd TelegramAgent/bot
OBSIDIAN_VAULT_PATH=../../ObsidianVault .venv/bin/python -m tests.test_pipeline
```

Expected output:

```
✅ BOOK_EXTENSIONS covers all requested formats
✅ is_book_file works
✅ extract_book_metadata -> Test Book Title by ['Jane Doe', 'John Smith']
✅ Book note written with frontmatter -> Books/2026-08-18-test-book-title.md
✅ parse_document extracted text
✅ write_note_to_vault -> Programming/2026-08-18-sample-note-title.md
✅ End-to-end pipeline test PASSED
```

---

## Deploy on Oracle Free VPS (Docker)

```bash
# 1. On the VPS, clone the repo:
git clone <your-repo-url> telegram-agent && cd telegram-agent/TelegramAgent/bot

# 2. Create .env with your real secrets (never commit):
cp .env.example .env
nano .env

# 3. Mount your Google Drive vault.
#    Option A — Drive folder is on the VPS (rsync from your Mac):
#       rsync -avz ~/path/to/ObsidianVault/ user@vps:/srv/obsidian-vault/
#    Option B — Mount Google Drive via rclone (cloud mount, headless-friendly):
#       sudo bash setup_rclone.sh
#       (installs rclone, guides headless OAuth, creates systemd service)

# 4. Build & run:
docker compose up -d --build

# 5. Check logs:
docker compose logs -f telegram-agent
```

### Automated Google Drive mount (`setup_rclone.sh`)

For a **headless Ubuntu server** (no browser/window manager), run the included script to mount your Google Drive vault automatically:

```bash
sudo bash setup_rclone.sh
```

It will:

1. Install `rclone` + `fuse3`
2. Create the `gdrive` remote pointing at your vault folder ID
3. Guide you through **headless OAuth** — it prints a URL; open it in your local browser, authorize, and paste the verification code back
4. Create the mount point `/srv/obsidian-vault`
5. Create + enable a **systemd service** (`rclone-gdrive`) so the mount survives reboots

After it finishes, set in `.env`:

```ini
OBSIDIAN_VAULT_HOST_PATH=/srv/obsidian-vault
```

Then rebuild: `docker compose up -d --build`

`docker-compose.yml`:

- No host ports exposed (polling only — safest)
- `env_file: ./.env` injects secrets at runtime (never baked into the image)
- **Bind mount** `${OBSIDIAN_VAULT_HOST_PATH:-/srv/obsidian-vault}:/data/vault` — writes directly to the Google Drive folder on the host
- Runs as non-root user

The bind mount path is read from `OBSIDIAN_VAULT_HOST_PATH` in `.env` (defaults to `/srv/obsidian-vault`).

---

## Multi-device sync (zero data loss)

| Device | How |
|---|---|
| MacBook Air M2 | Google Drive for Desktop |
| Windows PC (personal) | Google Drive for Desktop |
| Windows PC (work) | Google Drive for Desktop |
| Android / iOS | **DriveSync** or **FolderSync** (two-way sync of the vault folder) |
| All devices | **Obsidian Git plugin** — install, set auto-push every 5 min |

Git inside the vault prevents silent overwrite conflicts: two devices editing the same file produce a Git-detectable conflict instead of Google Drive's "file (1)" duplication.

> **First-time Git setup in the vault:**
> ```
> cd ObsidianVault
> git init
> git add -A && git commit -m "init vault"
> git remote add origin <private-github-repo-url>
> git push -u origin main
> ```
> Then install the **Obsidian Git** community plugin on each device and enable auto commit/push.

---

## Telegram usage

| Command | Action |
|---|---|
| `/start` | Help message |
| `/summarize` `/detailed` `/precise` `/raw` `/book` | Set default detail level |
| *(send doc + caption)* "summarize" | Process at that detail level |
| *(send e-book, any supported ext)* | Auto-detected → book note with title/author/year + attachment |
| *(paste URL)* | Scrapes + summarizes the page (SSRF-protected) |

### Book notes (`/book` or caption `book`)

When a document matches a book extension (or detail level is `book`), the bot:

1. Extracts **title, author(s), publish year** from the file metadata
2. Writes a note to `Books/` with YAML frontmatter:

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

3. Copies the original e-book into `90_Attachments/` so you can open it from Obsidian.

> **Note**: EPUB / FB2 / PDF metadata is parsed from the file. For MOBI / AZW / DJVU / LIT full text decoding is best-effort — the file is still attached to the note.

---

## Security

| Concern | Mitigation |
|---|---|
| Secrets in repo / image | `.env` in `.gitignore` + `.dockerignore`; secrets only via `env_file` at runtime |
| SSRF (link parser) | Blocks private/reserved IP ranges (RFC 1918, link-local, cloud metadata `169.254.169.254`, IPv6) |
| XXE / billion-laughs (FB2) | `defusedxml` for all XML parsing |
| Path traversal | Vault writer sanitizes folder names + filename slugs (strips `..`, `/`, `\`) |
| Brute-force / flooding | Per-user cooldown (10 s) + Telegram `AIORateLimiter` |
| Exposed ports | None — outbound HTTPS polling only |
| Non-root container | Docker runs as `appuser` |
| API-key validation | Clear error message before any network call if `GEMINI_API_KEY` missing |

---

## Environment vars (`.env`)

```ini
TELEGRAM_BOT_TOKEN=xxxx:token
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-2.5-pro
OBSIDIAN_VAULT_PATH=/data/vault
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `RuntimeError: Set TELEGRAM_BOT_TOKEN in .env` | `.env` missing or empty — `cp .env.example .env` and fill it |
| `GEMINI_API_KEY is not set...` | Add your Gemini key to `.env` |
| `ModuleNotFoundError: No module named 'ebooklib'` | `pip install -r requirements.txt` |
| Bot not writing to vault | Verify `OBSIDIAN_VAULT_PATH` points at a writable folder |
| E-book metadata empty (MOBI/AZW/etc.) | Best-effort — the file still gets attached to the note |
| Docker build copies `.env` | Use the provided `.dockerignore` |