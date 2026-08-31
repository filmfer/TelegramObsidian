"""Image ingestion tests: Pillow prep, data-URI, OCR fallback, vision model pick."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("VISION_MODEL", "zai/glm-4v-flash")

from PIL import Image, ImageDraw

from parsers.image_parser import (
    image_to_base64_data_uri,
    ocr_image,
    prepare_image_bytes,
    vision_extract,
)
from llm.provider import default_vision_model


def _make_image(path, text_lines):
    img = Image.new("RGB", (600, 220), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(text_lines):
        d.text((15, 15 + i * 45), line, fill="black")
    img.save(path)


def test_prep_and_uri():
    p = "/tmp/_t_prep.png"
    _make_image(p, ["Test"])
    data = prepare_image_bytes(p)
    assert 0 < len(data) < 200_000, f"bad size {len(data)}"
    uri = image_to_base64_data_uri(p)
    assert uri.startswith("data:image/jpeg;base64,")
    os.remove(p)
    print("✅ image prep + data URI OK")


def test_ocr():
    p = "/tmp/_t_ocr.png"
    _make_image(p, ["TelegramObsidian Agent", "- reads images", "- creates notes"])
    text = ocr_image(p)
    os.remove(p)
    assert "TelegramObsidian" in text, f"OCR missed text: {text!r}"
    print("✅ OCR fallback extract OK")


def test_default_vision_model():
    m = default_vision_model()
    assert m == os.environ["VISION_MODEL"]
    print(f"✅ vision model pick: {m}")


def test_vision_extract_ocr_fallback():
    """No vision API key set → vision_extract falls back to OCR."""
    for k in ("ZAI_API_KEY", "ZHIPU_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        os.environ.pop(k, None)
    p = "/tmp/_t_ve.png"
    _make_image(p, ["Fallback Title", "- item one", "- item two"])
    out = asyncio.run(vision_extract([p]))
    os.remove(p)
    assert out and "Fallback" in out, f"vision_extract OCR failed: {out!r}"
    print("✅ vision_extract OCR fallback OK")


if __name__ == "__main__":
    test_prep_and_uri()
    test_ocr()
    test_default_vision_model()
    test_vision_extract_ocr_fallback()
    print("\n🎉 ALL IMAGE TESTS PASSED")