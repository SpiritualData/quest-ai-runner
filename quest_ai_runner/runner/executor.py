"""Executor — run ONE claimed Quest task through the brain + goal-runner, report the result.

Given a single claimed assistant-task, the executor:
  1. Builds the request (the task text) + a small context view.
  2. Runs it through ``core.Orchestrator`` with the configured adapters.
  3. Maps the OrchestratorResult onto the Quest task callback:
       - answer            -> PATCH done   + result (the answer text)
       - deep (met)        -> PATCH done   + result (the run output / summary)
       - deep (not met)    -> PATCH incomplete | failed | needs_you  (ran and did work but fell
                              short of the goal -> incomplete; produced nothing because it
                              errored -> failed; raised a decision -> needs_you with the
                              decision_id)
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

import inspect
import time
from typing import Any, Callable, Dict, List, Optional

from ..core import provider_outage, usage_limit
from ..core.adapters import Mode
from ..core.orchestrator import Orchestrator, OrchestratorResult
from ..core.requester import requester_from_task
from ..core.run_control import ControlMessage, RunControl
from ..core.work_model import WorkModelUnavailable, apply_work_model, default_effective, is_tier_word

from .executor_contracts import (
    AUTOMATIC_DELIVERY_NOTE,
    CANCEL_CHECK_INTERVAL_SECONDS,
    GOAL_UPDATE_CONTEXT_LIMIT,
    KEEP_GOING_CONTRACT,
    PERSON_NOTE_FLOOR,
    REPLY_LOOP_CONTRACT,
    RESULT_IS_THE_WORK_CONTRACT,
    describe_reply_target,
    earlier_run_block,
    log,
    render_goal_notes,
    render_goal_updates,
)
from .executor_support import (
    CONV_CONTEXT_MAX_BYTES,
    ExecutionOutcome,
    MESSAGE_CLAIM_INTERVAL_SECONDS,
    RUN_CONTROL_POLL_SECONDS,
    _TaskProgressSink,
    _autopilot_composed_text,
    _is_autopilot_pass,
    _model_used_note,
    _run_accepts,
    across_pass_resume,
    task_scope_tags,
    terminal_session_id,
)
from .executor_context import ContextViewMixin
from .executor_followup import FollowUpMixin
from .executor_pauses import PausesMixin
from .executor_reporting import ReportingMixin
from .run_operations import pinned_operations




class TaskExecutor(ContextViewMixin, FollowUpMixin, PausesMixin, ReportingMixin):
    def __init__(self, client, orchestrator: Orchestrator, *,
                quest_folder_map: Optional[Dict[str, str]] = None,
                autopilot_pass: Optional[Any] = None,
                quest_folder_zones: bool = True,
                update_engine: Optional[Any] = None,
                guidance_manager: Optional[Any] = None):
        self._client = client
        self._orch = orchestrator
        # Where a STANDING instruction goes so that future runs retrieve it (a
        # ``GuidanceCardManager``-shaped object, or None). The ledger tracks whether a rule is
        # being followed; guidance is what puts it in front of tomorrow's run. Neither does the
        # other's job, and with no manager wired a standing item is still tracked, just not carded.
        self._guidance_manager = guidance_manager
        # The consumer's ``UpdateEngine`` (runner/context_updates.py), wired by the poller. With
        # one, a task's context view carries what changed on the quest's channels since a run last
        # looked (notes, captures judged for this card, document comments, a watched collection),
        # ref-tagged so the result can carry a receipt. None -> the context view reads the notes
        # and captures itself, exactly as before the engine existed.
        self._update_engine = update_engine
        # The bundle this task was handed, kept so the run's result can carry its receipt and so
        # the watermark can move once the run really had it. One executor serves one task.
        self._context_bundle: Optional[Any] = None
        # Cache the retrieval adapter from the orchestrator so _build_context_view can fetch
        # conversation history when conv_id is present
        self._retrieval = getattr(orchestrator, "retrieval", None)
        # {goal_or_quest_id: folder} -- see _resolve_working_dir. Same map the poller consults for
        # QUEST_SYNC.md sync (RunnerConfig.quest_folder_map); None/empty = every task uses the
        # deep-runner's configured global working_dir, exactly as before this feature existed.
        self._quest_folder_map = quest_folder_map or {}
        # Whether a run on a foldered quest is told the three-zone provenance convention
        # (RunnerConfig.quest_folder_zones). Off = the folder is organised some other way and the
        # contract would be describing directories that do not exist.
        self._quest_folder_zones = bool(quest_folder_zones)
        # The consumer's AutopilotPass (see runner/autopilot.py), wired by the poller. A task whose
        # ``handler == "autopilot"`` is routed to it instead of a normal deep run (see execute()).
        # None (no consumer wiring, or a runner that never sees such a task) -> untouched behavior.
        self._autopilot = autopilot_pass

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

    # --- mid-run steering messages (human sends a message while this task runs) --------------

    def _claim_task_messages(self, task_id: str) -> List[Dict[str, Any]]:
        """Best-effort: claim any pending mid-task messages for ``task_id`` via the Quest client.

        No-ops (returns ``[]``) when there's no task id or the client lacks
        ``claim_task_messages`` (older clients / mocks), and never raises -- the client's own
        method is already best-effort by contract, but this call site guards too so a claim
        failure can never affect the run.
        """
        if not task_id:
            return []
        claim = getattr(self._client, "claim_task_messages", None)
        if not callable(claim):
            return []
        try:
            return claim(task_id) or []
        except Exception:  # noqa: BLE001
            return []

    def _mark_messages_seen(self, task_id: str, claimed: List[Dict[str, Any]], where: str) -> None:
        """Tell the backend these claimed messages have now reached a model turn.

        Claiming only means the runner holds a message; ``seen`` is what the person is shown, so it
        is reported at the point the text is handed on to the model (``where`` names which). No-ops
        for a client without ``mark_task_messages_seen`` and never raises."""
        ids = [str(m.get("message_id")) for m in (claimed or []) if m.get("message_id")]
        mark = getattr(self._client, "mark_task_messages_seen", None)
        if not task_id or not ids or not callable(mark):
            return
        try:
            mark(task_id, ids, where)
        except Exception:  # noqa: BLE001 -- a status stamp must never affect the run
            pass

    @staticmethod
    def _message_texts(claimed: List[Dict[str, Any]]) -> List[str]:
        """Extract non-empty ``text`` fields from a list of claimed message dicts, in order."""
        texts = [str(m.get("text") or "").strip() for m in (claimed or [])]
        return [t for t in texts if t]

    def _build_pending_inputs(self, task_id: str,
                              interval: float = MESSAGE_CLAIM_INTERVAL_SECONDS
                              ) -> Callable[[], List[str]]:
        """Build a THROTTLED ``pending_inputs`` callable to pass EXPLICITLY into
        ``Orchestrator.run()``.

        Passing it explicitly (rather than leaving ``pending_inputs`` as ``None`` and letting the
        orchestrator auto-wire from a wired ``input_inbox``) is deliberate: that auto-wiring's
        ``_conv_key`` resolution (``orchestrator.py`` around lines 6839-6844) only resolves a key
        via ``quest_id`` / ``conversation_id`` / ``session_id`` / ``user_id`` in the context meta --
        a goal-only personal task (no quest_id, no chat identity in scope) would never resolve a
        key there and so would never see a mid-run message at all. Claiming straight from this
        task's own id sidesteps that: every claimed task has a task id, quest-linked or not.

        Mirrors ``_build_cancel_check``'s throttle: the orchestrator may poll this at every
        internal loop boundary (plan/gather/replan step, each deep-goal retry attempt, each
        answer-improve attempt) -- far more often than a human is plausibly sending a new message,
        so this only actually calls ``claim_task_messages`` at most once per ``interval`` seconds
        of real time and returns ``[]`` in between. A message sent inside that window is never
        lost, only picked up on the next allowed claim.

        Posts a ``status`` progress tick whenever it actually hands messages over, so the human
        sees the pickup in the task's live feed. Returns ``List[str]`` of message texts (the
        orchestrator's own ``_drain_pending`` renders these into the fold-in block). Never raises.
        """
        state = {"checked_at": 0.0}

        def _poll() -> List[str]:
            now = time.monotonic()
            if now - state["checked_at"] < interval:
                return []
            state["checked_at"] = now
            try:
                claimed = self._claim_task_messages(task_id)
                texts = self._message_texts(claimed)
            except Exception:  # noqa: BLE001 -- polling must never break the run
                return []
            # No "Picked up your message." tick here: the backend's claim already writes that line
            # to the task feed (the ``message_ack`` tick), and posting it again showed it twice.
            # What this side adds is the SEEN stamp: the texts go to the model's next step now.
            if texts:
                self._mark_messages_seen(task_id, claimed, "next_step")
            return texts

        return _poll

    def _build_run_control(self, task_id: str) -> RunControl:
        """The ``RunControl`` of THIS task's run, for a deep worker that holds a live session.

        Bound to one task id, so a message claimed through it belongs to this task and no other.
        ``claim_inputs`` claims straight from the task (atomic on the backend: each message is
        handed over once); the worker calls it only when it is about to write the result to its
        model. ``cancel_requested`` and ``pause_requested`` are read at the control poll cadence.
        Every callable is best-effort and never raises.
        """
        def claim_inputs() -> List[ControlMessage]:
            out: List[ControlMessage] = []
            for m in self._claim_task_messages(task_id):
                text = str(m.get("text") or "").strip()
                if text:
                    out.append(ControlMessage(id=str(m.get("message_id") or ""), text=text))
            return out

        def mark_seen(ids: List[str], where: str) -> None:
            mark = getattr(self._client, "mark_task_messages_seen", None)
            ids = [i for i in ids if i]
            if not callable(mark) or not ids:
                return
            try:
                mark(task_id, ids, where)
            except Exception:  # noqa: BLE001 -- a status stamp must never affect the run
                pass

        def pause_requested() -> bool:
            asked = getattr(self._client, "is_task_pause_requested", None)
            if not callable(asked):
                return False
            try:
                return bool(asked(task_id))
            except Exception:  # noqa: BLE001
                return False

        return RunControl(
            claim_inputs=claim_inputs, mark_seen=mark_seen,
            cancel_requested=self._build_cancel_check(task_id, RUN_CONTROL_POLL_SECONDS),
            pause_requested=pause_requested, poll_seconds=RUN_CONTROL_POLL_SECONDS,
            label=task_id)

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
        resolved a rep and pulled its profile, it builds this preamble and passes it here; failing
        that, it passes the task document's own optional ``rep_preamble`` field (see
        ``Poller._task_rep_preamble``). The executor threads it straight into
        ``Orchestrator.run(rep_preamble=...)``, which forwards it to a deep runner that accepts a
        per-call ``context_preamble``, and reuses it as the voice of the fold-back done report (see
        ``_compose_done_report``). When ``None`` (any existing caller, or no rep resolved and no
        field on the task), behaviour is exactly as before.
        """
        task_id = str(task.get("id") or task.get("task_id") or "")
        # An Autopilot pass task REPLACES the normal deep-run path entirely: the pass itself is the
        # work for this task (it scans opted-in quests and creates/suggests other tasks as a side
        # effect), so it is routed BEFORE any of the context/orchestrator machinery below runs.
        if _is_autopilot_pass(task):
            return self._execute_autopilot(task, task_id)
        text = self._task_text(task)
        goal_id = task.get("goal_id")
        quest_id = task.get("quest_id")
        # If only goal_id is set, we'll need quest_id to fetch the goal. Try to infer it from
        # task metadata or fetch it separately if the backend provides it.
        if goal_id and not quest_id:
            quest_id = task.get("_inferred_quest_id")
        # related_goal_id (optional, additive): a SPECIFIC goal inside a quest, distinct from
        # goal_id (which holds the QUEST's id on a quest-scoped task -- see _build_context_view's
        # docstring). Read defensively: not every backend/task sends it, and its absence must not
        # change anything below.
        related_goal_id = task.get("related_goal_id")
        # conv_id links this task back to the Quest AI conversation it was delegated from. When
        # present, the chat hears the task START and its verified CLOSING message (done / failed /
        # needs_you / decision), so the conversation doesn't go silent after the hand-off. Interim
        # milestones stay on the task's own progress feed (see ``_on_milestone``).
        conv_id = task.get("conv_id") or None
        # card_id (reserved, no behavior yet): forwarded on every conversation progress post so
        # a future backend can thread this task's posts under a per-idea thread.
        card_id = task.get("card_id") or None
        # model_hint: an optional per-task model/tier string stored by the consumer on the task
        # document (e.g. "opus", or any string the consumer's ModelRegistry understands).
        # Threaded into the orchestrator so the registry can honor it. None = default behavior.
        #
        # TWO field names, in priority order, because a consumer may separate the two things this
        # string used to conflate: "deep_run_model" pins the literal model for THIS deep run, while
        # "model" may carry only the consumer's own tier for its lightweight assistant calls. When a
        # consumer sets both, the deep-run field is the one that is about this execution, so it
        # wins; when it sets only "model" (every task written before the split, and every consumer
        # that never made it), behaviour is exactly as before. This mirrors the same precedence on
        # the in-process path, so a task runs on the same model whichever lane picks it up.
        model_hint: Optional[str] = task.get("deep_run_model") or task.get("model") or None
        # The environment's work-model configuration (allowed models, default, excluded cost tiers) is
        # applied to the literal deep-run pin, or to an unpinned run. A QAR-call tier word is not a work
        # model and passes through unchanged. A refused run fails here, before any work starts.
        try:
            model_hint = self._work_model_hint(task, model_hint)
        except WorkModelUnavailable as e:
            self._report_progress(task_id, "error", text=str(e))
            self._safe_report_failed(task_id, str(e))
            return ExecutionOutcome(task_id, "failed", str(e))
        # resume_session_id (optional, additive): the Claude session a PREVIOUS run on this same
        # thread left behind, handed back by the backend so this run opens holding what that run
        # read and decided instead of rediscovering it. Absent on a first run, on a backend that
        # does not track sessions, and on any older task document -- all of which mean a cold start,
        # exactly as every run behaved before this existed. Resume is best effort throughout: a
        # session Claude Code no longer holds degrades to a cold start inside the deep runner and
        # the run still completes (see ``SubprocessGoalRunner.run_goal``).
        resume_session_id: Optional[str] = str(task.get("resume_session_id") or "").strip() or None
        resume_session_id = across_pass_resume(task, resume_session_id)
        # Which task a receipt recorded at the end of this run belongs to (``_record_dispositions``).
        self._receipt_task_id = str(task.get("task_id") or task.get("id") or "")
        if not text:
            self._report_progress(task_id, "error", text="task had no instruction text to run")
            self._safe_report_failed(task_id, "task had no text/description to run")
            self._post_conv(conv_id, "I couldn't run this: the task had no instruction text.",
                            kind="failed", task_id=task_id, card_id=card_id)
            return ExecutionOutcome(task_id, "failed", "task had no text/description")

        # Announce the start: a live progress event on the task (the task-detail stream) AND, when a
        # conv_id links this task to a chat, a started message into that chat.
        self._report_progress(task_id, "started", text=f"Started working on this: {text}")
        self._post_conv(conv_id, f"Started working on this: {text}", kind="started",
                        task_id=task_id, card_id=card_id)
        # A card store at its limit says so in the lane's log; nobody watches a lane's log, so the first
        # task to start afterwards carries the same two lines on its live feed (once per notice).
        from ..adapters.card_limit import take_task_notes
        for store_note in take_task_notes():
            self._report_progress(task_id, "status", text=store_note)

        # ONE initial drain: claim any messages the human sent while this task sat queued (e.g. a
        # steer sent right after delegating, before a background lane picked it up) and fold them
        # into the FIRST prompt the orchestrator sees, so they land on this run's opening attempt
        # instead of waiting for a later drain point. The backend's claim endpoint already posts
        # its own ``message_ack`` progress tick when it hands these over; this status tick is
        # separate -- it names that the FOLD happened, for the live feed.
        _initial_claimed = self._claim_task_messages(task_id)
        _initial_texts = self._message_texts(_initial_claimed)
        if _initial_texts:
            self._mark_messages_seen(task_id, _initial_claimed, "planner")
            # Reuse the orchestrator's own rendering (a one-shot callable over the already-claimed
            # texts) so this fold-in reads IDENTICAL to one folded in mid-run by the orchestrator's
            # ``_drain_pending`` (deep retry loop, answer-improve loop, planner loop) -- one voice
            # for "here is what the user said since you started", regardless of which drain point
            # picked it up.
            _folded_block = Orchestrator._drain_pending(lambda: _initial_texts)
            if _folded_block:
                text = f"{text}\n\n{_folded_block}"
            _n = len(_initial_texts)
            self._report_progress(
                task_id, "status",
                text=("Picked up your message that arrived before this started."
                      if _n == 1 else
                      f"Picked up your {_n} messages that arrived before this started."))

        # Fetch goal + quest context + conversation history from Quest API if available, and build
        # a context_view for the orchestrator so the deep agent knows what goal/quest it's working on
        # and the prior conversation that led to the task.
        # A batch the autopilot pass composed already carries its own context-updates block, with
        # its own refs; collecting again here would hand the run the same notes twice under two
        # numberings. Keyed on the task's routing metadata (task_kind), not on whether the text
        # happens to contain the block's marker string: a task whose text merely QUOTES a prior
        # brief (someone pasting a previous run's output into chat, which the backend files as its
        # own task) is not autopilot-composed and must still get fresh context collected.
        _earlier = earlier_run_block(task, resume_session_id)
        if _earlier:
            text = f"{text}\n\n{_earlier}"
        composed_by_autopilot = _autopilot_composed_text(task)
        if (goal_id or quest_id) and self._mailing_quest_id(goal_id, quest_id):
            text = f"{text}\n\n{AUTOMATIC_DELIVERY_NOTE}"
        context_view = self._build_context_view(
            goal_id, quest_id, conv_id, rep_id=task.get("assignee_rep_id"),
            related_goal_id=related_goal_id,
            with_updates=not composed_by_autopilot)

        # Route all orchestrator events (except raw streaming partials) to the task's live progress
        # stream so the task-detail SSE shows step-by-step what the AI is doing (plan, read, replan,
        # tokens). Milestones stay on that stream only; the chat gets start and close.
        sink = _TaskProgressSink(
            task_id,
            self._report_progress,
            on_milestone=lambda ev: self._on_milestone(task_id, conv_id, ev,
                                                       card_id=card_id),
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
        #
        # The task id travels the same way: a direct tool's ToolContext is built from this meta,
        # and docs/tools.md promises every command tool QAR_TOOL_TASK_ID. Without it here a tool
        # that needs to know which task it serves (who asked, what they replied) got nothing.
        context_meta: Optional[Dict[str, Any]] = {"goal_id": goal_id} if goal_id else None
        if task_id:
            context_meta = {**(context_meta or {}), "task_id": task_id}
        # Who the work is for (the task's creator), so every process this run spawns carries it
        # as QAR_REQUESTER_USER_ID (see core/requester.py).
        requester = requester_from_task(task)
        if requester:
            context_meta = {**(context_meta or {}), "requester_user_id": requester}
        # Operations this kind of run owes on every pass (see ``run_operations``): their how-to is
        # shown first in the brief, whatever the brief's wording.
        run_pins = pinned_operations(task)
        if run_pins:
            context_meta = {**(context_meta or {}), "tool_pins": list(run_pins)}
        # The quest fence for every scoped context arm (see ``task_scope_tags``).
        run_scope_tags = task_scope_tags(task)
        if run_scope_tags:
            context_meta = {**(context_meta or {}), "scope_tags": run_scope_tags}

        # Cooperative mid-run cancellation: a THROTTLED check (see _build_cancel_check) threaded
        # into the orchestrator so a human hitting "stop" while this task is in_progress can abort
        # the run cleanly at its next loop boundary instead of running to completion regardless.
        cancel_check = self._build_cancel_check(task_id)

        # Explicit mid-run steering channel: a THROTTLED callable (see _build_pending_inputs) that
        # claims any NEW messages the human sends while this task is in_progress, so the deep retry
        # loop / answer-improve loop / outer planner loop can fold them in at their next drain
        # point. Passed EXPLICITLY rather than left as None -- see _build_pending_inputs' docstring
        # for why relying on Orchestrator.run's own input_inbox auto-wiring would silently miss a
        # goal-only personal task (its _conv_key resolution only resolves via quest_id today).
        pending_inputs = self._build_pending_inputs(task_id)

        # Per-task working directory: when this task's goal/quest resolves through the configured
        # quest_folder_map, the deep run starts in THAT folder (its synced quest folder) for this
        # run only, instead of the deep-runner's configured global working_dir. Applies to every
        # task, not just autopilot-created ones (see quest_autopilot_design.md's execution-
        # environment section).
        working_dir_override = self._resolve_working_dir(goal_id, quest_id)

        # When this run began, so a usage limit recorded DURING it can be told apart from one some
        # other concurrent run hit (see ``_usage_limit_behind``).
        run_started = time.time()
        # THE RUN'S CONTROL CHANNEL, bound to this task: a deep worker with a live session takes
        # steering messages and stop/pause requests through it. Only passed to a brain whose
        # ``run`` accepts it (an older brain, or a test double, is called exactly as before).
        control_kwargs: Dict[str, Any] = {}
        try:
            if "run_control" in inspect.signature(self._orch.run).parameters:
                control_kwargs["run_control"] = self._build_run_control(task_id)
        except (TypeError, ValueError):
            pass
        def run_once(brief: str, resume: Optional[str],
                     extra: Optional[Dict[str, Any]] = None) -> OrchestratorResult:
            return self._orch.run(
                brief, quest_id=quest_id, context_view=context_view, mode=Mode.BACKGROUND,
                sink=sink, model_hint=model_hint, rep_preamble=rep_preamble,
                context_meta=context_meta, working_dir_override=working_dir_override,
                conv_id=conv_id, conv_scope=conv_scope or None, cancel_check=cancel_check,
                pending_inputs=pending_inputs,
                # ACROSS-RUN CONTINUITY: the thread's previous session, if the backend sent one.
                # It rides the SAME plumbing as the within-run turn-budget continuation (the deep
                # loop's ``resume_session``, forwarded to a runner that accepts it), so there is one
                # resume path, not two. None = today's cold start, byte-for-byte.
                resume_session_id=resume,
                # This text is a QUEUED TASK's brief, not something the human typed at us this
                # turn, so the user-veto gate must not read it. See the no-action gate in
                # Orchestrator.run: a work brief full of legitimate instructions ("stop asking",
                # "do not silently drop them") otherwise degrades the run to an answer with no
                # tools, and it reports as though it had looked.
                message_is_user_turn=False, **control_kwargs, **(extra or {}))

        decisions_before = self._open_decision_ids(quest_id)
        try:
            result: OrchestratorResult = run_once(
                text, resume_session_id,
                self._direct_deep_extra(self._task_text(task) or text,
                                        _run_accepts(self._orch.run, "deep_goal")))
            # AUTOMATIC FOLLOW-UP RUNS: while the ask is clearly unfinished and nothing waits on a
            # person, run again for the remaining items (see ``runner/auto_followup.py``).
            result = self._follow_up_while_incomplete(
                result, task, task_id, run_once, ask=self._task_text(task) or text,
                quest_id=quest_id, run_started=run_started, resume_session_id=resume_session_id,
                supports_deep_goal=_run_accepts(self._orch.run, "deep_goal"),
                decisions_before=decisions_before)
        except Exception as e:  # noqa: BLE001 — brain failure -> failed report, never crash poller
            # A run that raises BECAUSE it was interrupted must not be reported as failed: check
            # (unthrottled, this is the terminal path) whether the task was cancelled meanwhile.
            if self._is_task_cancelled(task_id):
                return self._quiet_cancelled(task_id)
            # Claude Code refused on its usage limit (a planner or answer call raised it, or a
            # concurrent run recorded it while this one was failing): a wait, not a failure.
            limit = usage_limit.from_exception(e) or usage_limit.seen_since(run_started)
            if limit is not None:
                return self._pause_for_usage_limit(task, task_id, limit, conv_id=conv_id,
                                                   card_id=card_id, session_id=resume_session_id)
            # The model provider is overloaded or down: the same kind of wait, not a failure.
            outage = provider_outage.from_exception(e)
            if outage is not None:
                return self._pause_for_provider_outage(task, task_id, outage, conv_id=conv_id,
                                                       card_id=card_id, session_id=resume_session_id)
            msg = f"orchestrator error: {type(e).__name__}: {e}"
            self._report_progress(task_id, "error", text=msg)
            self._safe_report_failed(task_id, msg)
            self._post_conv(conv_id, f"I hit an error working on this: {msg}", kind="failed",
                            task_id=task_id, card_id=card_id)
            return ExecutionOutcome(task_id, "failed", msg)

        # A person PAUSED the run (the worker reports ``paused`` with its session): the task goes
        # back to the queue holding that session, to be resumed by a person. Checked before the
        # usage-limit and outage reads, which treat a stopped run as theirs, and before ``report``,
        # which would read the stopped run as a cancel and leave the task in_progress.
        paused_results = self.paused_deep_results(result)
        if paused_results:
            paused_session = ((paused_results[-1].session_id or "").strip()
                              or terminal_session_id(result) or resume_session_id)
            return self.hand_back_paused_task(task, task_id, result, session_id=paused_session)
        # The run had the person's material in front of it, so the next look starts after it.
        # After the run and not before: an orchestrator that raised before doing anything has not
        # delivered it (see ``ContextUpdates.mark_seen``).
        # A run that did not finish because Claude Code hit its usage limit goes back in the queue
        # to resume at the reset, holding its session, instead of being reported done or failed.
        limit = self._usage_limit_behind(result, run_started)
        if limit is not None:
            return self._pause_for_usage_limit(
                task, task_id, limit, conv_id=conv_id, card_id=card_id,
                session_id=terminal_session_id(result) or resume_session_id)
        outage = self._provider_outage_behind(result, run_started)
        if outage is not None:
            return self._pause_for_provider_outage(
                task, task_id, outage, conv_id=conv_id, card_id=card_id,
                session_id=terminal_session_id(result) or resume_session_id)
        if self._context_bundle is not None:
            self._safe(self._context_bundle.mark_seen)
        return self.report(task_id, result, conv_id, request_text=text,
                           rep_preamble=rep_preamble,
                           card_id=(task.get("card_id") or None),
                           autopilot_composed=composed_by_autopilot)

    # --- per-task working directory (quest_folder_map) ------------------------

    def _work_model_hint(self, task: Dict[str, Any], model_hint: Optional[str]) -> Optional[str]:
        """The model hint for this run after the environment's work-model configuration is applied.

        Reads the effective view for the task's environment (``env_id`` on the task, else this runner's
        own). A literal pin the environment does not allow falls back to its default. An unpinned run
        takes the configured default only when the environment has one; otherwise it stays unpinned.
        If the read fails, nothing is restricted, exactly as before this existed, and the failure is
        logged. Raises ``WorkModelUnavailable`` when the environment allows no Claude model this runner
        can run.
        """
        if model_hint and is_tier_word(model_hint):
            return model_hint
        requested = task.get("deep_run_model") or None
        requested = requested or (None if is_tier_word(task.get("model")) else task.get("model") or None)
        fetch = getattr(self._client, "get_work_model_config", None)
        effective = None
        configured = False
        if fetch is not None:
            try:
                payload = fetch(team_id=task.get("team_id") or None,
                                env_id=task.get("env_id") or None) or {}
                effective = payload.get("effective")
                configured = (payload.get("source") or "default") != "default"
            except Exception as e:  # noqa: BLE001 (a config read must never break a run)
                log.warning("work-model config unavailable for task %s, using the Claude default: %s",
                            task.get("id") or task.get("task_id"), e)
        choice = apply_work_model(requested, effective or default_effective(), configured=configured)
        if choice.note:
            log.info("task %s: %s", task.get("id") or task.get("task_id"), choice.note)
        return choice.model

    def _resolve_working_dir(self, goal_id: Optional[str],
                             quest_id: Optional[str]) -> Optional[str]:
        """Resolve this task's deep-run working-directory override from ``quest_folder_map``.

        Mirrors the poller's own ``_quest_folder_for`` precedence (goal_id first, then quest_id) so
        a task tied to a synced quest folder starts the deep agent THERE instead of the deep-
        runner's configured global working_dir. Returns None (fallback = the global default)
        when unconfigured or the task's ids aren't in the map."""
        if not self._quest_folder_map:
            return None
        qid = goal_id or quest_id
        if not qid:
            return None
        return self._quest_folder_map.get(str(qid)) or None

    # --- autopilot pass routing (handler == "autopilot") ----------------------

    def _execute_autopilot(self, task: Dict[str, Any], task_id: str) -> ExecutionOutcome:
        """Run the wired ``AutopilotPass`` for a ``handler == "autopilot"`` task and report its
        outcome through the normal progress/report calls (a plain 'done' unless the pass itself
        cannot run at all). ``AutopilotPass.run`` already isolates each quest's own failures into
        the result (see ``runner.autopilot``), so nothing here needs a per-quest try/except; this
        method's own try/except only guards against the pass failing to run at all."""
        self._report_progress(task_id, "started", text="Started an autopilot pass.")
        if self._autopilot is None:
            msg = "autopilot is not configured on this runner (no AutopilotPass wired)"
            self._report_progress(task_id, "error", text=msg)
            self._safe_report_failed(task_id, msg)
            return ExecutionOutcome(task_id, "failed", msg)
        try:
            result = self._autopilot.run(task)
            summary = result.summary_text()
            self._report_progress(task_id, "done", text="Done.", output=summary)
            self._safe(lambda: self._client.report_done(task_id, summary))
            return ExecutionOutcome(task_id, "done", summary)
        except Exception as e:  # noqa: BLE001 — the pass itself must never crash the poller
            msg = f"autopilot pass error: {type(e).__name__}: {e}"
            self._report_progress(task_id, "error", text=msg)
            self._safe_report_failed(task_id, msg)
            return ExecutionOutcome(task_id, "failed", msg)
