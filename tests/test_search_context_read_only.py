"""``search-context`` only looks: it must not write a card file.

A search used to stamp per-source usage heat (``last_used_ts`` / ``use_count``) on every card it
selected, so a documented read-only report rewrote cards (changing their hashes and mtimes) and
warmed cards that no task had used. The real use of a card (a task being sent) still warms it.
"""
import argparse
import hashlib
from pathlib import Path

from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.cli_context import _cmd_search_context, select_card_ids_for_text


def _corpus(tmp_path: Path) -> Path:
    for i in (1, 2, 3):
        (tmp_path / f"widget{i}.py").write_text(f"def widget{i}():\n    return {i}\n")
    store = FileContextStore(str(tmp_path / ".quest-context"), repo_root=str(tmp_path), auto_bootstrap=False)
    for i in (1, 2, 3):
        store.record(f"alpha widgets topic {i}", {"kind": "met", "files": [f"widget{i}.py"],
                                                  "response": f"Alpha widgets number {i} explained"})
    return tmp_path / ".quest-context"


def _hashes(cards: Path) -> dict:
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(cards.glob("*.json"))}


def test_search_context_writes_no_card_file(tmp_path, capsys):
    cards = _corpus(tmp_path)
    before = _hashes(cards)
    assert len(before) == 3
    args = argparse.Namespace(query="alpha widgets topic", corpus=str(tmp_path), cards_dir=str(cards),
                              no_llm=True)
    assert _cmd_search_context(args, None) == 0
    assert "Found" in capsys.readouterr().out, "the search must find the cards for this test to mean anything"
    assert _hashes(cards) == before


def test_a_task_that_uses_cards_still_warms_them(tmp_path):
    cards = _corpus(tmp_path)
    before = _hashes(cards)
    picked = select_card_ids_for_text("alpha widgets topic", corpus=str(tmp_path), cards_dir=str(cards))
    assert picked.card_ids
    assert _hashes(cards) != before
