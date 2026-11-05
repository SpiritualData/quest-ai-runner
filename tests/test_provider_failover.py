"""Provider failover through the router QAR builds: a vendor with no credit, over quota or down does not end the turn when another
configured vendor can answer. Offline, fake providers, no model call (``fallback_models`` is the hub router's ordered failover list)."""
import pytest

from quest_ai_runner.the_body.configuration import config_orchestrator
from quest_ai_runner.the_body.configuration.config import RunnerConfig
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.providers import failover_models, routed


class FakeVendor:
    def __init__(self, name, key="k", error=None, fallback_models=()):
        self.name, self.api_key, self.error, self.fallback_models = name, key, error, fallback_models
        self.tokens_in = self.tokens_out = 0
        self.asked = []

    def answer(self, messages, *, model, system=None, layers=None, reasoning=None, step=None):
        self.asked.append(model)
        if self.error:
            raise self.error
        return f"{self.name} answered with {model}"

    def plan(self, prompt, *, model, tool_schema, layers=None, step=None):
        self.asked.append(model)
        if self.error:
            raise self.error
        return {"action": "answer", "rationale": self.name}

    def list_models(self):
        return []


CREDIT = RuntimeError("Error code: 400 - {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API.'}")


class FakeAnthropic(FakeVendor):
    pass


class FakeGemini(FakeVendor):
    pass


class FakeOpenAI(FakeVendor):
    pass


class FakeDeepSeek(FakeVendor):
    pass


def vendors(**errors):
    """One distinct class per vendor, as the real ones are (the router excludes the primary's vendor by type)."""
    return {"anthropic": FakeAnthropic("anthropic", error=errors.get("anthropic")), "gemini": FakeGemini("gemini", error=errors.get("gemini")),
            "openai": FakeOpenAI("openai", key=None, fallback_models=("gpt-4o",)), "deepseek": FakeDeepSeek("deepseek", key=None)}


def router(providers, primary="anthropic", pins=None, env=None):
    models = failover_models(providers, providers[primary], pins or {}, env or {})
    return routed(providers[primary], providers, fallback_models=models)


def ask(provider, model="claude-sonnet-4-5"):
    return provider.answer([{"role": "user", "content": "hi"}], model=model)


def test_an_empty_anthropic_balance_is_answered_by_gemini():
    providers = vendors(anthropic=CREDIT)
    assert ask(router(providers)) == "gemini answered with gemini-3.5-flash"
    assert providers["anthropic"].asked == ["claude-sonnet-4-5"]


def test_planning_fails_over_too():
    providers = vendors(anthropic=CREDIT)
    decision = router(providers).plan("p", model="claude-sonnet-4-5", tool_schema={})
    assert decision["rationale"] == "gemini"


def test_the_failed_model_is_skipped_for_the_next_turns_not_asked_again_at_once():
    providers = vendors(anthropic=CREDIT)
    provider = router(providers)
    ask(provider), ask(provider)
    assert providers["anthropic"].asked == ["claude-sonnet-4-5"] and len(providers["gemini"].asked) == 2


def test_quota_and_outage_errors_fail_over_as_well():
    for error in (RuntimeError("429 RESOURCE_EXHAUSTED quota"), RuntimeError("529 overloaded_error"), TimeoutError("request timed out")):
        assert ask(router(vendors(anthropic=error))).startswith("gemini answered")


def test_a_second_configured_vendor_is_tried_when_the_first_is_also_down():
    providers = vendors(anthropic=CREDIT, gemini=RuntimeError("503 service unavailable"))
    providers["openai"].api_key = "o"
    assert ask(router(providers)) == "openai answered with gpt-4o"


def test_a_bad_key_or_request_is_raised_not_hidden_by_another_vendor():
    providers = vendors(anthropic=RuntimeError("401 invalid x-api-key"))
    with pytest.raises(RuntimeError, match="401"):
        ask(router(providers))
    assert providers["gemini"].asked == []


def test_with_no_other_vendor_configured_the_error_is_raised():
    providers = vendors(anthropic=CREDIT)
    providers["gemini"].api_key = None
    with pytest.raises(RuntimeError, match="credit balance"):
        ask(router(providers))


def test_the_failover_list_names_one_model_per_other_configured_vendor_and_honours_pins_and_the_variable():
    providers = vendors()
    providers["openai"].api_key = "o"
    assert failover_models(providers, providers["anthropic"], {}, {}) == ["gemini-3.5-flash", "gpt-4o"]
    assert failover_models(providers, providers["gemini"], {}, {}) == ["gpt-4o", "claude-sonnet"]
    assert failover_models(providers, providers["anthropic"], {"best": "gemini-9-pro"}, {})[0] == "gemini-9-pro"
    assert failover_models(providers, providers["anthropic"], {}, {"QAR_FALLBACK_MODELS": "gpt-5, gemini-x"}) == ["gpt-5", "gemini-x"]


def test_build_orchestrators_wiring_gives_the_router_its_failover_models(monkeypatch):
    from hubofhubs.api import AnthropicProvider
    for variable in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_BASE_URL", "QAR_FALLBACK_MODELS"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    cfg = RunnerConfig()
    cfg.model_provider = AnthropicProvider(api_key="k", env_name="ANTHROPIC_API_KEY")
    config_orchestrator.wire_providers(cfg)
    assert cfg.model_provider.fallback_models == ("gemini-3.5-flash",)
