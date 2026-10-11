"""Difficulty-based STARTING model for unpinned deep runs (core/deep_model_selection.py).

The planner rates deep work simple / normal / hard on the planning call it already makes, and the
deep run starts on that difficulty's model (defaults haiku / sonnet / sonnet), then escalates up
the ladder on a not-met goal exactly as before. Pinned here:

  * the difficulty -> model mapping, including a start model missing from the ladder;
  * a pin (per-task model, guidance preference) always beats the automatic choice;
  * escalation still climbs from every starting rung;
  * QAR_DEEP_AUTO_MODEL=0 restores the old first-rung start;
  * a missing / unparseable rating falls back safely;
  * the env wiring, and the one status event that records the choice.

Fully offline.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult, PlanDecision
from quest_ai_runner.core.deep_model_selection import (
    normalize_difficulty,
    select_deep_start,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig, normalize_decision

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider

NOT_MET = {"met": False, "what_fell_short": "incomplete", "next_steps": "finish it"}


def deep_plan(difficulty: Optional[str], reason: str = "because") -> Dict[str, Any]:
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Do the work", "brief": "do it"}],
            "rationale": "deep"}
    if difficulty is not None:
        plan["deep_difficulty"] = difficulty
        plan["deep_difficulty_reason"] = reason
    return plan


class RecordingRunner:
    def __init__(self, results: Optional[List[DeepResult]] = None):
        self.results = list(results or [])
        self.models: List[Optional[str]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None, context_preamble: Optional[str] = None,
                 resume_session_id: Optional[str] = None) -> DeepResult:
        self.models.append(model)
        if self.results:
            return self.results.pop(0)
        return DeepResult(met=True, output="done")


class RecordingSink:
    def __init__(self):
        self.events = []

    def update(self, event, mode) -> None:
        self.events.append(event)


def build(provider, runner, **cfg) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=runner,
                        config=OrchestratorConfig(**cfg))


def run_models(difficulty: Optional[str], verdicts: List[Dict[str, Any]], *,
               model_hint: Optional[str] = None, **cfg) -> List[Optional[str]]:
    provider = ScriptedProvider(plans=[deep_plan(difficulty)], verdicts=verdicts)
    runner = RecordingRunner()
    build(provider, runner, **cfg).run("please do the thing", model_hint=model_hint)
    return runner.models


# --- the pure mapping --------------------------------------------------------------------------

def test_default_mapping_on_the_default_ladder():
    ladder = ["haiku", "sonnet", "opus"]
    assert select_deep_start("simple", ladder)[0] == ["haiku", "sonnet", "opus"]
    assert select_deep_start("normal", ladder)[0] == ["sonnet", "opus"]
    # Hard work STARTS on sonnet by default: opus is the escalation rung, not a starting point.
    assert select_deep_start("hard", ladder)[0] == ["sonnet", "opus"]


def test_selection_record_carries_difficulty_start_ladder_and_reason():
    _, sel = select_deep_start("simple", ["haiku", "sonnet", "opus"], reason="a status read")
    assert sel == {"difficulty": "simple", "start_model": "haiku",
                   "ladder": ["haiku", "sonnet", "opus"], "reason": "a status read"}


def test_configured_hard_model_is_honoured():
    ladder, _ = select_deep_start("hard", ["haiku", "sonnet", "opus"], {"hard": "opus"})
    assert ladder == ["opus"]


def test_rungs_match_by_what_the_worker_invokes():
    # "claude-sonnet-4-6" and the alias "sonnet" are the same rung.
    ladder, _ = select_deep_start("normal", ["claude-haiku-4-5", "claude-sonnet-4-6",
                                             "claude-opus-4-8"])
    assert ladder == ["claude-sonnet-4-6", "claude-opus-4-8"]


def test_a_start_model_missing_from_the_ladder_goes_to_the_nearest_stronger_rung():
    # An operator ladder without haiku never starts on haiku.
    ladder, sel = select_deep_start("simple", ["sonnet", "opus"])
    assert ladder == ["sonnet", "opus"]
    assert "not on the ladder" in sel["reason"]
    # Start model not on the ladder and nothing stronger on it: the whole ladder is kept.
    ladder, _ = select_deep_start("hard", ["haiku", "sonnet"], {"hard": "opus"})
    assert ladder == ["haiku", "sonnet"]


def test_unknown_difficulty_or_empty_ladder_means_no_selection():
    assert select_deep_start(None, ["haiku", "sonnet"]) is None
    assert select_deep_start("impossible", ["haiku", "sonnet"]) is None
    assert select_deep_start("simple", []) is None


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


# --- end to end through the goal loop ----------------------------------------------------------

def test_simple_work_starts_on_haiku_and_escalates_to_sonnet_and_stops_there():
    # The default ladder tops out at the balanced tier (sonnet); opus is never a default rung.
    assert run_models("simple", [{"met": True}]) == ["haiku"]
    assert run_models("simple", [NOT_MET, NOT_MET, {"met": True}]) == ["haiku", "sonnet", "sonnet"]


def test_normal_work_starts_on_sonnet_and_stays_at_the_ceiling():
    assert run_models("normal", [NOT_MET, {"met": True}]) == ["sonnet", "sonnet"]


def test_hard_work_starts_on_sonnet_by_default_and_stays_at_the_ceiling():
    assert run_models("hard", [NOT_MET, {"met": True}]) == ["sonnet", "sonnet"]


def test_configured_ladder_is_the_escalation_path():
    models = run_models("simple", [NOT_MET, {"met": True}],
                        deep_model_ladder=["haiku", "claude-sonnet-4-6", "claude-opus-4-8"])
    assert models == ["haiku", "claude-sonnet-4-6"]


def test_a_per_task_model_pin_beats_the_automatic_choice():
    # Rated simple, but the task pinned fable: fable runs, alone, with no escalation.
    assert run_models("simple", [NOT_MET, {"met": True}], model_hint="fable")[0] == "fable"
    assert set(run_models("simple", [NOT_MET, {"met": True}], model_hint="fable")) == {"fable"}


def test_a_per_task_tier_pin_also_beats_the_automatic_choice():
    provider = ScriptedProvider(plans=[deep_plan("simple")], verdicts=[{"met": True}])
    runner = RecordingRunner()
    build(provider, runner).run("x", model_hint="best")
    assert runner.models == [ModelRegistry(provider).resolve_tier("best")]


def test_disabled_restores_the_first_rung_start():
    models = run_models("hard", [{"met": True}], deep_auto_model=False,
                        deep_model_ladder=["haiku", "sonnet", "opus"])
    assert models == ["haiku"]


def test_disabled_with_no_ladder_uses_the_generic_fallback_ladder():
    provider = ScriptedProvider(plans=[], verdicts=[])
    orch = build(provider, RecordingRunner(), deep_auto_model=False)
    expected, pinned = orch.resolve_deep_ladder(None, None, "claude-opus-4-8")
    ladder, selection = orch.deep_model_plan(None, None, "claude-opus-4-8", difficulty="simple")
    assert (ladder, selection, pinned) == (expected, None, False)


def test_no_rating_on_a_configured_ladder_starts_at_the_normal_rung():
    models = run_models(None, [{"met": True}], deep_model_ladder=["haiku", "sonnet", "opus"])
    assert models == ["sonnet"]
    # ...which is exactly today's start on the common sonnet,opus ladder.
    assert run_models(None, [{"met": True}], deep_model_ladder=["sonnet", "opus"]) == ["sonnet"]


def test_no_rating_and_no_ladder_keeps_the_generic_fallback_ladder_unchanged():
    provider = ScriptedProvider(plans=[], verdicts=[])
    orch = build(provider, RecordingRunner())
    expected, _ = orch.resolve_deep_ladder(None, None, "claude-opus-4-8")
    ladder, selection = orch.deep_model_plan(None, None, "claude-opus-4-8", difficulty=None)
    assert ladder == expected and selection is None


def test_an_unparseable_rating_is_treated_as_no_rating():
    assert run_models("banana", [{"met": True}],
                      deep_model_ladder=["haiku", "sonnet", "opus"]) == ["sonnet"]


def test_the_choice_is_announced_once_with_structured_data():
    provider = ScriptedProvider(plans=[deep_plan("simple", reason="a status read")],
                                verdicts=[NOT_MET, {"met": True}])
    sink = RecordingSink()
    build(provider, RecordingRunner()).run("x", sink=sink)
    picks = [e for e in sink.events if "deep_model_selection" in (e.data or {})]
    assert len(picks) == 1
    assert picks[0].data["deep_model_selection"]["start_model"] == "haiku"
    assert picks[0].data["deep_model_selection"]["difficulty"] == "simple"
    assert "haiku" in picks[0].text and "—" not in picks[0].text


def test_a_pinned_run_announces_no_automatic_choice():
    provider = ScriptedProvider(plans=[deep_plan("simple")], verdicts=[{"met": True}])
    sink = RecordingSink()
    build(provider, RecordingRunner()).run("x", sink=sink, model_hint="fable")
    assert not [e for e in sink.events if "deep_model_selection" in (e.data or {})]


def test_plan_decision_defaults_to_no_rating():
    assert PlanDecision(action="deep").deep_difficulty is None


# --- env wiring --------------------------------------------------------------------------------

def test_env_wiring(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    monkeypatch.setenv("QAR_DEEP_AUTO_MODEL", "off")
    monkeypatch.setenv("QAR_DEEP_MODEL_HARD", "opus")
    monkeypatch.setenv("QAR_DEEP_MODEL_SIMPLE", "fast")   # a tier name: ignored
    cfg = _config_from_env()
    assert cfg.orchestrator.deep_auto_model is False
    assert cfg.orchestrator.deep_difficulty_models == {
        "simple": "haiku", "normal": "sonnet", "hard": "opus"}


def test_env_defaults(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    for var in ("QAR_DEEP_AUTO_MODEL", "QAR_DEEP_MODEL_SIMPLE", "QAR_DEEP_MODEL_NORMAL",
                "QAR_DEEP_MODEL_HARD"):
        monkeypatch.delenv(var, raising=False)
    cfg = _config_from_env()
    assert cfg.orchestrator.deep_auto_model is True
    assert cfg.orchestrator.deep_difficulty_models == {
        "simple": "haiku", "normal": "sonnet", "hard": "sonnet"}
