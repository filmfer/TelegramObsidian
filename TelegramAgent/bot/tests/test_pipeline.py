"""End-to-end test for the Obsidian Knowledge Agent pipeline.

This test exercises:
  1. parse_document  -> extracts text from a sample .txt
  2. analyze_content -> mocked (to avoid needing a real Gemini API key)
  3. write_note_to_vault -> writes a Markdown note into the vault

Run:
    cd TelegramAgent/bot && OBSIDIAN_VAULT_PATH=ObsidianVault .venv/bin/python -m tests.test_pipeline
"""
import os
import sys
from pathlib import Path

# Ensure the bot package root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.document_parser import parse_document
from storage.vault_writer import write_note_to_vault, derive_detail_level


# --- Fake / mocked analyze_content (stand-in for the Gemini call) ---
def fake_analyze_content(content: str, detail: str, source_url: str = ""):
    """Mimic the real analyzer's output schema so the pipeline can be tested offline."""
    return {
        "title": "Sample Note Title",
        "category": "programming",
        "content": f"## Summary\nThis is a {detail} summary.\n\n## Source\n{content[:200]}",
        "tags": ["test", "sample", "pipeline"],
        "source_url": source_url,
        "detail_level": detail,
    }


def main():
    vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "ObsidianVault")
    os.makedirs(vault_root, exist_ok=True)

    # 1. Create a sample document
    sample_path = "sample_test_doc.txt"
    sample_text = "Project requirements for the Obsidian Knowledge Agent. It must parse Telegram documents, links, and emails, then categorize and summarize them into an Obsidian vault."
    with open(sample_path, "w", encoding="utf-8") as f:
        f.write(sample_text)

    # 2. Parse the document
    extracted = parse_document(sample_path)
    if not extracted:
        print("❌ parse_document failed")
        sys.exit(1)
    print("✅ parse_document extracted text")

    # 3. Mock the LLM call
    detail = derive_detail_level("summarize")
    note = fake_analyze_content(extracted, detail)
    note["source"] = f"telegram-doc::{sample_path}"
    note["source_type"] = "document"

    # 4. Write note to obsidian vault
    note_path = write_note_to_vault(note)
    if not note_path:
        print("❌ write_note_to_vault failed")
        sys.exit(1)
    print(f"✅ write_note_to_vault -> {note_path}")

    full = Path(vault_root) / note_path
    if not full.exists():
        print("❌ Note file was not created on disk")
        sys.exit(1)
    print("✅ End-to-end pipeline test PASSED")


if __name__ == "__main__":
    main()
