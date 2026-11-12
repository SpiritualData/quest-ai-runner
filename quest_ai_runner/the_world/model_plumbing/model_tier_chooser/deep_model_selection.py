"""The STARTING model of a deep run: the hub's tier rule, resolved to a model through the registry's tiers.

The hub decides (``hubofhubs.api.role_tiers``): a deep run starts on the cheapest tier measured good enough for deep work, unless the person named a tier in
the current request, or their standing guidance on model tiers names one. An escalation moves one rung up for one task only and needs a specific reason. This
module is Quest AI Runner's binding of that rule, and it writes no model name: every model comes from a tier through ``ModelRegistry``.

Order of what decides the start of a run nobody pinned (a pin on the task or a model asked for in the message is resolved before this, in
``orchestrator_deep_models.py``, and starts that model alone):

  1. the person's standing guidance (a guidance card's model preference that names a tier), supplied to the hub as its ``StandingTierGuidance`` function;
  2. the operator's configuration (``QAR_DEEP_MODEL_SIMPLE`` / ``_NORMAL`` / ``_HARD``), which counts as standing guidance and is read only when no card
     names a tier;
  3. the hub's default for the role of the task kind (``main_session`` when the kind is not known), the cheapest sufficient tier. The planner may ask for
     the next tier up before the run, but only with a specific reason; without one the default stands.

The planner's difficulty rating is information and the input of that reason. It never raises the start by itself. The ladder an escalation climbs is the
operator's ``QAR_DEEP_MODELS`` when set, else the registry's tiers in order. When the hub cannot be read the start is the registry's cheapest tier, and the
selection says so. ``escalate_choice`` is the one place an escalation is decided: one rung, a specific reason, this task only; nothing here is written to a
task's ``deep_run_model`` (that would turn an automatic choice into a pin for later runs).

Everything here is pure apart from the one-time log lines and never raises: any surprise returns ``None`` and the caller keeps the ladder it already has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import LEGACY_TIER_ALIASES

log = logging.getLogger("quest-ai-runner")

DIFFICULTIES: Tuple[str, ...] = ("simple", "normal", "hard")

# The tier words, weak to strong. Names of tiers, never of models; best is a person's own tier or the structure watch's, never an escalation rung.
TIER_ORDER: Tuple[str, ...] = ("fast", "balanced", "quality", "best")
LADDER_TIERS: Tuple[str, ...] = ("fast", "balanced", "quality")
MAIN_ROLE = "main_session"
OPERATOR_SOURCE = "standing_guidance"
PIN_SOURCE = "task_pin"
FALLBACK_SOURCE = "fallback"

# Common synonyms a model might emit instead of the schema's three words.
DIFFICULTY_ALIASES: Dict[str, str] = {
    "trivial": "simple", "easy": "simple", "low": "simple",
    "medium": "normal", "moderate": "normal", "standard": "normal",
    "complex": "hard", "difficult": "hard", "high": "hard", "high_stakes": "hard",
    "high-stakes": "hard",
}

warned_single_rung: set = set()


def normalize_difficulty(value: Any) -> Optional[str]:
    """``"simple" | "normal" | "hard"`` or None for anything unrecognised (never raises)."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in DIFFICULTIES:
        return v
    return DIFFICULTY_ALIASES.get(v)


def default_rung_key(model: Optional[str]) -> str:
    """What a model string resolves to at invoke time, so one family's alias, its bucket name and a dated id count as one rung. Uses the deep
    worker's own translator."""
    try:
        from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import cli_safe_model
        return (cli_safe_model(model) or model or "").strip().lower()
    except Exception:  # noqa: BLE001 (an untranslatable id is just its own rung)
        return (model or "").strip().lower()


def tier_name_of(word: Any) -> Optional[str]:
    """The tier a word names (a tier, one of the registry's legacy family words, or the hub's older names), else None."""
    w = str(word or "").strip().lower()
    w = LEGACY_TIER_ALIASES.get(w, w)
    if w in TIER_ORDER:
        return w
    try:
        from hubofhubs.api import role_tiers
        n = role_tiers.normalise(w)
        return n if n in TIER_ORDER else None
    except Exception:  # noqa: BLE001
        return None


def hub_roles():
    from hubofhubs.api import role_tiers
    return role_tiers


def hub_choice(task_kind: str = "", standing_tier: Optional[str] = None):
    """The hub's ``TierChoice`` for a deep run of ``task_kind`` (role by kind, else ``main_session``): the standing tier when the person's guidance names
    one, else the cheapest sufficient tier. Raises when the hub cannot be read; the caller falls back."""
    role_tiers = hub_roles()
    standing = (lambda role: standing_tier) if standing_tier else None
    return role_tiers.task_choice((task_kind or "").strip().lower(), "", standing=standing)


def tier_models(registry: Any) -> Dict[str, str]:
    """Tier to the model the registry resolves it to (a tier that does not resolve is absent)."""
    out: Dict[str, str] = {}
    for tier in TIER_ORDER:
        try:
            model = registry.resolve_tier(tier)
        except Exception:  # noqa: BLE001
            continue
        if model:
            out[tier] = str(model)
    return out


def tier_ladder(registry: Any, rung_key: Callable[[Optional[str]], str] = default_rung_key) -> List[str]:
    """The escalation ladder when ``QAR_DEEP_MODELS`` is unset: the registry's tiers in order (fast, balanced, quality), each the model the deep worker
    can run, with a model two tiers share kept once."""
    from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import _is_claude_model
    try:
        names = [t for t in hub_roles().ladder_for(hub_roles().read_rules()) if t in TIER_ORDER] or list(LADDER_TIERS)
    except Exception:  # noqa: BLE001 (the hub's ladder is the same three tiers)
        names = list(LADDER_TIERS)
    models = tier_models(registry)
    ladder: List[str] = []
    seen = set()
    for tier in names:
        model = models.get(tier)
        if not model or not _is_claude_model(model) or rung_key(model) in seen:
            continue
        seen.add(rung_key(model))
        ladder.append(model)
    return ladder


def tier_of_model(model: Optional[str], models: Mapping[str, str], rung_key: Callable[[Optional[str]], str] = default_rung_key) -> str:
    """The tier whose resolved model is ``model``, else an empty string."""
    key = rung_key(model)
    for tier in TIER_ORDER:
        if tier in models and rung_key(models[tier]) == key:
            return tier
    return ""


def start_index(keys: Sequence[str], models: Mapping[str, str], tier: str, wanted: str, rung_key: Callable[[Optional[str]], str]) -> Tuple[int, bool]:
    """Index of the ladder rung to start on and whether it is the exact one. A start that is not on the ladder is the nearest STRONGER tier's rung,
    then the nearest weaker one, then the first rung."""
    if wanted in keys:
        return keys.index(wanted), True
    if tier in TIER_ORDER:
        at = TIER_ORDER.index(tier)
        for other in list(TIER_ORDER[at + 1:]) + list(reversed(TIER_ORDER[:at])):
            if other in models and rung_key(models[other]) in keys:
                return keys.index(rung_key(models[other])), False
    return 0, False


def specific_reason(text: Any, signal: str = "") -> str:
    """The reason in one line, or an empty string when it is blank, only repeats the signal, or only names a difficulty rating (it must say what the
    tier did not or would not do on this task)."""
    clean = " ".join(str(text or "").split())
    low = clean.lower()
    generic = set(DIFFICULTIES) | set(DIFFICULTY_ALIASES) | {signal.lower(), signal.lower().replace("_", " ")}
    if not clean or low.rstrip(".") in generic:
        return ""
    return clean[:300]


@dataclass
class StartPlan:
    """The start of one deep run: the ladder from the starting rung and the selection record (what is logged and sent on the status event)."""
    ladder: List[str]
    selection: Dict[str, Any]


def record_choice(selection: Dict[str, Any], choice: Any) -> None:
    selection.update(choice.record())
    selection["tier_role"] = choice.role


def plan_start(registry: Any, *, configured_ladder: Optional[Sequence[str]] = None, task_kind: str = "", standing_tier: Optional[str] = None,
               operator_model: Optional[str] = None, operator_var: str = "", difficulty: Optional[str] = None,
               difficulty_reason: Optional[str] = None,
               rung_key: Callable[[Optional[str]], str] = default_rung_key) -> Optional[StartPlan]:
    """Choose the start of a run nobody pinned. Returns None when there is no ladder to start on (the caller keeps the ladder it already resolved)."""
    try:
        models = tier_models(registry)
        configured = [m for m in (configured_ladder or []) if m]
        ladder = configured or tier_ladder(registry, rung_key)
        if not ladder:
            return None
        level = normalize_difficulty(difficulty)
        role_tiers = None
        try:
            role_tiers = hub_roles()
            choice = hub_choice(task_kind, standing_tier)
        except Exception as exc:  # noqa: BLE001 (the hub is a dependency, but a missing or broken rule must not stop a run)
            cheapest = next((t for t in TIER_ORDER if t in models), "fast")
            log.warning("Deep-worker tier: the hub's tier rule could not be applied (%s: %s); starting on the registry's cheapest tier (%s)",
                         type(exc).__name__, exc, cheapest)
            choice = FallbackChoice(MAIN_ROLE, cheapest, FALLBACK_SOURCE, f"the hub's tier rule was unavailable ({type(exc).__name__}); the registry's cheapest tier")
            role_tiers = None
        wanted_model = models.get(choice.tier, "")
        if operator_model and choice.source != "standing_guidance":
            # Operator configuration is standing guidance, read only when the person's own guidance names no tier.
            wanted_model = operator_model
            choice = make_choice(role_tiers, choice.role, tier_of_model(operator_model, models, rung_key), OPERATOR_SOURCE,
                                  f"the operator's configuration {operator_var or 'for the start model'} names this model")
        elif choice.source == "default" and role_tiers is not None:
            asked = planner_ask(role_tiers, choice, level, difficulty_reason)
            if asked is not None:
                choice = asked
                wanted_model = models.get(choice.tier, wanted_model)
        keys = [rung_key(m) for m in ladder]
        idx, exact = start_index(keys, models, choice.tier, rung_key(wanted_model), rung_key)
        start_ladder = [str(m) for m in ladder[idx:]]
        reason = choice.reason + ("" if exact else f" ({wanted_model or 'that model'} is not on the ladder, so the closest rung is used)")
        selection: Dict[str, Any] = {"difficulty": level, "start_model": start_ladder[0], "ladder": start_ladder, "reason": reason[:300]}
        record_choice(selection, choice)
        if len(ladder) == 1 and tuple(ladder) not in warned_single_rung:
            warned_single_rung.add(tuple(ladder))
            log.warning("Deep-worker ladder has one rung (%s): runs start there and cannot escalate. Set QAR_DEEP_MODELS to more than one rung, or "
                        "leave it unset so the registry's tiers are the ladder.", ladder[0])
        return StartPlan(start_ladder, selection)
    except Exception:  # noqa: BLE001 (selection must never break a deep run)
        log.warning("Deep-worker tier: the start could not be chosen; the resolved ladder is used", exc_info=True)
        return None


@dataclass
class FallbackChoice:
    """Stands in for the hub's ``TierChoice`` when the hub cannot be read."""
    role: str
    tier: str
    source: str
    reason: str = ""
    from_tier: str = ""

    def record(self) -> Dict[str, str]:
        out = {"tier": self.tier, "tier_source": self.source, "tier_reason": self.reason}
        if self.from_tier:
            out["tier_from"] = self.from_tier
        return out


def make_choice(role_tiers: Any, role: str, tier: str, source: str, reason: str, from_tier: str = "") -> Any:
    cls = getattr(role_tiers, "TierChoice", None) if role_tiers is not None else None
    return (cls or FallbackChoice)(role, tier, source, reason, from_tier)


def planner_ask(role_tiers: Any, default: Any, level: Optional[str], reason: Any) -> Optional[Any]:
    """The choice one rung above ``default`` when the planner rated the work hard AND gave a specific reason for it; None otherwise (no reason, no
    request: the default stands)."""
    if level != "hard":
        return None
    text = specific_reason(reason)
    if not text:
        log.info("Deep-worker tier: the planner rated the work hard without a specific reason, so the default tier (%s) stands", default.tier)
        return None
    try:
        rungs = role_tiers.ladder_for(role_tiers.read_rules())
        at = rungs.index(default.tier)
        if at + 1 >= len(rungs):
            return None
        return make_choice(role_tiers, default.role, rungs[at + 1], "main_session_ask", text, from_tier=default.tier)
    except Exception:  # noqa: BLE001
        return None


def choice_of(selection: Optional[Mapping[str, Any]], run_model: Optional[str] = None, models: Optional[Mapping[str, str]] = None) -> Any:
    """The choice a selection record describes (for escalating it), or one built from the model a run is on when nothing was chosen automatically."""
    role_tiers = None
    try:
        role_tiers = hub_roles()
    except Exception:  # noqa: BLE001
        pass
    if selection and selection.get("tier_source"):
        return make_choice(role_tiers, str(selection.get("tier_role") or MAIN_ROLE), str(selection.get("tier") or ""),
                            str(selection["tier_source"]), str(selection.get("tier_reason") or ""), str(selection.get("tier_from") or ""))
    return make_choice(role_tiers, MAIN_ROLE, tier_of_model(run_model, models or {}), OPERATOR_SOURCE, "the operator's configured ladder")


def escalate_choice(choice: Any, signal: str, reason: Any) -> Optional[Any]:
    """The choice for ONE task whose tier fell short: one rung up, with the recorded reason; None when it must not move (no specific reason, the top
    rung, a role that is not a deep-run role). Goes through the hub's ``escalate``; a choice whose tier is not a hub rung (a model the operator
    configured that no tier resolves to) is moved by the same rules locally. Nothing is stored: the next task is chosen again from scratch."""
    text = specific_reason(reason, signal)
    if not text:
        log.info("Deep-worker tier: no escalation on %s, the finding named nothing specific the tier did not do", signal)
        return None
    try:
        role_tiers = hub_roles()
        try:
            return role_tiers.escalate(choice, signal, text)
        except ValueError as exc:
            if choice.tier:
                log.info("Deep-worker tier: the hub refused the escalation (%s)", exc)
                return None
        return make_choice(role_tiers, choice.role, "", "escalation", f"{signal}: {text}", from_tier=choice.tier)
    except Exception:  # noqa: BLE001 (the hub is unreadable: the ladder still steps, with the same recorded reason)
        return FallbackChoice(choice.role, "", "escalation", f"{signal}: {text}", from_tier=choice.tier)


sub_agent_model_value: Optional[str] = None


def install_sub_agent_model(model: Optional[str]) -> None:
    """Use ``model`` as the default model of the sub-agents a deep worker starts (None clears it)."""
    global sub_agent_model_value
    sub_agent_model_value = model or None


def sub_agent_default_model(registry: Any) -> Optional[str]:
    """The model of the hub's ``sub_agent`` choice (its own cheapest sufficient tier), resolved through the registry. None when it cannot be chosen."""
    try:
        from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import cli_safe_model
        choice = hub_roles().sub_agent_choice()
        return cli_safe_model(registry.resolve_tier(choice.tier)) or None
    except Exception:  # noqa: BLE001
        log.warning("Deep-worker sub-agents: the hub's sub-agent tier could not be applied; the worker keeps its own default", exc_info=True)
        return None


SUB_AGENT_ENV = "CLAUDE_CODE_SUBAGENT_MODEL"


def sub_agent_environment(env: Mapping[str, str]) -> Dict[str, str]:
    """``env`` with the sub-agent model set to the hub's choice, unless the operator already set it."""
    out = dict(env)
    if sub_agent_model_value and not (out.get(SUB_AGENT_ENV) or "").strip():
        out[SUB_AGENT_ENV] = sub_agent_model_value
    return out


def start_status_text(selection: Dict[str, Any]) -> str:
    """The status line for the start of a deep run: which model it starts on and, when the ladder has a stronger rung,
    that one is ready. One short plain sentence (the task's difficulty is in ``selection`` for whoever records it): it
    has to fit the terminal's 80-column activity bar beside the clock and the spinner, so it stays near 50 characters."""
    text = f"Starting on {selection['start_model']}"
    if len(selection.get("ladder") or []) > 1:
        text += ", with a stronger model ready"
    return text + "."
