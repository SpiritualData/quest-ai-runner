"""Call-count budget for model calls: a runaway loop of calls is stopped by count, not only by the daily token limit.

Three rules, all applied in ``BudgetedMultiProvider.routed`` (the one place every plan, answer and web-search call passes):

  * A per-job budget: a job is a named scope (``with llm_job("context-index"):``). Past ``QAR_MAX_LLM_CALLS_PER_JOB`` calls in one job
    ``LlmBudgetExceeded`` is raised, and logged once per job with the job name and the counts. Never retried.
  * An hourly budget over every call in the process, in or out of a job: ``QAR_MAX_LLM_CALLS_PER_HOUR``.
  * Step-down by volume: inside a job, after ``STEP_DOWN_AFTER`` calls a request above the balanced tier (quality, best, or an Opus, Fable or
    Mythos model) runs on the balanced tier instead; fast stays fast. Logged once per job.

Also here: the rule that the ``best`` tier is used only when a person asked for it (``QAR_BEST_REQUIRES_USER_REQUEST``, default 1). The code
that records a person's own tier request calls ``mark_user_requested_best()``; any other request for ``best`` resolves to ``quality``.

Env vars:
  QAR_MAX_LLM_CALLS_PER_JOB          calls one job may make (default 200; 0 or off disables).
  QAR_MAX_LLM_CALLS_PER_HOUR         calls the process may make in any rolling hour (default 600; 0 or off disables).
  QAR_BEST_REQUIRES_USER_REQUEST     1 (default): the best tier runs only for a person's explicit request; 0 lifts the rule.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
import time
from collections import deque
from typing import Callable, Dict, Iterator, Mapping, Optional

log = logging.getLogger("quest-ai-runner.llm-budget")

DEFAULT_MAX_PER_JOB = 200
DEFAULT_MAX_PER_HOUR = 600
STEP_DOWN_AFTER = 20          # calls in one job before a request above balanced runs on balanced
HOUR = 3600.0
ABOVE_SONNET_FAMILIES = ("opus", "fable", "mythos")


class LlmBudgetExceeded(RuntimeError):
    """The call budget of a job or of the hour is used up. Raised before any call is made; a caller must stop, not retry."""


def _limit(name: str, default: int, env: Optional[Mapping[str, str]] = None) -> Optional[int]:
    raw = ((env if env is not None else os.environ).get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "off", "none", "false", "disabled"):
        return None
    try:
        value = int(raw)
    except ValueError:
        log.warning("ignoring %s=%r: not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else None


def max_calls_per_job(env: Optional[Mapping[str, str]] = None) -> Optional[int]:
    return _limit("QAR_MAX_LLM_CALLS_PER_JOB", DEFAULT_MAX_PER_JOB, env)


def max_calls_per_hour(env: Optional[Mapping[str, str]] = None) -> Optional[int]:
    return _limit("QAR_MAX_LLM_CALLS_PER_HOUR", DEFAULT_MAX_PER_HOUR, env)


def best_requires_user_request(env: Optional[Mapping[str, str]] = None) -> bool:
    raw = ((env if env is not None else os.environ).get("QAR_BEST_REQUIRES_USER_REQUEST") or "1").strip().lower()
    return raw not in ("0", "off", "false", "no")


class _Job:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.warned = False
        self.stepped = False
        self.lock = threading.Lock()


_CURRENT: "contextvars.ContextVar[Optional[_Job]]" = contextvars.ContextVar("qar_llm_job", default=None)
_ACTIVE: list = []                  # jobs entered and not yet left: worker threads do not inherit the context, so they fall back to the newest
_ACTIVE_LOCK = threading.Lock()
_HOUR_CALLS: deque = deque()
_HOUR_LOCK = threading.Lock()
_HOUR_WARNED = [0.0]
_USER_BEST: "contextvars.ContextVar[bool]" = contextvars.ContextVar("qar_user_requested_best", default=False)


@contextlib.contextmanager
def llm_job(name: str) -> Iterator[_Job]:
    """A named scope whose model calls are counted against ``QAR_MAX_LLM_CALLS_PER_JOB`` (see the module docstring). Nested jobs count separately."""
    job = _Job(name)
    token = _CURRENT.set(job)
    with _ACTIVE_LOCK:
        _ACTIVE.append(job)
    try:
        yield job
    finally:
        with _ACTIVE_LOCK:
            if job in _ACTIVE:
                _ACTIVE.remove(job)
        _CURRENT.reset(token)


def current_job() -> Optional[_Job]:
    job = _CURRENT.get()
    if job is not None:
        return job
    with _ACTIVE_LOCK:
        return _ACTIVE[-1] if _ACTIVE else None


def mark_user_requested_best() -> None:
    """Record that a person explicitly asked for the best tier in the current context (called where the request's tier is recorded)."""
    _USER_BEST.set(True)


def user_requested_best() -> bool:
    return _USER_BEST.get()


def reset_for_tests() -> None:
    with _HOUR_LOCK:
        _HOUR_CALLS.clear()
        _HOUR_WARNED[0] = 0.0
    with _ACTIVE_LOCK:
        _ACTIVE.clear()
    _USER_BEST.set(False)


def count_call(now: Optional[float] = None, env: Optional[Mapping[str, str]] = None) -> Optional[_Job]:
    """Count one model call against the hour and the current job. Raises ``LlmBudgetExceeded`` past a limit. Returns the job (or None)."""
    stamp = time.monotonic() if now is None else now
    hourly = max_calls_per_hour(env)
    with _HOUR_LOCK:
        while _HOUR_CALLS and stamp - _HOUR_CALLS[0] > HOUR:
            _HOUR_CALLS.popleft()
        if hourly is not None and len(_HOUR_CALLS) >= hourly:
            if stamp - _HOUR_WARNED[0] > HOUR or not _HOUR_WARNED[0]:
                _HOUR_WARNED[0] = stamp or 1e-9
                log.error("LLM call budget: %d calls in the last hour reached QAR_MAX_LLM_CALLS_PER_HOUR=%d; refusing further calls "
                          "(job: %s)", len(_HOUR_CALLS), hourly, (current_job().name if current_job() else "(none)"))
            raise LlmBudgetExceeded(f"hourly model call budget used up ({len(_HOUR_CALLS)}/{hourly}); raise QAR_MAX_LLM_CALLS_PER_HOUR to allow more")
        job = current_job()
        per_job = max_calls_per_job(env)
        if job is not None:
            with job.lock:
                if per_job is not None and job.calls >= per_job:
                    if not job.warned:
                        job.warned = True
                        log.error("LLM call budget: job %r made %d calls, QAR_MAX_LLM_CALLS_PER_JOB=%d reached; refusing further calls",
                                  job.name, job.calls, per_job)
                    raise LlmBudgetExceeded(f"job {job.name!r} used its model call budget ({job.calls}/{per_job}); "
                                            f"raise QAR_MAX_LLM_CALLS_PER_JOB to allow more")
                job.calls += 1
        _HOUR_CALLS.append(stamp)
    return job


def is_above_sonnet(model: str, tier: Optional[str]) -> bool:
    if tier in ("quality", "best"):
        return True
    low = (model or "").lower()
    return any(family in low for family in ABOVE_SONNET_FAMILIES)


def settle_model(model: str, tier_of: Callable[[str], Optional[str]], resolve_tier: Callable[[str], str],
                 job: Optional[_Job], env: Optional[Mapping[str, str]] = None) -> str:
    """The model a call runs on after the volume step-down and the best-needs-a-request rule."""
    tier = tier_of(model)
    if tier == "best" and best_requires_user_request(env) and not user_requested_best():
        lowered = resolve_tier("quality")
        log.info("best tier asked for with no person's request; using the quality tier (%s)", lowered)
        model, tier = lowered, "quality"
    if job is not None and job.calls > STEP_DOWN_AFTER and is_above_sonnet(model, tier):
        lowered = resolve_tier("balanced")
        if lowered and lowered != model:
            if not job.stepped:
                job.stepped = True
                log.warning("LLM call budget: job %r passed %d calls; requests above the balanced tier now run on %s", job.name, STEP_DOWN_AFTER, lowered)
            return lowered
    return model
