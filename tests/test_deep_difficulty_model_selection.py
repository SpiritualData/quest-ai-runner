"""The START of an unpinned deep run follows the hub's tier rule (deep_model_selection.py), and an escalation needs a reason.

Pinned here:

  * precedence: a pin on the task, a model or tier asked for in the message, the person's standing guidance (a guidance card), the operator's
    configuration (QAR_DEEP_MODEL_*), then the hub's default (the cheapest tier measured good enough);
  * the planner's difficulty rating alone never raises the start; a hard rating with a specific reason asks for one tier up;
  * the registry's tiers are the ladder when QAR_DEEP_MODELS is unset, and a single-rung ladder is logged once;
  * the hub unreadable: the registry's cheapest tier, said in the record;
  * an escalation needs a specific finding, moves one rung and one task, is recorded (tier, tier_source, tier_reason, tier_from) and never
    stored (no stickiness across tasks, nothing written to the task's deep_run_model);
  * the env wiring and the one status event that records the choice.

Fully offline.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pytest

from hubofhubs.api import DeepResult, role_tiers
from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig, normalize_decision
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser import deep_model_selection as selection
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.deep_model_selection import (
    choice_of, escalate_choice, normalize_difficulty, plan_start, sub_agent_environment, install_sub_agent_model)
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider

# A registry whose tiers are pinned to three distinct models, so a tier is visible as a model.
TIER_MODELS = {"fast": "haiku", "balanced": "sonnet", "quality": "opus", "best": "opus"}
MODEL_OF = {"fast": "haiku", "balanced": "sonnet", "quality": "opus"}
HUB_DEFAULT = role_tiers.task_choice().tier   # the cheapest sufficient tier today; the tests follow the hub, they do not restate it
RUNGS = list(role_tiers.ladder_for(role_tiers.read_rules()))


def one_up(tier: str) -> str:
    return RUNGS[RUNGS.index(tier) + 1]


NOT_MET = {"met": False, "what_fell_short": "incomplete", "next_steps": "finish it"}


def unmet(gap: str) -> Dict[str, Any]:
    """A not-met verdict that names its own gap (what was missing), so two in a row are not the same miss."""
    return {"met": False, "reason": gap, "next_action": "add " + gap, "what_fell_short": gap, "next_steps": "close " + gap}


def deep_plan(difficulty: Optional[str], reason: str = "because") -> Dict[str, Any]:
    plan = {"action": "deep", "goal": "Do the work", "deep_subtasks": [{"goal": "Do the work", "brief": "do it"}], "rationale": "deep"}
    if difficulty is not None:
        plan["deep_difficulty"] = difficulty
        plan["deep_difficulty_reason"] = reason
    return plan


class RecordingRunner:
    def __init__(self, results: Optional[List[DeepResult]] = None):
        self.results = list(results or [])
        self.models: List[Optional[str]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None, resume_session_id: Optional[str] = None) -> DeepResult:
        self.models.append(model)
        if self.results:
            return self.results.pop(0)
        return DeepResult(met=True, output="done")


class RecordingSink:
    def __init__(self):
        self.events = []

    def update(self, event, mode) -> None:
        self.events.append(event)


def registry_for(provider) -> ModelRegistry:
    return ModelRegistry(provider, fallback=dict(TIER_MODELS))


def build(provider, runner, **cfg) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=registry_for(provider), deep_runner=runner,
                        config=OrchestratorConfig(**cfg))


def run_models(difficulty: Optional[str], verdicts: List[Dict[str, Any]], *, reason: str = "because", model_hint: Optional[str] = None,
               sink: Any = None, **cfg) -> List[Optional[str]]:
    provider = ScriptedProvider(plans=[deep_plan(difficulty, reason)], verdicts=verdicts)
    runner = RecordingRunner()
    build(provider, runner, **cfg).run("please do the thing", model_hint=model_hint, sink=sink)
    return runner.models


def plan_of(orch: Orchestrator, **kw):
    return orch.deep_model_plan(kw.pop("model_hint", None), kw.pop("quality_standards", None), kw.pop("fallback", "sonnet"), **kw)


def fresh_orchestrator(**cfg) -> Orchestrator:
    provider = ScriptedProvider(plans=[], verdicts=[])
    return build(provider, RecordingRunner(), **cfg)


# --- what decides the start --------------------------------------------------------------------

def test_with_no_pin_and_nothing_set_the_hub_default_decides():
    ladder, sel = plan_of(fresh_orchestrator(), difficulty="normal")
    assert sel["tier"] == HUB_DEFAULT and sel["tier_source"] == "default"
    assert ladder[0] == MODEL_OF[HUB_DEFAULT]
    assert ladder == [MODEL_OF[t] for t in RUNGS][RUNGS.index(HUB_DEFAULT):]   # the registry's tiers in order are the ladder
    assert sel["tier_reason"] and "tier_from" not in sel


def test_the_difficulty_rating_alone_never_picks_or_raises_the_start():
    starts = {level: plan_of(fresh_orchestrator(), difficulty=level, difficulty_reason="hard")[1]["start_model"]
              for level in ("simple", "normal", "hard", None)}
    assert set(starts.values()) == {MODEL_OF[HUB_DEFAULT]}


def test_the_role_follows_the_task_kind_where_the_plan_knows_it():
    _, sel = plan_of(fresh_orchestrator(), task_kind="gather")
    assert sel["tier_role"] == "deep_work_gather"
    _, sel = plan_of(fresh_orchestrator(), task_kind="not-a-kind")
    assert sel["tier_role"] == "main_session"


def test_a_hard_rating_with_a_specific_reason_asks_for_one_tier_up_a_bare_one_does_not():
    ladder, sel = plan_of(fresh_orchestrator(), difficulty="hard", difficulty_reason="it rewrites a schema used by three services")
    assert (sel["tier"], sel["tier_source"], sel["tier_from"]) == (one_up(HUB_DEFAULT), "main_session_ask", HUB_DEFAULT)
    assert "schema" in sel["tier_reason"] and ladder[0] == MODEL_OF[one_up(HUB_DEFAULT)]
    for bare in (None, "", "hard", "complex"):
        _, sel = plan_of(fresh_orchestrator(), difficulty="hard", difficulty_reason=bare)
        assert (sel["tier"], sel["tier_source"]) == (HUB_DEFAULT, "default")


def test_the_persons_standing_guidance_card_is_the_hubs_standing_guidance():
    ladder, sel = plan_of(fresh_orchestrator(), quality_standards="Model selection card. preferred model: quality", difficulty="simple")
    assert (sel["tier"], sel["tier_source"]) == ("quality", "standing_guidance")
    assert ladder[0] == MODEL_OF["quality"]
    # The legacy family word names a tier too; escalation stays possible from it.
    ladder, sel = plan_of(fresh_orchestrator(), quality_standards="model: sonnet")
    assert (sel["tier"], sel["tier_source"], ladder) == ("balanced", "standing_guidance", ["sonnet", "opus"])


def test_a_guidance_card_naming_a_model_id_still_pins_it_alone():
    ladder, sel = plan_of(fresh_orchestrator(), quality_standards="model: claude-opus-4-8")
    assert ladder == ["claude-opus-4-8"] and sel is None


def test_a_per_task_pin_and_a_message_request_beat_standing_guidance_and_the_default():
    assert run_models("simple", [NOT_MET, {"met": True}], model_hint="fable")[0] == "fable"
    assert set(run_models("simple", [NOT_MET, {"met": True}], model_hint="fable")) == {"fable"}
    provider = ScriptedProvider(plans=[{**deep_plan("simple"), "deep_model_request": "use opus for this"}], verdicts=[{"met": True}])
    runner = RecordingRunner()
    build(provider, runner).run("x", model_hint="haiku")
    assert runner.models == ["opus"]                      # the message asks for a stronger model: it wins over the task's standing pin


def test_a_per_task_tier_pin_beats_the_automatic_choice():
    provider = ScriptedProvider(plans=[deep_plan("simple")], verdicts=[{"met": True}])
    runner = RecordingRunner()
    build(provider, runner).run("x", model_hint="best")
    assert runner.models == [registry_for(provider).resolve_tier("best")]


def test_the_operators_start_models_count_as_standing_guidance():
    cfg = dict(deep_difficulty_models={"hard": "opus"})
    ladder, sel = plan_of(fresh_orchestrator(**cfg), difficulty="hard")
    assert ladder == ["opus"] and (sel["tier"], sel["tier_source"]) == ("quality", "standing_guidance")
    assert "QAR_DEEP_MODEL_HARD" in sel["tier_reason"]
    # Work of another rating is not touched by it: the hub default decides.
    _, sel = plan_of(fresh_orchestrator(**cfg), difficulty="simple")
    assert (sel["tier"], sel["tier_source"]) == (HUB_DEFAULT, "default")
    # A person's own guidance card outranks the operator.
    _, sel = plan_of(fresh_orchestrator(**cfg), difficulty="hard", quality_standards="model: balanced")
    assert (sel["tier"], sel["tier_source"]) == ("balanced", "standing_guidance")


def test_auto_model_off_restores_the_resolved_ladder_from_its_first_rung():
    assert run_models("hard", [{"met": True}], deep_auto_model=False, deep_model_ladder=["haiku", "sonnet", "opus"]) == ["haiku"]
    orch = fresh_orchestrator(deep_auto_model=False)
    expected, pinned = orch.resolve_deep_ladder(None, None, "claude-opus-4-8")
    ladder, sel = orch.deep_model_plan(None, None, "claude-opus-4-8", difficulty="simple")
    assert (ladder, sel, pinned) == (expected, None, False)


def test_a_configured_ladder_is_the_escalation_path_and_the_start_is_the_hubs_tier_on_it():
    ladder, sel = plan_of(fresh_orchestrator(deep_model_ladder=["haiku", "claude-sonnet-4-6", "claude-opus-4-8"]))
    assert ladder[0] == {"fast": "haiku", "balanced": "claude-sonnet-4-6", "quality": "claude-opus-4-8"}[HUB_DEFAULT]
    ladder, _ = plan_of(fresh_orchestrator(deep_model_ladder=["claude-sonnet-4-6", "claude-opus-4-8"]), quality_standards="model: balanced")
    assert ladder == ["claude-sonnet-4-6", "claude-opus-4-8"]   # sonnet and claude-sonnet-4-6 are one rung


def test_a_start_not_on_the_ladder_goes_to_the_nearest_stronger_rung():
    ladder, sel = plan_of(fresh_orchestrator(deep_model_ladder=["sonnet", "opus"]), quality_standards="model: fast")
    assert ladder == ["sonnet", "opus"] and "not on the ladder" in sel["reason"]


def test_a_single_rung_ladder_works_and_is_logged_once(caplog):
    selection.warned_single_rung.clear()
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner"):
        for _ in range(3):
            ladder, _ = plan_of(fresh_orchestrator(deep_model_ladder=["sonnet"]))
    assert ladder == ["sonnet"]
    assert len([r for r in caplog.records if "one rung" in r.getMessage()]) == 1


def test_an_unreadable_hub_falls_back_to_the_registrys_cheapest_tier_and_says_so(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise ImportError("the hub is not importable")
    monkeypatch.setattr(selection, "hub_choice", broken)
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner"):
        ladder, sel = plan_of(fresh_orchestrator())
    assert (sel["tier"], sel["tier_source"], ladder[0]) == ("fast", "fallback", "haiku")
    assert "unavailable" in sel["tier_reason"]
    assert any("could not be applied" in r.getMessage() for r in caplog.records)


def test_unknown_difficulty_or_no_ladder_means_no_selection():
    class Dead:
        def resolve_tier(self, tier):
            raise RuntimeError("no model for any tier")
    assert plan_start(Dead()) is None


def test_normalize_difficulty_accepts_synonyms_and_rejects_garbage():
    assert normalize_difficulty(" Trivial ") == "simple"
    assert normalize_difficulty("complex") == "hard"
    assert normalize_difficulty("normal") == "normal"
    assert normalize_difficulty("banana") is None
    assert normalize_difficulty(3) is None


def test_planner_fields_are_parsed_and_fail_safe():
    cfg = OrchestratorConfig()
    d = normalize_decision(deep_plan("easy", reason="  a lookup "), cfg)
    assert (d.deep_difficulty, d.deep_difficulty_reason) == ("simple", "a lookup")
    d = normalize_decision({**deep_plan("banana"), "deep_difficulty_reason": 7}, cfg)
    assert (d.deep_difficulty, d.deep_difficulty_reason) == (None, None)


# --- escalation --------------------------------------------------------------------------------

def test_an_escalation_needs_a_specific_reason_and_moves_one_rung():
    start = role_tiers.task_choice()
    for blank in (None, "", "   ", "stop_decision", "stop decision", "hard"):
        assert escalate_choice(start, "stop_decision", blank) is None
    moved = escalate_choice(start, "stop_decision", "the migration was never run against the test database")
    assert moved.tier == one_up(start.tier) and moved.source == "escalation" and moved.from_tier == start.tier
    assert moved.record() == {"tier": one_up(start.tier), "tier_source": "escalation",
                              "tier_reason": "stop_decision: the migration was never run against the test database", "tier_from": start.tier}
    assert start.tier == HUB_DEFAULT      # the original choice is untouched: nothing is stored


def test_the_top_rung_does_not_escalate_and_best_is_never_a_rung():
    top = role_tiers.TierChoice("main_session", RUNGS[-1], "default", "x")
    assert escalate_choice(top, "stop_decision", "still missing the summary table") is None
    best = role_tiers.TierChoice("main_session", "best", "current_input", "x")
    assert escalate_choice(best, "stop_decision", "still missing the summary table") is None


def test_the_goal_loop_escalates_one_rung_with_the_verifiers_finding_and_records_it():
    sink = RecordingSink()
    models = run_models("normal", [unmet("the totals row"), {"met": True}], sink=sink)
    assert models == [MODEL_OF[HUB_DEFAULT], MODEL_OF[one_up(HUB_DEFAULT)]]
    events = [e.data["deep_tier_escalation"] for e in sink.events if "deep_tier_escalation" in (e.data or {})]
    assert len(events) == 1
    assert events[0]["tier"] == one_up(HUB_DEFAULT) and events[0]["tier_from"] == HUB_DEFAULT and events[0]["tier_source"] == "escalation"
    assert "add the totals row" in events[0]["tier_reason"]


def test_a_finding_with_nothing_specific_keeps_the_same_model_for_the_retry():
    blank = {"met": False, "reason": "", "next_action": "", "what_fell_short": "gap one", "next_steps": "go on"}
    other = {"met": False, "reason": "", "next_action": "", "what_fell_short": "gap two", "next_steps": "go on"}
    models = run_models("normal", [blank, other, {"met": True}])
    assert set(models) == {MODEL_OF[HUB_DEFAULT]}


def test_the_verifiers_next_tier_suggestion_does_not_jump_rungs():
    models = run_models("normal", [{**unmet("the totals row"), "next_tier": "best"}, {"met": True}])
    assert models == [MODEL_OF[HUB_DEFAULT], MODEL_OF[one_up(HUB_DEFAULT)]]


def test_an_escalation_is_not_sticky_across_tasks_and_is_never_written_to_the_task():
    orch = fresh_orchestrator()
    first = plan_of(orch, difficulty="normal")
    moved = orch.escalate_deep_choice(first[1], first[0][0], "stop_decision", "the totals row was missing")
    assert moved["tier"] == one_up(HUB_DEFAULT)
    second = plan_of(orch, difficulty="normal")          # the next task is chosen again from scratch
    assert second[1]["tier"] == HUB_DEFAULT and second[0] == first[0]
    assert first[1]["tier"] == HUB_DEFAULT               # the recorded start of the first task is unchanged
    task = {"id": "t1", "model": None, "deep_run_model": None}
    before = dict(task)
    run_models("normal", [unmet("the totals row"), {"met": True}])
    assert task == before and "deep_run_model" not in orch.__dict__.get("task", {})


def test_the_tier_rule_never_writes_a_deep_run_model_anywhere():
    import inspect
    from quest_ai_runner.the_intelligence.mind.delegation import orchestrator_deep_models
    assert "deep_run_model" not in inspect.getsource(selection).replace("``deep_run_model``", "")
    assert "update_task" not in inspect.getsource(orchestrator_deep_models)


def test_choice_of_rebuilds_a_choice_from_a_record_or_a_model():
    _, sel = plan_of(fresh_orchestrator(), difficulty="normal")
    choice = choice_of(sel)
    assert (choice.tier, choice.source, choice.role) == (sel["tier"], sel["tier_source"], sel["tier_role"])
    from_model = choice_of(None, "opus", {"quality": "opus"})
    assert from_model.tier == "quality" and from_model.source == "standing_guidance"


# --- the record --------------------------------------------------------------------------------

def test_the_choice_is_announced_once_with_the_tier_record():
    provider = ScriptedProvider(plans=[deep_plan("simple", reason="a status read")], verdicts=[NOT_MET, {"met": True}])
    sink = RecordingSink()
    build(provider, RecordingRunner()).run("x", sink=sink)
    picks = [e for e in sink.events if "deep_model_selection" in (e.data or {})]
    assert len(picks) == 1
    data = picks[0].data["deep_model_selection"]
    assert data["start_model"] == MODEL_OF[HUB_DEFAULT] and data["difficulty"] == "simple"
    assert (data["tier"], data["tier_source"]) == (HUB_DEFAULT, "default") and data["tier_reason"]
    assert MODEL_OF[HUB_DEFAULT] in picks[0].text and "—" not in picks[0].text


def test_a_pinned_run_announces_no_automatic_choice():
    provider = ScriptedProvider(plans=[deep_plan("simple")], verdicts=[{"met": True}])
    sink = RecordingSink()
    build(provider, RecordingRunner()).run("x", sink=sink, model_hint="fable")
    assert not [e for e in sink.events if "deep_model_selection" in (e.data or {})]


def test_plan_decision_defaults_to_no_rating():
    assert PlanDecision(action="deep").deep_difficulty is None


# --- sub-agents --------------------------------------------------------------------------------

def test_sub_agents_default_to_the_hubs_sub_agent_choice_unless_the_operator_set_one():
    registry = ModelRegistry(ScriptedProvider(plans=[], verdicts=[]), fallback=dict(TIER_MODELS))
    expected = MODEL_OF[role_tiers.sub_agent_choice().tier]
    try:
        install_sub_agent_model(selection.sub_agent_default_model(registry))
        assert sub_agent_environment({})[selection.SUB_AGENT_ENV] == expected
        assert sub_agent_environment({selection.SUB_AGENT_ENV: "mine"})[selection.SUB_AGENT_ENV] == "mine"
    finally:
        install_sub_agent_model(None)
    assert selection.SUB_AGENT_ENV not in sub_agent_environment({})


# --- env wiring --------------------------------------------------------------------------------

def test_env_wiring(monkeypatch):
    from quest_ai_runner.the_consciousness.terminal.cli import _config_from_env

    monkeypatch.setenv("QAR_DEEP_AUTO_MODEL", "off")
    monkeypatch.setenv("QAR_DEEP_MODEL_HARD", "opus")
    monkeypatch.setenv("QAR_DEEP_MODEL_SIMPLE", "fast")   # a tier name: ignored
    cfg = _config_from_env()
    assert cfg.orchestrator.deep_auto_model is False
    assert cfg.orchestrator.deep_difficulty_models == {"hard": "opus"}


def test_with_no_operator_variable_set_nothing_overrides_the_hub_rule(monkeypatch):
    from quest_ai_runner.the_consciousness.terminal.cli import _config_from_env

    for var in ("QAR_DEEP_AUTO_MODEL", "QAR_DEEP_MODEL_SIMPLE", "QAR_DEEP_MODEL_NORMAL", "QAR_DEEP_MODEL_HARD", "QAR_DEEP_MODELS"):
        monkeypatch.delenv(var, raising=False)
    cfg = _config_from_env()
    assert cfg.orchestrator.deep_auto_model is True
    assert cfg.orchestrator.deep_difficulty_models == {}
    assert not cfg.orchestrator.deep_model_ladder
