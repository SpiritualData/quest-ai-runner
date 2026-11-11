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


def test_the_review_s_generic_synthesis_lines_never_become_the_findings():
    from quest_ai_runner.the_world.science.investigate import research_note
    record = {**RECORD, "synthesis": {"known": ["Supported by a graded source: a.com, b.com"], "unknown": ["Not established: licence of a.com", "licence of b.com", "a validated minimum"]}}
    note = research_note(record)
    assert "Supported by a graded source" not in note and "licence" not in note and "Not established: a validated minimum" in note
    assert "Ten to twenty minutes a day" in note


def test_the_same_finding_from_several_sites_is_one_finding_and_the_reply_is_composed_from_the_note():
    from quest_ai_runner.the_world.science.investigate import research_note, research_reply
    items = [{"claim": "Five minutes a day is enough to start", "grade": "C", "source": f"https://s{n}.example/a", "verified": "snippet"} for n in range(3)]
    note = research_note({**RECORD, "items": items + RECORD["items"]})
    assert note.count("Five minutes a day is enough to start") == 1
    reply = research_reply(note)
    assert reply.startswith("I looked into this and saved a note on the quest: How long does daily qigong need") and "How sure: not very sure." in reply
    assert "Not established: a validated minimum number of minutes" in reply and "—" not in reply
    assert research_reply("a hand-written note") == ""


def test_a_research_turn_with_its_note_saved_and_a_reply_composed_from_it_ends_verified_when_nothing_is_unbacked():
    saved = []
    record = {"question": "How long?", "best_grade": "C", "queries": [{}], "synthesis": {"known": [], "unknown": []},
              "items": [{"claim": "Ten minutes a day is the usual advice", "grade": "C", "source": "https://example.org/a", "verified": "opened"}]}
    from quest_ai_runner.the_world.science.investigate import research_note
    investigate = ToolSpec(name="investigate_for_quest", handler=lambda a, c: ToolResult(ok=True, data={**record, "note_text": research_note(record)}, text="Looked into: How long?"),
                           description="d", when_to_use="w", parameters={"type": "object"}, mutates=True)
    note = ToolSpec(name="add_quest_note", handler=lambda a, c: (saved.append(a["text"]), ToolResult(ok=True, text="Added a note to quest q1."))[1],
                    description="d", when_to_use="w", parameters={"type": "object", "properties": {"text": {"type": "string"}}}, mutates=True)

    class Picky(ClaimingProvider):
        def plan(self, prompt, *, model, tool_schema):
            if tool_schema.get("name") == "goal_verdict":
                return {"met": False, "reason": "wants more", "next_action": "", "claims_unexecuted": False, "claims_unsupported": False, "blocker": "more_work"}
            return StubProvider.plan(self, prompt, model=model, tool_schema=tool_schema)
    provider = Picky(decisions=[
        {"action": "tool", "user_intent": "act", "rationale": "go", "tool_calls": [{"name": "investigate_for_quest", "args": {"question": "q"}}, {"name": "add_quest_note", "args": {"text": "x"}}]},
        {"action": "answer", "user_intent": "act", "rationale": "done"},
    ])
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.verify_claims = True
    cfg.answer_goal_max_iterations = 3
    result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=cfg, tools=ToolRegistry([investigate, note])).run(
        "Look into how long, then add a note.", quest_id="q1")
    assert saved and saved[0].startswith("Research note: How long?")
    assert result.text.startswith("I looked into this and saved a note on the quest: How long.")
    assert result.exit_reason == "verified"
