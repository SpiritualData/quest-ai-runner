"""The goal-verification JUDGE tier and its risk-managed fallback.

``_verify_goal`` is the run's risk gate: its verdict decides done vs needs_you/failed and whether a
reply's completion claims are honest. It now runs at ``OrchestratorConfig.verify_tier`` (default
"best" — spend the strong model on judgment), and if the strong-tier call fails or returns an
unusable verdict it retries ONCE at ``planner_tier`` (the previous judge) instead of silently
degrading to "no verification". The answer goal loop also verifies against the turn's DERIVED GOAL
CONDITION (the checkable done-standard from Step 1 / Fix 13), not just the plan's goal restatement.

Also covers the CLI env wiring for the overseer (the simulated-conscious judge) and the verify
tier, which previously could not be configured from the environment at all.

Offline, no network.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval


class _PlanRecordingProvider(StubProvider):
    """Records every model id passed to plan(); optionally raises for chosen models."""

    def __init__(self, decisions: List[Dict[str, Any]], raise_for_models: List[str] | None = None,
                 answer_replies: List[str] | None = None):
        super().__init__(decisions=decisions)
        self.plan_models: List[str] = []
        self.raise_for_models = list(raise_for_models or [])
        self._answer_replies = list(answer_replies or [])
        self.answer_models: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        self.plan_models.append(model)
        if model in self.raise_for_models:
            self.plan_calls += 1
            self.plan_prompts.append(prompt)
            raise RuntimeError(f"provider cannot serve {model}")
        return super().plan(prompt, model=model, tool_schema=tool_schema)

    def answer(self, messages, *, model, system=None) -> str:
        self.answer_models.append(model)
        if self._answer_replies:
            self.answer_calls += 1
            return self._answer_replies.pop(0)
        return super().answer(messages, model=model, system=system)


def _orch(provider, **kw):
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), **kw)


def test_verify_goal_runs_at_best_tier_by_default():
    provider = _PlanRecordingProvider(decisions=[{"met": True, "reason": "done"}])
    orch = _orch(provider)
    verdict = orch._verify_goal("the goal", "the brief", "the worker output")
    assert verdict is not None and verdict["met"] is True
    registry = ModelRegistry(provider)
    assert provider.plan_models == [registry.resolve_tier("best")]


def test_verify_goal_falls_back_to_planner_tier_when_best_fails():
    # The strong-tier call raising must NOT lose the goal/claims gate: the judge retries once at
    # planner_tier and that verdict is used.
    registry = ModelRegistry(StubProvider(decisions=[]))
    best = registry.resolve_tier("best")
    provider = _PlanRecordingProvider(
        decisions=[{"met": False, "reason": "still missing the summary"}],
        raise_for_models=[best])
    orch = _orch(provider)
    verdict = orch._verify_goal("the goal", "the brief", "the worker output")
    assert verdict is not None and verdict["met"] is False
    assert provider.plan_models == [best, registry.resolve_tier("balanced")]


def test_verify_goal_falls_back_on_unusable_verdict_too():
    # A strong-tier response with no "met" key (e.g. a provider that can't serve the model and
    # returns an error string) also falls back to planner_tier rather than returning None.
    provider = _PlanRecordingProvider(decisions=[
        {"unexpected": "shape"},                 # best-tier reply: unusable
        {"met": True, "reason": "fine"},         # planner-tier retry: usable
    ])
    orch = _orch(provider)
    verdict = orch._verify_goal("the goal", "the brief", "the worker output")
    assert verdict is not None and verdict["met"] is True
    registry = ModelRegistry(provider)
    assert provider.plan_models == [registry.resolve_tier("best"),
                                    registry.resolve_tier("balanced")]


def test_verify_tier_empty_uses_planner_tier_once():
    # verify_tier="" restores the previous behavior: ONE call at planner_tier, no double call.
    provider = _PlanRecordingProvider(decisions=[{"met": True, "reason": "done"}])
    orch = _orch(provider, config=OrchestratorConfig(verify_tier=""))
    verdict = orch._verify_goal("the goal", "the brief", "the worker output")
    assert verdict is not None and verdict["met"] is True
    registry = ModelRegistry(provider)
    assert provider.plan_models == [registry.resolve_tier("balanced")]


def test_answer_verification_checks_the_derived_goal_condition():
    # Step 1 derives a checkable done-standard for the turn; the answer goal loop must verify
    # against THAT condition, not only the plan's own goal restatement.
    condition = "CONDITION-XYZ: the reply states the current subscription price in dollars"
    provider = _PlanRecordingProvider(
        decisions=[
            {"action": "answer", "rationale": "have it"},
            {"met": True, "reason": "states the price"},
        ],
        # answer() call order: (1) the Fix-13 goal-condition derivation, (2) the real answer.
        answer_replies=[condition, "The price is $9/mo."])
    res = _orch(provider).run("What's the price?")
    assert res.kind == "answer"
    verify_prompts = [p for p in provider.plan_prompts if "verifying whether" in p]
    assert verify_prompts, "the answer goal verification never ran"
    assert condition in verify_prompts[-1]


def test_env_wiring_for_overseer_and_verify_tier(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    for var in ("QAR_OVERSEER", "QAR_OVERSEER_TIER", "QAR_OVERSEER_MAX_SIGNALS",
                "QAR_VERIFY_TIER"):
        monkeypatch.delenv(var, raising=False)
    cfg = _config_from_env()
    assert cfg.orchestrator.overseer is False          # library default: off
    assert cfg.orchestrator.verify_tier == "best"      # library default: strong judge

    monkeypatch.setenv("QAR_OVERSEER", "true")
    monkeypatch.setenv("QAR_OVERSEER_TIER", "quality")
    monkeypatch.setenv("QAR_OVERSEER_MAX_SIGNALS", "5")
    monkeypatch.setenv("QAR_VERIFY_TIER", "balanced")
    cfg = _config_from_env()
    assert cfg.orchestrator.overseer is True
    assert cfg.orchestrator.overseer_tier == "quality"
    assert cfg.orchestrator.overseer_max_signals == 5
    assert cfg.orchestrator.verify_tier == "balanced"

    monkeypatch.setenv("QAR_OVERSEER", "off")
    monkeypatch.setenv("QAR_OVERSEER_MAX_SIGNALS", "not-a-number")
    cfg = _config_from_env()
    assert cfg.orchestrator.overseer is False
    assert cfg.orchestrator.overseer_max_signals == 3  # bad int ignored, default kept
