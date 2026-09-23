"""A per-task model REQUEST names a model, and the deep worker must run that model.

The live failure: a quest pinned ``fable`` (its ``autopilot.model``), every deep task ran on the
deployment's balanced model instead, and the runner logged the substitution as though it were the
request being honoured ("pinned to 'sonnet' (explicit per-task model request)"). Nothing said the
name had been thrown away.

The cause was an impedance mismatch, not a typo. One field carries EITHER a tier name ("best") OR a
concrete model id ("fable"), and it was resolved as if it were always a tier: ``resolve_tier``
understands the four tiers plus the legacy haiku/sonnet/opus aliases and silently rewrites anything
else to "balanced". The deep ladder then pinned that rewritten value, so the configured
``deep_model_ladder`` was never consulted either.

Pinned here: a model id survives verbatim to the worker, a tier name still resolves through the
registry exactly as before, and the silent substitution now says so in the log.

Fully offline.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider

PLAN = {"action": "deep", "goal": "Do the work",
        "deep_subtasks": [{"goal": "Write the brief", "brief": "write it"}],
        "rationale": "deep"}


class RecordingRunner:
    def __init__(self, results: Optional[List[DeepResult]] = None):
        self._results = list(results or [])
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None, context_preamble: Optional[str] = None,
                 resume_session_id: Optional[str] = None) -> DeepResult:
        self.calls.append({"model": model})
        if self._results:
            return self._results.pop(0)
        return DeepResult(met=True, output="done")


def _orch(provider, runner, **cfg):
    return Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        deep_runner=runner, config=OrchestratorConfig(**cfg))


# --- what the registry does, and now admits to doing --------------------------------------------

def test_is_tier_name_separates_a_tier_from_a_model_id():
    assert ModelRegistry.is_tier_name("best") is True
    assert ModelRegistry.is_tier_name("balanced") is True
    # The legacy provider-specific aliases are TIER names: "opus" means "the quality tier here".
    assert ModelRegistry.is_tier_name("opus") is True
    assert ModelRegistry.is_tier_name("SONNET") is True
    # These are model ids. Resolving them as tiers is what silently substituted a different model.
    assert ModelRegistry.is_tier_name("fable") is False
    assert ModelRegistry.is_tier_name("claude-opus-4-8") is False
    assert ModelRegistry.is_tier_name(None) is False
    assert ModelRegistry.is_tier_name("  ") is False


def test_resolving_a_model_id_as_a_tier_now_warns_once(caplog):
    reg = ModelRegistry(ScriptedProvider(plans=[], verdicts=[]))
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.model_registry"):
        first = reg.resolve_tier("fable")
        reg.resolve_tier("fable")
        reg.resolve_tier("fable")

    assert first == reg.resolve_tier("balanced"), "the substitution itself is unchanged"
    warnings = [r for r in caplog.records if "fable" in r.getMessage()]
    assert len(warnings) == 1, "named once, not on every call"
    assert "is not a known tier" in warnings[0].getMessage()


def test_a_real_tier_name_never_warns(caplog):
    reg = ModelRegistry(ScriptedProvider(plans=[], verdicts=[]))
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.model_registry"):
        for tier in ("fast", "balanced", "quality", "best", "haiku", "sonnet", "opus", None):
            reg.resolve_tier(tier)

    assert not caplog.records


# --- what the deep worker is actually launched with ---------------------------------------------

def test_a_per_task_model_id_reaches_the_worker_verbatim():
    """The regression: ``fable`` asked for, ``fable`` run."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = RecordingRunner()

    _orch(provider, runner).run("write today's brief", model_hint="fable")

    assert [c["model"] for c in runner.calls] == ["fable"]


def test_a_per_task_TIER_still_resolves_through_the_registry():
    """Unchanged: a tier is a question for the deployment's tier map, not a model id."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = RecordingRunner()

    _orch(provider, runner).run("build X", model_hint="best")

    resolved = ModelRegistry(provider).resolve_tier("best")
    assert [c["model"] for c in runner.calls] == [resolved]
    assert "fable" not in (runner.calls[0]["model"] or "")


def test_the_legacy_aliases_are_tiers_not_pins():
    """``opus`` has always meant "the quality tier", and a deployment may map that anywhere. Taking
    it literally here would quietly repoint every task that uses the old spelling."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = RecordingRunner()

    _orch(provider, runner).run("build X", model_hint="opus")

    assert [c["model"] for c in runner.calls] == [ModelRegistry(provider).resolve_tier("opus")]


def test_a_model_the_deep_worker_cannot_run_is_never_passed_through():
    """The deep worker is Claude Code. Handing it a Gemini/OpenAI id makes it exit having done
    nothing, so "honour the id verbatim" must apply ONLY to ids this worker can actually invoke.
    Anything else keeps the old behaviour: the hint resolved through the registry."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = RecordingRunner()

    _orch(provider, runner, deep_model_ladder=["sonnet", "opus"]).run(
        "build X", model_hint="gemini-3.5-flash")

    got = runner.calls[0]["model"]
    assert got != "gemini-3.5-flash", "Claude Code cannot run it, so it must not be pinned"
    assert got == ModelRegistry(provider).resolve_tier("gemini-3.5-flash")
