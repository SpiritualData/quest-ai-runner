"""Model registry — tier (haiku/sonnet/opus) -> the CURRENT top model id for that family.

Ported generic from the cockpit's ``model_registry``. The brain picks a TIER per step
("haiku" for triage, "sonnet" for most answers, "opus" for hard reasoning + deep runs); this
module maps a tier to a concrete, live model id.

The source of ids is a pluggable ``ModelProvider.list_models()`` (a live, latest-first id list,
e.g. from ``client.models.list()``) — NOT pinned versions, NOT env vars. We bucket the list by
family substring and take the FIRST (latest) of each. A last-known fallback map is used ONLY
when the live list is empty/unreachable, so resolution never dies.

The bucketed result is cached against the identity of the live list, so we only re-bucket when
the provider's list actually changes (the provider is expected to cache its own ``list_models``).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .adapters import ModelProvider

# Tier names in capability order (cheap -> expensive). The brain uses these names.
TIERS = ("haiku", "sonnet", "opus")


# ---------------------------------------------------------------------------
# Vision-capability seam — the ONE place "can this model take images natively?"
# is decided. The multimodal handler (core.attachments) and the orchestrator ask
# HERE, never inline a model-name check of their own.
#
# Capability is keyed by MODEL FAMILY, not by pinned id, so newer dated/point
# releases of a known-vision family resolve correctly without a registry edit:
#   * Anthropic Claude 3.x / 4.x (opus | sonnet | haiku)   → vision
#   * Google Gemini 1.5 / 2.x / 3.x                        → vision
#   * OpenAI gpt-4o, gpt-4.1, and the o-series (o1/o3/o4)  → vision
# Anything not matched (incl. unknown families and older text-only models) is
# treated as NOT vision-capable, so we describe-fallback rather than send an
# image to a model that would reject it. ``None``/empty → False.
#
# A consumer can extend this by appending to ``VISION_FAMILY_PATTERNS`` (e.g. to
# teach the runner a new provider's vision family).
# ---------------------------------------------------------------------------

# Each entry is a compiled regex matched (case-insensitively) against the model id.
VISION_FAMILY_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # Anthropic Claude 3.x and 4.x — all tiers are vision-capable. Matches
        # "claude-3-5-sonnet…", "claude-3-opus…", "claude-sonnet-4-6", "claude-opus-4-8",
        # the family aliases ("opus"/"sonnet"/"haiku"), and Fable-style ids.
        r"claude[-_]?3",
        r"claude[-_]?(?:opus|sonnet|haiku)[-_]?4",
        r"claude[-_]?4[-_]?(?:opus|sonnet|haiku)",
        r"^(?:opus|sonnet|haiku)$",                 # bare CLI family aliases
        # Google Gemini 1.5 / 2.x / 3.x — all vision-capable.
        r"gemini[-_]?(?:1\.5|2|3)",
        # OpenAI multimodal: gpt-4o, gpt-4.1, and the reasoning o-series (o1/o3/o4).
        r"gpt[-_]?4o",
        r"gpt[-_]?4\.1",
        r"\bo[134]\b",
        r"^o[134][-_]",
    )
]


def is_vision_capable(model: Optional[str]) -> bool:
    """Whether ``model`` can accept images as NATIVE input (vs. needing describe-fallback).

    The single source of truth for vision capability. Keyed by model FAMILY (regex over the
    id), so dated/point releases of a known family resolve without a registry change. Unknown
    or text-only families → ``False``. Never raises.
    """
    mid = (model or "").strip()
    if not mid:
        return False
    return any(p.search(mid) for p in VISION_FAMILY_PATTERNS)

# LAST-KNOWN fallback ONLY — used when the live list is empty/unreachable. NOT the primary
# source. A consumer can override this map via ModelRegistry(fallback=...).
DEFAULT_FALLBACK_TOP = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def bucket_top(models: List[str], fallback: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Bucket a latest-first id list by family substring, taking the FIRST (latest) of each.

    Families the live list doesn't cover fall back to ``fallback`` so all tiers always resolve.
    Pure function — exposed for direct testing of the bucketing logic.
    """
    fb = fallback or DEFAULT_FALLBACK_TOP
    top: Dict[str, str] = {}
    for mid in models or []:
        low = (mid or "").lower()
        for tier in TIERS:
            if tier in low and tier not in top:
                top[tier] = mid
    for tier in TIERS:
        top.setdefault(tier, fb[tier])
    return top


class ModelRegistry:
    """Resolves tier -> live top model id from a ModelProvider."""

    def __init__(self, provider: ModelProvider, *, fallback: Optional[Dict[str, str]] = None):
        self._provider = provider
        self._fallback = dict(fallback or DEFAULT_FALLBACK_TOP)
        self._cache: Dict[str, object] = {"source_id": None, "top": None}

    def top_models(self) -> Dict[str, str]:
        """``{"opus": id, "sonnet": id, "haiku": id}`` for the current latest models.

        Falls back to the last-known map if the provider's list is empty/unreachable.
        Re-buckets only when the provider returns a new list object.
        """
        try:
            models = self._provider.list_models()
        except Exception:  # noqa: BLE001 — a provider hiccup must never break resolution
            models = []
        if not models:
            return dict(self._fallback)
        if self._cache["source_id"] != id(models) or self._cache["top"] is None:
            self._cache["source_id"] = id(models)
            self._cache["top"] = bucket_top(models, self._fallback)
        return dict(self._cache["top"])  # copy so callers can't mutate the cache

    def resolve_tier(self, tier: Optional[str]) -> str:
        """Resolve a tier name to the current top model id. Unknown/None -> "sonnet". Never raises."""
        t = (tier or "sonnet").strip().lower()
        if t not in TIERS:
            t = "sonnet"
        return self.top_models()[t]
