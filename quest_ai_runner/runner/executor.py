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
from typing import Any, Callable, Dict, Optional

from ..core.adapters import Mode, ProgressEvent
from ..core.orchestrator import Orchestrator, OrchestratorResult


@dataclass
class ExecutionOutcome:
    task_id: str
    status: str                       # "done" | "needs_you" | "failed"
    result: str = ""
    decision_id: Optional[str] = None


class _TaskProgressSink:
    """Routes orchestrator events to the task's live progress stream.

    Forwards all events EXCEPT raw streaming partials to report_progress, so the
    task-detail SSE stream shows step-by-step what the AI is doing (plan -> read ->
    answer) and live token counts. Milestones additionally post into the originating
    chat (same behavior as the old MilestoneSink path).
    """
    _SKIP = frozenset({"partial"})

    def __init__(self, task_id: str, report_fn: Callable, on_milestone: Optional[Callable]):
        self._task_id = task_id
        self._report = report_fn
        self._on_milestone = on_milestone

    def update(self, event: ProgressEvent, mode) -> None:
        if event.type in self._SKIP:
            return
        try:
            self._report(self._task_id, event.type, text=event.text, data=event.data)
        except Exception:  # noqa: BLE001
            pass
        try:
            if event.type == "milestone" and self._on_milestone:
                self._on_milestone(event)
        except Exception:  # noqa: BLE001
            pass


class TaskExecutor:
    def __init__(self, client, orchestrator: Orchestrator):
        self._client = client
        self._orch = orchestrator

    @staticmethod
    def _task_text(task: Dict[str, Any]) -> str:
        return (task.get("text") or task.get("title") or task.get("description") or "").strip()

    def execute(self, task: Dict[str, Any], *,
                rep_preamble: Optional[str] = None) -> ExecutionOutcome:
        """Run ONE claimed task and report its outcome.

        ``rep_preamble`` (optional) is a per-task context preamble forwarded to the deep run so the
        task executes AS a specific AI rep (its persona + learned corrections). When the poller has
        resolved a rep and pulled its profile, it builds this preamble and passes it here; the
        executor threads it straight into ``Orchestrator.run(rep_preamble=...)``, which forwards it
        to a deep runner that accepts a per-call ``context_preamble``. When ``None`` (any existing
        caller, or no rep resolved), behaviour is exactly as before.
        """
        task_id = str(task.get("id") or task.get("task_id") or "")
        text = self._task_text(task)
        quest_id = task.get("goal_id") or task.get("quest_id")
        # conv_id links this task back to the Quest AI conversation it was delegated from. When
        # present, we post LIVE progress (started → milestones → done) INTO that chat so the
        # conversation doesn't go silent after the hand-off.
        conv_id = task.get("conv_id") or None
        # model_hint: an optional per-task model/tier string stored by the consumer on the task
        # document (e.g. "opus", or any string the consumer's ModelRegistry understands).
        # Threaded into the orchestrator so the registry can honor it. None = default behavior.
        model_hint: Optional[str] = task.get("model") or None
        if not text:
            self._report_progress(task_id, "error", text="task had no instruction text to run")
            self._safe_report_failed(task_id, "task had no text/description to run")
            self._post_conv(conv_id, "I couldn't run this — the task had no instruction text.",
                            kind="done")
            return ExecutionOutcome(task_id, "failed", "task had no text/description")

        # Announce the start: a live progress event on the task (the task-detail stream) AND, when a
        # conv_id links this task to a chat, a started message into that chat.
        self._report_progress(task_id, "started", text=f"Started working on this: {text}")
        self._post_conv(conv_id, f"Started working on this: {text}", kind="started")

        # Route all orchestrator events (except raw streaming partials) to the task's live progress
        # stream so the task-detail SSE shows step-by-step what the AI is doing (plan, read, replan,
        # tokens). Milestones additionally post into the originating chat (same as MilestoneSink).
        sink = _TaskProgressSink(
            task_id,
            self._report_progress,
            on_milestone=lambda ev: self._on_milestone(task_id, conv_id, ev),
        )

        try:
            result: OrchestratorResult = self._orch.run(
                text, quest_id=quest_id, mode=Mode.BACKGROUND, sink=sink,
                model_hint=model_hint, rep_preamble=rep_preamble)
        except Exception as e:  # noqa: BLE001 — brain failure -> failed report, never crash poller
            msg = f"orchestrator error: {type(e).__name__}: {e}"
            self._report_progress(task_id, "error", text=msg)
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
        """Surface a real milestone: the live task-detail stream AND the originating chat.

        Background runs surface only real milestones/decisions/results (the MilestoneSink policy),
        so this fires for genuine progress — never planning/reading chatter. We fan each milestone
        to BOTH the task progress stream (kind="exec") and the chat (kind="progress"). Both posts
        are best-effort: a dropped progress event must never affect the task outcome."""
        if event.text:
            self._report_progress(task_id, "exec", text=event.text)
            self._post_conv(conv_id, event.text, kind="progress")

    def _report_progress(self, task_id: str, kind: str, *, text: Optional[str] = None,
                         output: Optional[str] = None,
                         data: Optional[Dict[str, Any]] = None) -> None:
        """Best-effort: post a live execution-progress event onto the task (the task-detail stream).

        No-ops when the client lacks ``report_progress`` (older clients / mocks), and never raises —
        the client's own ``report_progress`` is best-effort, but we also guard the call here so a
        progress event can never affect the task's success/failure."""
        if not task_id:
            return
        report = getattr(self._client, "report_progress", None)
        if callable(report):
            self._safe(lambda _d=data: report(task_id, kind, text=text, output=output, data=_d))

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
            # BROKEN-PROMISE GUARD: if the orchestrator rewrote this answer to be honest about a
            # claimed action that did NOT actually complete (claim_corrected), the task is NOT done
            # — surface it as needs_you so a human picks it up, rather than marking it complete on a
            # reply that says the work was not finished. (A plain ``partial`` best-effort answer,
            # from the read-budget cap, is still a legitimate informational answer and stays done.)
            if getattr(result, "claim_corrected", False):
                self._report_progress(task_id, "done", text="Paused. Needs you.", output=text)
                self._safe(lambda: self._client.report_needs_you(task_id, text, ""))
                self._post_conv(conv_id, text, kind="done")
                return ExecutionOutcome(task_id, "needs_you", text)
            self._report_progress(task_id, "done", text="Done.", output=text)
            self._safe(lambda: self._client.report_done(task_id, text))
            self._post_conv(conv_id, text, kind="done")
            return ExecutionOutcome(task_id, "done", text)

        if result.kind == "confirm":
            summary = result.question or "A human decision is required before proceeding."
            # needs_you is a terminal-but-paused state; close the live stream with a 'done' tick
            # noting it now needs a human, so the stream doesn't hang open.
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
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
            self._report_progress(task_id, "done", text="Done.", output=summary)
            self._safe(lambda: self._client.report_done(task_id, summary))
            self._post_conv(conv_id, summary, kind="done")
            return ExecutionOutcome(task_id, "done", summary)
        # A deep run that raised a human decision instead of finishing.
        decision_id = next((d.decision_id for d in deep if d.decision_id), None)
        if decision_id:
            summary = "A human decision is required to finish this task."
            # A confirm-before-act run carries the prepared output (e.g. the code awaiting review).
            chat_text = next((d.output for d in deep if d.output), None) or summary
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
            self._safe(lambda: self._client.report_needs_you(task_id, summary, decision_id))
            self._post_conv(conv_id, chat_text, kind="decision")
            return ExecutionOutcome(task_id, "needs_you", summary, decision_id)
        # Otherwise the run hit a limit / errored.
        errs = "; ".join(d.error for d in deep if d.error) or "the goal was not met"
        if not deep:                 # deep requested but no runner wired -> needs human/runner
            errs = "deep work required but no deep-runner is configured: " + "; ".join(result.goals)
        self._report_progress(task_id, "error", text=errs)
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
