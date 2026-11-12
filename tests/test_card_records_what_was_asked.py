"""A card may record what a turn OBSERVED, never what it CLAIMED.

Measured live: a reply that declined a question ("I can't point you to anything in your records about X") became a learned card whose summary was that
sentence, and every later turn on the subject retrieved it and copied the decline. A turn that pinned no file and added no content observed nothing
to keep, so its card now says what was ASKED; a turn that did pin files (or added content) still summarises what it did.

Fully offline, no model calls.
"""
import json
from pathlib import Path

from quest_ai_runner.the_body.internal_context.card_store.file_context_store import FileContextStore


def read_cards(folder: Path):
    return [json.loads(path.read_text()) for path in folder.rglob("*.json")]


def test_an_answer_that_pinned_nothing_leaves_a_card_saying_what_was_asked(tmp_path):
    store = FileContextStore(str(tmp_path), auto_bootstrap=False)
    store.record("What can I do to practice focus every day?",
                 {"kind": "answer", "files": [], "response": "I can't point you to anything in your records about focus."})
    summaries = [c.get("summary", "") for c in read_cards(tmp_path)]
    assert summaries, "the turn is still recorded"
    assert all("can't point you" not in s for s in summaries)
    assert any("practice focus" in s for s in summaries)


def test_a_turn_that_added_content_still_summarises_what_it_did(tmp_path):
    store = FileContextStore(str(tmp_path), auto_bootstrap=False)
    store.record("Add the note about focus", {"kind": "answer", "files": [], "response": "Saved the focus note on the quest.",
                                              "content": [{"type": "note", "locator": {"text": "focus note"}}]})
    assert any("Saved the focus note" in c.get("summary", "") for c in read_cards(tmp_path))
