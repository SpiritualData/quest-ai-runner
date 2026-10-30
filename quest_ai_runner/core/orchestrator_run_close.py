"""Closing a turn: deferred deep work, goal verification of the answer, and the final result.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance and in the
run-local namespace ``rs`` that ``RunMixin.run`` builds and hands from one phase to the next.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

from .auxiliary_prompts import SUPPORT_REWRITE_STEER
from hubofhubs.ports.events import EVENT_INTENT, EVENT_MILESTONE, Mode
from hubofhubs.ports.deep import ProgressEvent
from .adapters_values import PlanDecision
from .orchestrator_constants import log, result_landed_work
from .orchestrator_decide_tool import (
    DEFERRED_RUNNER_KEY,
    OWN_ESCALATION_KEPT_STATUS,
    own_escalation_adds_nothing,
)
from .orchestrator_decisions import _truncate_goal, user_ordered_work
from .orchestrator_events import grounding_context_layer
from .orchestrator_text import _strip_future_context, unconfirmed_no_change_text
from .orchestrator_types import OrchestratorResult
from .planner_context import gathered_content
from .planner_prompt import NOT_QUEUED_NOTE
from .tools_status import fact_is_held


class RunCloseMixin:
    """Closing a turn: deferred deep work, goal verification of the answer, and the final result."""

    def keep_landed_work_reported(self, rs: SimpleNamespace, text: str) -> str:
        """Never let a reply deny work the execution record proves landed.

        After a deferred run that really changed something, the reply is a model's rewrite (the
        post-run synthesis, and any goal-loop regeneration steered by the verifier). Both read the
        pre-work proposal, which says nothing has been made yet, and a rewrite that trusts it
        tells the person the landed change was not made. The run's own account of what landed
        (``rs.landed_account``) is code-written from the write receipts, so it wins: when the
        record shows a mutation succeeded and none failed, and a judge says the rewrite denies the
        account, the account itself is the reply. Judged on structure plus one contradiction
        verdict, never on keywords; a judge that cannot run keeps the reply. Never raises."""
        try:
            account = (getattr(rs, "landed_account", "") or "").strip()
            exec_record = rs.exec_record
            if (not account or not (text or "").strip() or exec_record is None
                    or not exec_record.any_success or exec_record.any_failure):
                return text
            if self.reply_denies_landed_work(rs.user_message, text, account) is True:
                log.warning("reply denied work the execution record proves landed; "
                            "reporting the run's own account instead")
                return account
        except Exception:  # noqa: BLE001 — a guard must never break the turn
            log.warning("landed-work guard failed", exc_info=True)
        return text

    def _run_deferred_work(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """Hand deferred deep work off (or run it inline) when the answer turn calls for it."""
        _ctx_meta = rs._ctx_meta
        _gen_answer = rs._gen_answer
        _queued_mode = rs._queued_mode
        cancel_check = rs.cancel_check
        emit = rs.emit
        exec_record = rs.exec_record
        finish = rs.finish
        gathered = rs.gathered
        pin_queue = rs.pin_queue
        rep_preamble = rs.rep_preamble
        take_resume_session = rs.take_resume_session
        transcript = rs.transcript
        working_dir_override = rs.working_dir_override
        if rs.should_defer_deep:
            try:
                if pin_queue:
                    emit.status("Handing this work to the background queue…")
                elif not rs.plan.deferred_deep:
                    emit.status("Executing follow-up work…")
                else:
                    emit.status("Continuing with the follow-up work now…")
                deferred_plan = PlanDecision(
                    action="deep",
                    goal=_truncate_goal(rs.should_defer_deep.get("goal") or f"Execute: {rs.user_message}"),
                    deep_brief=(rs.should_defer_deep.get("brief") or rs.user_message)[:2000],
                    rationale=rs.should_defer_deep.get("rationale") or "follow-up work from answer phase",
                    deep_difficulty=getattr(rs.plan, "deep_difficulty", None),
                    deep_difficulty_reason=getattr(rs.plan, "deep_difficulty_reason", None),
                )
                deep_model = self._answer_model(deferred_plan, "balanced", hint=rs.model_hint)
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
                deep_res = self._run_deep(deferred_plan, rs.user_message, deep_model,
                                         emit=emit, rep_preamble=rep_preamble,
                                         exec_record=exec_record, gathered=gathered,
                                         quality_standards=rs.quality_standards,
                                         pending_inputs=rs.pending_inputs, model_hint=rs.model_hint,
                                         ctx_meta=_ctx_meta, cancel_check=cancel_check,
                                         runner_override=_deferred_runner,
                                         working_dir_override=working_dir_override,
                                         resume_session_id=take_resume_session(),
                                         run_control=rs.run_control, card_context=rs.card_context)
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
                    rs.text = self._synthesize_after_queued(
                        rs.user_message, prior_answer=rs.text, handoff_output=_handoff_out,
                        transcript=transcript, model=rs.model, rep_preamble=rep_preamble)
                    rs._deferred_handoff_confirmed = True
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
                    rs.text = self._synthesize_after_deep(
                        rs.user_message, prior_answer=rs.text, deep_output=deep_output,
                        transcript=transcript, model=rs.model, rep_preamble=rep_preamble,
                        standard=rs.should_defer_deep.get("goal") or "")
                    _deep_block = "--- WHAT WAS JUST EXECUTED (deep run output) ---\n" + deep_output
                    rs.context_view = (rs.context_view + "\n\n" + _deep_block) if rs.context_view else _deep_block
                    rs._deferred_deep_grounded = True
                    rs.landed_account = deep_output
                    rs.text = self.keep_landed_work_reported(rs, rs.text)
                    if unconfirmed_text and unconfirmed_text not in rs.text:
                        # A sibling that did NOT land: its own honest text, verbatim, after the
                        # landed work and never through the synthesis above.
                        rs.text = f"{rs.text}\n\n{unconfirmed_text}"
                    if _parked is not None:
                        # Something ALSO parked alongside the landed work this turn: append its
                        # own code-written wording verbatim, never through the "already done"
                        # synthesis above, so the ask is neither dropped nor rewritten into a claim.
                        _parked_text = (_parked.output or "").strip()
                        if _parked_text and _parked_text not in rs.text:
                            rs.text = f"{rs.text}\n\n{_parked_text}"
                        rs._decision_parked = True
                        rs._parked_decision_id = _parked.decision_id
                elif _parked is not None:
                    # NOTHING landed this turn: the whole deferred attempt resolved to a parked
                    # approval card. The reply IS that card's own code-written lead, verbatim --
                    # not a fresh LLM synthesis grounded on a misleading "you already DID the
                    # work" framing, which is what fabricated the false completion this guards.
                    if emit is not None:
                        emit.status("Parked on an approval card, waiting for your answer…")
                    _parked_text = (_parked.output or "").strip()
                    # What the person asked to be TOLD survives the park. A request that reads
                    # something and then changes something has two halves; the answer written
                    # before the change (grounded in this turn's reads) is the first half, and
                    # dropping it left the person with only an approval prompt that never said
                    # what the read found (a stored injury, never surfaced). It is kept only
                    # when the turn really read something, so a bare "I'll do it" announcement is
                    # still replaced by the ask. Pre-work text, never a done claim: the answer
                    # step cannot claim a change.
                    _findings = ((rs.text or "").strip()
                                 if gathered_content(gathered) and _parked_text not in (rs.text or "")
                                 else "")
                    # What happened first (a sibling that did not land, in its own words), then
                    # the one ask.
                    reply_parts = [p for p in (_findings, unconfirmed_text, _parked_text) if p]
                    if reply_parts:
                        rs.text = "\n\n".join(reply_parts)
                    rs._decision_parked = True
                    rs._parked_decision_id = _parked.decision_id
                elif unconfirmed_text:
                    # Nothing landed and nothing parked: the runs failed or could not be verified.
                    # Their own honest text IS the reply, verbatim; the goal-verification loop is
                    # skipped (``deferred_unconfirmed``) so no regeneration can rewrite a failure
                    # toward "done".
                    rs.text = unconfirmed_text
                    rs.deferred_unconfirmed = True
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
                        rs.text = _gen_answer(
                            "The attempt to hand this work to the background queue FAILED: "
                            + _handoff_err + "\n\nRewrite your reply to be honest: the work has NOT "
                            "been queued, started, or done. Keep any real findings you already have, "
                            "say plainly that the background hand-off failed, and suggest the user "
                            "try again or ask you to retry. Do not use em dashes.\n\n"
                            f"--- YOUR PREVIOUS ANSWER ---\n{rs.text}")
                    except Exception:  # noqa: BLE001 — honesty must not depend on an LLM call
                        # DETERMINISTIC FLOOR. The regeneration is the only thing standing between
                        # the user and a pre-deep draft that (under queued doctrine) may already
                        # claim "I have queued this". If that call fails, we still correct the
                        # record without a model: append the plain not-queued sentence.
                        log.warning("honest-enqueue rewrite failed; appending the not-queued note",
                                    exc_info=True)
                        rs.text = ((rs.text or "").strip() + "\n\n" + NOT_QUEUED_NOTE).strip()
                # Async, best-effort: prepare this user's cards for next time from the deferred run.
                # Skipped for a confirmed hand-off: a queue receipt sentinel holds nothing to learn.
                if not rs._deferred_handoff_confirmed:
                    self._kickoff_card_update(deep_res, deferred_plan, rs.user_message, _ctx_meta,
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
        rs._last_verdict: Optional[Dict[str, Any]] = None
        rs._claim_corrected = False
        # Set when one of the orchestrator's own escalations resolved to a background-work runner
        # and was not started (see _run_deep's ``self_initiated``): no further escalation this turn.
        rs.own_escalation_settled = False

    def _run_verify_answer(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """Hold the answer to the goal: verification, regeneration, the last-resort deep run, and the final result."""
        _ctx_meta = rs._ctx_meta
        _gen_answer = rs._gen_answer
        attachments = rs.attachments
        cancel_check = rs.cancel_check
        emit = rs.emit
        exec_record = rs.exec_record
        finish = rs.finish
        gathered = rs.gathered
        message_is_user_turn = rs.message_is_user_turn
        mode = rs.mode
        rep_preamble = rs.rep_preamble
        take_resume_session = rs.take_resume_session
        transcript = rs.transcript
        working_dir_override = rs.working_dir_override
        if (not rs._deferred_handoff_confirmed and not rs._decision_parked and not rs.deferred_unconfirmed
                and (self.cfg.answer_goal_max_iterations > 1 or self.cfg.verify_claims)):
            try:
                # Verify against the turn's DERIVED GOAL CONDITION (the checkable done-standard
                # established at turn start by _understand_input/_derive_goal_condition) when one
                # was actually derived: that is the user-level bar for the WHOLE turn. Deriving a
                # done-standard and then judging the answer against something else (the plan's own
                # goal restatement) would let the two drift. When no distinct condition was derived
                # (it equals the raw message, e.g. the derivation failed safe), fall back to the
                # plan's goal, then to the generic bar, exactly as before.
                overall_goal = (rs.goal_condition or "").strip()
                if not overall_goal or overall_goal == (rs.user_message or "").strip():
                    overall_goal = (rs.plan.goal or "").strip() or (
                        "Fully and correctly answer the user's request to their satisfaction: "
                        + rs.user_message)
                # The SAME rendered L2 context layer the answer call(s) this turn used (see
                # ``_grounded_answer``'s ``layers`` wiring): ``context_view`` does not change across
                # this loop's regenerate/verify iterations (a regeneration only rewrites ``text``), so
                # computing it once here gives every verify call in this loop a byte-identical L2 to
                # the answer it is judging, and the same L2 across iterations too.
                answer_context_layer = grounding_context_layer(rs.context_view)
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
                        overall_goal, rs.user_message, rs.text,
                        rep_preamble=rep_preamble,
                        quality_standards=rs.quality_standards,
                        transcript=transcript,
                        exec_record=exec_record if self.cfg.verify_claims else None,
                        context_layer=answer_context_layer,
                        gathered=gathered,
                        check_support=bool(self.cfg.verify_claims and self.cfg.verify_support))
                    if verdict is not None:
                        rs._last_verdict = verdict
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
                                       and not rs.brainstorm_active
                                       and exec_record is not None
                                       and not exec_record.any_success
                                       and not exec_record.any_failure
                                       and not any(fact_is_held(f) for f in exec_record.facts)
                                       and _remediations < max(0, self.cfg.max_remediations))
                        if can_execute:
                            _remediations += 1
                            if emit is not None:
                                emit.status("That change was claimed but never executed, doing it now…")
                            _rem_plan = PlanDecision(
                                action="deep",
                                goal=_truncate_goal(f"Carry out the user's request for real: {rs.user_message}"),
                                deep_brief=(f"{rs.user_message}\n\nThe assistant REPLIED as if this was "
                                            f"already done, but nothing actually executed. APPLY the "
                                            f"change for real now (make the actual code/file/data "
                                            f"changes):\n{(rs.text or '').strip()}")[:2000],
                                rationale="claim verification: answer claimed unexecuted work",
                                deep_difficulty=getattr(rs.plan, "deep_difficulty", None),
                                deep_difficulty_reason=getattr(rs.plan, "deep_difficulty_reason", None),
                            )
                            _rem_model = self._answer_model(_rem_plan, "balanced", hint=rs.model_hint)
                            _rem_res = self._run_deep(_rem_plan, rs.user_message, _rem_model,
                                                      emit=emit, rep_preamble=rep_preamble,
                                                      exec_record=exec_record, gathered=gathered,
                                                      quality_standards=rs.quality_standards,
                                                      pending_inputs=rs.pending_inputs,
                                                      model_hint=rs.model_hint, ctx_meta=_ctx_meta,
                                                      cancel_check=cancel_check,
                                                      working_dir_override=working_dir_override,
                                                      resume_session_id=take_resume_session(),
                                                      run_control=rs.run_control,
                                                      card_context=rs.card_context)
                            if _rem_res.kind == "cancelled":
                                return finish(_rem_res)
                            if not attachments:
                                self._recipe_learn_deep(
                                    rs.user_message, PlanDecision(action="deep", goal=rs.user_message,
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
                                rs.text = self._synthesize_after_deep(
                                    rs.user_message, prior_answer=rs.text, deep_output=_rem_out,
                                    transcript=transcript, model=rs.model, rep_preamble=rep_preamble)
                                self._kickoff_card_update(_rem_res, _rem_plan, rs.user_message,
                                                          _ctx_meta, emit, gathered=gathered)
                            continue  # re-verify the remediated answer (attempt not consumed)
                        # Cannot safely re-run: correct the reply instead and flag the result so a
                        # background task maps to needs_you/failed, never a false done.
                        rs._claim_corrected = True
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
                                 f"--- YOUR PREVIOUS ANSWER ---\n{rs.text}")
                        rs.text = _gen_answer(steer)
                        _attempt += 1
                        continue
                    # When the verifier says the answer lacked the context needed to be definitive,
                    # escalate to deep so it can search further. Regenerating with the SAME gathered
                    # context won't help — the deep runner can grep/read on its own.
                    if (verdict.get("need_more_context") and not rs.brainstorm_active
                            and not rs.own_escalation_settled
                            and self._has_deep_execution_capability()):
                        if emit is not None:
                            emit.status("Need more context to answer, searching further…")
                        _context_q = verdict.get("context_query") or rs.user_message
                        _esc_plan = PlanDecision(
                            action="deep",
                            goal=_truncate_goal(overall_goal),
                            deep_brief=(
                                f"{rs.user_message}\n\n"
                                "NOTE: An initial search was done but the result was inconclusive. "
                                f"What is missing: {_context_q}. "
                                "Search more thoroughly and give a definitive answer."
                            ),
                            rationale="answer verification escalated to deep (insufficient context)",
                            deep_difficulty=getattr(rs.plan, "deep_difficulty", None),
                            deep_difficulty_reason=getattr(rs.plan, "deep_difficulty_reason", None),
                        )
                        _esc_model = self._answer_model(_esc_plan, "balanced", hint=rs.model_hint)
                        esc_facts_before = len(getattr(exec_record, "facts", None) or [])
                        _esc_res = self._run_deep(
                            _esc_plan, rs.user_message, _esc_model,
                            emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                            gathered=gathered, quality_standards=rs.quality_standards,
                            pending_inputs=rs.pending_inputs, model_hint=rs.model_hint,
                            ctx_meta=_ctx_meta, cancel_check=cancel_check,
                            working_dir_override=working_dir_override,
                            resume_session_id=take_resume_session(), self_initiated=True,
                            user_ordered=user_ordered_work(rs.turn_user_intent),
                            run_control=rs.run_control, card_context=rs.card_context)
                        if _esc_res.kind == "cancelled":
                            return finish(_esc_res)
                        if own_escalation_adds_nothing(_esc_res, exec_record, esc_facts_before):
                            # Not started (see _run_deep's ``self_initiated``) or it produced
                            # nothing; the last-resort run below must not try again. The status
                            # line above announced a search, so say it ended here.
                            rs.own_escalation_settled = True
                            log.info("own escalation (need more context) added nothing; "
                                     "keeping the answer")
                            if emit is not None:
                                emit.status(OWN_ESCALATION_KEPT_STATUS)
                        else:
                            _esc_res.exit_reason = "escalated_deep"
                            _esc_res.goal_verdict = verdict
                            # Async, best-effort: prepare this user's cards for next time.
                            self._kickoff_card_update(_esc_res, _esc_plan, rs.user_message, _ctx_meta,
                                                      emit, gathered=gathered)
                            return finish(_esc_res)
                    # SUPPORT: the reply states facts or advice as certain that nothing this turn
                    # backs. Rewrite it ONCE (state the basis, hedge, say there is no evidence, or
                    # drop the claim). The flag is spent after one rewrite: a second verdict that
                    # still flags it ends the loop with the reply as written rather than looping.
                    if verdict.get("claims_unsupported"):
                        if getattr(rs, "support_rewritten", False):
                            break
                        rs.support_rewritten = True
                        if emit is not None:
                            emit.status("Adding the basis for the claims in the reply…")
                        rs.text = self.keep_landed_work_reported(rs, _gen_answer(
                            SUPPORT_REWRITE_STEER.format(
                                reason=verdict.get("reason") or "see the claims above", text=rs.text)))
                        _attempt += 1
                        continue
                    # Goal not met: surface the current answer as a milestone so the user sees
                    # progress while we continue iterating toward the goal.
                    if emit is not None and rs.text:
                        emit.emit(ProgressEvent(type=EVENT_MILESTONE, text=rs.text,
                                                data={"goal_not_met": True,
                                                      "reason": verdict.get("reason") or ""}))
                    if emit is not None:
                        emit.status("Answer not yet at the bar, improving it…")
                    _new = self._drain_pending(rs.pending_inputs)
                    steer = (f"Why your previous answer fell short: "
                             f"{verdict.get('reason') or 'it did not meet the quality bar'}. "
                             f"Do this now: {verdict.get('next_action') or 'address the gap and answer fully.'}\n\n"
                             + (_new + "\n\n" if _new else "")
                             + f"--- YOUR PREVIOUS ANSWER ---\n{rs.text}")
                    rs.text = self.keep_landed_work_reported(rs, _gen_answer(steer))
                    _attempt += 1
            except Exception:  # noqa: BLE001 — answer verification must never break the turn
                log.warning("answer goal verification failed", exc_info=True)

        # LAST-RESORT DEEP RUN. The goal is definitively not met, the turn never ran deep work, and a
        # deep path is wired: use it before giving up. Covers every way the loop above can end
        # without the in-loop escalation firing (attempts exhausted after a regeneration, the
        # verifier naming a gap without setting need_more_context, a non-answer like "which file
        # should I look in?"). One pass only; skipped when something already mutated this turn (a
        # re-run could double the change) or the human asked us to hold off.
        if (self.cfg.deep_before_giving_up and not rs.own_escalation_settled
                and not rs._deferred_handoff_confirmed and not rs._decision_parked
                and not rs.deferred_unconfirmed
                and not rs._claim_corrected and not rs.brainstorm_active
                and rs._last_verdict is not None and not rs._last_verdict.get("met")
                and not rs._last_verdict.get("claims_unsupported")
                and not _ctx_meta.get("deep_attempted")
                and self._has_deep_execution_capability()
                and (exec_record is None or not exec_record.any_mutation_attempted)):
            try:
                if emit is not None:
                    emit.status("Not enough to answer yet, searching deeper before giving up…")
                _gap = (rs._last_verdict.get("next_action") or rs._last_verdict.get("reason")
                        or "the answer did not meet the goal")
                _lr_plan = PlanDecision(
                    action="deep",
                    goal=_truncate_goal(overall_goal),
                    deep_brief=(
                        f"{rs.user_message}\n\n"
                        "NOTE: The quick answer did not meet the goal and no deep search has been "
                        f"done yet. What is missing: {_gap}. Search the corpus, cards, notes and "
                        "files thoroughly and give a definitive answer. Do not ask the user which "
                        "file or quest to look in; find it yourself. Only say something could not "
                        "be found after actually searching, and name where you looked."
                    )[:2000],
                    rationale="goal not met and no deep run yet: deep run before giving up",
                    deep_difficulty=getattr(rs.plan, "deep_difficulty", None),
                    deep_difficulty_reason=getattr(rs.plan, "deep_difficulty_reason", None),
                )
                _lr_model = self._answer_model(_lr_plan, "balanced", hint=rs.model_hint)
                lr_facts_before = len(getattr(exec_record, "facts", None) or [])
                _lr_res = self._run_deep(
                    _lr_plan, rs.user_message, _lr_model,
                    emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                    gathered=gathered, quality_standards=rs.quality_standards,
                    pending_inputs=rs.pending_inputs, model_hint=rs.model_hint,
                    ctx_meta=_ctx_meta, cancel_check=cancel_check,
                    working_dir_override=working_dir_override,
                    resume_session_id=take_resume_session(), self_initiated=True,
                    user_ordered=user_ordered_work(rs.turn_user_intent),
                    run_control=rs.run_control, card_context=rs.card_context)
                if _lr_res.kind == "cancelled":
                    return finish(_lr_res)
                if not own_escalation_adds_nothing(_lr_res, exec_record, lr_facts_before):
                    _lr_res.exit_reason = "escalated_deep"
                    _lr_res.goal_verdict = rs._last_verdict
                    self._kickoff_card_update(_lr_res, _lr_plan, rs.user_message, _ctx_meta, emit,
                                              gathered=gathered)
                    return finish(_lr_res)
                log.info("last-resort escalation added nothing; keeping the answer")
                if emit is not None:
                    emit.status(OWN_ESCALATION_KEPT_STATUS)
            except Exception:  # noqa: BLE001 — the net must never break the turn
                log.warning("last-resort deep run failed", exc_info=True)

        _exit_reason = "unverified"
        if rs._deferred_handoff_confirmed:
            # The work was confirmed queued out-of-band (deferred contract): neither verified nor
            # unverified applies to this turn; the external runner verifies the real outcome.
            _exit_reason = "deferred"
        elif rs._decision_parked:
            # This turn's change was parked on an approval decision, not completed: the goal is
            # intentionally not met yet (a human still has to answer the card), so this is neither
            # "verified" nor "unverified" -- it never entered that loop at all (see the gate above).
            _exit_reason = "parked"
        elif rs._last_verdict is not None:
            _exit_reason = "verified" if rs._last_verdict.get("met") else "max_turns"
        # An overseer answer_now that short-circuited the read loop to this answer wins the reason,
        # so a consumer can see the terminal path was decided by the overseer.
        if (rs.overseer_decided == "overseer_answer_now" and rs._last_verdict is None
                and not rs._deferred_handoff_confirmed and not rs._decision_parked):
            _exit_reason = "overseer_answer_now"
        _res = OrchestratorResult(kind="answer", text=rs.text, rationale=rs.plan.rationale,
                                  model=rs.model, exit_reason=_exit_reason,
                                  goal_verdict=rs._last_verdict,
                                  decision_id=rs._parked_decision_id)
        if rs._claim_corrected:
            # The reply had to be corrected for honesty and the claimed work never executed: flag
            # the result so a background task maps to needs_you/failed, never a false done.
            _res.claim_corrected = True
            _res.partial = True
        return finish(_res)
