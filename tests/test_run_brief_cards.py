"""A run's brief is input to the run, not knowledge: it is never stored as a card, and one already
stored is never selected.

A queued run's text is a composed brief (standing instructions, scope, history). ``record`` used to
store a card for it, so a store filled with cards whose summary and keywords were that brief, with no
file behind them, and they answered ordinary searches. Everything here is structural: a queued run is
told apart by its ``task_id``, a stored brief by its shape (no file, content, convention or
editor-written field; a task id or a multi-paragraph request as provenance), never by its words.
"""
import json
from pathlib import Path

from hubofhubs.recall import card_origin
from quest_ai_runner.adapters.file_context_store import FileContextStore

BRIEF = ("Act as Someone.\n\nQuest outcome: ship the widgets\n\nStanding instructions for this quest. "
         "Nobody has written a brief yet.\n\nWork the widgets request above in full.")


def _store(tmp_path: Path) -> FileContextStore:
    (tmp_path / "widget.py").write_text("def widget():\n    return 1\n")
    return FileContextStore(str(tmp_path / ".quest-context"), repo_root=str(tmp_path), auto_bootstrap=False)


def _cards(tmp_path: Path):
    return sorted((tmp_path / ".quest-context").glob("*.json"))


def test_a_queued_run_that_pinned_nothing_stores_no_card(tmp_path):
    store = _store(tmp_path)
    store.record(BRIEF, {"kind": "met", "task_id": "task_1", "response": "Done, nothing more to say."})
    assert _cards(tmp_path) == []


def test_a_queued_run_that_pinned_a_file_stores_a_card_not_made_of_the_brief(tmp_path):
    store = _store(tmp_path)
    store.record(BRIEF, {"kind": "met", "task_id": "task_1", "files": ["widget.py"],
                         "response": "The widget returns one."})
    (path,) = _cards(tmp_path)
    card = json.loads(path.read_text())
    assert [f["path"] for f in card["files"]] == ["widget.py"]
    assert card["summary"] == "The widget returns one."
    assert "Nobody" not in json.dumps(card) and "Standing" not in json.dumps(card)
    assert card["provenance"]["created_by_task"] == "run task_1"


def test_an_ordinary_turn_is_still_recorded(tmp_path):
    store = _store(tmp_path)
    store.record("how do widgets work", {"kind": "met", "response": "They return one."})
    assert len(_cards(tmp_path)) == 1


def _brief_card(card_id, **extra):
    card = {"id": card_id, "keywords": ["widgets", "quest", "outcome"], "summary": "Widgets brief for the quest.",
            "conventions": [], "usage_count": 1, "last_outcome": "met",
            "provenance": {"created_by_task": BRIEF[:100], "model": "", "created_at": "", "last_verified_at": ""}}
    card.update(extra)
    return card


def test_the_structural_test_for_a_stored_brief():
    assert card_origin.is_run_brief_record(_brief_card("a"))
    thread = _brief_card("b")
    thread["provenance"] = {"created_by_task": "x", "task_id": "t1"}
    assert card_origin.is_run_brief_record(thread)
    # None of these is a brief: something is pinned, learned, written or owned.
    assert not card_origin.is_run_brief_record(_brief_card("c", files=[{"path": "widget.py"}]))
    assert not card_origin.is_run_brief_record(_brief_card("d", content=[{"id": "n", "type": "note", "text": "t"}]))
    assert not card_origin.is_run_brief_record(_brief_card("e", name="Widgets"))
    assert not card_origin.is_run_brief_record(_brief_card("f", conventions=["use tabs"]))
    one_line = _brief_card("g")
    one_line["provenance"]["created_by_task"] = "how do widgets work"
    assert not card_origin.is_run_brief_record(one_line)


def test_selection_ignores_a_brief_already_stored(tmp_path):
    store = _store(tmp_path)
    store.record("how do widgets work", {"kind": "met", "files": ["widget.py"], "response": "Widgets return one."})
    cards_dir = tmp_path / ".quest-context"
    stale = _brief_card("old-brief")
    (cards_dir / "old-brief.json").write_text(json.dumps(stale))
    kept = _brief_card("same-shape-but-written", name="Widgets quest notes")   # an editor wrote a name
    (cards_dir / "same-shape-but-written.json").write_text(json.dumps(kept))
    fresh = FileContextStore(str(cards_dir), repo_root=str(tmp_path), auto_bootstrap=False)
    ids = fresh.assemble("widgets quest outcome", read_only=True).card_ids
    assert "old-brief" not in ids
    assert "same-shape-but-written" in ids, "the same words on a card somebody wrote are still found"
