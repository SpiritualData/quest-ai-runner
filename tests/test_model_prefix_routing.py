"""Model ids route to providers by a data table, so a new vendor is one registered prefix and no routing code."""
from quest_ai_runner.core import model_family
from quest_ai_runner.adapters.multi_provider import MultiProvider


def test_shipped_prefixes_route_as_before():
    names = {"anthropic", "gemini", "openai", "deepseek"}
    route = model_family.provider_name_for_model
    assert route("claude-sonnet-4-6", names) == "anthropic"
    assert route("gemini-3.1-flash-lite", names) == "gemini"
    assert route("models/gemini-pro", names) == "gemini"
    assert route("gpt-4o", names) == "openai"
    assert route("deepseek-chat", names) == "deepseek"
    assert route("llama-3", names) is None
    assert route("claude-x", {"gemini"}) is None            # provider not registered: the caller uses its primary
    assert route("claude-x") == "anthropic"                  # without a filter the table answers alone


def test_a_new_vendor_is_one_registered_prefix(monkeypatch):
    monkeypatch.setattr(model_family, "MODEL_PREFIX_PROVIDERS", dict(model_family.MODEL_PREFIX_PROVIDERS))
    model_family.register_model_prefix("Newvendor", "newvendor")
    assert model_family.provider_name_for_model("newvendor-9", {"newvendor"}) == "newvendor"


def test_multi_provider_uses_the_same_table(monkeypatch):
    monkeypatch.setattr(model_family, "MODEL_PREFIX_PROVIDERS", dict(model_family.MODEL_PREFIX_PROVIDERS))
    model_family.register_model_prefix("newvendor", "newvendor")
    primary, extra = object(), object()
    multi = MultiProvider.__new__(MultiProvider)
    multi.providers = {"primary": primary, "newvendor": extra}
    multi.primary = primary
    assert multi._get_provider_for_model("newvendor-1") is extra
    assert multi._get_provider_for_model("other-1") is primary


def test_an_openai_compatible_endpoint_is_configuration_only(monkeypatch):
    """LiteLLM, vLLM or Ollama: OPENAI_BASE_URL points the provider at it, QAR_OPENAI_MODEL_PREFIXES routes its ids."""
    from quest_ai_runner.adapters.openai_provider import OpenAIProvider
    monkeypatch.setattr(model_family, "MODEL_PREFIX_PROVIDERS", dict(model_family.MODEL_PREFIX_PROVIDERS))
    monkeypatch.setenv("OPENAI_BASE_URL", "http://hub.invalid/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider()
    assert provider._base_url == "http://hub.invalid/v1"
    added = model_family.route_extra_model_prefixes("openai", {"QAR_OPENAI_MODEL_PREFIXES": "Llama, qwen"})
    assert added == ["llama", "qwen"]
    assert model_family.provider_name_for_model("llama3.1:8b", {"openai"}) == "openai"
    assert model_family.provider_name_for_model("qwen2.5", {"openai"}) == "openai"


def test_a_subclass_keeps_its_own_base_url(monkeypatch):
    from quest_ai_runner.adapters.deepseek_provider import DeepSeekProvider
    monkeypatch.setenv("OPENAI_BASE_URL", "http://hub.invalid/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert "hub.invalid" not in (DeepSeekProvider()._base_url or "")
