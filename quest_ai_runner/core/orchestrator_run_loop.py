"""The turn's exit (``finish``) and the bounded plan, gather and re-plan loop.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance and in the
run-local namespace ``rs`` that ``RunMixin.run`` builds and hands from one phase to the next.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from hubofhubs.ports.scope import ScopedThreadPoolExecutor as ThreadPoolExecutor
from hubofhubs.ports.events import (
    EVENT_CARD_THREAD,
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_EXPLANATION,
    EVENT_MODE_SIGNAL,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_TOKENS,
    Mode,
)
from hubofhubs.ports.deep import ProgressEvent
from .adapters_values import PlanDecision
from .answer_explanation import is_eligible, trace_from_result
from hubofhubs.recall.card_thread import CardThreadDecision, parse_card_thread
from .goal_metrics import deep_metric_data
from .orchestrator_capabilities import (
    planner_full_view_start,
    planner_note_observation,
    read_spec_key,
    repeated_read_observation,
    split_repeated_reads,
)
from .orchestrator_constants import PENDING_READ_MAX_RUNS, log
from .orchestrator_decisions import _clarify_question_text
from .orchestrator_events import _is_orchestrator_command
from .orchestrator_text import _future_context_for_display, unconfirmed_no_change_text
from .orchestrator_types import OrchestratorResult
from .planner_context import gathered_content
from .scope_tags import scope_fence_block
from .tools import ToolContext


class RunLoopMixin:
    """The turn's exit (``finish``) and the bounded plan, gather and re-plan loop."""

    def _run_define_finish(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """Define the turn's exit: the budget check and ``finish``, which stamps every terminal result."""
        _ctx_meta = rs._ctx_meta
        attachments = rs.attachments
        card_context = rs.card_context
        cfg = rs.cfg
        emit = rs.emit
        exec_record = rs.exec_record
        gathered = rs.gathered
        message_is_user_turn = rs.message_is_user_turn
        mode = rs.mode
        narrator = rs.narrator
        overseer_signals = rs.overseer_signals
        started = rs.started

        def budget_exhausted() -> bool:
            size = sum(len(o.get("text", "")) + len(str(o.get("hits", ""))) for o in gathered)
            return (time.monotonic() - started) > cfg.max_elapsed_seconds or size > cfg.max_gathered_chars
        rs.budget_exhausted = budget_exhausted

        def finish(res: OrchestratorResult) -> OrchestratorResult:
            res.steps = rs.steps
            res.gathered = gathered_content(gathered)
            res.execution_record = exec_record
            # The metric record of the turn's deep goals, as data on the result itself (and on the terminal event
            # below), taken from the deep results alone: whoever shows a turn reads it here, not from any text.
            metric_data = deep_metric_data(res)
            res.metric_results = list(metric_data["metrics"])
            res.metric_missing = bool(metric_data.get("metric_missing"))
            res.retrieval_constraints = rs.retrieval_constraints
            res.mode_signal = rs.mode_signal_detected
            # PER-IDEA THREADING: the topic this turn was assigned to. Present on EVERY terminal
            # result (answer, deep, confirm, cancelled) once the flag is on, because the consumer
            # stamps the turn's messages from it and a turn that ended early still happened on an
            # idea. None when threading is off, or when the run ended before the first plan.
            if rs.card_thread_decision is not None:
                res.card_thread = rs.card_thread_decision.as_dict()
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
            if rs._oversee_executor is not None:
                try:
                    rs._oversee_executor.shutdown(wait=False)
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
                        rs.user_message, PlanDecision(action="deep", goal=rs.user_message), res,
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
                emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=rs.steps))
                return res
            # --- LINK GUARD (hubofhubs.critique.link_guard) ----------------------------------------------
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
                            # Provenance (policy.require_provenance) judges the reply against what
                            # THIS turn had. A deep worker's output below is exempt: what that
                            # worker read is not visible to the brain, so only reachability applies.
                            _evidence = (self.link_evidence_for(rs, res)
                                         if guard.policy.require_provenance else None)
                            res.text, _lv = guard.sanitize(res.text, evidence=_evidence)
                            res.link_checks.extend(v.to_dict() for v in _lv)
                        for _d in res.deep_results:
                            if getattr(_d, "output", None):
                                # A real Claude Code worker's output is exempt (what it read is
                                # invisible to the brain). An in-process runner's output is model
                                # writing: judged against the turn's evidence plus what that runner
                                # itself read.
                                _d_evidence = None
                                if getattr(_d, "model_written", False) and guard.policy.require_provenance:
                                    _d_evidence = self.link_evidence_for(rs, res)
                                _d.output, _lv = guard.sanitize(_d.output, evidence=_d_evidence)
                                res.link_checks.extend(v.to_dict() for v in _lv)
                        _stripped = [v for v in res.link_checks if v.get("verdict") != "ok"]
                        if _stripped:
                            log.info("Link guard stripped %d unverified link(s): %s",
                                     len(_stripped),
                                     ", ".join(f"{v['url']} ({v.get('reason') or v['verdict']})"
                                               for v in _stripped[:5]))
                except Exception as e:  # noqa: BLE001 -- guarding links must never lose the reply
                    log.warning("Link guard skipped: %s: %s", type(e).__name__, e, exc_info=True)
                # The same turn's DOIs: an invented bare DOI is not a link, so the guard above lets it through.
                self.strip_unverified_dois(rs, res)
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
                        rs.user_message,
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
                self._kickoff_anticipation(user_message=rs.user_message, ctx_meta=_ctx_meta)
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
                                        data={**({"exit_reason": res.exit_reason,
                                                  "goal_verdict": res.goal_verdict} if res.exit_reason else {}),
                                              **(metric_data if res.deep_results else {})}))
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
                    data={**({"future_context": _future,
                              "exit_reason": res.exit_reason,
                              "goal_verdict": res.goal_verdict} if (_future or res.exit_reason) else {}),
                          **metric_data}))
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
                        res, user_message=rs.user_message, goal_condition=rs.goal_condition,
                        cards=rs.explain_cards, sources=rs.explain_sources)
                    if is_eligible(_trace):
                        _explanation = self.write_answer_explanation(_trace)
                        if _explanation:
                            res.explanation = _explanation
                            emit.emit(ProgressEvent(type=EVENT_EXPLANATION, data=_explanation))
                except Exception as e:  # noqa: BLE001 -- explaining must never break a turn
                    log.debug("Answer explanation skipped: %s: %s", type(e).__name__, e,
                              exc_info=True)
            emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=rs.steps))
            return res
        rs.finish = finish

        rs.plan: Optional[PlanDecision] = None
        rs.steps = 0
        rs.consecutive_reads = 0  # Track how many steps in a row chose "read"
        rs.failed_reads: Dict[str, int] = {}  # read_spec_key -> times it came back as an error
        rs.pending_read_runs = 0  # Times the pending-read gate ran a planner-declared read
        # REPEATED READS: canonical spec -> (the step it ran at, its index among real observations),
        # and how many CONSECUTIVE steps asked for nothing new. Cleared after a tool step, since a
        # write can change what a read returns. Only a read that returned something is recorded.
        executed_reads: Dict[str, Tuple[int, int]] = {}
        rs.executed_reads = executed_reads
        rs.repeat_only_steps = 0
        # Set by an OVERSEER signal that decided the terminal path this run, so finish() can stamp
        # the exit_reason ("overseer_answer_now" | "overseer_escalated_deep" |
        # "overseer_escalated_human"). "" when the overseer did not decide the path.
        rs.overseer_decided = ""

    def _run_plan_loop(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """The bounded plan, gather and re-plan loop."""
        _ctx_meta = rs._ctx_meta
        _prior_escalation_digest_lines = rs._prior_escalation_digest_lines
        _recent_conversation_digest_lines = rs._recent_conversation_digest_lines
        budget_exhausted = rs.budget_exhausted
        cancel_check = rs.cancel_check
        card_context = rs.card_context
        card_thread_ctx = rs.card_thread_ctx
        cfg = rs.cfg
        emit = rs.emit
        exec_record = rs.exec_record
        executed_reads = rs.executed_reads
        finish = rs.finish
        gathered = rs.gathered
        guidance_selected_ids = rs.guidance_selected_ids
        hold_off_active = rs.hold_off_active
        message_is_user_turn = rs.message_is_user_turn
        narrator = rs.narrator
        overseer_signals = rs.overseer_signals
        quest_id = rs.quest_id
        rep_preamble = rs.rep_preamble
        started = rs.started
        tool_ok_signatures = rs.tool_ok_signatures
        transcript = rs.transcript
        # A person's turn bound to quest(s) is told its scope once, in the context every planner step and the
        # answer read (see ``scope_tags.SCOPE_FENCE_NOTE``); an unbound turn adds nothing.
        _scope_note = scope_fence_block(self._meta_quest_ids(_ctx_meta)) if message_is_user_turn else ""
        if _scope_note:
            rs.context_view = _scope_note + ("\n\n" + rs.context_view if rs.context_view else "")
        for step in range(cfg.max_steps):
            rs.steps = step + 1
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
                _pending_block = self._drain_pending(rs.pending_inputs)
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
            if rs.pending_oversee is not None:
                try:
                    _psig = self._collect_oversee(
                        rs.pending_oversee, signals=overseer_signals, emit=emit,
                        timeout=cfg.overseer_poll_timeout_seconds)
                    if _psig is not None:
                        rs.pending_oversee = None  # resolved -> free the slot for a fresh consult
                        if _psig.signal == "redirect" and _psig.hint:
                            gathered.append({
                                "kind": "query", "locator": "overseer",
                                "text": f"COURSE CORRECTION: {_psig.hint}",
                            })
                        elif _psig.signal == "answer_now":
                            rs.plan = rs.plan or PlanDecision(action="answer")
                            rs.plan.action = "answer"
                            rs.overseer_decided = "overseer_answer_now"
                            break
                        elif _psig.signal in ("escalate_deep", "escalate_human") and rs.brainstorm_active:
                            # Brainstorm mode: escalations may not ADD actions; treat as proceed.
                            pass
                        elif _psig.signal == "escalate_deep":
                            rs.plan = rs.plan or PlanDecision(action="answer")
                            rs.plan.action = "deep"
                            rs.plan.goal = rs.plan.goal or f"Complete the request: {rs.user_message}"
                            rs.plan.deep_brief = rs.plan.deep_brief or rs.user_message
                            rs.overseer_decided = "overseer_escalated_deep"
                            break
                        elif _psig.signal == "escalate_human":
                            # Genuine human-only fork (Fix 2): route through the SAME confirm /
                            # decision-request mechanism a planner-originated confirm uses, rather
                            # than guessing or executing. Never auto-run deep work for this signal.
                            rs.plan = rs.plan or PlanDecision(action="answer")
                            rs.plan.action = "confirm"
                            rs.plan.confirm_question = (
                                _psig.reason
                                or f"This needs your input before I continue: {rs.user_message}")
                            rs.overseer_decided = "overseer_escalated_human"
                            break
                    # else: not resolved -> keep ``pending_oversee`` and re-check next step.
                except Exception:  # noqa: BLE001 — the overseer must never break the loop
                    pass

            # AUTO-INJECT FUNCTION DISCOVERY on step 0: pre-load all available operations
            # so the planner sees them from the start, ordered by relevance. This eliminates
            # the need for the planner to first ASK for operations; they're already in hand.
            if step == 0 and self.retrieval is not None and rs._forced_plan is None:
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
            rs._prev_plan_for_gate = rs.plan

            try:
                if rs._forced_plan is not None and step == 0:
                    rs.plan = rs._forced_plan         # deep recipe: no planner call for a known operation
                else:
                    rs.plan = self._plan(rs.user_message, transcript, rs.context_view, gathered, step=step,
                                      narrate=narrator.enabled, persona=rep_preamble or "",
                                      already_said=narrator._said if narrator.enabled else None,
                                      brainstorm=rs.brainstorm_active,
                                      user_vetoed_task=(hold_off_active
                                                        and cfg.execution_mode != "brainstorm"),
                                      card_thread_block=rs.card_thread_block,
                                      tool_pins=(rs._ctx_meta or {}).get("tool_pins") or (),
                                      turn_facts={"user_intent": rs.turn_user_intent,
                                                  "runs_in_flight": (rs._ctx_meta or {}).get("runs_in_flight")})
            except Exception as e:  # noqa: BLE001 — planner failure -> grounded fallback answer
                log.exception(
                    f"Planner failed on step {rs.steps}: {e}. Falling back to grounded answer."
                )
                rs.plan = PlanDecision(action="answer", rationale="planner error → grounded answer")
            if getattr(rs.plan, "user_intent", None):
                rs.turn_user_intent = rs.plan.user_intent

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
            if card_thread_ctx is not None and (rs.card_thread_decision is None
                                                or rs.card_thread_decision.fell_back):
                try:
                    _decision = parse_card_thread(
                        rs.plan.card_thread if rs.plan else None,
                        active_card_id=card_thread_ctx.active_card_id,
                        known_ids=rs.card_thread_candidate_ids or None,
                    )
                    # Keep a real judgment over a fail-safe; keep the FIRST real judgment forever.
                    if rs.card_thread_decision is None or not _decision.fell_back:
                        _changed = (rs.card_thread_decision is None
                                    or _decision.as_dict() != rs.card_thread_decision.as_dict())
                        rs.card_thread_decision = _decision
                        if _changed:
                            emit.emit(ProgressEvent(
                                type=EVENT_CARD_THREAD, step=rs.steps,
                                data={**rs.card_thread_decision.as_dict(),
                                      "previous_card_id": card_thread_ctx.active_card_id,
                                      "internal": True}))
                except Exception:  # noqa: BLE001 -- reporting must never break the turn
                    log.debug("card thread resolution failed; continuing the current card",
                              exc_info=True)
                    if rs.card_thread_decision is None:
                        rs.card_thread_decision = CardThreadDecision(
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
            if (cfg.mode_signals_enabled and rs.plan and rs.plan.mode_signal
                    and rs.mode_signal_detected is None):
                if (cfg.execution_mode == "brainstorm"
                        and rs.plan.mode_signal == "exit_brainstorm"):
                    log.info("Brainstorm mode: ignoring the planner's exit_brainstorm; the release "
                             "judge owns the exit while the latch is held.")
                else:
                    rs.mode_signal_detected = rs.plan.mode_signal
                    # A planner mode signal may ENGAGE the gate, never clear the user's own veto.
                    rs.brainstorm_active = (hold_off_active
                                         or rs.plan.mode_signal == "enter_brainstorm")
                    try:
                        emit.emit(ProgressEvent(type=EVENT_MODE_SIGNAL, step=rs.steps,
                                                data={"signal": rs.mode_signal_detected,
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
            if rs.plan and rs.brainstorm_active and rs.plan.action in ("deep", "confirm", "clarify"):
                log.info("Brainstorm mode: degrading planner action %r to 'answer'.", rs.plan.action)
                rs.brainstorm_suppressed_action = rs.plan.action
                if rs.plan.action == "clarify":
                    rs.brainstorm_clarify_question = (
                        _clarify_question_text(rs.plan) or rs.brainstorm_clarify_question)
                rs.plan.action = "answer"
            # A tool that ACTS is an action like any other: held while the latch is. A read-only
            # tool is just more gathered context and still runs.
            if (rs.plan and rs.brainstorm_active and rs.plan.action == "tool" and self.tools is not None
                    and any((self.tools.get(c["name"]) is None
                             or self.tools.get(c["name"]).mutates) for c in rs.plan.tool_calls)):
                log.info("Brainstorm mode: holding the planner's tool call(s) %s.",
                         [c["name"] for c in rs.plan.tool_calls])
                rs.brainstorm_suppressed_action = rs.brainstorm_suppressed_action or "tool"
                rs.plan.action = "answer"

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
            if rs.plan and rs.plan.action == "answer" and cfg.full_read_before_answer:
                try:
                    _to_fetch = rs.abridged_state.should_force_read()
                except Exception:  # noqa: BLE001 -- the gate must never break the loop
                    _to_fetch = []
                if _to_fetch:
                    rs.abridged_state.forced = True
                    _forced_reads = [dict(it.fetch) for it in _to_fetch[: cfg.max_reads_per_step]]
                    log.info(
                        "Sufficiency gate: the plan would answer from %d abridged context item(s) "
                        "whose full text was never fetched; forcing a read step first (%s).",
                        len(_forced_reads), ", ".join(it.label for it in _to_fetch)[:200])
                    rs.plan.action = "read"
                    rs.plan.reads = _forced_reads
                    emit.status("Pulling the full text before answering…")

            # --- PENDING-READ GATE: honor the planner's own "I still owe a read" ------------------
            # The planner declares, in a structured field on the call it already makes, which
            # reads its decision still depends on (``pending_read``: sources it located but did
            # not open). An ``answer`` that carries any is the planner contradicting itself, and
            # live it ended turns on "the README is the file to open, I will check it" with the
            # file never opened. This runs the declared reads instead of answering. It reads the
            # planner's STRUCTURED field, never its prose (hard rule #3), so there is no word list
            # to leak. Bounded: at most PENDING_READ_MAX_RUNS times a turn, never past the step or
            # gather budget, and a spec that already ran this turn is not run again, so a planner
            # that keeps declaring the same read gets its answer instead of a loop.
            if (rs.plan and rs.plan.action == "answer" and rs.plan.pending_read
                    and rs.pending_read_runs < PENDING_READ_MAX_RUNS
                    and step + 1 < cfg.max_steps and not budget_exhausted()):
                _owed, _already = split_repeated_reads(
                    rs.plan.pending_read, {k: ran[0] for k, ran in executed_reads.items()})
                # A spec that already failed was attempted, so it is not "owed" any more.
                _owed = [sp for sp in _owed if read_spec_key(sp) not in rs.failed_reads]
                _owed = _owed[: cfg.max_reads_per_step]
                if _owed:
                    rs.pending_read_runs += 1
                    log.info("Pending-read gate: the planner chose to answer while declaring %d "
                             "read(s) it still owes; running them first.", len(_owed))
                    rs.plan.action = "read"
                    rs.plan.reads = _owed
                    emit.status("Reading before answering…")

            # Safety gate: if planner chose "read" for many consecutive steps, force a terminal action
            if rs.plan and rs.plan.action == "read":
                rs.consecutive_reads += 1
                # Escalate to deep if: max_consecutive_reads+ consecutive reads
                if rs.consecutive_reads >= cfg.max_consecutive_reads:
                    log.warning(
                        f"Planner stuck in read loop after {rs.steps} steps / {rs.consecutive_reads} reads. "
                        f"Force-escalating to {'answer' if rs.brainstorm_active else 'deep'} with gathered context."
                    )
                    if rs.brainstorm_active:
                        # Brainstorm may not act: wrap up with a grounded answer instead.
                        rs.plan.action = "answer"
                    else:
                        rs.plan.action = "deep"
                        rs.plan.goal = rs.plan.goal or f"Complete the request: {rs.user_message}"
                        rs.plan.deep_brief = rs.plan.deep_brief or rs.user_message
            else:
                rs.consecutive_reads = 0  # Reset when planner chooses something else
            # When narrating, the planner's `rationale` is ALREADY the user-facing spoken beat for
            # this step (written conversationally, in the rep's voice). We relay it through the same
            # narration channel as the instant ack (a partial), so the plan/replan event itself
            # carries no duplicate text. When not narrating, the plan event keeps the terse rationale
            # as its expandable detail (legacy behavior).
            emit.emit(ProgressEvent(type=(EVENT_PLAN if step == 0 else EVENT_REPLAN),
                                    action=rs.plan.action, step=rs.steps,
                                    text=(None if narrator.enabled else (rs.plan.rationale or None))))
            # Relay this planner decision as the next beat of the train of thought, but only for
            # actions that involve a real wait (gathering context, deep work) so quick answers add
            # no latency. Zero extra LLM call: the beat is the planner's own conversational rationale.
            if narrator.enabled and rs.plan.action in ("read", "deep"):
                narrator.relay(rs.plan.rationale)
            # Emit cumulative token counts so live consumers see usage grow in real time.
            rs._ti = getattr(self.provider, 'tokens_in', 0)
            rs._to = getattr(self.provider, 'tokens_out', 0)
            if rs._ti or rs._to:
                emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                        data={"tokens_in": rs._ti, "tokens_out": rs._to,
                                              "total": rs._ti + rs._to}))

            # --- OVERSEER hook A submit (in-loop, NON-BLOCKING): fire a fresh minimal-intervention
            # consult for THIS plan into a background thread, then keep walking (Fix 1). Its result
            # is polled at the top of the NEXT step (above), one step late. We only submit when the
            # plan is a "read" (the loop will continue, so there IS a next step to apply a signal to)
            # and when no consult is already in flight (one at a time). Gated so it stays cheap:
            # cadence, min-step, a hard cap on total submissions shared with hook B, AND (Fix 12) a
            # free non-LLM pre-filter inside ``_submit_oversee`` (``gate=True``, the default) that
            # skips the consult entirely unless something actually looks worth a look. Wrapped so it
            # can NEVER break the loop. Off by default.
            if (cfg.overseer and rs.plan.action == "read" and rs.pending_oversee is None
                    and rs.steps >= cfg.overseer_min_step
                    and rs.steps % max(1, cfg.overseer_every_steps) == 0
                    and rs.overseer_submitted < cfg.overseer_max_signals):
                try:
                    if rs._oversee_executor is None:
                        rs._oversee_executor = ThreadPoolExecutor(max_workers=1)
                    # Fix 3/4: judge the run against the RAW request (user_message) AND the RESOLVED
                    # request (goal_condition, when it differs) plus the completion bar
                    # (quality_standards). Fix 5a/7: also carry this-conversation history.
                    _prev_sig = ((rs._prev_plan_for_gate.action, rs._prev_plan_for_gate.goal)
                                if rs._prev_plan_for_gate is not None else None)
                    rs.pending_oversee = self._submit_oversee(
                        rs._oversee_executor, user_message=rs.user_message, goal_condition=rs.goal_condition,
                        step=rs.steps, plan=rs.plan, gathered=gathered, started=started,
                        quality_standards=rs.quality_standards,
                        recent_conversation=_recent_conversation_digest_lines,
                        prior_escalations=_prior_escalation_digest_lines,
                        prev_plan_signature=_prev_sig)
                    if rs.pending_oversee is not None:
                        rs.overseer_submitted += 1
                except Exception:  # noqa: BLE001 — the overseer must never break the loop
                    pass

            if rs.plan.action == "tool":
                self.run_tool_calls(rs.plan.tool_calls, gathered=gathered, exec_record=exec_record,
                                     tool_ok_signatures=tool_ok_signatures, emit=emit,
                                     steps=rs.steps, ctx=ToolContext(
                                         quest_id=quest_id,
                                         user_id=_ctx_meta.get("user_id"),
                                         task_id=_ctx_meta.get("task_id"),
                                         meta=dict(_ctx_meta)),
                                     on_success=self._recipe_learner(
                                         rs.user_message, _ctx_meta.get("scope_tags"),
                                         learnable=message_is_user_turn))
                rs.tool_calls_ran = True
                executed_reads.clear()
                rs.repeat_only_steps = 0
                if budget_exhausted():
                    break
                continue
            if rs.plan.action == "read":
                visible_from = planner_full_view_start(
                    len(gathered_content(gathered)), cfg.planner_recent_full,
                    cfg.planner_compress_over)
                fresh_reads, repeated_reads = split_repeated_reads(
                    rs.plan.reads, {k: ran[0] for k, ran in executed_reads.items()
                                 if ran[1] >= visible_from})
                # The per-step cap _do_reads applies, applied here so a spec it would drop is
                # never recorded as having run.
                fresh_reads = fresh_reads[: cfg.max_reads_per_step]
                for spec, ran_at in repeated_reads:
                    gathered.append(repeated_read_observation(spec, ran_at))
                if repeated_reads and not fresh_reads:
                    rs.repeat_only_steps += 1
                elif fresh_reads:
                    rs.repeat_only_steps = 0
                if not rs.plan.reads:
                    rs.plan.action = "answer"
                elif not fresh_reads and rs.repeat_only_steps >= 2:
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
                           for r in rs.plan.reads):
                        emit.status("Exploring…")
                    elif any(r.get("web") is not None for r in rs.plan.reads):
                        emit.status("Searching the web…")
                    elif any(r.get("web_page") for r in rs.plan.reads):
                        emit.status("Reading a web page…")
                    else:
                        emit.status("Searching…" if any(r.get("grep") for r in rs.plan.reads) else "Reading…")
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
                                executed_reads[read_spec_key(spec)] = (rs.steps, content_before + j)
                            elif isinstance(obs, dict):
                                # One failure stays retryable (a timeout may be transient); the
                                # same spec failing AGAIN is an answer, not a flake, so it is
                                # recorded as run and a third try is shown the repeat note
                                # instead of burning another step (a live turn re-issued one
                                # missing path four times).
                                _fkey = read_spec_key(spec)
                                rs.failed_reads[_fkey] = rs.failed_reads.get(_fkey, 0) + 1
                                if rs.failed_reads[_fkey] >= 2:
                                    executed_reads[_fkey] = (rs.steps, content_before + j)
                    for obs in new_obs:
                        if isinstance(obs, dict) and obs.get("planner_note"):
                            gathered.append(planner_note_observation(
                                str(obs["planner_note"]), "not_answered_by"))
                    # The turn's REAL record of what was fetched, for the sufficiency gate above:
                    # these specs actually executed, whoever chose them (the planner on its own, or
                    # the gate). Recorded here, at the one place reads run, so the gate can never be
                    # satisfied by a plan that merely mentioned a fetch.
                    rs.abridged_state.record_reads(fresh_reads)
                    rs._sources: List[str] = []
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
                                    rs._sources.append(_rp)
                            if not _o_hits:
                                _pat = _o.get("pattern") or ""
                                if _pat:
                                    rs._sources.append(f"(searched {_pat!r} — nothing found)")
                        elif _o_hits and any(isinstance(_h, dict) and _h.get("url") for _h in _o_hits):
                            # A web search observation: show the RESULT URLs, not the synthetic
                            # "web_search:<query>" rel_path the adapter stamps on it.
                            _seen_u: set = set()
                            for _h in _o_hits:
                                _u = _h.get("url") if isinstance(_h, dict) else None
                                if _u and _u not in _seen_u:
                                    _seen_u.add(_u)
                                    rs._sources.append(_u)
                        else:
                            # A web PAGE fetch has no hits and ``rel_path`` is the fetched URL
                            # itself, so it falls through here unchanged.
                            _rp = _o.get("rel_path") or _o.get("pattern")
                            if _rp:
                                rs._sources.append(_rp)
                    emit.emit(ProgressEvent(type=EVENT_READ, step=rs.steps,
                                            data={"reads": len(fresh_reads),
                                                  "sources": rs._sources[:8]}))
                    if budget_exhausted():
                        break
                    continue
            if rs.plan.action in ("answer", "deep", "confirm", "clarify"):
                break
        else:
            rs.plan = rs.plan or PlanDecision(action="answer")

        rs.final = (rs.plan or PlanDecision(action="answer")).action
