"""The place_on_a_quest metric supports cases that carry earlier placed items (``examples``): they reach the backend's prompt after the quests
by quest number, and the example-vote arm fuses their vote with the hybrid order. Skipped when no quest-backend checkout is reachable
(``QAR_QUEST_BACKEND``)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation" / "components"))

from place_on_a_quest import account, backend_loader, model_pick  # noqa: E402

pytestmark = pytest.mark.skipif(not (backend_loader.ROOT / "app/business/quests/quest_examples.py").is_file(),
                                reason="no quest-backend checkout with quest_examples.py")


def fake_embed(texts):
    """One axis per topic word, so similarity is shared topic."""
    words = ["poster", "garden", "grant"]
    return np.array([[1.0 if w in t.lower() else 0.0 for w in words] + [0.01] for t in texts])


def test_examples_reach_the_prompt_after_the_quests_by_number():
    pick, _ = backend_loader.backend()
    shown = [model_pick.shown(q) for q in account.CORE[:3]]
    examples = [{"quest_id": account.CORE[2]["quest_id"], "text": "Order posters for the fair"}]
    prompt = pick.pick_prompt(shown, [{"text": "print the poster", "previous": "", "examples": examples}], None)
    assert prompt.index("--- QUESTS ---") < prompt.index('- "Order posters for the fair" -> 3') < prompt.index("--- MESSAGES ---")


def test_the_example_vote_arm_votes_the_closest_examples_and_leaves_exampleless_cases_alone():
    from place_on_a_quest.impls import example_vote_ranker as arm
    case = {"message": "print the poster", "previous": "", "examples": [
        {"quest_id": "q_a", "text": "poster for the fair"}, {"quest_id": "q_a", "text": "another poster idea"},
        {"quest_id": "q_b", "text": "garden grant budget"}]}
    found = arm.similar_examples(case, fake_embed)
    assert found[0]["quest_id"] == "q_a" and found[0]["score"] > found[-1]["score"]
    assert arm.similar_examples({"message": "x", "previous": ""}, fake_embed) == []
    backend = backend_loader.examples()
    assert backend.vote_order(backend.example_vote(found))[0] == "q_a"
