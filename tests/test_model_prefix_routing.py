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
