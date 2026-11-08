"""The place_on_a_quest metric passes a fixture quest's optional ``vision`` and ``purpose`` to the backend's own ``quest_aim``, so the offline
arms render the very prompt and index production builds. Skipped when no quest-backend checkout is reachable (``QAR_QUEST_BACKEND``)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation" / "components"))

from place_on_a_quest import account, backend_loader, model_pick  # noqa: E402

pytestmark = pytest.mark.skipif(not (backend_loader.ROOT / "app/business/quests/quest_aim.py").is_file(),
                                reason="no quest-backend checkout with quest_aim.py")

QUEST = {"quest_id": "q_eval", "outcome": "Evaluate the assistant", "category": "Research", "state": "Two runs done.",
         "scope": "Use for evaluation runs.", "open": ["Run the safety suite"],
         "vision": "The assistant is evaluated every week and the results are trusted",
         "purpose": "To measure reliability, accuracy and safety."}


def test_fixture_fields_reach_the_backend_prompt_as_their_own_lines():
    pick, _ = backend_loader.backend()
    shown = model_pick.shown(QUEST)
    assert shown["vision"] == QUEST["vision"] and shown["purpose"] == QUEST["purpose"]
    prompt = pick.pick_prompt([shown], [{"text": "check the safety numbers"}], None)
    assert f"\n   vision: {QUEST['vision']}" in prompt
    assert f"\n   purpose: {QUEST['purpose']}" in prompt
    assert "\n   OWNER'S SCOPE (binding): Use for evaluation runs." in prompt


def test_fixture_fields_reach_the_backend_word_index():
    _, candidates = backend_loader.backend()
    idx = backend_loader.quest_index(candidates, [QUEST])[0]
    assert {"outcome", "purpose", "scope context"} <= {label for label, _text, _tokens in idx.snippets}
    assert "reliability" in idx.terms


def test_a_quest_without_the_fields_reads_as_before():
    plain = {k: v for k, v in QUEST.items() if k not in ("vision", "purpose")}
    assert backend_loader.quest_aim(plain) == {"vision": plain["outcome"], "purpose": ""}
    assert "purpose" not in account.embedding_text(plain) and "reliability" not in account.card_text(plain)
    assert "reliability" in account.embedding_text(QUEST) and "reliability" in account.card_text(QUEST)
