"""DeepSeek provider in QAR: opt-in registration, tier safety, the environment it is built from. No network (the arm itself is tested in the hub)."""
import types

from hubofhubs.models.openai import DeepSeekProvider
from quest_ai_runner.core.model_registry import bucket_top


class Marker:
    def __init__(self, name):
        self.name = name

    def list_models(self):
        return []


def test_orchestrator_routes_deepseek_ids():
    from quest_ai_runner.core.orchestrator import Orchestrator
    ds, primary = Marker("ds"), Marker("primary")
    fake = types.SimpleNamespace(
        provider=primary, registry=types.SimpleNamespace(_providers={"deepseek": ds}))
    assert Orchestrator.get_provider_for_model(fake, "deepseek-flash") is ds
    fake.registry._providers = {}
    assert Orchestrator.get_provider_for_model(fake, "deepseek-flash") is primary


def test_bucket_top_ignores_deepseek_ids():
    base = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8",
            "gemini-2.5-flash", "gemini-2.5-pro", "gpt-4o"]
    ds = ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat"]
    assert bucket_top(base + ds, {}) == bucket_top(base, {})
    assert bucket_top(base + ds, {}) == bucket_top(base + ds[::-1], {})
    assert all("deepseek" not in v for v in bucket_top(ds + base, {}).values())


def test_bucket_top_honours_explicit_pin():
    top = bucket_top(["deepseek-flash"], {"fast": "deepseek-flash"})
    assert top["fast"] == "deepseek-flash"


def test_registered_only_when_key_set(monkeypatch):
    from quest_ai_runner.config_resolvers import register_deepseek
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    reg = {}
    register_deepseek(reg)
    assert reg == {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k-test")
    register_deepseek(reg)
    assert isinstance(reg["deepseek"], DeepSeekProvider)


def test_the_provider_is_built_from_the_environment(monkeypatch):
    from quest_ai_runner.providers import make_provider
    env = {"DEEPSEEK_API_KEY": "k-test", "DEEPSEEK_BASE_URL": "https://example.invalid", "DEEPSEEK_THINKING": "1"}
    p = make_provider("deepseek", env)
    assert (p.api_key, p.base_url, p.create_kwargs) == ("k-test", "https://example.invalid", {})
    assert make_provider("deepseek", {"DEEPSEEK_API_KEY": "k"}).create_kwargs == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_a_provider_that_cannot_be_built_does_not_stop_the_others(monkeypatch):
    import quest_ai_runner.config_resolvers as resolvers
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k-test")

    def broken(name):
        raise ImportError("openai is not installed")
    monkeypatch.setattr(resolvers, "make_provider", broken)
    reg = {"anthropic": object()}
    resolvers.register_deepseek(reg)
    assert list(reg) == ["anthropic"]
