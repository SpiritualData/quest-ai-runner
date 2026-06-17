"""DryRunProvider -- wraps a real provider but doesn't call the LLM.

Instead of executing API calls, it estimates token counts based on input length
and returns a generic response. Used by --dry-run to estimate actual bootstrap cost.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.adapters import ModelProvider


class DryRunProvider(ModelProvider):
    """Wrapper that estimates tokens without calling the LLM.

    Wraps a real ModelProvider (AnthropicProvider, etc.) and:
    - Estimates input tokens from the prompt (rough: 1 token per 4 chars)
    - Estimates output tokens based on typical response length
    - Returns a stub response without API calls
    - Tracks tokens in the wrapped provider for cost estimation
    """

    def __init__(self, wrapped: ModelProvider):
        self._wrapped = wrapped
        self.tokens_in: int = 0
        self.tokens_out: int = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Estimate tokens for a plan call without calling the LLM."""
        # Estimate input tokens (~1 token per 4 chars)
        input_tokens = max(1, len(prompt) // 4)
        self.tokens_in += input_tokens

        # Estimate output tokens for a typical plan response (~200 tokens)
        output_tokens = 200
        self.tokens_out += output_tokens

        # Propagate to wrapped provider for cost tracking
        if hasattr(self._wrapped, 'tokens_in'):
            self._wrapped.tokens_in += input_tokens
        if hasattr(self._wrapped, 'tokens_out'):
            self._wrapped.tokens_out += output_tokens

        # Return stub response with tool call
        return {
            "decide": {
                "modes": ["read"],
                "deep_criteria": [],
                "ask_user": False,
                "answer_candidate": "Dry run estimation only.",
            }
        }

    def answer(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: str,
    ) -> str:
        """Estimate tokens for an answer call without calling the LLM."""
        # Sum up prompt tokens from all messages
        prompt_text = "".join(str(msg.get("content", "")) for msg in messages)
        input_tokens = max(1, len(prompt_text) // 4)
        self.tokens_in += input_tokens

        # Estimate output tokens based on typical response (~500 tokens for analysis)
        output_tokens = 500
        self.tokens_out += output_tokens

        # Propagate to wrapped provider
        if hasattr(self._wrapped, 'tokens_in'):
            self._wrapped.tokens_in += input_tokens
        if hasattr(self._wrapped, 'tokens_out'):
            self._wrapped.tokens_out += output_tokens

        return "Dry run estimation only. This would be the actual response."

    def list_models(self) -> List[str]:
        """Delegate to wrapped provider."""
        return self._wrapped.list_models()
