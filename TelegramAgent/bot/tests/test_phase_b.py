"""Quick sanity tests for Phase B helpers (run then delete)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.book_parser import clean_book_text, split_into_chunks, count_chapters

sample = (
    "Cover\n\nTable of Contents\nChapter 1 ..... 5\nChapter 2 ..... 23\n\n"
    "12\n\nChapter 1: Introduction\n\nThis is real content about bash "
    "scripting. It teaches concepts.\n\nMore paragraph text with substance.\n" * 3
)

c = clean_book_text(sample)
assert "....." not in c, "TOC dotted lines not removed"
assert "\n12\n" not in c, "bare page numbers not removed"
print(f"✅ clean_book_text OK ({len(c)} chars)")

chunks = split_into_chunks(c, max_chars=200)
assert chunks and all(len(ch) <= 260 for ch in chunks), f"chunk sizing broken: {[len(ch) for ch in chunks]}"
print(f"✅ split_into_chunks OK ({len(chunks)} chunks)")

assert count_chapters(c) >= 3, "chapter detection too low"
print("✅ count_chapters OK")

from parsers.search_parser import search_web  # noqa: F401
print("✅ search_parser imports")

from parsers.audio_parser import transcribe_audio, TranscriptionError  # noqa: F401
try:
    import asyncio
    asyncio.run(transcribe_audio("/nonexistent.ogg"))
except TranscriptionError as e:
    assert "GROQ_API_KEY" in str(e) or "No such file" in str(e)
    print(f"✅ audio_parser error path OK ({str(e)[:50]})")
except FileNotFoundError:
    print("✅ audio_parser error path OK (file missing)")

from llm.analyzer import BOOK_SECTION_PROMPT, BOOK_FINAL_PROMPT, RESEARCH_PROMPT
p = BOOK_SECTION_PROMPT.format(section_num=1, total=10, book_title="T", authors="A")
assert '"T"' in p and "section 1 of 10" in p
p2 = RESEARCH_PROMPT.format(topic="linux", categories="programming")
assert 'Research: linux' in p2
print("✅ Phase B prompts format OK")

print("\n🎉 ALL PHASE B SANITY TESTS PASSED")