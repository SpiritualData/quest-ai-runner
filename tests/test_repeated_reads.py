"""A read the turn already ran is not run again (found in live turns, 2026-10-06).

A cheap planner re-issued the SAME read spec up to five times in one turn (the same heading of the
same doc, the same card search), each time paying for the read and another planning step, and the
turn ended on whatever was left of its budget. The generic fix is structural, on the planner's own
structured read specs (never on words in its output, hard rule #3):

  * an identical spec (same keys and values, any order) that already ran this turn is not executed
    again; the planner is told, as a gathered observation, that it ran and where its result is;
  * a second step whose reads were ALL repeats ends the read loop the way a spent read budget
    does (a best-effort answer from what was gathered, or the hand-off the wrap-up chooses),
    because no new information can arrive from it; the note itself is never answer content;
  * a tool step resets the record, since after a write the same read can return something new.

Fully offline: scripted planner decisions and an in-memory retrieval stub.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    read_spec_key,
    split_repeated_reads,
)

from .conftest import StubProvider, StubRetrieval


def build(decisions: List[Dict[str, Any]], **cfg: Any):
    provider = StubProvider(decisions)
    retrieval = StubRetrieval({"notes.md": "GROUNDING the notes", "plan.md": "GROUNDING the plan"})
    config = OrchestratorConfig(max_steps=6, **cfg)
    config.overseer = False
    orch = Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), config=config)
    return orch, provider, retrieval


READ_NOTES = {"action": "read", "reads": [{"rel_path": "notes.md", "heading": "Plan"}],
              "rationale": "read the notes"}
READ_NOTES_REORDERED = {"action": "read", "reads": [{"heading": "Plan", "rel_path": "notes.md"}],
                        "rationale": "read the notes again"}


def test_key_ignores_key_order_and_split_runs_a_spec_once():
    a = {"rel_path": "x.md", "heading": "H"}
    b = {"heading": "H", "rel_path": "x.md"}
    assert read_spec_key(a) == read_spec_key(b)
    fresh, repeated = split_repeated_reads([a, b, {"rel_path": "y.md"}], {})
    assert fresh == [a, {"rel_path": "y.md"}] and repeated == []
    fresh, repeated = split_repeated_reads([b, {"rel_path": "z.md"}], {read_spec_key(a): 2})
    assert fresh == [{"rel_path": "z.md"}] and repeated == [(b, 2)]


def test_a_repeated_read_is_not_run_again_and_the_planner_is_told():
    orch, provider, retrieval = build([READ_NOTES, READ_NOTES_REORDERED,
                                       {"action": "answer", "rationale": "have it"}])
    res = orch.run("what is the plan in my notes?")
    assert retrieval.read_calls == ["notes.md"]
    told = [p for p in provider.plan_prompts if "NOT RUN AGAIN" in p]
    assert told, "the re-plan after a repeat must see that it was not run again"
    assert res.kind == "answer"


def test_a_second_all_repeat_step_answers_from_what_was_gathered():
    orch, provider, retrieval = build([READ_NOTES, READ_NOTES, READ_NOTES, READ_NOTES, READ_NOTES])
    res = orch.run("what is the plan in my notes?")
    assert retrieval.read_calls == ["notes.md"]
    assert res.kind == "answer"
    answer_text = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "GROUNDING" in answer_text
    assert "NOT RUN AGAIN" not in answer_text, "the note is for the planner, never answer content"
    assert res.partial is True and res.exit_reason == "read_budget"


def test_repeat_notes_alone_are_not_something_gathered():
    """A turn whose only read returned nothing, then repeated, has gathered NOTHING: it takes the
    nothing-gathered path (a hand-off when one is available), not a best-effort answer on a note."""
    class EmptyRetrieval(StubRetrieval):
        def read_section(self, rel_path, **kw):
            self.read_calls.append(rel_path)
            return None
    provider = StubProvider([READ_NOTES, READ_NOTES, READ_NOTES])
    config = OrchestratorConfig(max_steps=6)
    config.overseer = False
    orch = Orchestrator(retrieval=EmptyRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=config)
    res = orch.run("what is the plan in my notes?")
    assert res.exit_reason != "read_budget"


def test_a_new_read_alongside_a_repeat_still_runs():
    mixed = {"action": "read", "reads": [{"rel_path": "notes.md", "heading": "Plan"},
                                         {"rel_path": "plan.md"}], "rationale": "more"}
    orch, provider, retrieval = build([READ_NOTES, mixed, {"action": "answer", "rationale": "ok"}])
    orch.run("what is the plan in my notes?")
    assert retrieval.read_calls == ["notes.md", "plan.md"]


def test_a_failed_read_stays_retryable():
    class FlakyRetrieval(StubRetrieval):
        def read_section(self, rel_path, **kw):
            self.read_calls.append(rel_path)
            if len(self.read_calls) == 1:
                from quest_ai_runner.core.adapters import Observation
                return Observation(kind="error", rel_path=rel_path, error="timed out")
            return super().read_section(rel_path, **kw)
    provider = StubProvider([READ_NOTES, READ_NOTES, {"action": "answer", "rationale": "ok"}])
    config = OrchestratorConfig(max_steps=6)
    config.overseer = False
    retrieval = FlakyRetrieval({"notes.md": "GROUNDING the notes"})
    Orchestrator(retrieval=retrieval, provider=provider, registry=ModelRegistry(provider),
                 config=config).run("what is the plan in my notes?")
    # The failed first read ran again (a later read may follow from the answer path's own gate).
    assert retrieval.read_calls[:2] == ["notes.md", "notes.md"]


def test_a_read_the_planner_now_sees_only_as_a_summary_may_run_again():
    """Once older reads are compressed to one line in the planner's view, refusing a re-read
    would leave the planner unable to see that result in full."""
    others = [{"action": "read", "reads": [{"rel_path": f"f{i}.md"}], "rationale": "more"}
              for i in range(3)]
    files = {"notes.md": "GROUNDING the notes", **{f"f{i}.md": f"file {i}" for i in range(3)}}
    provider = StubProvider([READ_NOTES] + others + [READ_NOTES,
                            {"action": "answer", "rationale": "ok"}])
    config = OrchestratorConfig(max_steps=8, planner_recent_full=2, planner_compress_over=3)
    config.overseer = False
    retrieval = StubRetrieval(files)
    Orchestrator(retrieval=retrieval, provider=provider, registry=ModelRegistry(provider),
                 config=config).run("what is the plan in my notes?")
    assert retrieval.read_calls.count("notes.md") == 2


def test_repeat_only_steps_must_be_consecutive():
    """Two repeats separated by a step that read something new do not end the loop."""
    other = {"action": "read", "reads": [{"rel_path": "plan.md"}], "rationale": "other"}
    orch, provider, retrieval = build([READ_NOTES, READ_NOTES, other, READ_NOTES,
                                       {"action": "answer", "rationale": "done"}])
    res = orch.run("what is the plan in my notes?")
    assert res.exit_reason != "read_budget"
    assert retrieval.read_calls == ["notes.md", "plan.md"]


def test_notes_are_not_in_the_returned_gathered():
    orch, provider, retrieval = build([READ_NOTES, READ_NOTES,
                                       {"action": "answer", "rationale": "have it"}])
    res = orch.run("what is the plan in my notes?")
    assert res.gathered and not any(o.get("planner_only") for o in res.gathered)


def test_a_planner_note_on_an_observation_reaches_the_planner_only():
    from quest_ai_runner.core.adapters import Observation

    class NotingRetrieval(StubRetrieval):
        def query(self, spec):
            return Observation(kind="query", text="GROUNDING loose hits",
                               planner_note="Not answered by: db: needs an operation")
    provider = StubProvider([{"action": "read", "reads": [{"query": {"kind": "x"}}],
                              "rationale": "look"},
                             {"action": "answer", "rationale": "ok"}])
    config = OrchestratorConfig(max_steps=4)
    config.overseer = False
    res = Orchestrator(retrieval=NotingRetrieval({}), provider=provider,
                       registry=ModelRegistry(provider), config=config).run("q")
    assert any("Not answered by: db" in p for p in provider.plan_prompts)
    answer_text = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "Not answered by" not in answer_text
    assert not any(o.get("planner_only") for o in res.gathered)
