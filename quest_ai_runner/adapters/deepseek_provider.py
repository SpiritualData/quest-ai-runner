"""DeepSeek provider: the OpenAI-compatible DeepSeek API behind the OpenAI adapter.

Env: ``DEEPSEEK_API_KEY`` (required, the provider is only registered when it is set),
``DEEPSEEK_BASE_URL`` (default ``https://api.deepseek.com``), ``DEEPSEEK_THINKING`` (default off:
set ``1`` to let the model think before answering, which is slower and costs more output tokens).

Model ids start with ``deepseek`` and route here by prefix. Model ids are listed from the live
``/models`` endpoint, with an explicit fallback list. DeepSeek ids are never resolved into a
fast/balanced/quality/best tier by auto-bucketing: a deployment opts in by pinning
``QAR_MODEL_<TIER>=<deepseek id>``.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .openai_provider import OpenAIProvider

DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAIProvider):
    key_env_name = "DEEPSEEK_API_KEY"
    model_id_marker = "deepseek"
    fallback_models: List[str] = ["deepseek-flash", "deepseek-v4-pro"]

    def __init__(self, *, api_key: Optional[str] = None, cache_seconds: float = 3600.0,
                 base_url: Optional[str] = None):
        super().__init__(
            api_key=api_key, cache_seconds=cache_seconds,
            base_url=base_url or os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL)

    def extra_create_kwargs(self) -> Dict[str, Any]:
        flag = (os.getenv("DEEPSEEK_THINKING") or "").strip().lower()
        if flag in ("1", "true", "yes", "on"):
            return {}
        return {"extra_body": {"thinking": {"type": "disabled"}}}
