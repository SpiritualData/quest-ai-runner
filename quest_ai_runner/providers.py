"""QAR's wiring of the hub's model providers (``hubofhubs.models``): the environment variables stay here, the hub takes arguments.

Each ``call-a-model`` implementation below builds the hub's arm from the vendor key variables and the ``QAR_*`` and vendor settings
(``make_provider`` picks one by name); ``routed`` builds the hub's router with QAR's prefix table, usage limit message and spend
tagging. ``MODEL_PREFIXES`` routes a model id to a provider name: the hub's table plus ``QAR_OPENAI_MODEL_PREFIXES`` for an
OpenAI-compatible endpoint's ids.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

from esoi import implementation
from hubofhubs.models.anthropic import AnthropicProvider
from hubofhubs.models.family import MODEL_PREFIX_PROVIDERS
from hubofhubs.models.gemini import GeminiProvider
from hubofhubs.models.openai import DeepSeekProvider, OpenAIProvider
from hubofhubs.models.router import MultiProvider

from .core.resolve import resolve
from .usage import active_spec, limit_message

MODEL_PREFIXES: Dict[str, str] = dict(MODEL_PREFIX_PROVIDERS)       # process-wide, like the config that extends it


def route_extra_model_prefixes(provider_name: str, env: Mapping[str, str]) -> list:
    """Route extra model-id prefixes to ``provider_name`` from ``QAR_<PROVIDER>_MODEL_PREFIXES`` (comma separated), so the ids of an
    OpenAI-compatible endpoint (``llama``, ``qwen``) reach the provider that serves it. Returns the prefixes added."""
    raw = (env.get(f"QAR_{provider_name.upper()}_MODEL_PREFIXES") or "").strip()
    added = [p.strip().lower() for p in raw.split(",") if p.strip()]
    MODEL_PREFIXES.update({prefix: provider_name for prefix in added})
    return added


def flag(env: Mapping[str, str], var: str) -> bool:
    return (env.get(var) or "").strip().lower() in ("on", "1", "true", "yes")


@implementation("call-a-model", "anthropic")
def anthropic(env: Mapping[str, str]) -> AnthropicProvider:
    return AnthropicProvider(api_key=env.get("ANTHROPIC_API_KEY") or None, thinking=flag(env, "QAR_ANTHROPIC_THINKING"),
                             env_name="ANTHROPIC_API_KEY")


@implementation("call-a-model", "gemini")
def gemini(env: Mapping[str, str]) -> GeminiProvider:
    return GeminiProvider(api_key=env.get("GOOGLE_API_KEY") or None, env_name="GOOGLE_API_KEY")


@implementation("call-a-model", "openai")
def openai(env: Mapping[str, str]) -> OpenAIProvider:
    base = env.get("OPENAI_BASE_URL") or None      # any OpenAI-compatible endpoint (a proxy, vLLM, Ollama): a self-hosted one needs no key
    return OpenAIProvider(api_key=env.get("OPENAI_API_KEY") or None, base_url=base, keyless=bool(base),
                          env_name="OPENAI_API_KEY")


@implementation("call-a-model", "deepseek")
def deepseek(env: Mapping[str, str]) -> DeepSeekProvider:
    return DeepSeekProvider(api_key=env.get("DEEPSEEK_API_KEY") or None, base_url=env.get("DEEPSEEK_BASE_URL") or None,
                            thinking=flag(env, "DEEPSEEK_THINKING"), env_name="DEEPSEEK_API_KEY")


def make_provider(name: str, env: Optional[Mapping[str, str]] = None) -> Any:
    """The vendor provider ``name`` (anthropic, gemini, openai or deepseek) configured from ``env`` (default: the process environment)."""
    return resolve("call-a-model", name)(os.environ if env is None else env)


@implementation("call-a-model", "multi_provider")
def routed(primary: Any, providers: Optional[Dict[str, Any]] = None, usage_tracker: Any = None, registry: Any = None) -> MultiProvider:
    """The hub's router with QAR's prefix table, the daily-limit message that names the variable to raise, and spend tagging by spec."""
    return MultiProvider(primary, providers, usage_tracker, registry, prefixes=MODEL_PREFIXES,
                         limit_message=limit_message(usage_tracker), spec_of=active_spec)
