"""QAR extends the hub's model-id prefix table from configuration: an OpenAI-compatible endpoint is configuration only."""
from hubofhubs.models.family import provider_name_for_model
from hubofhubs.models.router import MultiProvider
from quest_ai_runner import providers


def test_an_extra_prefix_comes_from_configuration_and_the_router_uses_the_same_table(monkeypatch):
    """LiteLLM, vLLM or Ollama: OPENAI_BASE_URL points the provider at it, QAR_OPENAI_MODEL_PREFIXES routes its ids."""
    monkeypatch.setattr(providers, "MODEL_PREFIXES", dict(providers.MODEL_PREFIXES))
    added = providers.route_extra_model_prefixes("openai", {"QAR_OPENAI_MODEL_PREFIXES": "Llama, qwen"})
    assert added == ["llama", "qwen"]
    assert provider_name_for_model("llama3.1:8b", {"openai"}, providers.MODEL_PREFIXES) == "openai"
    assert provider_name_for_model("llama3.1:8b", {"openai"}) is None            # the hub's own table is unchanged
    primary, extra = object(), object()
    multi = MultiProvider(primary, {"openai": extra}, prefixes=providers.MODEL_PREFIXES)
    assert multi.provider_for("qwen2.5") is extra and multi.provider_for("other-1") is primary


def test_the_openai_provider_takes_an_endpoint_from_the_environment_and_deepseek_keeps_its_own():
    p = providers.make_provider("openai", {"OPENAI_BASE_URL": "http://hub.invalid/v1"})
    assert (p.base_url, p.keyless, p.api_key) == ("http://hub.invalid/v1", True, None)
    ds = providers.make_provider("deepseek", {"OPENAI_BASE_URL": "http://hub.invalid/v1", "DEEPSEEK_API_KEY": "x"})
    assert "hub.invalid" not in (ds.base_url or "")
