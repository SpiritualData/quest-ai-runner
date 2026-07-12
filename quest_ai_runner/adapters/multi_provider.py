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
from typing import Any, Dict, List, Optional

from ..core.adapters import ModelProvider

_log = logging.getLogger("quest-ai-runner.multi-provider")


class MultiProvider(ModelProvider):
    """Routes model calls to the correct provider based on model name prefix."""

    def __init__(
        self,
        primary: ModelProvider,
        providers: Optional[Dict[str, ModelProvider]] = None,
        usage_tracker: Optional[Any] = None,
    ):
        """Initialize with a primary provider and optional provider map.

        Args:
            primary: The default provider to use (fallback)
            providers: Optional dict of provider_name -> ModelProvider
                      (e.g., {"anthropic": AnthropicProvider(), "gemini": GeminiProvider()})
            usage_tracker: Optional DailyUsageTracker; when set, records token deltas
                           from each plan/answer call toward the daily budget.
        """
        super().__init__()
        self.primary = primary
        self.providers = providers or {}
        self.call_count = 0
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self._usage_tracker = usage_tracker

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
        """Route plan call to correct provider, passing the cache ``layers`` through untouched."""
        if self._usage_tracker and self._usage_tracker.over_limit():
            # Return a terminal "answer" decision so the orchestrator skips further LLM work
            # and proceeds straight to generating the limit-reached response.
            return {"action": "answer", "rationale": "daily token limit reached"}
        provider = self._get_provider_for_model(model)
        before_in = getattr(provider, "tokens_in", 0)
        before_out = getattr(provider, "tokens_out", 0)
        self.call_count += 1
        result = provider.plan(prompt, model=model, tool_schema=tool_schema, layers=layers)
        self._record_token_delta(provider, before_in, before_out)
        return result

    def answer(
        self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None,
        layers: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Route answer call to correct provider, passing the cache ``layers`` through untouched."""
        if self._usage_tracker and self._usage_tracker.over_limit():
            return self._limit_message()
        provider = self._get_provider_for_model(model)
        before_in = getattr(provider, "tokens_in", 0)
        before_out = getattr(provider, "tokens_out", 0)
        self.call_count += 1
        result = provider.answer(messages, model=model, system=system, layers=layers)
        self._record_token_delta(provider, before_in, before_out)
        return result

    def supports_web_search(self, model: Optional[str] = None) -> bool:
        """Whether the provider that would handle ``model`` supports native web search."""
        provider = self._get_provider_for_model(model) if model else self.primary
        fn = getattr(provider, "supports_web_search", None)
        try:
            return bool(fn(model)) if callable(fn) else False
        except Exception:  # noqa: BLE001 — capability probe must never raise
            return False

    def web_search(self, query: str, *, model: str, max_results: int = 5) -> Dict[str, Any]:
        """Route a native web search to the correct provider for ``model``."""
        if self._usage_tracker and self._usage_tracker.over_limit():
            return {"answer": self._limit_message(), "results": []}
        provider = self._get_provider_for_model(model)
        before_in = getattr(provider, "tokens_in", 0)
        before_out = getattr(provider, "tokens_out", 0)
        self.call_count += 1
        result = provider.web_search(query, model=model, max_results=max_results)
        self._record_token_delta(provider, before_in, before_out)
        return result

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
