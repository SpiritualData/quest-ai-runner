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
from ..core.prompt_layers import blocks_to_prompt, cache_control_indices
from .retry_utils import retry_transient


def build_cached_system(system: Optional[str], layers: List[Dict[str, Any]]) -> tuple:
    """Render prompt-layer blocks into an Anthropic ``system`` array + the volatile tail text.

    The stable (``cache=True``) blocks become ``system`` text blocks; each one that should carry a
    breakpoint gets ``cache_control: {"type": "ephemeral"}`` so the provider caches the prefix up to
    and including it. The number of breakpoints is capped (``cache_control_indices``) so a request
    can never exceed Anthropic's four-breakpoint limit. A plain ``system`` string (e.g. the reply
    voice contract) rides first as an uncached text block so it stays part of the cached prefix
    across turns without spending one of the capped breakpoints. The single ``cache=False`` tail
    block is returned separately to become the user turn. Returns ``(system_array, tail_text)``.
    """
    cached = [b for b in (layers or []) if b.get("cache")]
    tail_text = blocks_to_prompt([b for b in (layers or []) if not b.get("cache")])
    system_array: List[Dict[str, Any]] = []
    if system and system.strip():
        system_array.append({"type": "text", "text": system})
    breakpoints = set(cache_control_indices(cached))
    for i, block in enumerate(cached):
        entry: Dict[str, Any] = {"type": "text", "text": str(block.get("text") or "")}
        if i in breakpoints:
            entry["cache_control"] = {"type": "ephemeral"}
        system_array.append(entry)
    return system_array, tail_text


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
    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             layers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        client = self._get_client()
        self.call_count += 1
        # Layered path: cache-eligible head + context ride in the ``system`` array with
        # cache_control breakpoints; the volatile tail is the user turn. Without layers, the plain
        # prompt is the user turn exactly as before.
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_plan_tokens,
            "tools": [tool_schema],
            "tool_choice": {"type": "tool", "name": tool_schema["name"]},
        }
        if layers:
            system_array, tail_text = build_cached_system(None, layers)
            if system_array:
                kwargs["system"] = system_array
            kwargs["messages"] = [{"role": "user", "content": tail_text}]
        else:
            kwargs["messages"] = [{"role": "user", "content": prompt}]
        resp = client.messages.create(**kwargs)
        if hasattr(resp, "usage"):
            self.tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            self.tokens_out += getattr(resp.usage, "output_tokens", 0) or 0
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_schema["name"]:
                return dict(block.input or {})
        raise RuntimeError("planner returned no structured decision")

    @retry_transient(max_retries=3, base_delay=1.0)
    def answer(self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None,
               layers: Optional[List[Dict[str, Any]]] = None) -> str:
        client = self._get_client()
        # Layered path: the cache-eligible head + context become the ``system`` array (with the
        # reply-voice ``system`` string riding first, uncached, so it stays in the cached prefix),
        # and the volatile tail becomes the single user turn. Multimodal / multi-turn callers pass
        # no ``layers`` and keep the message-list path below unchanged.
        if layers:
            system_array, tail_text = build_cached_system(system, layers)
            create_kwargs: Dict[str, Any] = {
                "model": model,
                "max_tokens": self.max_answer_tokens,
                "messages": [{"role": "user", "content": tail_text}],
            }
            if system_array:
                create_kwargs["system"] = system_array
            self.call_count += 1
            resp = client.messages.create(**create_kwargs)
            if hasattr(resp, "usage"):
                self.tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
                self.tokens_out += getattr(resp.usage, "output_tokens", 0) or 0
            return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
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

    def supports_web_search(self, model: Optional[str] = None) -> bool:
        """Claude ships a native web_search server tool; available whenever the key is set."""
        return bool(self._api_key)

    @retry_transient(max_retries=3, base_delay=1.0)
    def web_search(self, query: str, *, model: str, max_results: int = 5) -> Dict[str, Any]:
        """Search the live web via Claude's native web_search server tool (no extra key).

        Returns ``{"answer": <synthesized text>, "results": [{"title","url","snippet"}, ...]}``.
        ``max_results`` also caps the number of server-side searches (Anthropic's max_uses,
        1..5) so a single call stays fast and cheap.
        """
        client = self._get_client()
        max_uses = max(1, min(int(max_results), 5))
        self.call_count += 1
        resp = client.messages.create(
            model=model,
            max_tokens=self.max_answer_tokens,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
            messages=[{"role": "user", "content": query}],
        )
        if hasattr(resp, "usage"):
            self.tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            self.tokens_out += getattr(resp.usage, "output_tokens", 0) or 0

        answer_parts: List[str] = []
        results: List[Dict[str, str]] = []
        seen = set()

        def _add(title: str, url: str, snippet: str = "") -> None:
            if url and url not in seen:
                seen.add(url)
                results.append({"title": title or "", "url": url, "snippet": snippet or ""})

        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                answer_parts.append(getattr(block, "text", "") or "")
                for cit in (getattr(block, "citations", None) or []):
                    _add(getattr(cit, "title", ""), getattr(cit, "url", ""), getattr(cit, "cited_text", ""))
            elif btype == "web_search_tool_result":
                for r in (getattr(block, "content", None) or []):
                    if getattr(r, "type", None) == "web_search_result":
                        _add(getattr(r, "title", ""), getattr(r, "url", ""))
        return {"answer": "".join(answer_parts), "results": results[:max_results]}

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
