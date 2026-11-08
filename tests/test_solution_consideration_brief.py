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
