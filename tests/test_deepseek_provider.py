"""DeepSeek provider: routing, opt-in registration, tier safety, client wiring. No network."""
import sys
import types

import pytest

from quest_ai_runner.adapters.deepseek_provider import DeepSeekProvider, DEFAULT_BASE_URL
from quest_ai_runner.adapters.multi_provider import MultiProvider
from quest_ai_runner.core.model_registry import bucket_top


class Marker:
    def __init__(self, name):
        self.name = name

    def list_models(self):
        return []


def fake_openai(monkeypatch, captured):
    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)
    mod = types.ModuleType("openai")
    mod.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", mod)


def test_provider_passes_base_url_and_key(monkeypatch):
    captured = {}
    fake_openai(monkeypatch, captured)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    DeepSeekProvider(api_key="k-test")._get_client()
    assert captured == {"api_key": "k-test", "base_url": DEFAULT_BASE_URL}


def test_base_url_env_override(monkeypatch):
    captured = {}
    fake_openai(monkeypatch, captured)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid")
    DeepSeekProvider(api_key="k")._get_client()
    assert captured["base_url"] == "https://example.invalid"


def test_missing_key_message_names_deepseek(monkeypatch):
    fake_openai(monkeypatch, {})
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider()._get_client()


def test_thinking_off_by_default_and_opt_in(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    p = DeepSeekProvider(api_key="k")
    assert p.extra_create_kwargs() == {"extra_body": {"thinking": {"type": "disabled"}}}
    monkeypatch.setenv("DEEPSEEK_THINKING", "1")
    assert p.extra_create_kwargs() == {}


def test_multiprovider_routes_deepseek_ids():
    ds, oa, primary = Marker("ds"), Marker("oa"), Marker("primary")
    mp = MultiProvider(primary, {"deepseek": ds, "openai": oa})
    assert mp._get_provider_for_model("deepseek-flash") is ds
    assert mp._get_provider_for_model("deepseek-v4-pro") is ds
    assert mp._get_provider_for_model("gpt-4o") is oa
    assert MultiProvider(primary, {"openai": oa})._get_provider_for_model("deepseek-flash") is primary


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
    from quest_ai_runner.config import register_deepseek
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    reg = {}
    register_deepseek(reg)
    assert reg == {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k-test")
    register_deepseek(reg)
    assert isinstance(reg["deepseek"], DeepSeekProvider)
