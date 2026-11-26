# Handoff: `fix/security-hardening`

Hand-off from a Claude Code session on the work PC (2026-11-26) to continue on the MacBook.
To continue there, open Claude Code in this repo and say: *"read HANDOFF.md and continue"*.
Delete this file before merging.

## ⚠️ Start here: rebase onto the VM code
This branch is based on GitHub `main` @ `0d06c83`. **The bot running on the Hetzner VM is newer
and was never pushed:** its `/vault` reply shows "🛟 Emergency mirror", which isn't in this repo.

1. Push the VM/Mac version to `main`.
2. Then:
   ```bash
   git fetch origin
   git checkout fix/security-hardening
   git rebase main
   ```
3. Expect conflicts in `TelegramAgent/bot/bot.py`. Keep **both** the VM features and the changes below.
4. Re-run the tests (see "How to test").

## What this branch changes

### Security fixes
| Fix | Where |
|---|---|
| **Sender allowlist.** A group −1 `TypeHandler` silently drops updates from anyone not in `TELEGRAM_CHAT_ID` / `TELEGRAM_ALLOWED_USERS`. The bot **refuses to start** without an allowlist (`ALLOW_ANY_USER=true` turns this off). Before, anyone could write to the vault and use up the LLM quota. | `access_control.py`, `bot.py` `main()` |
| Runtime config defaults to `data/config.json` (inside the Docker volume, so it survives rebuilds). The old `config.json` is still read once. | `llm/provider.py` |
| User-facing error replies no longer include exception text (details go to the log only). | `bot.py` |
| Same-day notes with the same title get `-2`, `-3`… suffixes instead of overwriting (`open(..., "x")`). | `storage/vault_writer.py` |
| Caption → detail level matches whole words only ("drawing" ≠ raw, "Facebook" ≠ book). | `storage/vault_writer.py` |
| Old cooldown entries are cleaned out of the per-user cooldown dicts. | `bot.py` |

### LLM fallback rework (why `/book` failed on every model)
**Causes:**
- Chapter requests were about 9–10k tokens. Groq's free tier allows about 6–12k tokens per
  minute and answers with a 413 error labelled as a rate limit, so the old code treated it as
  one and retrying could never succeed.
- Gemini's free per-minute limit was hit with no waiting.
- A successful fallback permanently replaced the user's chosen model.
- One failed chapter killed the whole book.
- Chapters were silently cut off at 24,000 characters.

**New design:**
- `llm/errors.py`: `classify_error()` sorts each failure as AUTH / NOT_FOUND / TOO_LARGE /
  RATE_LIMIT / QUOTA / TRANSIENT / UNKNOWN. It also reads the provider's retry-after and its
  token limit (`Limit 6000, Requested 9512`).
- `llm/health.py`: `HEALTH` remembers, per model, how long to rest after a failure, which models
  are gone, which provider keys were rejected, and the largest request each model refused. It is
  kept in memory only, so a restart or `/models` → Reset clears it.
- `llm/free_pool.py`: the free pool comes from the live model lists plus a curated set:
  - all Groq chat models, Gemini *flash* models, Z.AI GLM flash
  - the 4 largest-context OpenRouter `:free` models
  - Ollama
  - Ranking: Groq > Z.AI > Gemini > OpenRouter > Ollama, then by capability hints.
  - `LLM_FREE_MODELS` pins your own order.
- `llm/provider.py` `chat()`:
  - Chain: chosen model → `LLM_FALLBACKS` → free pool. In `auto` mode the free pool comes first.
  - **Never overwrites the user's chosen model.**
  - Shrinks `max_tokens` to fit each model's learned limit.
  - Retries a temporary error once.
  - With `max_wait` it waits for the first resting model to recover.
  - Raises `AllProvidersFailedError(too_large=True)` when request size was a factor, so callers can split the input.
  - The model catalogue (with context sizes) refreshes lazily every 24 h, and the Gemini listing runs off the event loop.
- Ordinary notes wait up to 60 s for a rate limit to reset (`LLM_INTERACTIVE_MAX_WAIT`).
- `model_menu.py` (new `/models`):
  - Status screen: providers, current fallback order, resting / gone / too-small models.
  - Buttons: 🆓 Auto, 🧪 Test chain, ♻️ Reset health, one per model.
- `/book`:
  - `llm/book_summarizer.py`: about 12,000 characters per call (`BOOK_CHUNK_CHARS`); long chapters go in parts; a piece every model finds too large is halved (up to 3 times).
  - `storage/book_progress.py`: every finished chapter is saved to `data/book_progress/`.
  - After 3 failed chapters in a row the bot **pauses**; resending the same file resumes where it stopped.
  - Failed chapters get a second try at the end. Up to 10% may end as placeholders.
  - If the summary call fails, the note is still written without it.

### Other
- `requirements.txt`: `litellm==1.83.9` → `1.83.7`. 1.83.9 is no longer on PyPI, so a fresh Docker build failed.
- `scripts/triage_vault.sh`: host-side script for the "Transport endpoint is not connected" (Errno 107) vault failure.
  It has four modes: `diagnose`, `backup`, `fix`, `organize`.
- `.gitattributes`: `*.sh` files are always LF.

## Deploy checklist (VM `.env`)
- [ ] `TELEGRAM_CHAT_ID` is set (**the bot won't start without it**). Add more users with `TELEGRAM_ALLOWED_USERS` if needed.
- [ ] `LLM_FALLBACKS=` empty: the free pool is added automatically. The old list includes `openrouter/deepseek/deepseek-chat:free`, which is probably retired.
- [ ] Optional: `LLM_MODEL=auto`.
- [ ] `docker compose build` succeeds. **The litellm 1.83.7 pin hasn't been tested in Docker yet.**
- [ ] `docker compose up -d --force-recreate`, because `restart` doesn't re-read `.env`.
- [ ] Smoke test in Telegram:
  - `/models`, then 🧪 Test chain
  - a link
  - a small EPUB with `/book`
  - send from a second Telegram account: it should get no reply

## Open items / not done
- **Not checked against real providers.** All LLM tests mock litellm. The real error texts used
  in the tests follow the Groq, Gemini and OpenRouter formats, but check them in the logs after
  deploying: `docker compose logs telegram-agent | grep "LLM '"`.
- To confirm what broke `/book` before: `docker compose logs telegram-agent | grep "Book pipeline failed"`.
- `/organize` runs `git add -A` + `git commit` over the rclone FUSE mount. This probably caused
  the Errno 107 outage. Fix idea: skip git on FUSE, and put a time limit on the moves.
- Not done: splitting the `bot.py` monolith; moving the tests to pytest; about 15 bare
  `except Exception:` blocks.
- Already failing before this branch, unrelated to it: `tests/test_dashboard.py` (3 failures).
  `test_image.py` needs the Tesseract binary. `test_image_cmd.py` / `test_prod_fixes.py` need an
  explicit event loop on Python ≥ 3.14 (`asyncio.get_event_loop()`).

## How to test
```bash
cd TelegramAgent/bot
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tests/test_llm_fallback.py     # 65 checks: chain, health, free pool, /models, /book resume
.venv/bin/python tests/test_security_fixes.py   # 23 checks: allowlist, config path, overwrite, captions
for t in tests/test_*.py; do .venv/bin/python "$t" >/dev/null 2>&1 && echo "OK   $t" || echo "FAIL $t"; done
```
On the work PC (Python 3.15, litellm mocked):
- `test_llm_fallback`: 65/65.
- `test_security_fixes`: 23/23.
- `test_prod_fixes`: 43/43.
- Every other suite matches the results from before this branch.
