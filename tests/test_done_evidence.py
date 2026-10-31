"""Finish the job before reporting: work shape and done evidence from the planner, the verifier reading the run's
declared coverage, the structural continue or stop on a coverage gap, the pre-flight plan check for an analysis only,
and the interim event. Fully offline: scripted providers, no network, no model.

Run: .venv/bin/python -m pytest tests/test_done_evidence.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from hubofhubs.ports.deep import DeepResult
from hubofhubs.ports.events import EVENT_INTERIM
from quest_ai_runner.core.adapters_sinks import StreamSink
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.orchestrator_decide_tool import decide_tool_for
from quest_ai_runner.core.orchestrator_decisions import normalize_decision
from quest_ai_runner.core.planner_prompt import PLANNER_PROFILES
from quest_ai_runner.core.prompt_budget import estimate_tokens

from .conftest import StubRetrieval

ANALYSIS = {"action": "deep", "goal": "Answer from every entry", "rationale": "deep",
            "deep_subtasks": [{"goal": "Answer from every entry", "brief": "analyse the log"}],
            "work_shape": "analysis", "done_evidence": ["covers all 344 entries", "method stated"]}
LOOKUP = {"action": "answer", "rationale": "a count", "work_shape": "lookup", "done_evidence": ["ignored"]}


class Provider:
    """Replays planner decisions, verdicts and pre-flight verdicts, told apart by tool schema name; records every call."""

    def __init__(self, plans, verdicts=(), preflights=()):
        self.plans, self.verdicts, self.preflights = list(plans), list(verdicts), list(preflights)
        self.calls: List[str] = []
        self.prompts: Dict[str, List[str]] = {}

    def plan(self, prompt, *, model, tool_schema):  # noqa: ARG002
        name = tool_schema.get("name")
        self.calls.append(name)
        self.prompts.setdefault(name, []).append(prompt)
        if name == "decide":
            return self.plans.pop(0) if self.plans else {"action": "answer", "rationale": "done"}
        if name == "goal_verdict":
            return self.verdicts.pop(0) if self.verdicts else {"met": True}
        if name == "plan_sufficiency":
            return self.preflights.pop(0) if self.preflights else {"sufficient": True}
        return {"signal": "proceed", "reason": ""}

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        return "ANSWER"

    def list_models(self):
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class Runner:
    """A deep runner that returns scripted results and keeps every brief it was handed."""

    def __init__(self, results):
        self.results, self.briefs = list(results), []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None):  # noqa: ARG002
        self.briefs.append(brief)
        return self.results.pop(0) if self.results else DeepResult(met=True, output="done")


def cov(processed, total=344, **more):
    return {"processed": processed, "total": total, "unit": "entries", "next_skip": processed, **more}


def build(provider, runner=None, **cfg):
    cfg.setdefault("deep_goal_max_iterations", 4)
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=runner, config=OrchestratorConfig(**cfg))


def run_with_events(orch, message="analyse every entry"):
    events: List[Dict[str, Any]] = []
    result = orch.run(message, sink=StreamSink(events.append))
    return result, events


# ---- (1) the planner fields

def test_the_fields_exist_only_when_opted_in_and_the_off_schema_is_unchanged():
    off = decide_tool_for(False, False)
    assert "work_shape" not in off["input_schema"]["properties"]
    assert off == decide_tool_for(False, False, done_evidence=False)
    on = decide_tool_for(False, False, done_evidence=True)
    assert on["input_schema"]["properties"]["work_shape"]["enum"] == ["lookup", "action", "analysis", "conversation", None]
    assert "done_evidence" in on["input_schema"]["properties"]
    assert "work_shape" not in on["input_schema"]["required"]
    compact = decide_tool_for(False, False, compact=True, done_evidence=True)
    assert compact["input_schema"]["properties"]["done_evidence"].get("description")


def test_normalize_keeps_evidence_for_an_analysis_only_and_only_when_opted_in():
    on = OrchestratorConfig(done_evidence_enabled=True)
    d = normalize_decision(dict(ANALYSIS, work_shape=" Analysis "), on)
    assert d.work_shape == "analysis" and d.done_evidence == ["covers all 344 entries", "method stated"]
    d = normalize_decision(LOOKUP, on)
    assert d.work_shape == "lookup" and d.done_evidence == []
    assert normalize_decision(dict(ANALYSIS, work_shape="poem"), on).work_shape is None
    off = normalize_decision(ANALYSIS, OrchestratorConfig())
    assert off.work_shape is None and off.done_evidence == []


def test_planner_tokens_do_not_grow_in_the_full_profile_and_stay_small_in_the_compact(capsys):
    def tokens(profile, flag):
        schema = json.dumps(decide_tool_for(False, False, compact=(profile == "compact"), done_evidence=flag))
        return estimate_tokens(PLANNER_PROFILES[profile]) + estimate_tokens(schema)
    full_off, full_on = tokens("full", False), tokens("full", True)
    compact_off, compact_on = tokens("compact", False), tokens("compact", True)
    print(f"planner tokens full {full_off} -> {full_on}, compact {compact_off} -> {compact_on}")
    assert full_on <= full_off
    assert compact_on - compact_off <= 130


def test_a_lookup_adds_no_call_and_no_prompt_text():
    plain = Provider([dict(LOOKUP)])
    build(plain).run("how many entries?")
    flagged = Provider([dict(LOOKUP)])
    build(flagged, done_evidence_enabled=True, consumer_limits="reads page at 200 rows").run("how many entries?")
    assert flagged.calls == plain.calls and "plan_sufficiency" not in flagged.calls
    assert flagged.prompts["decide"] == plain.prompts["decide"]


# ---- (2) the verifier and the structural coverage gate

def test_the_verifier_is_shown_the_requirements_and_the_declared_coverage_and_nothing_extra_otherwise():
    provider = Provider([dict(ANALYSIS)])
    runner = Runner([DeepResult(met=True, output="found it", coverage=cov(344))])
    build(provider, runner, done_evidence_enabled=True).run("analyse every entry")
    prompt = provider.prompts["goal_verdict"][0]
    assert "DONE EVIDENCE" in prompt and "R1. covers all 344 entries" in prompt
    assert "Coverage: 344 of 344 entries processed." in prompt
    bare = Provider([{"action": "deep", "goal": "g", "rationale": "r"}])
    build(bare, Runner([DeepResult(met=True, output="ok")])).run("do it")
    assert "DONE EVIDENCE" not in bare.prompts["goal_verdict"][0]


def test_partial_coverage_is_not_met_even_when_the_verifier_says_met_and_the_run_continues_to_the_end():
    provider = Provider([dict(ANALYSIS)], verdicts=[{"met": True}, {"met": True}])
    runner = Runner([DeepResult(met=True, output="first 200", coverage=cov(200)),
                     DeepResult(met=True, output="all of it", coverage=cov(344))])
    result = build(provider, runner).run("analyse every entry")
    assert len(runner.briefs) == 2 and "continue from entries 201" in runner.briefs[1]
    final = result.deep_results[0]
    assert final.met is True
    assert final.output.endswith("Coverage: 344 of 344 entries processed.")


def test_a_run_that_stops_advancing_ends_honestly_partial_and_states_how_to_continue():
    provider = Provider([dict(ANALYSIS)])
    runner = Runner([DeepResult(met=True, output="a", coverage=cov(200)),
                     DeepResult(met=True, output="b", coverage=cov(200))])
    result = build(provider, runner).run("analyse every entry")
    final = result.deep_results[0]
    assert len(runner.briefs) == 2 and final.met is False
    assert "Partial: Coverage: 200 of 344" in final.error
    assert "Coverage: 200 of 344 entries processed (144 not yet covered)." in final.output
    assert "continue from entries 201" in final.output and "no_progress" not in final.output


def test_a_spent_attempt_budget_stops_with_the_gap_and_never_claims_met():
    provider = Provider([dict(ANALYSIS)])
    runner = Runner([DeepResult(met=True, output="a", coverage=cov(100)),
                     DeepResult(met=True, output="b", coverage=cov(200))])
    final = build(provider, runner, deep_goal_max_iterations=2).run("analyse every entry").deep_results[0]
    assert final.met is False and len(runner.briefs) == 2
    assert final.verdict_next_action.startswith("To cover the rest (144 entries)")


def test_a_runner_that_declares_nothing_behaves_exactly_as_before():
    provider = Provider([dict(ANALYSIS)])
    runner = Runner([DeepResult(met=True, output="done")])
    final = build(provider, runner).run("analyse every entry").deep_results[0]
    assert final.met is True and final.output == "done" and len(runner.briefs) == 1


# ---- (3) the pre-flight plan check

def test_the_preflight_runs_once_for_an_analysis_and_hands_the_worker_the_revised_brief_and_requirements():
    provider = Provider([dict(ANALYSIS)], preflights=[{
        "sufficient": False, "gaps": ["reads page at 200"],
        "revised_brief": "count first, then page through all entries", "honest_scope": ""}])
    runner = Runner([DeepResult(met=True, output="x", coverage=cov(344))])
    build(provider, runner, done_evidence_enabled=True, consumer_limits="a read returns at most 200 rows").run("analyse")
    assert provider.calls.count("plan_sufficiency") == 1
    prompt = provider.prompts["plan_sufficiency"][0]
    assert "a read returns at most 200 rows" in prompt and "R2. method stated" in prompt
    assert "count first, then page through all entries" in runner.briefs[0]
    assert "R1. covers all 344 entries" in runner.briefs[0]


def test_a_sufficient_plan_runs_as_planned_with_the_requirements_appended():
    provider = Provider([dict(ANALYSIS)])
    runner = Runner([DeepResult(met=True, output="x", coverage=cov(344))])
    build(provider, runner, done_evidence_enabled=True).run("analyse")
    assert "analyse the log" in runner.briefs[0] and "R2. method stated" in runner.briefs[0]


def test_a_question_from_the_preflight_stops_the_run_before_any_work():
    provider = Provider([dict(ANALYSIS)], preflights=[{"sufficient": False, "question": "Which log do you mean?"}])
    runner = Runner([])
    result = build(provider, runner, done_evidence_enabled=True).run("analyse")
    assert runner.briefs == []
    assert result.deep_results[0].needs_person == "Which log do you mean?" and not result.deep_results[0].met


def test_an_honest_scope_rides_into_the_brief_and_a_failed_preflight_changes_nothing():
    provider = Provider([dict(ANALYSIS)], preflights=[{"sufficient": False, "honest_scope": "only the newest 1,200"}])
    runner = Runner([DeepResult(met=True, output="x")])
    build(provider, runner, done_evidence_enabled=True).run("analyse")
    assert "STATE THIS SCOPE IN YOUR RESULT: only the newest 1,200" in runner.briefs[0]

    class Broken(Provider):
        def plan(self, prompt, *, model, tool_schema):
            if tool_schema.get("name") == "plan_sufficiency":
                raise RuntimeError("down")
            return super().plan(prompt, model=model, tool_schema=tool_schema)
    runner = Runner([DeepResult(met=True, output="x")])
    build(Broken([dict(ANALYSIS)]), runner, done_evidence_enabled=True).run("analyse")
    assert "analyse the log" in runner.briefs[0]


def test_an_action_or_a_flag_off_analysis_never_makes_the_preflight_call():
    for plan, cfg in ((dict(ANALYSIS, work_shape="action"), {"done_evidence_enabled": True}),
                      (dict(ANALYSIS), {})):
        provider = Provider([plan])
        build(provider, Runner([DeepResult(met=True, output="x")]), **cfg).run("do it")
        assert "plan_sufficiency" not in provider.calls


# ---- (4) the interim event

def test_a_partial_result_that_continues_emits_an_interim_event_with_the_agreed_shape():
    provider = Provider([dict(ANALYSIS)])
    runner = Runner([DeepResult(met=True, output="the first 200 show X", coverage=cov(200)),
                     DeepResult(met=True, output="all show X", coverage=cov(344))])
    _, events = run_with_events(build(provider, runner, done_evidence_enabled=True))
    interim = [e for e in events if e["type"] == EVENT_INTERIM]
    assert len(interim) == 1
    event = interim[0]
    assert event["data"]["final"] is False and event["data"]["coverage"]["processed"] == 200
    assert event["data"]["coverage"]["total"] == 344 and event["data"]["attempt"] == 1
    assert "the first 200 show X" in event["text"] and "Coverage: 200 of 344" in event["text"]
    assert "Still working" in event["text"] and "partial result" in event["text"]
    assert "—" not in event["text"]


def test_no_interim_event_without_the_flag_or_when_the_first_attempt_is_complete():
    runner = lambda: Runner([DeepResult(met=True, output="a", coverage=cov(200)),
                             DeepResult(met=True, output="b", coverage=cov(344))])
    _, events = run_with_events(build(Provider([dict(ANALYSIS)]), runner()))
    assert not [e for e in events if e["type"] == EVENT_INTERIM]
    _, events = run_with_events(build(Provider([dict(ANALYSIS)]), Runner([DeepResult(met=True, output="a", coverage=cov(344))]),
                                      done_evidence_enabled=True))
    assert not [e for e in events if e["type"] == EVENT_INTERIM]
