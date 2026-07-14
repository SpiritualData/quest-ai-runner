"""MultiProvider — intelligent routing of model calls to the right provider.

Wraps a primary provider and routes models to the correct provider based on
model name prefix:
- claude-* → Anthropic
- gemini-* or models/* → Gemini
- gpt-* → OpenAI

Any code using this provider automatically gets intelligent routing without
needing to know about multi-provider setup.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import ModelProvider

_log = logging.getLogger("quest-ai-runner.multi-provider")


class MultiProvider(ModelProvider):
    """Routes model calls to the correct provider based on model name prefix."""

    def __init__(
        self,
        primary: ModelProvider,
        providers: Optional[Dict[str, ModelProvider]] = None,
        usage_tracker: Optional[Any] = None,
        registry: Optional[Any] = None,
    ):
        """Initialize with a primary provider and optional provider map.

        Args:
            primary: The default provider to use (fallback)
            providers: Optional dict of provider_name -> ModelProvider
                      (e.g., {"anthropic": AnthropicProvider(), "gemini": GeminiProvider()})
            usage_tracker: Optional DailyUsageTracker; when set, records token deltas
                           from each plan/answer call toward the daily budget.
            registry: Optional ModelRegistry; when set, a quota/rate-limit error on a
                      tier-resolved model automatically steps down to the next cheaper
                      tier and retries (see set_tier_registry). None (default): no
                      change from prior behavior, errors propagate as before.
        """
        super().__init__()
        self.primary = primary
        self.providers = providers or {}
        self.call_count = 0
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self._usage_tracker = usage_tracker
        self._registry = registry

    def set_tier_registry(self, registry: Optional[Any]) -> None:
        """Attach (or replace) the ModelRegistry used for quota-exhaustion tier fallback.

        Exists because MultiProvider is typically constructed before the registry (the
        registry's own auto-bucketing wants the fully-wrapped provider); a consumer wires
        this in right after building both. Passing None disables the fallback.
        """
        self._registry = registry

    def _tier_for_model(self, model: str) -> Optional[str]:
        """Which tier currently resolves to ``model``, or None if unknown/no registry.

        A pinned model= call that doesn't match any tier's CURRENT resolution (e.g. the
        model isn't one produced by resolve_tier at all) correctly yields None, so it is
        left alone rather than guessed at.
        """
        if not self._registry or not model:
            return None
        try:
            top = self._registry.top_models()
        except Exception:  # noqa: BLE001 — a registry hiccup must never break a call
            return None
        for tier, resolved in top.items():
            if resolved == model:
                return tier
        return None

    def _is_quota_or_rate_limit_error(self, exc: Exception) -> bool:
        """Whether ``exc`` looks like a quota/rate-limit failure (429, resource_exhausted).

        Same string-based classification as conceptai's call_llm, since provider SDKs
        don't share one exception type for this across Anthropic/Gemini/OpenAI.
        """
        s = str(exc).lower()
        return "429" in s or "resource_exhausted" in s or "quota" in s or "rate limit" in s or "rate_limit" in s

    def _tier_fallback_model(self, model: str, exc: Exception) -> Optional[str]:
        """The model to retry with on a quota/rate-limit error, or None to just re-raise.

        None covers: not a quota/rate-limit error, no registry attached, ``model`` isn't
        tied to a known tier (a pinned model= call), the tier is already the cheapest
        ("fast"), or the lower tier resolves to the SAME model (nothing to gain).
        """
        if not self._is_quota_or_rate_limit_error(exc):
            return None
        tier = self._tier_for_model(model)
        if not tier:
            return None
        from ..core.model_registry import next_lower_tier
        lower_tier = next_lower_tier(tier)
        if not lower_tier:
            return None
        lower_model = self._registry.resolve_tier(lower_tier)
        if not lower_model or lower_model == model:
            return None
        return lower_model

    def _call_with_tier_fallback(self, model: str, make_call: Callable[[str], Any]) -> Any:
        """THE ONE place the quota/rate-limit retry-and-step-down loop lives.

        ``make_call(m)`` performs one routed call (provider lookup, the actual
        plan/answer/web_search invocation, and token-delta recording) for model ``m`` and
        returns its result. On a quota/rate-limit error, steps down to the next cheaper
        tier (see set_tier_registry) and calls ``make_call`` again with that model,
        cascading tier by tier until a call succeeds or there is no lower tier left, at
        which point the original exception propagates. plan/answer/web_search each just
        supply what "make the call" means for their own signature; none of them
        duplicate the retry logic itself.
        """
        try:
            return make_call(model)
        except Exception as exc:
            fallback_model = self._tier_fallback_model(model, exc)
            if fallback_model is None:
                raise
            _log.warning(f"{model} hit a quota/rate limit; falling back to {fallback_model}")
            return self._call_with_tier_fallback(fallback_model, make_call)

    def _get_provider_for_model(self, model: str) -> ModelProvider:
        """Route to the correct provider based on model name."""
        if not model:
            return self.primary

        model_lower = model.lower()

        # Auto-detect based on model prefix
        if model_lower.startswith("claude"):
            if "anthropic" in self.providers:
                _log.debug(f"Routing Claude model '{model}' to Anthropic provider")
                return self.providers["anthropic"]
        elif model_lower.startswith("gemini") or model.startswith("models/"):
            if "gemini" in self.providers:
                _log.debug(f"Routing Gemini model '{model}' to Gemini provider")
                return self.providers["gemini"]
        elif model_lower.startswith("gpt"):
            if "openai" in self.providers:
                _log.debug(f"Routing GPT model '{model}' to OpenAI provider")
                return self.providers["openai"]

        # Fallback to primary provider
        _log.debug(f"Model '{model}' routing to primary provider ({type(self.primary).__name__})")
        return self.primary

    def _record_token_delta(self, provider: ModelProvider, before_in: int, before_out: int) -> None:
        """Record the token delta from one LLM call; update own totals and the usage tracker."""
        after_in = getattr(provider, "tokens_in", 0)
        after_out = getattr(provider, "tokens_out", 0)
        delta_in = max(0, after_in - before_in)
        delta_out = max(0, after_out - before_out)
        if delta_in == 0 and delta_out == 0:
            return
        self.tokens_in += delta_in
        self.tokens_out += delta_out
        if self._usage_tracker is not None:
            try:
                self._usage_tracker.record(delta_in, delta_out)
            except Exception:  # noqa: BLE001 — token tracking must never break a call
                pass

    def _limit_message(self) -> str:
        """User-facing message shown when the daily token limit is reached."""
        status = self._usage_tracker.status() if self._usage_tracker else ""
        limit = getattr(getattr(self._usage_tracker, "_limits", None), "max_daily_tokens", None)
        line = f"Daily token limit reached ({status})."
        if limit is not None:
            new = limit * 2
            line += (
                f" To raise it, set QAR_DAILY_TOKEN_LIMIT={new} in your .env file"
                f" (current: {limit:,}). To disable the limit entirely, set"
                f" QAR_DAILY_TOKEN_LIMIT=0."
            )
        line += " The counter resets at midnight UTC."
        return line

    def plan(
        self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
        layers: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Route plan call to correct provider, passing the cache ``layers`` through untouched.

        On a quota/rate-limit error from a tier-resolved model, steps down to the next
        cheaper tier and retries (see set_tier_registry); a pinned model= call, or one
        already at the cheapest tier, just raises as before.
        """
        if self._usage_tracker and self._usage_tracker.over_limit():
            # Return a terminal "answer" decision so the orchestrator skips further LLM work
            # and proceeds straight to generating the limit-reached response.
            return {"action": "answer", "rationale": "daily token limit reached"}
        self.call_count += 1

        def _call(m: str) -> Dict[str, Any]:
            provider = self._get_provider_for_model(m)
            before_in = getattr(provider, "tokens_in", 0)
            before_out = getattr(provider, "tokens_out", 0)
            result = provider.plan(prompt, model=m, tool_schema=tool_schema, layers=layers)
            self._record_token_delta(provider, before_in, before_out)
            return result

        return self._call_with_tier_fallback(model, _call)

    def answer(
        self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None,
        layers: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Route answer call to correct provider, passing the cache ``layers`` through untouched.

        On a quota/rate-limit error from a tier-resolved model, steps down to the next
        cheaper tier and retries (see set_tier_registry); a pinned model= call, or one
        already at the cheapest tier, just raises as before.
        """
        if self._usage_tracker and self._usage_tracker.over_limit():
            return self._limit_message()
        self.call_count += 1

        def _call(m: str) -> str:
            provider = self._get_provider_for_model(m)
            before_in = getattr(provider, "tokens_in", 0)
            before_out = getattr(provider, "tokens_out", 0)
            result = provider.answer(messages, model=m, system=system, layers=layers)
            self._record_token_delta(provider, before_in, before_out)
            return result

        return self._call_with_tier_fallback(model, _call)

    def supports_web_search(self, model: Optional[str] = None) -> bool:
        """Whether the provider that would handle ``model`` supports native web search."""
        provider = self._get_provider_for_model(model) if model else self.primary
        fn = getattr(provider, "supports_web_search", None)
        try:
            return bool(fn(model)) if callable(fn) else False
        except Exception:  # noqa: BLE001 — capability probe must never raise
            return False

    def web_search(self, query: str, *, model: str, max_results: int = 5) -> Dict[str, Any]:
        """Route a native web search to the correct provider for ``model``.

        On a quota/rate-limit error from a tier-resolved model, steps down to the next
        cheaper tier and retries (see set_tier_registry); a pinned model= call, or one
        already at the cheapest tier, just raises as before.
        """
        if self._usage_tracker and self._usage_tracker.over_limit():
            return {"answer": self._limit_message(), "results": []}
        self.call_count += 1

        def _call(m: str) -> Dict[str, Any]:
            provider = self._get_provider_for_model(m)
            before_in = getattr(provider, "tokens_in", 0)
            before_out = getattr(provider, "tokens_out", 0)
            result = provider.web_search(query, model=m, max_results=max_results)
            self._record_token_delta(provider, before_in, before_out)
            return result

        return self._call_with_tier_fallback(model, _call)

    def list_models(self) -> List[str]:
        """List all models from all providers."""
        all_models = []
        # Get models from primary provider
        try:
            all_models.extend(self.primary.list_models())
        except Exception:  # noqa: BLE001
            pass
        # Get models from all registered providers
        for provider in self.providers.values():
            try:
                all_models.extend(provider.list_models())
            except Exception:  # noqa: BLE001
                pass
        # Return deduplicated list
        return list(set(all_models))
