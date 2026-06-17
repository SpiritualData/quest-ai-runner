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
    ):
        """Initialize with a primary provider and optional provider map.

        Args:
            primary: The default provider to use (fallback)
            providers: Optional dict of provider_name -> ModelProvider
                      (e.g., {"anthropic": AnthropicProvider(), "gemini": GeminiProvider()})
        """
        super().__init__()
        self.primary = primary
        self.providers = providers or {}
        self.call_count = 0

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

    def plan(
        self, prompt: str, *, model: str, tool_schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Route plan call to correct provider."""
        provider = self._get_provider_for_model(model)
        self.call_count += 1
        return provider.plan(prompt, model=model, tool_schema=tool_schema)

    def answer(
        self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None
    ) -> str:
        """Route answer call to correct provider."""
        provider = self._get_provider_for_model(model)
        self.call_count += 1
        return provider.answer(messages, model=model, system=system)

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
