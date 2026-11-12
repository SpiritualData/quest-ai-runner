"""Which tier each model call runs on: the hub's role table decides, and three guards hold in code.

  * classification is never above fast, whatever tier is asked;
  * a response a person reads is never below balanced, whatever tier is asked, and only a person's explicit tier raises it;
  * deep work defaults to balanced unless the person or task sets a tier, and ``best`` is reachable only by that explicit tier or by the one
    ``structure-watch`` role.

Fully offline: no model is called.
"""
from __future__ import annotations

import importlib
import pkgutil
from types import SimpleNamespace

import pytest

import quest_ai_runner
from hubofhubs.api import role_tiers
from quest_ai_runner.the_intelligence.mind.delegation.orchestrator_deep_models import DeepModelsMixin
from quest_ai_runner.the_intelligence.mind.orchestrator_types import OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_answering import AnsweringMixin
from quest_ai_runner.the_self.integrity.prompts.prompt_specs import SPECS_BY_ID
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.call_roles import (
    CALL_ROLES, ORDER, PERSON_SET_CALLS, STRUCTURE_WATCH_CALL, TIERS, calls_at, role_of, tier_for_call)
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.deep_model_selection import DEFAULT_DIFFICULTY_MODELS, select_deep_start
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

TABLE = role_tiers.read_table()
ALL_ASKS = (None, "", "fast", "balanced", "quality", "best", "strong", "science", "not-a-tier")


class Lister:
    def __init__(self, live):
        self.live = live

    def list_models(self):
        return list(self.live)


class Recorder:
    """A registry stand-in that records which tiers were asked for."""

    def __init__(self):
        self.asked = []

    def resolve_tier(self, tier):
        self.asked.append(tier)
        return f"model-for-{tier}"

    def resolve_role(self, call, requested=None):
        return self.resolve_tier(tier_for_call(call, requested))

    @staticmethod
    def is_tier_name(name):
        return ModelRegistry.is_tier_name(name)


def kind_of(call):
    return TABLE[role_of(call)]["kind"]


def load_every_module():
    for module in pkgutil.walk_packages(quest_ai_runner.__path__, "quest_ai_runner."):
        try:
            importlib.import_module(module.name)
        except Exception:  # noqa: BLE001 -- an optional dependency missing here is not this test's subject
            continue


def test_every_call_names_a_role_of_the_table():
    for call, role in CALL_ROLES.items():
        assert role in TABLE, (call, role)
    with pytest.raises(KeyError):
        tier_for_call("a-call-nobody-registered")


def test_every_prompt_spec_is_a_call_with_the_tier_the_table_gives():
    load_every_module()
    assert SPECS_BY_ID
    for spec_id, spec in SPECS_BY_ID.items():
        assert spec_id in CALL_ROLES, f"prompt spec {spec_id!r} has no role in CALL_ROLES"
        assert spec.tier == tier_for_call(spec_id), spec_id


def test_classification_is_never_above_fast_whatever_is_asked():
    calls = [call for call in CALL_ROLES if kind_of(call) == "classify"]
    assert len(calls) >= 8
    for call in calls:
        for asked in ALL_ASKS:
            assert tier_for_call(call, asked) == "fast", (call, asked)


def test_a_response_is_never_below_balanced_and_only_a_person_raises_it():
    calls = [call for call in CALL_ROLES if kind_of(call) == "write"]
    assert {"answer.response", "answer.task-report", "explain-an-answer.how-i-got-this", "narrate-a-turn.first-beat"} <= set(calls)
    for call in calls:
        for asked in ALL_ASKS:
            assert ORDER[tier_for_call(call, asked)] >= ORDER["balanced"], (call, asked)
        assert tier_for_call(call) == "balanced"
        assert tier_for_call(call, "quality") == "quality"
        assert tier_for_call(call, "fast") == "balanced"
        assert (tier_for_call(call, "best") == "best") == (call in PERSON_SET_CALLS)   # best only on the person's own work


def test_the_reply_ignores_the_planners_own_tier_and_never_runs_on_fast():
    registry = Recorder()
    orch = SimpleNamespace(registry=registry)
    plan = SimpleNamespace(model_tier="haiku")
    assert AnsweringMixin._answer_model(orch, plan, "sonnet") == "model-for-balanced"          # the planner's haiku picks nothing
    assert AnsweringMixin._answer_model(orch, plan, "balanced", hint="fast") == "model-for-balanced"   # a person's fast is lifted to the floor
    assert AnsweringMixin._answer_model(orch, plan, "balanced", hint="quality") == "model-for-quality"


def test_deep_work_defaults_to_the_tables_tier_and_the_persons_or_tasks_tier_wins():
    registry = Recorder()
    orch = SimpleNamespace(registry=registry)
    plan = SimpleNamespace(model_tier="opus")
    deep = TABLE["main_session"]["tier"]                                            # the role-tier metric decides it (hub role_tiers.csv)
    assert AnsweringMixin._deep_model(orch, plan) == f"model-for-{deep}"          # the planner's own opus does not pick the work's tier
    assert AnsweringMixin._deep_model(orch, plan, hint="quality") == "model-for-quality"
    assert AnsweringMixin._deep_model(orch, plan, hint="fast") == "model-for-fast"
    assert AnsweringMixin._deep_model(orch, plan, hint="best") == "model-for-best"
    assert tier_for_call("deep-run.sub-agent") == deep and tier_for_call("deep-run.sub-agent", "quality") == "quality"


def test_deep_runs_start_on_balanced_whatever_the_difficulty():
    assert set(DEFAULT_DIFFICULTY_MODELS.values()) == {"sonnet"}
    for ladder in (["haiku", "sonnet", "opus"], ["haiku", "sonnet"]):
        for level in ("simple", "normal", "hard"):
            ladder_from_start, _ = select_deep_start(level, ladder)
            assert ladder_from_start[0] == "sonnet", (level, ladder)


def test_best_is_not_an_escalation_rung_and_a_verifiers_best_is_lifted_to_quality():
    registry = Recorder()
    orch = SimpleNamespace(registry=registry)
    DeepModelsMixin.fallback_deep_ladder(orch, "claude-sonnet")
    assert "best" not in registry.asked
    DeepModelsMixin._resolved_deep_tier(orch, "best", ["claude-sonnet"])
    assert registry.asked[-1] == "quality"


def test_best_is_reached_only_by_the_persons_explicit_tier_or_the_structure_watch():
    unasked = [call for call in CALL_ROLES if tier_for_call(call) == "best"]
    assert unasked == [STRUCTURE_WATCH_CALL]
    person_may_set = sorted(call for call in CALL_ROLES if tier_for_call(call, "best") == "best" and call != STRUCTURE_WATCH_CALL)
    assert person_may_set == ["answer.response", "answer.task-report", "autopilot.work", "deep-run.session", "deep-run.sub-agent", "draft-a-metric.value"]
    for call in set(CALL_ROLES) - set(person_may_set) - {STRUCTURE_WATCH_CALL}:
        assert tier_for_call(call, "best") != "best", call
    # the lane-side settings that feed a tier by name are guarded too
    for field in ("planner_tier", "verify_tier", "intent_judge_tier", "mode_release_tier", "overseer_tier", "explain_tier", "reply_check_tier"):
        assert getattr(OrchestratorConfig(), field) != "best", field


def test_every_tier_default_of_the_orchestrator_is_its_calls_tier_in_the_table():
    cfg = OrchestratorConfig()
    for field, call in (("planner_tier", "route-a-message.planner"), ("planner_cascade_tier", "route-a-message.cascade-review"),
                        ("planner_reach_judge_tier", "claim-reach.reach-judge"), ("reply_check_tier", "reply-check.view"),
                        ("verify_tier", "verify-goal.verdict"), ("intent_judge_tier", "judge-a-decision.intent-directive"),
                        ("mode_release_tier", "judge-a-decision.mode-release"), ("overseer_tier", "watch-a-run.overseer"),
                        ("explain_tier", "explain-an-answer.how-i-got-this"), ("milestone_writer_tier", "milestone-progress.deliverable")):
        assert getattr(cfg, field) == tier_for_call(call), field


def test_a_lanes_explicit_tier_applies_to_a_call_that_is_neither_classification_nor_a_response():
    assert tier_for_call("route-a-message.planner") == "balanced"                  # a misrouted turn has a significant downstream effect
    assert tier_for_call("route-a-message.planner", "fast") == "fast"              # a lane that sets its planner tier keeps it
    assert tier_for_call("replay-known-operations.fill-args") == TABLE["request_extraction"]["tier"]   # the metric's tier for extraction
    assert tier_for_call("watch-a-run.overseer", "quality") == "quality"
    assert tier_for_call("watch-a-run.overseer", "best") == "quality"              # but never best


def test_science_is_a_domain_word_that_reads_as_its_roles_tier():
    registry = ModelRegistry(Lister(["claude-haiku-5-5", "claude-sonnet-5-5"]))
    assert ModelRegistry.is_tier_name("science")
    assert registry.resolve_tier("science") == registry.resolve_tier("fast")
    assert registry.resolve_tier("science") not in ("", None)
    assert tier_for_call("answer.response", "science") == "balanced"   # a response is lifted to its floor


def test_resolve_role_gives_the_lanes_model_for_the_tier():
    registry = ModelRegistry(Lister(["claude-haiku-5-5", "claude-sonnet-5-5"]), fallback={"fast": "pin-fast", "balanced": "pin-balanced"})
    assert registry.resolve_role("select-context.rank-files") == "pin-fast"
    assert registry.resolve_role("answer.response") == "pin-balanced"
    assert registry.resolve_role("answer.response", "fast") == "pin-balanced"


def test_calls_at_lists_the_calls_of_a_tier():
    assert "answer.response" in calls_at("balanced") and "select-context.rank-files" in calls_at("fast")
    assert calls_at("best") == [STRUCTURE_WATCH_CALL] and set(TIERS) == {"fast", "balanced", "quality", "best"}


def test_a_deployment_can_keep_a_model_out_of_its_tier_defaults_without_touching_pins():
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser import model_registry
    from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.providers import excluded_models_from_environment
    assert excluded_models_from_environment({"QAR_EXCLUDED_MODELS": " Opus, gpt "}) == ("opus", "gpt")
    assert excluded_models_from_environment({}) == ()
    top = {"fast": "claude-haiku-5-5", "balanced": "claude-sonnet-5-5", "quality": "claude-opus-5-5", "best": "claude-opus-5-5"}
    assert model_registry.drop_excluded(top, ()) == top                       # nothing is excluded by rule
    dropped = model_registry.drop_excluded(top, ("opus",))
    assert dropped["quality"] == dropped["best"] == "claude-sonnet-5-5" and dropped["fast"] == "claude-haiku-5-5"
    previous = model_registry.excluded_source
    model_registry.set_excluded_source(lambda: ("opus",))
    try:
        registry = ModelRegistry(Lister(["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]), fallback={"best": "claude-opus-5-5"})
        assert "opus" not in registry.top_models()["quality"]
        assert registry.top_models()["best"] == "claude-opus-5-5"            # a pin is honored
    finally:
        model_registry.set_excluded_source(previous)


def test_spend_by_role_reads_the_per_call_records_and_marks_the_evidence_above_fast(tmp_path, capsys):
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.call_roles import render_spend_report, spend_by_role
    rows = [
        {"ts": 100, "spec_id": "select-context.rank-files", "tokens_in": 1000, "tokens_out": 100, "cost_usd": 0.01},
        {"ts": 101, "spec_id": "select-context.rank-files", "tokens_in": 1000, "tokens_out": 100, "cost_usd": 0.01},
        {"ts": 102, "spec_id": "answer.response", "tokens_in": 2000, "tokens_out": 50, "cost_usd": 0.06},
        {"ts": 103, "spec_id": "(untagged)", "tokens_in": 10, "tokens_out": 10, "cost_usd": 0.02},
        {"ts": 5, "spec_id": "answer.response", "tokens_in": 9, "tokens_out": 9, "cost_usd": 9.0},   # before the period
    ]
    report = spend_by_role(rows, since=50)
    by_role = {r["role"]: r for r in report}
    assert [r["role"] for r in report][0] == "response_writing" and by_role["context_retrieval"]["calls"] == 2
    assert by_role["context_retrieval"]["tokens"] == 2200 and by_role["context_retrieval"]["tier"] == "fast"
    assert by_role["context_retrieval"]["evidence"] == ""                                   # fast: no mark needed
    assert by_role["response_writing"]["evidence"].startswith(("direct public", "proxy public", "STALE OR MISSING"))   # computed by the hub's role-tier metric
    assert by_role["(untagged)"]["calls"] == 1
    assert abs(sum(r["share"] for r in report) - 1.0) < 1e-9
    assert "response_writing" in render_spend_report(report)
    log = tmp_path / "calls.jsonl"
    import json
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    from quest_ai_runner.the_consciousness.terminal.cli_spend import _cmd_spend_by_role
    assert _cmd_spend_by_role(SimpleNamespace(log=str(log), days=36500, json=True), None) == 0
    assert "response_writing" in capsys.readouterr().out
