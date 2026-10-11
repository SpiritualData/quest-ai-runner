"""No model default may resolve to Opus; only an explicit request may.

A silent drift here changes cost on every chat turn. Opus is reachable only by an explicit per-task
pin (covered by ``test_deep_model_pin.test_an_explicit_opus_request_runs_opus``) or an explicit
``QAR_DEEP_MODELS`` / ``QAR_MODEL_*`` override, never by tier resolution or a built-in default.

Fully offline.
"""
from __future__ import annotations

from quest_ai_runner.core.deep_model_selection import (
    DEFAULT_AUTO_DEEP_LADDER,
    DEFAULT_DIFFICULTY_MODELS,
)
from quest_ai_runner.core.model_registry import DEFAULT_FALLBACK_TOP, bucket_top

LIVE_WITH_OPUS = [
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
]


def test_static_defaults_never_name_opus():
    assert not [m for m in DEFAULT_AUTO_DEEP_LADDER if "opus" in m.lower()]
    assert not [m for m in DEFAULT_DIFFICULTY_MODELS.values() if "opus" in m.lower()]
    assert not [m for m in DEFAULT_FALLBACK_TOP.values() if "opus" in m.lower()]


def test_tier_resolution_skips_opus_even_when_it_is_live():
    resolved = bucket_top(LIVE_WITH_OPUS)
    assert set(resolved) >= {"fast", "balanced", "quality", "best"}
    assert not {t: m for t, m in resolved.items() if "opus" in str(m).lower()}


def test_tier_resolution_with_no_live_models_never_names_opus():
    assert not [m for m in bucket_top([]).values() if "opus" in str(m).lower()]


def test_an_explicit_override_still_reaches_opus():
    """The other half: an explicit choice is honored, so the guard does not forbid Opus outright."""
    assert bucket_top(LIVE_WITH_OPUS, {"best": "claude-opus-5-5"})["best"] == "claude-opus-5-5"
