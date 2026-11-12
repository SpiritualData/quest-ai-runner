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
from typing import Any, Dict, List, Mapping, Optional, Sequence

from esoi import implementation
from hubofhubs.api import tiers as hub_tiers
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.call_roles import LEGACY_TIER_ROLES, legacy_word_tier, tier_for_call
from hubofhubs.api import newest_in_family, split_tokens
from hubofhubs.api import ModelProvider

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

# Family rules, in order: the first pattern that matches a lower-case model id names its family. DATA, not branches: a new family
# is one more entry. (Lookaheads make a pattern order-independent: "claude and haiku" in any position.)
FAMILY_RULES = tuple((name, re.compile(rx)) for name, rx in (
    ("claude-haiku", r"(?=.*claude)(?=.*haiku)"),
    ("claude-opus", r"(?=.*claude)(?=.*opus)"),
    ("claude-sonnet", r"(?=.*claude)(?=.*sonnet)"),
    ("claude-other", r"claude"),
    ("gemini-1.5", r"(?=.*gemini)(?=.*(1\.5|1-5))"),
    ("gemini-3", r"(?=.*gemini)(?=.*3)"),
    ("gemini-2.0", r"gemini"),
    ("gpt-4o", r"gpt-4o"),
    ("o-series", r"\bo[134]\b"),
))
# Ids never auto-bucketed into a tier: a deployment opts in by pinning QAR_MODEL_<TIER>, so registering such a provider
# (today DeepSeek) cannot change an existing lane's tiers.
EXCLUDED_FROM_TIERS = re.compile(r"^deepseek")

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


# Each tier QAR asks for, and the hub tier that serves it: the hub's own four names (fast, balanced, quality, best), so a tier's default is the
# chooser's public choice (the cheapest and fastest arm that clears the tier's minimum scores on public evaluations) and no model family is
# excluded by rule here. A deployment keeps a model out of its defaults with QAR_EXCLUDED_MODELS or pins a tier with QAR_MODEL_<TIER>.
HUB_TIER_OF = {"fast": "fast", "balanced": "balanced", "quality": "quality", "best": "best"}

# The bars QAR gives the hub's ``select`` and ``pick`` over measured arms (enough shared cases, and a pass rate that would not make the answers
# worse). They no longer decide a tier's model: ``choose`` uses public evaluations only.
TIER_MIN_CASES = 10
TIER_MIN_PASS_RATE = 0.9


def no_credits() -> list:
    return []


# Where the credit rows come from (``provider``, ``balance``, ``allowance``, ``credit_value``). Core reads no environment: the
# package wiring installs the source that does (``set_credit_source``); none installed means no credit is counted.
credit_source = no_credits


def set_credit_source(source) -> None:
    global credit_source
    credit_source = source or no_credits


def no_exclusions() -> tuple:
    return ()


# Model-name fragments a deployment never wants a tier to resolve to (``QAR_EXCLUDED_MODELS``, wired like the credit source). No model
# family is excluded by rule here: the default of each tier is the hub chooser's measured pick, and a deployment that wants a model
# kept out of its defaults says so in its own configuration. A pin or a model a person names is never filtered.
excluded_source = no_exclusions


def set_excluded_source(source) -> None:
    global excluded_source
    excluded_source = source or no_exclusions


def drop_excluded(top: Dict[str, str], patterns) -> Dict[str, str]:
    """``top`` with each tier whose model matches an excluded fragment stepped down to the nearest lower tier's model that does not
    (a tier with nothing below it that is acceptable keeps its model: resolution never fails)."""
    pats = [p for p in (patterns or ()) if p]
    if not pats:
        return dict(top)

    def bad(model: Optional[str]) -> bool:
        return any(p in str(model or "").lower() for p in pats)

    out = dict(top)
    order = ["best", "quality", "balanced", "fast"]
    for index, tier in enumerate(order):
        if tier in out and bad(out[tier]):
            for lower in order[index + 1:]:
                if lower in out and not bad(out[lower]):
                    out[tier] = out[lower]
                    break
    return out


def public_pick(got: Mapping[str, Any]) -> bool:
    """Whether the hub's choice for a tier came from the public metric (an arm priced, current and clearing the tier's minimums), and not from a
    held default or the cheapest price, which are preferences."""
    return bool(got) and bool(got.get("arm")) and got.get("stage") == "public"


def hub_picks(available: set) -> Dict[str, str]:
    """QAR tier -> model id of the hub's public choice, only among the providers the lane can call; a tier with no public choice is absent, so the
    live list or the pin decides it. A key alone never switches provider: the hub names an arm only when it is priced. Balanced is dropped when it
    would equal fast, so stepping down a tier on a quota error still changes model."""
    picks = hub_tiers.choose(available=available)
    out = {t: picks[h]["arm"][1] for t, h in HUB_TIER_OF.items() if h in picks and public_pick(picks[h])}
    if out.get("balanced") and out.get("balanced") == out.get("fast"):
        del out["balanced"]
    return out


def configured_providers(providers, primary=None) -> set:
    """The names of the providers that hold a key (or a keyless endpoint): the only ones the tier selector may offer. The keyless Claude Code
    login runs every Claude family, so a lane on it (as the primary provider or one of the providers) offers the hub's ``anthropic`` models;
    without this a login-only lane offered none, the hub's public choice was never consulted and a tier fell to the live list (best to quality's model)."""
    names = {name for name, p in (providers or {}).items() if getattr(p, "api_key", None) or getattr(p, "keyless", False)}
    from hubofhubs.api import claude_cli as hub_cli
    if any(isinstance(p, hub_cli.ClaudeCliProvider) for p in [primary, *(providers or {}).values()]):
        names.add("anthropic")
    return names


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
        r"^(?:claude[-_]?)?(?:opus|sonnet|haiku|fable)$",   # bare family names/aliases (latest of family)
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
    "best": "claude-sonnet",  # family name, never a release: each provider resolves it to the newest
    # Sonnet (the CLI maps it to the `sonnet` alias, AnthropicProvider.resolve_model to the newest
    # live id), so a new Sonnet release needs no edit here. This is only the unmeasured last resort: the
    # chooser's measured pick and a lane's pins or QAR_EXCLUDED_MODELS decide the rest.
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
    # DeepSeek ids are never auto-bucketed into a tier: a deployment opts in by pinning
    # QAR_MODEL_<TIER>, so registering the DeepSeek provider cannot change an existing lane's tiers.
    models = [m for m in models if not EXCLUDED_FROM_TIERS.search(m.lower())]
    for m in models:
        low = m.lower()
        family = next((name for name, rx in FAMILY_RULES if rx.search(low)), "other")
        families.setdefault(family, []).append(m)

    # Map families to tiers; take the NEWEST model of each family (highest version numbers, not the
    # order the provider listed them in).
    # Priority: more capable families for higher tiers.
    # Gemini/OpenAI tiers come from FAMILY names ("flash-lite", "flash", "pro"), newest live release,
    # so a new Gemini or GPT generation is picked up with no edit here.
    for members in families.values():
        members.sort(key=lambda mid: split_tokens(mid)[1], reverse=True)
    gem_fast = newest_in_family("gemini-flash-lite", models)
    gem_balanced = newest_in_family("gemini-flash", models)
    gem_quality = newest_in_family("gemini-pro", models)
    gpt_fast = newest_in_family("gpt-mini", models)
    gpt_top = newest_in_family("gpt", models)
    fast_candidates = [
        families.get("claude-haiku", [None])[0],
        gem_fast,
        gpt_fast,
        families.get("gemini-1.5", [None])[0],
        families.get("gpt-4o", [None])[0],
    ]
    balanced_candidates = [
        gem_balanced,
        families.get("gemini-2.0", [None])[0],
        families.get("claude-sonnet", [None])[0],
        families.get("gemini-1.5", [None])[0],
        families.get("o-series", [None])[0],
    ]
    quality_candidates = [
        families.get("claude-sonnet", [None])[0],
        gem_quality,
        gpt_top,
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
        credits: Optional[list] = None,
    ):
        """Initialize ModelRegistry with optional multi-provider support.

        Args:
            provider: Primary provider (used for all tiers by default)
            fallback: User-specified model overrides per tier (QAR_MODEL_*)
            providers: Optional dict of provider_name -> ModelProvider for multi-provider
            provider_overrides: Optional dict of tier -> provider_name for per-tier routing
                               (e.g. {"best": "anthropic", "fast": "gemini"})
            credits: Optional credit rows for the tier selector (default: ``credit_source()``)
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
        self._credits = credits
        self._selected: Dict[str, str] = {}
        self._selected_for: Optional[tuple] = None

    def selected(self) -> Dict[str, str]:
        """tier -> model id of the hub's public choice (``hubofhubs.tiers.choose``) among the configured providers. A tier with no public
        choice is absent: the live list or the pin decides it. Never raises: a failing selector leaves the live list in charge."""
        try:
            names = configured_providers(getattr(self._provider, "providers", None) or self._providers, self._provider)
            key = frozenset(names)
        except Exception:  # noqa: BLE001 -- resolve_tier never raises
            log.warning("tier selection skipped: could not read the providers or credits", exc_info=True)
            return {}
        if key != self._selected_for:
            try:
                self._selected = hub_picks(names) if names else {}
            except Exception:  # noqa: BLE001 -- a bad registry file must not stop a lane from resolving a model
                log.warning("tier selection skipped: the hub's selector failed", exc_info=True)
                self._selected = {}
            self._selected_for = key
        return dict(self._selected)

    def served_models(self) -> List[str]:
        """The explicit models the hub can serve among the providers this lane can call (``hubofhubs.tiers.served_models``, read from the hub's registry, never listed here),
        for a picker to offer after the tiers. Never raises: a failing read offers none."""
        try:
            names = configured_providers(getattr(self._provider, "providers", None) or self._providers, self._provider)
            return list(dict.fromkeys(m for name in sorted(names) for m in hub_tiers.served_models(name)))
        except Exception:  # noqa: BLE001 -- a picker must open even when the registry cannot be read
            log.warning("served models skipped: the hub's registry could not be read", exc_info=True)
            return []

    def explicit_model(self, name: Optional[str]) -> Optional[str]:
        """The served model ``name`` names (case does not matter), or None."""
        wanted = (name or "").strip().lower()
        return next((m for m in self.served_models() if m.lower() == wanted), None) if wanted else None

    def lower_tier(self, tier: str) -> Optional[str]:
        return next_lower_tier(tier)

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

        # Cache is invalidated if the combined model set changes.
        cache_key = tuple(sorted(set(all_models)))
        if self._cache["source_id"] != cache_key or self._cache["top"] is None:
            self._cache["source_id"] = cache_key
            self._cache["top"] = bucket_top(all_models) if all_models else dict(DEFAULT_FALLBACK_TOP)
        # The hub's public choice replaces the live-list bucketing for its tier; an explicit pin still wins.
        chosen = drop_excluded({**self._cache["top"], **self.selected()}, excluded_source())
        return {**chosen, **self._user_overrides}

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
        return bool(n) and (n in TIERS or n in LEGACY_TIER_ALIASES or n in LEGACY_TIER_ROLES)

    def resolve_role(self, call: str, requested: Optional[str] = None, task_kind: str = "") -> str:
        """The model for the model call ``call``: its role's tier from the hub's table (``call_roles.tier_for_call``), with an explicit tier
        from the person, the task or the lane applied under the guards (classification never above fast, a response never below balanced,
        best only where a person asked or for the daily structure call). A call with no role raises ``KeyError``. ``task_kind`` is the kind of a deep-work
        task (see ``tier_for_call``)."""
        return self.resolve_tier(tier_for_call(call, requested, task_kind=task_kind))

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
        elif t in LEGACY_TIER_ROLES:   # a domain word (science) reads as the tier of its role in the hub's table
            t = legacy_word_tier(t) or t
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



@implementation("model-tier-chooser", "model_registry")
class RegistryTiers:
    """The hub's tier selector. ``choose`` returns ``{hub tier: {arm, effort, reason, scored, stage}}`` for the providers given (public
    evaluations only); ``hub_picks`` is the same answer reduced to QAR's tiers, which ``ModelRegistry.selected`` applies to the live list;
    ``select`` ranks measured arms with QAR's bars."""
    REQUIRES = ()     # the hub's selector reads its registry files and nothing else

    def choose(self, folder: Any = None, available: Optional[Sequence[str]] = None,
               credits: Optional[Sequence[Mapping[str, str]]] = None, **bars: Any) -> Mapping[str, Any]:
        return hub_tiers.choose(folder, available, credits, **bars)

    def select(self, tiers: Mapping[str, Sequence[Mapping[str, Any]]], prices: Sequence[Mapping[str, str]],
               credits: Sequence[Mapping[str, str]] = (), defaults: Any = None, **bars: Any) -> Mapping[str, Any]:
        return hub_tiers.select(tiers, prices, credits, defaults, **{"min_cases": TIER_MIN_CASES, "min_pass_rate": TIER_MIN_PASS_RATE, **bars})

    def hub_picks(self, available: Sequence[str]) -> Dict[str, str]:
        return hub_picks(set(available))
