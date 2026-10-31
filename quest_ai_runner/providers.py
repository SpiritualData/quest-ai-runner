"""QAR's wiring of the hub's model providers (``hubofhubs.models``). The vendor arms (anthropic, gemini, openai, deepseek) and the
router are the hub's own, built by the hub assembly in ``hub.py`` (``hub.make_provider`` picks a vendor by name); ``routed`` is QAR's one ``call-a-model`` arm, the hub's router with QAR's prefix
table, usage limit message and spend tagging.
``MODEL_PREFIXES`` routes a model id to a provider name: the hub's table plus ``QAR_OPENAI_MODEL_PREFIXES`` for an OpenAI-compatible endpoint's ids.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

from esoi import implementation
from hubofhubs.models.family import MODEL_PREFIX_PROVIDERS
from hubofhubs.models.router import MultiProvider

from .usage import active_spec, limit_message

MODEL_PREFIXES: Dict[str, str] = dict(MODEL_PREFIX_PROVIDERS)       # process-wide, like the config that extends it


def route_extra_model_prefixes(provider_name: str, env: Mapping[str, str]) -> list:
    """Route extra model-id prefixes to ``provider_name`` from ``QAR_<PROVIDER>_MODEL_PREFIXES`` (comma separated), so the ids of an
    OpenAI-compatible endpoint (``llama``, ``qwen``) reach the provider that serves it. Returns the prefixes added."""
    raw = (env.get(f"QAR_{provider_name.upper()}_MODEL_PREFIXES") or "").strip()
    added = [p.strip().lower() for p in raw.split(",") if p.strip()]
    MODEL_PREFIXES.update({prefix: provider_name for prefix in added})
    return added


def credits_from_environment(env: Optional[Mapping[str, str]] = None) -> list:
    """The prepaid-credit rows the hub's tier selector counts, from ``QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD`` (the monthly allowance, in
    USD) and ``QAR_ANTHROPIC_CREDIT_VALUE`` (0 to 1: what a credit is worth if not used here; unset means face value, 1). Unset
    allowance means no credit is counted. The hub holds no organization's allowance; this is where QAR's deployment supplies it."""
    env = os.environ if env is None else env
    try:
        allowance = float((env.get("QAR_ANTHROPIC_CREDIT_ALLOWANCE_USD") or "").strip())
    except ValueError:
        return []
    if allowance <= 0:
        return []
    return [{"provider": "anthropic", "balance": "", "allowance": str(allowance),
             "credit_value": (env.get("QAR_ANTHROPIC_CREDIT_VALUE") or "").strip(), "currency": "USD", "period": "monthly"}]


@implementation("call-a-model", "multi_provider")
def routed(primary: Any, providers: Optional[Dict[str, Any]] = None, usage_tracker: Any = None, registry: Any = None) -> MultiProvider:
    """The hub's router with QAR's prefix table, the daily-limit message that names the variable to raise, and spend tagging by spec."""
    return MultiProvider(primary, providers, usage_tracker, registry, prefixes=MODEL_PREFIXES,
                         limit_message=limit_message(usage_tracker), spec_of=active_spec)
