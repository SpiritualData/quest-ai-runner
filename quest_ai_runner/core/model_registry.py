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
# Semantic names (provider-agnostic): fast/balanced/quality (best = quality if not overridden)
TIERS = ("fast", "balanced", "quality", "best")


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

# LAST-KNOWN fallback — used for tiers not explicitly overridden, and when the live list is
# empty/unreachable. A consumer can override this map via ModelRegistry(fallback=...).
# User-specified models (via fallback) take FULL precedence and bypass auto-bucketing entirely.
DEFAULT_FALLBACK_TOP = {
    "fast": "claude-haiku-4-5",
    "balanced": "claude-sonnet-4-6",
    "quality": "claude-opus-4-8",
    "best": "claude-opus-4-8",  # defaults to quality tier
}


def bucket_top(models: List[str], fallback: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Resolve tier -> model by auto-bucketing from live list, then applying fallback overrides.

    Process:
    1. Bucket live models by family (claude-haiku, claude-sonnet, claude-opus, gemini-1.5, gemini-2.0, gpt-4o, etc.)
    2. Map each family to a semantic tier (fast/balanced/quality)
    3. For each tier, use: user-specified (in fallback) > auto-bucketed > DEFAULT_FALLBACK_TOP

    User-specified models (passed via fallback) take FULL precedence for any tier they specify.
    When a user specifies QAR_MODEL_BALANCED=gpt-4o, that exact model is used even if not in
    the live provider list.

    Pure function — exposed for testing.
    """
    fb = dict(fallback or {})
    result = dict(DEFAULT_FALLBACK_TOP)

    if not models:
        # No live models; apply user overrides to defaults and return
        result.update(fb)
        return result

    # Auto-bucket the live list by family; infer tier assignment from family name + position
    families = {}  # family -> [model, model, ...]
    for m in models:
        # Infer family from model name (exact patterns depend on provider)
        if "claude" in m.lower():
            if "haiku" in m.lower():
                families.setdefault("claude-haiku", []).append(m)
            elif "opus" in m.lower():
                families.setdefault("claude-opus", []).append(m)
            elif "sonnet" in m.lower():
                families.setdefault("claude-sonnet", []).append(m)
            else:
                families.setdefault("claude-other", []).append(m)
        elif "gemini" in m.lower():
            if "1.5" in m or "1-5" in m:
                families.setdefault("gemini-1.5", []).append(m)
            elif "3" in m:
                families.setdefault("gemini-3", []).append(m)
            else:
                families.setdefault("gemini-2.0", []).append(m)
        elif "gpt-4o" in m.lower():
            families.setdefault("gpt-4o", []).append(m)
        elif re.search(r"\bo[134]\b", m.lower()):
            families.setdefault("o-series", []).append(m)
        else:
            families.setdefault("other", []).append(m)

    # Map families to tiers; take the first (newest) model of each family.
    # Priority: more capable families for higher tiers.
    fast_candidates = [
        families.get("claude-haiku", [None])[0],
        families.get("gemini-1.5", [None])[0],
        families.get("gpt-4o", [None])[0],
    ]
    balanced_candidates = [
        families.get("gemini-2.0", [None])[0],
        families.get("claude-sonnet", [None])[0],
        families.get("gemini-1.5", [None])[0],
        families.get("o-series", [None])[0],
    ]
    quality_candidates = [
        families.get("claude-opus", [None])[0],
        families.get("gemini-2.0", [None])[0],
        families.get("gemini-3", [None])[0],
        families.get("o-series", [None])[0],
    ]

    # Assign: use the first non-None candidate for each tier
    if any(fast_candidates):
        result["fast"] = next((m for m in fast_candidates if m), result["fast"])
    if any(balanced_candidates):
        result["balanced"] = next((m for m in balanced_candidates if m), result["balanced"])
    if any(quality_candidates):
        result["quality"] = next((m for m in quality_candidates if m), result["quality"])
    # best defaults to quality
    result["best"] = result.get("best") or result["quality"]

    # Apply user overrides (these take full precedence over auto-bucketed)
    result.update(fb)
    return result


class ModelRegistry:
    """Resolves tier -> model id, using fallback (user-specified or defaults)."""

    def __init__(self, provider: ModelProvider, *, fallback: Optional[Dict[str, str]] = None):
        self._provider = provider
        # Keep only user-specified overrides, not defaults
        self._user_overrides = dict(fallback or {})
        self._cache: Dict[str, object] = {"source_id": None, "top": None}

    def top_models(self) -> Dict[str, str]:
        """Return tier -> model mapping from auto-bucketing + user overrides.

        User-specified models (QAR_MODEL_*) override auto-bucketed/defaults completely.
        Falls back to defaults if nothing specified for a tier.
        """
        try:
            models = self._provider.list_models()
        except Exception:  # noqa: BLE001 — a provider hiccup must never break resolution
            models = []
        if not models:
            result = dict(DEFAULT_FALLBACK_TOP)
            result.update(self._user_overrides)
            return result
        if self._cache["source_id"] != id(models) or self._cache["top"] is None:
            self._cache["source_id"] = id(models)
            self._cache["top"] = bucket_top(models, self._user_overrides)
        return dict(self._cache["top"])  # copy so callers can't mutate the cache

    def resolve_tier(self, tier: Optional[str]) -> str:
        """Resolve a tier name to the current top model id. Unknown/None -> "balanced". Never raises."""
        # Map old provider-specific tier names to new semantic names for backward compatibility
        tier_map = {
            "haiku": "fast",
            "sonnet": "balanced",
            "opus": "quality",
        }
        t = (tier or "balanced").strip().lower()
        # Check if it's an old tier name and map it
        if t in tier_map:
            t = tier_map[t]
        # If still not in TIERS, default to balanced
        if t not in TIERS:
            t = "balanced"
        return self.top_models()[t]
