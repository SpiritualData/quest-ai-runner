"""Difficulty-based STARTING model for a deep run (automatic model selection).

When nobody pinned a model (no per-task ``deep_run_model``, no chat model hint, no guidance
preference), the planner already reads the request to decide answer-now vs deep run. On that SAME
call it also rates the work (``deep_difficulty``: "simple" | "normal" | "hard"), so choosing the
starting model costs zero extra LLM calls. This module turns that rating into a starting rung on
the deep-worker model ladder:

  * simple -> the "simple" model (default ``haiku``): clearly trivial, mechanical work only, such
    as a lookup, reformatting, or a short status read.
  * normal -> the "normal" model (default ``sonnet``): the default for everything else.
  * hard   -> the "hard" model (default ``sonnet`` too). Starting hard work on the strongest model
    by default doubles the cost of every hard task that the normal model could have finished, so
    the strongest model stays where it pays: the escalation rung a not-met goal climbs to.

Escalation is unchanged: the goal loop still climbs the ladder on a not-met goal, so a haiku start
can climb to sonnet and then opus. The ladder is the operator's configured one
(``QAR_DEEP_MODELS``) when set, else ``DEFAULT_AUTO_DEEP_LADDER``. A starting model that is not on
the ladder resolves to the nearest STRONGER rung (never silently to a weaker one), then the nearest
weaker one as a last resort.

Everything here is pure and never raises: any surprise returns ``None`` and the caller keeps its
existing ladder, which is the documented fallback to the behaviour before this existed.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DIFFICULTIES: Tuple[str, ...] = ("simple", "normal", "hard")

# Claude Code aliases, weak -> strong, up to the balanced tier (sonnet). Aliases (not dated ids) so
# the keyless CLI path runs them. Opus is never a default; a deployment that wants it on the ladder
# sets QAR_DEEP_MODELS explicitly.
DEFAULT_AUTO_DEEP_LADDER: Tuple[str, ...] = ("haiku", "sonnet")

DEFAULT_DIFFICULTY_MODELS: Dict[str, str] = {
    "simple": "haiku",
    "normal": "sonnet",
    "hard": "sonnet",
}

# Common synonyms a model might emit instead of the schema's three words.
DIFFICULTY_ALIASES: Dict[str, str] = {
    "trivial": "simple", "easy": "simple", "low": "simple",
    "medium": "normal", "moderate": "normal", "standard": "normal",
    "complex": "hard", "difficult": "hard", "high": "hard", "high_stakes": "hard",
    "high-stakes": "hard",
}


def normalize_difficulty(value: Any) -> Optional[str]:
    """``"simple" | "normal" | "hard"`` or None for anything unrecognised (never raises)."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in DIFFICULTIES:
        return v
    return DIFFICULTY_ALIASES.get(v)


def default_rung_key(model: Optional[str]) -> str:
    """What a model string resolves to at invoke time, so ``sonnet``, ``claude-sonnet`` and a
    dated sonnet id count as one rung. Uses the deep worker's own translator."""
    try:
        from .goal_runner import cli_safe_model
        return (cli_safe_model(model) or model or "").strip().lower()
    except Exception:  # noqa: BLE001 (an untranslatable id is just its own rung)
        return (model or "").strip().lower()


def select_deep_start(difficulty: Optional[str],
                      ladder: Sequence[Optional[str]],
                      difficulty_models: Optional[Dict[str, str]] = None,
                      *,
                      reason: Optional[str] = None,
                      rung_key: Callable[[Optional[str]], str] = default_rung_key,
                      ) -> Optional[Tuple[List[str], Dict[str, Any]]]:
    """Pick the starting rung for ``difficulty`` on ``ladder``.

    Returns ``(ladder_from_start, selection)`` where ``ladder_from_start`` is ``ladder`` sliced so
    its first element is the starting model (the goal loop then escalates through the rest), and
    ``selection`` is a small dict for logging and recording: difficulty, start_model, ladder,
    reason. Returns None when the difficulty is unknown or the ladder is empty, meaning "keep the
    existing ladder unchanged"."""
    try:
        level = normalize_difficulty(difficulty)
        rungs = [m for m in (ladder or []) if m]
        if level is None or not rungs:
            return None
        models = dict(DEFAULT_DIFFICULTY_MODELS)
        models.update({k: v for k, v in (difficulty_models or {}).items() if v})
        wanted = models[level]
        keys = [rung_key(m) for m in rungs]
        wanted_key = rung_key(wanted)
        start_idx: Optional[int] = keys.index(wanted_key) if wanted_key in keys else None
        note = ""
        if start_idx is None:
            # Not on the ladder: the nearest STRONGER model any difficulty maps to, in ladder order,
            # else the strongest rung below it. Strength is ladder position, so the first ladder
            # rung whose key is the default strength order's next step is used.
            order = [rung_key(m) for m in DEFAULT_AUTO_DEEP_LADDER]
            if wanted_key in order:
                stronger = order[order.index(wanted_key) + 1:]
                weaker = list(reversed(order[:order.index(wanted_key)]))
                for k in stronger + weaker:
                    if k in keys:
                        start_idx = keys.index(k)
                        break
            if start_idx is None:
                start_idx = 0
            note = f" ({wanted} is not on the ladder, so the closest rung is used)"
        start_ladder = [str(m) for m in rungs[start_idx:]]
        why = (reason or "").strip()
        selection = {
            "difficulty": level,
            "start_model": start_ladder[0],
            "ladder": start_ladder,
            "reason": (why[:300] if why else f"planner rated the work {level}") + note,
        }
        return start_ladder, selection
    except Exception:  # noqa: BLE001 (selection must never break a deep run)
        return None


def parse_difficulty_models(simple: Optional[str], normal: Optional[str],
                            hard: Optional[str]) -> Dict[str, str]:
    """Build a difficulty -> model map from three optional values (unset keeps the default)."""
    models = dict(DEFAULT_DIFFICULTY_MODELS)
    for level, value in (("simple", simple), ("normal", normal), ("hard", hard)):
        v = (value or "").strip()
        if v:
            models[level] = v
    return models
