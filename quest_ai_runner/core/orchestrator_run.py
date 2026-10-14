"""The per-request loop (``run``) and its streaming wrapper.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime

from .sufficiency import AbridgedTurnState, collect_abridged_items, render_abridged_notice
from .planner_prompt import (
    BRAINSTORM_CLARIFY_ACK_PREFIX,
    BRAINSTORM_HELD_WORK_ACK_NOTE,
    BRAINSTORM_NO_ACTION_ACK_NOTE,
    HOLD_OFF_HELD_WORK_ACK_NOTE,
    HOLD_OFF_NO_ACTION_ACK_NOTE,
    NOT_QUEUED_NOTE,
    READ_BUDGET_WRAPUP_HONESTY_NOTE,
    _CARD_THREAD_PLANNER_BLOCK_TEMPLATE,
)
from .context_doctrine import CACHED_HINT_GATE, CARD_LIFECYCLE_GATE
from .card_thread import (
    CardThreadContext,
    CardThreadDecision,
    merge_candidates,
    parse_card_thread,
    render_thread_hint,
)
from .adapters import (
    EVENT_CARD_THREAD,
    EVENT_CONTEXT,
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_EXPLANATION,
    EVENT_INTENT,
    EVENT_MILESTONE,
    EVENT_MODE_SIGNAL,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    EVENT_TOKENS,
    EVENT_UNDERSTANDING,
    Mode,
    PlanDecision,
    ProgressEvent,
    ProgressSink,
    ScopedThreadPoolExecutor as ThreadPoolExecutor,
)
from .guard import ExecutionRecord
from .overseer import OverseerSignal
from .tools import ToolContext
from .planner_context import (
    _prior_escalation_lines,
    _recent_conversation_turns,
    declined_proposals_block,
    gathered_content,
)
from .recent_context import (
    build_item_usage_hint,
    filter_relevant,
    quest_scope_key,
    render_recent_cards,
)
from .answer_explanation import is_eligible, trace_from_result
from .orchestrator_decisions import (
    _clarify_question_text,
    _truncate_goal,
    message_forbids_new_task,
    restates_meaningfully,
    user_ordered_work,
)
from .orchestrator_capabilities import (
    context_assembly_timeout_seconds,
    guidance_selection_timeout_seconds,
    planner_full_view_start,
    planner_note_observation,
    read_spec_key,
    record_context_assembly_timeout,
    repeated_read_observation,
    split_repeated_reads,
)
from .orchestrator_turn import Narrator, TurnCardCache, _Emitter
from .orchestrator_text import (
    _answer_describes_unexecuted_work,
    _future_context_for_display,
    _strip_future_context,
    unconfirmed_no_change_text,
)
from .orchestrator_events import (
    _is_orchestrator_command,
    _project_card_metadata_for_event,
    _project_sources_for_event,
    grounding_context_layer,
)
from .orchestrator_constants import RECIPE_SKIP_GOAL_CONTEXT, log, result_landed_work
from .orchestrator_decide_tool import (
    DEFERRED_RUNNER_KEY,
    OWN_ESCALATION_KEPT_STATUS,
    USER_INTENT_HOLD_OFF,
    own_escalation_adds_nothing,
)
from .orchestrator_types import OrchestratorResult


class RunMixin:
    """The per-request loop (``run``) and its streaming wrapper."""

    # --- the loop ------------------------------------------------------------

    def run(self, user_message: str, *, transcript: str = "", context_view: str = "",
            quest_id: Optional[str] = None,
            mode: Mode = Mode.LIVE,
            sink: Optional[ProgressSink] = None,
            background_sink: Optional[ProgressSink] = None,
            detach_check: Optional[Callable[[], bool]] = None,
            model_hint: Optional[str] = None,
            attachments: Optional[List[Dict[str, Any]]] = None,
            context_meta: Optional[Dict[str, Any]] = None,
            rep_preamble: Optional[str] = None,
            working_dir_override: Optional[str] = None,
            pending_inputs: Optional[Callable[[], List[str]]] = None,
            conv_id: Optional[str] = None,
            conv_scope: Optional[Dict[str, Any]] = None,
            prior_escalations: Optional[List[Dict[str, Any]]] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
            card_thread: Optional[Any] = None,
            prior_narration: Optional[List[str]] = None,
            anticipated_id: Optional[str] = None,
            message_is_user_turn: bool = True,
            resume_session_id: Optional[str] = None,
            now: Optional[str] = None) -> OrchestratorResult:
        """Run the bounded loop for one request and return a terminal OrchestratorResult.

        Streaming/event interface (both lanes use the SAME emissions; the SINK decides policy):

        * ``mode``        — ``Mode.LIVE`` (attended; stream everything) or ``Mode.BACKGROUND``
                            (sent-off; surface only result/decision/milestone). Default LIVE.
        * ``sink``        — a ``ProgressSink`` the orchestrator routes every internal event
                            through. For LIVE pass a ``StreamSink``; for BACKGROUND a
                            ``MilestoneSink``. If ``None``, only the legacy ``status`` callback
                            fires (a plain non-streaming run, unchanged for old callers).
        * ``background_sink`` + ``detach_check`` — the LIVE↔BACKGROUND handoff. If a LIVE run is
                            abandoned (``detach_check()`` returns True, e.g. the consumer
                            disconnected), the run CONTINUES to completion and the REST of its
                            events (incl. the final result) are delivered via ``background_sink``
                            (a ``MilestoneSink``) instead of the dropped live stream.
        * ``model_hint``  — optional opaque model/tier string carried by the task or caller
                            (e.g. from ``task.model``). When set, it overrides the planner's own
                            tier choice and the compile-time default for answer and deep steps.
                            The consumer's ``ModelProvider`` and ``ModelRegistry`` interpret the
                            string (a tier name or a model id). Unknown values degrade gracefully
                            to the registry's default. Absent/None means exactly today's behavior.
        * ``attachments`` — optional list of in-memory attachment items (chat file uploads AND/OR
                            panel context-docs, the SAME generic shape; see
                            ``core.attachments.prepare_attachments``). Each is a dict
                            ``{filename, mime_type, data: bytes, kind: "image"|"file"}``. The
                            runner OWNS multimodal (the text provider does not): images go NATIVELY
                            to the answer when the answering model/provider is vision-capable, else
                            they are described to text; non-image files are text-extracted. Their
                            text descriptions/inventory join the PLANNER context (so planning is
                            grounded in them), and native image blocks ride the final ANSWER
                            message. Absent/None means exactly today's behavior.
        * ``rep_preamble`` — optional PER-RUN context preamble for the deep step only (e.g. an AI
                            rep's persona + learned corrections, so a deep run executes AS that
                            rep). The brain stays ignorant of its content — it forwards the string
                            to a deep runner that accepts ``context_preamble`` and leaves older
                            runners untouched. Absent/None means exactly today's behavior.
        * ``working_dir_override`` — optional PER-RUN working-directory override for the deep step
                            only (e.g. a quest's synced folder, resolved by the caller from its own
                            quest-folder map). Forwarded to a deep runner that accepts a
                            ``working_dir`` kwarg (see ``core.goal_runner.SubprocessGoalRunner``);
                            older runners are untouched. Absent/None means the deep runner's own
                            configured global working directory applies, exactly as before.

        * ``conv_id``     — optional id of the CURRENT conversation. When set AND a
                            ``conversation_store`` is wired, Step 1 (User Input Understanding) may
                            pull a relevant slice of that conversation to resolve a short/anaphoric
                            message into a self-contained goal condition before context selection.
                            Absent/None (or no store) means Step 1 is a no-op (zero latency).
        * ``conv_scope``  — optional scope dict ({user_id, team_ids, since, participant_id, ...})
                            for finding RELATED past conversations when the current slice is not
                            enough to resolve the message. Interpreted by the ConversationStore.
        * ``prior_escalations`` — optional caller-supplied history of EARLIER turns in this SAME
                            conversation that already escalated (to deep execution or to a human),
                            each e.g. ``{"kind": "deep"|"human", "exit_reason": "...", "outcome":
                            "..."}``. The brain has no persistent storage of its own (core stays
                            domain-free; see ``adapters.ConversationStore`` for the same pattern), so
                            a caller that wants the overseer to see this history across turns passes
                            it forward here (e.g. sourced from the ``exit_reason``/``overseer_signals``
                            of previous ``OrchestratorResult``s it already persisted). Feeds the
                            overseer digest's PRIOR ESCALATIONS THIS CONVERSATION section. Absent/None
                            means "none yet" (today's behavior, byte-for-byte, when overseer is off).
        * ``cancel_check`` -- optional zero-arg callable a caller polls for COOPERATIVE mid-run
                            cancellation (e.g. a runner backed by a task store where a human can
                            cancel a background task while it is in progress). Checked at natural
                            loop boundaries: before starting each new plan/gather/replan step, before
                            each deep-goal retry attempt, and once more after deep execution
                            finishes. When it returns True the run stops cleanly and returns an
                            ``OrchestratorResult`` with ``kind="cancelled"`` (no ``text``/
                            ``deep_results``/``decision_id`` to act on) instead of the normal
                            answer/deep/confirm outcome. It is never called more than the loop's own
                            natural cadence, so a cheap/throttled implementation is fine. Absent/None
                            means exactly today's behavior (a run can never be cancelled mid-flight).
        * ``card_thread`` -- optional PER-IDEA THREADING context (a ``CardThreadContext`` or the
                            equivalent dict; see ``core.card_thread``). Only consulted when the
                            consumer opted in with ``cfg.card_thread_enabled``. It names the card
                            (idea) the conversation is CURRENTLY on plus any candidates the consumer
                            always wants offered; the orchestrator adds the cards its own retrieval
                            already scored this turn (the free PRIOR) and asks the planner, on the
                            call it already makes, which card THIS message belongs to. The resolved
                            assignment comes back as ``OrchestratorResult.card_thread`` and
                            EVENT_CARD_THREAD. The orchestrator persists nothing: the consumer owns
                            the card store and stamps its own messages. Absent/None (or the flag off)
                            means exactly today's behavior.
        * ``prior_narration`` -- optional list of narration lines (most recent last) already spoken
                            aloud to this user in EARLIER turns of this same conversation, when
                            narration is on (``cfg.narrate``). The brain has no persistent storage of
                            its own (same reasoning as ``prior_escalations`` above), so a caller that
                            narrates every turn out loud (e.g. a voice consumer) and wants the ack to
                            stop reopening with its own recent generic "let me look into that" shape
                            passes the last few lines it actually delivered to audio back in here; a
                            reasonable source is ``OrchestratorResult.narration_said`` from the
                            PREVIOUS turn(s), capped by the caller to a handful of lines. Feeds the
                            narrator's repeat-detector and its ack/relay prompts (see ``Narrator``).
                            Absent/None means "no cross-turn memory" (today's behavior).
        * ``resume_session_id`` -- optional id of a deep-worker session an EARLIER run (a previous
                            run of the same task/thread, hours or days ago) left behind. When set,
                            the FIRST deep attempt of this turn resumes that session instead of
                            opening a fresh one, so it starts holding what that run read and
                            decided. It rides the same plumbing as the within-run turn-budget
                            continuation (see ``_run_deep``) rather than a second resume path, and
                            it is consumed ONCE per turn: a later deep call in the same turn starts
                            cold, since the thread has one session to continue, not several.
                            Best effort by contract: a runner that cannot resume ignores it, and a
                            session the worker no longer holds degrades to a cold start rather than
                            failing the run. Absent/None means exactly today's behavior.
        * ``now``          -- optional ISO date/datetime string, the CALLER's notion of "now". Fed
                            into goal-condition/constraint derivation (see ``_derive_goal_condition``)
                            so a relative date in the message ("Wednesday", "last week") resolves
                            against the caller's real clock instead of the server's. Absent/None
                            falls back to the system clock (UTC) -- relative-date resolution still
                            works, just against this process's clock.

        ``run`` still works with NO event args (back-compat: same signature callers used before
        plus keyword-only extras), returning the terminal ``OrchestratorResult``.
        """
        user_message = (user_message or "").strip()
        gathered: List[Dict[str, Any]] = []
        # "EXPLAIN HOW I GOT THIS" (cfg.explain_answer): the ALREADY-PROJECTED cards and sources
        # this turn actually assembled, captured at the same place EVENT_CONTEXT is emitted. Kept
        # here rather than re-derived in finish() for two reasons: what the panel shows can never
        # disagree with what the context event showed, and nothing UNPROJECTED can reach the panel
        # by some new route later (the projections deliberately strip item text and keep only
        # labels, counts and path-like items). Stays empty when the feature is off or no context
        # was assembled.
        explain_cards: List[Dict[str, Any]] = []
        explain_sources: List[Dict[str, Any]] = []
        # Per-run record of every minimal-intervention OVERSEER consultation (both hook points), and
        # the count both hooks share against cfg.overseer_max_signals. Stamped onto the result in
        # finish(). Stays empty (and overseer_signals None) whenever the overseer is off.
        overseer_signals: List[Dict[str, Any]] = []
        # NON-BLOCKING overseer plumbing (Fix 1). The overseer's provider call runs in a BACKGROUND
        # thread (same idiom as context assembly) so consulting it NEVER stalls the user-facing loop.
        #  - ``_oversee_executor``: a single per-run worker, created lazily on first consult and torn
        #    down (wait=False) in finish(); a still-running consult finishes on its own (bounded
        #    network call) and is never joined synchronously.
        #  - ``pending_oversee``: the in-flight hook-A consult submitted on a prior step, polled at
        #    the top of the next step; kept until it resolves (re-checked each step, never dropped).
        #  - ``overseer_submitted``: submissions counted against cfg.overseer_max_signals (a
        #    submitted-but-uncollected consult still counts, so the shared cap holds across both hooks).
        _oversee_executor: Optional[ThreadPoolExecutor] = None
        pending_oversee: Optional[Dict[str, Any]] = None
        overseer_submitted = 0
        # Previous step's plan (Fix 12's repeat-plan gate signal), captured just before each step's
        # planner call overwrites ``plan``. None until the first plan exists.
        _prev_plan_for_gate: Optional[PlanDecision] = None
        # Fix 7: the caller-supplied cross-turn escalation history, pre-formatted once for the
        # digest (see ``_prior_escalation_lines``). Computed once; reused by both hooks this run.
        _prior_escalation_digest_lines = _prior_escalation_lines(prior_escalations)
        # Any DECLINED proposal in that same history is a fact the PLANNER itself must hold, not
        # only the (off-by-default) overseer, so it goes into ``context_view`` right here, the same
        # way the STAGE 1 "UNDERSTOOD REQUEST" block is prepended further below. Empty when nothing
        # in ``prior_escalations`` was refused, so this is byte-for-byte a no-op for every caller
        # that passes none.
        declined_block = declined_proposals_block(prior_escalations)
        if declined_block:
            context_view = (declined_block + "\n\n" + context_view
                             if context_view else declined_block)
        # Run-local durable EXECUTION FACTS (the broken-promise guard's evidence): each deep
        # subtask that actually executes records its outcome (success/failure) here, threaded like
        # ``gathered``. Attached to the OrchestratorResult in finish().
        exec_record = ExecutionRecord()
        started = time.monotonic()
        cfg = self.cfg

        # ACROSS-RUN SESSION CONTINUITY, consumed ONCE. ``resume_session_id`` names the session a
        # previous RUN of this thread left behind; a turn can reach deep execution by more than one
        # route (the main deep path, a deferred hand-off, an escalation from the answer turn), and
        # only the first of them may pick that session up. A thread has one session to continue, so
        # handing the same id to a second deep call would put two workers in one transcript.
        pending_resume: Dict[str, Optional[str]] = {
            "id": (resume_session_id or "").strip() or None}

        def take_resume_session() -> Optional[str]:
            sid = pending_resume["id"]
            pending_resume["id"] = None
            return sid

        # --- EXECUTION MODE (brainstorm latch, consumer-owned) --------------------------------
        # ``brainstorm_active`` gates every path that could ACT this turn (deep, confirm, and the
        # nets that can only ADD execution). It starts from the consumer-supplied config. Two
        # things can move it within the turn: the dedicated release judgment below (the ONLY way a
        # held latch opens; fail-safe HOLD), and a planner "enter_brainstorm" signal, which engages
        # the gating from an unlatched turn. Any other execution_mode value behaves as "normal".
        # ``mode_signal_detected`` is the first (and only) signal captured this turn, surfaced via
        # EVENT_MODE_SIGNAL + OrchestratorResult.mode_signal; the orchestrator persists nothing.
        # THE USER'S OWN VETO. When the message itself says no task may be opened ("don't create a
        # task", "just answer me here", "hold off"), this turn runs under the SAME no-action gate
        # as brainstorm mode: the planner may still choose "deep", and it is degraded to "answer"
        # at the two structural gates below, exactly as a latched turn is. Reusing that gate rather
        # than adding a new bypass is deliberate -- the invariant "a no-action turn executes nothing
        # and creates no decision-request" stays in ONE place. It is turn-scoped (re-derived from
        # each message, never latched) and immutable within the turn, so neither the release judge
        # nor a planner mode signal can clear a veto the user just gave.
        # ...but ONLY for a message the human actually typed this turn. A QUEUED TASK being
        # executed is not a conversational turn: the decision to act was already taken when the
        # task was created (a veto in chat prevents the task from existing at all), and the brief
        # the executor passes here is machine-composed -- goal briefs, carried-over notes, a
        # month's review lines. Running the veto heuristic over that text made ordinary
        # instructions inside the work ("stop asking", "do not silently drop them") read as the
        # user forbidding action, and the run degraded to a text-only answer with NO TOOLS. The
        # visible symptom was a report claiming it had looked at files and found nothing, when it
        # never had a filesystem at all -- a silent false-completion, which is the exact failure
        # this file is otherwise full of guards against.
        hold_off_active = message_is_user_turn and message_forbids_new_task(user_message)
        # The same reasoning covers the ESCALATION NETS' own hold-off check (the "is this a request
        # for a change?" fallbacks below): on a queued task's brief it would read quoted output of
        # earlier runs as the human holding off. Only a message typed this turn can hold off.
        nets_honor_hold_off = message_is_user_turn
        # The planner's structured verdict on what the CURRENT message asks of the assistant
        # (``PlanDecision.user_intent``): the latest one any planner step this turn gave. The
        # escalation nets below honor it in place of the retired regex net over the message.
        # None = no planner step gave a usable verdict (planner error, a path with no planner
        # call): the nets then ask ``judge_execution_directive`` instead.
        turn_user_intent: Optional[str] = None
        if hold_off_active:
            log.info("User message forbids opening a task this turn; running under the no-action "
                     "gate (planner deep/confirm will degrade to answer).")
        brainstorm_active = (cfg.execution_mode == "brainstorm") or hold_off_active
        mode_signal_detected: Optional[str] = None
        # --- PER-IDEA THREADING (the idea IS the card; opt-in via cfg.card_thread_enabled) -------
        # ``card_thread_ctx`` is the consumer's per-turn thread context (active card + the cards it
        # always wants offered). ``card_thread_decision`` is what the planner resolved this turn,
        # stamped onto the result in finish(). Both stay None when the flag is off, so the run is
        # byte-identical to a build without the feature.
        card_thread_ctx: Optional[CardThreadContext] = (
            CardThreadContext.coerce(card_thread) if cfg.card_thread_enabled else None)
        card_thread_decision: Optional[CardThreadDecision] = None
        card_thread_block: str = ""
        card_thread_candidate_ids: List[str] = []
        # The action ("deep"/"confirm"/"clarify") the turn chose that the brainstorm latch degraded
        # to "answer", if any. Feeds the no-action acknowledgment steer on the answer path so the
        # reply can say the work was held rather than silently dropping it.
        brainstorm_suppressed_action: Optional[str] = None
        # Direct tool calls (core/tools.py): whether any ran this turn, and the (name, args)
        # signatures that SUCCEEDED, so a re-plan that repeats a done call cannot run it twice.
        tool_calls_ran = False
        tool_ok_signatures: set = set()
        # The question a suppressed "clarify" (planner) or a suppressed understanding-clarify
        # (stage 1) wanted to park as a decision-request. While the latch is held nothing may
        # escalate, so the question rides into the REPLY instead (see BRAINSTORM_CLARIFY_ACK_PREFIX).
        brainstorm_clarify_question: Optional[str] = None
        # True when the release judge LIFTED the hold on this very turn. The user just told us to
        # stop holding back and act on what was discussed, so the turn is a directive by definition:
        # it must not end as one more proposal (that is what the whole conversation already was).
        brainstorm_released_this_turn = False

        # Reset per-turn token counters on the provider (if it tracks them).
        try:
            if hasattr(self.provider, "tokens_in"):
                self.provider.tokens_in = 0
                self.provider.tokens_out = 0
        except Exception:  # noqa: BLE001
            pass

        # If a handoff is configured, route events through a FanoutSink that flips live->bg on detach.
        # We need to set up the emitter early so instant_ack can use it.
        on_detach = None
        active_sink = sink
        if background_sink is not None and detach_check is not None:
            from .adapters import FanoutSink
            fan = FanoutSink(live=sink, background=background_sink) if sink is not None else \
                FanoutSink(live=background_sink, background=background_sink)
            active_sink = fan
            on_detach = fan.detach
        emit = _Emitter(active_sink, mode, self._status, detach_check=detach_check, on_detach=on_detach)

        # --- RECIPE FAST PATH (core/recipes.py): a known operation runs BEFORE any context search ---
        # The lookup is lexical and model-free, so it costs microseconds ahead of everything else in
        # the turn; only a strong nominee pays for one small fast-tier call (same operation? what
        # arguments?). A hit returns the tool's receipt now; a miss, a failure, or a near-miss
        # falls through to the unchanged run below.
        if (self.recipes is not None and self.tools is not None and cfg.recipe_fast_path
                and mode is Mode.LIVE and message_is_user_turn and not attachments):
            _fast = self._recipe_fast_path(user_message, quest_id=quest_id,
                                           context_meta=context_meta, emit=emit)
            if _fast is not None:
                return _fast
        # DEEP RECIPE: a request that repeats a saved quest-data operation gets a ready plan, so the
        # planner call and the turn-start context search below never run for it.
        _forced_plan: Optional[PlanDecision] = None
        if (self.recipes is not None and cfg.recipe_fast_path and cfg.recipe_deep
                and mode is Mode.LIVE and message_is_user_turn and not attachments
                and self._has_deep_execution_capability()):
            _forced_plan = self._recipe_deep_plan(user_message, quest_id=quest_id,
                                                  context_meta=context_meta, emit=emit)

        # --- INSTANT ACK (Feature 1): best-effort, no latency impact -------------------------
        # When cfg.instant_ack is True:
        #   1. Synchronously emit "Looking into this..." so the consumer gets an immediate tick.
        #   2. Launch a CHEAP one-sentence ack call IN A BACKGROUND THREAD — it runs concurrently
        #      with context assembly + the first planner step, so it adds ZERO wall-clock latency.
        # The ack prompt explicitly forbids em dashes (brand rule). A failure is swallowed.
        # The narrator unifies the old instant-ack into a single conversational train of thought:
        # its first beat (started here, concurrent with context assembly) acknowledges the new
        # message, and later beats are the planner's own conversational rationale, relayed at the
        # slow stages (read/deep) via narrator.relay() with NO extra LLM call. ``narrate`` supersedes
        # the legacy ``instant_ack`` flag. Speaks in the selected rep's persona; HOW the ack narrates
        # is overridable by the consumer via cfg.narration_system_prompt.
        narrator = Narrator(
            provider=self.provider,
            model=self.registry.resolve_tier(cfg.planner_tier),
            emit=emit,
            persona=rep_preamble or "",
            transcript_tail=transcript,
            system_prompt=cfg.narration_system_prompt,
            enabled=bool(cfg.narrate or cfg.instant_ack),
            prior_narration=prior_narration,
        )
        if narrator.enabled:
            try:
                emit.status("Looking into this...")
            except Exception:  # noqa: BLE001
                pass
            narrator.begin(user_message)

        # --- THE REACH JUDGE, started now so it overlaps everything before the first plan -------
        # Its inputs are only the literal request and the consumer's reach summary, so it does not
        # need to wait for understanding, context or guidance. Run serially in front of the first
        # plan it measured +0.7s p50 per turn on a cheap planner (2026-10-05); started here, the
        # first _plan() only collects a result that is normally already there. No-op when off.
        # Known cost, accepted: a turn that ends before planning (an understanding clarify) has
        # paid for one judge call (~1k input tokens) that nobody reads.
        self.prefetch_reach_verdict(user_message)

        # --- ContextAssembler: pre-flight context injection (optional fifth adapter) -----------
        # When a ContextAssembler is wired, call assemble() once before the loop so task-specific
        # context is GUARANTEED applied, not left to the reactive gather. It COMPOSES with any
        # context_view the caller already supplied (e.g. a Quest AI chat's bound-quest context):
        # the assembled cards go FIRST, then the caller's context, so cards apply even when the
        # caller provided its own grounding. Panel context-docs / chat uploads (``attachments``)
        # append below, so a single run grounds on cards + caller context + Quest-panel docs.
        # Best-effort: any exception leaves the run unchanged.
        # ``_ctx_meta`` carries the caller's identity/scope (quest_id, plus anything in
        # ``context_meta`` such as user_id/team_id) so a multi-tenant assembler (e.g. a Quest-backed
        # one in quest-backend serving all users) can scope its lookup to the right user/team/quest.
        # The default FileContextStore ignores it. Threaded to both assemble() and record().
        # Context assembly (a corpus search) can be slow, so it runs in a BACKGROUND THREAD
        # concurrently with the ack call and guidance selection — the same pattern as the
        # instant-ack above.  We start it here, do the fast synchronous work below, then
        # collect the result with a short timeout so corpus search never blocks the turn.
        # ``_ctx_meta`` carries the caller's identity/scope (quest_id, plus anything in
        # ``context_meta`` such as user_id/team_id) so a multi-tenant assembler (e.g. a Quest-backed
        # one in quest-backend serving all users) can scope its lookup to the right user/team/quest.
        # The default FileContextStore ignores it. Threaded to both assemble() and record().
        _ctx_meta: Dict[str, Any] = {**(context_meta or {})}
        if quest_id is not None:
            _ctx_meta.setdefault("quest_id", quest_id)
        # Carry the conversation identity into the context meta so PER-GOAL deep context selection
        # (``_run_deep`` -> ``_assemble_for_goal`` / ``_widen_for_goal``) can pull a conversation
        # slice / related conversations for each goal. Inert when no conv_id/scope is in scope.
        if conv_id is not None:
            _ctx_meta.setdefault("conv_id", conv_id)
        if conv_scope is not None:
            _ctx_meta.setdefault("conv_scope", conv_scope)
        # PER-IDEA THREADING: the card the conversation is currently on, so a CARD-AWARE assembler
        # can prefer this idea's material (priority blending, never hard isolation: everything else
        # stays reachable). An assembler that does not know the key ignores it, so this is inert for
        # every existing assembler.
        if card_thread_ctx is not None and card_thread_ctx.active_card_id:
            _ctx_meta.setdefault("thread_card_id", card_thread_ctx.active_card_id)

        # SCOPE TAGS (see core/scope_tags.py): when this turn is scoped to one or more quests and
        # the caller didn't already supply its own ``scope_tags``, derive them from quest_id/
        # quest_ids so every downstream assembler/store can read ONE field to fence cross-quest
        # leakage (a card/vector-hit/recent-context record tagged for a different quest never
        # surfaces here). A caller that already set scope_tags explicitly is left untouched.
        _scope_tag_quest_ids = self._meta_quest_ids(_ctx_meta)
        if _scope_tag_quest_ids:
            _ctx_meta.setdefault(
                "scope_tags", [quest_scope_key(q) for q in _scope_tag_quest_ids]
            )
        if _forced_plan is not None:
            _ctx_meta[RECIPE_SKIP_GOAL_CONTEXT] = True

        # --- Mid-run user messages: auto-drain a wired inbox for THIS conversation ----------------
        # If the caller didn't pass an explicit ``pending_inputs`` but an ``input_inbox`` is wired,
        # build one that drains this conversation's pending messages. This makes mid-run message
        # folding work for ANY interface that pushes to the inbox (chat, Quest frontend, ...) with no
        # per-interface wiring beyond the push. The conversation key is resolved generically.
        if pending_inputs is None and self.input_inbox is not None:
            _conv_key = (quest_id or _ctx_meta.get("conversation_id")
                         or _ctx_meta.get("session_id") or _ctx_meta.get("user_id"))
            if _conv_key:
                _inbox = self.input_inbox
                pending_inputs = lambda: _inbox.drain(_conv_key)  # noqa: E731

        # --- BRAINSTORM RELEASE (latched turns only; the exit authority) -----------------------
        # While the latch is held, ONE dedicated structured judgment decides whether this message
        # lifts the hold (see judge_brainstorm_release). It runs BEFORE ANY stage that could act or
        # escalate (input understanding, the planner loop, the terminal paths), so the whole turn --
        # understanding, planner note, action gating, acknowledgment -- runs in the mode the user is
        # actually in. A HOLD verdict (including every failure mode) leaves the latch on; only a
        # release flips it, and that release is what the consumer sees as mode_signal.
        # Cost is bounded to brainstorm turns: nothing here runs in normal mode.
        if brainstorm_active and cfg.mode_signals_enabled:
            _released, _release_reason = self.judge_brainstorm_release(user_message, transcript)
            log.info("Brainstorm-release judge: %s (%s)",
                     "RELEASE" if _released else "HOLD", _release_reason)
            if _released:
                # A release lifts the CONSUMER'S LATCH, never the veto the user gave in this very
                # message: "go ahead, but don't open a task for it" releases the mode and still
                # forbids the task.
                brainstorm_active = hold_off_active
                brainstorm_released_this_turn = True
                mode_signal_detected = "exit_brainstorm"
                try:
                    emit.emit(ProgressEvent(type=EVENT_MODE_SIGNAL, step=0,
                                            data={"signal": "exit_brainstorm",
                                                  "execution_mode": cfg.execution_mode,
                                                  "reason": _release_reason}))
                except Exception:  # noqa: BLE001 — reporting the signal must never break the turn
                    pass

        # --- STAGE 1: USER INPUT UNDERSTANDING (resolve the request) --------------------------
        # Three distinct stages follow, each relating to the goal condition but kept separate:
        #   1. UNDERSTAND the input -> a self-contained GOAL CONDITION (uses CONVERSATION context).
        #   2. FIND CONTEXT to understand how to ACHIEVE the goal (STAGE 2 below: context selection
        #      driven by the goal condition, not the raw message).
        #   3. PLAN how to achieve the goal (the planner loop, using stage-2 achievement context and
        #      free to search for more / as a planned gather step).
        # A short or anaphoric input ("ok do it", "the first one") can't be understood alone. Only
        # when a ConversationStore is wired, a conv_id is present, AND a cheap keyword check says the
        # input leans on context, do we pull a relevant slice of the conversation and ask the model
        # ONCE to rewrite the message as a goal condition. A self-contained input skips this entirely
        # (no LLM hop, ZERO added latency). Context ACCUMULATES: the conversation slice gathered here
        # flows forward into stages 2 and 3 (each starts from what is already gathered and searches
        # for more as needed). A subgoal the planner spawns is the exception: it is handed only its
        # own focused context (see _run_deep). The planner still sees the user's LITERAL words too, so
        # word-for-word fidelity is preserved.
        goal_condition = user_message
        conv_ctx_text = ""
        # Query-aware retrieval routing (spec v3, work package C): structured constraints parsed
        # alongside the goal condition below (else-branch only -- see ``_derive_goal_condition``).
        # None whenever nothing in the message calls for filtering (today's behavior).
        retrieval_constraints: Optional[Dict[str, Any]] = None
        if (self.conversation_store is not None and conv_id
                and self._needs_context_to_understand(user_message)):
            try:
                goal_condition, conv_ctx_text, clarify_q = self._understand_input(
                    user_message, conv_id, conv_scope or {}, emit)
            except Exception:  # noqa: BLE001 — understanding must never break the run
                goal_condition, conv_ctx_text, clarify_q = user_message, "", None
            if clarify_q and brainstorm_active:
                # BRAINSTORM: a latched turn escalates NOTHING, so this may not park a decision-
                # request. Carry the question into the reply instead (the answer asks it inline) and
                # let the loop run normally on the best available reading of the message.
                log.info("Brainstorm mode: understanding-clarify degraded to an in-reply question.")
                brainstorm_suppressed_action = brainstorm_suppressed_action or "clarify"
                brainstorm_clarify_question = clarify_q
            elif clarify_q:
                # Short-circuit the turn: ask the user, do NOT run the planner loop. Reuse the
                # clarify/confirm mechanism so the executor maps it to needs_you and chat posts it.
                plan = PlanDecision(action="confirm", confirm_question=clarify_q,
                                    rationale="clarification needed to understand the request")
                res = self._run_confirm(plan, quest_id=quest_id)
                return self._finish_understanding_clarify(
                    res, emit=emit, exec_record=exec_record, user_message=user_message,
                    ctx_meta=_ctx_meta)
            # STAGE 1 produced a goal condition. Context ACCUMULATES across the stages: the
            # conversation context selected here flows FORWARD, so stage 2 (achievement-context
            # selection, below) and stage 3 (the planner) each START from what was already gathered
            # and only search for MORE as needed. So we inject the gathered conversation context AND
            # the resolved request. (Narrowing to a minimal, just-what-it-needs slice happens later
            # per SUBGOAL in _run_deep, to keep each subgoal focused, not on this main flow.)
            if restates_meaningfully(goal_condition, user_message) or conv_ctx_text:
                emit.emit(ProgressEvent(
                    type=EVENT_UNDERSTANDING,
                    text=f"Understood as: {goal_condition}",
                    data={"goal_condition": goal_condition, "internal": True}))
                # The resolver used this context to derive goal_condition, so it IS relevant.
                # Strip the defensive hedge label added during retrieval so the answerer does
                # not treat confirmed-useful context as uncertain background noise.
                clean_conv_ctx = conv_ctx_text.replace(
                    "=== OTHER PAST CONVERSATIONS (may be unrelated) ===",
                    "=== RETRIEVED PAST CONVERSATIONS ==="
                ) if conv_ctx_text else conv_ctx_text
                _understood_block = (
                    "--- CONVERSATION CONTEXT ---\n" + clean_conv_ctx + "\n"
                    if clean_conv_ctx else ""
                ) + ("--- UNDERSTOOD REQUEST (INTERNAL: this is how the run resolved their message; "
                     "answer it, never read it back to them) ---\n" + goal_condition + "\n")
                context_view = (_understood_block + "\n" + context_view
                                if context_view else _understood_block)
        else:
            # Fix 13: goal-condition ESTABLISHMENT is a separate concern from context-FETCHING above,
            # and now always happens -- even for a self-contained input that needed no conversation
            # context. One cheap-tier LLM call derives a concrete, checkable done-standard from the
            # message alone (see ``_derive_goal_condition``; fails safe to the raw ``user_message``,
            # never raises). This adds one LLM round trip to every turn that reaches here (a real
            # cost/latency change from before, when this branch did nothing); see CHANGELOG.md.
            # Announced, for the same reason the verification pass below is: it is a real model
            # round trip that runs before any context is even fetched, and it used to be silent.
            emit.status("Working out what you're asking for…")
            goal_condition, retrieval_constraints = self._derive_goal_condition(user_message, now=now)
            if restates_meaningfully(goal_condition, user_message) or retrieval_constraints:
                _event_data: Dict[str, Any] = {"goal_condition": goal_condition, "internal": True}
                if retrieval_constraints:
                    _event_data["constraints"] = retrieval_constraints
                emit.emit(ProgressEvent(
                    type=EVENT_UNDERSTANDING,
                    text=f"Understood as: {goal_condition}",
                    data=_event_data))
            if retrieval_constraints and isinstance(retrieval_constraints.get("time_range"), dict):
                # C2 card stores (item level): thread the turn's time_range into the assembly meta
                # so a time-filter-capable assembler (e.g. ``FileContextStore``) hard-filters card
                # CONTENT ITEMS by their ``ts``, and the recent-context filter/render calls below
                # apply the same rule. ``_ctx_meta`` also flows into every per-goal assembly this
                # turn spawns (``_run_deep`` -> ``_assemble_for_goal_with_cards``), so deep goals
                # inherit the filter. Assemblers that ignore unknown meta keys are unaffected.
                _ctx_meta["time_range"] = retrieval_constraints["time_range"]
            if retrieval_constraints and self.conversation_store is not None:
                # C3 routing: constraints name a time period / topic / actor / content kind, so do
                # a BOUNDED, hard-filtered cross-conversation search now instead of leaving this to
                # undirected relevance-only recall. The store applies filters FIRST (hard filter),
                # relevance ranks WITHIN the filtered set, and degrades to relevance-only with an
                # explicit labeled note when the filtered set is empty (see ``related_slices``).
                # Best-effort: any failure here must never break the turn.
                try:
                    _filtered_ctx = self.conversation_store.related_slices(
                        goal_condition, conv_scope or {}, exclude_conv_id=conv_id,
                        filters=retrieval_constraints)
                except Exception:  # noqa: BLE001
                    _filtered_ctx = None
                if _filtered_ctx is not None and _filtered_ctx.text:
                    _filtered_block = "=== FILTERED CONVERSATION SEARCH ===\n" + _filtered_ctx.text
                    context_view = (_filtered_block + "\n\n" + context_view
                                    if context_view else _filtered_block)

        # Fix 5a: a handful of PRIOR user turns in this SAME conversation, for the overseer digest's
        # RECENT CONVERSATION section. Computed once (from whatever ``conv_ctx_text`` STAGE 1 pulled;
        # empty when no conversation_store/conv_id was in play), excluding the current turn's own
        # request (raw AND resolved) so it is never duplicated against CURRENT USER REQUEST /
        # RESOLVED AS. Reused by both overseer hooks this run. Never raises.
        _recent_conversation_digest_lines = _recent_conversation_turns(
            conv_ctx_text, exclude=[user_message, goal_condition])

        # --- Recent-turn context: warm NO-LLM fallback (see core/recent_context.py) ------------
        # Independent of the ConversationStore-driven Stage 1 above (that rewrites the message; this
        # carries forward the CARDS recently selected). A fast local file read across THREE scopes
        # -- conv:<conv_id>, quest:<quest_id>, and always "global" (unless the consumer disabled
        # cross-conversation memory) -- gated through a pure lexical filter (no LLM) so an unrelated
        # new question never drags in stale cards. Cards that survive are merged into context_view
        # further below, crucially EVEN WHEN the fresh background assembly started next times out or
        # finds nothing -- that resilience is the point. ``_recent_scope_keys`` is empty only when
        # recent-context is fully off (no conv/quest id AND global disabled is impossible since
        # global defaults on; it is only empty when the consumer wired no recent_context at all,
        # handled by the outer None check below).
        _recent_scope_keys_this_turn = self._recent_scope_keys(_ctx_meta)
        _recent_filtered: List[Dict[str, Any]] = []
        _recent_records: List[Dict[str, Any]] = []
        if (self.recent_context is not None and cfg.recent_context_enabled
                and _recent_scope_keys_this_turn):
            try:
                _recent_records = self.recent_context.load(_recent_scope_keys_this_turn)
            except Exception:  # noqa: BLE001 -- a store failure must never break the run
                _recent_records = []
            try:
                _is_followup = self._needs_context_to_understand(user_message)
            except Exception:  # noqa: BLE001
                _is_followup = False
            try:
                _recent_filtered = filter_relevant(
                    _recent_records, f"{goal_condition} {user_message}",
                    is_followup=_is_followup, max_cards=cfg.recent_context_max_cards,
                    time_range=_ctx_meta.get("time_range"))
            except Exception:  # noqa: BLE001
                _recent_filtered = []
            # ITEM-LEVEL RANKING HINT for fresh assembly (see core/recent_context.py): built from
            # ALL loaded records (not just the ones that survived the CARD-level gate above), since
            # this only influences item ORDER within a card fresh assembly re-selects on its own --
            # even a card that did not itself pass filter_relevant this turn may resurface via fresh
            # assembly, and its item-usage history should still apply. Threaded into the
            # consolidating LLM pass via meta; a pure hint, never a hard override.
            try:
                _recent_item_usage_hint = build_item_usage_hint(
                    _recent_records, f"{goal_condition} {user_message}")
                if _recent_item_usage_hint:
                    _ctx_meta["recent_item_usage"] = _recent_item_usage_hint
            except Exception:  # noqa: BLE001
                pass

        # --- Anticipation engine, TURN START (opt-in; see core/anticipation.py) ----------------
        # Score the ACTUAL message against the predictions planned after the previous turn: every
        # live prediction gets an outcome (hit or miss decay, logged -- the objective function's
        # measurable record), and when the best match clears MATCH_SERVE, this turn is seeded with
        # that prediction's PRECOMPUTED context as a cheap, discardable hint under the
        # CACHED_HINT_GATE doctrine (it may only ever save work, never cost work: the fresh
        # assembly below still runs and its context still leads). Guarded so ANY failure degrades
        # to the normal path; with the flag off (the default) this block does nothing at all.
        if self.anticipator is not None and cfg.anticipation_enabled:
            try:
                _ant_keys = self._anticipation_scope_keys(_ctx_meta)
                _ant_view = ""
                if _ant_keys:
                    _ant_match = self.anticipator.observe(
                        user_message, _ant_keys, now=datetime.now(),
                        anticipated_id=anticipated_id)
                    if (_ant_match is not None and _ant_match.matched is not None
                            and _ant_match.precomputed is not None):
                        _ant_view = (getattr(_ant_match.precomputed, "context_view", "")
                                     or "").strip()
                if _ant_view:
                    _ant_block = (
                        "--- ANTICIPATED CONTEXT (precomputed for a predicted ask) ---\n"
                        + CACHED_HINT_GATE + "\n\n" + _ant_view)
                    context_view = (_ant_block + "\n\n" + context_view if context_view
                                    else _ant_block)
            except Exception:  # noqa: BLE001 -- anticipation must never break the turn
                log.debug("anticipation observe failed; continuing on the normal path",
                          exc_info=True)

        # ONE shared, query-keyed in-run context cache (the unified primitive, docs sec. 3): the
        # turn-start pre-fetch below and any mid-loop {"cards": ...} / {"card": ...} read reach the
        # SAME assembler through it, so a turn-start assembly timeout is recoverable mid-loop (a
        # late-landing pre-fetch future is kept referenced and served from cache) instead of dropping
        # all fresh context for the whole turn. It carries ``emit`` so a mid-loop card read can emit
        # EVENT_CONTEXT. Closed in ``finish()``.
        card_context = TurnCardCache(self.context_assembler, _ctx_meta)
        card_context.emit = emit
        _ctx_prefetch_query: Optional[str] = None

        _assembled = None
        _ctx_future = None
        _ctx_executor = None
        if self.context_assembler is not None and _forced_plan is None:
            try:
                _ctx_assembler = self.context_assembler
                # STEP 2 (context selection) targets the RESOLVED request: when Step 1 rewrote the
                # message into a goal_condition, select context for THAT, not the raw anaphoric text.
                # The planner still receives the literal ``user_message`` (literal-words fidelity);
                # the goal_condition rides in context_view. When Step 1 did not run, goal_condition
                # == user_message, so this is byte-for-byte the prior behavior.
                _ctx_msg = goal_condition
                _ctx_prefetch_query = _ctx_msg

                def _do_assemble() -> Any:
                    # Soft internal deadline slightly under the hard collect timeout below,
                    # threaded via ``meta["assembly_deadline"]`` (a ``time.monotonic()``
                    # timestamp). A deadline-aware assembler (e.g. HybridContextAssembler)
                    # uses it to return whatever its retrieval arms completed in time as a
                    # PARTIAL result (``AssembledContext.partial``) instead of overrunning the
                    # budget and losing everything. Assemblers that ignore the hint behave
                    # exactly as before. Only THIS turn-start prefetch is deadline-bounded:
                    # ``_ctx_meta`` itself is left untouched, so mid-loop reads through
                    # ``TurnCardCache`` stay unbounded and a late full assembly is still
                    # recoverable there.
                    hard_budget = context_assembly_timeout_seconds()
                    soft_budget = max(hard_budget - 0.5, hard_budget * 0.5)
                    assemble_meta = {**(_ctx_meta or {}),
                                     "assembly_deadline": time.monotonic() + soft_budget}
                    # DOCS-FALLBACK GATING (see ``gate_docs_fallback``): usually a miss this early
                    # in the turn (the reach judge was only just prefetched above, around the same
                    # moment as this), which is correct -- this must never wait on it -- but a
                    # repeated/already-cached message resolves it for free. The far more common
                    # case is the matching gate at the top of ``_run_deep``, where a deep goal's
                    # own context assembly runs well after the planner already blocked on this
                    # same verdict.
                    self.gate_docs_fallback(assemble_meta, user_message)
                    return _ctx_assembler.assemble(_ctx_msg, meta=assemble_meta)

                _ctx_executor = ThreadPoolExecutor(max_workers=1)
                _ctx_future = _ctx_executor.submit(_do_assemble)
                # Register the eager pre-fetch so a later mid-loop {"cards": <same query>} read can
                # serve it from the shared cache -- crucially EVEN IF the turn-start collect below
                # times out: the future is not cancelled, so a late result still lands here.
                card_context.register_prefetch(_ctx_msg, _ctx_future)
                try:
                    # This is the ContextAssembler.assemble() call: a hybrid keyword + vector
                    # search over the wired context (cards + turn history), not a single named
                    # source. "searching context" names the stage honestly (STAGE 2: FIND CONTEXT).
                    emit.status("Searching context…")
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 -- assembly setup failure must never break the run
                _ctx_future = None
                _ctx_executor = None

        # --- GuidanceProvider: use-case-specific instruction PRE-SELECTION (optional) ----------
        # When a GuidanceProvider is wired, ask it for the cards most relevant to THIS message and
        # render them into an "APPLICABLE GUIDANCE" block, PREPENDED to context_view (so it leads,
        # ahead of any assembled/caller context — the same compose order the ContextAssembler uses
        # for its own output). The planner is told this block may already cover the request, and to
        # list_guidance/read_guidance for anything it doesn't. ``guidance_selected_ids`` records
        # what was pre-selected so a later read_guidance of the same id returns a de-dupe note.
        # Best-effort: any failure leaves the run exactly as if no guidance were wired.
        guidance_selected_ids: set = set()
        # The guidance cards selected for THIS input are the QUALITY STANDARDS the result must meet.
        # We keep them in ``quality_standards`` so the goal loop can verify the done-standard against
        # the same standards the planner was given (see _run_deep / _verify_goal).
        quality_standards: Optional[str] = None
        if self.guidance is not None:
            # Bounded, like the concurrent context-assembly fetch above: select() is a
            # caller-supplied GuidanceProvider implementation that may block on I/O (a
            # dynamic_guidance_loader hitting a DB/network, or its own LLM filtering call)
            # with no timeout of its own. Called directly with no bound, that hangs the whole
            # turn indefinitely -- in the SAME "Searching context…" status the concurrent
            # context-assembly fetch already shows, making it look identical to that fetch
            # stalling even though it was never protected by that fetch's timeout.
            _guidance_timeout = guidance_selection_timeout_seconds()
            # Not a `with ThreadPoolExecutor(...)` block: its __exit__ calls shutdown(wait=True),
            # which would block on the SAME hung call this is meant to bound. shutdown(wait=False)
            # below lets a genuinely stuck select() keep running in the background (Python threads
            # cannot be force-killed) without the timeout itself becoming just as blocking.
            _guidance_pool = ThreadPoolExecutor(max_workers=1)
            try:
                _cards = _guidance_pool.submit(
                    self.guidance.select,
                    user_message,
                    team_id=_ctx_meta.get("team_id") if _ctx_meta else None,
                    org_id=_ctx_meta.get("org_id") if _ctx_meta else None,
                    limit=cfg.guidance_topk,
                ).result(timeout=_guidance_timeout) or []
            except FuturesTimeoutError:
                log.warning(
                    "Guidance selection timed out after %.1fs; this turn proceeds without "
                    "guidance (QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS to adjust)",
                    _guidance_timeout)
                _cards = []
            except Exception as e:  # noqa: BLE001 -- a provider must never break the run
                log.warning(f"Guidance selection failed: {type(e).__name__}: {e}", exc_info=True)
                _cards = []
            finally:
                try:
                    _guidance_pool.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
            if _cards:
                _blocks = ["--- APPLICABLE GUIDANCE ---"]
                for _c in _cards:
                    guidance_selected_ids.add(_c.id)
                    _blocks.append(f"[{_c.id}] {_c.title}\n(applies when: {_c.relevance})\n{_c.body}")
                _guidance_view = "\n\n".join(_blocks)
                quality_standards = _guidance_view  # rolls into goal verification as the quality bar
                context_view = (_guidance_view + "\n\n" + context_view if context_view
                                else _guidance_view)
                # Transparency: a STATUS tick naming the guidance applied this turn.
                try:
                    emit.status("Applied guidance: "
                                + ", ".join(c.title for c in _cards) + ".")
                except Exception:  # noqa: BLE001
                    pass

        # --- Narrator: ordering barrier for the first beat ------------------------------------
        # The first conversational beat EMITS ITSELF from its background thread the instant the model
        # returns (see Narrator.begin / _gen_and_say), so the instant response already went out as
        # early as possible, concurrently with context assembly + guidance and never sequenced behind
        # them. This is only a JOIN barrier: it waits for that emit to finish before the planner loop
        # emits any relay beat, so the ack always precedes the planner's rationale, then cleans up the
        # background thread before run() returns. A timeout just proceeds.
        narrator.flush_first()

        # --- ContextAssembler: collect the background assemble() result -----------------------
        # The assemble() call has been running concurrently with the ack + guidance.  Collect it
        # NOW with a short timeout so corpus search never blocks the interactive turn.  On timeout
        # or error we proceed with no assembled context (the reactive gather still runs in-loop).
        # The assembled cards go FIRST, then the caller's context, so cards apply even when the
        # caller provided its own grounding (panel docs / chat uploads append below).
        context_assembly_timed_out = False
        context_assembly_partial = False
        if _ctx_future is not None:
            ctx_timeout = context_assembly_timeout_seconds()
            try:
                _assembled = _ctx_future.result(timeout=ctx_timeout)
            except TimeoutError:
                # LOUD by design: this drops ALL fresh context for the turn — not even the
                # assembler's soft-deadline PARTIAL path (see _do_assemble) returned in time —
                # the exact mechanism behind "forgets what I just said". It must never be a
                # debug-only breadcrumb.
                context_assembly_timed_out = True
                timeout_count = record_context_assembly_timeout()
                log.warning(
                    "Context assembly timed out after %.1fs with no partial result; "
                    "turn-start context was dropped and this turn proceeds without it "
                    "(QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust; total timeouts this "
                    "process: %d)",
                    ctx_timeout, timeout_count)
                _assembled = None
            except Exception as e:  # noqa: BLE001 — cancelled or assembler error: skip
                log.warning(f"Context assembly failed: {type(e).__name__}: {e}", exc_info=True)
                _assembled = None
            finally:
                try:
                    _ctx_executor.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
            if _assembled is not None:
                # PARTIAL result: the assembler hit its soft deadline and returned only the
                # retrieval arm(s) that completed. Using it beats the drop-everything path,
                # but stay LOUD about the degradation (same visibility bar as the timeout).
                # ``assembly_partial`` / the WARNING only fire when the partial actually
                # CARRIES content -- an empty partial means nothing was used, and saying
                # "partial context was used" would be untrue.
                _assembly_partial_flag = bool(getattr(_assembled, "partial", False))
                context_assembly_partial = _assembly_partial_flag and bool(
                    getattr(_assembled, "context_view", "") or "")
                if context_assembly_partial:
                    log.warning(
                        "Context assembly hit its soft deadline; PARTIAL context was used "
                        "for this turn (only the retrieval arm(s) that finished within "
                        "budget; QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust)")
                elif _assembly_partial_flag:
                    log.warning(
                        "Context assembly hit its soft deadline and returned an EMPTY "
                        "partial result; no fresh turn-start context was used this turn "
                        "(QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust)")
                # FULL turn-start assembly completed in time: seed the shared cache so a
                # same-query mid-loop {"cards": ...} read is a pure cache hit (no second assembly
                # run). A PARTIAL result is used for THIS turn-start prompt but never cached as
                # the query's completed result -- registering it would displace the full fuse a
                # later mid-loop read can still recover; instead the (already-done) prefetch
                # future is discarded so that read falls through to a fresh, deadline-free
                # assemble. On the timeout branch above we deliberately leave the still-running
                # future registered, so if it lands late a mid-loop read for the same query
                # serves it from the cache.
                if _ctx_prefetch_query:
                    if _assembly_partial_flag:
                        card_context.discard_prefetch(_ctx_prefetch_query)
                    else:
                        card_context.register_result(_ctx_prefetch_query, _assembled)
                try:
                    if _assembled.context_view:
                        context_view = (_assembled.context_view + "\n\n" + context_view if context_view
                                        else _assembled.context_view)
                    if model_hint is None and _assembled.model_tier_hint:
                        model_hint = _assembled.model_tier_hint
                except Exception:  # noqa: BLE001
                    pass

        # --- Recent-turn context: merge cards that survived the relevance gate ----------------
        # Folded in REGARDLESS of whether fresh assembly produced anything -- this is the
        # resilience win from core/recent_context.py: if ``_assembled`` is None (no assembler
        # wired, or the background assemble() above timed out/failed), a follow-up turn still gets
        # the cards its own recent turns already selected, gated by filter_relevant so an unrelated
        # new question never drags them in. A recent card already re-found by fresh assembly this
        # turn is dropped here (the fresh one wins). Never raises.
        _card_meta: List[Dict[str, Any]] = (
            list(getattr(_assembled, "card_metadata", None) or []) if _assembled is not None else []
        )
        _sources: List[Dict[str, Any]] = (
            list(getattr(_assembled, "sources", None) or []) if _assembled is not None else []
        )
        _recent_entries: List[Dict[str, Any]] = []
        if _recent_filtered:
            try:
                _fresh_ids = {cm.get("id") for cm in _card_meta if isinstance(cm, dict)}
                _recent_survivors = [r for r in _recent_filtered if r.get("id") not in _fresh_ids]
                _recent_text, _recent_entries = render_recent_cards(
                    _recent_survivors, f"{goal_condition} {user_message}",
                    time_range=_ctx_meta.get("time_range"))
                if _recent_text:
                    context_view = (_recent_text + "\n\n" + context_view if context_view
                                    else _recent_text)
                if _recent_survivors:
                    _sources.append({
                        "adapter": "recent",
                        "label": "recent turns",
                        "items": [r.get("title") or r.get("id", "") for r in _recent_survivors],
                    })
            except Exception:  # noqa: BLE001 -- recent-context merge must never break the run
                _recent_entries = []
        _merged_card_meta = _card_meta + _recent_entries

        # --- STRUCTURAL SUFFICIENCY GATE: which of this turn's context is only a SUMMARY ---------
        # A content item whose locator declares ``full_ref`` is saying, structurally, "the text
        # stored on the card is an abridged stand-in, the real source is fetched with THIS read
        # spec". Two things ride on that (see core/sufficiency.py): the planner is TOLD which items
        # are summaries and how to open them (they render identically to full content otherwise, so
        # it had no way to know), and the loop refuses to let an "answer" terminate while one of
        # those fetches has not run. Empty and inert for every item that declares nothing.
        abridged_state = AbridgedTurnState()
        if cfg.full_read_before_answer:
            try:
                abridged_state.items = collect_abridged_items(_merged_card_meta)
                _abridged_notice = render_abridged_notice(abridged_state.items)
                if _abridged_notice:
                    context_view = (context_view + "\n\n" + _abridged_notice if context_view
                                    else _abridged_notice)
            except Exception:  # noqa: BLE001 -- the gate must never break a turn
                log.debug("abridged-item collection failed; the turn continues without the gate",
                          exc_info=True)
                abridged_state = AbridgedTurnState()

        # --- PER-IDEA THREADING: the cheap PRIOR, then the planner decides --------------------
        # The candidate set is the cards this turn's HYBRID RETRIEVAL already scored (keyword/IDF
        # arm + vector arm, plus the warm recent-turn cards), merged with whatever the consumer
        # always wants offered (its general/small-talk card, the ideas recently live in this
        # conversation). So the prior costs ZERO extra model calls and ZERO extra searches: it is
        # the same assembly the turn ran anyway. It NARROWS and SURFACES; it never decides. The
        # planner picks the topic on the call it already makes, and any parse failure or ambiguity
        # continues the current card (see core.card_thread.parse_card_thread).
        if card_thread_ctx is not None:
            try:
                _thread_candidates = merge_candidates(card_thread_ctx, _merged_card_meta)
                card_thread_candidate_ids = [c.id for c in _thread_candidates]
                _hint = render_thread_hint(card_thread_ctx, _thread_candidates)
                card_thread_block = _CARD_THREAD_PLANNER_BLOCK_TEMPLATE.format(thread_hint=_hint)
            except Exception:  # noqa: BLE001 -- threading must never break a turn
                log.debug("card thread hint build failed; the turn continues its current card",
                          exc_info=True)
                card_thread_block = ""
                card_thread_candidate_ids = []

        # --- CONTEXT EVENT: emit EVENT_CONTEXT showing which cards were selected -----------
        # Dedicated event for context assembly: card selection + sources. Surfaces in all modes.
        # Always emitted when assembly ran OR recent-turn cards survived OR assembly timed out
        # (even with 0 cards + 0 sources otherwise) so consumers can show "assembly ran but
        # found nothing" -- or "assembly timed out and was dropped" -- without needing a
        # separate signal. Never raises.
        if _assembled is not None or _recent_entries or context_assembly_timed_out:
            try:
                log.debug(f"Assembled context: {len(_merged_card_meta)} cards, {len(_sources)} sources")
                # LIGHTWEIGHT projection: the card_metadata items carry each item's full resolved
                # ``text`` (heavy), and a conversation-history card's TITLE is the raw user turn, so
                # both the card titles and the source items are sanitized before they leave the
                # process. Nothing on this event quotes the person's own words back at them.
                _card_meta_light = _project_card_metadata_for_event(_merged_card_meta)
                _sources_light = _project_sources_for_event(_sources)
                # Same projected shapes the panel is allowed to show (see explain_cards above).
                explain_cards = list(_card_meta_light or [])
                explain_sources = list(_sources_light or [])
                # The text field used to concatenate the card titles ("Selected cards: Hi, Hello."),
                # which is the same verbatim-turn leak by another route. Counts, not content.
                _text = (f"Selected {len(_merged_card_meta)} context card"
                         f"{'s' if len(_merged_card_meta) != 1 else ''}."
                         if _merged_card_meta else "")
                log.debug(f"Emitting EVENT_CONTEXT: {_text}")
                emit.emit(ProgressEvent(
                    type=EVENT_CONTEXT,
                    text=_text,
                    data={
                        "card_metadata": _card_meta_light,
                        "sources": _sources_light,
                        "card_count": len(_merged_card_meta),
                        "source_count": len(_sources),
                        # Structured marker a consumer can observe/aggregate: this turn's
                        # turn-start context was dropped because assembly ran out of time (see
                        # the WARNING logged at the timeout call site and
                        # ``record_context_assembly_timeout``).
                        "assembly_timed_out": context_assembly_timed_out,
                        # Sibling marker: assembly hit its soft deadline and this turn ran on
                        # a PARTIAL result (the arm(s) that completed in time) instead of
                        # dropping fresh context entirely.
                        "assembly_partial": context_assembly_partial,
                        # Retrieval metadata is the run explaining itself. A consumer routes this to
                        # a debug surface, never into the chat bubble.
                        "internal": True,
                    }
                ))
            except Exception as e:  # noqa: BLE001
                log.error(f"Failed to emit EVENT_CONTEXT: {e}", exc_info=True)

        # --- Recent-turn context: write back the merged selection for the NEXT turn ------------
        # Best-effort, never raises. Includes BOTH freshly assembled and surviving recent cards so
        # the warm set follows the conversation forward; a card that has dropped out of relevance is
        # pruned next turn by filter_relevant, not by record() itself.
        if (self.recent_context is not None and cfg.recent_context_enabled
                and _recent_scope_keys_this_turn and _merged_card_meta):
            try:
                self.recent_context.record(_recent_scope_keys_this_turn, _merged_card_meta, user_message)
            except Exception:  # noqa: BLE001
                pass

        # --- CONTEXT TRANSPARENCY (Feature 2): emit a human-readable summary of sources ------
        # Best-effort: emit a STATUS event naming the adapters + file items so the consumer/UI
        # can show "Context from: ...". Only fires when the assembled context carries source
        # attribution (assemblers opt in by populating AssembledContext.sources). Never raises.
        if _assembled is not None:
            try:
                _sources = getattr(_assembled, "sources", None) or []
                if _sources:
                    _parts: List[str] = []
                    for _src in _sources:
                        _label = _src.get("label") or _src.get("adapter") or "unknown"
                        _items = _src.get("items") or []
                        if _items:
                            _item_names = ", ".join(str(x).split("/")[-1] for x in _items[:5])
                            _extra = f" (+{len(_items) - 5} more)" if len(_items) > 5 else ""
                            _parts.append(f"{_label} ({_item_names}{_extra})")
                        else:
                            _parts.append(_label)
                    if _parts:
                        emit.status("Context from: " + ", ".join(_parts) + ".")
            except Exception:  # noqa: BLE001
                pass

        # --- Attachments (multimodal): ONE path for chat uploads + panel context-docs ----------
        # Prepare against the model that WILL answer (model_hint or the default answer tier), so
        # native-vs-describe is decided by the real answering model's vision capability. The text
        # context grounds the PLANNER; native image blocks are appended to the final answer call.
        native_blocks: List[Dict[str, Any]] = []
        if attachments:
            from .attachments import prepare_attachments
            answer_model = self.registry.resolve_tier(model_hint or "sonnet")
            prepared = prepare_attachments(
                attachments,
                model=answer_model,
                provider=self.provider,
                vision_provider=self.vision_provider,
                vision_model=self.vision_model,
            )
            native_blocks = prepared.native_blocks
            if prepared.text_context:
                # PREPEND, never append. ``_strip_discovery_section`` builds the ANSWER's grounding
                # by keeping only what sits BEFORE the "## Discovery" block and dropping the rest,
                # so anything appended to the end of context_view reaches the PLANNER and is then
                # cut out of the answer. Appending here meant a described image (the only way a
                # text-only answering model can see an upload) grounded the plan and vanished from
                # the reply, and the model answered about the picture from nothing: measured
                # 2026-10-01, a solid magenta upload was answered "Green". The attachment is the
                # user's own most salient context, so it belongs at the front either way.
                context_view = (prepared.text_context + "\n\n" + context_view if context_view
                                else prepared.text_context)

        def budget_exhausted() -> bool:
            size = sum(len(o.get("text", "")) + len(str(o.get("hits", ""))) for o in gathered)
            return (time.monotonic() - started) > cfg.max_elapsed_seconds or size > cfg.max_gathered_chars

        def finish(res: OrchestratorResult) -> OrchestratorResult:
            res.steps = steps
            res.gathered = gathered_content(gathered)
            res.execution_record = exec_record
            res.retrieval_constraints = retrieval_constraints
            res.mode_signal = mode_signal_detected
            # PER-IDEA THREADING: the topic this turn was assigned to. Present on EVERY terminal
            # result (answer, deep, confirm, cancelled) once the flag is on, because the consumer
            # stamps the turn's messages from it and a turn that ended early still happened on an
            # idea. None when threading is off, or when the run ended before the first plan.
            if card_thread_decision is not None:
                res.card_thread = card_thread_decision.as_dict()
            if overseer_signals:
                res.overseer_signals = list(overseer_signals)
            # What the narrator actually said aloud this turn (empty when narration is off or it
            # said nothing) — a consumer that persists a rolling window of this across turns and
            # passes it back in as ``run(prior_narration=...)`` is what lets the ack stop reopening
            # every turn with its own recent generic shape; see ``Narrator``.
            if narrator.enabled:
                res.narration_said = list(narrator._said)
            # Tear down the background overseer worker (Fix 1). ``wait=False`` mirrors the
            # context-assembly teardown: any still-running consult (a bounded provider call) finishes
            # on its own without blocking the return, and is never joined synchronously here.
            if _oversee_executor is not None:
                try:
                    _oversee_executor.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
            # Tear down the shared context cache's fresh-assemble executor (``wait=False`` again: a
            # still-running late pre-fetch finishes on its own, exactly like the assembly teardown).
            try:
                card_context.close()
            except Exception:  # noqa: BLE001
                pass
            # A chat turn reaches its deep run through several paths (planner, overseer, own
            # escalation, last resort) that all end here, so this is where a completed governed
            # write is learned as a recipe (the store de-duplicates a repeat of the same request).
            if (res.deep_results and not attachments
                    and any(getattr(d, "has_write_receipt", False) for d in res.deep_results)):
                try:
                    self._recipe_learn_deep(
                        user_message, PlanDecision(action="deep", goal=user_message), res,
                        (_ctx_meta or {}).get("scope_tags"),
                        learnable=message_is_user_turn and mode is Mode.LIVE)
                except Exception:  # noqa: BLE001 -- learning never breaks a turn
                    pass
            # Collect token counts from the provider if it tracks them.
            try:
                if hasattr(self.provider, "tokens_in"):
                    res.tokens_in = self.provider.tokens_in
                    res.tokens_out = self.provider.tokens_out
            except Exception:  # noqa: BLE001
                pass
            if res.kind == "cancelled":
                # Cooperative mid-run cancellation: there is no real outcome to write back or verify
                # against the goal, and no answer/deep/confirm event to surface. Just close out the
                # stream so any consumer waiting on EVENT_DONE is not left hanging.
                emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=steps))
                return res
            # --- LINK GUARD (core/link_guard.py) ----------------------------------------------
            # THE LAST THING THAT TOUCHES THE WORDS, and deliberately so: it runs before the
            # context write-back and before EVENT_RESULT, so what the reader sees, what a consumer
            # persists, and what the assembler learns from are all the SAME sanitized text. Every
            # link is checked (external URLs fetched for real, in-app paths matched against the
            # host app's route table) over several passes, and anything that does not come back
            # verified is stripped to plain words. A fabricated address that reads perfectly is the
            # failure mode here, so "could not verify" is treated like "does not exist".
            if cfg.link_guard and res.kind in ("answer", "deep"):
                try:
                    guard = self.resolve_link_guard()
                    if guard is not None:
                        if res.text:
                            res.text, _lv = guard.sanitize(res.text)
                            res.link_checks.extend(v.to_dict() for v in _lv)
                        for _d in res.deep_results:
                            if getattr(_d, "output", None):
                                _d.output, _lv = guard.sanitize(_d.output)
                                res.link_checks.extend(v.to_dict() for v in _lv)
                        _stripped = [v for v in res.link_checks if v.get("verdict") != "ok"]
                        if _stripped:
                            log.info("Link guard stripped %d unverified link(s): %s",
                                     len(_stripped),
                                     ", ".join(f"{v['url']} ({v.get('reason') or v['verdict']})"
                                               for v in _stripped[:5]))
                except Exception as e:  # noqa: BLE001 -- guarding links must never lose the reply
                    log.warning("Link guard skipped: %s: %s", type(e).__name__, e, exc_info=True)
            # Best-effort ContextAssembler write-back (learn from the outcome for next run). Pass the
            # files the brain ACTUALLY read this run (their rel_paths, from the gathered reads/greps)
            # so the card PINS them: that is what makes the loop compound and what staleness later
            # invalidates. Without this the card would pin nothing and never go stale.
            if self.context_assembler is not None:
                try:
                    used_files: List[str] = []
                    _seen = set()
                    for o in gathered:
                        for rp in [o.get("rel_path")] + [h.get("rel_path") for h in (o.get("hits") or [])]:
                            if rp and rp not in _seen:
                                _seen.add(rp)
                                used_files.append(rp)
                    self.context_assembler.record(
                        user_message,
                        {"kind": res.kind, "steps": res.steps, "files": used_files,
                         "response": res.text,
                         **_ctx_meta},
                    )
                except Exception:  # noqa: BLE001 -- write-back must never break the run
                    pass
            # --- Anticipation engine, TURN END (opt-in; see core/anticipation.py) --------------
            # Learn this turn's ask pattern and plan + precompute the NEXT predictions in a
            # background daemon thread (registered like the index threads so tests/consumers can
            # join it). With the flag off (the default) this is dead code: zero calls, zero
            # threads, zero events.
            if self.anticipator is not None and cfg.anticipation_enabled:
                self._kickoff_anticipation(user_message=user_message, ctx_meta=_ctx_meta)
            # Final token event so consumers get the definitive total alongside the result.
            _fti = res.tokens_in
            _fto = res.tokens_out
            if _fti or _fto:
                emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                        data={"tokens_in": _fti, "tokens_out": _fto,
                                              "total": _fti + _fto, "final": True}))
            # The terminal result + an explicit done event. RESULT/DONE always surface (both lanes).
            # Emit a goal-verdict status line so the user sees WHY the answer was accepted
            # or not — not just a yes/no, but the verifier's reasoning and what was missing.
            if res.goal_verdict is not None:
                _v = res.goal_verdict
                _reason = (_v.get("reason") or "").strip()
                _next = (_v.get("next_action") or "").strip()
                if _v.get("met"):
                    _verdict_line = f"Goal reached. {_reason}" if _reason else "Goal reached."
                elif res.exit_reason == "escalated_deep":
                    _verdict_line = (f"Searching further. {_reason}" if _reason
                                     else "Searching further for a definitive answer.")
                else:
                    # Not met, max turns or unverified — tell the user what was missing
                    _verdict_line = _reason or "Could not fully verify the goal was met."
                    if _next:
                        _verdict_line += f" To complete: {_next}"
                emit.status(_verdict_line)
            if res.kind == "answer":
                # Sanity check: answer text should NEVER be an orchestrator command.
                # If it is, something went wrong in the planner/answer path.
                if res.text and _is_orchestrator_command(res.text):
                    log.error(f"Orchestrator: answer result is an internal command {res.text}; "
                             "replacing with error message")
                    result_text = "I had trouble formulating a proper response to that. Please try again."
                else:
                    result_text = res.text
                emit.emit(ProgressEvent(type=EVENT_RESULT, text=result_text, result_kind="answer",
                                        data={"exit_reason": res.exit_reason,
                                              "goal_verdict": res.goal_verdict} if res.exit_reason else {}))
            elif res.kind == "confirm":
                emit.emit(ProgressEvent(type=EVENT_DECISION, text=res.question,
                                        decision_id=res.decision_id, result_kind="confirm"))
            elif res.kind == "deep":
                out = "\n\n".join(
                    s for s in (unconfirmed_no_change_text(d) for d in res.deep_results) if s
                ) or None
                # Surface the internal FUTURE-CONTEXT bullets as structured data (NOT in the message
                # body) so a consumer can show them as an expandable "what I'll remember" panel.
                _future = _future_context_for_display(res.deep_results)
                emit.emit(ProgressEvent(
                    type=EVENT_RESULT, text=out, result_kind="deep",
                    data={"future_context": _future,
                          "exit_reason": res.exit_reason,
                          "goal_verdict": res.goal_verdict} if (_future or res.exit_reason) else {}))
            # --- "EXPLAIN HOW I GOT THIS" (opt-in; see core/answer_explanation.py) -------------
            # ORDER IS THE POINT: this runs AFTER the terminal EVENT_RESULT above, so the answer
            # has already left for the consumer's screen. The reader is reading while this is
            # written, which is why a second call costs zero PERCEIVED latency. Folding it into the
            # answer call instead would delay the answer itself by its whole generation time (the
            # answer is one blocking call, not a token stream) and would fight REPLY_VOICE_SYSTEM,
            # which forbids the reply from carrying exactly this material.
            # The eligibility gate is model-free and runs first, so an ineligible turn (plain small
            # talk: nothing read, nothing executed, answered at the first step) costs NOTHING and
            # emits NOTHING, and the consumer's toggle simply does not appear.
            if cfg.explain_answer and res.kind in ("answer", "deep"):
                try:
                    _trace = trace_from_result(
                        res, user_message=user_message, goal_condition=goal_condition,
                        cards=explain_cards, sources=explain_sources)
                    if is_eligible(_trace):
                        _explanation = self.write_answer_explanation(_trace)
                        if _explanation:
                            res.explanation = _explanation
                            emit.emit(ProgressEvent(type=EVENT_EXPLANATION, data=_explanation))
                except Exception as e:  # noqa: BLE001 -- explaining must never break a turn
                    log.debug("Answer explanation skipped: %s: %s", type(e).__name__, e,
                              exc_info=True)
            emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=steps))
            return res

        plan: Optional[PlanDecision] = None
        steps = 0
        consecutive_reads = 0  # Track how many steps in a row chose "read"
        # REPEATED READS: canonical spec -> (the step it ran at, its index among real observations),
        # and how many CONSECUTIVE steps asked for nothing new. Cleared after a tool step, since a
        # write can change what a read returns. Only a read that returned something is recorded.
        executed_reads: Dict[str, Tuple[int, int]] = {}
        repeat_only_steps = 0
        # Set by an OVERSEER signal that decided the terminal path this run, so finish() can stamp
        # the exit_reason ("overseer_answer_now" | "overseer_escalated_deep" |
        # "overseer_escalated_human"). "" when the overseer did not decide the path.
        overseer_decided = ""
        for step in range(cfg.max_steps):
            steps = step + 1
            # Cooperative cancellation, checked before starting each new plan/gather/replan step.
            if cancel_check is not None and cancel_check():
                return finish(OrchestratorResult(kind="cancelled", rationale="cancelled mid-run",
                                                  exit_reason="cancelled"))

            # --- Mid-run user messages, OUTER planner-loop drain: fold in anything a human sent
            # while this loop's own gather/plan/re-plan steps were running. The deep retry loop
            # (:5403) and the answer-improve loop (:8531) already drain ``pending_inputs`` at their
            # own iteration boundaries, but THIS loop never did -- so a message arriving during
            # gather/plan iterations sat invisible until the run happened to reach one of those
            # inner loops, which can be many steps away, or never on a plain answer turn. Folded
            # into ``gathered`` as an observation the very next planner call reads, mirroring the
            # OVERSEER's "COURSE CORRECTION" injection immediately below (same gathered-observation
            # shape, same never-raise safety). Reuses ``_drain_pending`` as-is; does not touch it or
            # the :5403 / :8531 drain points.
            try:
                _pending_block = self._drain_pending(pending_inputs)
                if _pending_block:
                    gathered.append({
                        "kind": "query", "locator": "user_message",
                        "text": _pending_block,
                    })
            except Exception:  # noqa: BLE001 -- draining must never break the loop
                pass

            emit.status("Planning…" if step == 0 else "Re-planning…")

            # --- OVERSEER poll (hook A, applied ONE STEP LATE): pick up a consult SUBMITTED on a
            # prior step whose background call has since resolved (Fix 1). This runs BEFORE this
            # step's planner so a redirect can steer the plan we are about to make. A resolved
            # redirect is injected as a COURSE CORRECTION observation (the planner re-reads it,
            # exactly as the old inline hook did); answer_now / escalate_deep / escalate_human end
            # the loop here. If the consult has NOT resolved yet, we simply proceed and re-check next
            # step (never block: ``overseer_poll_timeout_seconds`` is 0.0 in production). Wrapped so
            # it can NEVER break the loop. The one-step-late application is the intended cost of not
            # stalling the walk.
            if pending_oversee is not None:
                try:
                    _psig = self._collect_oversee(
                        pending_oversee, signals=overseer_signals, emit=emit,
                        timeout=cfg.overseer_poll_timeout_seconds)
                    if _psig is not None:
                        pending_oversee = None  # resolved -> free the slot for a fresh consult
                        if _psig.signal == "redirect" and _psig.hint:
                            gathered.append({
                                "kind": "query", "locator": "overseer",
                                "text": f"COURSE CORRECTION: {_psig.hint}",
                            })
                        elif _psig.signal == "answer_now":
                            plan = plan or PlanDecision(action="answer")
                            plan.action = "answer"
                            overseer_decided = "overseer_answer_now"
                            break
                        elif _psig.signal in ("escalate_deep", "escalate_human") and brainstorm_active:
                            # Brainstorm mode: escalations may not ADD actions; treat as proceed.
                            pass
                        elif _psig.signal == "escalate_deep":
                            plan = plan or PlanDecision(action="answer")
                            plan.action = "deep"
                            plan.goal = plan.goal or f"Complete the request: {user_message}"
                            plan.deep_brief = plan.deep_brief or user_message
                            overseer_decided = "overseer_escalated_deep"
                            break
                        elif _psig.signal == "escalate_human":
                            # Genuine human-only fork (Fix 2): route through the SAME confirm /
                            # decision-request mechanism a planner-originated confirm uses, rather
                            # than guessing or executing. Never auto-run deep work for this signal.
                            plan = plan or PlanDecision(action="answer")
                            plan.action = "confirm"
                            plan.confirm_question = (
                                _psig.reason
                                or f"This needs your input before I continue: {user_message}")
                            overseer_decided = "overseer_escalated_human"
                            break
                    # else: not resolved -> keep ``pending_oversee`` and re-check next step.
                except Exception:  # noqa: BLE001 — the overseer must never break the loop
                    pass

            # AUTO-INJECT FUNCTION DISCOVERY on step 0: pre-load all available operations
            # so the planner sees them from the start, ordered by relevance. This eliminates
            # the need for the planner to first ASK for operations; they're already in hand.
            if step == 0 and self.retrieval is not None and _forced_plan is None:
                if getattr(self.retrieval, "list_operations", None) is not None:
                    try:
                        ops_obs = self._exec_one_read({"list_operations": True})
                        if ops_obs is not None:
                            _ops = ops_obs.to_dict()
                            _ops["discovery"] = True  # a capability menu, not answer content
                            # The planner call for THIS step (step 0) is the one that reads it in
                            # full; every later re-plan step this turn sees only a reminder (see
                            # ``collapse_shown_discovery``).
                            _ops["discovery_step"] = step
                            gathered.append(_ops)
                    except Exception as e:  # noqa: BLE001
                        log.debug(f"Auto-injection of list_operations failed: {type(e).__name__}: {e}")

            # Fix 12: remember THIS step's plan (before the planner call below overwrites it) so the
            # hook-A gate can tell whether the NEXT plan repeats it (a cheap looping signal).
            _prev_plan_for_gate = plan

            try:
                if _forced_plan is not None and step == 0:
                    plan = _forced_plan         # deep recipe: no planner call for a known operation
                else:
                    plan = self._plan(user_message, transcript, context_view, gathered, step=step,
                                      narrate=narrator.enabled, persona=rep_preamble or "",
                                      already_said=narrator._said if narrator.enabled else None,
                                      brainstorm=brainstorm_active,
                                      user_vetoed_task=(hold_off_active
                                                        and cfg.execution_mode != "brainstorm"),
                                      card_thread_block=card_thread_block)
            except Exception as e:  # noqa: BLE001 — planner failure -> grounded fallback answer
                log.exception(
                    f"Planner failed on step {steps}: {e}. Falling back to grounded answer."
                )
                plan = PlanDecision(action="answer", rationale="planner error → grounded answer")
            if getattr(plan, "user_intent", None):
                turn_user_intent = plan.user_intent

            # --- PER-IDEA THREADING: resolve the turn's TOPIC ----------------------------------
            # The topic is a property of the MESSAGE, so the FIRST plan that actually EXPRESSES one
            # owns it, and no later step may re-litigate it (a mid-loop read must not be able to move
            # the turn to a different idea).
            #
            # "Expresses one" is doing real work here. The field is required, but a model can still
            # return nothing for it, and it does so exactly when the turn is busy: a real run had the
            # planner omit the topic on a "back to the launch plan" turn while it was planning reads,
            # which landed the fail-safe (continue) and filed that turn under the idea the user was
            # explicitly leaving. So a FELL-BACK decision is not treated as an answer: it is held as
            # the current best, and a later plan step in the SAME turn may still supply the real one.
            # If no step ever does, the fail-safe stands, which is the standing rule (any parse
            # failure or ambiguity continues the current card).
            #
            # A "new" decision leaves ``card_id`` None: the CONSUMER owns the card store, so it is the
            # one that creates (or dedupes onto) the card and stamps the turn.
            #
            # A TOPIC SWITCH IS NOT A MODE SIGNAL. This block sets no mode, touches no latch, and
            # runs regardless of ``brainstorm_active``: moving to another idea inside a held
            # conversation leaves it held, and coming back to an old idea does not release it.
            if card_thread_ctx is not None and (card_thread_decision is None
                                                or card_thread_decision.fell_back):
                try:
                    _decision = parse_card_thread(
                        plan.card_thread if plan else None,
                        active_card_id=card_thread_ctx.active_card_id,
                        known_ids=card_thread_candidate_ids or None,
                    )
                    # Keep a real judgment over a fail-safe; keep the FIRST real judgment forever.
                    if card_thread_decision is None or not _decision.fell_back:
                        _changed = (card_thread_decision is None
                                    or _decision.as_dict() != card_thread_decision.as_dict())
                        card_thread_decision = _decision
                        if _changed:
                            emit.emit(ProgressEvent(
                                type=EVENT_CARD_THREAD, step=steps,
                                data={**card_thread_decision.as_dict(),
                                      "previous_card_id": card_thread_ctx.active_card_id,
                                      "internal": True}))
                except Exception:  # noqa: BLE001 -- reporting must never break the turn
                    log.debug("card thread resolution failed; continuing the current card",
                              exc_info=True)
                    if card_thread_decision is None:
                        card_thread_decision = CardThreadDecision(
                            action="continue", card_id=card_thread_ctx.active_card_id,
                            fell_back=True)

            # --- EXECUTION-MODE SIGNAL (opt-in via cfg.mode_signals_enabled): capture the first
            # explicit mode change the planner detected this turn (LLM judgment riding the
            # planning call that already ran; zero extra calls). Surfaced to the consumer as an
            # event here and on the result in finish(); the consumer owns persisting the latch.
            # An "enter_brainstorm" engages the gating for the rest of this same turn. Fail-safe by
            # construction: normalize_decision already reduced anything unrecognized -- and, with
            # the flag off, ANY value -- to None, and None changes nothing; the flag check here
            # is belt and braces so a disabled consumer can never see a latch flip or the event.
            #
            # EXIT WHILE LATCHED IS NOT THE PLANNER'S TO GIVE. The planner runs at the cheap
            # planner tier and judges any imperative -- including one purely about the subject
            # matter ("create a goal called X and add it to my plan") -- as the user asking to
            # proceed, which released the latch mid-turn and executed work in a conversation the
            # user had put on hold. The release was already decided ONCE, before the loop, by
            # judge_brainstorm_release (fail-safe HOLD). So while cfg.execution_mode is
            # "brainstorm", a planner exit_brainstorm is dropped here.
            if (cfg.mode_signals_enabled and plan and plan.mode_signal
                    and mode_signal_detected is None):
                if (cfg.execution_mode == "brainstorm"
                        and plan.mode_signal == "exit_brainstorm"):
                    log.info("Brainstorm mode: ignoring the planner's exit_brainstorm; the release "
                             "judge owns the exit while the latch is held.")
                else:
                    mode_signal_detected = plan.mode_signal
                    # A planner mode signal may ENGAGE the gate, never clear the user's own veto.
                    brainstorm_active = (hold_off_active
                                         or plan.mode_signal == "enter_brainstorm")
                    try:
                        emit.emit(ProgressEvent(type=EVENT_MODE_SIGNAL, step=steps,
                                                data={"signal": mode_signal_detected,
                                                      "execution_mode": cfg.execution_mode}))
                    except Exception:  # noqa: BLE001 — reporting must never break the turn
                        pass

            # BRAINSTORM GATE: acting AND escalating are both unavailable while the latch is held.
            # If the planner still chose "deep", "confirm" or "clarify" despite the prompt note,
            # degrade to "answer" with everything gathered so far (same fail-safe style as a planner
            # failure above): the turn keeps its full context and produces a grounded reply, it just
            # does not act. "clarify" is included because it surfaces its question through the
            # ESCALATION SINK (a real decision-request in a consumer), which would park a pending ask
            # on a conversation the user explicitly put on hold. Its question is not lost: it rides
            # into the reply text instead (see brainstorm_clarify_question below).
            if plan and brainstorm_active and plan.action in ("deep", "confirm", "clarify"):
                log.info("Brainstorm mode: degrading planner action %r to 'answer'.", plan.action)
                brainstorm_suppressed_action = plan.action
                if plan.action == "clarify":
                    brainstorm_clarify_question = (
                        _clarify_question_text(plan) or brainstorm_clarify_question)
                plan.action = "answer"
            # A tool that ACTS is an action like any other: held while the latch is. A read-only
            # tool is just more gathered context and still runs.
            if (plan and brainstorm_active and plan.action == "tool" and self.tools is not None
                    and any((self.tools.get(c["name"]) is None
                             or self.tools.get(c["name"]).mutates) for c in plan.tool_calls)):
                log.info("Brainstorm mode: holding the planner's tool call(s) %s.",
                         [c["name"] for c in plan.tool_calls])
                brainstorm_suppressed_action = brainstorm_suppressed_action or "tool"
                plan.action = "answer"

            # --- STRUCTURAL SUFFICIENCY GATE: never answer ABOUT a summary you never opened ------
            # The prose SUFFICIENCY gate in the planner prompt asks the model to check, before
            # answering, that it has READ (not merely located) what it is about to answer from.
            # Nothing enforced it, and a real turn answered from a card item holding a short
            # synthesized SUMMARY of a note, spending its reply saying it had "only the header" and
            # asking whether it should go and fetch the rest. This is that check, made structural:
            # the item itself declared (at capture time) that its stored text is abridged and named
            # the read spec that fetches the full source, so "was the full text actually pulled?" is
            # a fact about which read specs RAN this turn, not a reading of anything the model said
            # (hard rule #3). Fires at most ONCE per turn: it closes that hole, it does not give the
            # loop a new way to spin. Inert when no item declares a fetch spec.
            if plan and plan.action == "answer" and cfg.full_read_before_answer:
                try:
                    _to_fetch = abridged_state.should_force_read()
                except Exception:  # noqa: BLE001 -- the gate must never break the loop
                    _to_fetch = []
                if _to_fetch:
                    abridged_state.forced = True
                    _forced_reads = [dict(it.fetch) for it in _to_fetch[: cfg.max_reads_per_step]]
                    log.info(
                        "Sufficiency gate: the plan would answer from %d abridged context item(s) "
                        "whose full text was never fetched; forcing a read step first (%s).",
                        len(_forced_reads), ", ".join(it.label for it in _to_fetch)[:200])
                    plan.action = "read"
                    plan.reads = _forced_reads
                    emit.status("Pulling the full text before answering…")

            # Safety gate: if planner chose "read" for many consecutive steps, force a terminal action
            if plan and plan.action == "read":
                consecutive_reads += 1
                # Escalate to deep if: max_consecutive_reads+ consecutive reads
                if consecutive_reads >= cfg.max_consecutive_reads:
                    log.warning(
                        f"Planner stuck in read loop after {steps} steps / {consecutive_reads} reads. "
                        f"Force-escalating to {'answer' if brainstorm_active else 'deep'} with gathered context."
                    )
                    if brainstorm_active:
                        # Brainstorm may not act: wrap up with a grounded answer instead.
                        plan.action = "answer"
                    else:
                        plan.action = "deep"
                        plan.goal = plan.goal or f"Complete the request: {user_message}"
                        plan.deep_brief = plan.deep_brief or user_message
            else:
                consecutive_reads = 0  # Reset when planner chooses something else
            # When narrating, the planner's `rationale` is ALREADY the user-facing spoken beat for
            # this step (written conversationally, in the rep's voice). We relay it through the same
            # narration channel as the instant ack (a partial), so the plan/replan event itself
            # carries no duplicate text. When not narrating, the plan event keeps the terse rationale
            # as its expandable detail (legacy behavior).
            emit.emit(ProgressEvent(type=(EVENT_PLAN if step == 0 else EVENT_REPLAN),
                                    action=plan.action, step=steps,
                                    text=(None if narrator.enabled else (plan.rationale or None))))
            # Relay this planner decision as the next beat of the train of thought, but only for
            # actions that involve a real wait (gathering context, deep work) so quick answers add
            # no latency. Zero extra LLM call: the beat is the planner's own conversational rationale.
            if narrator.enabled and plan.action in ("read", "deep"):
                narrator.relay(plan.rationale)
            # Emit cumulative token counts so live consumers see usage grow in real time.
            _ti = getattr(self.provider, 'tokens_in', 0)
            _to = getattr(self.provider, 'tokens_out', 0)
            if _ti or _to:
                emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                        data={"tokens_in": _ti, "tokens_out": _to,
                                              "total": _ti + _to}))

            # --- OVERSEER hook A submit (in-loop, NON-BLOCKING): fire a fresh minimal-intervention
            # consult for THIS plan into a background thread, then keep walking (Fix 1). Its result
            # is polled at the top of the NEXT step (above), one step late. We only submit when the
            # plan is a "read" (the loop will continue, so there IS a next step to apply a signal to)
            # and when no consult is already in flight (one at a time). Gated so it stays cheap:
            # cadence, min-step, a hard cap on total submissions shared with hook B, AND (Fix 12) a
            # free non-LLM pre-filter inside ``_submit_oversee`` (``gate=True``, the default) that
            # skips the consult entirely unless something actually looks worth a look. Wrapped so it
            # can NEVER break the loop. Off by default.
            if (cfg.overseer and plan.action == "read" and pending_oversee is None
                    and steps >= cfg.overseer_min_step
                    and steps % max(1, cfg.overseer_every_steps) == 0
                    and overseer_submitted < cfg.overseer_max_signals):
                try:
                    if _oversee_executor is None:
                        _oversee_executor = ThreadPoolExecutor(max_workers=1)
                    # Fix 3/4: judge the run against the RAW request (user_message) AND the RESOLVED
                    # request (goal_condition, when it differs) plus the completion bar
                    # (quality_standards). Fix 5a/7: also carry this-conversation history.
                    _prev_sig = ((_prev_plan_for_gate.action, _prev_plan_for_gate.goal)
                                if _prev_plan_for_gate is not None else None)
                    pending_oversee = self._submit_oversee(
                        _oversee_executor, user_message=user_message, goal_condition=goal_condition,
                        step=steps, plan=plan, gathered=gathered, started=started,
                        quality_standards=quality_standards,
                        recent_conversation=_recent_conversation_digest_lines,
                        prior_escalations=_prior_escalation_digest_lines,
                        prev_plan_signature=_prev_sig)
                    if pending_oversee is not None:
                        overseer_submitted += 1
                except Exception:  # noqa: BLE001 — the overseer must never break the loop
                    pass

            if plan.action == "tool":
                self.run_tool_calls(plan.tool_calls, gathered=gathered, exec_record=exec_record,
                                     tool_ok_signatures=tool_ok_signatures, emit=emit,
                                     steps=steps, ctx=ToolContext(
                                         quest_id=quest_id,
                                         user_id=_ctx_meta.get("user_id"),
                                         task_id=_ctx_meta.get("task_id"),
                                         meta=dict(_ctx_meta)),
                                     on_success=self._recipe_learner(
                                         user_message, _ctx_meta.get("scope_tags"),
                                         learnable=message_is_user_turn))
                tool_calls_ran = True
                executed_reads.clear()
                repeat_only_steps = 0
                if budget_exhausted():
                    break
                continue
            if plan.action == "read":
                visible_from = planner_full_view_start(
                    len(gathered_content(gathered)), cfg.planner_recent_full,
                    cfg.planner_compress_over)
                fresh_reads, repeated_reads = split_repeated_reads(
                    plan.reads, {k: ran[0] for k, ran in executed_reads.items()
                                 if ran[1] >= visible_from})
                # The per-step cap _do_reads applies, applied here so a spec it would drop is
                # never recorded as having run.
                fresh_reads = fresh_reads[: cfg.max_reads_per_step]
                for spec, ran_at in repeated_reads:
                    gathered.append(repeated_read_observation(spec, ran_at))
                if repeated_reads and not fresh_reads:
                    repeat_only_steps += 1
                elif fresh_reads:
                    repeat_only_steps = 0
                if not plan.reads:
                    plan.action = "answer"
                elif not fresh_reads and repeat_only_steps >= 2:
                    # Nothing new can arrive: end the loop exactly as a spent read budget does, so
                    # the wrap-up below still honors a prepared hand-off and answers best-effort.
                    break
                elif not fresh_reads:
                    if budget_exhausted():
                        break
                    continue
                else:
                    if any(r.get("list_sources") or r.get("describe_source")
                           or r.get("list_operations") or r.get("describe_operation")
                           or r.get("list_guidance") or r.get("read_guidance")
                           for r in plan.reads):
                        emit.status("Exploring…")
                    elif any(r.get("web") is not None for r in plan.reads):
                        emit.status("Searching the web…")
                    elif any(r.get("web_page") for r in plan.reads):
                        emit.status("Reading a web page…")
                    else:
                        emit.status("Searching…" if any(r.get("grep") for r in plan.reads) else "Reading…")
                    content_before = len(gathered_content(gathered))
                    new_obs = self._do_reads(fresh_reads, guidance_selected_ids, card_context)
                    for obs in new_obs:
                        if isinstance(obs, dict) and obs.get("discovery"):
                            # The NEXT planner call (the re-plan for this step's own gather) is the
                            # one that reads it in full; every later step sees only a reminder.
                            obs["discovery_step"] = step + 1
                    gathered.extend(new_obs)
                    # Recorded only when each spec's own observation is known and is not an error:
                    # a failed or timed-out read stays retryable.
                    if len(new_obs) == len(fresh_reads):
                        for j, (spec, obs) in enumerate(zip(fresh_reads, new_obs)):
                            if isinstance(obs, dict) and obs.get("kind") != "error":
                                executed_reads[read_spec_key(spec)] = (steps, content_before + j)
                    for obs in new_obs:
                        if isinstance(obs, dict) and obs.get("planner_note"):
                            gathered.append(planner_note_observation(
                                str(obs["planner_note"]), "not_answered_by"))
                    # The turn's REAL record of what was fetched, for the sufficiency gate above:
                    # these specs actually executed, whoever chose them (the planner on its own, or
                    # the gate). Recorded here, at the one place reads run, so the gate can never be
                    # satisfied by a plan that merely mentioned a fetch.
                    abridged_state.record_reads(fresh_reads)
                    _sources: List[str] = []
                    for _o in new_obs:
                        if not isinstance(_o, dict):
                            continue
                        _kind = _o.get("kind", "")
                        _o_hits = _o.get("hits") or []
                        if _kind == "grep":
                            # Show matched file paths; on empty show a "(no matches)" marker
                            _seen_rp: set = set()
                            for _h in _o_hits:
                                _rp = _h.get("rel_path")
                                if _rp and _rp not in _seen_rp:
                                    _seen_rp.add(_rp)
                                    _sources.append(_rp)
                            if not _o_hits:
                                _pat = _o.get("pattern") or ""
                                if _pat:
                                    _sources.append(f"(searched {_pat!r} — nothing found)")
                        elif _o_hits and any(isinstance(_h, dict) and _h.get("url") for _h in _o_hits):
                            # A web search observation: show the RESULT URLs, not the synthetic
                            # "web_search:<query>" rel_path the adapter stamps on it.
                            _seen_u: set = set()
                            for _h in _o_hits:
                                _u = _h.get("url") if isinstance(_h, dict) else None
                                if _u and _u not in _seen_u:
                                    _seen_u.add(_u)
                                    _sources.append(_u)
                        else:
                            # A web PAGE fetch has no hits and ``rel_path`` is the fetched URL
                            # itself, so it falls through here unchanged.
                            _rp = _o.get("rel_path") or _o.get("pattern")
                            if _rp:
                                _sources.append(_rp)
                    emit.emit(ProgressEvent(type=EVENT_READ, step=steps,
                                            data={"reads": len(fresh_reads),
                                                  "sources": _sources[:8]}))
                    if budget_exhausted():
                        break
                    continue
            if plan.action in ("answer", "deep", "confirm", "clarify"):
                break
        else:
            plan = plan or PlanDecision(action="answer")

        final = (plan or PlanDecision(action="answer")).action
        # A turn that ENDED on a tool step (the budget ran out right after the calls ran) or on a
        # read after its tool calls already ran has done its work: it answers with the receipts in
        # ``gathered``. It must not fall into the read-budget wrap-up below, whose change-request
        # net would otherwise escalate "send the email" to a deep run after the email was sent.
        if final == "tool" or (final == "read" and tool_calls_ran):
            plan.action = final = "answer"

        # --- BRAINSTORM TERMINAL GATE (the single choke point) --------------------------------
        # Whatever set the action -- the planner, the read-loop safety escalation, an overseer
        # signal -- a latched turn may neither ACT nor ESCALATE. Every terminal path below funnels
        # through here, so the invariant ("a latched turn executes nothing and creates no
        # decision-request") holds structurally instead of depending on each path remembering to
        # check the latch. "clarify"/"confirm" carry a question the user still deserves to hear, so
        # it is not dropped: it rides into the reply text via the acknowledgment note below.
        if brainstorm_active and final in ("deep", "confirm", "clarify"):
            log.info("Brainstorm mode: degrading terminal action %r to 'answer'.", final)
            brainstorm_suppressed_action = brainstorm_suppressed_action or final
            if final == "clarify":
                brainstorm_clarify_question = (_clarify_question_text(plan)
                                               or brainstorm_clarify_question)
            elif final == "confirm" and plan.confirm_question:
                brainstorm_clarify_question = (brainstorm_clarify_question
                                               or plan.confirm_question.strip())
            plan.action = final = "answer"

        # BRAINSTORM NO-ACTION ACKNOWLEDGMENT (zero extra LLM calls): while the latch is held, the
        # escalation nets are suppressed wholesale, so a held turn would otherwise end with a reply
        # that never says nothing ran (or, worse, improvises that it is about to run). The note is
        # computed ONCE, HERE, before ANY reply-producing path runs, and passed as the
        # ``reply_directive`` of EVERY reply generator below (the read-budget wrap-up, the main
        # answer, the sub-question answerer, and every regeneration, all of which run through
        # ``_gen_answer``). It lands on the answering call's SYSTEM prompt, next to the rest of the
        # reply contract, which is the layer an instruction the reply must OBEY belongs in.
        #
        # Both halves of that are the fix for a real failure. It used to be folded into the answer
        # GROUNDING at the main answer only, so (a) turns that returned earlier (clarify, the
        # read-budget wrap-up) shipped replies that had never been told the turn was held, and
        # (b) even when it did reach the model it sat inside a block introduced as "answer FROM
        # this, never mention that you read it", the wrong frame for an instruction to speak up.
        # It rides on every latched turn, not only ones an intent detector flags (see the note's own
        # comment: the detector missed exactly the phrasings that needed it most), and the two extra
        # steers below are added when the turn ALSO wanted to act or to ask.
        brainstorm_ack_note: Optional[str] = None
        if brainstorm_active:
            # Which reason held the turn decides the wording: the user's own veto is not a mode to
            # explain to them, it is an instruction that was followed.
            user_vetoed = hold_off_active and cfg.execution_mode != "brainstorm"
            brainstorm_ack_note = (HOLD_OFF_NO_ACTION_ACK_NOTE if user_vetoed
                                   else BRAINSTORM_NO_ACTION_ACK_NOTE)
            planner_tried_to_act = (brainstorm_suppressed_action in ("deep", "confirm", "tool")
                                    or bool(plan.deferred_deep)
                                    or bool(plan.answer_contains_work_to_execute))
            if planner_tried_to_act:
                brainstorm_ack_note += (HOLD_OFF_HELD_WORK_ACK_NOTE if user_vetoed
                                        else BRAINSTORM_HELD_WORK_ACK_NOTE)
            if brainstorm_clarify_question:
                brainstorm_ack_note += (BRAINSTORM_CLARIFY_ACK_PREFIX
                                        + brainstorm_clarify_question.strip() + "\n")

        # THE REPLY DIRECTIVE for every reply generator this turn (it lands on the answering call's
        # SYSTEM prompt, next to the reply contract, which is where an instruction the reply must
        # OBEY belongs). Two things can ride it, and they compose:
        #   * the brainstorm no-action acknowledgment (above), when the latch is held; and
        #   * the CARD LIFECYCLE gate, whenever per-idea threading is on. A card outlives the work
        #     it describes, so a COMPLETED quest or a finished project can surface as context on any
        #     turn. Without this the assistant reads it as live work and starts proposing how to do
        #     it. The gate says: treat finished work as knowledge you may cite and build on, never
        #     as open work, unless the user explicitly reopens it.
        reply_directive: Optional[str] = brainstorm_ack_note
        if cfg.card_thread_enabled:
            reply_directive = (CARD_LIFECYCLE_GATE + "\n\n" + brainstorm_ack_note
                               if brainstorm_ack_note else CARD_LIFECYCLE_GATE)

        def _answer_grounding(steering: Optional[str] = None) -> str:
            # The L2 grounding EVERY reply this turn is built on.
            cv = context_view
            if steering:
                cv = ((context_view + "\n\n" if context_view else "")
                      + "--- IMPROVE YOUR ANSWER (it did not yet meet the goal) ---\n" + steering)
            # Forward the planner's reasoning so the answerer benefits from what the planner
            # already worked out. Without this the answerer re-derives from scratch and may
            # reach a different (wrong) conclusion, e.g. "no prior history" despite the planner
            # having already found and summarised the conversation history correctly.
            if plan.rationale and plan.action == "answer":
                planner_block = f"--- PLANNER ANALYSIS ---\n{plan.rationale}"
                cv = (cv + "\n\n" + planner_block if cv else planner_block)
            return cv

        # Cap/budget fallback: still in read mode -> best-effort answer or escalate to deep.
        # In brainstorm mode escalation is unavailable, so a budget-capped turn always wraps up
        # with a best-effort grounded answer (even with nothing gathered) instead of acting -- and
        # it grounds through ``_answer_grounding``, so it carries the no-action acknowledgment too.
        # A CHANGE REQUEST never wraps up with words when escalation is available: a budget-capped
        # "do X" answered with "here is what the system would need to do" used to be reported done
        # with nothing executed (caught live by the 2026-07-19 reliability battery: a write-a-file
        # probe answered "I cannot execute this task in the read-and-answer step" and PATCHed done,
        # bypassing the _answer_describes_unexecuted_work net below, which only guards the normal
        # answer path). Requests for work escalate to deep instead.
        #
        # "Is this a request for work?" is answered in three steps, cheapest first (live
        # 2026-09-27: a reply on a bug-fix thread reporting two new bugs was diagnosed correctly,
        # then wrapped up as a best-effort answer and marked done, and nothing was fixed until the
        # reporter said "yes, fix it"):
        #   1. the PLANNER already prepared deep work (``deferred_deep`` or a ``deep_brief`` on its
        #      last read step: it was reading "to ground a brief before escalating"). Honoring its
        #      own structured decision, never keywords in its text;
        #   2. the planner's structured ``user_intent`` verdict on the user's message ("act");
        #   3. no verdict at all: the same one-shot LLM judgment the answer path uses.
        # A "hold_off" verdict counts only for a message typed this turn: a queued task's brief is
        # machine-composed and quotes earlier runs' output, and the decision to act on it was
        # already taken when the task was created. On a brief it is treated as no verdict.
        nets_intent = (None if (turn_user_intent == USER_INTENT_HOLD_OFF and not nets_honor_hold_off)
                       else turn_user_intent)
        if final not in ("answer", "deep", "confirm", "clarify"):
            must_execute = False
            last_plan = plan or PlanDecision(action="answer")
            if not brainstorm_active:
                planner_prepared_deep = bool(last_plan.deferred_deep
                                             or (last_plan.deep_brief or "").strip())
                must_execute = planner_prepared_deep or user_ordered_work(nets_intent)
                if (not must_execute and nets_intent is None
                        and (self._has_deep_execution_capability()
                             or self._has_deferred_queue_capability())):
                    must_execute, _why = self.judge_execution_directive(user_message, "")
                    log.info("Read budget spent; intent judgment on the request: %s (%s).",
                             must_execute, _why)
                if must_execute and last_plan.deferred_deep:
                    last_plan.goal = last_plan.goal or last_plan.deferred_deep.get("goal")
                    last_plan.deep_brief = (last_plan.deep_brief
                                            or last_plan.deferred_deep.get("brief"))
            # A planner_only note (a repeated read) is not something gathered.
            if (gathered_content(gathered) or brainstorm_active) and not must_execute:
                emit.status("Wrapping up with a best-effort answer…")
                model = self._answer_model(plan, "balanced", hint=model_hint)
                # The wrap-up reply ENDS the turn, and the answerer is not otherwise told so: a
                # 2026-08-24 dissertation run closed with "What I'm doing now: spec all nine
                # gaps..." and was reported done having specced none, because a budget-capped
                # answer reads like any other answer from inside. The honesty note says the turn
                # is over, so what is left is named as left rather than promised.
                wrapup_directive = (reply_directive + "\n\n" + READ_BUDGET_WRAPUP_HONESTY_NOTE
                                    if reply_directive else READ_BUDGET_WRAPUP_HONESTY_NOTE)
                text = self._grounded_answer(user_message, transcript, _answer_grounding(), gathered,
                                             model, True, native_blocks=native_blocks,
                                             rep_preamble=rep_preamble,
                                             reply_directive=wrapup_directive)
                return finish(OrchestratorResult(kind="answer", text=text, rationale=plan.rationale,
                                                 partial=True, model=model,
                                                 exit_reason="read_budget"))
            plan = last_plan
            plan.action = final = "deep"
            plan.goal = _truncate_goal(plan.goal or f"Fully address the request: {user_message}")
            plan.deep_brief = plan.deep_brief or user_message
            if gathered_content(gathered):
                emit.status("This asks for a change, so doing the work instead of wrapping up "
                            "with an answer…")

        # A planner-originated "confirm" is HONORED as a confirm (it surfaces below via
        # _run_confirm). QAR does not re-route or auto-execute a confirm by inspecting keywords in
        # the planner's confirm question — that brittle keyword gate was removed (see the note by
        # clip_head_and_tail). If the planner over-confirms, fix the planner, not this.

        if final == "clarify":
            # User clarification/selection needed: surface as decision-request
            res = self._run_clarify(plan, quest_id=quest_id, emit=emit)
            res.exit_reason = "clarify"
            return finish(res)

        if final == "deep":
            # Announce the goal condition before executing — but ONLY when something can actually
            # execute it. Two things were wrong here and both produced the same live failure (a
            # turn whose final bubble read "Executing: <goal>" although nothing had run, no file
            # was touched, and a dim side note said "No deep executor configured"):
            #   1. the announcement fired unconditionally, including on runs with no execution
            #      capability at all, where _run_deep below returns immediately having done nothing;
            #   2. it was typed EVENT_RESULT, the type that carries a turn's actual outcome, so
            #      both chat UIs' "fall back to the last result text" path picked this interim
            #      sentence up and presented it as the answer.
            # It is now gated on real capability and typed EVENT_INTENT, which no renderer may use
            # as a turn's outcome. The gate mirrors _run_deep's own (``_has_deep_execution_capability``
            # covers the single default runner AND the named registry), so the announcement fires
            # exactly when the work is genuinely about to be attempted.
            if self._has_deep_execution_capability():
                goal_text = plan.goal or f"Complete: {user_message[:100]}"
                emit.emit(ProgressEvent(type=EVENT_INTENT, text=f"Executing: {goal_text}"))
                emit.status("Running now…")
            res = self._run_deep(plan, user_message, self._answer_model(plan, "balanced", hint=model_hint),
                                 emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                                 gathered=gathered, quality_standards=quality_standards,
                                 pending_inputs=pending_inputs, model_hint=model_hint,
                                 ctx_meta=_ctx_meta, cancel_check=cancel_check,
                                 working_dir_override=working_dir_override,
                                 resume_session_id=take_resume_session())
            if res.kind == "cancelled":
                return finish(res)
            res.exit_reason = "deep_met" if (res.deep_results and all(d.met for d in res.deep_results)) else "deep_not_met"
            if overseer_decided == "overseer_escalated_deep":
                res.exit_reason = "overseer_escalated_deep"
            if not attachments:
                self._recipe_learn_deep(user_message, plan, res, _ctx_meta.get("scope_tags"),
                                        learnable=message_is_user_turn and mode is Mode.LIVE)
            # Background: categorize edited files into context cards (deep runner returns edited_files in metadata)
            if res.deep_results and any(dr.met for dr in res.deep_results):
                self._update_context_cards_after_deep(res, context_meta)
            # Background (ASYNC, best-effort): prepare reusable context for this user's NEXT similar
            # request by updating their cards from this run. Off the result path; never blocks finish.
            self._kickoff_card_update(res, plan, user_message, _ctx_meta, emit,
                                      gathered=gathered)
            return finish(res)

        if final == "confirm":
            res = self._run_confirm(plan, quest_id=quest_id)
            # A confirm can be planner-originated (rare) or come from an OVERSEER escalate_human
            # signal (Fix 2); distinguish the exit_reason so a caller/consumer can tell the two apart
            # (e.g. for building next-turn's ``prior_escalations``).
            res.exit_reason = ("overseer_escalated_human" if overseer_decided == "overseer_escalated_human"
                               else "confirm")
            return finish(res)

        # answer
        model = self._answer_model(plan, "sonnet", hint=model_hint)
        # Tell the task feed which tier wrote this reply and the model it resolved to.
        reply_tier = model_hint or plan.model_tier or "sonnet"
        emit.emit(ProgressEvent(
            type=EVENT_STATUS,
            text=f"Replying with the {reply_tier} tier ({model}).",
            data={"response_model": {"tier": reply_tier, "model": model}}))

        def _gen_answer(steering: Optional[str]) -> str:
            # Produce an answer, optionally STEERED by goal-verification feedback (the prior answer
            # plus why it fell short + what to fix) folded into the grounding context. EVERY reply
            # this turn produces (including each regeneration) goes through here or the read-budget
            # wrap-up above, and both pass ``brainstorm_ack_note`` as the reply directive, so a
            # latched turn cannot ship a reply that was never told the turn was held.
            cv = _answer_grounding(steering)
            if len(plan.subquestions) >= 2:
                return self._answer_subquestions(user_message, transcript, cv, gathered,
                                                 model, plan.subquestions, native_blocks=native_blocks,
                                                 reply_directive=reply_directive,
                                                 rep_preamble=rep_preamble)
            return self._grounded_answer(user_message, transcript, cv, gathered, model,
                                         False, native_blocks=native_blocks,
                                         rep_preamble=rep_preamble,
                                         reply_directive=reply_directive)

        emit.status(f"Answering {len(plan.subquestions)} parts in parallel…"
                    if len(plan.subquestions) >= 2 else "Answering")
        text = _gen_answer(None)
        _ti = getattr(self.provider, 'tokens_in', 0)
        _to = getattr(self.provider, 'tokens_out', 0)
        if _ti or _to:
            emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                    data={"tokens_in": _ti, "tokens_out": _to, "total": _ti + _to}))

        # --- OVERSEER hook B (answer checkpoint): one last minimal-intervention watch, now with the
        # DRAFT answer in the digest. It can escalate_deep (this is not really an answer, it needs
        # real execution), escalate_human (a genuine human-only fork), or redirect (regenerate the
        # answer once with a one-line steering hint). proceed / answer_now accept the draft. Counted
        # against the same per-run cap as hook A. Wrapped so it can never break the turn (any failure
        # degrades to accepting the draft). ON by default. Not gated by the Fix-12 cadence heuristic
        # (``gate=False``): this is a one-time final check, not a cadence, so it always consults.
        #
        # DESIGN NOTE (hook B's bounded wait): hook B originally WAITED synchronously before shipping;
        # Fix 11 made it fully non-blocking (reusing hook A's 0.0 poll), which cost it its authority
        # -- no provider call resolves in zero seconds, so the draft always won the race and the
        # verdict landed too late to change it. It now waits its OWN small bounded
        # ``overseer_answer_checkpoint_timeout_seconds`` (0.0 restores the Fix-11 behavior): a quick
        # judge corrects the same answer it is judging, and a slow one still SHIPS THE DRAFT NOW
        # and hands the pending consult to ``_finish_oversee_in_background`` instead of blocking. That
        # background finisher raises a REAL decision-request for a late ``escalate_human`` (durable,
        # reaches the human regardless of the stream) and records a late ``EVENT_OVERSEER`` for
        # ``redirect``/``escalate_deep`` (best-effort telemetry for a consumer to fold into next
        # turn's ``prior_escalations``); it deliberately does NOT auto-launch a new deep execution
        # with no one left to receive its result. See ``_finish_oversee_in_background``'s docstring
        # and docs/overseer.md for the full tradeoff. A FAST-resolving consult (the common case for a
        # quick model) still corrects the SAME answer before it ships, exactly as before.
        if cfg.overseer and overseer_submitted < cfg.overseer_max_signals:
            try:
                if _oversee_executor is None:
                    _oversee_executor = ThreadPoolExecutor(max_workers=1)
                # Fix 3/4: the raw request (user_message) AND the resolved request (goal_condition)
                # + completion bar + this-conversation history.
                _bpending = self._submit_oversee(
                    _oversee_executor, user_message=user_message, goal_condition=goal_condition,
                    step=steps, plan=plan, gathered=gathered, started=started, draft_answer=text,
                    quality_standards=quality_standards,
                    recent_conversation=_recent_conversation_digest_lines,
                    prior_escalations=_prior_escalation_digest_lines,
                    gate=False)
                _bsig = OverseerSignal("proceed")
                if _bpending is not None:
                    overseer_submitted += 1
                    _collected = self._collect_oversee(
                        _bpending, signals=overseer_signals, emit=emit,
                        timeout=cfg.overseer_answer_checkpoint_timeout_seconds)
                    if _collected is not None:
                        _bsig = _collected
                    else:
                        # Not resolved yet: ship the draft now; a late resolution is handled async.
                        self._finish_oversee_in_background(
                            _bpending, emit=emit, quest_id=quest_id,
                            brainstorm_active=brainstorm_active)
                if brainstorm_active and _bsig.signal in ("escalate_deep", "escalate_human"):
                    # Brainstorm mode: overseer escalations may not ADD actions; ship the draft.
                    pass
                elif _bsig.signal == "escalate_deep" and self._has_deep_execution_capability():
                    if emit is not None:
                        emit.status("Overseer: this needs real execution, running it now…")
                    _ov_plan = PlanDecision(
                        action="deep",
                        goal=_truncate_goal(plan.goal or f"Carry out the user's request: {user_message}"),
                        deep_brief=(user_message)[:2000],
                        rationale="overseer escalated the answer to deep execution",
                        deep_difficulty=getattr(plan, "deep_difficulty", None),
                        deep_difficulty_reason=getattr(plan, "deep_difficulty_reason", None),
                    )
                    _ov_model = self._answer_model(_ov_plan, "balanced", hint=model_hint)
                    ov_facts_before = len(getattr(exec_record, "facts", None) or [])
                    _ov_res = self._run_deep(
                        _ov_plan, user_message, _ov_model,
                        emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                        gathered=gathered, quality_standards=quality_standards,
                        pending_inputs=pending_inputs, model_hint=model_hint,
                        ctx_meta=_ctx_meta, cancel_check=cancel_check,
                        working_dir_override=working_dir_override,
                        resume_session_id=take_resume_session(), self_initiated=True,
                        user_ordered=user_ordered_work(turn_user_intent))
                    if _ov_res.kind == "cancelled":
                        return finish(_ov_res)
                    if not own_escalation_adds_nothing(_ov_res, exec_record, ov_facts_before):
                        _ov_res.exit_reason = "overseer_escalated_deep"
                        self._kickoff_card_update(_ov_res, _ov_plan, user_message, _ctx_meta,
                                                  emit, gathered=gathered)
                        return finish(_ov_res)
                    # Not started (background work the user did not ask for) or it produced
                    # nothing: the draft answer ships.
                    log.info("overseer escalation added nothing; keeping the draft answer")
                    if emit is not None:
                        emit.status(OWN_ESCALATION_KEPT_STATUS)
                elif _bsig.signal == "escalate_human":
                    # Genuine human-only fork (Fix 2): route through the SAME confirm / decision-
                    # request mechanism as a planner-originated confirm, discarding the drafted
                    # answer (mirrors the pre-existing escalate-to-deep precedent just above, which
                    # also discards the draft in favor of the correct terminal path).
                    if emit is not None:
                        emit.status("Overseer: this needs your input before I can finish…")
                    _confirm_plan = PlanDecision(
                        action="confirm",
                        confirm_question=(_bsig.reason
                                          or f"This needs your input before I continue: {user_message}"),
                        rationale="overseer escalated the answer to a human decision",
                    )
                    _confirm_res = self._run_confirm(_confirm_plan, quest_id=quest_id)
                    _confirm_res.exit_reason = "overseer_escalated_human"
                    return finish(_confirm_res)
                if _bsig.signal == "redirect" and _bsig.hint:
                    if emit is not None:
                        emit.status("Overseer: refining the answer…")
                    _steer = (f"A reviewer flagged this for one correction: {_bsig.hint}\n\n"
                              f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                    text = _gen_answer(_steer)
            except Exception:  # noqa: BLE001 — the overseer must never break the turn
                pass

        # If deferred_deep is set, also run the deep task now, synchronously, right after the
        # answer text is produced (same turn; nothing is saved for later)
        # OR if planner explicitly flagged answer_contains_work_to_execute
        # OR auto-detect false claims (fallback for broken prompts)
        # BRAINSTORM MODE: this entire block is an escalation net (it can only ADD execution to
        # an answer turn), so while the latch is held it is skipped wholesale: no deferred deep,
        # no work-to-execute flag, no described-work net, no message-intent fallback (planner
        # verdict OR LLM judgment). Describing possible work IS the product in brainstorm.
        should_defer_deep = None if brainstorm_active else plan.deferred_deep
        # Capability for these nets = inline execution OR a wired deferred queue: everything they
        # can set flows through the deferred block below, which reaches the queue runner by explicit
        # override, so a queue-only consumer (no default runner, no classifier) is capable here.
        # Every net below can only ADD execution to an answer turn, so the human telling us to hold
        # off ("don't create a task", "just answer here", "I haven't given you an instruction yet",
        # "kill those runs") turns the whole block off. That is the planner's structured
        # ``user_intent == "hold_off"`` verdict on a typed message (``nets_intent``), never a
        # keyword reading: those messages carry change verbs, and the keyword net that preceded
        # this escalated them most reliably of all.
        if (not should_defer_deep and not brainstorm_active
                and nets_intent != USER_INTENT_HOLD_OFF
                and (self._has_deep_execution_capability()
                     or self._has_deferred_queue_capability())):
            # Primary: trust planner's explicit flag
            if plan.answer_contains_work_to_execute:
                should_defer_deep = {"goal": f"Execute what the answer describes: {user_message}",
                                      "rationale": "planner indicated answer contains work to execute"}
                if emit is not None:
                    emit.status("Executing described work now…")
            # Fallback: the answer DESCRIBES executable work it never did ("I need to update X",
            # "to fix this I need to..."). The cheap planner frequently forgets to set
            # answer_contains_work_to_execute on code/file-change tasks, so without this net the
            # turn ends having only TALKED about the fix instead of doing it (the "it just finishes
            # the request" regression). Re-wired here so a described-but-unexecuted fix still
            # escalates to a deep run that actually applies it -- but ONLY when the planner's
            # verdict says the user's own message ordered work (``user_intent == "act"``), never
            # for a genuine question ("why is X broken?", "what would it take to fix Y?").
            # Explaining what a fix would involve IS the correct answer to a question; describing
            # it must never silently open a task.
            elif _answer_describes_unexecuted_work(text) and user_ordered_work(nets_intent):
                should_defer_deep = {"goal": f"Execute the work the answer describes: {user_message}",
                                      "rationale": "auto-detected unexecuted work in answer (fallback)"}
                if emit is not None:
                    emit.status("Executing described work now…")
            # Decisive fallback: the planner's own verdict says the user ORDERED work
            # (``user_intent == "act"``), a deep runner is available, yet the planner routed to
            # "answer" and nothing executed this turn, so an actionable request would silently end
            # as a proposal. The brief carries the assistant's proposed approach so the deep run
            # APPLIES it rather than re-deriving. (Executing here is fine even when the answer
            # falsely claims completion: the goal verification below re-checks the folded post-deep
            # answer against the execution record.)
            #
            # This honors a STRUCTURED field the planner fills on the call it already makes, so it
            # costs nothing. Only when no planner step gave a usable verdict does ONE structured LLM
            # judgment step in (``judge_execution_directive``), hard-timeout-guarded and falling
            # back to "no directive" on any failure. There is no keyword reading of the message here
            # any more: the regex net it replaced leaked every phrasing nobody had listed.
            elif exec_record is None or not exec_record.any_mutation_attempted:
                # A turn that just RELEASED the brainstorm hold is a directive by definition: the
                # release judge only says true when the user told us to stop holding back and act on
                # what was discussed. The work itself usually lives in the transcript, not in that
                # short message ("go ahead"), so the turn would otherwise end with one more proposal
                # (and, worse, a reply claiming it had acted).
                _is_directive = brainstorm_released_this_turn or user_ordered_work(nets_intent)
                _directive_reason = ("brainstorm release: the user lifted the hold and told us to act"
                                     if brainstorm_released_this_turn
                                     else "message-intent fallback (planner user_intent)")
                if not _is_directive and nets_intent is None:
                    _is_directive, _llm_reason = self.judge_execution_directive(user_message, text)
                    _directive_reason = f"message-intent fallback (LLM judgment: {_llm_reason})"
                if _is_directive:
                    log.info("Escalating answer->deep: user message requests a change but the turn "
                             "only produced a proposal; running it now (%s).", _directive_reason)
                    _proposal = (text or "").strip()
                    _brief = user_message if not _proposal else (
                        f"{user_message}\n\nThe assistant proposed this approach. APPLY it (make the "
                        f"actual code/file/data changes, do not just describe them):\n{_proposal}"
                    )
                    should_defer_deep = {
                        "goal": f"Carry out the user's request: {user_message}",
                        "brief": _brief,
                        "rationale": "user message requests a change but turn only proposed it (message-intent fallback)",
                    }
                    if emit is not None:
                        emit.status("You asked for a change, making it now…")

        # True once a deferred deep run has produced substantive output that we folded back into the
        # final answer. Gates the post-deep goal-verification loop below so a deferred-deep turn is
        # still held to the overall goal (grounded in what the deep run actually produced).
        _deferred_deep_grounded = False
        # True once a QUEUED deployment's deferred hand-off is CONFIRMED (the deep runner enqueued
        # the work and returned a deferred met result). Gates the goal-verification loop below off
        # for this turn: the deferred contract trusts met and never re-verifies a hand-off sentinel
        # against the user's goal (that would always fail and could relaunch, double-enqueueing).
        _deferred_handoff_confirmed = False
        # True once this turn's deferred deep work resolved to a PARKED approval decision
        # (``DeepResult.decision_id``) with nothing landed. Gates the goal-verification loop off
        # for this turn, same reasoning as ``_deferred_handoff_confirmed``: the goal is
        # intentionally not met yet (a human still has to answer the card), so regenerating
        # toward "met" would only invite a rewrite that claims the parked change already happened.
        _decision_parked = False
        # True once this turn's deferred deep work neither landed nor parked (every run failed or
        # could not be verified), so the reply is the runs' own honest text. Gates the
        # goal-verification loop off, same reasoning as ``_decision_parked``.
        deferred_unconfirmed = False
        # The parked result's own decision_id, carried onto the final OrchestratorResult below so
        # a consumer can tell structurally (never by scanning the reply text) that this turn ended
        # on a pending approval card, not a completed answer.
        _parked_decision_id: Optional[str] = None
        # The queue pin is for the PLANNER'S OWN hand-off (``deferred_deep``): it decided the work
        # belongs in the background. The escalation nets above only infer that the user asked for
        # a change the turn did not make ("you asked for a change, making it now"), so their work
        # is routed like any deep action (the classifier picks inline or queue) instead of being
        # forced into the background queue. Found 2026-10-07 in Quest's chat: "work out how far
        # over budget I am ... then add a goal" ended in a background task for a goal the chat can
        # add inline in a second.
        _queued_mode = bool(self.cfg.deferred_deep_queued)
        # A queue-only wiring (no inline runner) still pins: the queue is the only place the
        # inferred work can go (settled: test_queue_only_wiring_can_still_hand_off).
        pin_queue = _queued_mode and (bool(plan.deferred_deep)
                                      or not self._has_deep_execution_capability())
        if should_defer_deep:
            try:
                if pin_queue:
                    emit.status("Handing this work to the background queue…")
                elif not plan.deferred_deep:
                    emit.status("Executing follow-up work…")
                else:
                    emit.status("Continuing with the follow-up work now…")
                deferred_plan = PlanDecision(
                    action="deep",
                    goal=_truncate_goal(should_defer_deep.get("goal") or f"Execute: {user_message}"),
                    deep_brief=(should_defer_deep.get("brief") or user_message)[:2000],
                    rationale=should_defer_deep.get("rationale") or "follow-up work from answer phase",
                    deep_difficulty=getattr(plan, "deep_difficulty", None),
                    deep_difficulty_reason=getattr(plan, "deep_difficulty_reason", None),
                )
                deep_model = self._answer_model(deferred_plan, "balanced", hint=model_hint)
                # Queued deployments pin deferred work to the registered queue runner (reserved
                # key), so the classifier can never re-route it to an inline runner.
                _deferred_runner = (self.deep_runners.get(DEFERRED_RUNNER_KEY)
                                    if pin_queue else None)
                # Announce the follow-up goal before executing it — same rules as the main deep
                # branch above: EVENT_INTENT (an announcement of intent, never usable as a turn's
                # outcome), and only when something can actually run it. The gate mirrors
                # _run_deep's exactly: a pinned ``runner_override`` IS the capability.
                if emit is not None and (_deferred_runner is not None
                                         or self._has_deep_execution_capability()):
                    _followup_verb = "Queueing" if pin_queue else "Executing"
                    emit.emit(ProgressEvent(type=EVENT_INTENT,
                                            text=f"{_followup_verb} follow-up: {deferred_plan.goal}"))
                deep_res = self._run_deep(deferred_plan, user_message, deep_model,
                                         emit=emit, rep_preamble=rep_preamble,
                                         exec_record=exec_record, gathered=gathered,
                                         quality_standards=quality_standards,
                                         pending_inputs=pending_inputs, model_hint=model_hint,
                                         ctx_meta=_ctx_meta, cancel_check=cancel_check,
                                         runner_override=_deferred_runner,
                                         working_dir_override=working_dir_override,
                                         resume_session_id=take_resume_session())
                if deep_res.kind == "cancelled":
                    return finish(deep_res)
                # WHAT ACTUALLY CAME BACK? Three outcomes, and the reply must match the one that
                # really happened this turn:
                #   * CONFIRMED HAND-OFF (queued deployments only): the runner did not execute the
                #     work, it queued it out-of-band and returned a receipt. Report it as queued,
                #     never as done; the real work is reported back into the conversation by the
                #     external runner when it finishes. A receipt is never folded through the
                #     after-deep prompt (that would present a queue acknowledgement as finished work).
                #   * REAL INLINE OUTPUT: the work RAN and produced something. Fold it back and
                #     report it, EVEN IN QUEUED MODE. That is exactly what happens when a queued
                #     consumer's ``deep_runners`` map lacks (or typos) DEFERRED_RUNNER_KEY: the
                #     hand-off pin resolves to nothing, the normal wiring executes the work for
                #     real, and telling that user "this was not queued" would be a lie about a turn
                #     that did the job, with the output dropped on the floor.
                #   * NEITHER: no receipt, no output. Only then may the turn say it is not queued.
                _results = deep_res.deep_results or []
                # A hand-off is TRUSTED only when this deployment actually queues deferred work AND
                # the runner CONFIRMED the enqueue: deferred, met, no error, and a non-empty
                # receipt. A runner that reports met=True with deferred=True on a FAILED enqueue (or
                # returns an empty receipt) is not believed; that turn falls through to the honest
                # paths below rather than promising a queue entry that does not exist.
                _handoffs = ([d for d in _results if getattr(d, "deferred", False)]
                             if _queued_mode else [])
                _confirmed = [d for d in _handoffs
                              if d.met and not (d.error or "").strip() and (d.output or "").strip()]
                # Inline output = output from results that are NOT hand-off receipts. In queued mode
                # every deferred result is a receipt (trusted or not) and never counts as work
                # output; with the flag off there is no hand-off contract at all, so every result is
                # ordinary deep output, exactly as before.
                _inline_results = ([d for d in _results if not getattr(d, "deferred", False)]
                                   if _queued_mode else list(_results))
                # PARKED DECISION (round-2 regression, MS-046): a DeepResult with ``decision_id``
                # set was NOT executed, only parked on an approval card awaiting the person's
                # answer; its own ``output`` IS the user-facing ask, already written by the park
                # path (e.g. quest-backend's ``park_for_approval``) in the correct "awaiting your
                # answer" voice. Folding that text into ``deep_output`` fed the "you already DID
                # the work" synthesis prompt below, which could turn an honest ask into a false
                # "I have added this" claim, and the goal-verification loop after it then tried to
                # "improve" the honest ask into an executed one by telling a plain text-completion
                # step (no tool access) to "execute the create_goal operation" -- which it cannot
                # do, so it fabricated having done so. Keep every parked result OUT of
                # ``deep_output`` so nothing downstream can describe it as done; a mixed turn
                # (some work landed, one result parked) still reports the landed work normally and
                # appends the parked ask verbatim, never through the LLM resynthesis.
                _parked = next((d for d in _inline_results if getattr(d, "decision_id", None)),
                               None)
                # "Not parked" is not "landed" (review, 2026-10-07): only a result whose own
                # structured fields say its work landed (``result_landed_work``: met, and its
                # receipts do not say it changed nothing) may feed the "you already DID the work"
                # synthesis. A failed or unverified sibling is reported in its own words through
                # ``unconfirmed_no_change_text`` (which leads with the honest line when it both
                # failed and changed nothing), never rewritten into a done claim.
                landed_results = [d for d in _inline_results if result_landed_work(d)]
                unconfirmed_text = "\n\n".join(
                    s for s in (unconfirmed_no_change_text(d) for d in _inline_results
                                if not getattr(d, "decision_id", None)
                                and not result_landed_work(d)) if s
                ).strip()
                deep_output = "\n\n".join(
                    s for s in (_strip_future_context(d.output) for d in landed_results) if s
                ).strip()
                if _confirmed:
                    _handoff_out = "\n\n".join(
                        (d.output or "").strip() for d in _confirmed).strip()
                    if emit is not None:
                        emit.status("Queued. It will report back here when it finishes.")
                    text = self._synthesize_after_queued(
                        user_message, prior_answer=text, handoff_output=_handoff_out,
                        transcript=transcript, model=model, rep_preamble=rep_preamble)
                    _deferred_handoff_confirmed = True
                elif deep_output:
                    # FOLD THE DEEP OUTPUT BACK INTO THE FINAL ANSWER. The pre-deep `text` is a proposal
                    # ("shall I proceed?"); the real deliverable is what the deep run just produced. If we
                    # only emit it as a side milestone, the user-facing reply stays the stale proposal with
                    # no awareness of the work (and some consumers truncate a milestone to its first line),
                    # so the substance is lost. Re-synthesize the reply grounded in the deep output, ground
                    # the context_view in it too (so any goal-verification regeneration stays aware of it),
                    # and flag the turn so the goal loop below still holds it to the overall goal.
                    if _queued_mode:
                        # Queued mode, yet the work ran inline: the queue runner is not reachable
                        # (missing/typoed DEFERRED_RUNNER_KEY, or a runner that executed instead of
                        # enqueueing). Loud in the log, honest to the user: they get the real result.
                        log.warning(
                            "deferred_deep_queued is on but the deferred work ran INLINE and "
                            "produced output (no confirmed hand-off): check that deep_runners has a "
                            "runner under the %r key. Reporting the real inline result.",
                            DEFERRED_RUNNER_KEY)
                    if emit is not None:
                        emit.status("Writing up what was done…")
                    text = self._synthesize_after_deep(
                        user_message, prior_answer=text, deep_output=deep_output,
                        transcript=transcript, model=model, rep_preamble=rep_preamble)
                    _deep_block = "--- WHAT WAS JUST EXECUTED (deep run output) ---\n" + deep_output
                    context_view = (context_view + "\n\n" + _deep_block) if context_view else _deep_block
                    _deferred_deep_grounded = True
                    if unconfirmed_text and unconfirmed_text not in text:
                        # A sibling that did NOT land: its own honest text, verbatim, after the
                        # landed work and never through the synthesis above.
                        text = f"{text}\n\n{unconfirmed_text}"
                    if _parked is not None:
                        # Something ALSO parked alongside the landed work this turn: append its
                        # own code-written wording verbatim, never through the "already done"
                        # synthesis above, so the ask is neither dropped nor rewritten into a claim.
                        _parked_text = (_parked.output or "").strip()
                        if _parked_text and _parked_text not in text:
                            text = f"{text}\n\n{_parked_text}"
                        _decision_parked = True
                        _parked_decision_id = _parked.decision_id
                elif _parked is not None:
                    # NOTHING landed this turn: the whole deferred attempt resolved to a parked
                    # approval card. The reply IS that card's own code-written lead, verbatim --
                    # not a fresh LLM synthesis grounded on a misleading "you already DID the
                    # work" framing, which is what fabricated the false completion this guards.
                    if emit is not None:
                        emit.status("Parked on an approval card, waiting for your answer…")
                    _parked_text = (_parked.output or "").strip()
                    # What happened first (a sibling that did not land, in its own words), then
                    # the one ask.
                    reply_parts = [p for p in (unconfirmed_text, _parked_text) if p]
                    if reply_parts:
                        text = "\n\n".join(reply_parts)
                    _decision_parked = True
                    _parked_decision_id = _parked.decision_id
                elif unconfirmed_text:
                    # Nothing landed and nothing parked: the runs failed or could not be verified.
                    # Their own honest text IS the reply, verbatim; the goal-verification loop is
                    # skipped (``deferred_unconfirmed``) so no regeneration can rewrite a failure
                    # toward "done".
                    text = unconfirmed_text
                    deferred_unconfirmed = True
                elif _queued_mode:
                    # HONEST-ENQUEUE: this deployment queues deferred work, NO hand-off was
                    # confirmed this turn (the enqueue failed, or the run errored before it), and
                    # nothing ran inline either, so there is genuinely no completed work to report.
                    # The reply must not claim the work is queued, running, or done. Nothing here
                    # retries the enqueue: a silent second attempt could double-queue.
                    _handoff_err = "; ".join(
                        (d.error or "").strip() for d in _results
                        if (d.error or "").strip()) or "the hand-off did not complete"
                    if emit is not None:
                        emit.status("Could not queue the background work.")
                    try:
                        text = _gen_answer(
                            "The attempt to hand this work to the background queue FAILED: "
                            + _handoff_err + "\n\nRewrite your reply to be honest: the work has NOT "
                            "been queued, started, or done. Keep any real findings you already have, "
                            "say plainly that the background hand-off failed, and suggest the user "
                            "try again or ask you to retry. Do not use em dashes.\n\n"
                            f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                    except Exception:  # noqa: BLE001 — honesty must not depend on an LLM call
                        # DETERMINISTIC FLOOR. The regeneration is the only thing standing between
                        # the user and a pre-deep draft that (under queued doctrine) may already
                        # claim "I have queued this". If that call fails, we still correct the
                        # record without a model: append the plain not-queued sentence.
                        log.warning("honest-enqueue rewrite failed; appending the not-queued note",
                                    exc_info=True)
                        text = ((text or "").strip() + "\n\n" + NOT_QUEUED_NOTE).strip()
                # Async, best-effort: prepare this user's cards for next time from the deferred run.
                # Skipped for a confirmed hand-off: a queue receipt sentinel holds nothing to learn.
                if not _deferred_handoff_confirmed:
                    self._kickoff_card_update(deep_res, deferred_plan, user_message, _ctx_meta,
                                              emit, gathered=gathered)
            except Exception as e:  # noqa: BLE001 — deferred work must never break the answer
                log.warning(f"Deferred deep work failed: {type(e).__name__}: {e}", exc_info=True)

        # TOP-TIER GOAL VERIFICATION — the SAME goal loop, now applied to a plain ANSWER so EVERY
        # input is pursued as a goal. Hold the answer to the user's overall goal at the quality bar
        # and regenerate with steering (the prior answer + why it fell short + what to fix) until it
        # meets the bar or attempts run out. When the verifier says the answer lacks the context needed
        # to be definitive (need_more_context=True), escalate to deep so the deep runner can search
        # further — never accept "I couldn't find it" as a final answer when more searching is possible.
        # With ``verify_claims`` on, the SAME verdict also checks honesty: any change the answer claims
        # it completed must be backed by the turn's EXECUTION RECORD (claims_unexecuted). An unbacked
        # claim remediates here: execute the work for real via a deep run when nothing ran this turn
        # (safe, capped by max_remediations), else regenerate the reply to be honest about what
        # actually happened. Always runs — even when a deferred deep fired but produced no output (in
        # that case the pre-deep proposal still needs to be held to the goal bar). Best-effort: never
        # breaks the turn.
        # DEFERRED HAND-OFF EXCEPTION: when this turn's work was confirmed QUEUED (deferred
        # contract), the goal loop is skipped wholesale. The user's goal is intentionally not met
        # yet (the work runs out-of-band), so verifying the hand-off reply against it would always
        # fail and the remediation net could relaunch the work, double-enqueueing the task. The
        # real outcome is verified by the external runner's own goal loop and reported back.
        _last_verdict: Optional[Dict[str, Any]] = None
        _claim_corrected = False
        # Set when one of the orchestrator's own escalations resolved to a background-work runner
        # and was not started (see _run_deep's ``self_initiated``): no further escalation this turn.
        own_escalation_settled = False
        if (not _deferred_handoff_confirmed and not _decision_parked and not deferred_unconfirmed
                and (self.cfg.answer_goal_max_iterations > 1 or self.cfg.verify_claims)):
            try:
                # Verify against the turn's DERIVED GOAL CONDITION (the checkable done-standard
                # established at turn start by _understand_input/_derive_goal_condition) when one
                # was actually derived: that is the user-level bar for the WHOLE turn. Deriving a
                # done-standard and then judging the answer against something else (the plan's own
                # goal restatement) would let the two drift. When no distinct condition was derived
                # (it equals the raw message, e.g. the derivation failed safe), fall back to the
                # plan's goal, then to the generic bar, exactly as before.
                overall_goal = (goal_condition or "").strip()
                if not overall_goal or overall_goal == (user_message or "").strip():
                    overall_goal = (plan.goal or "").strip() or (
                        "Fully and correctly answer the user's request to their satisfaction: "
                        + user_message)
                # The SAME rendered L2 context layer the answer call(s) this turn used (see
                # ``_grounded_answer``'s ``layers`` wiring): ``context_view`` does not change across
                # this loop's regenerate/verify iterations (a regeneration only rewrites ``text``), so
                # computing it once here gives every verify call in this loop a byte-identical L2 to
                # the answer it is judging, and the same L2 across iterations too.
                answer_context_layer = grounding_context_layer(context_view)
                _max = max(1, self.cfg.answer_goal_max_iterations)
                if self.cfg.verify_claims:
                    # The claim check needs at least one verification pass even when the goal loop
                    # itself is configured off (answer_goal_max_iterations <= 1).
                    _max = max(2, _max)
                _remediations = 0
                _attempt = 1
                while _attempt < _max:  # at most _max-1 regenerations after the first answer
                    # SAY that this is happening. The verdict runs at ``verify_tier`` (the strongest
                    # model) over the same context the answer saw, so it is one of the longest waits
                    # in a turn, it sits AFTER the answer is already written, and nothing announced
                    # it: the reader saw "Answering…" and then silence until the whole turn landed.
                    # Only the outcome lines were ever emitted, and only once it was over.
                    if emit is not None:
                        emit.status("Checking the answer against the goal…")
                    verdict, verify_error = self._verify_goal(
                        overall_goal, user_message, text,
                        rep_preamble=rep_preamble,
                        quality_standards=quality_standards,
                        transcript=transcript,
                        exec_record=exec_record if self.cfg.verify_claims else None,
                        context_layer=answer_context_layer,
                        gathered=gathered)
                    if verdict is not None:
                        _last_verdict = verdict
                    if verdict is not None and verdict.get("met"):
                        if emit is not None:
                            emit.status("Answer verified against the goal.")
                        break
                    if verdict is None:
                        # Verifier call failed (LLM error, parse failure, etc.) - do not silently
                        # accept. Retry on the next attempt; if this was the last attempt, the turn
                        # proceeds unverified (``_exit_reason`` stays "unverified" below, never
                        # "verified") rather than being blocked, but the real reason is logged so an
                        # outage is diagnosable, not silent.
                        log.warning("Goal verifier did not run (attempt %d/%d): %s",
                                    _attempt, _max - 1, verify_error or "unknown reason")
                        if _attempt >= _max - 1:
                            break  # out of attempts, proceeds unverified
                        _attempt += 1
                        continue  # retry the verification call
                    # HONESTY: the answer claims a completed change the execution record does not
                    # back. The answering step can never change files/data itself, so either DO the
                    # work for real now (safe only when NOTHING mutated this turn; a prior success or
                    # failure means a re-run risks a double mutation) or rewrite the reply to be
                    # honest. A deep remediation does NOT consume a regeneration attempt: the loop
                    # re-verifies the post-deep answer against the record on the next pass.
                    if verdict.get("claims_unexecuted"):
                        can_execute = (self._has_deep_execution_capability()
                                       and not brainstorm_active
                                       and exec_record is not None
                                       and not exec_record.any_success
                                       and not exec_record.any_failure
                                       and _remediations < max(0, self.cfg.max_remediations))
                        if can_execute:
                            _remediations += 1
                            if emit is not None:
                                emit.status("That change was claimed but never executed, doing it now…")
                            _rem_plan = PlanDecision(
                                action="deep",
                                goal=_truncate_goal(f"Carry out the user's request for real: {user_message}"),
                                deep_brief=(f"{user_message}\n\nThe assistant REPLIED as if this was "
                                            f"already done, but nothing actually executed. APPLY the "
                                            f"change for real now (make the actual code/file/data "
                                            f"changes):\n{(text or '').strip()}")[:2000],
                                rationale="claim verification: answer claimed unexecuted work",
                                deep_difficulty=getattr(plan, "deep_difficulty", None),
                                deep_difficulty_reason=getattr(plan, "deep_difficulty_reason", None),
                            )
                            _rem_model = self._answer_model(_rem_plan, "balanced", hint=model_hint)
                            _rem_res = self._run_deep(_rem_plan, user_message, _rem_model,
                                                      emit=emit, rep_preamble=rep_preamble,
                                                      exec_record=exec_record, gathered=gathered,
                                                      quality_standards=quality_standards,
                                                      pending_inputs=pending_inputs,
                                                      model_hint=model_hint, ctx_meta=_ctx_meta,
                                                      cancel_check=cancel_check,
                                                      working_dir_override=working_dir_override,
                                                      resume_session_id=take_resume_session())
                            if _rem_res.kind == "cancelled":
                                return finish(_rem_res)
                            if not attachments:
                                self._recipe_learn_deep(
                                    user_message, PlanDecision(action="deep", goal=user_message,
                                                               deep_target="quest_data"), _rem_res,
                                    (_ctx_meta or {}).get("scope_tags"),
                                    learnable=message_is_user_turn and mode is Mode.LIVE)
                            _rem_out = ""
                            if _rem_res and _rem_res.deep_results:
                                _rem_out = "\n\n".join(
                                    s for s in (_strip_future_context(d.output)
                                                for d in _rem_res.deep_results) if s).strip()
                            if _rem_out:
                                if emit is not None:
                                    emit.status("Writing up what was done…")
                                text = self._synthesize_after_deep(
                                    user_message, prior_answer=text, deep_output=_rem_out,
                                    transcript=transcript, model=model, rep_preamble=rep_preamble)
                                self._kickoff_card_update(_rem_res, _rem_plan, user_message,
                                                          _ctx_meta, emit, gathered=gathered)
                            continue  # re-verify the remediated answer (attempt not consumed)
                        # Cannot safely re-run: correct the reply instead and flag the result so a
                        # background task maps to needs_you/failed, never a false done.
                        _claim_corrected = True
                        if emit is not None:
                            emit.status("Correcting the reply to reflect what actually happened…")
                        _record_summary = exec_record.summary() if exec_record is not None else ""
                        steer = ("Your previous answer claims a change was completed that did NOT "
                                 "actually execute. What actually ran this turn:\n"
                                 f"{_record_summary}\n\n"
                                 "Rewrite your answer to be honest: never claim a change was made "
                                 "when the record does not show it succeeding. Say plainly what has "
                                 "and has not been done, and what should happen next. Do not use em "
                                 "dashes.\n\n"
                                 f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                        text = _gen_answer(steer)
                        _attempt += 1
                        continue
                    # When the verifier says the answer lacked the context needed to be definitive,
                    # escalate to deep so it can search further. Regenerating with the SAME gathered
                    # context won't help — the deep runner can grep/read on its own.
                    if (verdict.get("need_more_context") and not brainstorm_active
                            and not own_escalation_settled
                            and self._has_deep_execution_capability()):
                        if emit is not None:
                            emit.status("Need more context to answer — searching further…")
                        _context_q = verdict.get("context_query") or user_message
                        _esc_plan = PlanDecision(
                            action="deep",
                            goal=_truncate_goal(overall_goal),
                            deep_brief=(
                                f"{user_message}\n\n"
                                "NOTE: An initial search was done but the result was inconclusive. "
                                f"What is missing: {_context_q}. "
                                "Search more thoroughly and give a definitive answer."
                            ),
                            rationale="answer verification escalated to deep (insufficient context)",
                            deep_difficulty=getattr(plan, "deep_difficulty", None),
                            deep_difficulty_reason=getattr(plan, "deep_difficulty_reason", None),
                        )
                        _esc_model = self._answer_model(_esc_plan, "balanced", hint=model_hint)
                        esc_facts_before = len(getattr(exec_record, "facts", None) or [])
                        _esc_res = self._run_deep(
                            _esc_plan, user_message, _esc_model,
                            emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                            gathered=gathered, quality_standards=quality_standards,
                            pending_inputs=pending_inputs, model_hint=model_hint,
                            ctx_meta=_ctx_meta, cancel_check=cancel_check,
                            working_dir_override=working_dir_override,
                            resume_session_id=take_resume_session(), self_initiated=True,
                            user_ordered=user_ordered_work(turn_user_intent))
                        if _esc_res.kind == "cancelled":
                            return finish(_esc_res)
                        if own_escalation_adds_nothing(_esc_res, exec_record, esc_facts_before):
                            # Not started (see _run_deep's ``self_initiated``) or it produced
                            # nothing; the last-resort run below must not try again. The status
                            # line above announced a search, so say it ended here.
                            own_escalation_settled = True
                            log.info("own escalation (need more context) added nothing; "
                                     "keeping the answer")
                            if emit is not None:
                                emit.status(OWN_ESCALATION_KEPT_STATUS)
                        else:
                            _esc_res.exit_reason = "escalated_deep"
                            _esc_res.goal_verdict = verdict
                            # Async, best-effort: prepare this user's cards for next time.
                            self._kickoff_card_update(_esc_res, _esc_plan, user_message, _ctx_meta,
                                                      emit, gathered=gathered)
                            return finish(_esc_res)
                    # Goal not met: surface the current answer as a milestone so the user sees
                    # progress while we continue iterating toward the goal.
                    if emit is not None and text:
                        emit.emit(ProgressEvent(type=EVENT_MILESTONE, text=text,
                                                data={"goal_not_met": True,
                                                      "reason": verdict.get("reason") or ""}))
                    if emit is not None:
                        emit.status("Answer not yet at the bar, improving it…")
                    _new = self._drain_pending(pending_inputs)
                    steer = (f"Why your previous answer fell short: "
                             f"{verdict.get('reason') or 'it did not meet the quality bar'}. "
                             f"Do this now: {verdict.get('next_action') or 'address the gap and answer fully.'}\n\n"
                             + (_new + "\n\n" if _new else "")
                             + f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                    text = _gen_answer(steer)
                    _attempt += 1
            except Exception:  # noqa: BLE001 — answer verification must never break the turn
                log.warning("answer goal verification failed", exc_info=True)

        # LAST-RESORT DEEP RUN. The goal is definitively not met, the turn never ran deep work, and a
        # deep path is wired: use it before giving up. Covers every way the loop above can end
        # without the in-loop escalation firing (attempts exhausted after a regeneration, the
        # verifier naming a gap without setting need_more_context, a non-answer like "which file
        # should I look in?"). One pass only; skipped when something already mutated this turn (a
        # re-run could double the change) or the human asked us to hold off.
        if (self.cfg.deep_before_giving_up and not own_escalation_settled
                and not _deferred_handoff_confirmed and not _decision_parked
                and not deferred_unconfirmed
                and not _claim_corrected and not brainstorm_active
                and _last_verdict is not None and not _last_verdict.get("met")
                and not _ctx_meta.get("deep_attempted")
                and self._has_deep_execution_capability()
                and (exec_record is None or not exec_record.any_mutation_attempted)):
            try:
                if emit is not None:
                    emit.status("Not enough to answer yet, searching deeper before giving up…")
                _gap = (_last_verdict.get("next_action") or _last_verdict.get("reason")
                        or "the answer did not meet the goal")
                _lr_plan = PlanDecision(
                    action="deep",
                    goal=_truncate_goal(overall_goal),
                    deep_brief=(
                        f"{user_message}\n\n"
                        "NOTE: The quick answer did not meet the goal and no deep search has been "
                        f"done yet. What is missing: {_gap}. Search the corpus, cards, notes and "
                        "files thoroughly and give a definitive answer. Do not ask the user which "
                        "file or quest to look in; find it yourself. Only say something could not "
                        "be found after actually searching, and name where you looked."
                    )[:2000],
                    rationale="goal not met and no deep run yet: deep run before giving up",
                    deep_difficulty=getattr(plan, "deep_difficulty", None),
                    deep_difficulty_reason=getattr(plan, "deep_difficulty_reason", None),
                )
                _lr_model = self._answer_model(_lr_plan, "balanced", hint=model_hint)
                lr_facts_before = len(getattr(exec_record, "facts", None) or [])
                _lr_res = self._run_deep(
                    _lr_plan, user_message, _lr_model,
                    emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                    gathered=gathered, quality_standards=quality_standards,
                    pending_inputs=pending_inputs, model_hint=model_hint,
                    ctx_meta=_ctx_meta, cancel_check=cancel_check,
                    working_dir_override=working_dir_override,
                    resume_session_id=take_resume_session(), self_initiated=True,
                    user_ordered=user_ordered_work(turn_user_intent))
                if _lr_res.kind == "cancelled":
                    return finish(_lr_res)
                if not own_escalation_adds_nothing(_lr_res, exec_record, lr_facts_before):
                    _lr_res.exit_reason = "escalated_deep"
                    _lr_res.goal_verdict = _last_verdict
                    self._kickoff_card_update(_lr_res, _lr_plan, user_message, _ctx_meta, emit,
                                              gathered=gathered)
                    return finish(_lr_res)
                log.info("last-resort escalation added nothing; keeping the answer")
                if emit is not None:
                    emit.status(OWN_ESCALATION_KEPT_STATUS)
            except Exception:  # noqa: BLE001 — the net must never break the turn
                log.warning("last-resort deep run failed", exc_info=True)

        _exit_reason = "unverified"
        if _deferred_handoff_confirmed:
            # The work was confirmed queued out-of-band (deferred contract): neither verified nor
            # unverified applies to this turn; the external runner verifies the real outcome.
            _exit_reason = "deferred"
        elif _decision_parked:
            # This turn's change was parked on an approval decision, not completed: the goal is
            # intentionally not met yet (a human still has to answer the card), so this is neither
            # "verified" nor "unverified" -- it never entered that loop at all (see the gate above).
            _exit_reason = "parked"
        elif _last_verdict is not None:
            _exit_reason = "verified" if _last_verdict.get("met") else "max_turns"
        # An overseer answer_now that short-circuited the read loop to this answer wins the reason,
        # so a consumer can see the terminal path was decided by the overseer.
        if (overseer_decided == "overseer_answer_now" and _last_verdict is None
                and not _deferred_handoff_confirmed and not _decision_parked):
            _exit_reason = "overseer_answer_now"
        _res = OrchestratorResult(kind="answer", text=text, rationale=plan.rationale,
                                  model=model, exit_reason=_exit_reason,
                                  goal_verdict=_last_verdict,
                                  decision_id=_parked_decision_id)
        if _claim_corrected:
            # The reply had to be corrected for honesty and the claimed work never executed: flag
            # the result so a background task maps to needs_you/failed, never a false done.
            _res.claim_corrected = True
            _res.partial = True
        return finish(_res)

    # --- LIVE streaming convenience: a generator yielding events as they happen --------

    def run_stream(self, user_message: str, *, transcript: str = "", context_view: str = "",
                   quest_id: Optional[str] = None,
                   mode: Mode = Mode.LIVE,
                   model_hint: Optional[str] = None,
                   attachments: Optional[List[Dict[str, Any]]] = None,
                   rep_preamble: Optional[str] = None,
                   pending_inputs: Optional[Callable[[], List[str]]] = None,
                   conv_id: Optional[str] = None,
                   conv_scope: Optional[Dict[str, Any]] = None,
                   now: Optional[str] = None,
                   context_meta: Optional[Dict[str, Any]] = None):
        """Generator form of ``run`` for a LIVE consumer that wants to iterate events.

        Yields each ``ProgressEvent`` (as emitted, post-sink-policy for the given mode) and,
        as the FINAL item, the terminal ``OrchestratorResult``. Implemented by feeding a
        ``StreamSink`` whose forward appends to a thread-safe queue while ``run`` executes in a
        worker thread, so the caller streams in real time and still gets the structured result.

        ``model_hint`` and ``context_meta`` are forwarded to ``run`` unchanged — see ``run`` for
        semantics.

        Example::

            for item in orch.run_stream("..."):
                if isinstance(item, OrchestratorResult):
                    final = item            # terminal result
                else:
                    render(item)            # a dict event
        """
        import queue as _queue
        import threading as _threading

        from .adapters import StreamSink

        q: "_queue.Queue" = _queue.Queue()
        _SENTINEL = object()
        sink = StreamSink(lambda ev: q.put(("event", ev)))

        result_box: Dict[str, Any] = {}

        def _worker():
            try:
                res = self.run(user_message, transcript=transcript, context_view=context_view,
                               quest_id=quest_id, mode=mode, sink=sink, model_hint=model_hint,
                               attachments=attachments, rep_preamble=rep_preamble,
                               pending_inputs=pending_inputs, conv_id=conv_id,
                               conv_scope=conv_scope, now=now, context_meta=context_meta)
                result_box["result"] = res
            except Exception as e:  # noqa: BLE001
                result_box["error"] = e
            finally:
                q.put(("done", _SENTINEL))

        t = _threading.Thread(target=_worker, daemon=True)
        t.start()
        while True:
            kind, payload = q.get()
            if kind == "event":
                yield payload
            else:
                break
        t.join()
        if "error" in result_box:
            raise result_box["error"]
        yield result_box.get("result")
