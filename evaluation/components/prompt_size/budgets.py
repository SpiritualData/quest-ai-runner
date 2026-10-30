"""Derive each assembly point's prompt budget from documented inputs instead of fitting it to a measurement.

For a point with its own call (planner, deep prompt)::

    window_tokens   = context window of the CHEAPEST tier in the model registry
                      (every prompt must still fit after a tier downgrade, TIER_DOWNGRADE_ORDER)
    window_bound    = window_tokens * window_share
                      (share "worker_reserve" = the part of the window the deep worker leaves us:
                      window_tokens - WORKER_RESERVED_TOKENS)
    cost_bound      = usd_cap_per_run / calls_per_run / price_per_token of the point's own tier
    budget_tokens   = min(window_bound, cost_bound)
    budget_chars    = budget_tokens * CHARS_PER_TOKEN

A point that is a section of another (autopilot batch, context-update bundle) takes ``share`` of its parent's
budget. Every constant comes from the code that owns it: the window and the characters per token from
``core.prompt_budget``, the tier models from the hub tier selector (``core.model_registry.HUB_TIER_OF``), the prices from ``pricing``, the call
counts from ``OrchestratorConfig``. Only the shares and dollar caps are policy, and they live in
``budget_inputs.json`` with the reason for each."""
import json
from pathlib import Path

import common  # noqa: F401  (sets sys.path for the repo)
from hubofhubs import tiers as hub_tiers
from quest_ai_runner import pricing
from quest_ai_runner.core import model_registry, prompt_budget
from quest_ai_runner.core.orchestrator_types import OrchestratorConfig

INPUTS = json.loads((Path(__file__).resolve().parent / "budget_inputs.json").read_text())["points"]


def tier_models():
    """tier -> the model the live registry serves it: the hub tier selector's pick for each tier's hub tier."""
    picks = hub_tiers.choose()
    return {tier: picks[hub_tier]["arm"][1] for tier, hub_tier in model_registry.HUB_TIER_OF.items()}


def price_per_mtok(model):
    """Input price per million tokens of ``model``, at the hub's price table."""
    return pricing.get_input_cost_per_mtok(pricing.provider_for_model(model), model)


def cheapest_tier():
    """The registry tier whose model has the lowest input price."""
    top = tier_models()
    return min(top, key=lambda tier: (price_per_mtok(top[tier]), tier))


def calls_per_run(spec):
    if isinstance(spec, int):
        return spec
    return getattr(OrchestratorConfig(), spec)


def derive(point):
    """The budget of ``point`` and every input that produced it, as a dict."""
    spec = INPUTS[point]
    if "share_of" in spec:
        parent = derive(spec["share_of"])
        tokens = int(parent["budget_tokens"] * spec["share"])
        return dict(point=point, budget_tokens=tokens, budget_chars=int(tokens * prompt_budget.CHARS_PER_TOKEN),
                    basis=f"{spec['share']:.0%} of {spec['share_of']}", **{k: parent[k] for k in ("window_tokens",)})
    top = tier_models()
    window = prompt_budget.context_window_tokens(top[cheapest_tier()])
    share = spec["window_share"]
    model = top[spec["tier"]]
    price = price_per_mtok(model) / 1_000_000
    calls = calls_per_run(spec["calls_per_run"])
    window_bound = window - prompt_budget.WORKER_RESERVED_TOKENS if share == "worker_reserve" else int(window * share)
    cost_bound = int(spec["usd_cap_per_run"] / calls / price)
    tokens = min(window_bound, cost_bound)
    return dict(point=point, budget_tokens=tokens, budget_chars=int(tokens * prompt_budget.CHARS_PER_TOKEN),
                window_tokens=window, window_bound=window_bound, cost_bound=cost_bound,
                binding="window" if window_bound <= cost_bound else "cost", model=model, calls=calls,
                basis=f"{'window' if window_bound <= cost_bound else 'cost'} bound ({model}, {calls} calls)")
