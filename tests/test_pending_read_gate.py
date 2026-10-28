"""An `answer` that declares reads it still owes runs them first (found live 2026-10-16).

A planner listed a folder, saw the file that holds the fact, and chose "answer" with a reply that
promised to open it: the turn ended without the file ever being read. The planner's own structured
`pending_read` field declares what it still owes, and the loop honors it (no word list over the
planner's prose; hard rule #3). Bounded, and a read that already ran is never repeated.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.orchestrator_decide_tool import DECIDE_TOOL
from quest_ai_runner.core.orchestrator_decisions import normalize_decision

from .conftest import StubProvider, StubRetrieval

FILES = {"a/README.md": "GROUNDING the service listens on port 8096", "a/other.md": "GROUNDING other"}
LISTED = {"action": "read", "reads": [{"rel_path": "a/other.md"}], "user_intent": "ask",
          "rationale": "look around first"}


def orch(provider, retrieval, **cfg):
    config = OrchestratorConfig(**cfg)
    config.overseer = False
    return Orchestrator(retrieval=retrieval, provider=provider, registry=ModelRegistry(provider),
                        config=config)


def answer(pending):
    return {"action": "answer", "user_intent": "ask", "rationale": "answering",
            "pending_read": pending}


def test_a_declared_pending_read_runs_before_the_answer():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([LISTED, answer([{"rel_path": "a/README.md"}]), answer([])])
    res = orch(provider, retrieval).run("Which port does it listen on?")
    assert "a/README.md" in retrieval.read_calls
    assert res.kind == "answer"


def test_an_empty_pending_read_answers_at_once():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([LISTED, answer([])])
    orch(provider, retrieval).run("Which port does it listen on?")
    assert retrieval.read_calls == ["a/other.md"]


def test_a_read_that_already_ran_is_not_run_again():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([LISTED, answer([{"rel_path": "a/other.md"}])])
    orch(provider, retrieval).run("Which port does it listen on?")
    assert retrieval.read_calls == ["a/other.md"]


def test_a_planner_that_keeps_declaring_reads_still_ends_in_an_answer():
    retrieval = StubRetrieval({f"f{i}.md": "GROUNDING" for i in range(10)})
    decisions = [LISTED] + [answer([{"rel_path": f"f{i}.md"}]) for i in range(10)]
    provider = StubProvider(decisions)
    res = orch(provider, retrieval).run("Which port does it listen on?")
    assert res.kind == "answer"
    # LISTED's own read plus at most two pending-read swaps.
    assert len(retrieval.read_calls) <= 3


def test_pending_read_is_cleaned_like_reads_and_required_by_the_schema():
    plan = normalize_decision({"action": "answer", "pending_read": [{"rel_path": "x.md"}, "junk", {}]},
                              OrchestratorConfig())
    assert plan.pending_read == [{"rel_path": "x.md"}]
    assert normalize_decision({"action": "answer"}, OrchestratorConfig()).pending_read == []
    assert "pending_read" in DECIDE_TOOL["input_schema"]["required"]


def test_the_same_read_failing_twice_is_not_issued_a_third_time():
    retrieval = StubRetrieval(FILES)
    missing = {"action": "read", "reads": [{"rel_path": "a/nope.md"}], "user_intent": "ask",
               "rationale": "try it"}
    provider = StubProvider([missing, missing, missing, answer([])])
    orch(provider, retrieval).run("Which port does it listen on?")
    assert retrieval.read_calls.count("a/nope.md") == 2


def test_a_read_step_with_its_specs_in_pending_read_reads_them():
    """A cheap planner chose "read" but wrote the web query into `pending_read`: it ran nothing."""
    raw = {"action": "read", "user_intent": "ask", "rationale": "check",
           "pending_read": [{"web": "an event date 2026"}]}
    plan = normalize_decision(raw, OrchestratorConfig(), web_enabled=True)
    assert plan.reads == [{"web": "an event date 2026"}]
    assert plan.pending_read == []
    # An answer keeps its pending reads for the gate, and a real `reads` list is left alone.
    kept = normalize_decision({**raw, "action": "answer"}, OrchestratorConfig(), web_enabled=True)
    assert kept.reads == [] and kept.pending_read == [{"web": "an event date 2026"}]
    own = normalize_decision({**raw, "reads": [{"rel_path": "x.md"}]}, OrchestratorConfig(),
                             web_enabled=True)
    assert own.reads == [{"rel_path": "x.md"}]
