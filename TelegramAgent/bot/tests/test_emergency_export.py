"""Tests for storage/emergency_export.py (v1.12 vault-safety net)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate from any real vault/env on the dev machine.
_TMP = tempfile.mkdtemp(prefix="ee_vault_")
os.environ["OBSIDIAN_VAULT_PATH"] = _TMP
os.environ["EMERGENCY_EXPORT_DIR"] = os.path.join(_TMP, "emergency_export")

from storage.emergency_export import (  # noqa: E402
    HAZARDOUS_VAULT_DIRS,
    _guard_component,
    export_all,
    export_root,
    mirror_note,
    note_write_lag,
    scan_hazardous_notes,
)


def _write(root, rel, content="hello"):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_mirror_note_basic():
    src = _write(_TMP, "Programming/new note.md", "data-1")
    rel = str(src.relative_to(_TMP))
    out_rel = mirror_note(rel)
    assert out_rel is not None
    dest = export_root() / out_rel
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "data-1"


def test_mirror_note_preserves_tree_and_unicode():
    src = _write(_TMP, "Books/Série pt/Livro — cap.md", "livro")
    out_rel = mirror_note(str(src.relative_to(_TMP)))
    assert out_rel is not None
    assert (export_root() / out_rel).read_text(encoding="utf-8") == "livro"


def test_mirror_note_rejects_unsafe_paths():
    assert mirror_note("../escape.md") is None
    assert mirror_note("/absolute/path.md") is None
    assert mirror_note("a//b.md") is None


def test_mirror_note_missing_source_is_none():
    assert mirror_note("Nope/not-there.md") is None


def test_guard_component_blocks_traversal():
    assert "/" not in _guard_component("a/b/c", "f")
    assert ".." not in _guard_component("../evil", "f")
    assert _guard_component("../evil", "fallback") == "sevil"  # stripped, safe


def test_export_all_mirrors_vault():
    _write(_TMP, "AI/note1.md", "a")
    _write(_TMP, "Finance/note2.md", "b")
    _write(_TMP, "90_Attachments/thumb.png", "img")
    # Vault internals must NOT be mirrored.
    _write(_TMP, ".obsidian/plugins/x/main.js", "plugin")
    _write(_TMP, ".git/HEAD", "ref")
    result = export_all()
    assert result["files"] == 3
    assert (export_root() / "AI" / "note1.md").is_file()
    assert not (export_root() / ".obsidian").exists()
    assert not (export_root() / ".git").exists()


def test_export_all_idempotent():
    _write(_TMP, "AI/note1.md", "a")
    r1 = export_all()
    r2 = export_all()
    assert r1["files"] == r2["files"] == 1


def test_scan_hazardous_notes_detects_backups():
    (Path(_TMP) / "_Backups").mkdir(exist_ok=True)
    # .git should already be there from previous test; assert detection
    found = scan_hazardous_notes(_TMP)
    assert any("_Backups" in x for x in found)


def test_note_write_lag_quiet_when_no_recent():
    # Existing notes written now ARE recent; simulate empty vault fresh.
    empty = tempfile.mkdtemp(prefix="ee_empty_")
    n = note_write_lag(7) if False else _count_recent(empty)
    assert n == 0


def _count_recent(vault: str) -> int:
    # Inline copy of note_write_lag against an isolated root via env swap.
    old_root = os.environ["OBSIDIAN_VAULT_PATH"]
    os.environ["OBSIDIAN_VAULT_PATH"] = vault
    try:
        return note_write_lag(7)
    finally:
        os.environ["OBSIDIAN_VAULT_PATH"] = old_root


def test_mirror_note_uses_subdir_in_export():
    out = mirror_note("Programming/new note.md")
    assert out == "Programming/new note.md"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except Exception as e:
            fails += 1
            import traceback
            print(f"❌ {fn.__name__}:")
            traceback.print_exc(limit=3)
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)