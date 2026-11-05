"""QAR's wiring of the hub's model providers (``hubofhubs.models``). The vendor arms (anthropic, gemini, openai, deepseek) and the
router are the hub's own, built by the hub assembly in ``hub.py`` (``hub.make_provider`` picks a vendor by name); ``routed`` is QAR's one ``call-a-model`` arm, the hub's router with QAR's prefix
table, usage limit message and spend tagging.
``MODEL_PREFIXES`` routes a model id to a provider name: the hub's table plus ``QAR_OPENAI_MODEL_PREFIXES`` for an OpenAI-compatible endpoint's ids.
"""
from __future__ import annotations

import os
import typing
from typing import Any, Dict, Mapping, Optional, Sequence

from esoi import implementation, solution
from hubofhubs.the_self.assembler import arm_needs as needs
from hubofhubs.the_world.model_plumbing.vendor_model_caller.claude_cli import ClaudeCliProvider
from hubofhubs.the_world.model_plumbing.vendor_model_caller.family import MODEL_PREFIX_PROVIDERS, provider_name_for_model
from hubofhubs.the_world.model_plumbing.vendor_model_caller.router import MultiProvider
from hubofhubs.the_world.model_plumbing.vendor_model_caller.interface import ModelCalling

from .core.model_registry import DEFAULT_FALLBACK_TOP, configured_providers
from .usage import active_spec, limit_message

MODEL_PREFIXES: Dict[str, str] = dict(MODEL_PREFIX_PROVIDERS)       # process-wide, like the config that extends it


def route_extra_model_prefixes(provider_name: str, env: Mapping[str, str]) -> list:
    """Route extra model-id prefixes to ``provider_name`` from ``QAR_<PROVIDER>_MODEL_PREFIXES`` (comma separated), so the ids of an
    OpenAI-compatible endpoint (``llama``, ``qwen``) reach the provider that serves it. Returns the prefixes added."""
    raw = (env.get(f"QAR_{provider_name.upper()}_MODEL_PREFIXES") or "").strip()
    added = [p.strip().lower() for p in raw.split(",") if p.strip()]
    MODEL_PREFIXES.update({prefix: provider_name for prefix in added})
    return added


FAILOVER_ORDER = ("gemini", "openai", "anthropic", "deepseek")      # which configured vendor answers next when a model cannot answer now


def failover_models(providers: Mapping[str, Any], primary: Any, pins: Mapping[str, str], env: Mapping[str, str]) -> list:
    """The ordered models the router asks next, on any vendor, when a model has no credit, is over quota, times out or is down
    (the hub's ``fallback_models``): one per CONFIGURED vendor (a key, or a keyless endpoint) other than the one serving ``primary``,
    in ``FAILOVER_ORDER``. A model pinned in ``QAR_MODEL_<TIER>`` that the vendor serves is preferred, else the vendor's own
    default (QAR's last-known tier map for gemini and anthropic, the provider's listed fallback for the OpenAI-compatible ones).
    ``QAR_FALLBACK_MODELS`` (comma separated, ordered) replaces all of this. No other configured vendor means an empty list."""
    explicit = [m.strip() for m in (env.get("QAR_FALLBACK_MODELS") or "").split(",") if m.strip()]
    if explicit:
        return explicit
    configured = configured_providers(providers) - {name for name, p in providers.items() if p is primary or type(p) is type(primary)}
    defaults = {"gemini": DEFAULT_FALLBACK_TOP["quality"], "anthropic": DEFAULT_FALLBACK_TOP["best"]}
    found = []
    for name in FAILOVER_ORDER:
        if name not in configured:
            continue
        pinned = next((m for m in pins.values() if provider_name_for_model(m, [name], MODEL_PREFIXES) == name), None)
        listed = getattr(providers[name], "fallback_models", ())
        model = pinned or defaults.get(name) or (listed[0] if listed else None)
        if model and model not in found:
            found.append(model)
    return found


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


@solution("call-a-model", kind="event", entry="answer",
          example={"messages": [{"role": "user", "content": "Reply with the single word: ok"}], "model": "claude-haiku"})
@typing.runtime_checkable
class RoutedModelCalling(ModelCalling, typing.Protocol):
    """The model seat as QAR fills it: the hub's ``call-a-model`` interface (plan, answer, list models) over the router below, which
    sends each model id to the vendor that serves it. A consumer's own provider satisfies it the same way."""


@implementation("call-a-model", "multi_provider")
def routed(primary: Any = None, providers: Optional[Dict[str, Any]] = None, usage_tracker: Any = None, registry: Any = None,
           fallback_models: Sequence[str] = ()) -> MultiProvider:
    """The hub's router with QAR's prefix table, the daily-limit message that names the variable to raise, spend tagging by the registered
    prompt spec each call was rendered from (``justify-model-calls``: ``PromptSpec.render`` tags the call, ``spec_of`` reads the tag),
    and the ordered ``fallback_models`` that keep a turn answered when one vendor cannot (see ``failover_models``). With no
    ``primary`` the router is built over the hub's default ``call-a-model`` arm, the Claude Code subscription (``claude``)."""
    if primary is None:
        primary = ClaudeCliProvider()
    return MultiProvider(primary, providers, usage_tracker, registry, prefixes=MODEL_PREFIXES,
                         limit_message=limit_message(usage_tracker), spec_of=active_spec, fallback_models=fallback_models)


routed.REQUIRES = (needs.CLAUDE_BINARY,)
