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
