#!/usr/bin/env python3
"""Tests for storage/dashboard.py (v1.7 — Recent Notes dashboard).

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_dashboard.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.dashboard import (  # noqa: E402
    build_dashboard_markdown,
    collect_recent_notes,
    write_dashboard,
)

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("✅" if cond else "❌"), name)
    if not cond:
        failures += 1


with tempfile.TemporaryDirectory() as td:
    vault = Path(td)

    def make_note(folder: str, name: str, title: str, age_days: float = 0.0) -> None:
        d = vault / folder
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(f"---\ntitle: {title}\ncategory: {folder}\n---\n\nBody.\n", encoding="utf-8")
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(p, (old, old))

    make_note("Programming", "a.md", "Fresh Python Note")
    make_note("Programming", "old.md", "Old Note", age_days=20)
    (vault / "Programming" / "Sub").mkdir()
    make_note("Programming/Sub", "b.md", "Fresh Sub Note")
    make_note("AI", "c.md", "Fresh AI Note", age_days=3)
    make_note("Finance", "f.md", "Old Finance", age_days=30)
    (vault / "99_Templates").mkdir()
    make_note("99_Templates", "tpl.md", "Template — must be skipped")

    groups = collect_recent_notes(str(vault), days=7)
    check("Programming has 2 recent notes", len(groups.get("Programming", [])) == 2)
    check("AI has 1 recent note", len(groups.get("AI", [])) == 1)
    check("Old Finance note excluded", "Finance" not in groups)
    check("Sub-folder note collected", any(n.name == "b.md" for _, n, _ in groups["Programming"]))
    check("Newest first within category",
          groups["Programming"][0][2] == "Fresh Sub Note")

    md = build_dashboard_markdown(str(vault), days=7)
    check("Markdown has frontmatter", md.startswith("---"))
    check("Markdown links recent note", "[[Programming/a|Fresh Python Note]]" in md)
    check("Markdown links sub-folder note", "[[Programming/Sub/b|Fresh Sub Note]]" in md)
    check("Old note not in markdown", "Old Note" not in md)
    check("Reserved folder skipped", "99_Templates" not in md)
    check("Category headers with counts", "## AI (1)" in md and "## Programming (2)" in md)

    out = write_dashboard(str(vault))
    check("Dashboard file written", out is not None and (vault / "Recent Notes.md").is_file())
    written = (vault / "Recent Notes.md").read_text(encoding="utf-8")
    check("Written file contains links", "[[Programming/a|Fresh Python Note]]" in written)

with tempfile.TemporaryDirectory() as td:
    check("Empty vault → no dashboard", write_dashboard(td) is None)

print("\n🎉 ALL DASHBOARD TESTS PASSED" if failures == 0 else f"\n💥 {failures} FAILURES")
sys.exit(1 if failures else 0)
