"""Vault organizer: consolidate sparse category folders (Task 3).

/organize preview — shows the merge plan, touches nothing.
/organize        — shows the plan and asks for confirmation before moving.

Category folders live at the vault root (created by vault_writer).
Reads config/category_taxonomy.yaml for protected folders, manual merges
and the note-count threshold.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# Folders that are never touched by the organizer
_RESERVED_PREFIXES = ("00_", "90_", "99_")


def _taxonomy_path() -> Path:
    return Path(
        os.getenv(
            "CATEGORY_TAXONOMY_PATH",
            str(Path(__file__).resolve().parent.parent / "config" / "category_taxonomy.yaml"),
        )
    )


def load_taxonomy() -> Dict:
    try:
        with open(_taxonomy_path(), "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = _create_default_taxonomy()
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"Could not read taxonomy: {e} — using defaults")
        data = {}
    return {
        "protected": list(data.get("protected", [])),
        "manual": dict(data.get("manual", {})),
        "keywords": {
            str(broad).lower(): [str(k).lower() for k in (kws or [])]
            for broad, kws in dict(data.get("keywords", {})).items()
        },
        "threshold": int(data.get("threshold", 3)),
    }


def _create_default_taxonomy() -> dict:
    """Write config/category_taxonomy.yaml with sensible defaults if missing."""
    default = {
        "protected": ["Books", "Finance"],
        "manual": {
            "Kubernetes": "Programming",
            "Docker": "Programming",
            "Machine-Learning": "AI",
        },
        "keywords": {
            "Programming": ["python", "javascript", "typescript", "code", "dev",
                            "git", "software", "web", "sql", "database"],
            "AI": ["ai", "llm", "gpt", "neural", "machine-learning", "prompt",
                   "chatbot", "agent"],
            "Finance": ["money", "invest", "budget", "crypto", "tax", "stocks",
                        "bank", "savings"],
            "Car": ["car", "auto", "vehicle", "mechanic", "engine", "tyre", "tire"],
            "Religion": ["bible", "faith", "theology", "church", "spiritual",
                         "scripture"],
            "Food": ["diet", "nutrition", "recipe", "cooking", "meal", "food"],
            "Travel": ["trip", "flight", "vacation", "hotel", "itinerary",
                       "travel"],
            "Hacking": ["hack", "pentest", "security", "ctf", "exploit"],
        },
        "threshold": 3,
    }
    try:
        path = _taxonomy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(default, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        logger.info("Created default category taxonomy at %s", path)
    except OSError as e:
        logger.warning(f"Could not write default taxonomy: {e}")
    return default


def scan_categories(vault_root: str) -> Dict[str, int]:
    """Count .md notes per category folder at the vault root.

    A single unreadable sub-folder (e.g. a slow/intermittent rclone mount
    or a permissions error) must NOT abort the whole scan — it is logged and
    skipped so /organize always returns instead of crashing.
    """
    vault = Path(vault_root)
    counts: Dict[str, int] = {}
    try:
        children = list(vault.iterdir())
    except OSError as e:
        logger.error(f"Could not list vault root {vault}: {e}")
        return counts
    for child in children:
        if child.name.startswith(".") or child.name.startswith(_RESERVED_PREFIXES):
            continue
        try:
            if not child.is_dir():
                continue
            # Recursive: folders whose notes live in sub-categories still count
            n = len(list(child.rglob("*.md")))
        except OSError as e:
            logger.warning(f"Skipping unreadable category folder {child}: {e}")
            continue
        if n:
            counts[child.name] = n
    return counts


def build_merge_plan(
    vault_root: str,
    suggest_fn: Optional[Callable[[str, List[str]], Optional[str]]] = None,
) -> List[Tuple[str, str, int]]:
    """
    Compute a merge plan: [(folder, target, note_count), ...].
    `suggest_fn(folder, keepers)` may return a target folder name; if it
    returns None the folder is left alone. Never raises.
    """
    tax = load_taxonomy()
    counts = scan_categories(vault_root)
    # Biggest keepers first — sparse folders merge into the dominant broad category
    keepers = [
        c for c, n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= tax["threshold"]
    ]

    plan: List[Tuple[str, str, int]] = []
    for folder, count in sorted(counts.items(), key=lambda kv: kv[1]):
        if folder in tax["protected"] or folder in keepers:
            continue
        if folder in tax["manual"]:
            target = tax["manual"][folder]
            if target in counts or target in [t for _, t, _ in plan]:
                plan.append((folder, target, count))
            continue
        if suggest_fn and keepers:
            target = suggest_fn(folder, keepers)
            if target and target != folder and target in keepers:
                plan.append((folder, target, count))
    return plan


def make_keyword_suggester() -> Callable[[str, List[str]], Optional[str]]:
    """
    Build a deterministic suggest_fn that maps sparse folders to broad
    categories using the taxonomy `keywords:` rules — a merge is suggested
    when a keyword appears (case-insensitive) inside the folder name.
    Manual mappings in `manual:` always take precedence (checked earlier).
    """
    keywords = load_taxonomy().get("keywords", {})

    def suggest(folder: str, keepers: List[str]) -> Optional[str]:
        name = folder.lower()
        for broad in keepers:
            for kw in keywords.get(broad.lower(), []):
                if kw and kw in name:
                    return broad
        return None

    return suggest


def _rewrite_frontmatter(note: Path, new_category: str, old_category: str) -> bool:
    """Set category=new_category, keep old as tag; True if file changed."""
    try:
        text = note.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        logger.error(f"Could not read {note}: {e}")
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return False

    fm["category"] = new_category
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    old_tag = old_category.lower()
    if old_tag not in [str(t).lower() for t in tags]:
        tags.append(old_tag)
    fm["tags"] = tags

    body = text[end + 4:]
    new_text = "---\n" + yaml.dump(fm, allow_unicode=True, sort_keys=False) + "---" + body
    try:
        note.write_text(new_text, encoding="utf-8")
        return True
    except OSError as e:
        logger.error(f"Could not write {note}: {e}")
        return False


def apply_merge(vault_root: str, plan: List[Tuple[str, str, int]]) -> int:
    """
    Execute the plan: move notes, update frontmatter, git-commit once.
    Returns the number of notes moved. Never raises.
    """
    vault = Path(vault_root)
    moved = 0
    for folder, target, _count in plan:
        src_dir = vault / folder
        dst_dir = vault / target
        if not src_dir.is_dir():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        for note in list(src_dir.glob("*.md")):
            if _rewrite_frontmatter(note, target, folder):
                dest = dst_dir / note.name
                if dest.exists():
                    dest = dst_dir / f"{note.stem}-moved{note.suffix}"
                try:
                    shutil.move(str(note), str(dest))
                    moved += 1
                except OSError as e:
                    logger.error(f"Move failed for {note}: {e}")
        # Move sub-category folders as units — sub-structure is preserved
        try:
            subdirs = [s for s in src_dir.iterdir() if s.is_dir()]
        except OSError as e:
            logger.warning(f"Could not list {src_dir} sub-folders: {e}")
            subdirs = []
        for sub in subdirs:
            dest = dst_dir / sub.name
            if dest.exists():
                logger.warning(
                    f"Sub-folder conflict: {sub} → {dest} already exists — left in place"
                )
                continue
            try:
                shutil.move(str(sub), str(dest))
            except OSError as e:
                logger.error(f"Sub-folder move failed for {sub}: {e}")
        try:
            src_dir.rmdir()  # only succeeds when empty
        except OSError:
            logger.warning(f"Folder {src_dir} not empty after merge — left in place")

    if moved:
        _git_commit(vault, plan, moved)
        _append_log(vault, plan, moved)
    return moved


def _git_commit(vault: Path, plan: List[Tuple[str, str, int]], moved: int) -> None:
    if not (vault / ".git").exists():
        return
    summary = ", ".join(f"{f}→{t}" for f, t, _ in plan[:5])
    try:
        subprocess.run(["git", "add", "-A"], cwd=vault, check=True, timeout=60)
        subprocess.run(
            ["git", "commit", "-m", f"organize: merged {moved} notes ({summary})"],
            cwd=vault, check=True, timeout=60, capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"Git commit after organize failed: {e}")


def _append_log(vault: Path, plan: List[Tuple[str, str, int]], moved: int) -> None:
    log_file = vault / "10_Categories" / "_organize_log.md"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"- **{datetime.now().strftime('%Y-%m-%d %H:%M')}** — moved {moved} notes:"]
    lines += [f"  - `{f}` → `{t}` ({c} notes)" for f, t, c in plan]
    try:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as e:
        logger.warning(f"Could not write organize log: {e}")

