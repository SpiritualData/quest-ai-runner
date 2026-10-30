"""The planner lists the facts it read that bear on the request, and the list travels with the work.

Cause found in a recorded chat miss: a request to read a log and then add a goal that fits what the log
holds read the log (a stored injury and a ten day ban on running were in it), then handed the code
writer a brief that said "work out the weekly distance from the log". The code writer never saw the
constraint, so the proposal it parked asked for more running. The planner is the one reader that has the
newest reads in full beside the request. It now fills a required `facts_read` list (short quotes with
their source) on the call it already makes, and the list is carried: appended to a deep brief (and a
deferred hand-off brief), and shown to the answer next to the planner analysis.

Fully offline, no model calls.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.orchestrator_decide_tool import DECIDE_TOOL, decide_tool_for
from quest_ai_runner.core.orchestrator_decisions import (
    FACTS_ANSWER_HEAD,
    FACTS_BRIEF_HEAD,
    MAX_FACT_CHARS,
    MAX_FACTS_READ,
    normalize_decision,
)
from quest_ai_runner.core.planner_prompt import PLANNER_PROFILES

from .conftest import StubDeepRunner, StubProvider, StubRetrieval

FACT = "Run Log, 2 days ago: sharp knee pain, physio says no running for 10 days"


def norm(raw, **kw):
    return normalize_decision(raw, OrchestratorConfig(), **kw)


def orch(provider, retrieval=None, **kw):
    config = OrchestratorConfig()
    config.overseer = False
    return Orchestrator(retrieval=retrieval or StubRetrieval({"log.md": "GROUNDING " + FACT}),
                        provider=provider, registry=ModelRegistry(provider), config=config, **kw)


def test_the_schema_requires_the_list_in_both_variants_and_both_prompt_profiles_ask_for_it():
    assert "facts_read" in DECIDE_TOOL["input_schema"]["required"]
    compact = decide_tool_for(False, False, compact=True)
    assert "facts_read" in compact["input_schema"]["required"]
    assert compact["input_schema"]["properties"]["facts_read"]["type"] == "array"
    for name in PLANNER_PROFILES:
        assert "ALWAYS set `facts_read`" in PLANNER_PROFILES[name], name


def test_facts_are_cleaned_clipped_deduplicated_and_bounded():
    many = [f"fact {i}" for i in range(20)]
    plan = norm({"action": "answer", "facts_read": ["  a   spaced  fact ", "a spaced fact", "", 7, None,
                                                    "x" * 1000] + many})
    assert plan.facts_read[0] == "a spaced fact"
    assert plan.facts_read.count("a spaced fact") == 1
    assert all(isinstance(f, str) and 0 < len(f) <= MAX_FACT_CHARS for f in plan.facts_read)
    assert len(plan.facts_read) == MAX_FACTS_READ
    assert norm({"action": "answer"}).facts_read == []
    assert norm({"action": "answer", "facts_read": "not a list"}).facts_read == []


def test_a_deep_brief_carries_the_facts_after_the_planners_own_brief():
    plan = norm({"action": "deep", "goal": "g", "deep_brief": "Add a weekly goal for this week",
                 "facts_read": [FACT]})
    assert plan.deep_brief.startswith("Add a weekly goal for this week")
    assert FACTS_BRIEF_HEAD in plan.deep_brief and FACT in plan.deep_brief


def test_a_deep_decision_without_a_brief_gets_the_request_plus_the_facts():
    plan = norm({"action": "deep", "goal": "g", "facts_read": [FACT]}, user_message="add a weekly goal")
    assert plan.deep_brief.startswith("add a weekly goal") and FACT in plan.deep_brief


def test_a_deferred_hand_off_brief_carries_the_facts_too():
    plan = norm({"action": "answer", "facts_read": [FACT],
                 "deferred_deep": {"goal": "add the goal", "brief": "add a weekly goal"}})
    assert plan.deferred_deep["brief"].startswith("add a weekly goal")
    assert FACT in plan.deferred_deep["brief"]
    bare = norm({"action": "answer", "facts_read": [FACT], "deferred_deep": {"goal": "add the goal"}},
                user_message="add a weekly goal")
    assert bare.deferred_deep["brief"].startswith("add a weekly goal") and FACT in bare.deferred_deep["brief"]


def test_nothing_changes_when_no_facts_are_listed():
    plan = norm({"action": "deep", "goal": "g", "deep_brief": "do it",
                 "deferred_deep": None})
    assert plan.deep_brief == "do it"
    kept = norm({"action": "answer", "deferred_deep": {"goal": "g", "brief": "b"}})
    assert kept.deferred_deep["brief"] == "b"


def test_the_deep_runner_receives_the_fact_in_its_brief_end_to_end():
    provider = StubProvider([
        {"action": "read", "reads": [{"rel_path": "log.md"}], "user_intent": "act", "rationale": "read the log",
         "facts_read": []},
        {"action": "deep", "goal": "A weekly goal fitting the log exists", "deep_brief": "Add a weekly goal",
         "user_intent": "act", "rationale": "write it", "facts_read": [FACT]},
        {"met": True, "reason": "done"},
    ])
    runner = StubDeepRunner(met=True, output="done")
    orch(provider, deep_runner=runner).run("Check my log, then add a weekly goal that fits")
    brief = runner.calls[0]["brief"]
    assert "Add a weekly goal" in brief and FACT in brief


def test_the_answer_is_shown_the_facts_the_planner_listed():
    provider = StubProvider([
        {"action": "read", "reads": [{"rel_path": "log.md"}], "user_intent": "ask", "rationale": "read",
         "facts_read": []},
        {"action": "answer", "user_intent": "ask", "rationale": "answering", "pending_read": [],
         "facts_read": [FACT]},
    ])
    orch(provider).run("Anything in my log I should know about?")
    joined = "\n".join(m["content"] for m in provider.last_answer_messages if isinstance(m["content"], str))
    assert FACTS_ANSWER_HEAD in joined and FACT in joined
    quiet = StubProvider([
        {"action": "answer", "user_intent": "ask", "rationale": "answering", "pending_read": [],
         "facts_read": []}])
    orch(quiet).run("hello")
    assert FACTS_ANSWER_HEAD not in "\n".join(
        m["content"] for m in quiet.last_answer_messages if isinstance(m["content"], str))
