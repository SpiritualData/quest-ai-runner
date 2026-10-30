"""Pausing a task: on a person's request, behind a usage limit or behind a provider outage.

A mixin of ``TaskExecutor`` (see ``executor.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from hubofhubs.models import outage as provider_outage

from ..core.orchestrator import OrchestratorResult
from ..core import usage_limit
from .executor_support import ExecutionOutcome, utc_stamp
from .executor_contracts import log

# How long a provider-unavailable task waits before its next try: short enough that a brief overload costs little, long enough
# that a long outage does not burn a claim every poll.
RETRY_INTERVAL = timedelta(minutes=10)


def next_try_at(now: Optional[datetime] = None) -> datetime:
    """When a task held for a provider outage is next tried."""
    return (now or datetime.now(timezone.utc)) + RETRY_INTERVAL


class PausesMixin:
    """Pausing a task: on a person's request, behind a usage limit or behind a provider outage."""

    @staticmethod
    def paused_deep_results(result: OrchestratorResult) -> List[Any]:
        """The deep results the worker reported as PAUSED on request (``DeepResult.paused``).

        Structural: the stream runner sets the flag from its own interrupt, so nothing here reads
        any text. Empty for every run that was not paused, which is how the executor tells a pause
        from a cancel (both end the orchestrator's result as ``cancelled``).
        """
        return [d for d in (getattr(result, "deep_results", None) or [])
                if getattr(d, "paused", False)]

    def hand_back_paused_task(self, task: Dict[str, Any], task_id: str, result: OrchestratorResult, *,
                              session_id: Optional[str]) -> ExecutionOutcome:
        """Hand a task whose run a person paused back to the queue, holding its Claude session.

        The same put-back as ``_pause_for_usage_limit`` (status queued plus ``resume_session_id``)
        with none of its timing: no ``start_at``, because a person's pause is lifted by a person.
        The backend keeps ``pause_requested`` on the task, so no lane can claim it until
        ``POST /api/assistant-tasks/{id}/resume`` clears the flag and a lane resumes this same
        session. A pause is not a result: no verifier call, no done, failed or needs_you report, and
        nothing posted into the chat (the person asked for it and the backend tells them).

        Messages are never lost. This method claims none (a message steered in after the pause
        stays unclaimed and the resumed run's opening drain delivers it). One the run had claimed
        but never confirmed reaching the model (``DeepResult.unseen_inputs``) goes back onto the
        task as a new message, and is named on the task's feed if even that write fails.

        If the hand-back write is refused the task would sit in_progress forever, so it is reported
        failed, saying why. A task cancelled while the run was stopping stays cancelled.
        """
        if self._is_task_cancelled(task_id):
            return self._quiet_cancelled(task_id, result)
        sid = (session_id or "").strip() or None
        requeue = getattr(self._client, "requeue_paused_task", None)
        requeued = None
        if callable(requeue):
            try:
                requeued = requeue(task_id, resume_session_id=sid)
            except Exception:  # noqa: BLE001 -- handled below exactly like a refused write
                log.error("pause hand-back failed for task %s", task_id, exc_info=True)
        if not requeued:
            msg = ("Paused, but the task could not be put back in the queue, so it is marked failed. "
                   "Run it again to continue.")
            self._safe_report_failed(task_id, msg)
            return ExecutionOutcome(task_id, "failed", msg)
        note = ("Paused. The run stopped at a safe point and kept the work it had done. Resume it "
                "to continue in the same session.")
        self._report_progress(task_id, "status", text=note,
                              data={"phase": "run_paused", "resume_session_id": sid})
        log.info("task %s paused on request; session %s is kept for the resume", task_id, sid)
        self.return_unseen_messages(task_id, self.paused_unseen_inputs(result))
        return ExecutionOutcome(task_id, "paused", note)

    @staticmethod
    def paused_unseen_inputs(result: OrchestratorResult) -> List[str]:
        """Messages a paused run claimed but never confirmed reaching its model, in order, once each."""
        seen: List[str] = []
        for deep in PausesMixin.paused_deep_results(result):
            for text in (getattr(deep, "unseen_inputs", None) or []):
                text = str(text or "").strip()
                if text and text not in seen:
                    seen.append(text)
        return seen

    def return_unseen_messages(self, task_id: str, texts: List[str]) -> None:
        """Put messages the run could not deliver back on the task, unclaimed, for the resume.

        Never raises. A message that cannot be written back is quoted on the task's feed instead,
        so it stays readable by the person who sent it.
        """
        post = getattr(self._client, "post_task_message", None)
        for text in texts:
            try:
                if not callable(post):
                    raise RuntimeError("this client cannot post a message to a task")
                post(task_id, text)
            except Exception as e:  # noqa: BLE001 -- the pause already went through
                log.warning("could not return an unconfirmed message to task %s: %s", task_id, e)
                self._report_progress(
                    task_id, "error",
                    text=("A message sent just before the pause was not delivered and could not be "
                          f"kept for the resume. Send it again: {text}"))

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
        resume_at = next_try_at()
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
