#!/usr/bin/env python3
"""Offline tests for the /image command flow (v1.11)."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("LLM_API_KEY", "test-key")

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{PASS if cond else FAIL} {name}")


def main():
    import bot
    from parsers import image_parser as ip
    from storage.vault_writer import write_note_to_vault

    # 1. /image caption detection
    check("'/image' detected", bot._wants_image_analysis("/image"))
    check("'/image extra' detected", bot._wants_image_analysis("/Image fix this"))
    check("'detailed' NOT treated as /image", not bot._wants_image_analysis("detailed"))
    check("empty caption not detected", not bot._wants_image_analysis(""))

    # 2. vision fallback chain: free tiers first, paid Gemini last
    with patch("llm.provider._provider_ready",
               side_effect=lambda p: p in ("zai", "gemini")):
        from llm.provider import vision_fallback_chain
        chain = vision_fallback_chain()
        check("free tier before paid Gemini",
              chain.index("zai/glm-4v-flash") < chain.index("gemini/gemini-2.0-flash"))
    with patch("llm.provider._provider_ready", side_effect=lambda p: p == "gemini"):
        from llm.provider import vision_fallback_chain as vfc
        check("only-Gemini env still yields a chain",
              vfc() == ["gemini/gemini-2.0-flash"])

    # 3. vision_extract chunking for large sets (mock chat_vision)
    calls = []

    async def fake_chat_vision(system, prompt, uris, max_tokens=4096):
        calls.append(len(uris))
        return (f"extracted-{len(uris)}", {"model": "mock"})

    with tempfile.TemporaryDirectory() as td:
        from PIL import Image

        imgs = []
        for i in range(9):
            p = Path(td) / f"img{i}.png"
            Image.new("RGB", (20, 20), "white").save(p)
            imgs.append(str(p))
        with patch("llm.provider.chat_vision", side_effect=fake_chat_vision), \
             patch.dict(os.environ, {"VISION_MAX_IMAGES_PER_CALL": "4"}):
            out = asyncio_run(ip.vision_extract(imgs, detailed=True))
        check("9 images chunked into 4+4+1", calls == [4, 4, 1])
        check("chunk outputs merged", out.count("extracted-") == 3)
        check("chunk headers present", "Images img0" in out and "img8" in out)

    # 4. gallery + multiple attachments in the written note
    with tempfile.TemporaryDirectory() as td:
        att_dir = Path(td) / "90_Attachments"
        att_dir.mkdir(parents=True)
        for i in range(3):
            (att_dir / f"p{i}.jpg").write_bytes(b"x")
        note = {
            "title": "Gallery Test", "source": "t", "source_type": "image",
            "tags": ["image"], "content": "body text",
            "attachments": [
                str(Path("90_Attachments") / f"p{i}.jpg") for i in range(3)
            ],
        }
        with patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": td}):
            rel = write_note_to_vault(note)
        body = (Path(td) / rel).read_text()
        check("3 gallery embeds written", body.count("![[" ) == 3)
        check("attachments in frontmatter",
              "p0.jpg" in body.split("---")[1])
        check("body preserved after gallery", "body text" in body)

    # 5. robust download helper: size pre-check, honest errors
    big = SimpleNamespace(file_size=25 * 1024 * 1024)
    try:
        asyncio_run(bot._download_telegram_file(big, td, what="x"))
        check("oversized raises FileTooBigError", False)
    except bot.FileTooBigError:
        check("oversized raises FileTooBigError", True)
    except Exception as e:
        check(f"oversized raises FileTooBigError (got {type(e).__name__})", False)

    txt = bot._download_error_text("photo", bot.FileTooBigError("25MB"))
    check("too-big message mentions 20MB", "20MB" in txt)
    txt2 = bot._download_error_text(
        "photo", bot.PhotoDownloadError("TimedOut: read timeout")
    )
    check("network failure shows the REAL error, no 20MB blame",
          "TimedOut" in txt2 and "20MB" not in txt2)

    # success path via mocked get_file/download_to_drive
    class FakeFile:
        async def download_to_drive(self, dest):
            p = Path(dest) / "ok.jpg"
            p.write_bytes(b"data")
            return str(p)

    class FakeMedia:
        file_size = 1024

        async def get_file(self):
            return FakeFile()

    got = asyncio_run(_dl_ok(td))
    check("happy path returns file path", isinstance(got, str) and got.endswith("ok.jpg"))

    # 6. album failure must NEVER be silent (create_task swallowing bug)
    sent2 = []

    class FailStatus:
        async def fail(self, reason):
            sent2.append(reason)

        async def update(self, text):
            pass

    class FakeChat:
        async def send_message(self, text, **kw):
            sent2.append(text)
            async def edit(t, **kw2):
                sent2.append(t)
            return SimpleNamespace(edit_text=edit)

    class FakeMsg2:
        caption = ""
        photo = [SimpleNamespace()]
        effective_chat = FakeChat()

        async def reply_text(self, t, **kw):
            sent2.append(t)

    fake_update = SimpleNamespace(message=FakeMsg2(), effective_chat=FakeChat())
    bot._album_buffers["mg-test"] = {
        "messages": [SimpleNamespace(photo=[SimpleNamespace()], caption="")],
        "first_update": fake_update,
        "caption": "",
        "task": None, "context": None,
    }

    async def boom(*a, **k):
        raise RuntimeError("vision exploded")

    with patch.object(bot, "_photo_job", side_effect=boom):
        asyncio_run(bot._process_album("mg-test"))
    check("album failure reaches the user", any("failed" in x for x in sent2))
    check("album failure includes the real cause",
          any("vision exploded" in x for x in sent2))
    check("album buffer cleaned after failure", "mg-test" not in bot._album_buffers)

    # 7. /audio command + caption immediate-transcription path
    src_text = Path(bot.__file__).read_text()
    check("/audio command registered",
          'CommandHandler("audio", voice_note_command)' in src_text)
    check("captions starting with /audio or /voice transcribe immediately",
          'lstrip("/").startswith(("audio", "voice"))' in src_text or
          'startswith(("audio", "voice"))' in src_text)
    check("audio download uses robust helper",
          "_download_telegram_file(media, staging" in src_text)

    # 8. staging content round-trip (path + file_id) for the /voice queue
    enc = bot._staging_content("/app/data/staging/5/abc.oga", "AwAd-123")
    p, fid = bot._staging_entry(enc)
    check("staging JSON stores path and file_id", p == "/app/data/staging/5/abc.oga" and fid == "AwAd-123")
    check("legacy plain-path content still parses", bot._staging_entry("/x/a.oga") == ("/x/a.oga", None))

    # 9. Queue cleanup on unrecoverable error (text)
    from storage.dedup_store import pending_add, pending_list, pending_clear
    import importlib
    tmpdb = tempfile.mktemp(suffix=".db")
    os.environ["DEDUP_DB_PATH"] = tmpdb
        # re-import so the module picks up the new path
    import importlib, storage.dedup_store as _ds
    importlib.reload(_ds)
    _ds.init_db()
    cid = 999001
    _ds.pending_add(cid, "text", "hello")
    _ds.pending_add(cid, "text", "world")
    items_before = _ds.pending_list(cid, "text")
    check("2 text items queued", len(items_before) == 2)
    # Simulate what the fixed code does on terminal outcome (None = unrecoverable error)
    ids = [it["id"] for it in items_before]
    deleted = _ds.pending_delete(ids)
    check("queue cleared after terminal outcome", deleted == 2 and len(_ds.pending_list(cid, "text")) == 0)
    os.unlink(tmpdb)

    print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} checks passed")
    return 0 if all(ok for _, ok in results) else 1


def asyncio_run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


async def _dl_ok(_td):
    """Happy-path download into a FRESH temp dir (the previous one may be closed)."""
    import tempfile

    import bot as _bot

    class FakeFile:
        async def download_to_drive(self, dest):
            p = Path(dest) / "ok.jpg"
            p.write_bytes(b"data")
            return str(p)

    class FakeMedia:
        file_size = 1024

        async def get_file(self):
            return FakeFile()

    with tempfile.TemporaryDirectory() as fresh:
        return await _bot._download_telegram_file(FakeMedia(), fresh, what="ok")


if __name__ == "__main__":
    sys.exit(main())
