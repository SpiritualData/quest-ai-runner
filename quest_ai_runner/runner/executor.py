"""Executor — run ONE claimed Quest task through the brain + goal-runner, report the result.

Given a single claimed assistant-task, the executor:
  1. Builds the request (the task text) + a small context view.
  2. Runs it through ``core.Orchestrator`` with the configured adapters.
  3. Maps the OrchestratorResult onto the Quest task callback:
       - answer            -> PATCH done   + result (the answer text)
       - deep (met)        -> PATCH done   + result (the run output / summary)
       - deep (not met)    -> PATCH failed | needs_you  (limit/error -> failed; raised a
                              decision -> needs_you with the decision_id)
       - confirm           -> a decision-request was raised -> PATCH needs_you + decision_id
  4. Never raises to the poller: any error becomes a PATCH failed with the message.

It does NOT claim or discover (the poller owns that) — it is the unit of work for one task, so
it can be unit-tested against a mock Quest client + stub brain with no network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..core.adapters import Mode, MilestoneSink, ProgressEvent
from ..core.orchestrator import Orchestrator, OrchestratorResult


@dataclass
class ExecutionOutcome:
    task_id: str
    status: str                       # "done" | "needs_you" | "failed"
    result: str = ""
    decision_id: Optional[str] = None


class TaskExecutor:
    def __init__(self, client, orchestrator: Orchestrator):
        self._client = client
        self._orch = orchestrator

    @staticmethod
    def _task_text(task: Dict[str, Any]) -> str:
        return (task.get("text") or task.get("title") or task.get("description") or "").strip()

    def execute(self, task: Dict[str, Any]) -> ExecutionOutcome:
        task_id = str(task.get("id") or task.get("task_id") or "")
        text = self._task_text(task)
        quest_id = task.get("goal_id") or task.get("quest_id")
        # conv_id links this task back to the Quest AI conversation it was delegated from. When
        # present, we post LIVE progress (started → milestones → done) INTO that chat so the
        # conversation doesn't go silent after the hand-off.
        conv_id = task.get("conv_id") or None
        if not text:
            self._safe_report_failed(task_id, "task had no text/description to run")
            self._post_conv(conv_id, "I couldn't run this — the task had no instruction text.",
                            kind="done")
            return ExecutionOutcome(task_id, "failed", "task had no text/description")

        # Announce the start into the chat as soon as we pick it up.
        self._post_conv(conv_id, f"Started working on this: {text}", kind="started")

        # BACKGROUND lane: nobody is attending the run loop, so route it through a MilestoneSink.
        # The sink (NOT the brain) drops planning/reading/re-planning chatter and surfaces only
        # real milestones / decisions / the result — the "inform along the way" discipline. The
        # SAME MilestoneSink both keeps the optional Quest progress note AND (when a conv_id is
        # present) posts each milestone into the originating chat. Final result + decision still go
        # through the established _report path below, which also posts the closing chat message.
        sink = MilestoneSink(
            on_milestone=lambda ev: self._on_milestone(task_id, conv_id, ev))

        try:
            result: OrchestratorResult = self._orch.run(
                text, quest_id=quest_id, mode=Mode.BACKGROUND, sink=sink)
        except Exception as e:  # noqa: BLE001 — brain failure -> failed report, never crash poller
            msg = f"orchestrator error: {type(e).__name__}: {e}"
            self._safe_report_failed(task_id, msg)
            self._post_conv(conv_id, f"I hit an error working on this: {msg}", kind="done")
            return ExecutionOutcome(task_id, "failed", msg)

        return self._report(task_id, result, conv_id)

    def report(self, task_id: str, result: OrchestratorResult,
               conv_id: Optional[str] = None) -> ExecutionOutcome:
        """Public: map an ALREADY-PRODUCED OrchestratorResult onto the Quest callback + chat.

        ``execute()`` runs the brain and then reports; but an integrator whose deep run executes
        ASYNCHRONOUSLY *outside* ``execute()`` (e.g. a host application spawns a ``/goal`` subprocess
        and only learns the outcome later, on a different thread) needs to report that finished
        outcome through the SAME three-way policy — done / needs_you / failed — and the same
        post-back-into-chat behaviour. They build the OrchestratorResult from the finished run and
        call ``report(...)`` so async and in-loop runs report IDENTICALLY. Thin, deliberate
        wrapper over the internal ``_report`` so the policy lives in exactly one place."""
        return self._report(task_id, result, conv_id)

    def _on_milestone(self, task_id: str, conv_id: Optional[str], event: ProgressEvent) -> None:
        """Surface a real milestone: the optional Quest progress note AND the originating chat.

        Background runs surface only real milestones/decisions/results (the MilestoneSink policy),
        so this fires for genuine progress — never planning/reading chatter. Both posts are
        best-effort: a dropped progress note or chat post must never affect the task outcome."""
        report = getattr(self._client, "report_progress", None)
        if callable(report) and event.text:
            self._safe(lambda: report(task_id, event.text))
        if event.text:
            self._post_conv(conv_id, event.text, kind="progress")

    def _post_conv(self, conv_id: Optional[str], content: str, *, kind: str) -> None:
        """Best-effort: append a live progress message into the originating chat, if one is linked.

        Never raises and never affects the task's success/failure — if the conversation post fails
        (network, conversation gone), the task still reports its result normally via PATCH."""
        if not conv_id or not content:
            return
        post = getattr(self._client, "post_conversation_message", None)
        if callable(post):
            self._safe(lambda: post(conv_id, content, kind=kind))

    # --- result -> Quest callback -------------------------------------------

    def _report(self, task_id: str, result: OrchestratorResult,
                conv_id: Optional[str] = None) -> ExecutionOutcome:
        if result.kind == "answer":
            text = result.text or "(no answer produced)"
            self._safe(lambda: self._client.report_done(task_id, text))
            self._post_conv(conv_id, text, kind="done")
            return ExecutionOutcome(task_id, "done", text)

        if result.kind == "confirm":
            summary = result.question or "A human decision is required before proceeding."
            self._post_conv(conv_id, summary, kind="decision")
            if result.decision_id:
                self._safe(lambda: self._client.report_needs_you(task_id, summary, result.decision_id))
                return ExecutionOutcome(task_id, "needs_you", summary, result.decision_id)
            # No decision id (no escalation sink wired) — surface as needs_you without an id.
            self._safe(lambda: self._client.report_needs_you(task_id, summary, ""))
            return ExecutionOutcome(task_id, "needs_you", summary)

        # deep
        deep = result.deep_results
        if deep and all(d.met for d in deep):
            summary = "\n\n".join(d.output for d in deep if d.output) or "Goal(s) met."
            self._safe(lambda: self._client.report_done(task_id, summary))
            self._post_conv(conv_id, summary, kind="done")
            return ExecutionOutcome(task_id, "done", summary)
        # A deep run that raised a human decision instead of finishing.
        decision_id = next((d.decision_id for d in deep if d.decision_id), None)
        if decision_id:
            summary = "A human decision is required to finish this task."
            # A confirm-before-act run carries the prepared output (e.g. the code awaiting review).
            chat_text = next((d.output for d in deep if d.output), None) or summary
            self._safe(lambda: self._client.report_needs_you(task_id, summary, decision_id))
            self._post_conv(conv_id, chat_text, kind="decision")
            return ExecutionOutcome(task_id, "needs_you", summary, decision_id)
        # Otherwise the run hit a limit / errored.
        errs = "; ".join(d.error for d in deep if d.error) or "the goal was not met"
        if not deep:                 # deep requested but no runner wired -> needs human/runner
            errs = "deep work required but no deep-runner is configured: " + "; ".join(result.goals)
        self._safe(lambda: self._client.report_failed(task_id, errs))
        self._post_conv(conv_id, f"I couldn't complete this: {errs}", kind="done")
        return ExecutionOutcome(task_id, "failed", errs)

    # --- safety wrappers (reporting must not crash the poller) ---------------

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"[executor] report failed: {type(e).__name__}: {e}")

    def _safe_report_failed(self, task_id: str, msg: str):
        self._safe(lambda: self._client.report_failed(task_id, msg))
