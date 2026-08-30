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
    make_keyword_suggester,
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

# ---- v1.6: recursive counting · sub-folder preservation · keyword suggester ----
# The earlier blocks point CATEGORY_TAXONOMY_PATH at a minimal temp taxonomy;
# restore the repo default (with manual mappings + keywords) for these blocks.
os.environ.pop("CATEGORY_TAXONOMY_PATH", None)

with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    # Notes only inside sub-folders → must still be counted (rglob)
    (vault / "ML" / "Papers").mkdir(parents=True)
    (vault / "ML" / "Papers" / "a.md").write_text("---\ncategory: ML\n---\nx\n")
    (vault / "ML" / "Papers" / "b.md").write_text("---\ncategory: ML\n---\nx\n")
    (vault / "ML" / "Papers" / "c.md").write_text("---\ncategory: ML\n---\nx\n")
    (vault / "ML" / "Papers" / "d.md").write_text("---\ncategory: ML\n---\nx\n")
    counts = scan_categories(str(vault))
    check("Recursive count sees sub-folder notes", counts.get("ML") == 4)

with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    # Broad keeper + sparse folder with a sub-category inside
    (vault / "Programming").mkdir()
    for i in range(4):
        (vault / "Programming" / f"p{i}.md").write_text("---\ncategory: Programming\n---\nx\n")
    (vault / "Kubernetes").mkdir()
    (vault / "Kubernetes" / "k.md").write_text("---\ncategory: Kubernetes\n---\nx\n")
    (vault / "Kubernetes" / "Clusters").mkdir()
    (vault / "Kubernetes" / "Clusters" / "c1.md").write_text("---\ncategory: Kubernetes\n---\nx\n")
    plan = build_merge_plan(str(vault), make_keyword_suggester())
    kmerge = [m for m in plan if m[0] == "Kubernetes"]
    check("Manual merge planned for Kubernetes", bool(kmerge) and kmerge[0][1] == "Programming")
    moved = apply_merge(str(vault), plan)
    check("Notes moved", moved == 1)
    check("Sparse folder removed", not (vault / "Kubernetes").exists())
    check("Sub-folder preserved under target", (vault / "Programming" / "Clusters" / "c1.md").is_file())
    note = vault / "Programming" / "k.md"
    check("Merged note exists in target", note.is_file())
    check("Sub-folder note intact", "category" in (vault / "Programming" / "Clusters" / "c1.md").read_text(encoding="utf-8"))

with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    # Keyword-based suggestion (no manual entry): LLM-Notes → AI
    (vault / "AI").mkdir()
    for i in range(4):
        (vault / "AI" / f"a{i}.md").write_text("---\ncategory: AI\n---\nx\n")
    (vault / "LLM-Notes").mkdir()
    (vault / "LLM-Notes" / "n.md").write_text("---\ncategory: LLM-Notes\n---\nx\n")
    plan = build_merge_plan(str(vault), make_keyword_suggester())
    check("Keyword suggestion LLM-Notes → AI", any(m[0] == "LLM-Notes" and m[1] == "AI" for m in plan))

suggest = make_keyword_suggester()
check("Keyword suggester no match → None", suggest("MiscNotes", ["AI", "Programming"]) is None)

print("\n🎉 ALL ORGANIZER TESTS PASSED" if failures == 0 else f"\n💥 {failures} FAILURES")
sys.exit(1 if failures else 0)
