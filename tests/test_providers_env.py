"""QAR's wiring of the hub's providers: which environment variable feeds which argument, and that a missing key names its variable."""
import pytest

from quest_ai_runner.providers import make_provider


def test_each_vendor_key_variable_feeds_its_provider_and_empty_means_unset():
    assert make_provider("anthropic", {"ANTHROPIC_API_KEY": "k"}).api_key == "k"
    assert make_provider("anthropic", {"ANTHROPIC_API_KEY": ""}).api_key is None
    assert make_provider("gemini", {"GOOGLE_API_KEY": "g"}).api_key == "g"
    assert make_provider("openai", {"OPENAI_API_KEY": "o"}).api_key == "o"
    assert make_provider("deepseek", {"DEEPSEEK_API_KEY": "d"}).api_key == "d"


def test_thinking_flags_and_the_openai_base_url_come_from_the_environment():
    assert make_provider("anthropic", {"QAR_ANTHROPIC_THINKING": "on"}).thinking is True
    assert make_provider("anthropic", {}).thinking is False
    local = make_provider("openai", {"OPENAI_BASE_URL": "http://localhost:8000/v1"})
    assert (local.base_url, local.keyless) == ("http://localhost:8000/v1", True)
    hosted = make_provider("openai", {"OPENAI_API_KEY": "o"})
    assert (hosted.base_url, hosted.keyless) == (None, False)


@pytest.mark.parametrize("name,variable", [("anthropic", "ANTHROPIC_API_KEY"), ("gemini", "GOOGLE_API_KEY"),
                                           ("openai", "OPENAI_API_KEY"), ("deepseek", "DEEPSEEK_API_KEY")])
def test_a_missing_key_names_the_variable_to_set(name, variable, monkeypatch):
    import sys
    import types
    for module in ("anthropic", "openai"):
        fake = types.ModuleType(module)
        fake.Anthropic = fake.OpenAI = lambda **kw: object()
        monkeypatch.setitem(sys.modules, module, fake)
    google = types.ModuleType("google")
    google.genai = types.ModuleType("google.genai")
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", google.genai)
    with pytest.raises(RuntimeError, match=variable):
        make_provider(name, {}).get_client()


def test_the_credit_rows_come_from_the_two_credit_variables_and_nothing_set_means_none():
    from quest_ai_runner.providers import credits_from_environment
    assert credits_from_environment({}) == [] and credits_from_environment({"QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD": "not a number"}) == []
    assert credits_from_environment({"QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD": "0"}) == []
    [row] = credits_from_environment({"QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD": "150"})
    assert (row["provider"], row["allowance"], row["credit_value"]) == ("anthropic", "150.0", "")       # unset value: face value
    [row] = credits_from_environment({"QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD": "150", "QAR_ANTHROPIC_CREDIT_VALUE": "0.5"})
    assert row["credit_value"] == "0.5"


def test_the_environment_credit_reaches_the_tier_selector_the_way_the_hub_counts_it(monkeypatch):
    from hubofhubs import tiers
    from quest_ai_runner.core import model_registry
    from quest_ai_runner.providers import credits_from_environment
    monkeypatch.setenv("QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD", "200")
    monkeypatch.setenv("QAR_ANTHROPIC_CREDIT_VALUE", "0.5")
    assert model_registry.credit_source is credits_from_environment or model_registry.credit_source() == credits_from_environment()
    assert tiers.credit_of(model_registry.credit_source(), "anthropic") == (200.0, 0.5)
    monkeypatch.delenv("QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD")
    assert tiers.credit_of(model_registry.credit_source(), "anthropic") == (0.0, 1.0)
