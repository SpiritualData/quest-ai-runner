"""A reply never states an action happened without its receipt in this turn.

The failing case (real dev turn): the investigation tool ran and succeeded, the planner never called the note tool, and the reply said a note was added. The verifier's structured
``claims_unexecuted`` finds it; a tool succeeding earlier made the deep re-run unsafe and the rewrite attempts ran out, so the false claim shipped. The reply is now written from
the receipts.
"""
from typing import Any, Dict

from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec

from .conftest import StubProvider, StubRetrieval

CLAIM = "I added a note to your quest with what I found on daily qigong and focus."


class ClaimingProvider(StubProvider):
    def answer(self, messages, *, model, system=None) -> str:
        super().answer(messages, model=model, system=system)
        return CLAIM

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "goal_verdict":
            return {"met": False, "reason": "claims a note with no receipt", "next_action": "", "claims_unexecuted": True, "blocker": "more_work"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def test_a_reply_claiming_a_note_the_record_does_not_show_is_replaced_by_the_receipts():
    investigate = ToolSpec(name="investigate_for_quest", handler=lambda a, c: ToolResult(ok=True, text="Looked into: how long daily qigong takes. Found: short sessions."),
                           description="d", when_to_use="w", parameters={"type": "object"}, mutates=True)
    provider = ClaimingProvider(decisions=[
        {"action": "tool", "user_intent": "act", "rationale": "research", "tool_calls": [{"name": "investigate_for_quest", "args": {"question": "q", "informs": "person_asked_for_research"}}]},
        {"action": "answer", "user_intent": "act", "rationale": "done"},
    ])
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.verify_claims = True
    cfg.answer_goal_max_iterations = 2
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=cfg, tools=ToolRegistry([investigate]))
    result = orch.run("Look into daily qigong, then add a short note to this quest.", quest_id="q1")
    assert "added a note" not in result.text.lower()
    assert result.text.startswith("Done this turn: Looked into: how long daily qigong takes.")
    assert "not carried out" in result.text and "—" not in result.text
    assert result.exit_reason == "max_turns"


RECORD = {"question": "How long does daily qigong need to be to notice a change in focus?", "best_grade": "C",
          "items": [{"claim": "Ten to twenty minutes a day is the usual advice for beginners", "grade": "C", "source": "https://www.nccih.nih.gov/health/tai-chi", "verified": "opened"},
                    {"claim": "Effects build over several weeks of regular practice", "grade": "C", "source": "https://learnqigong.com/x", "verified": "snippet"}],
          "synthesis": {"known": [], "unknown": ["a validated minimum number of minutes"]}}


def test_the_research_note_is_composed_from_the_record_and_is_short_and_readable():
    from quest_ai_runner.the_world.science.investigate import research_note
    from quest_ai_runner.the_world.tools.placeholder_guard import placeholder_reason
    note = research_note(RECORD)
    assert note.startswith("Research note: How long does daily qigong need to be to notice a change in focus?")
    assert "How sure: not very sure." in note and "(not very sure; nccih.nih.gov)" in note and "Not established: a validated minimum number of minutes" in note
    assert "1 of 2 findings come from pages that were opened" in note and "—" not in note and len(note) < 800
    assert placeholder_reason(note) is None
    assert research_note({"question": "q", "items": [], "synthesis": {}}) == ""


def test_a_note_the_planner_writes_after_researching_is_replaced_by_the_composed_one():
    saved = []
    investigate = ToolSpec(name="investigate_for_quest", handler=lambda a, c: ToolResult(ok=True, data={"note_text": "Research note: composed from the record"}, text="Looked into: x."),
                           description="d", when_to_use="w", parameters={"type": "object"}, mutates=True)
    note = ToolSpec(name="add_quest_note", handler=lambda a, c: (saved.append(a["text"]), ToolResult(ok=True, text="Added a note to quest q1."))[1],
                    description="d", when_to_use="w", parameters={"type": "object", "properties": {"text": {"type": "string"}}}, mutates=True)
    provider = StubProvider(decisions=[
        {"action": "tool", "user_intent": "act", "rationale": "both", "tool_calls": [
            {"name": "add_quest_note", "args": {"text": "Research note (pending): findings to be added"}},
            {"name": "investigate_for_quest", "args": {"question": "q", "informs": "person_asked_for_research"}}]},
        {"action": "answer", "user_intent": "act", "rationale": "done"},
    ])
    cfg = OrchestratorConfig()
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=cfg, tools=ToolRegistry([investigate, note]))
    orch.run("Look into daily qigong, then add a short note to this quest.", quest_id="q1")
    assert saved == ["Research note: composed from the record"]
