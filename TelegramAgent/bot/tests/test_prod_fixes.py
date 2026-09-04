#!/usr/bin/env python3
"""Validation for the 5 production fixes (v1.10.1).

1. Telegram 20MB download pre-check   2. YouTube URL normalization
3. Queue lifecycle (add→process→clear) 4. rate-limit warn suppression
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("LLM_API_KEY", "test-key")
_tmpdb = tempfile.mkdtemp()
os.environ["DEDUP_DB_PATH"] = str(Path(_tmpdb) / "test.db")

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{PASS if cond else FAIL} {name}")


def main():
    import bot
    from parsers.video_parser import extract_youtube_id, is_youtube_url
    from storage import dedup_store as ds

    # ---- Fix 1: 20MB pre-check helpers ----
    check("limit constant is 20MB", bot.TELEGRAM_DOWNLOAD_LIMIT == 20 * 1024 * 1024)
    small = SimpleNamespace(file_size=19 * 1024 * 1024)
    big = SimpleNamespace(file_size=24 * 1024 * 1024)
    none_size = SimpleNamespace(file_size=None)
    check("19MB passes", not bot._too_large(small))
    check("24MB rejected", bot._too_large(big))
    check("unknown size passes (download attempted)", not bot._too_large(none_size))
    check("friendly text mentions 20MB and real size",
          "20MB" in bot._too_large_text(big) and "24MB" in bot._too_large_text(big))

    # ---- Fix 2: YouTube URL normalization ----
    vid = "dQw4w9WgXcQ"
    urls = [
        f"https://www.youtube.com/watch?v={vid}",
        f"https://www.youtube.com/watch?v={vid}&si=abc123",
        f"https://www.youtube.com/watch?si=xyz&v={vid}&t=42",
        f"https://youtu.be/{vid}?si=abc",
        f"https://m.youtube.com/watch?v={vid}",
        f"https://music.youtube.com/watch?v={vid}&list=PL1",
        f"https://www.youtube.com/shorts/{vid}",
        f"https://www.youtube.com/embed/{vid}",
        f"https://www.youtube.com/live/{vid}?feature=share",
    ]
    for u in urls:
        check(f"id extracted from {u.split('//')[1][:42]}", extract_youtube_id(u) == vid)
    check("is_youtube_url true for all", all(is_youtube_url(u) for u in urls))
    check("non-URL rejected", extract_youtube_id("https://example.com/watch?v=nope") is None)

    # ---- Fix 3: queue lifecycle add→process→clear by id ----
    ids = [ds.pending_add(777, "text", "alpha"), ds.pending_add(777, "text", "beta")]
    ds.pending_add(888, "text", "other chat")
    check("2 items queued", len(ds.pending_list(777, "text")) == 2)
    deleted = ds.pending_delete(ids)
    check("both items deleted atomically", deleted == 2)
    check("queue empty after clear", ds.pending_list(777, "text") == [])
    check("other chat untouched", len(ds.pending_list(888, "text")) == 1)
    check("delete with empty list is a no-op", ds.pending_delete([]) == 0)
    check("analyze_and_save documents 'duplicate' outcome",
          'return "duplicate"' in Path(bot.__file__).read_text())

    # ---- Fix 4/5: rate-limit warning suppression ----
    sent = []

    class FakeMsg:
        def __init__(self):
            self.text = None

        async def reply_text(self, t, **kw):
            sent.append(t)

    class FakeUpdate:
        def __init__(self):
            self.message = FakeMsg()

    async def scenario():
        bot._user_last_request.clear()
        bot._user_last_warning.clear()
        u = FakeUpdate()
        blocked1 = await bot._rate_limited_reply(u, 42)  # first passes → False
        # force into cooldown
        import time
        bot._user_last_request[42] = time.monotonic()
        b2 = await bot._rate_limited_reply(u, 42)  # warns once
        b3 = await bot._rate_limited_reply(u, 42)  # suppressed, no second warning
        return blocked1, b2, b3, len(sent)

    b1, b2, b3, n_warn = asyncio.get_event_loop().run_until_complete(scenario())
    check("first request allowed", b1 is False)
    check("blocked during cooldown", b2 is True)
    check("still blocked", b3 is True)
    check("exactly ONE warning for the burst", n_warn == 1)

    # ---- Fix 6: command guard in handle_text ----
    # Bare commands must NOT be queued as text.
    sent_cmd = []

    class FakeMsg2:
        def __init__(self, text):
            self.text = text

        async def reply_text(self, t, **kw):
            sent_cmd.append(t)

    class FakeEffectiveUser:
        id = 42

    class FakeUpdate2:
        effective_user = FakeEffectiveUser()
        def __init__(self, text):
            self.message = FakeMsg2(text)
            self.effective_chat = SimpleNamespace(id=999)

    async def cmd_guard_tests():
        # Test 1: bare commands -> guard catches, replies, returns (no queueing)
        for cmd in ["/text", "/voice", "/audio"]:
            sent_cmd.clear()
            await bot.handle_text(FakeUpdate2(cmd), SimpleNamespace(user_data={}))
            check(f"guard caught bare {cmd}", len(sent_cmd) == 1)
            check(f"bare {cmd} not queued", len(ds.pending_list(999, "text")) == 0)
        # Test 2: command with arg -> guard does NOT catch (has a space) -> queued
        sent_cmd.clear()
        await bot.handle_text(FakeUpdate2("/text some arg"), SimpleNamespace(user_data={}))
        check("guard skipped /text with argument", len(sent_cmd) == 1)
        check("/text with arg queued", len(ds.pending_list(999, "text")) == 1)
        # Test 3: plain text -> NOT caught -> queued
        sent_cmd.clear()
        await bot.handle_text(FakeUpdate2("hello world"), SimpleNamespace(user_data={}))
        check("guard skipped plain text", len(sent_cmd) == 1)
        check("plain text queued", len(ds.pending_list(999, "text")) == 2)
    guard_sent = asyncio.get_event_loop().run_until_complete(cmd_guard_tests())
    # Verify queue was NOT polluted with the bare commands
    check("queue has exactly 2 items (no bare commands)", len(ds.pending_list(999, "text")) == 2)
    check("bare /text not in queue", "/text" not in [
        it["content"] for it in ds.pending_list(999, "text")])
    check("bare /voice not in queue", "/voice" not in [
        it["content"] for it in ds.pending_list(999, "text")])
    check("bare /audio not in queue", "/audio" not in [
        it["content"] for it in ds.pending_list(999, "text")])
    # Clean up test data
    ds.pending_clear(999, "text")

    # ---- Fix 7: /research CommandHandler registration ----
    # Verify the /research command handler is registered (was missing, causing
    # /research <topic> to be silently swallowed by handle_unsupported).
    bot_src = Path(bot.__file__).read_text()
    check("/research handler registered in main()", 'CommandHandler("research"' in bot_src)
    check("research_command function exists", hasattr(bot, "research_command"))
    check("research_command is callable", callable(bot.research_command))


    print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} checks passed")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
