"""GeminiProvider — Google Gemini model provider (plan / answer / list_models).

Wraps the Google Generative AI SDK. Three responsibilities:
  * ``plan``  — one cheap, FORCED-structured planner call (returns JSON decision dict).
  * ``answer`` — a normal grounded completion from a message list.
  * ``list_models`` — list available Gemini models (cached).

The ``google-generativeai`` package is imported lazily so the library imports
cleanly even when the SDK isn't installed.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ..core.adapters import ModelProviderBase


class GeminiProvider(ModelProviderBase):
    def __init__(self, *, api_key: Optional[str] = None, cache_seconds: float = 3600.0):
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
                import google.generativeai as genai  # lazy
            except ImportError:
                raise RuntimeError(
                    "google-generativeai is not installed. "
                    "Install it with: pip install google-generativeai"
                )
            if not self._api_key:
                raise RuntimeError("GOOGLE_API_KEY is not configured")
            genai.configure(api_key=self._api_key)
            self._client = genai
        return self._client

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Run the planner with structured JSON output.

        Gemini doesn't have tool_choice like Anthropic, so we use forced JSON mode
        and extract the response.
        """
        client = self._get_client()
        model_obj = client.GenerativeModel(model_name=model)
        # Request structured JSON output
        response = model_obj.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, AttributeError):
            # Fallback: return empty dict on parse failure
            return {}

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
        model_obj = client.GenerativeModel(model_name=model)
        response = model_obj.generate_content(full_prompt)
        return response.text if response and response.text else ""

    def list_models(self) -> List[str]:
        """List available Gemini models."""
        now = time.monotonic()
        if self._models_cache is not None and (now - self._models_cached_at) < self.cache_seconds:
            return self._models_cache

        try:
            client = self._get_client()
            # Use the genai.list_models() method to get available models
            models = client.list_models()
            # Filter to Gemini models and extract names
            gemini_models = [
                m.name for m in models
                if hasattr(m, "name") and "gemini" in m.name.lower()
            ]
            self._models_cache = gemini_models
            self._models_cached_at = now
            return gemini_models
        except Exception:  # noqa: BLE001
            # Return fallback model list if API fails
            self._models_cache = ["gemini-2.0-flash", "gemini-1.5-pro"]
            self._models_cached_at = now
            return self._models_cache
