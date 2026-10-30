"""model_registry bucketing — tier -> live top model id."""
import pytest

from quest_ai_runner.core.model_registry import (
    DEFAULT_FALLBACK_TOP,
    ModelRegistry,
    bucket_top,
    is_vision_capable,
)


def test_bucket_takes_latest_first_of_each_family():
    # Latest-first list: the FIRST sonnet/haiku seen wins.
    # Semantic tiers: sonnet -> quality and balanced, haiku -> fast. Opus is never bucketed.
    models = [
        "claude-opus-4-9", "claude-opus-4-8",
        "claude-sonnet-4-7", "claude-sonnet-4-6",
        "claude-haiku-4-6", "claude-haiku-4-5",
    ]
    top = bucket_top(models)
    assert top["quality"] == "claude-sonnet-4-7"
    assert top["balanced"] == "claude-sonnet-4-7"
    assert top["fast"] == "claude-haiku-4-6"


def test_bucket_fills_missing_families_from_fallback():
    # Only opus present live -> opus is never a tier default; balanced/fast/quality come from the
    # fallback map.
    top = bucket_top(["claude-opus-9-9"])
    assert top["quality"] == DEFAULT_FALLBACK_TOP["quality"]
    assert top["balanced"] == DEFAULT_FALLBACK_TOP["balanced"]
    assert top["fast"] == DEFAULT_FALLBACK_TOP["fast"]


def test_registry_resolves_tiers_from_provider():
    class P:
        def plan(self, *a, **k): ...
        def answer(self, *a, **k): ...
        def list_models(self):
            return ["claude-opus-5-0", "claude-sonnet-5-0", "claude-haiku-5-0"]

    reg = ModelRegistry(P())
    # The legacy "opus" name is the quality tier, which resolves to sonnet, never opus.
    assert reg.resolve_tier("opus") == "claude-sonnet-5-0"
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
    # Claude 5-generation ids, including the newer Fable/Mythos families.
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5-1",
    "claude-mythos-5-1",
    "opus", "sonnet", "haiku", "fable",
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


def test_family_names_resolve_to_newest_for_every_vendor():
    from hubofhubs.models.family import newest_in_family
    live = ["models/gemini-3.5-flash", "models/gemini-3.1-flash-lite", "models/gemini-2.0-flash",
            "models/gemini-3.1-pro", "gpt-4o", "gpt-4o-mini", "gpt-5", "gpt-5-mini", "gpt-image-1",
            "claude-sonnet-4-6", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "claude-opus-5-5"]
    assert newest_in_family("gemini-flash", live) == "models/gemini-3.5-flash"
    assert newest_in_family("gemini-flash-lite", live) == "models/gemini-3.1-flash-lite"
    assert newest_in_family("gemini-pro", live) == "models/gemini-3.1-pro"
    assert newest_in_family("gpt", live) == "gpt-5"
    assert newest_in_family("gpt-mini", live) == "gpt-5-mini"
    assert newest_in_family("sonnet", live) == "claude-sonnet-5-5"
    assert newest_in_family("claude-haiku", live) == "claude-haiku-4-5-20251001"
    assert newest_in_family("gpt-4o", live) is None  # a pinned release is never rewritten
    # Quality is the newest sonnet, whatever the list order.
    assert bucket_top(live)["quality"] == "claude-sonnet-5-5"
    assert bucket_top(live)["fast"] == "claude-haiku-4-5-20251001"


def test_bucket_picks_the_newest_member_regardless_of_list_order():
    oldest_first = ["claude-sonnet-4-6", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-haiku-5-0"]
    top = bucket_top(oldest_first)
    assert top["balanced"] == "claude-sonnet-5-5"
    assert top["quality"] == "claude-sonnet-5-5"
    assert top["fast"] == "claude-haiku-5-0"


class Keyed:
    def __init__(self, api_key, models=()):
        self.api_key, self.models = api_key, list(models)

    def list_models(self):
        return list(self.models)


class Router(Keyed):
    def __init__(self, models=(), **providers):
        super().__init__("x", models)
        self.providers = providers


LIVE = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


def measurement(tier, provider, model, cases=20, passed=19, tokens_in=1_000_000, tokens_out=200_000):
    return dict(provider=provider, model=model, cases=cases, passed=passed, tokens_in=tokens_in, tokens_out=tokens_out)


@pytest.fixture
def measured(monkeypatch):
    """Plant tier measurements (the registry ships none); returns the dict to fill, ``{hub tier: [arm]}``."""
    from hubofhubs import tiers as hub_tiers
    rows = {}
    monkeypatch.setattr(hub_tiers, "measured_arms", lambda folder=None: {t: list(a) for t, a in rows.items()})
    return rows


def test_with_no_measurement_the_live_list_decides_and_fast_and_balanced_stay_distinct():
    """The registry ships no measurement, so the selector changes nothing: fast stays the newest haiku and balanced the newest
    sonnet, so stepping down a tier when a quota runs out still lands on a different model."""
    anthropic = Keyed("k", LIVE)
    top = ModelRegistry(Router(LIVE, anthropic=anthropic, gemini=Keyed("k"))).top_models()
    assert top == bucket_top(LIVE)
    assert top["fast"] != top["balanced"] and top["fast"] == "claude-haiku-5-5" and top["balanced"] == "claude-sonnet-5-5"
    assert ModelRegistry(Router(LIVE, anthropic=anthropic)).selected() == {}


def test_no_key_and_no_measurement_still_resolves_every_tier():
    assert ModelRegistry(Router([], anthropic=Keyed(None))).top_models() == dict(DEFAULT_FALLBACK_TOP)


def test_a_pin_wins_over_the_live_list_and_over_a_measured_winner(measured):
    measured["cheap"] = [measurement("cheap", "anthropic", "claude-haiku-5-5")]
    pinned = ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE)), fallback={"fast": "gemini-3.1-flash-lite", "balanced": "gemini-3.6-flash"})
    assert pinned.resolve_tier("fast") == "gemini-3.1-flash-lite" and pinned.resolve_tier("balanced") == "gemini-3.6-flash"
    assert pinned.lower_tier("best") == "quality"


def test_a_measured_winner_replaces_the_live_list_for_its_tiers_only(measured):
    measured["cheap"] = [measurement("cheap", "gemini", "gemini-3.1-flash-lite"), measurement("cheap", "anthropic", "claude-haiku-5-5", passed=10)]
    measured["mid"] = [measurement("mid", "anthropic", "claude-sonnet-5-5")]
    registry = ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE), gemini=Keyed("k")))
    top = registry.top_models()
    assert top["fast"] == "gemini-3.1-flash-lite"
    assert top["balanced"] == "claude-sonnet-5-5" and top["fast"] != top["balanced"]      # the cheap winner is not reused for balanced
    assert top["quality"] == "claude-sonnet-5-5"


def test_a_measurement_below_the_bars_is_not_applied(measured):
    measured["cheap"] = [measurement("cheap", "gemini", "gemini-3.1-flash-lite", cases=3, passed=3)]          # too few cases
    assert ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE), gemini=Keyed("k"))).selected() == {}
    measured["cheap"] = [measurement("cheap", "gemini", "gemini-3.1-flash-lite", cases=20, passed=10)]         # passes too few
    assert ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE), gemini=Keyed("k"))).selected() == {}


def test_deepseek_is_picked_only_when_it_is_measured_and_priced(measured, monkeypatch):
    from hubofhubs import tiers as hub_tiers
    registry = lambda: ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE), deepseek=Keyed("k")))
    assert registry().selected() == {} and registry().top_models()["fast"] == "claude-haiku-5-5"      # a key alone changes nothing
    real = hub_tiers.registry.read
    monkeypatch.setattr(hub_tiers.registry, "read", lambda name, folder=None: real(name, folder) + (
        [dict(provider="deepseek", model="deepseek-chat", input_per_mtok="0.01", output_per_mtok="0.02", source_url="https://example.org",
              checked_on="2026-10-30")] if name == "tier_prices.csv" else []))
    assert registry().selected() == {}                                                                  # priced but not measured
    measured["cheap"] = [measurement("cheap", "deepseek", "deepseek-chat")]
    assert registry().selected() == {}                              # the hub's floor: the held arm is unmeasured, so nothing switches
    measured["cheap"].append(measurement("cheap", "anthropic", "claude-haiku-5-5", passed=15))
    assert registry().top_models()["fast"] == "deepseek-chat"


def test_a_provider_with_no_key_is_never_offered(measured):
    measured["cheap"] = [measurement("cheap", "gemini", "gemini-3.1-flash-lite")]
    assert ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE), gemini=Keyed(None))).selected() == {}


def test_resolve_tier_never_raises_when_the_selector_does(monkeypatch):
    from hubofhubs import tiers as hub_tiers

    def broken(**kwargs):
        raise ValueError("bad csv")
    monkeypatch.setattr(hub_tiers, "choose", broken)
    registry = ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE)))
    assert registry.resolve_tier("fast") == "claude-haiku-5-5" and registry.selected() == {}


def test_the_credit_source_reaches_the_selector(measured, monkeypatch):
    from hubofhubs import tiers as hub_tiers
    seen = []
    real = hub_tiers.choose
    monkeypatch.setattr(hub_tiers, "choose", lambda **kw: seen.append(kw.get("credits")) or real(**kw))
    own = [dict(provider="anthropic", balance="", allowance="50", credit_value="")]
    ModelRegistry(Router(LIVE, anthropic=Keyed("k", LIVE)), credits=own).selected()
    assert seen == [own]
