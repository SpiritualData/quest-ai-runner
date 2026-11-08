"""QAR's wiring of the hub's per-solution consideration: the deep brief block over a records root, with a scripted provider (no model call)."""
import json
from pathlib import Path

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import solution_consideration

HUB = Path(__file__).resolve().parents[2] / "ai-hub-of-hubs-structure"


class Scripted:
    """Answers every pass in the shape it asks for; counts the calls."""

    def __init__(self):
        self.calls = 0

    def answer(self, messages, model=None, **kwargs):
        self.calls += 1
        prompt = messages[-1]["content"]
        if "Its children's own passes found" in prompt:
            return json.dumps({"combination_effect": False, "why": "no effect"})
        if "This solution relies on" in prompt:
            return json.dumps({"still_holds": True, "why": "unchanged"})
        return json.dumps({"applies": True, "needs": "a measured comparison", "target": {"metric_id": "metric-x", "value": "1", "done_when": "measured"},
                           "conflicts": [{"record": "results", "conflict": "it already lost"}]})


def test_the_brief_carries_each_touched_solutions_prompt_and_its_finding(monkeypatch):
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    provider = Scripted()
    block = solution_consideration.brief_block("Should Cards From Files switch to the Graphify arm?", str(HUB), provider)
    assert "### Cards From Files (cards-from-files)" in block
    assert "graphify_files: passed 3 of 63" in block and "Conflict with its records: results: it already lost" in block
    assert "### The scopes above" in block and provider.calls >= 2


def test_no_records_or_no_provider_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("QAR_SPEC_ROOT", raising=False)
    assert solution_consideration.brief_block("anything", str(tmp_path), Scripted()) == ""
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    assert solution_consideration.brief_block("anything", str(HUB), None) == ""


class Store:
    def __init__(self):
        self.cards = {}

    def write_card(self, card_id, card):
        self.cards[card_id] = card
        return True

    def get_card(self, card_id):
        return self.cards.get(card_id)


class Orch:
    def __init__(self, store, provider):
        self.context_assembler, self.provider = store, provider


def test_a_chat_turn_writes_the_touched_solutions_cards_and_puts_them_first(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    monkeypatch.setenv("QAR_SOLUTION_CONSIDERATION_SECONDS", "60")
    store, meta = Store(), {"priority_card_ids": ["quest-folder-q1"]}
    rs = SimpleNamespace(cue_resolved=None, message_is_user_turn=True, goal_condition="Should Cards From Files switch to the Graphify arm?")
    solution_consideration.for_turn(Orch(store, Scripted()), rs, meta)
    assert "solution-cards-from-files" in store.cards and meta["priority_card_ids"][0] == "quest-folder-q1"
    assert "solution-cards-from-files" in meta["priority_card_ids"]
    assert "graphify_files: passed 3 of 63" in store.cards["solution-cards-from-files"]["content"][0]["locator"]["text"]


def test_a_milestone_turn_is_left_to_its_own_step(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    store, meta = Store(), {}
    solution_consideration.for_turn(Orch(store, Scripted()), SimpleNamespace(cue_resolved=object(), message_is_user_turn=True), meta)
    assert store.cards == {} and "priority_card_ids" not in meta
