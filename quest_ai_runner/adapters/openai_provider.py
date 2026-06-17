"""OpenAI Provider — OpenAI API model provider (plan / answer / list_models).

Wraps the OpenAI SDK. Three responsibilities:
  * ``plan``  — one structured planner call using JSON mode for forced output.
  * ``answer`` — a normal grounded completion from a message list.
  * ``list_models`` — list available OpenAI models (cached).

The ``openai`` package is imported lazily so the library imports cleanly
even when the SDK isn't installed.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ..core.adapters import ModelProviderBase


class OpenAIProvider(ModelProviderBase):
    def __init__(self, *, api_key: Optional[str] = None, cache_seconds: float = 3600.0):
        super().__init__()
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.cache_seconds = cache_seconds
        self._client = None
        self._models_cache: Optional[List[str]] = None
        self._models_cached_at = 0.0
        # Token counts from OpenAI API responses
        self.tokens_in: int = 0
        self.tokens_out: int = 0

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI  # lazy
            except ImportError:
                raise RuntimeError(
                    "openai is not installed. "
                    "Install it with: pip install openai"
                )
            if not self._api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Run the planner with JSON mode forced output.

        OpenAI's JSON mode (response_format={'type': 'json_object'}) ensures valid JSON output.
        """
        client = self._get_client()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        if hasattr(response, "usage"):
            self.tokens_in += getattr(response.usage, "prompt_tokens", 0) or 0
            self.tokens_out += getattr(response.usage, "completion_tokens", 0) or 0
        try:
            content = response.choices[0].message.content
            return json.loads(content) if content else {}
        except (json.JSONDecodeError, AttributeError, IndexError):
            return {}

    def answer(self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None) -> str:
        """Generate an answer from a conversation history."""
        client = self._get_client()
        # Convert messages to OpenAI format
        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            # Handle both string content and multimodal content lists
            api_messages.append({"role": role, "content": content})

        response = client.chat.completions.create(
            model=model,
            messages=api_messages,
        )
        if hasattr(response, "usage"):
            self.tokens_in += getattr(response.usage, "prompt_tokens", 0) or 0
            self.tokens_out += getattr(response.usage, "completion_tokens", 0) or 0

        return response.choices[0].message.content if response and response.choices else ""

    def list_models(self) -> List[str]:
        """List available OpenAI models."""
        now = time.monotonic()
        if self._models_cache is not None and (now - self._models_cached_at) < self.cache_seconds:
            return self._models_cache

        try:
            client = self._get_client()
            # List all models owned by OpenAI (first-party models)
            models = client.models.list()
            # Extract model IDs and filter to commonly used ones
            model_ids = [
                m.id for m in models.data
                if hasattr(m, "id") and ("gpt" in m.id.lower())
            ]
            self._models_cache = model_ids
            self._models_cached_at = now
            return model_ids
        except Exception:  # noqa: BLE001
            # Return fallback model list if API fails
            self._models_cache = ["gpt-4o", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo"]
            self._models_cached_at = now
            return self._models_cache
