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

import logging
import re
from typing import Dict, List, Optional

from .adapters import ModelProvider

# Tier names in capability order (cheap -> expensive). The brain uses these names.
# Semantic names (provider-agnostic): fast/balanced/quality (best = quality if not overridden)
TIERS = ("fast", "balanced", "quality", "best")

log = logging.getLogger("quest-ai-runner.model_registry")

# Provider-specific tier names kept working for backward compatibility. These are TIER names, not
# model ids: "opus" here means "whatever the quality tier resolves to on this deployment", which is
# why they must never be mistaken for a literal model pin (see ``is_tier_name``).
LEGACY_TIER_ALIASES = {
    "haiku": "fast",
    "sonnet": "balanced",
    "opus": "quality",
}

# Downgrade order (expensive -> cheap), used when a tier's resolved model exhausts its
# quota/rate limit: stepping down to a cheaper tier keeps the caller answered instead of
# erroring out, since backoff alone does not fix a per-model DAILY quota. See
# MultiProvider's tier-fallback wrapping (added 2026-07-14 alongside enforced per-model
# daily Gemini quotas).
TIER_DOWNGRADE_ORDER = {
    "best": "quality",
    "quality": "balanced",
    "balanced": "fast",
    # "fast" has no lower tier to fall back to.
}


def next_lower_tier(tier: Optional[str]) -> Optional[str]:
    """The next cheaper tier to fall back to when ``tier``'s model is exhausted.

    Args:
        tier: The tier whose resolved model just failed (quota/rate limit).

    Returns:
        The next cheaper tier name, or None if ``tier`` is unset/unrecognized or
        already the cheapest ("fast").
    """
    if not tier:
        return None
    return TIER_DOWNGRADE_ORDER.get(tier)


# ---------------------------------------------------------------------------
# Vision-capability seam — the ONE place "can this model take images natively?"
# is decided. The multimodal handler (core.attachments) and the orchestrator ask
# HERE, never inline a model-name check of their own.
#
# Capability is keyed by MODEL FAMILY, not by pinned id, so newer dated/point
# releases of a known-vision family resolve correctly without a registry edit:
#   * Anthropic Claude, version 3 and up, any family
#     (opus | sonnet | haiku | fable | mythos)          → vision
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
        # Anthropic Claude, version 3 and up, any family — all tiers are vision-capable.
        # "claude[-_]?3" alone covers the odd "claude-3-5-sonnet…"/"claude-3-opus…" 3.x id shapes
        # (a minor-version digit sits between the major version and the family name, so the
        # family/version-scoped patterns below don't reach them). The two patterns below match
        # every current family (opus/sonnet/haiku/fable/mythos) paired with a major version 3-9,
        # in either id order: "claude-sonnet-4-6", "claude-opus-4-8", "claude-opus-5",
        # "claude-fable-5-1", "claude-mythos-5-1", and the older "claude-3-opus" order.
        r"claude[-_]?3",
        r"claude[-_]?(?:opus|sonnet|haiku|fable|mythos)[-_]?[3-9]",
        r"claude[-_]?[3-9][-_]?(?:opus|sonnet|haiku|fable|mythos)",
        r"^(?:opus|sonnet|haiku|fable)$",            # bare CLI family aliases
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
# Balanced uses flash-lite (cheap, high-volume filtering/judgment work); quality stays on
# gemini-3.5-flash for tasks that need the stronger model.
DEFAULT_FALLBACK_TOP = {
    "fast": "gemini-3.1-flash-lite",
    "balanced": "gemini-3.1-flash-lite",
    "quality": "gemini-3.5-flash",
    "best": "claude-opus-5",  # last-known-good pin, bumped as newer Opus releases confirm (2026-09-22); the
    # live list_models() path above is what actually keeps pace release-to-release, this only
    # covers a live-list outage
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
    """Resolves tier -> model id, using fallback (user-specified or defaults).

    Supports multi-provider operation: can route different tiers to different providers.
    Default behavior (single provider) unchanged for backward compatibility.
    """

    def __init__(
        self,
        provider: ModelProvider,
        *,
        fallback: Optional[Dict[str, str]] = None,
        providers: Optional[Dict[str, ModelProvider]] = None,
        provider_overrides: Optional[Dict[str, str]] = None,
    ):
        """Initialize ModelRegistry with optional multi-provider support.

        Args:
            provider: Primary provider (used for all tiers by default)
            fallback: User-specified model overrides per tier (QAR_MODEL_*)
            providers: Optional dict of provider_name -> ModelProvider for multi-provider
            provider_overrides: Optional dict of tier -> provider_name for per-tier routing
                               (e.g. {"best": "anthropic", "fast": "gemini"})
        """
        self._provider = provider
        self._providers = providers or {}
        self._provider_overrides = dict(provider_overrides or {})
        # Keep only user-specified overrides, not defaults
        self._user_overrides = dict(fallback or {})
        self._cache: Dict[str, object] = {"source_id": None, "top": None}
        # Names ``resolve_tier`` has already warned about substituting, so the warning names each
        # distinct one once rather than on every call.
        self._warned_unknown_tiers: set = set()

    def get_provider_for_tier(self, tier: str) -> ModelProvider:
        """Get the provider to use for a given tier (supports per-tier provider routing).

        Returns the provider specified in provider_overrides, or the primary provider.
        """
        provider_name = self._provider_overrides.get(tier)
        if provider_name and provider_name in self._providers:
            return self._providers[provider_name]
        return self._provider

    def top_models(self) -> Dict[str, str]:
        """Return tier -> model mapping from auto-bucketing + user overrides.

        With multi-provider support, each tier's models come from its assigned provider.
        User-specified models (QAR_MODEL_*) override auto-bucketed/defaults completely.
        Falls back to defaults if nothing specified for a tier.
        """
        # Collect models from all providers (primary + per-tier overrides)
        all_models = []
        for tier in TIERS:
            provider = self.get_provider_for_tier(tier)
            try:
                models = provider.list_models()
                all_models.extend(models)
            except Exception:  # noqa: BLE001 — a provider hiccup must never break resolution
                pass

        if not all_models:
            result = dict(DEFAULT_FALLBACK_TOP)
            result.update(self._user_overrides)
            return result

        # Cache is invalidated if the combined model set changes
        # (simple approach: use tuple of all model lists as cache key)
        cache_key = tuple(sorted(set(all_models)))
        if self._cache["source_id"] != cache_key or self._cache["top"] is None:
            self._cache["source_id"] = cache_key
            self._cache["top"] = bucket_top(all_models, self._user_overrides)
        return dict(self._cache["top"])  # copy so callers can't mutate the cache

    @staticmethod
    def is_tier_name(name: Optional[str]) -> bool:
        """Whether ``name`` is a TIER this registry owns, rather than a concrete model id.

        The four semantic tiers plus the legacy provider-specific aliases. Callers that accept
        "a tier OR a model id" in one field (a task's stored ``model``, a guidance preference) use
        this to tell the two apart: a tier goes through ``resolve_tier``, a model id must be honoured
        verbatim instead of being fed to ``resolve_tier``, which would silently rewrite it (see the
        warning there). ``None``/blank is not a tier name: it is "unspecified".
        """
        n = (name or "").strip().lower()
        return bool(n) and (n in TIERS or n in LEGACY_TIER_ALIASES)

    def resolve_tier(self, tier: Optional[str]) -> str:
        """Resolve a tier name to the current top model id. Unknown/None -> "balanced". Never raises.

        NOTE the asymmetry this WARNS about: a name that is not a tier at all (a concrete model id
        such as ``fable``, handed in where a tier was expected) is not an error here: it silently
        becomes "balanced", i.e. whatever this deployment's balanced tier resolves to. That is the
        right degradation for a genuinely unknown tier and the wrong one for a model the caller
        meant literally, and because it was silent it was invisible: a lane that pinned Fable ran
        every deep task on its balanced model for weeks with nothing in the log saying so. Callers
        holding a field that may be EITHER a tier or a model id must ask ``is_tier_name`` first.
        """
        t = (tier or "balanced").strip().lower()
        # Check if it's an old provider-specific tier name and map it to the semantic one
        if t in LEGACY_TIER_ALIASES:
            t = LEGACY_TIER_ALIASES[t]
        # If still not in TIERS, default to balanced, and say so ONCE per distinct name, so the
        # substitution is discoverable without spamming a line on every call that repeats it.
        if t not in TIERS:
            if t and t not in self._warned_unknown_tiers:
                self._warned_unknown_tiers.add(t)
                log.warning(
                    "model tier %r is not a known tier (%s), so it resolves as \"balanced\" "
                    "instead. If this is a MODEL ID rather than a tier, the caller should check "
                    "ModelRegistry.is_tier_name() and pin the id verbatim; resolving it here "
                    "silently substitutes a different model.",
                    tier, ", ".join(TIERS))
            t = "balanced"
        return self.top_models()[t]
