"""GeminiProvider — Google Gemini model provider (plan / answer / list_models).

Wraps the Google Genai SDK. Three responsibilities:
  * ``plan``  — one cheap, FORCED-structured planner call (returns JSON decision dict).
  * ``answer`` — a normal grounded completion from a message list.
  * ``list_models`` — list available Gemini models (cached).

The ``google-genai`` package is imported lazily so the library imports
cleanly even when the SDK isn't installed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from ..core.adapters import ModelProviderBase
from .retry_utils import parse_json_with_retry, retry_transient

_log = logging.getLogger("quest-ai-runner.gemini")


class GeminiProvider(ModelProviderBase):
    def __init__(self, *, api_key: Optional[str] = None, cache_seconds: float = 3600.0):
        super().__init__()
        self._api_key = api_key or os.getenv("GOOGLE_API_KEY")
        self.cache_seconds = cache_seconds
        self._client = None
        self._models_cache: Optional[List[str]] = None
        self._models_cached_at = 0.0
        # Token counts not tracked by Gemini SDK (no usage field); just initialize to 0.
        self.tokens_in: int = 0
        self.tokens_out: int = 0

    def _get_client(self):
        if self._client is None:
            try:
                import google.genai  # lazy
            except ImportError:
                raise RuntimeError(
                    "google-genai is not installed. "
                    "Install it with: pip install google-genai"
                )
            if not self._api_key:
                raise RuntimeError("GOOGLE_API_KEY is not configured")
            self._client = google.genai.Client(api_key=self._api_key)
        return self._client

    @retry_transient(max_retries=3, base_delay=1.0)
    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Run the planner with structured JSON output.

        Gemini doesn't have tool_choice like Anthropic, so we use forced JSON mode
        and extract the response.
        """
        client = self._get_client()

        def _call() -> str:
            # A FRESH structured-JSON generation each attempt, so a malformed response is re-asked.
            self.call_count += 1
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            return response.text

        try:
            # Standard JSON-output retry: re-ask the model if its structured output won't parse into
            # a dict/list, instead of silently degrading to {} on the first bad shape.
            return parse_json_with_retry(
                _call, validate=lambda o: isinstance(o, (dict, list)), label="gemini planner")
        except Exception:  # noqa: BLE001 — after retries, fall back to a safe empty decision
            return {}

    @retry_transient(max_retries=3, base_delay=1.0)
    def answer(self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None) -> str:
        """Generate an answer from a conversation history.

        Gemini API expects a single prompt, so we convert the message list to text.
        """
        client = self._get_client()
        # Convert message list to prompt text
        prompt_parts = []
        if system:
            prompt_parts.append(f"System: {system}\n")
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str):
                prompt_parts.append(f"{role.capitalize()}: {content}\n")
            elif isinstance(content, list):
                # Handle multimodal content (text + images)
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            prompt_parts.append(f"{role.capitalize()}: {block.get('text', '')}\n")
                        elif block.get("type") == "image":
                            # Gemini SDK will handle image blocks if passed correctly
                            # For now, just note that an image was present
                            prompt_parts.append(f"[Image included]\n")

        full_prompt = "".join(prompt_parts).strip()
        self.call_count += 1
        response = client.models.generate_content(
            model=model,
            contents=full_prompt
        )
        return response.text if response and response.text else ""

    @retry_transient(max_retries=2, base_delay=1.0)
    def _list_models_api(self) -> List[str]:
        """Call Gemini API to list models; wrapped by list_models() for caching."""
        client = self._get_client()
        models_list = client.models.list()
        # Known unavailable/deprecated Gemini models to exclude
        exclude_patterns = ["robotics", "experimental", "exp-"]
        gemini_models = [
            m.name for m in models_list
            if hasattr(m, "name") and "gemini" in m.name.lower()
            and not any(pattern in m.name.lower() for pattern in exclude_patterns)
        ]
        # Fallback to known-good models if filtered list is empty
        if not gemini_models:
            _log.warning("Gemini API returned no usable models; using fallback list")
            gemini_models = ["gemini-2.0-flash", "gemini-1.5-pro"]
        return gemini_models

    def list_models(self) -> List[str]:
        """List available Gemini models, excluding known unavailable/deprecated ones."""
        now = time.monotonic()
        if self._models_cache is not None and (now - self._models_cached_at) < self.cache_seconds:
            return self._models_cache

        try:
            gemini_models = self._list_models_api()
            self._models_cache = gemini_models
            self._models_cached_at = now
            return gemini_models
        except Exception:  # noqa: BLE001
            _log.exception("list_models failed for Gemini after retries")
            # Return fallback model list if API fails
            self._models_cache = ["gemini-2.0-flash", "gemini-1.5-pro"]
            self._models_cached_at = now
            return self._models_cache
