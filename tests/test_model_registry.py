"""model_registry bucketing — tier -> live top model id."""
from quest_ai_runner.core.model_registry import (
    DEFAULT_FALLBACK_TOP,
    ModelRegistry,
    bucket_top,
)


def test_bucket_takes_latest_first_of_each_family():
    # Latest-first list: the FIRST opus/sonnet/haiku seen wins.
    models = [
        "claude-opus-4-9", "claude-opus-4-8",
        "claude-sonnet-4-7", "claude-sonnet-4-6",
        "claude-haiku-4-6", "claude-haiku-4-5",
    ]
    top = bucket_top(models)
    assert top == {"opus": "claude-opus-4-9", "sonnet": "claude-sonnet-4-7",
                   "haiku": "claude-haiku-4-6"}


def test_bucket_fills_missing_families_from_fallback():
    # Only opus present live -> sonnet/haiku come from the fallback map.
    top = bucket_top(["claude-opus-9-9"])
    assert top["opus"] == "claude-opus-9-9"
    assert top["sonnet"] == DEFAULT_FALLBACK_TOP["sonnet"]
    assert top["haiku"] == DEFAULT_FALLBACK_TOP["haiku"]


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
    assert reg.resolve_tier("opus") == DEFAULT_FALLBACK_TOP["opus"]


def test_registry_survives_provider_exception():
    class Boom:
        def plan(self, *a, **k): ...
        def answer(self, *a, **k): ...
        def list_models(self):
            raise RuntimeError("network down")

    reg = ModelRegistry(Boom())
    # Must not raise; falls back.
    assert reg.resolve_tier("sonnet") == DEFAULT_FALLBACK_TOP["sonnet"]
