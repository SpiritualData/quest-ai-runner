"""WS3: the deep-worker model ESCALATION LADDER must do something real, even on a deployment
whose primary/session model is not Claude (the deep worker is always Claude Code, so it can only
ever run Claude models -- see HANDS_FREE_QUEST_AI_DESIGN.md section 2, point 4).

Covers:
  * ``QAR_DEEP_MODELS`` env wiring builds an explicit ladder (cli.py's ``_config_from_env``),
    and is left unset (None) rather than defaulting to bare tier names that are never
    Claude-runnable.
  * ``Orchestrator._deep_models`` uses a configured ``deep_model_ladder`` as-is.
  * When no ladder is configured and the resolved ``fallback`` model is not Claude-runnable,
    ``_deep_models`` extends the ladder with any Claude-runnable id it can resolve from the
    "quality"/"best" tiers (e.g. an operator override like QAR_MODEL_BEST=claude-opus-4-8).
  * A WARNING is logged when the resolved (non-pinned) ladder still comes out length <= 1.

Offline, no network.
"""
import logging

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval

ORCH_LOGGER = "quest-ai-runner.orchestrator"


def _orch(provider, **kw):
    registry = kw.pop("registry", None) or ModelRegistry(provider)
    return Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=registry, **kw)


# --- cli.py env wiring -------------------------------------------------------------------------

def test_qar_deep_models_env_builds_explicit_ladder(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    monkeypatch.setenv("QAR_DEEP_MODELS", "claude-sonnet-4-6, claude-opus-4-8")
    cfg = _config_from_env()
    assert cfg.orchestrator.deep_model_ladder == ["claude-sonnet-4-6", "claude-opus-4-8"]


def test_qar_deep_models_unset_leaves_ladder_none(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    monkeypatch.delenv("QAR_DEEP_MODELS", raising=False)
    cfg = _config_from_env()
    # Must NOT default to bare semantic tier names ("fast,balanced,quality,best") -- those are
    # never Claude-runnable and used to make the ladder silently inert. Left unset entirely so
    # Orchestrator._deep_models resolves a real one at run time.
    assert cfg.orchestrator.deep_model_ladder is None


# --- Orchestrator._deep_models ------------------------------------------------------------------

def test_deep_models_uses_configured_ladder_as_is():
    provider = StubProvider(decisions=[])
    orch = _orch(provider, config=OrchestratorConfig(
        deep_model_ladder=["claude-sonnet-4-6", "claude-opus-4-8"]))
    ladder = orch._deep_models(None, None, "gemini-3.5-flash")
    assert ladder == ["claude-sonnet-4-6", "claude-opus-4-8"]


def test_deep_models_pin_from_model_hint_bypasses_ladder():
    # An explicit per-task model request that IS Claude-runnable pins a single model, even though
    # a ladder is configured -- no auto-escalation when the caller asked for a specific model.
    provider = StubProvider(decisions=[])
    orch = _orch(provider, config=OrchestratorConfig(
        deep_model_ladder=["claude-sonnet-4-6", "claude-opus-4-8"]))
    ladder = orch._deep_models("claude-opus-4-8", None, "claude-opus-4-8")
    assert ladder == ["claude-opus-4-8"]


def test_deep_models_fallback_resolves_claude_id_from_tier_fallback_on_non_claude_session(caplog):
    # Simulate a Gemini deployment (session/primary model is Gemini) that still has an operator
    # override naming a real Claude id for its "best" tier (the documented QAR_MODEL_BEST
    # workaround) -- escalation must find and use it even with no explicit deep_model_ladder.
    provider = StubProvider(decisions=[])
    registry = ModelRegistry(provider, fallback={
        "fast": "gemini-3.1-flash-lite",
        "balanced": "gemini-3.5-flash",
        "quality": "gemini-3.5-flash",
        "best": "claude-opus-4-8",
    })
    orch = _orch(provider, registry=registry, config=OrchestratorConfig())
    with caplog.at_level(logging.INFO, logger=ORCH_LOGGER):
        ladder = orch._deep_models(None, None, "gemini-3.5-flash")
    assert ladder == ["gemini-3.5-flash", "claude-opus-4-8"]
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a real Claude id was found for escalation; no warning should fire")


def test_deep_models_warns_when_no_claude_id_available_anywhere(caplog):
    # A deployment with NO Claude id configured for ANY tier: escalation is genuinely unavailable,
    # and this must be surfaced loudly (WARNING), not silently swallowed.
    provider = StubProvider(decisions=[])
    registry = ModelRegistry(provider, fallback={
        "fast": "gemini-3.1-flash-lite",
        "balanced": "gemini-3.5-flash",
        "quality": "gemini-3.5-flash",
        "best": "gemini-3.5-flash",  # overrides the library's own Claude default for "best"
    })
    orch = _orch(provider, registry=registry, config=OrchestratorConfig())
    with caplog.at_level(logging.INFO, logger=ORCH_LOGGER):
        ladder = orch._deep_models(None, None, "gemini-3.5-flash")
    assert ladder == ["gemini-3.5-flash"]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a WARNING naming why escalation is unavailable"
    assert "Escalation unavailable" in warnings[0].getMessage()
    assert "QAR_DEEP_MODELS" in warnings[0].getMessage()


def test_deep_models_pinned_single_model_does_not_warn(caplog):
    # A PIN (explicit request or guidance pref) is an intentional single-model choice, not an
    # escalation failure -- must not trigger the "escalation unavailable" warning.
    provider = StubProvider(decisions=[])
    orch = _orch(provider, config=OrchestratorConfig())
    with caplog.at_level(logging.INFO, logger=ORCH_LOGGER):
        ladder = orch._deep_models("claude-opus-4-8", None, "claude-opus-4-8")
    assert ladder == ["claude-opus-4-8"]
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
