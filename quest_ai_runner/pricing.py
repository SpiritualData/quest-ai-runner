"""Cost estimation for bootstrap and context operations, read from the hub's price table (``hubofhubs/registry/tier_prices.csv``:
per million tokens, each row with its official source and the date it was checked). A model the table does not price has no
price here (None), and a bootstrap estimate then uses ``UNPRICED_PER_MTOK``. The keyless command-line provider bills against a
subscription, modelled as a flat rate per token.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from hubofhubs import registry
from hubofhubs.models.family import VENDOR_PREFIXES, provider_name_for_model, split_tokens
from hubofhubs.tiers import rates

# A $20 a month subscription over ~1.2M tokens a month, rough: $0.0167 per million tokens.
CLAUDE_CLI_PER_MTOK = 0.0167
UNPRICED_PER_MTOK = 3.0


def get_provider_and_model(env: Optional[Dict[str, str]] = None) -> Tuple[str, str, Optional[str]]:
    """``(provider_name, primary_model_id, api_key_status)`` from the environment: ``QAR_MODEL_BACKEND`` names the provider (default:
    Anthropic when its key is set, else the keyless command-line provider). Anthropic models are family names, never release ids,
    so each resolves to the newest live model; the status is "configured" or "missing" for display, None for the keyless one."""
    env = env or os.environ
    key = lambda var: (env.get(var) or "").strip()
    backend = (env.get("QAR_MODEL_BACKEND") or "").strip().lower() or ("anthropic" if key("ANTHROPIC_API_KEY") else "claude_cli")
    status = lambda var: "configured" if key(var) else "missing"
    if backend == "claude_cli":
        return "claude_cli", "sonnet", None        # a family alias: the CLI always runs the latest Sonnet
    if backend == "openai":
        return "openai", "gpt-4o", status("OPENAI_API_KEY")
    if backend == "gemini":
        return "gemini", "gemini-2.0-flash", status("GOOGLE_API_KEY")
    if backend != "anthropic":
        return "anthropic", "sonnet", None          # an unknown backend defaults to Anthropic
    tiers = {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus", "fast": "haiku", "balanced": "sonnet", "quality": "opus"}
    return "anthropic", tiers.get(key("QAR_PLANNER_TIER") or "haiku", "sonnet"), status("ANTHROPIC_API_KEY")


def table_rates(provider: str, model: str) -> Optional[Tuple[float, float]]:
    """``(input, output)`` USD per million tokens from the hub's table, or None. A release the table does not list is priced as the
    newest listed release of its family ("haiku", "claude-opus-4-8", "gemini-3.2-flash-lite"), so a new release needs no edit."""
    prices = registry.read("tier_prices.csv")
    name = (model or "").lower()
    found = rates(prices, provider, name)
    if found is not None:
        return found
    wanted = set(split_tokens(name)[0])
    family = lambda listed: set(split_tokens(listed)[0]) - set(VENDOR_PREFIXES)
    kin = [r["model"] for r in prices if r["provider"] == provider and family(r["model"]) and family(r["model"]) <= wanted]
    best = max(kin, key=lambda listed: (len(family(listed)), split_tokens(listed)[1]), default=None)
    return rates(prices, provider, best) if best else None


def get_input_cost_per_mtok(provider: str, model: str) -> Optional[float]:
    """Input cost per million tokens for a provider and model, or None when unpriced."""
    if provider == "claude_cli":
        return CLAUDE_CLI_PER_MTOK
    found = table_rates(provider, model)
    return None if found is None else found[0]


def get_output_cost_per_mtok(provider: str, model: str) -> Optional[float]:
    """Output cost per million tokens, or None when unpriced."""
    found = table_rates(provider, model)
    return None if found is None else found[1]


def provider_for_model(model: str) -> str:
    """The provider that serves a model id, by its name (the hub's prefix table); an id no prefix names is Anthropic's."""
    return provider_name_for_model(model) or "anthropic"


def estimate_bootstrap_cost(tokens: int, env: Optional[Dict[str, str]] = None) -> Tuple[float, str, str]:
    """``(cost_usd, provider_name, model_id)`` of a bootstrap with ``tokens`` input tokens."""
    provider, model, _ = get_provider_and_model(env)
    per_mtok = get_input_cost_per_mtok(provider, model)
    return (tokens / 1_000_000) * (UNPRICED_PER_MTOK if per_mtok is None else per_mtok), provider, model
