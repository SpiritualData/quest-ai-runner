"""What a person picks for a task's deep run reaches the run, from every source, as a START TIER or a PIN.

A tier (a task's ``model``, a conversation's or autopilot's tier, a per-run override's tier) is the person's requested start: it beats the standing guidance
and the default, it reaches the deep run, and an escalation may still move one rung from it. A specific model (``deep_run_model``, an autopilot Claude
model, a model named in the message) is a pin: it starts that model alone. The environment's default is standing guidance (below a card), and its allowed
list applies to both paths. Each test follows one task payload (the fields the backend stamps) to the run's resolved start.

Fully offline.
"""
from __future__ import annotations

import pytest

from hubofhubs.api import role_tiers
from quest_ai_runner.the_intelligence.mind.delegation.work_model import (
    DeepModelAsk, deep_model_ask, default_effective, rung_allowed, split_requested)

from .test_deep_difficulty_model_selection import (
    HUB_DEFAULT, MODEL_OF, RUNGS, NOT_MET, RecordingRunner, build, deep_plan, fresh_orchestrator, registry_for, unmet)
from .test_per_goal_context_iteration import ScriptedProvider


def effective_view(allowed=("haiku", "sonnet", "opus"), default="sonnet"):
    def entry(model):
        return {"provider": "anthropic", "model": model, "tier": "standard"}
    return {"default_model": entry(default), "allowed_models": [entry(m) for m in allowed], "excluded_tiers": [], "reported_models": None}


def plan_for(task, *, configured=False, view=None, quality_standards=None, orch=None, **kw):
    """The deep run's resolved ladder and selection for one task payload, as the lane builds it: the task's fields become a pin or a start tier, and
    the environment's default and allowed models ride the context meta."""
    ask = deep_model_ask(task.get("deep_run_model"), task.get("model"), view or default_effective(), configured=configured)
    meta = ask.meta()
    ladder, sel = (orch or fresh_orchestrator()).deep_model_plan(
        ask.hint, quality_standards, "sonnet", standing_model=meta.get("deep_standing_model"), allowed_models=meta.get("deep_allowed_models"), **kw)
    return ask, ladder, sel


# --- the split of a task's fields -------------------------------------------------------------------------

@pytest.mark.parametrize("deep_run_model,model,expected", [
    (None, None, (None, None)),
    ("opus", None, ("opus", None)),                              # a family word in the deep field is a pin
    ("claude-opus-4-8", None, ("claude-opus-4-8", None)),        # so is a versioned id
    (None, "quality", (None, "quality")),                        # a tier in model is the start tier
    (None, "opus", (None, "quality")),                           # the legacy family words read as their tier in ``model``
    ("balanced", None, (None, "balanced")),                      # a tier word in the deep field is a tier too
    ("opus", "fast", ("opus", None)),                            # both: the pin is the more specific
    (None, "science", (None, None)),                             # not a deep tier: no request
    (None, "claude-opus-4-8", ("claude-opus-4-8", None)),        # a literal id in ``model`` is a pin
])
def test_split_requested(deep_run_model, model, expected):
    assert split_requested(deep_run_model, model) == expected


# --- each source: modal task, conversation, autopilot, per-run override (all arrive as task fields) ---------

def test_a_task_tier_is_the_start_tier_beats_a_card_and_may_still_escalate():
    ask, ladder, sel = plan_for({"model": "quality"}, quality_standards="model: fast")
    assert (ask.pin, ask.tier) == (None, "quality")
    assert (sel["tier"], sel["tier_source"]) == ("quality", "current_input")
    assert ladder[0] == MODEL_OF["quality"]
    # not a pin: the ladder keeps a rung above only when the tier is not the top; the record is escalatable by the hub from a mid tier
    ask, ladder, sel = plan_for({"model": "fast"})
    assert (sel["tier"], sel["tier_source"]) == ("fast", "current_input") and len(ladder) > 1
    moved = fresh_orchestrator().escalate_deep_choice(sel, ladder[0], "stop_decision", "the totals row was never added")
    assert moved["tier"] == "balanced" and moved["tier_from"] == "fast" and moved["tier_source"] == "escalation"


def test_a_task_pin_starts_that_model_alone_and_never_escalates():
    ask, ladder, sel = plan_for({"deep_run_model": "opus", "model": "fast"})
    assert (ask.pin, ask.tier) == ("opus", None)
    assert ladder == ["opus"] and sel is None


def test_a_conversation_or_per_run_deep_tier_reaches_the_run_like_a_task_tier():
    # The backend stamps a conversation's deep tier and a per-run override's tier on the task's ``model`` field; the lane sees one shape.
    for task in ({"model": "balanced"}, {"model": "balanced", "deep_run_model": None}):
        _, ladder, sel = plan_for(task)
        assert (sel["tier"], sel["tier_source"], ladder[0]) == ("balanced", "current_input", MODEL_OF["balanced"])


def test_the_best_tier_can_be_the_start_but_is_never_an_escalation_rung():
    _, ladder, sel = plan_for({"model": "best"})
    assert (sel["tier"], sel["tier_source"]) == ("best", "current_input")
    assert fresh_orchestrator().escalate_deep_choice(sel, ladder[0], "stop_decision", "the summary table is still missing") is None


def test_an_autopilot_claude_model_is_a_pin_and_its_tier_is_a_start_tier():
    # The autopilot's own create path is pinned in test_autopilot.py; this is the shared split it calls.
    assert split_requested("opus", None) == ("opus", None) and split_requested("balanced", None) == (None, "balanced")


# --- standing guidance: the environment's default sits below a card and above the operator -----------------

def test_the_environment_default_is_standing_guidance_not_a_pin():
    ask, ladder, sel = plan_for({}, configured=True, view=effective_view(default="sonnet"))
    assert (ask.pin, ask.tier, ask.standing) == (None, None, "sonnet")
    assert (sel["tier"], sel["tier_source"]) == ("balanced", "standing_guidance") and len(ladder) > 1   # it can still escalate


def test_a_card_beats_the_environment_default_which_beats_the_operator_and_the_hub_default():
    _, _, sel = plan_for({}, configured=True, view=effective_view(default="sonnet"), quality_standards="model: quality")
    assert (sel["tier"], sel["tier_source"]) == ("quality", "standing_guidance")
    orch = fresh_orchestrator(deep_difficulty_models={"normal": "opus"})
    _, _, sel = plan_for({}, configured=True, view=effective_view(default="haiku"), orch=orch, difficulty="normal")
    assert sel["tier"] == "fast"                                  # the environment, not the operator's model
    _, _, sel = plan_for({}, configured=False, orch=orch, difficulty="normal")
    assert "QAR_DEEP_MODEL_NORMAL" in sel["tier_reason"]          # no environment default: the operator's variable decides
    _, _, sel = plan_for({}, configured=False)
    assert (sel["tier"], sel["tier_source"]) == (HUB_DEFAULT, "default")


def test_a_person_tier_beats_the_environment_default():
    _, _, sel = plan_for({"model": "quality"}, configured=True, view=effective_view(default="haiku"))
    assert (sel["tier"], sel["tier_source"]) == ("quality", "current_input")


def test_an_environment_default_naming_a_version_starts_on_its_familys_tier_as_standing_guidance():
    ask, ladder, sel = plan_for({}, configured=True, view=effective_view(allowed=("claude-sonnet-9-9", "opus"), default="claude-sonnet-9-9"))
    assert ask.standing == "claude-sonnet-9-9" and ask.pin is None
    assert (sel["tier"], sel["tier_source"]) == ("balanced", "standing_guidance")


# --- the environment's allowed list applies to both paths ---------------------------------------------------

def test_a_tier_run_never_starts_or_escalates_onto_an_excluded_model():
    view = effective_view(allowed=("haiku", "sonnet"))
    ask, ladder, sel = plan_for({"model": "quality"}, configured=True, view=view)
    assert ask.allowed == ("haiku", "sonnet")
    assert MODEL_OF["quality"] not in ladder and ladder[0] == MODEL_OF["balanced"]   # the start moves to the nearest allowed rung
    assert "not on the ladder" in sel["reason"]
    assert all(rung_allowed(m, list(ask.allowed)) for m in ladder)


def test_a_pin_the_environment_excludes_falls_back_to_its_default():
    ask, ladder, sel = plan_for({"deep_run_model": "opus"}, configured=True, view=effective_view(allowed=("haiku", "sonnet"), default="sonnet"))
    assert ask.pin == "sonnet" and ask.note and ladder == ["sonnet"]


def test_rung_allowed_judges_a_rung_by_its_family():
    assert rung_allowed("claude-opus-4-8", ["opus"]) and not rung_allowed("claude-opus-4-8", ["haiku", "sonnet"])
    assert rung_allowed("sonnet", ["claude-sonnet-5-5"]) and rung_allowed("some-other-model", ["haiku"]) and rung_allowed("opus", None)


# --- through the run: the context meta reaches the deep run -------------------------------------------------

def run_models(plan, task, *, configured=False, view=None, verdicts=None, **cfg):
    ask = deep_model_ask(task.get("deep_run_model"), task.get("model"), view or default_effective(), configured=configured)
    provider = ScriptedProvider(plans=[plan], verdicts=verdicts or [{"met": True}])
    runner = RecordingRunner()
    build(provider, runner, **cfg).run("please do the thing", model_hint=ask.hint, context_meta=ask.meta())
    return runner.models


def test_the_run_starts_on_the_tier_the_task_asked_for_and_escalates_one_rung_on_a_recorded_shortfall():
    models = run_models(deep_plan("simple"), {"model": "fast"}, verdicts=[unmet("the totals row"), {"met": True}])
    assert models == [MODEL_OF["fast"], MODEL_OF["balanced"]]


def test_the_run_keeps_a_pin_through_a_shortfall():
    models = run_models(deep_plan("simple"), {"deep_run_model": "haiku"}, verdicts=[unmet("the totals row"), {"met": True}])
    assert set(models) == {"haiku"}


def test_the_run_starts_on_the_environment_default_when_nothing_else_is_asked():
    models = run_models(deep_plan("simple"), {}, configured=True, view=effective_view(default="opus"))
    assert models == [MODEL_OF["quality"]]


def test_a_model_typed_in_the_message_that_the_environment_excludes_is_ignored_and_a_typed_tier_is_a_start_tier():
    view = effective_view(allowed=("haiku", "sonnet"))
    typed = {**deep_plan("simple"), "deep_model_request": "use opus for this"}
    assert run_models(typed, {}, configured=True, view=view)[0] != "opus"
    typed_tier = {**deep_plan("simple"), "deep_model_request": "quality"}
    models = run_models(typed_tier, {"model": "fast"}, verdicts=[unmet("the totals row"), {"met": True}])
    assert models[0] == MODEL_OF["quality"]


# --- no task kind is invented -----------------------------------------------------------------------------

def test_difficulty_is_never_passed_as_the_task_kind_and_the_plan_carries_no_kind():
    for level in ("simple", "normal", "hard"):
        _, sel = fresh_orchestrator().deep_model_plan(None, None, "sonnet", difficulty=level)
        assert sel["tier_role"] == "main_session"
    from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
    assert not hasattr(PlanDecision(action="deep"), "deep_task_kind")


def test_deep_model_ask_is_a_plain_value():
    ask = deep_model_ask(None, "balanced", default_effective(), configured=False)
    assert isinstance(ask, DeepModelAsk) and ask.hint == "balanced" and ask.meta()["deep_allowed_models"]
