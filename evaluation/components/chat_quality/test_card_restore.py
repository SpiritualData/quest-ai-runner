"""Offline unit tests for the per-case CARD CONTENT restore's selection logic.

``cards_created_since``/``sweep_new_cards`` (see ``test_card_sweep.py``) only catch a card a run
CREATED outright: an id-only diff against the baseline. The card learner also APPENDS learned
items onto a card that already existed -- most often a world quest's own auto-maintained card,
which an id-only sweep can never see since its id was already in the baseline before the case ran.
These tests pin the pure selection logic (``cards_to_restore``, ``managed_only_card``) that decides
what a full-dict snapshot/restore undoes; no network, no real waiting.

Run:  .venv/bin/python3 -m pytest evaluation/qualitative/test_card_restore.py -q

Kept beside the harness (not under tests/) for the same reason as the other files here: importing
``world`` pulls in ``devclient``, which only loads against this machine's dev-lane env, so these are
not part of the library's default offline suite (``testpaths = ["tests"]``).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import world as W  # noqa: E402


# --------------------------------------------------------------------------- cards_to_restore

CARD_A = {"id": "card-a", "name": "A", "content": [{"id": "x", "text": "hello"}]}
CARD_B = {"id": "card-b", "name": "B", "content": []}


def test_untouched_cards_are_neither_new_nor_changed():
    before = {"card-a": CARD_A, "card-b": CARD_B}
    after = {"card-a": CARD_A, "card-b": CARD_B}
    new_ids, changed = W.cards_to_restore(before, after)
    assert new_ids == []
    assert changed == {}


def test_a_card_created_during_the_case_is_queued_for_deletion():
    before = {"card-a": CARD_A}
    after = {"card-a": CARD_A, "card-new": {"id": "card-new", "content": []}}
    new_ids, changed = W.cards_to_restore(before, after)
    assert new_ids == ["card-new"]
    assert changed == {}


def test_a_card_whose_content_changed_is_queued_to_write_back_the_prior_dict():
    before = {"card-a": CARD_A}
    learned = {**CARD_A, "content": CARD_A["content"] + [{"id": "y", "text": "learned fact"}]}
    after = {"card-a": learned}
    new_ids, changed = W.cards_to_restore(before, after)
    assert new_ids == []
    assert changed == {"card-a": CARD_A}, "must write back the PRE-case dict, not the learned one"


def test_a_card_deleted_mid_case_is_also_queued_to_write_back():
    before = {"card-a": CARD_A, "card-b": CARD_B}
    after = {"card-a": CARD_A}  # card-b vanished
    new_ids, changed = W.cards_to_restore(before, after)
    assert new_ids == []
    assert changed == {"card-b": CARD_B}


def test_combination_of_new_changed_and_untouched():
    before = {"card-a": CARD_A, "card-b": CARD_B}
    after = {"card-a": {**CARD_A, "content": []}, "card-b": CARD_B,
             "card-new": {"id": "card-new", "content": []}}
    new_ids, changed = W.cards_to_restore(before, after)
    assert new_ids == ["card-new"]
    assert changed == {"card-a": CARD_A}


def test_empty_before_and_after_restores_nothing():
    assert W.cards_to_restore({}, {}) == ([], {})
    assert W.cards_to_restore(None, None) == ([], {})


# --------------------------------------------------------------------------- managed_only_card

MANAGED_CARD = {
    "id": "quest-q1",
    "managed_items": ["quest-state"],
    "content": [
        {"id": "quest-state", "type": "quest", "locator": {"quest_id": "q1"}},
        {"id": "learned-1", "text": "Goals added: ..."},
        {"id": "learned-2", "text": "reschedule this goal rather than ..."},
    ],
}


def test_strips_everything_not_in_managed_items():
    out = W.managed_only_card(MANAGED_CARD)
    assert out["content"] == [MANAGED_CARD["content"][0]]
    assert out is not MANAGED_CARD, "a card that changed must be a new dict, not the input mutated"


def test_a_card_with_only_managed_content_is_returned_unchanged():
    clean = {"id": "quest-q1", "managed_items": ["quest-state"],
             "content": [{"id": "quest-state", "type": "quest"}]}
    out = W.managed_only_card(clean)
    assert out is clean, "nothing to strip: must be the SAME object so a caller can skip the write"


def test_a_card_with_no_managed_items_declared_is_left_alone():
    # Only ever called on a card that declares the managed_items contract (see
    # quest_ai_quest_cards.build_quest_card); one that does not must never be stripped to nothing.
    plain = {"id": "card-x", "content": [{"id": "learned-1", "text": "..."}]}
    out = W.managed_only_card(plain)
    assert out is plain


def test_a_card_with_no_content_list_is_left_alone():
    out = W.managed_only_card({"id": "quest-q1", "managed_items": ["quest-state"]})
    assert out == {"id": "quest-q1", "managed_items": ["quest-state"]}


def test_original_card_dict_is_never_mutated_in_place():
    original_content = list(MANAGED_CARD["content"])
    W.managed_only_card(MANAGED_CARD)
    assert MANAGED_CARD["content"] == original_content


# --------------------------------------------------------------------------- world_quest_card_id

def test_world_quest_card_id_matches_the_backend_convention():
    # Must match quest-backend's quest_ai_quest_cards.card_id_for(quest_id) exactly: a mismatch
    # here means reset_world_quest_cards silently strips nothing, every time.
    assert W.world_quest_card_id("quest_abc123") == "quest-quest_abc123"
