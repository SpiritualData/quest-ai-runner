"""Starting a turn: set-up of run-local state and the context gathered before planning.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance and in the
run-local namespace ``rs`` that ``RunMixin.run`` builds and hands from one phase to the next.
"""
from __future__ import annotations

import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from .adapters import (
    EVENT_CONTEXT,
    EVENT_MODE_SIGNAL,
    EVENT_UNDERSTANDING,
    Mode,
    PlanDecision,
    ProgressEvent,
    ScopedThreadPoolExecutor as ThreadPoolExecutor,
)
from .card_thread import CardThreadContext, CardThreadDecision, merge_candidates, render_thread_hint
from .context_doctrine import CACHED_HINT_GATE
from .guard import ExecutionRecord
from .orchestrator_capabilities import (
    context_assembly_timeout_seconds,
    guidance_selection_timeout_seconds,
    record_context_assembly_timeout,
)
from .orchestrator_constants import RECIPE_SKIP_GOAL_CONTEXT, log
from .orchestrator_decisions import message_forbids_new_task, restates_meaningfully
from .orchestrator_events import _project_card_metadata_for_event, _project_sources_for_event
from .orchestrator_turn import Narrator, TurnCardCache, _Emitter
from .orchestrator_types import OrchestratorResult
from .planner_context import (
    _prior_escalation_lines,
    _recent_conversation_turns,
    declined_proposals_block,
)
from .planner_prompt import _CARD_THREAD_PLANNER_BLOCK_TEMPLATE
from .recent_context import (
    build_item_usage_hint,
    filter_relevant,
    quest_scope_key,
    render_recent_cards,
)
from .sufficiency import AbridgedTurnState, collect_abridged_items, render_abridged_notice


class RunBeginMixin:
    """Starting a turn: set-up of run-local state and the context gathered before planning."""

    def _run_begin(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """Set up the turn: run-local state, execution mode, recipe fast path, narrator and the first routing facts."""
        attachments = rs.attachments
        background_sink = rs.background_sink
        card_thread = rs.card_thread
        context_meta = rs.context_meta
        conv_id = rs.conv_id
        conv_scope = rs.conv_scope
        detach_check = rs.detach_check
        message_is_user_turn = rs.message_is_user_turn
        mode = rs.mode
        prior_escalations = rs.prior_escalations
        prior_narration = rs.prior_narration
        quest_id = rs.quest_id
        rep_preamble = rs.rep_preamble
        resume_session_id = rs.resume_session_id
        sink = rs.sink
        transcript = rs.transcript
        rs.user_message = (rs.user_message or "").strip()
        gathered: List[Dict[str, Any]] = []
        rs.gathered = gathered
        # "EXPLAIN HOW I GOT THIS" (cfg.explain_answer): the ALREADY-PROJECTED cards and sources
        # this turn actually assembled, captured at the same place EVENT_CONTEXT is emitted. Kept
        # here rather than re-derived in finish() for two reasons: what the panel shows can never
        # disagree with what the context event showed, and nothing UNPROJECTED can reach the panel
        # by some new route later (the projections deliberately strip item text and keep only
        # labels, counts and path-like items). Stays empty when the feature is off or no context
        # was assembled.
        rs.explain_cards: List[Dict[str, Any]] = []
        rs.explain_sources: List[Dict[str, Any]] = []
        # Per-run record of every minimal-intervention OVERSEER consultation (both hook points), and
        # the count both hooks share against cfg.overseer_max_signals. Stamped onto the result in
        # finish(). Stays empty (and overseer_signals None) whenever the overseer is off.
        overseer_signals: List[Dict[str, Any]] = []
        rs.overseer_signals = overseer_signals
        # NON-BLOCKING overseer plumbing (Fix 1). The overseer's provider call runs in a BACKGROUND
        # thread (same idiom as context assembly) so consulting it NEVER stalls the user-facing loop.
        #  - ``_oversee_executor``: a single per-run worker, created lazily on first consult and torn
        #    down (wait=False) in finish(); a still-running consult finishes on its own (bounded
        #    network call) and is never joined synchronously.
        #  - ``pending_oversee``: the in-flight hook-A consult submitted on a prior step, polled at
        #    the top of the next step; kept until it resolves (re-checked each step, never dropped).
        #  - ``overseer_submitted``: submissions counted against cfg.overseer_max_signals (a
        #    submitted-but-uncollected consult still counts, so the shared cap holds across both hooks).
        rs._oversee_executor: Optional[ThreadPoolExecutor] = None
        rs.pending_oversee: Optional[Dict[str, Any]] = None
        rs.overseer_submitted = 0
        # Previous step's plan (Fix 12's repeat-plan gate signal), captured just before each step's
        # planner call overwrites ``plan``. None until the first plan exists.
        rs._prev_plan_for_gate: Optional[PlanDecision] = None
        # Fix 7: the caller-supplied cross-turn escalation history, pre-formatted once for the
        # digest (see ``_prior_escalation_lines``). Computed once; reused by both hooks this run.
        _prior_escalation_digest_lines = _prior_escalation_lines(prior_escalations)
        rs._prior_escalation_digest_lines = _prior_escalation_digest_lines
        # Any DECLINED proposal in that same history is a fact the PLANNER itself must hold, not
        # only the (off-by-default) overseer, so it goes into ``context_view`` right here, the same
        # way the STAGE 1 "UNDERSTOOD REQUEST" block is prepended further below. Empty when nothing
        # in ``prior_escalations`` was refused, so this is byte-for-byte a no-op for every caller
        # that passes none.
        declined_block = declined_proposals_block(prior_escalations)
        if declined_block:
            rs.context_view = (declined_block + "\n\n" + rs.context_view
                             if rs.context_view else declined_block)
        # Run-local durable EXECUTION FACTS (the broken-promise guard's evidence): each deep
        # subtask that actually executes records its outcome (success/failure) here, threaded like
        # ``gathered``. Attached to the OrchestratorResult in finish().
        exec_record = ExecutionRecord()
        rs.exec_record = exec_record
        started = time.monotonic()
        rs.started = started
        cfg = self.cfg
        rs.cfg = cfg

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
        rs.take_resume_session = take_resume_session

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
        hold_off_active = message_is_user_turn and message_forbids_new_task(rs.user_message)
        rs.hold_off_active = hold_off_active
        # The same reasoning covers the ESCALATION NETS' own hold-off check (the "is this a request
        # for a change?" fallbacks below): on a queued task's brief it would read quoted output of
        # earlier runs as the human holding off. Only a message typed this turn can hold off.
        nets_honor_hold_off = message_is_user_turn
        rs.nets_honor_hold_off = nets_honor_hold_off
        # The planner's structured verdict on what the CURRENT message asks of the assistant
        # (``PlanDecision.user_intent``): the latest one any planner step this turn gave. The
        # escalation nets below honor it in place of the retired regex net over the message.
        # None = no planner step gave a usable verdict (planner error, a path with no planner
        # call): the nets then ask ``judge_execution_directive`` instead.
        rs.turn_user_intent: Optional[str] = None
        if hold_off_active:
            log.info("User message forbids opening a task this turn; running under the no-action "
                     "gate (planner deep/confirm will degrade to answer).")
        rs.brainstorm_active = (cfg.execution_mode == "brainstorm") or hold_off_active
        rs.mode_signal_detected: Optional[str] = None
        # --- PER-IDEA THREADING (the idea IS the card; opt-in via cfg.card_thread_enabled) -------
        # ``card_thread_ctx`` is the consumer's per-turn thread context (active card + the cards it
        # always wants offered). ``card_thread_decision`` is what the planner resolved this turn,
        # stamped onto the result in finish(). Both stay None when the flag is off, so the run is
        # byte-identical to a build without the feature.
        card_thread_ctx: Optional[CardThreadContext] = (
            CardThreadContext.coerce(card_thread) if cfg.card_thread_enabled else None)
        rs.card_thread_ctx = card_thread_ctx
        rs.card_thread_decision: Optional[CardThreadDecision] = None
        rs.card_thread_block: str = ""
        rs.card_thread_candidate_ids: List[str] = []
        # The action ("deep"/"confirm"/"clarify") the turn chose that the brainstorm latch degraded
        # to "answer", if any. Feeds the no-action acknowledgment steer on the answer path so the
        # reply can say the work was held rather than silently dropping it.
        rs.brainstorm_suppressed_action: Optional[str] = None
        # Direct tool calls (core/tools.py): whether any ran this turn, and the (name, args)
        # signatures that SUCCEEDED, so a re-plan that repeats a done call cannot run it twice.
        rs.tool_calls_ran = False
        tool_ok_signatures: set = set()
        rs.tool_ok_signatures = tool_ok_signatures
        # The question a suppressed "clarify" (planner) or a suppressed understanding-clarify
        # (stage 1) wanted to park as a decision-request. While the latch is held nothing may
        # escalate, so the question rides into the REPLY instead (see BRAINSTORM_CLARIFY_ACK_PREFIX).
        rs.brainstorm_clarify_question: Optional[str] = None
        # True when the release judge LIFTED the hold on this very turn. The user just told us to
        # stop holding back and act on what was discussed, so the turn is a directive by definition:
        # it must not end as one more proposal (that is what the whole conversation already was).
        rs.brainstorm_released_this_turn = False

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
        rs.emit = emit

        # --- RECIPE FAST PATH (core/recipes.py): a known operation runs BEFORE any context search ---
        # The lookup is lexical and model-free, so it costs microseconds ahead of everything else in
        # the turn; only a strong nominee pays for one small fast-tier call (same operation? what
        # arguments?). A hit returns the tool's receipt now; a miss, a failure, or a near-miss
        # falls through to the unchanged run below.
        if (self.recipes is not None and self.tools is not None and cfg.recipe_fast_path
                and mode is Mode.LIVE and message_is_user_turn and not attachments):
            _fast = self._recipe_fast_path(rs.user_message, quest_id=quest_id,
                                           context_meta=context_meta, emit=emit)
            if _fast is not None:
                return _fast
        # DEEP RECIPE: a request that repeats a saved quest-data operation gets a ready plan, so the
        # planner call and the turn-start context search below never run for it.
        rs._forced_plan: Optional[PlanDecision] = None
        if (self.recipes is not None and cfg.recipe_fast_path and cfg.recipe_deep
                and mode is Mode.LIVE and message_is_user_turn and not attachments
                and self._has_deep_execution_capability()):
            rs._forced_plan = self._recipe_deep_plan(rs.user_message, quest_id=quest_id,
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
        rs.narrator = narrator
        if narrator.enabled:
            try:
                emit.status("Looking into this...")
            except Exception:  # noqa: BLE001
                pass
            narrator.begin(rs.user_message)

        # --- THE REACH JUDGE, started now so it overlaps everything before the first plan -------
        # Its inputs are only the literal request and the consumer's reach summary, so it does not
        # need to wait for understanding, context or guidance. Run serially in front of the first
        # plan it measured +0.7s p50 per turn on a cheap planner (2026-10-05); started here, the
        # first _plan() only collects a result that is normally already there. No-op when off.
        # Known cost, accepted: a turn that ends before planning (an understanding clarify) has
        # paid for one judge call (~1k input tokens) that nobody reads.
        self.prefetch_reach_verdict(rs.user_message)

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
        rs._ctx_meta = _ctx_meta
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
        if rs._forced_plan is not None:
            _ctx_meta[RECIPE_SKIP_GOAL_CONTEXT] = True

        # --- Mid-run user messages: auto-drain a wired inbox for THIS conversation ----------------
        # If the caller didn't pass an explicit ``pending_inputs`` but an ``input_inbox`` is wired,
        # build one that drains this conversation's pending messages. This makes mid-run message
        # folding work for ANY interface that pushes to the inbox (chat, Quest frontend, ...) with no
        # per-interface wiring beyond the push. The conversation key is resolved generically.
        if rs.pending_inputs is None and self.input_inbox is not None:
            # The conversation id the caller ran this turn for (``conv_id``) is read FIRST: an
            # interface pushes a mid-run message under the id of the conversation the person is in,
            # while ``quest_id`` names the quest that conversation is bound to. Reading only the
            # quest id left a quest chat's voice message in memory, never drained (2026-10-19).
            # The older key chain stays, so an interface that pushes by quest or session id works.
            _legacy_key = (quest_id or _ctx_meta.get("conversation_id")
                           or _ctx_meta.get("session_id") or _ctx_meta.get("user_id"))
            _conv_keys = [k for k in dict.fromkeys(
                [str(conv_id) if conv_id else None,
                 str(_ctx_meta.get("conversation_id")) if _ctx_meta.get("conversation_id") else None,
                 str(_legacy_key) if _legacy_key else None]) if k]
            if _conv_keys:
                _inbox = self.input_inbox

                def _drain_conversation() -> List[str]:
                    drained: List[str] = []
                    for key in _conv_keys:
                        drained.extend(_inbox.drain(key))
                    return drained

                rs.pending_inputs = _drain_conversation

    def _run_gather_context(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """Understand the request and gather the turn's context: conversation slice, recent cards, anticipation, assembled context and guidance."""
        _ctx_meta = rs._ctx_meta
        anticipated_id = rs.anticipated_id
        attachments = rs.attachments
        card_thread_ctx = rs.card_thread_ctx
        cfg = rs.cfg
        conv_id = rs.conv_id
        conv_scope = rs.conv_scope
        emit = rs.emit
        exec_record = rs.exec_record
        hold_off_active = rs.hold_off_active
        narrator = rs.narrator
        now = rs.now
        quest_id = rs.quest_id
        transcript = rs.transcript

        # --- BRAINSTORM RELEASE (latched turns only; the exit authority) -----------------------
        # While the latch is held, ONE dedicated structured judgment decides whether this message
        # lifts the hold (see judge_brainstorm_release). It runs BEFORE ANY stage that could act or
        # escalate (input understanding, the planner loop, the terminal paths), so the whole turn --
        # understanding, planner note, action gating, acknowledgment -- runs in the mode the user is
        # actually in. A HOLD verdict (including every failure mode) leaves the latch on; only a
        # release flips it, and that release is what the consumer sees as mode_signal.
        # Cost is bounded to brainstorm turns: nothing here runs in normal mode.
        if rs.brainstorm_active and cfg.mode_signals_enabled:
            _released, _release_reason = self.judge_brainstorm_release(rs.user_message, transcript)
            log.info("Brainstorm-release judge: %s (%s)",
                     "RELEASE" if _released else "HOLD", _release_reason)
            if _released:
                # A release lifts the CONSUMER'S LATCH, never the veto the user gave in this very
                # message: "go ahead, but don't open a task for it" releases the mode and still
                # forbids the task.
                rs.brainstorm_active = hold_off_active
                rs.brainstorm_released_this_turn = True
                rs.mode_signal_detected = "exit_brainstorm"
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
        rs.goal_condition = rs.user_message
        conv_ctx_text = ""
        # Query-aware retrieval routing (spec v3, work package C): structured constraints parsed
        # alongside the goal condition below (else-branch only -- see ``_derive_goal_condition``).
        # None whenever nothing in the message calls for filtering (today's behavior).
        rs.retrieval_constraints: Optional[Dict[str, Any]] = None
        if (self.conversation_store is not None and conv_id
                and self._needs_context_to_understand(rs.user_message)):
            try:
                rs.goal_condition, conv_ctx_text, clarify_q = self._understand_input(
                    rs.user_message, conv_id, conv_scope or {}, emit)
            except Exception:  # noqa: BLE001 — understanding must never break the run
                rs.goal_condition, conv_ctx_text, clarify_q = rs.user_message, "", None
            if clarify_q and rs.brainstorm_active:
                # BRAINSTORM: a latched turn escalates NOTHING, so this may not park a decision-
                # request. Carry the question into the reply instead (the answer asks it inline) and
                # let the loop run normally on the best available reading of the message.
                log.info("Brainstorm mode: understanding-clarify degraded to an in-reply question.")
                rs.brainstorm_suppressed_action = rs.brainstorm_suppressed_action or "clarify"
                rs.brainstorm_clarify_question = clarify_q
            elif clarify_q:
                # Short-circuit the turn: ask the user, do NOT run the planner loop. Reuse the
                # clarify/confirm mechanism so the executor maps it to needs_you and chat posts it.
                rs.plan = PlanDecision(action="confirm", confirm_question=clarify_q,
                                    rationale="clarification needed to understand the request")
                rs.res = self._run_confirm(rs.plan, quest_id=quest_id)
                return self._finish_understanding_clarify(
                    rs.res, emit=emit, exec_record=exec_record, user_message=rs.user_message,
                    ctx_meta=_ctx_meta)
            # STAGE 1 produced a goal condition. Context ACCUMULATES across the stages: the
            # conversation context selected here flows FORWARD, so stage 2 (achievement-context
            # selection, below) and stage 3 (the planner) each START from what was already gathered
            # and only search for MORE as needed. So we inject the gathered conversation context AND
            # the resolved request. (Narrowing to a minimal, just-what-it-needs slice happens later
            # per SUBGOAL in _run_deep, to keep each subgoal focused, not on this main flow.)
            if restates_meaningfully(rs.goal_condition, rs.user_message) or conv_ctx_text:
                emit.emit(ProgressEvent(
                    type=EVENT_UNDERSTANDING,
                    text=f"Understood as: {rs.goal_condition}",
                    data={"goal_condition": rs.goal_condition, "internal": True}))
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
                     "answer it, never read it back to them) ---\n" + rs.goal_condition + "\n")
                rs.context_view = (_understood_block + "\n" + rs.context_view
                                if rs.context_view else _understood_block)
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
            rs.goal_condition, rs.retrieval_constraints = self._derive_goal_condition(rs.user_message, now=now)
            if restates_meaningfully(rs.goal_condition, rs.user_message) or rs.retrieval_constraints:
                _event_data: Dict[str, Any] = {"goal_condition": rs.goal_condition, "internal": True}
                if rs.retrieval_constraints:
                    _event_data["constraints"] = rs.retrieval_constraints
                emit.emit(ProgressEvent(
                    type=EVENT_UNDERSTANDING,
                    text=f"Understood as: {rs.goal_condition}",
                    data=_event_data))
            if rs.retrieval_constraints and isinstance(rs.retrieval_constraints.get("time_range"), dict):
                # C2 card stores (item level): thread the turn's time_range into the assembly meta
                # so a time-filter-capable assembler (e.g. ``FileContextStore``) hard-filters card
                # CONTENT ITEMS by their ``ts``, and the recent-context filter/render calls below
                # apply the same rule. ``_ctx_meta`` also flows into every per-goal assembly this
                # turn spawns (``_run_deep`` -> ``_assemble_for_goal_with_cards``), so deep goals
                # inherit the filter. Assemblers that ignore unknown meta keys are unaffected.
                _ctx_meta["time_range"] = rs.retrieval_constraints["time_range"]
            if rs.retrieval_constraints and self.conversation_store is not None:
                # C3 routing: constraints name a time period / topic / actor / content kind, so do
                # a BOUNDED, hard-filtered cross-conversation search now instead of leaving this to
                # undirected relevance-only recall. The store applies filters FIRST (hard filter),
                # relevance ranks WITHIN the filtered set, and degrades to relevance-only with an
                # explicit labeled note when the filtered set is empty (see ``related_slices``).
                # Best-effort: any failure here must never break the turn.
                try:
                    _filtered_ctx = self.conversation_store.related_slices(
                        rs.goal_condition, conv_scope or {}, exclude_conv_id=conv_id,
                        filters=rs.retrieval_constraints)
                except Exception:  # noqa: BLE001
                    _filtered_ctx = None
                if _filtered_ctx is not None and _filtered_ctx.text:
                    _filtered_block = "=== FILTERED CONVERSATION SEARCH ===\n" + _filtered_ctx.text
                    rs.context_view = (_filtered_block + "\n\n" + rs.context_view
                                    if rs.context_view else _filtered_block)

        # Fix 5a: a handful of PRIOR user turns in this SAME conversation, for the overseer digest's
        # RECENT CONVERSATION section. Computed once (from whatever ``conv_ctx_text`` STAGE 1 pulled;
        # empty when no conversation_store/conv_id was in play), excluding the current turn's own
        # request (raw AND resolved) so it is never duplicated against CURRENT USER REQUEST /
        # RESOLVED AS. Reused by both overseer hooks this run. Never raises.
        _recent_conversation_digest_lines = _recent_conversation_turns(
            conv_ctx_text, exclude=[rs.user_message, rs.goal_condition])
        rs._recent_conversation_digest_lines = _recent_conversation_digest_lines

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
                _is_followup = self._needs_context_to_understand(rs.user_message)
            except Exception:  # noqa: BLE001
                _is_followup = False
            try:
                _recent_filtered = filter_relevant(
                    _recent_records, f"{rs.goal_condition} {rs.user_message}",
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
                    _recent_records, f"{rs.goal_condition} {rs.user_message}")
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
                        rs.user_message, _ant_keys, now=datetime.now(),
                        anticipated_id=anticipated_id)
                    if (_ant_match is not None and _ant_match.matched is not None
                            and _ant_match.precomputed is not None):
                        _ant_view = (getattr(_ant_match.precomputed, "context_view", "")
                                     or "").strip()
                if _ant_view:
                    _ant_block = (
                        "--- ANTICIPATED CONTEXT (precomputed for a predicted ask) ---\n"
                        + CACHED_HINT_GATE + "\n\n" + _ant_view)
                    rs.context_view = (_ant_block + "\n\n" + rs.context_view if rs.context_view
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
        rs.card_context = card_context
        card_context.emit = emit
        _ctx_prefetch_query: Optional[str] = None

        _assembled = None
        _ctx_future = None
        _ctx_executor = None
        if self.context_assembler is not None and rs._forced_plan is None:
            try:
                _ctx_assembler = self.context_assembler
                # STEP 2 (context selection) targets the RESOLVED request: when Step 1 rewrote the
                # message into a goal_condition, select context for THAT, not the raw anaphoric text.
                # The planner still receives the literal ``user_message`` (literal-words fidelity);
                # the goal_condition rides in context_view. When Step 1 did not run, goal_condition
                # == user_message, so this is byte-for-byte the prior behavior.
                _ctx_msg = rs.goal_condition
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
                    self.gate_docs_fallback(assemble_meta, rs.user_message)
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
        rs.guidance_selected_ids = guidance_selected_ids
        # The guidance cards selected for THIS input are the QUALITY STANDARDS the result must meet.
        # We keep them in ``quality_standards`` so the goal loop can verify the done-standard against
        # the same standards the planner was given (see _run_deep / _verify_goal).
        rs.quality_standards: Optional[str] = None
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
                    rs.user_message,
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
                rs.quality_standards = _guidance_view  # rolls into goal verification as the quality bar
                rs.context_view = (_guidance_view + "\n\n" + rs.context_view if rs.context_view
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
                        rs.context_view = (_assembled.context_view + "\n\n" + rs.context_view if rs.context_view
                                        else _assembled.context_view)
                    if rs.model_hint is None and _assembled.model_tier_hint:
                        rs.model_hint = _assembled.model_tier_hint
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
        rs._sources: List[Dict[str, Any]] = (
            list(getattr(_assembled, "sources", None) or []) if _assembled is not None else []
        )
        _recent_entries: List[Dict[str, Any]] = []
        if _recent_filtered:
            try:
                _fresh_ids = {cm.get("id") for cm in _card_meta if isinstance(cm, dict)}
                _recent_survivors = [r for r in _recent_filtered if r.get("id") not in _fresh_ids]
                _recent_text, _recent_entries = render_recent_cards(
                    _recent_survivors, f"{rs.goal_condition} {rs.user_message}",
                    time_range=_ctx_meta.get("time_range"))
                if _recent_text:
                    rs.context_view = (_recent_text + "\n\n" + rs.context_view if rs.context_view
                                    else _recent_text)
                if _recent_survivors:
                    rs._sources.append({
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
        rs.abridged_state = AbridgedTurnState()
        if cfg.full_read_before_answer:
            try:
                rs.abridged_state.items = collect_abridged_items(_merged_card_meta)
                _abridged_notice = render_abridged_notice(rs.abridged_state.items)
                if _abridged_notice:
                    rs.context_view = (rs.context_view + "\n\n" + _abridged_notice if rs.context_view
                                    else _abridged_notice)
            except Exception:  # noqa: BLE001 -- the gate must never break a turn
                log.debug("abridged-item collection failed; the turn continues without the gate",
                          exc_info=True)
                rs.abridged_state = AbridgedTurnState()

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
                rs.card_thread_candidate_ids = [c.id for c in _thread_candidates]
                _hint = render_thread_hint(card_thread_ctx, _thread_candidates)
                rs.card_thread_block = _CARD_THREAD_PLANNER_BLOCK_TEMPLATE.format(thread_hint=_hint)
            except Exception:  # noqa: BLE001 -- threading must never break a turn
                log.debug("card thread hint build failed; the turn continues its current card",
                          exc_info=True)
                rs.card_thread_block = ""
                rs.card_thread_candidate_ids = []

        # --- CONTEXT EVENT: emit EVENT_CONTEXT showing which cards were selected -----------
        # Dedicated event for context assembly: card selection + sources. Surfaces in all modes.
        # Always emitted when assembly ran OR recent-turn cards survived OR assembly timed out
        # (even with 0 cards + 0 sources otherwise) so consumers can show "assembly ran but
        # found nothing" -- or "assembly timed out and was dropped" -- without needing a
        # separate signal. Never raises.
        if _assembled is not None or _recent_entries or context_assembly_timed_out:
            try:
                log.debug(f"Assembled context: {len(_merged_card_meta)} cards, {len(rs._sources)} sources")
                # LIGHTWEIGHT projection: the card_metadata items carry each item's full resolved
                # ``text`` (heavy), and a conversation-history card's TITLE is the raw user turn, so
                # both the card titles and the source items are sanitized before they leave the
                # process. Nothing on this event quotes the person's own words back at them.
                _card_meta_light = _project_card_metadata_for_event(_merged_card_meta)
                _sources_light = _project_sources_for_event(rs._sources)
                # Same projected shapes the panel is allowed to show (see explain_cards above).
                rs.explain_cards = list(_card_meta_light or [])
                rs.explain_sources = list(_sources_light or [])
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
                        "source_count": len(rs._sources),
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
                self.recent_context.record(_recent_scope_keys_this_turn, _merged_card_meta, rs.user_message)
            except Exception:  # noqa: BLE001
                pass

        # --- CONTEXT TRANSPARENCY (Feature 2): emit a human-readable summary of sources ------
        # Best-effort: emit a STATUS event naming the adapters + file items so the consumer/UI
        # can show "Context from: ...". Only fires when the assembled context carries source
        # attribution (assemblers opt in by populating AssembledContext.sources). Never raises.
        if _assembled is not None:
            try:
                rs._sources = getattr(_assembled, "sources", None) or []
                if rs._sources:
                    _parts: List[str] = []
                    for _src in rs._sources:
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
        rs.native_blocks: List[Dict[str, Any]] = []
        if attachments:
            from .attachments import prepare_attachments
            answer_model = self.registry.resolve_tier(rs.model_hint or "sonnet")
            prepared = prepare_attachments(
                attachments,
                model=answer_model,
                provider=self.provider,
                vision_provider=self.vision_provider,
                vision_model=self.vision_model,
            )
            rs.native_blocks = prepared.native_blocks
            if prepared.text_context:
                # PREPEND, never append. ``_strip_discovery_section`` builds the ANSWER's grounding
                # by keeping only what sits BEFORE the "## Discovery" block and dropping the rest,
                # so anything appended to the end of context_view reaches the PLANNER and is then
                # cut out of the answer. Appending here meant a described image (the only way a
                # text-only answering model can see an upload) grounded the plan and vanished from
                # the reply, and the model answered about the picture from nothing: measured
                # 2026-10-01, a solid magenta upload was answered "Green". The attachment is the
                # user's own most salient context, so it belongs at the front either way.
                rs.context_view = (prepared.text_context + "\n\n" + rs.context_view if rs.context_view
                                else prepared.text_context)
