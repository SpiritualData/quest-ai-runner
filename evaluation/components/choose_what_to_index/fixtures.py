"""A corpus with a backups folder of dated snapshots, rebuilt offline in a temporary directory with no randomness.

The shape follows the incident that motivated the risk gate: a machine's daily snapshots, one folder per date,
each a copy of the whole tree, each carrying the card store the tree had that day (and a copy of the tree's own
nested store). A corpus rooted above the backups folder walks into every one of them unless something decides it
should not. Beside the series the corpus holds what must keep working:

  docs/        an ordinary folder of notes that must stay indexed
  product/     one legitimate nested project with its own card store, which must still be imported or mounted
  backups/<date>/          one snapshot per date, its own card store at its root
  backups/<date>/product/  the snapshot's copy of the nested project's store

A case may set:

  dates        how many dated snapshots (one is a lone snapshot, not a series)
  large        add a bulk folder of notes so the corpus is over the folder review's file-count floor
  human_index  a person has written ``"index": true`` for ``backups`` in the lane's folder_review.json

Every card id that comes from a snapshot contains ``snap``; the nested project's card is ``nested-card``.
"""
import json
from pathlib import Path

from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import _write_bootstrap_meta

SERIES_DIR = "backups"
DOCS_DIR = "docs"
NESTED_DIR = "product"
NESTED_CARD = "nested-card"
BULK_FILES = 2100   # over the folder review's 2,000-file floor


def dates(n):
    return [f"2026-09-{day:02d}" for day in range(1, n + 1)]


def card(card_id, files):
    return {"id": card_id, "name": card_id.replace("-", " ").title(), "keywords": [card_id],
            "summary": f"About {card_id}", "description": f"About {card_id}",
            "files": [{"path": f, "sha256": "", "mtime": 0} for f in files],
            "provenance": {"created_by_task": "bootstrap"}}


def write_store(root, cards, recorded_root=None):
    """A bootstrapped store under ``root``: the cards, the files they pin, and the store's identity.

    ``recorded_root`` is the root the store records for itself (default ``root``); a backup copy of a
    live store carries the live corpus's root, not its own location."""
    cards_dir = root / ".quest-context"
    cards_dir.mkdir(parents=True, exist_ok=True)
    repo = FilesystemCardRepository(str(cards_dir))
    for c in cards:
        repo.write(c["id"], c)
        for fe in c["files"]:
            target = root / fe["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def f():\n    pass\n", encoding="utf-8")
    _write_bootstrap_meta(str(cards_dir), len(cards), root=str(recorded_root or root))
    return cards_dir


def build(corpus: Path, cards_dir: Path, case):
    """Write the corpus for ``case`` at ``corpus``; a person's verdict, when the case has one, goes in ``cards_dir``."""
    docs = corpus / DOCS_DIR
    docs.mkdir(parents=True)
    for name in ("guide", "plan", "glossary"):
        (docs / f"{name}.md").write_text(f"# {name}\nnotes about the {name}\n", encoding="utf-8")
    if case.get("large"):
        bulk = docs / "bulk"
        bulk.mkdir()
        for i in range(BULK_FILES):
            (bulk / f"n{i:04d}.md").write_text(f"# note {i}\n", encoding="utf-8")
    write_store(corpus / NESTED_DIR, [card(NESTED_CARD, ["pkg/m.py", "pkg/n.py"])])
    for d in dates(case.get("dates", 4)):
        snap = corpus / SERIES_DIR / d
        # A snapshot is a copy of the live tree: its notes folder, its nested project, and the live
        # store as it was that day (so it records the live corpus's root).
        write_store(snap, [card(f"snap-{d}", ["hq/docs/a.py", "hq/docs/b.py"])], recorded_root=corpus)
        (snap / DOCS_DIR).mkdir(exist_ok=True)
        (snap / DOCS_DIR / "guide.md").write_text("# guide\nnotes about the guide\n", encoding="utf-8")
        write_store(snap / NESTED_DIR, [card(f"snapprod-{d}", ["pkg/m.py", "pkg/n.py"])])
    if case.get("human_index"):
        cards_dir.mkdir(parents=True, exist_ok=True)
        (cards_dir / "folder_review.json").write_text(json.dumps(
            {"folders": {SERIES_DIR: {"index": True, "reason": "a person keeps these", "pinned": True}}}),
            encoding="utf-8")
