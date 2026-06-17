"""Pricing lookup for different model providers.

Provides cost estimation for bootstrap and context operations based on actual model pricing.
Pricing is per 1M input tokens, based on current market rates (as of 2026).
"""
from __future__ import annotations

import re
from typing import Optional, Dict, Tuple

# Pricing per 1M input tokens (USD)
# Sources:
#   - Anthropic: https://www.anthropic.com/pricing
#   - OpenAI: https://openai.com/pricing
#   - Google Gemini: https://ai.google.dev/pricing
# These are snapshot prices and may change; consider them estimates.

ANTHROPIC_PRICING = {
    # Claude 3.5 Sonnet (2024-10-22)
    "claude-3-5-sonnet-20241022": 3.0,
    "claude-3.5-sonnet-20241022": 3.0,
    # Claude 3.5 Haiku (2024-11-01)
    "claude-3-5-haiku-20241101": 0.8,
    "claude-3.5-haiku-20241101": 0.8,
    # Claude 3 Opus (older)
    "claude-3-opus": 15.0,
    "claude-opus-4-8": 3.0,  # Opus 4 (2025+)
    "claude-opus-4": 3.0,
    # Claude 3 Sonnet (older)
    "claude-3-sonnet": 3.0,
    "claude-sonnet-4-6": 3.0,  # Sonnet 4 (2025+)
    "claude-sonnet-4": 3.0,
    # Claude 3 Haiku (older)
    "claude-3-haiku": 0.8,
    "claude-haiku-4-5": 0.8,  # Haiku 4 (2025+)
    "claude-haiku-4": 0.8,
}

OPENAI_PRICING = {
    # GPT-4o (2024-11-20)
    "gpt-4o-2024-11-20": 2.5,
    "gpt-4o": 2.5,
    # GPT-4 Turbo (older)
    "gpt-4-turbo": 10.0,
    "gpt-4-turbo-2024-04-09": 10.0,
    # GPT-4 (older)
    "gpt-4": 30.0,
    # o1 (reasoning)
    "o1": 15.0,
    "o1-preview": 15.0,
    # o3 (2025)
    "o3": 20.0,
    "o3-mini": 1.0,
}

GOOGLE_PRICING = {
    # Gemini 2.0 Flash
    "gemini-2.0-flash": 0.075,
    "gemini-2.0-flash-001": 0.075,
    # Gemini 2.0 Flash Thinking
    "gemini-2.0-flash-thinking-exp-01-21": 0.0,  # free tier
    # Gemini 1.5 Pro
    "gemini-1.5-pro": 1.25,
    "gemini-1.5-pro-001": 1.25,
    # Gemini 1.5 Flash
    "gemini-1.5-flash": 0.075,
    "gemini-1.5-flash-001": 0.075,
}

CLAUDE_CLI_PRICING = {
    # Claude CLI uses subscription (no per-token billing)
    # We model it as $20/month subscription used across ~1.2M tokens average per month
    # = $0.0167 per 1M tokens (rough estimate based on Claude Code subscription pricing)
    "default": 0.0167,
}


def get_provider_and_model(
    env: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, Optional[str]]:
    """Detect the configured provider and primary model from environment.

    Returns:
        (provider_name, primary_model_id, api_key_status)
        - provider_name: "anthropic", "openai", "gemini", or "claude_cli"
        - primary_model_id: the model being used for planning/answering
        - api_key_status: "configured" or "missing" (for display)
    """
    import os

    env = env or os.environ

    # Check QAR_MODEL_BACKEND for explicit provider selection
    backend = (env.get("QAR_MODEL_BACKEND") or "").strip().lower()
    anthropic_key = (env.get("ANTHROPIC_API_KEY") or "").strip()

    # Auto-select: claude_cli if no key, else anthropic
    if not backend:
        backend = "anthropic" if anthropic_key else "claude_cli"

    # Map to provider name
    if backend == "claude_cli":
        return ("claude_cli", "claude-sonnet-4-6", None)  # default model for CLI
    elif backend == "anthropic":
        # Resolve the model tier to an actual model id
        planner_tier = (env.get("QAR_PLANNER_TIER") or "").strip() or "haiku"
        model_map = {
            "haiku": "claude-haiku-4-5",
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-8",
            "fast": "claude-haiku-4-5",
            "balanced": "claude-sonnet-4-6",
            "quality": "claude-opus-4-8",
        }
        model = model_map.get(planner_tier, "claude-sonnet-4-6")
        api_status = "configured" if anthropic_key else "missing"
        return ("anthropic", model, api_status)
    elif backend == "openai":
        openai_key = (env.get("OPENAI_API_KEY") or "").strip()
        api_status = "configured" if openai_key else "missing"
        return ("openai", "gpt-4o", api_status)
    elif backend == "gemini":
        gemini_key = (env.get("GOOGLE_API_KEY") or "").strip()
        api_status = "configured" if gemini_key else "missing"
        return ("gemini", "gemini-2.0-flash", api_status)
    else:
        # Unknown backend; default to anthropic
        return ("anthropic", "claude-sonnet-4-6", None)


def get_input_cost_per_mtok(provider: str, model: str) -> Optional[float]:
    """Look up input cost per 1M tokens for a provider/model combination.

    Args:
        provider: "anthropic", "openai", "gemini", or "claude_cli"
        model: the model id (e.g., "claude-sonnet-4-6")

    Returns:
        Cost per 1M input tokens (USD), or None if unknown.
    """
    model_lower = (model or "").lower()

    if provider == "anthropic":
        # Try exact match first
        if model_lower in ANTHROPIC_PRICING:
            return ANTHROPIC_PRICING[model_lower]
        # Fall back to pattern matching
        for key, price in ANTHROPIC_PRICING.items():
            if key.replace("-", "").replace("_", "") in model_lower.replace("-", "").replace(
                "_", ""
            ):
                return price
        # Default to Claude 3.5 Sonnet (most common)
        return ANTHROPIC_PRICING.get("claude-3-5-sonnet-20241022", 3.0)

    elif provider == "openai":
        if model_lower in OPENAI_PRICING:
            return OPENAI_PRICING[model_lower]
        for key, price in OPENAI_PRICING.items():
            if key in model_lower:
                return price
        # Default to GPT-4o
        return OPENAI_PRICING.get("gpt-4o", 2.5)

    elif provider == "gemini":
        if model_lower in GOOGLE_PRICING:
            return GOOGLE_PRICING[model_lower]
        for key, price in GOOGLE_PRICING.items():
            if key in model_lower:
                return price
        # Default to Gemini 2.0 Flash
        return GOOGLE_PRICING.get("gemini-2.0-flash", 0.075)

    elif provider == "claude_cli":
        # Subscription-based; estimate per token
        return CLAUDE_CLI_PRICING.get("default", 0.0167)

    return None


def estimate_bootstrap_cost(
    tokens: int,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[float, str, str]:
    """Estimate the cost of a bootstrap with the given token count.

    Args:
        tokens: estimated input tokens for the bootstrap
        env: environment dict (defaults to os.environ)

    Returns:
        (cost_usd, provider_name, model_id)
    """
    provider, model, _ = get_provider_and_model(env)
    cost_per_mtok = get_input_cost_per_mtok(provider, model)

    if cost_per_mtok is None:
        # Unknown pricing; use a safe default
        cost_per_mtok = 3.0

    cost = (tokens / 1_000_000) * cost_per_mtok
    return cost, provider, model
