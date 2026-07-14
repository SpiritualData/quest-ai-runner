"""MultiProvider tier-downgrade fallback on quota/rate-limit errors."""
import pytest

from quest_ai_runner.adapters.multi_provider import MultiProvider


class FakeRegistry:
    """Minimal stand-in for ModelRegistry: a fixed tier -> model map."""

    def __init__(self, top):
        self._top = dict(top)

    def top_models(self):
        return dict(self._top)

    def resolve_tier(self, tier):
        return self._top.get(tier, self._top["balanced"])


class FlakyProvider:
    """A ModelProvider whose answer()/plan()/web_search() fail for configured models.

    ``fail_models`` maps model -> the exception to raise; any other model succeeds
    and returns a string naming which model actually answered.
    """

    def __init__(self, fail_models=None):
        self.fail_models = dict(fail_models or {})
        self.tokens_in = 0
        self.tokens_out = 0

    def _maybe_fail(self, model):
        if model in self.fail_models:
            raise self.fail_models[model]

    def plan(self, prompt, *, model, tool_schema, layers=None):
        self._maybe_fail(model)
        return {"action": "answer", "model": model}

    def answer(self, messages, *, model, system=None, layers=None):
        self._maybe_fail(model)
        return f"answered by {model}"

    def web_search(self, query, *, model, max_results=5):
        self._maybe_fail(model)
        return {"answer": f"answered by {model}", "results": []}

    def list_models(self):
        return list(self.fail_models.keys())


QUOTA_ERROR = Exception("429 RESOURCE_EXHAUSTED: quota exceeded for this model")
OTHER_ERROR = ValueError("the model blocked this response")

TOP = {
    "fast": "gemini-3.1-flash-lite",
    "balanced": "gemini-3.1-flash-lite",
    "quality": "gemini-3.5-flash",
    "best": "claude-opus-4-8",
}


def test_quota_error_steps_down_one_tier():
    provider = FlakyProvider(fail_models={"gemini-3.5-flash": QUOTA_ERROR})
    mp = MultiProvider(provider, registry=FakeRegistry(TOP))

    result = mp.answer([{"role": "user", "content": "hi"}], model="gemini-3.5-flash")

    assert result == "answered by gemini-3.1-flash-lite"


def test_quota_error_cascades_through_multiple_tiers():
    top = {
        "fast": "model-fast",
        "balanced": "model-balanced",
        "quality": "model-quality",
        "best": "model-best",
    }
    provider = FlakyProvider(fail_models={
        "model-best": QUOTA_ERROR,
        "model-quality": QUOTA_ERROR,
    })
    mp = MultiProvider(provider, registry=FakeRegistry(top))

    result = mp.plan("do something", model="model-best", tool_schema={})

    assert result == {"action": "answer", "model": "model-balanced"}


def test_bottom_tier_failure_still_raises():
    top = {"fast": "model-fast", "balanced": "model-fast", "quality": "model-fast", "best": "model-fast"}
    provider = FlakyProvider(fail_models={"model-fast": QUOTA_ERROR})
    mp = MultiProvider(provider, registry=FakeRegistry(top))

    with pytest.raises(Exception):
        mp.answer([{"role": "user", "content": "hi"}], model="model-fast")


def test_non_quota_error_does_not_fall_back():
    provider = FlakyProvider(fail_models={"gemini-3.5-flash": OTHER_ERROR})
    mp = MultiProvider(provider, registry=FakeRegistry(TOP))

    with pytest.raises(ValueError):
        mp.answer([{"role": "user", "content": "hi"}], model="gemini-3.5-flash")


def test_pinned_model_not_in_any_tier_does_not_fall_back():
    provider = FlakyProvider(fail_models={"some-pinned-model": QUOTA_ERROR})
    mp = MultiProvider(provider, registry=FakeRegistry(TOP))

    with pytest.raises(Exception):
        mp.answer([{"role": "user", "content": "hi"}], model="some-pinned-model")


def test_no_registry_attached_disables_fallback():
    provider = FlakyProvider(fail_models={"gemini-3.5-flash": QUOTA_ERROR})
    mp = MultiProvider(provider)  # no registry passed

    with pytest.raises(Exception):
        mp.answer([{"role": "user", "content": "hi"}], model="gemini-3.5-flash")


def test_set_tier_registry_enables_fallback_after_construction():
    provider = FlakyProvider(fail_models={"gemini-3.5-flash": QUOTA_ERROR})
    mp = MultiProvider(provider)
    mp.set_tier_registry(FakeRegistry(TOP))

    result = mp.answer([{"role": "user", "content": "hi"}], model="gemini-3.5-flash")

    assert result == "answered by gemini-3.1-flash-lite"


def test_web_search_falls_back_too():
    provider = FlakyProvider(fail_models={"gemini-3.5-flash": QUOTA_ERROR})
    mp = MultiProvider(provider, registry=FakeRegistry(TOP))

    result = mp.web_search("weather today", model="gemini-3.5-flash")

    assert result["answer"] == "answered by gemini-3.1-flash-lite"
