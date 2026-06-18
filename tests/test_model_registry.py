"""model_registry bucketing — tier -> live top model id."""
import pytest

from quest_ai_runner.core.model_registry import (
    DEFAULT_FALLBACK_TOP,
    ModelRegistry,
    bucket_top,
    is_vision_capable,
)


def test_bucket_takes_latest_first_of_each_family():
    # Latest-first list: the FIRST opus/sonnet/haiku seen wins.
    # Semantic tiers: opus -> quality, sonnet -> balanced, haiku -> fast.
    models = [
        "claude-opus-4-9", "claude-opus-4-8",
        "claude-sonnet-4-7", "claude-sonnet-4-6",
        "claude-haiku-4-6", "claude-haiku-4-5",
    ]
    top = bucket_top(models)
    assert top["quality"] == "claude-opus-4-9"
    assert top["balanced"] == "claude-sonnet-4-7"
    assert top["fast"] == "claude-haiku-4-6"


def test_bucket_fills_missing_families_from_fallback():
    # Only opus present live -> balanced/fast come from the fallback map.
    top = bucket_top(["claude-opus-9-9"])
    assert top["quality"] == "claude-opus-9-9"
    assert top["balanced"] == DEFAULT_FALLBACK_TOP["balanced"]
    assert top["fast"] == DEFAULT_FALLBACK_TOP["fast"]


def test_registry_resolves_tiers_from_provider():
    class P:
        def plan(self, *a, **k): ...
        def answer(self, *a, **k): ...
        def list_models(self):
            return ["claude-opus-5-0", "claude-sonnet-5-0", "claude-haiku-5-0"]

    reg = ModelRegistry(P())
    assert reg.resolve_tier("opus") == "claude-opus-5-0"
    assert reg.resolve_tier("haiku") == "claude-haiku-5-0"
    # Unknown / None defaults to sonnet.
    assert reg.resolve_tier(None) == "claude-sonnet-5-0"
    assert reg.resolve_tier("banana") == "claude-sonnet-5-0"


def test_registry_falls_back_when_list_empty():
    class Empty:
        def plan(self, *a, **k): ...
        def answer(self, *a, **k): ...
        def list_models(self):
            return []

    reg = ModelRegistry(Empty())
    # "opus" tier-name maps to "quality" via backward-compat tier_map
    assert reg.resolve_tier("opus") == DEFAULT_FALLBACK_TOP["quality"]


def test_registry_survives_provider_exception():
    class Boom:
        def plan(self, *a, **k): ...
        def answer(self, *a, **k): ...
        def list_models(self):
            raise RuntimeError("network down")

    reg = ModelRegistry(Boom())
    # Must not raise; falls back. "sonnet" maps to "balanced".
    assert reg.resolve_tier("sonnet") == DEFAULT_FALLBACK_TOP["balanced"]


# --- vision-capability seam -------------------------------------------------

@pytest.mark.parametrize("model", [
    # Anthropic Claude 3.x / 4.x, all tiers + CLI aliases + the registry's fallbacks.
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-haiku-20240307",
    "claude-sonnet-4-6",
    "claude-opus-4-8",
    "claude-haiku-4-5",
    "opus", "sonnet", "haiku",
    DEFAULT_FALLBACK_TOP["best"],
    DEFAULT_FALLBACK_TOP["balanced"],
    DEFAULT_FALLBACK_TOP["fast"],
    # Google Gemini 1.5 / 2.x / 3.x.
    "gemini-1.5-pro", "gemini-2.0-flash", "gemini-3-pro",
    # OpenAI multimodal families.
    "gpt-4o", "gpt-4o-mini", "gpt-4.1", "o1", "o3-mini", "o4",
])
def test_is_vision_capable_true_for_known_vision_families(model):
    assert is_vision_capable(model) is True


@pytest.mark.parametrize("model", [
    "",
    None,
    "claude-2.1",            # pre-3 Claude: text only
    "claude-instant-1.2",
    "gpt-3.5-turbo",
    "gpt-4-0613",            # original gpt-4 (not 4o/4.1): text only here
    "text-embedding-3-large",
    "some-unknown-model",
    "llama-3-70b",
])
def test_is_vision_capable_false_for_text_only_or_unknown(model):
    assert is_vision_capable(model) is False
