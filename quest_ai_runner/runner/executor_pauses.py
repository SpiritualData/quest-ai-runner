"""Pausing a task behind a usage limit or a provider outage.

A mixin of ``TaskExecutor`` (see ``executor.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.orchestrator import OrchestratorResult
from ..core import provider_outage, usage_limit
from .executor_support import ExecutionOutcome, utc_stamp
from .executor_contracts import log


class PausesMixin:
    """Pausing a task behind a usage limit or a provider outage."""

    @staticmethod
    def _usage_limit_behind(result: OrchestratorResult,
                            run_started: float) -> Optional["usage_limit.UsageLimit"]:
        """The usage limit that stopped this run, or None when the run's outcome stands.

        A run that SUCCEEDED is never paused, even if some concurrent run recorded a limit while it
        worked: its result is real. Only an unfinished outcome is attributed to the limit, and only
        on evidence: a deep result the runner itself flagged ``usage_limited``, or a limit recorded
        during this very run (``seen_since``) behind a failed deep result or an empty answer.
        """
        kind = getattr(result, "kind", None)
        if kind == "cancelled":
            return None
        deep = list(getattr(result, "deep_results", None) or [])
        for d in deep:
            if getattr(d, "usage_limited", False):
                return (usage_limit.seen_since(run_started)
                        or usage_limit.detect_usage_limit(d.error or "")
                        or usage_limit.UsageLimit(message=(d.error or "Claude Code usage limit")))
        recent = usage_limit.seen_since(run_started)
        if recent is None:
            return None
        if kind == "answer":
            text = (getattr(result, "text", "") or "").strip()
            if not text or usage_limit.detect_usage_limit(text):
                return recent
            return None
        if kind == "deep":
            if deep and all(d.met for d in deep):
                return None
            if any(d.decision_id for d in deep):
                return None
            return recent
        return None

    def _pause_for_usage_limit(self, task: Dict[str, Any], task_id: str,
                               limit: "usage_limit.UsageLimit", *,
                               conv_id: Optional[str], card_id: Optional[str],
                               session_id: Optional[str]) -> ExecutionOutcome:
        """Put a task stopped by the Claude usage limit back in the queue to resume at the reset.

        The lane-wide note (``usage_limit.record``) makes the poller stop claiming new Claude work
        until then; this puts THIS task back with ``start_at`` = the reset (plus grace), or a capped
        backoff when no reset time could be read, and with the session to continue. The backend
        holds it until then and tells its owner once. If the requeue write itself fails the task
        would sit in_progress forever, so it is reported failed instead, saying why.
        """
        limit = usage_limit.record(limit)
        hits = int(task.get("limit_hit_count") or 0) + 1
        resume_at = limit.resume_at(hit_count=hits)
        note = (f"Claude Code reached its {limit.label()}, so this is paused, not failed. It will "
                "resume automatically when Claude Code is available again and continue where it "
                "left off.")
        self._report_progress(task_id, "status", text=note,
                              data={"phase": "usage_limit",
                                    "resume_at": utc_stamp(resume_at),
                                    "limit": limit.message})
        requeue = getattr(self._client, "requeue_for_usage_limit", None)
        requeued = None
        if callable(requeue):
            try:
                requeued = requeue(task_id, start_at=resume_at, detail=limit.message,
                                   resume_session_id=session_id)
            except Exception:  # noqa: BLE001 -- handled below exactly like a failed write
                log.error("usage-limit requeue failed for task %s", task_id, exc_info=True)
        if not requeued:
            # A backend that predates the start-time hold refuses its fields. Put the task back as
            # plain queued instead: this lane's own pause keeps it from running until the reset,
            # so it still waits rather than failing.
            plain = getattr(self._client, "update_task", None)
            if callable(plain):
                try:
                    requeued = plain(task_id, {"status": "queued"}) or None
                except Exception:  # noqa: BLE001
                    log.error("plain requeue failed for task %s", task_id, exc_info=True)
        if not requeued:
            msg = (f"{note} The task could not be put back in the queue automatically, so it is "
                   "marked failed; run it again once Claude Code is available.")
            self._post_conv(conv_id, msg, kind="failed", task_id=task_id, card_id=card_id)
            self._safe_report_failed(task_id, msg)
            return ExecutionOutcome(task_id, "failed", msg)
        log.info("task %s paused on the Claude usage limit (%s); resumes at %s", task_id,
                 limit.message, resume_at.isoformat())
        self._post_conv(conv_id, note, kind="progress", task_id=task_id, card_id=card_id)
        return ExecutionOutcome(task_id, "waiting", note)

    @staticmethod
    def _provider_outage_behind(result: OrchestratorResult,
                                run_started: float) -> Optional["provider_outage.ProviderOutage"]:
        """The provider outage that stopped this run, or None when the run's outcome stands.

        Same rule as ``_usage_limit_behind``: a run that produced its result is never paused, and an
        outage is blamed only when one was recorded during this run and the run came back empty
        or unmet. A deep run that the runner reports as met, or that escalated to a human, stands.
        """
        kind = getattr(result, "kind", None)
        if kind == "cancelled":
            return None
        recent = provider_outage.seen_since(run_started)
        if recent is None:
            return None
        deep = list(getattr(result, "deep_results", None) or [])
        if kind == "answer":
            return None if (getattr(result, "text", "") or "").strip() else recent
        if kind == "deep":
            if deep and all(d.met for d in deep):
                return None
            if any(d.decision_id for d in deep):
                return None
            return recent
        return None

    def _pause_for_provider_outage(self, task: Dict[str, Any], task_id: str,
                                   outage: "provider_outage.ProviderOutage", *,
                                   conv_id: Optional[str], card_id: Optional[str],
                                   session_id: Optional[str]) -> ExecutionOutcome:
        """Put a task back in the queue to retry once the model provider is available again.

        The task waits (``start_at`` a few minutes ahead, ``hold_reason=provider_unavailable``) and
        the backend shows the wait on it. It is never marked failed for an outage. The one case that
        is reported failed is a backend that refuses the hold: a plain requeue would let the lane
        claim it again at once, so the person is told to run it again instead.
        """
        resume_at = provider_outage.resume_at()
        note = (f"The AI model provider is unavailable right now, so this is paused, not failed. "
                f"It will be tried again at {utc_stamp(resume_at)}.")
        self._report_progress(task_id, "status", text=note,
                              data={"phase": "provider_unavailable",
                                    "resume_at": utc_stamp(resume_at),
                                    "detail": outage.message})
        requeue = getattr(self._client, "requeue_for_provider_outage", None)
        requeued = None
        if callable(requeue):
            try:
                requeued = requeue(task_id, start_at=resume_at, detail=outage.message,
                                   resume_session_id=session_id)
            except Exception:  # noqa: BLE001 -- handled below exactly like a refused write
                log.error("provider-outage requeue failed for task %s", task_id, exc_info=True)
        if not requeued:
            msg = (f"{note} The task could not be put back in the queue automatically, so it is "
                   f"marked failed. Run it again once the provider is available. ({outage.message})")
            self._post_conv(conv_id, msg, kind="failed", task_id=task_id, card_id=card_id)
            self._safe_report_failed(task_id, msg)
            return ExecutionOutcome(task_id, "failed", msg)
        log.info("task %s paused on a provider outage (%s); retries at %s", task_id,
                 outage.message, resume_at.isoformat())
        self._post_conv(conv_id, note, kind="progress", task_id=task_id, card_id=card_id)
        return ExecutionOutcome(task_id, "waiting", note)
