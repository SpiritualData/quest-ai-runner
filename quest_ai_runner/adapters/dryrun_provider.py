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
        """Estimate tokens for an answer call without calling the LLM.

        Returns a stub response in the appropriate format (JSON for structured outputs).
        """
        # Sum up prompt tokens from all messages
        prompt_text = "".join(str(msg.get("content", "")) for msg in messages)
        input_tokens = max(1, len(prompt_text) // 4)
        self.tokens_in += input_tokens

        # Estimate output tokens based on response type and size
        # For JSON responses (topics, areas): estimate based on expected structure
        # For text responses: estimate ~500 tokens
        if "json" in prompt_text.lower() or "array" in prompt_text.lower():
            # Likely a JSON response - estimate moderate size
            output_tokens = 800
        else:
            output_tokens = 500
        self.tokens_out += output_tokens

        # Propagate to wrapped provider
        if hasattr(self._wrapped, 'tokens_in'):
            self._wrapped.tokens_in += input_tokens
        if hasattr(self._wrapped, 'tokens_out'):
            self._wrapped.tokens_out += output_tokens

        # Return simple stubs that parse without breaking bootstrap logic
        # (Actual card count will be estimated via heuristic, not from these stubs)
        if "topic card" in prompt_text.lower():
            return '[{"id":"stub","name":"Stub","keywords":["stub"],"summary":"Dry run stub","files":[]}]'
        elif "area" in prompt_text.lower() and "identify" in prompt_text.lower():
            return '[{"name":"Stub Area","description":"Dry run stub","files":[]}]'
        elif "merge" in prompt_text.lower() or "group" in prompt_text.lower():
            return "[[0]]"
        else:
            return "Dry run estimation only."

    def list_models(self) -> List[str]:
        """Delegate to wrapped provider."""
        return self._wrapped.list_models()
