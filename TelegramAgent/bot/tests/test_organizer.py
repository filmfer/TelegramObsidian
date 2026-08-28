#!/usr/bin/env python3
"""Tests for storage/vault_organizer.py (Task 3 — /organize).

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_organizer.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.vault_organizer import (  # noqa: E402
    apply_merge,
    build_merge_plan,
    load_taxonomy,
    scan_categories,
)

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("✅" if cond else "❌"), name)
    if not cond:
        failures += 1


TMP = tempfile.mkdtemp(prefix="organize-test-")
vault = Path(TMP) / "vault"
vault.mkdir()


def make_note(folder: str, name: str, category: str) -> None:
    d = vault / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\ntitle: {name}\ncategory: {category}\ntags: [test]\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )


# Big keepers (>= threshold), tiny candidates, protected folder
for i in range(5):
    make_note("Programming", f"p{i}.md", "Programming")
for i in range(4):
    make_note("Finance", f"f{i}.md", "Finance")
make_note("Books", "b1.md", "Books")  # protected, only 1 note
make_note("OddStuff", "o1.md", "OddStuff")
make_note("OddStuff", "o2.md", "OddStuff")

# Custom taxonomy: threshold 3, Books protected, manual OddStuff→Finance
tax = Path(TMP) / "taxonomy.yaml"
tax.write_text(
    "protected:\n  - Books\nmanual:\n  OddStuff: Finance\nthreshold: 3\n",
    encoding="utf-8",
)
os.environ["CATEGORY_TAXONOMY_PATH"] = str(tax)

check("Taxonomy loads", load_taxonomy()["threshold"] == 3)
counts = scan_categories(str(vault))
check("Scan counts all folders",
      counts == {"Programming": 5, "Finance": 4, "Books": 1, "OddStuff": 2})

plan = build_merge_plan(str(vault))
check("Protected folder is not merged", ("Books", "Finance", 1) not in plan)
check("Big folders are not merged", ("Programming", "Finance", 5) not in plan)
check("Manual merge applied", ("OddStuff", "Finance", 2) in plan)

moved = apply_merge(str(vault), plan)
check("Merge moved the right number of notes", moved == 2)
check("Source folder removed", not (vault / "OddStuff").exists())
check("Notes landed in target", len(list((vault / "Finance").glob("*.md"))) == 6)

note = vault / "Finance" / "o1.md"
text = note.read_text(encoding="utf-8")
check("Frontmatter category updated", "category: Finance" in text)
check("Old category kept as tag", "oddstuff" in text.lower())
check("Note body preserved", "Body of o1" in text)

log = vault / "10_Categories" / "_organize_log.md"
check("Organize log written", log.is_file() and "OddStuff" in log.read_text(encoding="utf-8"))

print("\n🎉 ALL ORGANIZER TESTS PASSED" if failures == 0 else f"\n💥 {failures} FAILURES")
sys.exit(1 if failures else 0)
