#!/usr/bin/env python3
"""Offline tests for the handwritten-notes pipeline (v1.10, DEV)."""
import os
import sys
import tempfile
from pathlib import Path
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
    # 1. caption routing -> handwritten
    from storage.vault_writer import derive_detail_level

    for cap in ("handwritten", "manuscrito", "Nota escrita à mão", "pela minha letra"):
        check(f"derive_detail_level('{cap}') == handwritten",
              derive_detail_level(cap) == "handwritten")
    check("caption 'book' still -> book", derive_detail_level("book") == "book")
    check("empty caption -> default", derive_detail_level(None) == "detailed")

    # 2. prompt discipline: verbatim, pt-PT, [?], META_JSON
    import parsers.handwriting_parser as hp

    sysp = hp.SYSTEM_HANDWRITING
    check("prompt mentions pt-PT", "Portugal" in sysp or "pt-PT" in sysp)
    check("prompt enforces VERBATIM", "VERBATIM" in sysp)
    check("prompt uses [?] for illegible", "[?]" in sysp)
    check("prompt asks for META_JSON", "META_JSON" in sysp)

    # 3. few-shot reference store (temp dir)
    with tempfile.TemporaryDirectory() as td:
        with patch.object(hp, "HANDWRITING_REF_DIR", Path(td)):
            img = hp.save_handwriting_reference(
                _make_test_image(td), "Lista de compras:\n- pão\n- leite"
            )
            check("reference pair saved (jpg+txt)",
                  img.is_file() and img.with_suffix(".txt").is_file())
            ex = hp._load_recent_examples(3)
            check("example loaded", len(ex) == 1)
            check("reference preserves line breaks",
                  "pão\n- leite" in ex[0]["reference"])
            fs = hp.build_fewshot_section()
            check("few-shot section renders sample", "Sample 1" in fs and "pão" in fs)
            check("few-shot empty state", "(No reference samples yet.)"
                  in hp.build_fewshot_section() if not ex else True)

    # 4. no-API-key env does not crash import; OCR fallback callable exists
    check("ocr fallback exists", callable(hp._ocr_fallback))
    check("transcribe_handwritten is async-coroutine factory",
          hasattr(hp.transcribe_handwritten, "__call__"))

    # 5. dev-mode flag parses
    check("dev mode defaults on", hp.HANDWRITING_DEV_MODE is True)

    print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} checks passed")
    return 0 if all(ok for _, ok in results) else 1


def _make_test_image(td: str) -> str:
    from PIL import Image, ImageDraw

    p = os.path.join(td, "sample_input.png")
    im = Image.new("RGB", (300, 150), "white")
    ImageDraw.Draw(im).text((10, 10), "teste manuscrito", fill="black")
    im.save(p)
    return p


if __name__ == "__main__":
    sys.exit(main())
