#!/usr/bin/env python3
"""Validation for the security-hardening fixes.

1. Sender allowlist (parse, fail-closed startup, guard stops unknown senders)
2. Runtime config persisted under data/ (+ legacy fallback)
3. Same-day same-title notes never overwrite each other
4. Caption → detail level uses whole words only
5. Cooldown dicts are pruned
6. User-facing errors don't echo exception text

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_security_fixes.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ["DEDUP_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["OBSIDIAN_VAULT_PATH"] = tempfile.mkdtemp(prefix="agent_test_vault_")

from telegram import Chat, Update, User  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{PASS if cond else FAIL} {name}")


def _update(user_id: int, chat_id: int) -> Update:
    """Minimal real Update whose effective_user/effective_chat resolve via a message."""
    from datetime import datetime, timezone
    from telegram import Message

    msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, first_name="t", is_bot=False),
        text="hi",
    )
    return Update(update_id=1, message=msg)


def test_allowlist():
    import access_control as ac

    ids = ac.parse_allowed_ids(" 11, 22 ,bad,, -100123", "33")
    check("allowlist parses ids, keeps negative chat ids, skips junk",
          ids == frozenset({11, 22, -100123, 33}))
    check("empty config -> empty allowlist", ac.parse_allowed_ids(None, "") == frozenset())

    os.environ.pop("ALLOW_ANY_USER", None)
    try:
        ac.require_allowlist(frozenset())
        check("empty allowlist refuses to start", False)
    except RuntimeError:
        check("empty allowlist refuses to start", True)
    os.environ["ALLOW_ANY_USER"] = "true"
    try:
        ac.require_allowlist(frozenset())
        check("ALLOW_ANY_USER=true permits empty allowlist", True)
    except RuntimeError:
        check("ALLOW_ANY_USER=true permits empty allowlist", False)
    finally:
        os.environ.pop("ALLOW_ANY_USER", None)

    allowed = frozenset({42})
    check("owner user authorized", ac.is_authorized(_update(42, 42), allowed))
    check("stranger rejected", not ac.is_authorized(_update(7, 7), allowed))
    check("non-Update object rejected", not ac.is_authorized(object(), allowed))

    guard = ac.make_guard(allowed)
    asyncio.run(guard(_update(42, 42), None))  # must not raise
    check("guard lets owner through", True)
    try:
        asyncio.run(guard(_update(7, 7), None))
        check("guard stops stranger with ApplicationHandlerStop", False)
    except ApplicationHandlerStop:
        check("guard stops stranger with ApplicationHandlerStop", True)


def test_config_path():
    import llm.provider as prov

    check("config defaults to <bot>/data/config.json (Docker volume)",
          prov.CONFIG_PATH.parts[-2:] == ("data", "config.json")
          or "AGENT_CONFIG_PATH" in os.environ)

    with tempfile.TemporaryDirectory() as td:
        new, legacy = Path(td, "data", "config.json"), Path(td, "config.json")
        legacy.write_text(json.dumps({"llm": {"current_model": "groq/legacy"}}), encoding="utf-8")
        old = (prov.CONFIG_PATH, prov._LEGACY_CONFIG_PATH)
        prov.CONFIG_PATH, prov._LEGACY_CONFIG_PATH = new, legacy
        try:
            check("legacy config.json is read when new one is absent",
                  prov.get_current_model() == "groq/legacy")
            prov.set_current_model("gemini/new")
            check("save creates data/ and writes the new path", new.exists())
            check("new path wins after migration", prov.get_current_model() == "gemini/new")
        finally:
            prov.CONFIG_PATH, prov._LEGACY_CONFIG_PATH = old


def test_no_overwrite():
    from storage.vault_writer import write_note_to_vault

    note = {"title": "Same Title", "category": "ai", "content": "first"}
    p1 = write_note_to_vault(dict(note))
    p2 = write_note_to_vault(dict(note, content="second"))
    vault = Path(os.environ["OBSIDIAN_VAULT_PATH"])
    check("second same-day note gets a different path", p1 and p2 and p1 != p2)
    check("second path has -2 suffix", bool(p2) and p2.endswith("-2.md"))
    check("first note content preserved", "first" in (vault / p1).read_text(encoding="utf-8"))


def test_detail_level():
    from storage.vault_writer import derive_detail_level as d

    cases = {
        "summarize": "summarize", "/summary please": "summarize", "detailed": "detailed",
        "precise": "precise", "/raw": "raw", "book": "book", "/book": "book", "ebook": "book",
        "handwritten": "handwritten", "manuscrito": "handwritten",
        "Nota escrita à mão": "handwritten", "pela minha letra": "handwritten",
        "a drawing of a bridge": "detailed",         # "raw" inside "drawing"
        "letras de músicas": "detailed",             # "letra" inside "letras"
        "Facebook post": "detailed",                 # "book" inside "Facebook"
        "detailed --force": "detailed", None: "detailed",
    }
    bad = {c: (d(c), want) for c, want in cases.items() if d(c) != want}
    check(f"caption routing whole-word ({len(cases)} cases){'' if not bad else ' ' + str(bad)}",
          not bad)


def test_cooldown_prune_and_errors():
    import bot

    bot._user_last_request.clear()
    bot._user_last_warning.clear()
    stale = time.monotonic() - bot.USER_COOLDOWN_SECONDS - 1
    for uid in range(bot._COOLDOWN_PRUNE_AT):
        bot._user_last_request[uid] = stale
    bot._check_rate_limit(999_999)
    check("expired cooldown entries pruned",
          set(bot._user_last_request) == {999_999})

    src = Path(bot.__file__).read_text(encoding="utf-8")
    check("analysis error reply no longer echoes exception",
          'AI analysis failed. Error: {e}' not in src)
    check("quota reply no longer echoes exception", "Detalhes: {e}" not in src)
    check("allowlist guard registered at group -1",
          "TypeHandler(Update, make_guard(ALLOWED_IDS)), group=-1" in src)
    check("main() fails closed on empty allowlist", "require_allowlist(ALLOWED_IDS)" in src)

    replies = []

    async def reply_text(text, **_kw):
        replies.append(text)

    upd = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))

    async def boom(*_a, **_kw):
        raise RuntimeError("secret-key=sk-LEAK")

    orig = bot.analyze_content
    bot.analyze_content = boom
    try:
        asyncio.run(bot.analyze_and_save(upd, None, "txt", "detailed", "", "text"))
    except RuntimeError:
        pass
    finally:
        bot.analyze_content = orig
    check("user reply hides exception details",
          replies and all("LEAK" not in r for r in replies))


def main():
    test_allowlist()
    test_config_path()
    test_no_overwrite()
    test_detail_level()
    test_cooldown_prune_and_errors()
    print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} checks passed")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
