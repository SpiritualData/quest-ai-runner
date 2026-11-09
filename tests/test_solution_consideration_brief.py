"""QAR's wiring of the hub's per-solution consideration for work: the deep brief block over a records root (gated and capped), Quest-held
records as rows, and a failure that is said, with a scripted provider (no model call)."""
import json
import logging
from pathlib import Path

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import solution_consideration

HUB = Path(__file__).resolve().parents[2] / "ai-hub-of-hubs-structure"
GRAPHIFY = "Switch Cards From Files to the Graphify arm."


class Scripted:
    """Answers every pass in the shape it asks for; counts the calls."""

    def __init__(self, combination=False):
        self.calls, self.combination = 0, combination

    def answer(self, messages, model=None, **kwargs):
        self.calls += 1
        prompt = messages[-1]["content"]
        if "Its children's own passes found" in prompt:
            return json.dumps({"combination_effect": self.combination, "why": "no effect"})
        if "This solution relies on" in prompt:
            return json.dumps({"still_holds": True, "why": "unchanged"})
        return json.dumps({"applies": True, "needs": "a measured comparison", "target": {"metric_id": "metric-x", "value": "1", "done_when": "measured"},
                           "conflicts": [{"record": "results", "conflict": "it already lost"}]})


def test_the_brief_carries_each_touched_solutions_prompt_and_its_finding(monkeypatch):
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    provider = Scripted()
    block = solution_consideration.brief_block(GRAPHIFY, str(HUB), provider)
    assert "### Cards From Files (cards-from-files)" in block
    assert "graphify_files: passed 3 of 63" in block and "Conflict with its records: results: it already lost" in block


def test_a_goal_no_solution_concerns_is_gated_and_makes_no_model_call(monkeypatch):
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    provider = Scripted()
    assert solution_consideration.brief_block("thanks, that is all for today", str(HUB), provider) == "" and provider.calls == 0


def test_the_cap_is_one_pass_per_selected_solution_and_one_parent_pass(monkeypatch):
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    provider = Scripted(combination=True)       # every scope above says it is affected, so only the cap stops the climb
    chosen = solution_consideration.chosen_for(GRAPHIFY, str(HUB))
    solution_consideration.brief_block(GRAPHIFY, str(HUB), provider)
    assert chosen and provider.calls == len(chosen) + 1


def test_no_records_or_no_provider_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("QAR_SPEC_ROOT", raising=False)
    assert solution_consideration.brief_block("anything", str(tmp_path), Scripted()) == ""
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    assert solution_consideration.brief_block("anything", str(HUB), None) == ""


def test_quest_held_records_form_the_network_without_a_folder():
    tables = {"solutions": {"weekly-video": {"name": "Weekly Video", "purpose": "Publish one video each week.", "metric_ids": ["m1"],
                                             "knowledge": "[lesson] Short titles did better (source: last month's results)"}},
              "metrics": {"m1": {"name": "Videos per week", "solution_ids": ["weekly-video"], "purpose": "Count videos."}}}
    network = solution_consideration.quest_network("q1", tables)
    scripted = Scripted()
    found = solution_consideration.consider("publish the weekly video", network, lambda prompt: scripted.answer([{"role": "user", "content": prompt}]))
    assert found["briefs"]["solutions"][0]["solution_id"] == "weekly-video"
    assert "Short titles did better" in found["briefs"]["solutions"][0]["prompt"]


def test_a_failing_consideration_is_said_once_as_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("QAR_SPEC_ROOT", str(HUB))
    monkeypatch.setattr(solution_consideration, "FAILURES", {"count": 0, "said": False})

    def broken(*args, **kwargs):
        raise ImportError("no solution consideration in this esoi")
    monkeypatch.setattr(solution_consideration, "chosen_for", broken)
    with caplog.at_level(logging.INFO, logger="quest-ai-runner.solution-consideration"):
        assert solution_consideration.brief_block(GRAPHIFY, str(HUB), Scripted()) == ""
        assert solution_consideration.brief_block(GRAPHIFY, str(HUB), Scripted()) == ""
    assert solution_consideration.FAILURES["count"] == 2
    assert sum(1 for r in caplog.records if r.levelname == "WARNING" and "Solution consideration failed" in r.getMessage()) == 1
