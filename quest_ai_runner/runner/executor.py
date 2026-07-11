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
       - cancelled         -> NO PATCH (the backend already set status=cancelled and appends its
                              own terminal chat message; a PATCH here would just 409) -- a
                              best-effort progress note only.
  4. Never raises to the poller: any error becomes a PATCH failed with the message, unless the
     task was cancelled meanwhile (see above).

It does NOT claim or discover (the poller owns that) — it is the unit of work for one task, so
it can be unit-tested against a mock Quest client + stub brain with no network.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..core.adapters import Mode, ProgressEvent
from ..core.orchestrator import Orchestrator, OrchestratorResult

log = logging.getLogger("quest-ai-runner.executor")

# How often (seconds of real time) the throttled cancel_check built by ``_build_cancel_check`` is
# allowed to actually call the Quest API. The orchestrator may poll cancel_check at every loop
# boundary (frequently), but cancellation is a rare, human-triggered event -- hammering the API on
# every check would waste calls for no benefit.
CANCEL_CHECK_INTERVAL_SECONDS = 15.0

# Hard cap on the FALLBACK prior-conversation read in ``_build_context_view`` (the path taken only
# when no ConversationStore is wired). Without a cap, a long-running conversation would dump its
# entire transcript into every linked task's prompt, growing without bound as the conversation
# grows. Passed as ``max_bytes`` to ``read_section`` so the serving adapter truncates at the
# source; adapters that serve conversations keep the recent tail when truncating (see
# ``ClaudeConversationsAdapter.read_section``).
CONV_CONTEXT_MAX_BYTES = 16_000


@dataclass
class ExecutionOutcome:
    task_id: str
    status: str                       # "done" | "needs_you" | "failed" | "cancelled"
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
        # Cache the retrieval adapter from the orchestrator so _build_context_view can fetch
        # conversation history when conv_id is present
        self._retrieval = getattr(orchestrator, "retrieval", None)

    @staticmethod
    def _task_text(task: Dict[str, Any]) -> str:
        return (task.get("text") or task.get("title") or task.get("description") or "").strip()

    def _build_cancel_check(self, task_id: str,
                            interval: float = CANCEL_CHECK_INTERVAL_SECONDS) -> Callable[[], bool]:
        """Build a THROTTLED ``cancel_check`` callable to pass into ``Orchestrator.run()``.

        The orchestrator may poll this at every internal loop boundary (plan/gather/replan step,
        each deep-goal retry attempt) -- far more often than a cancellation could plausibly happen
        (a human hitting "stop" is rare and not latency-sensitive). Calling ``is_task_cancelled``
        on every poll would hammer the Quest API for no benefit, so this calls it AT MOST once per
        ``interval`` seconds of real time and returns the last known answer in between. Falls back
        to an always-False check when there's no task id or the client lacks the method (older
        clients / mocks), so the run behaves exactly as before.
        """
        if not task_id:
            return lambda: False
        is_cancelled = getattr(self._client, "is_task_cancelled", None)
        if not callable(is_cancelled):
            return lambda: False
        state = {"checked_at": 0.0, "cancelled": False}

        def _check() -> bool:
            now = time.monotonic()
            if now - state["checked_at"] >= interval:
                state["checked_at"] = now
                try:
                    state["cancelled"] = bool(is_cancelled(task_id))
                except Exception:  # noqa: BLE001 -- a check must never crash a run
                    pass
            return state["cancelled"]

        return _check

    def _is_task_cancelled(self, task_id: str) -> bool:
        """Best-effort, UNTHROTTLED cancellation check for the final reporting path.

        Used right before a terminal PATCH (done/failed) and after an orchestrator error, so a run
        that dies or finishes BECAUSE it was interrupted is not mistakenly reported once more (which
        would also just 409 against a task the backend already marked cancelled). Never raises:
        ``is_task_cancelled`` is fail-open by contract, and this also tolerates a client that lacks
        the method entirely (older clients / mocks).
        """
        if not task_id:
            return False
        is_cancelled = getattr(self._client, "is_task_cancelled", None)
        if not callable(is_cancelled):
            return False
        try:
            return bool(is_cancelled(task_id))
        except Exception:  # noqa: BLE001
            return False

    def _quiet_cancelled(self, task_id: str,
                         result: Optional[OrchestratorResult] = None) -> ExecutionOutcome:
        """The quiet-cancelled path: the task was stopped mid-run (cooperatively, by the
        orchestrator's own ``cancel_check``, or detected here right before reporting).

        Do NOT PATCH the task (the backend already set ``status=cancelled``; a PATCH would just
        409) and do NOT post a done/failed message into the conversation (the backend appends its
        own terminal "cancelled" chat message) -- just a best-effort status note on the task's own
        progress stream, a log line, and a "cancelled" outcome for the poller.
        """
        self._report_progress(task_id, "status", text="Stopped: this task was cancelled.")
        log.info("task %s stopped: cancelled mid-run", task_id)
        rationale = (getattr(result, "rationale", "") or "").strip() if result else ""
        return ExecutionOutcome(task_id, "cancelled", rationale or "task was cancelled")

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
        goal_id = task.get("goal_id")
        quest_id = task.get("quest_id")
        # If only goal_id is set, we'll need quest_id to fetch the goal. Try to infer it from
        # task metadata or fetch it separately if the backend provides it.
        if goal_id and not quest_id:
            quest_id = task.get("_inferred_quest_id")
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
                            kind="done", task_id=task_id)
            return ExecutionOutcome(task_id, "failed", "task had no text/description")

        # Announce the start: a live progress event on the task (the task-detail stream) AND, when a
        # conv_id links this task to a chat, a started message into that chat.
        self._report_progress(task_id, "started", text=f"Started working on this: {text}")
        self._post_conv(conv_id, f"Started working on this: {text}", kind="started", task_id=task_id)

        # Fetch goal + quest context + conversation history from Quest API if available, and build
        # a context_view for the orchestrator so the deep agent knows what goal/quest it's working on
        # and the prior conversation that led to the task.
        context_view = self._build_context_view(goal_id, quest_id, conv_id)

        # Route all orchestrator events (except raw streaming partials) to the task's live progress
        # stream so the task-detail SSE shows step-by-step what the AI is doing (plan, read, replan,
        # tokens). Milestones additionally post into the originating chat (same as MilestoneSink).
        sink = _TaskProgressSink(
            task_id,
            self._report_progress,
            on_milestone=lambda ev: self._on_milestone(task_id, conv_id, ev),
        )

        # Build a scope for finding RELATED past conversations (the orchestrator's Step 1, User
        # Input Understanding) from whatever identity the task carries. Omit missing keys so a
        # store's best-effort scope filter only constrains on fields actually present.
        conv_scope: Dict[str, Any] = {}
        for _src, _dst in (("user_id", "user_id"), ("team_id", "team_id"),
                           ("team_ids", "team_ids"), ("participant_id", "participant_id")):
            _val = task.get(_src)
            if _val is not None:
                conv_scope[_dst] = _val

        # Thread the task's goal identity into the orchestrator's context meta. quest_id already
        # travels as its own run() param, but a personal "goal is the hub" task carries its id in
        # goal_id (often with NO quest_id), and context assemblers that scope by goal — e.g.
        # FileContextStore's quest_folder_map boost — would otherwise never see it.
        context_meta: Optional[Dict[str, Any]] = {"goal_id": goal_id} if goal_id else None

        # Cooperative mid-run cancellation: a THROTTLED check (see _build_cancel_check) threaded
        # into the orchestrator so a human hitting "stop" while this task is in_progress can abort
        # the run cleanly at its next loop boundary instead of running to completion regardless.
        cancel_check = self._build_cancel_check(task_id)

        try:
            result: OrchestratorResult = self._orch.run(
                text, quest_id=quest_id, context_view=context_view, mode=Mode.BACKGROUND,
                sink=sink, model_hint=model_hint, rep_preamble=rep_preamble,
                context_meta=context_meta,
                conv_id=conv_id, conv_scope=conv_scope or None, cancel_check=cancel_check)
        except Exception as e:  # noqa: BLE001 — brain failure -> failed report, never crash poller
            # A run that raises BECAUSE it was interrupted must not be reported as failed: check
            # (unthrottled, this is the terminal path) whether the task was cancelled meanwhile.
            if self._is_task_cancelled(task_id):
                return self._quiet_cancelled(task_id)
            msg = f"orchestrator error: {type(e).__name__}: {e}"
            self._report_progress(task_id, "error", text=msg)
            self._safe_report_failed(task_id, msg)
            self._post_conv(conv_id, f"I hit an error working on this: {msg}", kind="done",
                            task_id=task_id)
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
            self._post_conv(conv_id, event.text, kind="progress", task_id=task_id)

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

    def _post_conv(self, conv_id: Optional[str], content: str, *, kind: str,
                   task_id: Optional[str] = None) -> None:
        """Best-effort: append a live progress message into the originating chat, if one is linked.

        ``task_id``, when given, is stamped on the posted message so the frontend can correlate it
        back to the task's own lifecycle. Never raises and never affects the task's success/failure:
        if the conversation post fails (network, conversation gone), the task still reports its
        result normally via PATCH."""
        if not conv_id or not content:
            return
        post = getattr(self._client, "post_conversation_message", None)
        if callable(post):
            self._safe(lambda: post(conv_id, content, kind=kind, task_id=task_id))

    def _build_context_view(self, goal_id: Optional[str], quest_id: Optional[str],
                            conv_id: Optional[str] = None) -> str:
        """Fetch goal + quest metadata + notes + conversation history from the Quest API.

        The context_view is passed to the orchestrator so the deep agent knows what goal/quest
        it's working on, what progress has been made, and the prior conversation that led to
        the task. Gracefully handles missing API (no-ops when client lacks needed methods) and
        API errors (builds partial context)."""
        parts = []

        # Fetch prior conversation history if this task was delegated from a chat — but ONLY as a
        # FALLBACK when the orchestrator has no ConversationStore wired. When a store IS wired, the
        # orchestrator's Step 1 (User Input Understanding) pulls the relevant slice itself (and
        # resolves the request from it), so we must not also dump the full transcript here.
        if (conv_id and self._retrieval
                and getattr(self._orch, "conversation_store", None) is None):
            try:
                # Try to read the EXACT conversation by its conv_id. BOUNDED: max_bytes caps the
                # transcript at the source, so a long conversation can never grow this task's
                # prompt without bound (a conversation-aware adapter keeps the recent tail).
                obs = self._retrieval.read_section(str(conv_id),
                                                   max_bytes=CONV_CONTEXT_MAX_BYTES)
                if obs and obs.kind == "read" and obs.text:
                    # Explicitly mark which conversation we loaded to disambiguate from previous tasks
                    parts.append(f"=== Prior Conversation Context (conv_id={conv_id}) ===\n{obs.text}\n")
                elif obs and obs.kind == "error":
                    # Conversation not found is non-critical, but log it for debugging
                    # (the task still runs with just the goal/quest context, not the prior chat)
                    pass
            except Exception:  # noqa: BLE001 — conversation fetch failure is non-critical
                pass

        if not goal_id and not quest_id:
            return "\n".join(parts) if parts else ""
        # Fetch quest metadata if available
        if quest_id:
            get_quest = getattr(self._client, "get_quest", None)
            if callable(get_quest):
                try:
                    quest = get_quest(quest_id)
                    if quest:
                        outcome = quest.get("outcome", "")
                        if outcome:
                            parts.append(f"Quest outcome: {outcome}")
                        completed = quest.get("completed")
                        if completed is not None:
                            status = "completed" if completed else "in progress"
                            parts.append(f"Quest status: {status}")
                except Exception:  # noqa: BLE001
                    pass  # API unavailable or error; continue with what we have

        # Fetch goal metadata if available
        if goal_id and quest_id:
            get_goal = getattr(self._client, "get_goal", None)
            if callable(get_goal):
                try:
                    goal = get_goal(goal_id, quest_id=quest_id)
                    if goal:
                        name = goal.get("name", "")
                        if name:
                            parts.append(f"Goal: {name}")
                        description = goal.get("description", "")
                        if description:
                            parts.append(f"Goal description: {description}")
                        deadline = goal.get("deadline", "")
                        if deadline:
                            parts.append(f"Goal deadline: {deadline}")
                        completed = goal.get("completed")
                        if completed is not None:
                            status = "completed" if completed else "in progress"
                            parts.append(f"Goal status: {status}")
                except Exception:  # noqa: BLE001
                    pass  # API unavailable or error; continue with what we have

            # Fetch recent goal notes for context
            list_notes = getattr(self._client, "list_goal_notes", None)
            if callable(list_notes):
                try:
                    notes = list_notes(goal_id, quest_id=quest_id, limit=5)
                    if notes:
                        notes_text = "\n".join(
                            f"  • {n.get('text', '')}" for n in notes if n.get("text")
                        )
                        if notes_text:
                            parts.append(f"Goal notes:\n{notes_text}")
                except Exception:  # noqa: BLE001
                    pass  # API unavailable or error; continue with what we have

        return "\n".join(parts) if parts else ""  # Return combined conversation + quest/goal context

    # --- result -> Quest callback -------------------------------------------

    def _report(self, task_id: str, result: OrchestratorResult,
                conv_id: Optional[str] = None) -> ExecutionOutcome:
        # Cooperative cancellation: ``result.kind == "cancelled"`` is the orchestrator's OWN
        # cooperative signal (its ``cancel_check`` returned True mid-run); the extra
        # ``_is_task_cancelled`` re-check covers the race where the run finished (or an async
        # caller reports through the public ``report()`` API) right as/after a human cancelled the
        # task, before we PATCH a terminal status that would just 409 anyway.
        if result.kind == "cancelled" or self._is_task_cancelled(task_id):
            return self._quiet_cancelled(task_id, result)
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
                self._post_conv(conv_id, text, kind="done", task_id=task_id)
                return ExecutionOutcome(task_id, "needs_you", text)
            # Append goal-verdict reasoning so the reader knows whether the goal was confirmed
            # met, hit max iterations unverified, or was a best-effort partial answer.
            verdict_suffix = ""
            exit_reason = getattr(result, "exit_reason", "")
            goal_verdict = getattr(result, "goal_verdict", None)
            if exit_reason == "max_turns" and goal_verdict:
                reason = (goal_verdict.get("reason") or "").strip()
                next_action = (goal_verdict.get("next_action") or "").strip()
                verdict_suffix = f"\n\n---\nGoal not fully verified after all attempts."
                if reason:
                    verdict_suffix += f" {reason}"
                if next_action:
                    verdict_suffix += f" To complete: {next_action}"
            elif exit_reason == "read_budget":
                verdict_suffix = "\n\n---\nNote: this is a best-effort answer based on context gathered so far."
            done_text = text + verdict_suffix if verdict_suffix else text
            self._report_progress(task_id, "done", text="Done.", output=done_text)
            self._safe(lambda: self._client.report_done(task_id, done_text))
            self._post_conv(conv_id, done_text, kind="done", task_id=task_id)
            return ExecutionOutcome(task_id, "done", done_text)

        if result.kind == "confirm":
            summary = result.question or "A human decision is required before proceeding."
            # needs_you is a terminal-but-paused state; close the live stream with a 'done' tick
            # noting it now needs a human, so the stream doesn't hang open.
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
            self._post_conv(conv_id, summary, kind="decision", task_id=task_id)
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
            self._post_conv(conv_id, summary, kind="done", task_id=task_id)
            return ExecutionOutcome(task_id, "done", summary)
        # A deep run that raised a human decision instead of finishing.
        decision_id = next((d.decision_id for d in deep if d.decision_id), None)
        if decision_id:
            summary = "A human decision is required to finish this task."
            # A confirm-before-act run carries the prepared output (e.g. the code awaiting review).
            chat_text = next((d.output for d in deep if d.output), None) or summary
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
            self._safe(lambda: self._client.report_needs_you(task_id, summary, decision_id))
            self._post_conv(conv_id, chat_text, kind="decision", task_id=task_id)
            return ExecutionOutcome(task_id, "needs_you", summary, decision_id)
        # Otherwise the run hit a limit / errored.
        errs = "; ".join(d.error for d in deep if d.error) or "the goal was not met"
        if not deep:                 # deep requested but no runner wired -> needs human/runner
            errs = "deep work required but no deep-runner is configured: " + "; ".join(result.goals)
        self._report_progress(task_id, "error", text=errs)
        self._safe(lambda: self._client.report_failed(task_id, errs))
        self._post_conv(conv_id, f"I couldn't complete this: {errs}", kind="done", task_id=task_id)
        return ExecutionOutcome(task_id, "failed", errs)

    # --- safety wrappers (reporting must not crash the poller) ---------------

    def _safe(self, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.error("report failed", exc_info=True)

    def _safe_report_failed(self, task_id: str, msg: str):
        self._safe(lambda: self._client.report_failed(task_id, msg))
