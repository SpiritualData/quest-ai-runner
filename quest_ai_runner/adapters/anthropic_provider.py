"""AnthropicProvider — the reference ModelProvider (plan / answer / list_models).

Wraps the Anthropic SDK. Three responsibilities:
  * ``plan``  — one cheap, FORCED-structured planner call (tool_choice forces the ``decide`` tool)
                returning the raw decision dict the brain normalizes.
  * ``answer`` — a normal grounded completion from a message list.
  * ``list_models`` — the live, latest-first model id list (cached for ``cache_seconds``) that
                ``ModelRegistry`` buckets into haiku/sonnet/opus tiers. NOTHING pinned, no env.

The ``anthropic`` package is imported lazily so the library imports cleanly (and the core brain
is testable with a stub provider) even where the SDK isn't installed.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from ..core.adapters import ModelProviderBase
from .retry_utils import retry_transient


class AnthropicProvider(ModelProviderBase):
    def __init__(self, *, api_key: Optional[str] = None, cache_seconds: float = 3600.0,
                 max_answer_tokens: int = 2048, max_plan_tokens: int = 800):
        super().__init__()
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.cache_seconds = cache_seconds
        self.max_answer_tokens = max_answer_tokens
        self.max_plan_tokens = max_plan_tokens
        self._client = None
        self._models_cache: Optional[List[str]] = None
        self._models_cached_at = 0.0
        # Accumulated token counts for the current turn; reset by Orchestrator.run() at the
        # start of each turn and read by finish() to populate OrchestratorResult.tokens_in/out.
        self.tokens_in: int = 0
        self.tokens_out: int = 0

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy
            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured")
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    @retry_transient(max_retries=3, base_delay=1.0)
    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        client = self._get_client()
        self.call_count += 1
        resp = client.messages.create(
            model=model,
            max_tokens=self.max_plan_tokens,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        if hasattr(resp, "usage"):
            self.tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            self.tokens_out += getattr(resp.usage, "output_tokens", 0) or 0
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_schema["name"]:
                return dict(block.input or {})
        raise RuntimeError("planner returned no structured decision")

    @retry_transient(max_retries=3, base_delay=1.0)
    def answer(self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None) -> str:
        client = self._get_client()
        # A message's ``content`` may be a plain string (the common path, unchanged) OR a LIST of
        # Anthropic content blocks (text + image), e.g.
        #   {"role": "user", "content": [
        #       {"type": "text", "text": "..."},
        #       {"type": "image", "source": {"type": "base64", "media_type": "image/png",
        #                                    "data": "<b64>"}}]}
        # The SDK's messages.create already accepts both shapes, so we pass content THROUGH
        # unflattened — the multimodal handler (core.attachments) produces the image blocks.
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_answer_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        self.call_count += 1
        resp = client.messages.create(**kwargs)
        if hasattr(resp, "usage"):
            self.tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            self.tokens_out += getattr(resp.usage, "output_tokens", 0) or 0
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")

    @retry_transient(max_retries=2, base_delay=1.0)
    def _list_models_api(self) -> List[str]:
        """Call Anthropic API to list models; wrapped by list_models() for caching."""
        client = self._get_client()
        page = client.models.list()
        return [m.id for m in getattr(page, "data", []) if getattr(m, "id", None)]

    def list_models(self) -> List[str]:
        now = time.monotonic()
        if self._models_cache is not None and (now - self._models_cached_at) < self.cache_seconds:
            return self._models_cache
        try:
            ids = self._list_models_api()
        except Exception:  # noqa: BLE001 — empty list -> registry uses its fallback map
            ids = []
        # client.models.list() is already latest-first; keep it as returned.
        self._models_cache = ids
        self._models_cached_at = now
        return ids
