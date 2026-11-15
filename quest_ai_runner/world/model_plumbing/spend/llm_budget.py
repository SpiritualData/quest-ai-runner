"""QAR's settings for the hub's call guard (``hubofhubs.api.CallGuard``): the ``QAR_*`` variables read into one ``CallLimits`` and ONE process-wide guard.

The counting, the per-job and hourly budgets, the step-down by volume, the best-only-on-request rule, the never-models and the keyless rule are the hub's
(``MultiProvider(..., call_guard=guard)`` applies them to every routed call); this module only reads the environment and says which models would use an API key.

Env vars:
  QAR_MAX_LLM_CALLS_PER_JOB          calls one job may make (default 200; 0 or off disables).
  QAR_MAX_LLM_CALLS_PER_HOUR         calls the process may make in any rolling hour (default 600; 0 or off disables).
  QAR_BEST_REQUIRES_USER_REQUEST     1 (default): the best tier runs only for a person's explicit request (or the structure watch's role); 0 lifts the rule.
  QAR_KEYLESS                        1: the process never uses an API key; a call that would is refused (default 0).
  QAR_NEVER_MODELS                   comma list of words; a model id containing one is refused (default none).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Mapping, Optional, Set

from hubofhubs.api import CallGuard, CallLimits, provider_name_for_model

log = logging.getLogger("quest-ai-runner.llm-budget")

DEFAULT_MAX_PER_JOB = 200
DEFAULT_MAX_PER_HOUR = 600
OFF_WORDS = ("0", "off", "none", "false", "disabled")

_API_PROVIDERS: Set[str] = set()        # names of the API-key vendors the process's routers hold (the claude_cli primary is not one)
_GUARD: Optional[CallGuard] = None
_LOCK = threading.Lock()


def _count(env: Mapping[str, str], name: str, default: int) -> Optional[int]:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in OFF_WORDS:
        return None
    try:
        value = int(raw)
    except ValueError:
        log.warning("ignoring %s=%r: not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else None


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in OFF_WORDS and raw != "no"


def limits_from_environment(env: Optional[Mapping[str, str]] = None) -> CallLimits:
    """The ``CallLimits`` the ``QAR_*`` variables describe."""
    env = os.environ if env is None else env
    never = tuple(part.strip() for part in (env.get("QAR_NEVER_MODELS") or "").split(",") if part.strip())
    return CallLimits(max_per_job=_count(env, "QAR_MAX_LLM_CALLS_PER_JOB", DEFAULT_MAX_PER_JOB),
                      max_per_hour=_count(env, "QAR_MAX_LLM_CALLS_PER_HOUR", DEFAULT_MAX_PER_HOUR),
                      best_requires_person=_flag(env, "QAR_BEST_REQUIRES_USER_REQUEST", True),
                      never_models=never,
                      keyless=_flag(env, "QAR_KEYLESS", False))


def register_api_providers(names) -> None:
    """Record the API-key vendors a router holds, so ``uses_key`` knows a model of theirs would use a key."""
    _API_PROVIDERS.update(names)


def uses_key(model: str) -> bool:
    """True when ``model`` is served by an API vendor (a key); false for the claude_cli path."""
    from quest_ai_runner.world.model_plumbing.vendor_model_caller.providers import MODEL_PREFIXES
    name = provider_name_for_model(model, _API_PROVIDERS, MODEL_PREFIXES)
    return name is not None


def guard() -> CallGuard:
    """The one process-wide guard, built from the environment on first use."""
    global _GUARD
    with _LOCK:
        if _GUARD is None:
            _GUARD = CallGuard(limits_from_environment(), uses_key=uses_key)
        return _GUARD


def mark_user_requested_best() -> None:
    """Record that a person explicitly asked for the best tier in the current context (the hub's guard reads it at the next call; there is no scope to wrap
    at the two places the person's tier is recorded, so this sets the guard's flag for the context)."""
    guard().person_best.set(True)
