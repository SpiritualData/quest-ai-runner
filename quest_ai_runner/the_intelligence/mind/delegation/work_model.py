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
  * no requested model: an explicitly configured default is pinned; with nothing configured the run is
    left unpinned, so the existing QAR behaviour is unchanged for environments that never set one;
  * only Anthropic models run here (the Claude Code deep worker), so other providers are skipped;
  * if the environment allows no Anthropic model at all, the run is refused (``WorkModelUnavailable``)
    rather than silently run on a model the org excluded.

A QAR-call tier word (``fast``, ``balanced``, ``quality``, ``best``, ``science``) in ``model`` is not a
work model. ``is_tier_word`` lets the executor leave those alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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

    # None = leave the run unpinned (nothing requested and no default configured).
    model: Optional[str]
    # "requested" = the task's own pin was allowed; "default" = the configured default was applied;
    # "unpinned" = nothing requested and nothing configured.
    source: str
    note: Optional[str] = None


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
        if model in RUNNABLE_MODELS and model not in names:
            names.append(model)
    return names


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
    backend's ``source`` is not "default"). Only then is the default pinned for a run that asked for no
    model; an unconfigured environment keeps today's unpinned behaviour.

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
            return WorkModelChoice(model=default, source="default")
        return WorkModelChoice(model=None, source="unpinned")
    if want in options:
        return WorkModelChoice(model=want, source="requested")
    return WorkModelChoice(
        model=default,
        source="default",
        note=(
            f"requested work model '{want}' is not allowed on this environment, "
            f"so the run uses the default '{default}'"
        ),
    )
