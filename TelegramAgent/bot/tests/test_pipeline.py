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
from parsers.book_parser import (
    BOOK_EXTENSIONS,
    is_book_file,
    extract_book_metadata,
)
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


def test_book_pipeline(vault_root: str) -> None:
    """Verify book metadata extraction + note writing with book frontmatter."""
    # 1. BOOK_EXTENSIONS covers the requested digital formats
    for ext in (".pdf", ".epub", ".mobi", ".azw", ".azw3", ".azw4", ".djvu", ".fb2", ".lit"):
        if ext not in BOOK_EXTENSIONS:
            print(f"❌ BOOK_EXTENSIONS missing {ext}")
            sys.exit(1)
    print("✅ BOOK_EXTENSIONS covers all requested formats")

    # 2. is_book_file
    if not is_book_file("some/Book Title.epub"):
        print("❌ is_book_file('.epub') returned False")
        sys.exit(1)
    if is_book_file("notes.txt"):
        print("❌ is_book_file('.txt') returned True")
        sys.exit(1)
    print("✅ is_book_file works")

    # 3. extract_book_metadata on a real minimal EPUB (built in-memory)
    import tempfile
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("test-123")
    book.set_title("Test Book Title")
    book.set_language("en")
    book.add_author("Jane Doe")
    book.add_author("John Smith")
    c1 = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
    c1.content = "<h1>Chapter 1</h1><p>This is the body of the test book.</p>"
    book.add_item(c1)
    book.toc = (c1,)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1]

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        epub.write_epub(tmp.name, book)
        meta = extract_book_metadata(tmp.name)

    if not meta:
        print("❌ extract_book_metadata returned None for EPUB")
        sys.exit(1)
    if meta["title"] != "Test Book Title":
        print(f"❌ Unexpected title: {meta['title']}")
        sys.exit(1)
    if "Jane Doe" not in meta["authors"] or "John Smith" not in meta["authors"]:
        print(f"❌ Unexpected authors: {meta['authors']}")
        sys.exit(1)
    if "body of the test book" not in meta["text"]:
        print("❌ Book text was not extracted")
        sys.exit(1)
    print(f"✅ extract_book_metadata -> {meta['title']} by {meta['authors']}")

    # 4. Write a book note and verify book frontmatter
    book_note = {
        "title": meta["title"],
        "category": "books",
        "content": meta["text"],
        "tags": ["book"],
        "source": "telegram-book::test.epub",
        "source_type": "book",
        "attachment": "90_Attachments/test.epub",
        "detail_level": "book",
        "book_title": meta["title"],
        "book_authors": meta["authors"],
        "book_year": meta["year"],
    }
    note_path = write_note_to_vault(book_note)
    if not note_path:
        print("❌ write_note_to_vault failed for book note")
        sys.exit(1)
    content = (Path(vault_root) / note_path).read_text(encoding="utf-8")
    if "book_title: Test Book Title" not in content:
        print(f"❌ book_title missing from frontmatter:\n{content[:400]}")
        sys.exit(1)
    if "book_authors" not in content or "Jane Doe" not in content:
        print(f"❌ book_authors missing from frontmatter:\n{content[:400]}")
        sys.exit(1)
    print(f"✅ Book note written with frontmatter -> {note_path}")


def main():
    vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "ObsidianVault")
    os.makedirs(vault_root, exist_ok=True)

    # 0. Book feature tests
    test_book_pipeline(vault_root)

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
