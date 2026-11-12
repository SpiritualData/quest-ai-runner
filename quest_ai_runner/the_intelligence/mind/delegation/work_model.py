"""Apply an environment's work-model configuration to a quest AI task run.

The "work model" is the literal model that executes a task's deep run on the external runner (the
``deep_run_model`` field). An org or team configures, on the environment record, which models the AI
may consider and which default it starts from. Quest's backend resolves the effective view (team
override, else org, else the Claude default) and returns it from
``GET /api/teams/{team_id}/environments/{env_id}/work-model`` under ``effective``. This module turns that
view plus the task's requested model into the one model the runner pins.

Rules (pure, never raises on bad input):
  * a requested model that is in the effective allowed list is used as asked;
  * a requested model that is excluded (not allowed, or in an excluded cost tier) falls back to the
    effective default, and the choice carries a note saying why, so the run log shows it;
  * no requested model: an explicitly configured default is STANDING GUIDANCE, not a pin (``standing``): it starts
    the run only when nothing above it (the person's request, a guidance card) names a start, and the run may
    still move up one tier on a recorded shortfall; with nothing configured nothing is applied;
  * only Anthropic models run here (the Claude Code deep worker), so other providers are skipped; a
    specific Claude version ("claude-opus-4-8") counts as runnable, through its family;
  * if the environment allows no Anthropic model at all, the run is refused (``WorkModelUnavailable``)
    rather than silently run on a model the org excluded.

A QAR-call tier word (``fast``, ``balanced``, ``quality``, ``best``, ``science``) in ``model`` is not a work
model: it is the person's requested START tier (``split_requested``), which beats the default and standing
guidance and may still escalate one rung. The environment's allowed list applies to both paths: a pin is checked
by ``apply_work_model`` and every rung of a tier run by ``rung_allowed`` (``deep_model_ask`` hands both to the run).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.deep_model_selection import pure_tier
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import LEGACY_TIER_ALIASES

ANTHROPIC = "anthropic"

# The literal Claude models the deep worker can run. The runner reports these as its available work
# models on every heartbeat, and they are the default allowed list when nothing is configured.
RUNNABLE_MODELS = ("haiku", "sonnet", "opus", "fable")

# The Claude default when nothing is configured anywhere: Sonnet, every Claude model allowed.
DEFAULT_WORK_MODEL = "sonnet"

# QAR-call tier words a task's ``model`` field may carry. These are not work models.
TIER_WORDS = frozenset({"fast", "balanced", "quality", "best", "science"})


class WorkModelUnavailable(RuntimeError):
    """The environment allows no Anthropic work model this runner can run."""


@dataclass(frozen=True)
class WorkModelChoice:
    """The model the run pins, and why it differs from the request (if it does)."""

    # None = no pin (nothing requested, or only a configured default, which is standing guidance).
    model: Optional[str]
    # "requested" = the task's own pin was allowed; "default" = a refused request fell back to the configured
    # default (pinned); "standing" = nothing requested and the configured default is returned as ``standing``;
    # "unpinned" = nothing requested and nothing configured.
    source: str
    note: Optional[str] = None
    # The configured default when nothing was requested: standing guidance, below the person's own choices.
    standing: Optional[str] = None


def is_tier_word(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in TIER_WORDS


def default_effective() -> Dict[str, Any]:
    """The effective view when nothing is configured: every Claude model, Sonnet the default."""
    return {
        "default_model": {"provider": ANTHROPIC, "model": DEFAULT_WORK_MODEL, "tier": "standard"},
        "allowed_models": [
            {"provider": ANTHROPIC, "model": m, "tier": t}
            for m, t in (("haiku", "economy"), ("sonnet", "standard"), ("opus", "premium"), ("fable", "premium"))
        ],
        "excluded_tiers": [],
        "reported_models": None,
    }


def anthropic_options(effective: Optional[Dict[str, Any]]) -> List[str]:
    """Literal Anthropic model names the AI may run, in the configured order. Non-Anthropic entries and
    anything this runner cannot run are skipped."""
    names: List[str] = []
    for entry in (effective or {}).get("allowed_models") or []:
        if not isinstance(entry, dict) or entry.get("provider") != ANTHROPIC:
            continue
        model = str(entry.get("model") or "").strip().lower()
        if (model in RUNNABLE_MODELS or family_of(model)) and model not in names:
            names.append(model)
    return names


def family_of(model: Optional[str]) -> Optional[str]:
    """The runnable Claude family a specific version belongs to ("claude-opus-4-8" -> "opus"), else
    None. Environments may allow a specific version, and the deep worker runs it as named."""
    low = (model or "").strip().lower()
    return next((f for f in RUNNABLE_MODELS if low.startswith(f"claude-{f}")), None)


def effective_default(effective: Optional[Dict[str, Any]], options: List[str]) -> Optional[str]:
    """The default work model: the configured default when it is an available Anthropic option, else the
    first available option. None when there are no options."""
    configured = ((effective or {}).get("default_model") or {})
    if configured.get("provider") == ANTHROPIC:
        name = str(configured.get("model") or "").strip().lower()
        if name in options:
            return name
    return options[0] if options else None


def apply_work_model(requested: Optional[str], effective: Optional[Dict[str, Any]],
                     *, configured: bool) -> WorkModelChoice:
    """Choose the work model to pin for one run. See the module docstring for the rules.

    ``configured`` is True when the effective view came from an org or team's own configuration (the
    backend's ``source`` is not "default"). Only then is the default returned (as ``standing`` guidance, never
    as a pin) for a run that asked for no model; an unconfigured environment applies nothing.

    Raises ``WorkModelUnavailable`` when the environment allows no Anthropic model this runner can run.
    """
    options = anthropic_options(effective)
    default = effective_default(effective, options)
    if default is None:
        raise WorkModelUnavailable(
            "This environment's work-model configuration allows no Claude model this runner can run. "
            "Ask an org or team admin to allow one in the environment's work-model settings."
        )
    want = (requested or "").strip().lower()
    if not want:
        if configured:
            return WorkModelChoice(model=None, source="standing", standing=default)
        return WorkModelChoice(model=None, source="unpinned")
    if want in options:
        return WorkModelChoice(model=want, source="requested")
    # A specific VERSION of an allowed family ("claude-opus-4-8" when opus is allowed) is used as
    # asked: environments hold more than one version, and the family word cannot name which.
    if family_of(want) in options:
        return WorkModelChoice(model=want, source="requested")
    return WorkModelChoice(
        model=default,
        source="default",
        note=(
            f"requested work model '{want}' is not allowed on this environment, "
            f"so the run uses the default '{default}'"
        ),
    )


# --- the person's request: a pin, or a start tier ----------------------------------------------------------

def split_requested(deep_run_model: Optional[str], model: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(pin, tier)`` of what a task asks for. A specific MODEL (a family word or a versioned id, in ``deep_run_model`` or as a literal in ``model``) is
    a pin: it starts that model alone. A TIER (fast, balanced, quality, best; in ``model``, the legacy family words read as their tier there) is the
    person's requested start tier: it beats the default and standing guidance and may still escalate one rung. When both are present the pin wins, being
    the more specific. ``science`` and any other word that is neither is no request."""
    deep = (deep_run_model or "").strip().lower()
    tier_word = (model or "").strip().lower()
    if deep:
        if pure_tier(deep):
            return None, deep
        return deep, None
    if not tier_word:
        return None, None
    if pure_tier(tier_word):
        return None, tier_word
    if tier_word in LEGACY_TIER_ALIASES:
        return None, LEGACY_TIER_ALIASES[tier_word]
    if is_tier_word(tier_word):
        return None, None
    return tier_word, None


def model_family(model: Optional[str]) -> Optional[str]:
    """The runnable family of a model name (a family word or a versioned id), else None."""
    low = (model or "").strip().lower()
    if low in RUNNABLE_MODELS or family_of(low):
        return low if low in RUNNABLE_MODELS else family_of(low)
    try:   # a phrase that names a model ("use opus") is judged by what the deep worker would run
        from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import cli_safe_model
        worker = (cli_safe_model(low) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return None
    return worker if worker in RUNNABLE_MODELS else family_of(worker)


def rung_allowed(model: Optional[str], options: Optional[List[str]]) -> bool:
    """Whether the environment's allowed list lets a ladder rung run. A rung is judged by its family (the list may hold a family word or a specific
    version); a rung that names no runnable family cannot be judged here and passes. ``options`` None means nothing is restricted."""
    if options is None:
        return True
    family = model_family(model)
    if family is None:
        return True
    return any(model_family(option) == family for option in options)


@dataclass(frozen=True)
class DeepModelAsk:
    """What one task asks of its deep run's model, after the environment's work-model configuration is applied."""

    pin: Optional[str] = None            # a specific model: starts alone, never escalates
    tier: Optional[str] = None           # the person's requested start tier: not a pin, may escalate one rung
    standing: Optional[str] = None       # the environment's configured default: standing guidance
    allowed: Optional[Tuple[str, ...]] = None   # the environment's allowed models, applied to every rung
    note: Optional[str] = None

    @property
    def hint(self) -> Optional[str]:
        """The one string the orchestrator takes as ``model_hint``: the pin, else the start tier."""
        return self.pin or self.tier

    def meta(self) -> Dict[str, Any]:
        """The keys the deep run reads from the run's context meta."""
        out: Dict[str, Any] = {}
        if self.standing:
            out["deep_standing_model"] = self.standing
        if self.allowed is not None:
            out["deep_allowed_models"] = list(self.allowed)
        return out


def deep_model_ask(deep_run_model: Optional[str], model: Optional[str], effective: Optional[Dict[str, Any]],
                   *, configured: bool) -> DeepModelAsk:
    """Split a task's request into a pin or a start tier, apply the environment's configuration to both paths, and carry its default as standing
    guidance. Raises ``WorkModelUnavailable`` when the environment allows no Claude model this runner can run."""
    pin, tier = split_requested(deep_run_model, model)
    view = effective or default_effective()
    choice = apply_work_model(pin, view, configured=configured)
    options = tuple(anthropic_options(view))
    return DeepModelAsk(pin=choice.model if pin else None, tier=None if pin else tier,
                        standing=choice.standing, allowed=options, note=choice.note)
