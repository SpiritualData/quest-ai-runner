"""Settling the loop's final action and drafting the answer, with the overseer's answer checkpoint.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance and in the
run-local namespace ``rs`` that ``RunMixin.run`` builds and hands from one phase to the next.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

from hubofhubs.api import ScopedThreadPoolExecutor as ThreadPoolExecutor
from hubofhubs.api import EVENT_INTENT, EVENT_STATUS, EVENT_TOKENS, Mode
from hubofhubs.api import ProgressEvent, evidence_text
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.call_roles import tier_for_call
from quest_ai_runner.the_world.tools import tool_guards
from quest_ai_runner.the_intelligence.mind.context_intake import shared_thread
from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import ask_state_block
from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
from quest_ai_runner.the_intelligence.mind.context_intake.context_doctrine import CARD_LIFECYCLE_GATE
from quest_ai_runner.the_intelligence.mind.orchestrator_constants import log
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decide_tool import (
    OWN_ESCALATION_KEPT_STATUS, USER_INTENT_HOLD_OFF, own_escalation_adds_nothing)
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import (
    FACTS_ANSWER_HEAD, _clarify_question_text, _truncate_goal, facts_block, user_ordered_work)
from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_text import _answer_describes_unexecuted_work, tool_landed_account
from quest_ai_runner.the_intelligence.mind.orchestrator_types import OrchestratorResult
from quest_ai_runner.the_intelligence.mind.delegation.overseer import OverseerSignal
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import (
    deliverable_texts,
    CLARIFY_DIRECTIVE, SHAPE_DEFAULT, constraint_violated, is_clarify, found_constraint_head, found_constraint_hint, keep_addresses_hint, lost_pointers, measure, pointed_items, repair, repair_limit, standard_for, steering, GENERAL_ANSWER_DIRECTIVE, SHAPE_PLAN, SHAPE_SETTLED, SHAPE_SPOKEN, SPOKEN_DIRECTIVE,
    turn_holds_records)
from quest_ai_runner.the_intelligence.mind.context_intake.planner_context import gathered_content
from quest_ai_runner.the_intelligence.critical_thinking.response_quality.link_provenance_pass import http_addresses
from quest_ai_runner.the_intelligence.critical_thinking.unmet_goal import pursue_before_giving_up, turn_awaits_person
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import is_information_turn
from quest_ai_runner.the_intelligence.mind.response_writer.fact_supersession import resolve as resolve_facts
from quest_ai_runner.the_intelligence.mind.planner.planner_prompt import (
    BRAINSTORM_CLARIFY_ACK_PREFIX, BRAINSTORM_HELD_WORK_ACK_NOTE, BRAINSTORM_NO_ACTION_ACK_NOTE, HOLD_OFF_HELD_WORK_ACK_NOTE,
    HOLD_OFF_NO_ACTION_ACK_NOTE, ASK_FIRST_DIRECTIVE, DRAFT_ANSWER_DIRECTIVE, HANDOFF_ANSWER_NOW_DIRECTIVE, INFORMATION_ANSWER_DIRECTIVE, NO_ATTACHMENT_DIRECTIVE, SETTLED_ANSWER_DIRECTIVE, STATUS_SNAPSHOT_DIRECTIVE, PLAN_SNAPSHOT_DIRECTIVE, NO_ATTACHMENT_LINE,
    READ_BUDGET_WRAPUP_HONESTY_NOTE)


class RunAnswerMixin:
    """Settling the loop's final action and drafting the answer, with the overseer's answer checkpoint."""


    def resolved_facts_of(self, rs: SimpleNamespace, gathered: Any) -> Any:
        """The facts the turn's records gave at more than one date, resolved by date (``fact_supersession``)."""
        recent = getattr(rs, "recent_context_text", "") or "\0"
        return resolve_facts(evidence_text(rs.user_message, getattr(rs, "transcript", ""),
                                           (getattr(rs, "context_view", "") or "").replace(recent, ""), gathered))

    def hold_to_standard(self, rs: SimpleNamespace, text: str, gathered: Any, write: Any) -> str:
        """Hold a reply written outside the answer stage (the read-budget wrap-up) to its situation standard: measure it against the
        turn's reads, regenerate with the measurement's own hint up to ``reply_standard_max_repairs`` times, then the deterministic repair.
        The same measurements the answer stage uses (figures, dates, superseded readings, length, spoken shape). Never raises."""
        cfg = self.cfg
        if not getattr(cfg, "reply_standards", False) or rs.plan is None or not (text or "").strip():
            return text
        try:
            standard = standard_for(rs.plan, user_intent=rs.turn_user_intent, delivery=rs.turn_delivery)
            exec_record = rs.exec_record
            record = exec_record.summary() if exec_record is not None else ""
            pool = evidence_text(rs.user_message, gathered) + "\n" + record
            evidence = self.link_evidence_for(rs, SimpleNamespace(deep_results=[])) + "\n" + record
            recent = getattr(rs, "recent_context_text", "") or "\0"
            basis = evidence_text(rs.user_message, getattr(rs, "transcript", ""),
                                  (getattr(rs, "context_view", "") or "").replace(recent, ""), gathered, pool)
            facts = list(getattr(rs.plan, "facts_read", None) or [])
            resolved = self.resolved_facts_of(rs, gathered)
            measured = measure(text, standard, evidence, pool, basis, facts, resolved=resolved, question=rs.user_message, drafted=deliverable_texts(rs, text))
            rounds = 0
            while measured.failed and rounds < repair_limit(cfg, standard):
                text = write(steering(measured.hint(), text)) or text
                rounds += 1
                measured = measure(text, standard, evidence, pool, basis, facts, resolved=resolved, question=rs.user_message, drafted=deliverable_texts(rs, text))
            if measured.failed:
                text = repair(text, standard, measured, evidence, pool, basis, question=rs.user_message, facts=facts, drafted=deliverable_texts(rs, text))
            return text
        except Exception:  # noqa: BLE001 -- a standard must never break the turn
            return text

    def regeneration_model(self, rs: SimpleNamespace) -> str:
        """The model a corrected reply is written with: the reply role's model again (a reply is never below balanced, so there is no tier to move up from;
        a pinned model id stays pinned)."""
        return rs.model

    def _run_route_action(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """Route the loop's final action: gates, clarify, confirm, deep, and the first answer draft."""
        _ctx_meta = rs._ctx_meta
        attachments = rs.attachments
        cancel_check = rs.cancel_check
        cfg = rs.cfg
        context_meta = rs.context_meta
        emit = rs.emit
        exec_record = rs.exec_record
        finish = rs.finish
        gathered = rs.gathered
        hold_off_active = rs.hold_off_active
        message_is_user_turn = rs.message_is_user_turn
        mode = rs.mode
        nets_honor_hold_off = rs.nets_honor_hold_off
        quest_id = rs.quest_id
        rep_preamble = rs.rep_preamble
        take_resume_session = rs.take_resume_session
        transcript = rs.transcript
        working_dir_override = rs.working_dir_override
        # A turn that ENDED on a tool step (the budget ran out right after the calls ran) or on a
        # read after its tool calls already ran has done its work: it answers with the receipts in
        # ``gathered``. It must not fall into the read-budget wrap-up below, whose change-request
        # net would otherwise escalate "send the email" to a deep run after the email was sent.
        if rs.final == "tool" or (rs.final == "read" and rs.tool_calls_ran):
            rs.plan.action = rs.final = "answer"

        # --- BRAINSTORM TERMINAL GATE (the single choke point) --------------------------------
        # Whatever set the action -- the planner, the read-loop safety escalation, an overseer
        # signal -- a latched turn may neither ACT nor ESCALATE. Every terminal path below funnels
        # through here, so the invariant ("a latched turn executes nothing and creates no
        # decision-request") holds structurally instead of depending on each path remembering to
        # check the latch. "clarify"/"confirm" carry a question the user still deserves to hear, so
        # it is not dropped: it rides into the reply text via the acknowledgment note below.
        if rs.brainstorm_active and rs.final in ("deep", "confirm", "clarify"):
            log.info("Brainstorm mode: degrading terminal action %r to 'answer'.", rs.final)
            rs.brainstorm_suppressed_action = rs.brainstorm_suppressed_action or rs.final
            if rs.final == "clarify":
                rs.brainstorm_clarify_question = (_clarify_question_text(rs.plan)
                                               or rs.brainstorm_clarify_question)
            elif rs.final == "confirm" and rs.plan.confirm_question:
                rs.brainstorm_clarify_question = (rs.brainstorm_clarify_question
                                               or rs.plan.confirm_question.strip())
            rs.plan.action = rs.final = "answer"

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
        if rs.brainstorm_active:
            # Which reason held the turn decides the wording: the user's own veto is not a mode to
            # explain to them, it is an instruction that was followed.
            user_vetoed = hold_off_active and cfg.execution_mode != "brainstorm"
            brainstorm_ack_note = (HOLD_OFF_NO_ACTION_ACK_NOTE if user_vetoed
                                   else BRAINSTORM_NO_ACTION_ACK_NOTE)
            planner_tried_to_act = (rs.brainstorm_suppressed_action in ("deep", "confirm", "tool")
                                    or bool(rs.plan.deferred_deep)
                                    or bool(rs.plan.answer_contains_work_to_execute))
            if planner_tried_to_act:
                brainstorm_ack_note += (HOLD_OFF_HELD_WORK_ACK_NOTE if user_vetoed
                                        else BRAINSTORM_HELD_WORK_ACK_NOTE)
            if rs.brainstorm_clarify_question:
                brainstorm_ack_note += (BRAINSTORM_CLARIFY_ACK_PREFIX
                                        + rs.brainstorm_clarify_question.strip() + "\n")

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

        if rs.plan is not None and rs.plan.action == "answer" and getattr(rs.plan, "asks_first", False):
            reply_directive = ASK_FIRST_DIRECTIVE + ("\n\n" + reply_directive if reply_directive else "")
        if (rs.plan is not None and rs.plan.action == "answer" and not getattr(rs.plan, "asks_first", False)
                and is_information_turn(rs.turn_user_intent, rs.plan)
                and getattr(rs.plan, "reply_shape", None) in (None, "default") and rs.turn_delivery != "spoken"):
            reply_directive = INFORMATION_ANSWER_DIRECTIVE + ("\n\n" + reply_directive if reply_directive else "")      # short by default
        if (rs.plan is not None and rs.plan.action == "answer" and not getattr(rs.plan, "asks_first", False)
                and standard_for(rs.plan, user_intent=rs.turn_user_intent, delivery=rs.turn_delivery).shape == SHAPE_SETTLED):
            reply_directive = SETTLED_ANSWER_DIRECTIVE + ("\n\n" + reply_directive if reply_directive else "")      # an order reported: what is now so, then stop
        if (rs.plan is not None and rs.plan.action == "answer" and not getattr(rs.plan, "asks_first", False)
                and standard_for(rs.plan, user_intent=rs.turn_user_intent, delivery=rs.turn_delivery).shape == SHAPE_SPOKEN):
            reply_directive = SPOKEN_DIRECTIVE + ("\n\n" + reply_directive if reply_directive else "")      # read aloud: speakable, three sentences
        if (rs.plan is not None and rs.plan.action == "answer" and not getattr(rs.plan, "asks_first", False)
                and (getattr(rs.plan, "deep_target", None) == "draft_in_chat" or getattr(rs, "writes_text", False)) and rs.turn_user_intent != "ask"
                and rs.cue_resolved is None):
            reply_directive = DRAFT_ANSWER_DIRECTIVE + ("\n\n" + reply_directive if reply_directive else "")      # the piece they asked for, written in full
        if rs.plan is not None and rs.plan.action == "answer" and getattr(rs, "status_snapshot", False):
            # A plan for a coming period has its own directive: the status one asks for two sentences and no list, which is the wrong reply to "plan my week".
            snapshot_directive = PLAN_SNAPSHOT_DIRECTIVE if getattr(rs.plan, "reply_shape", None) == SHAPE_PLAN else STATUS_SNAPSHOT_DIRECTIVE
            reply_directive = snapshot_directive + ("\n\n" + reply_directive if reply_directive else "")      # a status (or a plan) is written from the snapshot code read
        if rs.plan is not None and rs.plan.action == "answer" and getattr(rs, "no_attachment", False):
            reply_directive = NO_ATTACHMENT_DIRECTIVE + ("\n\n" + reply_directive if reply_directive else "")
        if (rs.plan is not None and rs.plan.action == "answer" and rs.plan.deferred_deep and not rs.brainstorm_active
                and not (rs.plan.act_declined or False)):
            reply_directive = HANDOFF_ANSWER_NOW_DIRECTIVE + ("\n\n" + reply_directive if reply_directive else "")

        def _answer_grounding(steering: Optional[str] = None) -> str:
            # The L2 grounding EVERY reply this turn is built on.
            cv = rs.context_view
            # What the system's own record says about asks, and who may read the reply: facts the reply is written from, never
            # something it is left to guess. Only for a thread turn (the asker is known) with the incremental replies switched on.
            if getattr(cfg, "interim_replies", False) and (rs._ctx_meta or {}).get("asker_user_id"):
                meta = rs._ctx_meta
                facts = ask_state_block(getattr(rs, "turn_asks", None), meta.get("participants") or [], str(meta.get("asker_user_id")))
                if shared_thread.is_shared_thread(meta):
                    facts = shared_thread.SHARED_THREAD_RULE + "\n\n" + facts
                cv = (cv + "\n\n" + facts) if cv else facts
            if steering:
                cv = ((rs.context_view + "\n\n" if rs.context_view else "")
                      + "--- IMPROVE YOUR ANSWER (it did not yet meet the goal) ---\n" + steering)
            # Forward the planner's reasoning so the answerer benefits from what the planner
            # already worked out. Without this the answerer re-derives from scratch and may
            # reach a different (wrong) conclusion, e.g. "no prior history" despite the planner
            # having already found and summarised the conversation history correctly.
            if rs.plan.rationale and rs.plan.action == "answer":
                planner_block = (f"--- PLANNER ANALYSIS (what this reply is for; follow it) ---\n{rs.plan.rationale}")
                cv = (cv + "\n\n" + planner_block if cv else planner_block)
            # The facts the planner itself listed from the reads (``facts_read``): the answerer reads
            # every gathered page of a long turn, the planner listed the ones that bear on the request.
            if rs.plan.facts_read and rs.plan.action == "answer":
                facts = facts_block(FACTS_ANSWER_HEAD, rs.plan.facts_read)
                cv = (cv + "\n\n" + facts if cv else facts)
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
        nets_intent = (None if (rs.turn_user_intent == USER_INTENT_HOLD_OFF and not nets_honor_hold_off)
                       else rs.turn_user_intent)
        rs.nets_intent = nets_intent
        if rs.final not in ("answer", "deep", "confirm", "clarify"):
            must_execute = False
            last_plan = rs.plan or PlanDecision(action="answer")
            if not rs.brainstorm_active:
                planner_prepared_deep = bool(last_plan.deferred_deep
                                             or (last_plan.deep_brief or "").strip())
                must_execute = (planner_prepared_deep and nets_intent != "ask") or user_ordered_work(nets_intent)      # a question is answered, never started as a background run
                if (not must_execute and nets_intent is None
                        and (self._has_deep_execution_capability()
                             or self._has_deferred_queue_capability())):
                    must_execute, _why = self.judge_execution_directive(rs.user_message, "")
                    log.info("Read budget spent; intent judgment on the request: %s (%s).",
                             must_execute, _why)
                if must_execute and last_plan.deferred_deep:
                    last_plan.goal = last_plan.goal or last_plan.deferred_deep.get("goal")
                    last_plan.deep_brief = (last_plan.deep_brief
                                            or last_plan.deferred_deep.get("brief"))
            # The same acceptance test as every other path to a deep run (``tool_guards.escalation_route``): text the reply carries, or Quest-record
            # work a tool performs, is not a deep run. The wrap-up then answers from what was read.
            if must_execute and tool_guards.escalation_route(last_plan, nets_intent, self.tools, bool(getattr(rs, "deep_to_tool_runs", 0)),
                                                            tool_guards.mutating_tool_ran(gathered, self.tools)) != tool_guards.ROUTE_DEEP:
                log.info("Read budget spent: a deep run is not accepted for this request; the wrap-up answers.")
                must_execute = False
            # PURSUE BEFORE THE WRAP-UP: the read budget is spent and the last read step returned nothing (a structured count the loop
            # keeps), so the answer would rest on records nobody opened. Before wrapping up with "I could not find it", the read-only
            # search runs through Critical Thinking's bounded replan; what it returns answers the turn, else the wrap-up goes ahead
            # with the one honest line the replan wrote.
            pursuit_line = ""
            if (not must_execute and not turn_awaits_person(rs) and not rs.brainstorm_active and getattr(rs, "empty_read_steps", 0) >= 1 and cfg.deep_before_giving_up
                    and nets_intent != USER_INTENT_HOLD_OFF and message_is_user_turn and self.search_untried(rs, exec_record, _ctx_meta)):
                rs.text = getattr(rs, "text", "") or ""
                rs._last_verdict = {"met": False, "reason": "the reads returned nothing the answer can rest on",
                                    "next_action": "open the records the question concerns (the quest's collections, by their ids) and read their entries"}
                try:
                    pursued = pursue_before_giving_up(
                        self, rs, _truncate_goal(last_plan.goal or f"Fully address the request: {rs.user_message}"), exec_record, _ctx_meta, emit,
                        cancel_check, take_resume_session, working_dir_override)
                    rs.pursuit = pursued["pursuit"]
                    if pursued["result"] is not None and pursued["result"].kind == "cancelled":
                        return finish(pursued["result"])
                    if pursued["pursuit"]["status"] == "met_after_replan" and pursued["result"] is not None:
                        self._kickoff_card_update(pursued["result"], PlanDecision(action="deep", goal=last_plan.goal), rs.user_message, _ctx_meta, emit,
                                                  gathered=gathered)
                        return finish(pursued["result"])
                    if pursued["pursuit"].get("message") and (pursued["pursuit"]["status"] == "continuing" or pursued["pursuit"].get("replans")):
                        pursuit_line = pursued["pursuit"]["message"]
                except Exception:  # noqa: BLE001 -- the net must never break the turn
                    log.warning("read-budget pursuit failed", exc_info=True)
                rs._last_verdict = None
            # A planner_only note (a repeated read) is not something gathered.
            if (gathered_content(gathered) or rs.brainstorm_active) and not must_execute:
                emit.status("Wrapping up with a best-effort answer…")
                rs.model = self._answer_model(rs.plan, hint=rs.model_hint)
                # The wrap-up reply ENDS the turn, and the answerer is not otherwise told so: a
                # 2026-08-24 dissertation run closed with "What I'm doing now: spec all nine
                # gaps..." and was reported done having specced none, because a budget-capped
                # answer reads like any other answer from inside. The honesty note says the turn
                # is over, so what is left is named as left rather than promised.
                wrapup_directive = (reply_directive + "\n\n" + READ_BUDGET_WRAPUP_HONESTY_NOTE
                                    if reply_directive else READ_BUDGET_WRAPUP_HONESTY_NOTE)
                try:
                    wrapup_facts = self.resolved_facts_of(rs, gathered).block()
                except Exception:  # noqa: BLE001
                    wrapup_facts = ""
                if wrapup_facts:
                    wrapup_directive = wrapup_facts + "\n" + wrapup_directive

                def write_wrapup(steer: Optional[str] = None) -> str:
                    return self._grounded_answer(rs.user_message, transcript, _answer_grounding(steer), gathered,
                                                 rs.model, True, native_blocks=rs.native_blocks,
                                                 rep_preamble=rep_preamble,
                                                 reply_directive=wrapup_directive)
                rs.text = self.hold_to_standard(rs, write_wrapup(), gathered, write_wrapup)
                if pursuit_line:
                    rs.text = (rs.text or "").rstrip() + "\n\n" + pursuit_line
                return finish(OrchestratorResult(kind="answer", text=rs.text, rationale=rs.plan.rationale,
                                                 partial=True, model=rs.model,
                                                 exit_reason="read_budget"))
            rs.plan = last_plan
            rs.plan.action = rs.final = "deep"
            rs.plan.goal = _truncate_goal(rs.plan.goal or f"Fully address the request: {rs.user_message}")
            rs.plan.deep_brief = rs.plan.deep_brief or rs.user_message
            if gathered_content(gathered):
                emit.status("This asks for a change, so doing the work instead of wrapping up "
                            "with an answer…")

        # A planner-originated "confirm" is HONORED as a confirm (it surfaces below via
        # _run_confirm). QAR does not re-route or auto-execute a confirm by inspecting keywords in
        # the planner's confirm question — that brittle keyword gate was removed (see the note by
        # clip_head_and_tail). If the planner over-confirms, fix the planner, not this.

        if rs.final == "clarify":
            # User clarification/selection needed: surface as decision-request
            rs.res = self._run_clarify(rs.plan, quest_id=quest_id, emit=emit, ask_inline=bool(_ctx_meta.get("live_user_turn")))
            rs.res.exit_reason = "clarify"
            return finish(rs.res)

        if rs.final == "deep":
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
                goal_text = rs.plan.goal or f"Complete: {rs.user_message[:100]}"
                emit.emit(ProgressEvent(type=EVENT_INTENT, text=f"Executing: {goal_text}"))
                emit.status("Running now…")
            rs.res = self._run_deep(rs.plan, rs.user_message, self._deep_model(rs.plan, hint=rs.model_hint),
                                 emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                                 gathered=gathered, quality_standards=rs.quality_standards,
                                 pending_inputs=rs.pending_inputs, model_hint=rs.model_hint,
                                 ctx_meta=_ctx_meta, cancel_check=cancel_check,
                                 working_dir_override=working_dir_override,
                                 resume_session_id=take_resume_session(),
                                 run_control=rs.run_control, card_context=rs.card_context)
            if rs.res.kind == "cancelled":
                return finish(rs.res)
            rs.res.exit_reason = "deep_met" if (rs.res.deep_results and all(d.met for d in rs.res.deep_results)) else "deep_not_met"
            if rs.overseer_decided == "overseer_escalated_deep":
                rs.res.exit_reason = "overseer_escalated_deep"
            if not attachments:
                self._recipe_learn_deep(rs.user_message, rs.plan, rs.res, _ctx_meta.get("scope_tags"),
                                        learnable=(message_is_user_turn and mode is Mode.LIVE
                                                    and rs.caller_deep_plan is None))
            # Background: categorize edited files into context cards (deep runner returns edited_files in metadata)
            if rs.res.deep_results and any(dr.met for dr in rs.res.deep_results):
                self._update_context_cards_after_deep(rs.res, context_meta)
            # Background (ASYNC, best-effort): prepare reusable context for this user's NEXT similar
            # request by updating their cards from this run. Off the result path; never blocks finish.
            self._kickoff_card_update(rs.res, rs.plan, rs.user_message, _ctx_meta, emit,
                                      gathered=gathered)
            return finish(rs.res)

        if rs.final == "confirm":
            rs.res = self._run_confirm(rs.plan, quest_id=quest_id)
            # A confirm can be planner-originated (rare) or come from an OVERSEER escalate_human
            # signal (Fix 2); distinguish the exit_reason so a caller/consumer can tell the two apart
            # (e.g. for building next-turn's ``prior_escalations``).
            rs.res.exit_reason = ("overseer_escalated_human" if rs.overseer_decided == "overseer_escalated_human"
                               else "confirm")
            return finish(rs.res)

        # answer
        rs.model = self._answer_model(rs.plan, hint=rs.model_hint)
        # Tell the task feed which tier wrote this reply and the model it resolved to.
        reply_tier = tier_for_call("answer.response", rs.model_hint)
        emit.emit(ProgressEvent(
            type=EVENT_STATUS,
            text=f"Replying with the {reply_tier} tier ({rs.model}).",
            data={"response_model": {"tier": reply_tier, "model": rs.model}}))

        def resolved_facts():
            return self.resolved_facts_of(rs, gathered)

        def _gen_answer(steering: Optional[str]) -> str:
            # Produce an answer, optionally STEERED by goal-verification feedback (the prior answer
            # plus why it fell short + what to fix) folded into the grounding context. EVERY reply
            # this turn produces (including each regeneration) goes through here or the read-budget
            # wrap-up above, and both pass ``brainstorm_ack_note`` as the reply directive, so a
            # latched turn cannot ship a reply that was never told the turn was held.
            # A CLARIFYING QUESTION is composed from the person's message alone (the planner's structured ``clarify`` shape): the writer is handed no
            # context, transcript, reads or persona, so it cannot name a topic it never saw; a regeneration gets the same restricted input, one tier up.
            if is_clarify(rs.plan) or rs.records_free:
                # A general question (the request analyser reported ``needs_records`` false) is written the same way: from the question alone.
                return self._grounded_answer(rs.user_message, "", "", [], rs.model if steering is None else self.regeneration_model(rs), False,
                                             native_blocks=None, rep_preamble=None,
                                             reply_directive=CLARIFY_DIRECTIVE if is_clarify(rs.plan) else GENERAL_ANSWER_DIRECTIVE)
            cv = _answer_grounding(steering)
            # A reply that came back for a correction is written one tier up: the cheaper model already missed it once (Joshua's rule: when a
            # cheaper model fails, retry one tier up rather than the same way).
            model = rs.model if steering is None else self.regeneration_model(rs)
            directive = reply_directive
            if rs.plan.action == "answer":
                # Facts the records give at two dates are resolved by date in code; the writer is told only the latest and its date.
                try:
                    block = resolved_facts().block()
                except Exception:  # noqa: BLE001 -- resolving never breaks a reply
                    block = ""
                if block:
                    directive = block + ("\n\n" + directive if directive else "")
            if len(rs.plan.subquestions) >= 2:
                return self._answer_subquestions(rs.user_message, transcript, cv, gathered,
                                                 model, rs.plan.subquestions, native_blocks=rs.native_blocks,
                                                 reply_directive=directive,
                                                 rep_preamble=rep_preamble)
            return self._grounded_answer(rs.user_message, transcript, cv, gathered, model,
                                         False, native_blocks=rs.native_blocks,
                                         rep_preamble=rep_preamble,
                                         reply_directive=directive)
        rs._gen_answer = _gen_answer

        emit.status(f"Answering {len(rs.plan.subquestions)} parts in parallel…"
                    if len(rs.plan.subquestions) >= 2 else "Answering")
        if rs.trace is not None:
            rs.trace.answering()    # liveness only: the wait between the last read and the first words
        try:
            rs.text = _gen_answer(None)
        except BaseException:
            if rs.trace is not None:
                rs.trace.answered()    # a failed or cancelled draft never leaves the line running
            raise
        rs._ti =getattr(self.provider, 'tokens_in', 0)
        rs._to = getattr(self.provider, 'tokens_out', 0)
        if rs._ti or rs._to:
            emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                    data={"tokens_in": rs._ti, "tokens_out": rs._to, "total": rs._ti + rs._to}))

    def _run_check_answer(self, rs: SimpleNamespace) -> Optional[OrchestratorResult]:
        """Check the drafted answer: the overseer's answer checkpoint and the nets that add execution to an answer turn."""
        _ctx_meta = rs._ctx_meta
        _gen_answer = rs._gen_answer
        _prior_escalation_digest_lines = rs._prior_escalation_digest_lines
        _recent_conversation_digest_lines = rs._recent_conversation_digest_lines
        cancel_check = rs.cancel_check
        cfg = rs.cfg
        emit = rs.emit
        exec_record = rs.exec_record
        finish = rs.finish
        gathered = rs.gathered
        nets_intent = rs.nets_intent
        overseer_signals = rs.overseer_signals
        quest_id = rs.quest_id
        rep_preamble = rs.rep_preamble
        started = rs.started
        take_resume_session = rs.take_resume_session
        working_dir_override = rs.working_dir_override

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
        # and docs/explanation/overseer.md for the full tradeoff. A FAST-resolving consult (the common case for a
        # quick model) still corrects the SAME answer before it ships, exactly as before.
        # SITUATION STANDARD (core/reply_standards.py): the standard for this reply's situation (selected from the planner's
        # structured ``reply_shape`` / ``asks_first``) and the deterministic measurements of the draft against it. A standard in
        # force gives the checkpoint real authority below: a longer wait for the verdict and code-built repair rounds.
        standard, measured, standard_evidence, standard_in_force, figure_pool, figure_evidence = None, None, "", False, "", ""
        turn_resolved = None
        if getattr(cfg, "reply_standards", False) and rs.plan is not None and rs.plan.action == "answer":
            try:
                standard_evidence = (self.link_evidence_for(rs, SimpleNamespace(deep_results=[]))
                                     + "\n" + (exec_record.summary() if exec_record is not None else ""))
                standard = standard_for(rs.plan, user_intent=rs.turn_user_intent, delivery=rs.turn_delivery)
                turn_resolved = self.resolved_facts_of(rs, gathered)
                # what the turn READ (the operands a derived figure may use): the person's message, the reads and the execution record
                figure_pool = evidence_text(rs.user_message, gathered) + "\n" + (exec_record.summary() if exec_record is not None else "")
                # What may vouch for a FIGURE is narrower than what may vouch for a link: the person's message, this conversation, the context minus
                # the earlier replies kept as recent-turn cards, and the reads. An earlier model reply (a recent card, or the consumer's whole-history
                # evidence) is not evidence: a wrong total once stated and stored would vouch for itself when stated again.
                figure_evidence = evidence_text(rs.user_message, getattr(rs, "transcript", ""),
                                                (getattr(rs, "context_view", "") or "").replace(getattr(rs, "recent_context_text", "") or "\0", ""),
                                                gathered, figure_pool)
                measured = measure(rs.text, standard, standard_evidence, figure_pool, figure_evidence, list(getattr(rs.plan, "facts_read", None) or []),
                                   resolved=turn_resolved, question=rs.user_message, drafted=deliverable_texts(rs, rs.text))
                standard_in_force = standard.shape != SHAPE_DEFAULT or measured.failed
                log.info("Reply standard %s in force=%s before the verdict: %s", standard.shape, standard_in_force, measured.as_text())
            except Exception:  # noqa: BLE001 -- a standard must never break the turn
                standard, measured, standard_in_force = None, None, False
        repair_rounds = 0
        # An information turn is read-only and its draft is held to the situation standard (figures, dates, length, voice) by code: no overseer consult is spent on it.
        information_turn = bool(getattr(cfg, "cheap_information_turns", True) and is_information_turn(rs.turn_user_intent, rs.plan)
                                and not (getattr(rs, "exec_record", None) is not None and rs.exec_record.any_mutation_attempted))
        if cfg.overseer and rs.overseer_submitted < cfg.overseer_max_signals and not rs.records_free and not information_turn and not getattr(rs, "cue_step_attempted", False) and not getattr(rs, "draft_in_chat", False):      # nothing of the person's to check a general answer against
            try:
                if rs._oversee_executor is None:
                    rs._oversee_executor = ThreadPoolExecutor(max_workers=1)
                # Fix 3/4: the raw request (user_message) AND the resolved request (goal_condition)
                # + completion bar + this-conversation history.
                _bpending = self._submit_oversee(
                    rs._oversee_executor, user_message=rs.user_message, goal_condition=rs.goal_condition,
                    step=rs.steps, plan=rs.plan, gathered=gathered, started=started, draft_answer=rs.text,
                    quality_standards=rs.quality_standards,
                    recent_conversation=_recent_conversation_digest_lines,
                    prior_escalations=_prior_escalation_digest_lines,
                    situation_standard=(standard.text if standard_in_force else None),
                    draft_measurements=(measured.as_text() if standard_in_force else None),
                    draft_chars=(700 if standard_in_force else 200),
                    gate=False)
                _bsig = OverseerSignal("proceed")
                if _bpending is not None:
                    rs.overseer_submitted += 1
                    _collected = self._collect_oversee(
                        _bpending, signals=overseer_signals, emit=emit,
                        timeout=(max(cfg.reply_standard_wait_seconds, cfg.overseer_answer_checkpoint_timeout_seconds)
                                 if standard_in_force else cfg.overseer_answer_checkpoint_timeout_seconds))
                    if _collected is not None:
                        _bsig = _collected
                    else:
                        # Not resolved yet: ship the draft now; a late resolution is handled async.
                        self._finish_oversee_in_background(
                            _bpending, emit=emit, quest_id=quest_id,
                            brainstorm_active=rs.brainstorm_active)
                if rs.brainstorm_active and _bsig.signal in ("escalate_deep", "escalate_human"):
                    # Brainstorm mode: overseer escalations may not ADD actions; ship the draft.
                    pass
                elif (_bsig.signal == "escalate_deep" and tool_guards.escalation_route(
                        rs.plan, rs.turn_user_intent, self.tools, bool(getattr(rs, "deep_to_tool_runs", 0)),
                        tool_guards.mutating_tool_ran(gathered, self.tools)) != tool_guards.ROUTE_DEEP):
                    # Accepted only for files, code, long research or many steps (``tool_guards.escalation_route``): a reply-sized or
                    # tool-sized order keeps its draft, which is the reply, rather than becoming a deep run the person is only told about.
                    log.info("Overseer escalation to deep not accepted at the answer; the draft ships.")
                elif _bsig.signal == "escalate_deep" and self._has_deep_execution_capability():
                    if emit is not None:
                        emit.status("Overseer: this needs real execution, running it now…")
                    _ov_plan = PlanDecision(
                        action="deep",
                        goal=_truncate_goal(rs.plan.goal or f"Carry out the user's request: {rs.user_message}"),
                        deep_brief=(rs.user_message)[:2000],
                        rationale="overseer escalated the answer to deep execution",
                        deep_difficulty=getattr(rs.plan, "deep_difficulty", None),
                        deep_difficulty_reason=getattr(rs.plan, "deep_difficulty_reason", None),
                    )
                    _ov_model = self._deep_model(_ov_plan, hint=rs.model_hint)
                    ov_facts_before = len(getattr(exec_record, "facts", None) or [])
                    _ov_res = self._run_deep(
                        _ov_plan, rs.user_message, _ov_model,
                        emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                        gathered=gathered, quality_standards=rs.quality_standards,
                        pending_inputs=rs.pending_inputs, model_hint=rs.model_hint,
                        ctx_meta=_ctx_meta, cancel_check=cancel_check,
                        working_dir_override=working_dir_override,
                        resume_session_id=take_resume_session(), self_initiated=True,
                        user_ordered=user_ordered_work(rs.turn_user_intent),
                        run_control=rs.run_control, card_context=rs.card_context)
                    if _ov_res.kind == "cancelled":
                        return finish(_ov_res)
                    if not own_escalation_adds_nothing(_ov_res, exec_record, ov_facts_before):
                        _ov_res.exit_reason = "overseer_escalated_deep"
                        self._kickoff_card_update(_ov_res, _ov_plan, rs.user_message, _ctx_meta,
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
                                          or f"This needs your input before I continue: {rs.user_message}"),
                        rationale="overseer escalated the answer to a human decision",
                    )
                    _confirm_res = self._run_confirm(_confirm_plan, quest_id=quest_id)
                    _confirm_res.exit_reason = "overseer_escalated_human"
                    return finish(_confirm_res)
                if _bsig.signal == "redirect" and _bsig.hint:
                    if emit is not None:
                        emit.status("Overseer: refining the answer…")
                    hint = _bsig.hint
                    if standard_in_force and measured is not None and measured.failed:
                        hint = f"{hint} Also: {measured.hint()}"
                    rs.text = _gen_answer(steering(hint, rs.text))
                    repair_rounds += 1
            except Exception:  # noqa: BLE001 — the overseer must never break the turn
                pass

        # FOUND_CONSTRAINT standard (core/reply_standards.py): one structured verdict on whether the person's records conflict with
        # the request and the draft fails to say so first. At most one regeneration, steered by the verdict's own fields.
        rs.found_constraint = False
        log.info("Reply standard found_constraint: judge gate records=%s sources=%s", turn_holds_records(getattr(rs, "_sources", None), gathered),
                 [(x.get("adapter"), len(x.get("items") or [])) for x in (getattr(rs, "_sources", None) or []) if isinstance(x, dict)])
        if (getattr(cfg, "reply_standards", False) and rs.plan is not None and rs.plan.action == "answer"
                and turn_holds_records(getattr(rs, "_sources", None), gathered)):
            try:
                verdict = self.found_constraint_verdict(
                    rs.user_message, (getattr(rs, "context_view", "") or "")[:3500] + "\n" + (standard_evidence or ""), rs.text)
                log.info("Reply standard found_constraint verdict: %s", verdict)
                if verdict and verdict.get("conflict"):
                    # The records say the request cannot happen as asked: nothing may be queued for it below, whatever the
                    # planner's own act_declined flag said (it forgets), and the reply must not leave it open.
                    rs.found_constraint = True
                    try:
                        rs._ctx_meta["found_constraint_head"] = found_constraint_head(
                            str(verdict.get("fact") or ""), str(verdict.get("consequence") or ""))
                    except Exception:  # noqa: BLE001
                        pass
                if constraint_violated(verdict):
                    if emit is not None:
                        emit.status("Reply standard: stating the conflict first…")
                    rs.text = _gen_answer(steering(found_constraint_hint(str(verdict.get("fact") or ""),
                                                                         str(verdict.get("consequence") or "")), rs.text))
                    repair_rounds += 1
                    log.info("Reply standard found_constraint: conflict not stated first or request left open; regenerated once")
            except Exception:  # noqa: BLE001 -- a standard must never break the turn
                pass

        # Code-built repair rounds: a measurement the draft still fails is fixed by regenerating with a hint built from the
        # measurement itself (it names the exact violation and repair), at most ``reply_standard_max_repairs`` times in all, each
        # re-measured. What still fails after the last round gets the cheapest safe deterministic repair. Never unbounded.
        # The context the reply was written from, at debug level on its own logger: an evaluation replay records it as part of what the turn was shown.
        logging.getLogger("quest-ai-runner.grounding").debug("Reply grounding: %s", str(getattr(rs, "context_view", "") or "")[:20000])
        if standard is not None and standard_in_force:
            try:
                measured = measure(rs.text, standard, standard_evidence, figure_pool, figure_evidence, list(getattr(rs.plan, "facts_read", None) or []),
                                   resolved=turn_resolved, question=rs.user_message, drafted=deliverable_texts(rs, rs.text))
                measurement_rounds = 0     # the overseer's and the constraint's regenerations do not use up the rounds the measurements are owed
                while measured.failed and measurement_rounds < repair_limit(cfg, standard):
                    if emit is not None:
                        emit.status("Reply standard: refining the answer…")
                    # An address the draft holds is an item the person may have asked for: it outranks a length or list cap.
                    # The hint names the addresses that must survive, and a repair that loses one is rejected (the
                    # address sets are compared, no prose is read); the longer draft ships.
                    held = pointed_items(rs.text)
                    candidate = _gen_answer(steering(measured.hint() + keep_addresses_hint(rs.text, held), rs.text))
                    repair_rounds += 1
                    measurement_rounds += 1
                    if lost_pointers(candidate, held):
                        log.info("Reply standard %s: repair dropped %d address(es) the draft held; kept the longer draft",
                                 standard.shape, len(lost_pointers(candidate, held)))
                        break
                    rs.text = candidate
                    measured = measure(rs.text, standard, standard_evidence, figure_pool, figure_evidence, list(getattr(rs.plan, "facts_read", None) or []),
                                   resolved=turn_resolved, question=rs.user_message, drafted=deliverable_texts(rs, rs.text))
                if measured.failed:
                    log.info("Reply standard repair: still failing after %d round(s): %s", measurement_rounds,
                             [(v.kind, list(v.values)[:6]) for v in measured.violations])
                    rs.text = repair(rs.text, standard, measured, standard_evidence, figure_pool, figure_evidence,
                                     question=rs.user_message, facts=list(getattr(rs.plan, "facts_read", None) or []), drafted=deliverable_texts(rs, rs.text))
                log.info("Reply standard %s: %d repair round(s), %s", standard.shape, repair_rounds, measured.as_text())
                rs.reply_standard = {"shape": standard.shape, "repair_rounds": repair_rounds,
                                     "still_failing": bool(measured.failed), "measured": measured.as_text()}
            except Exception:  # noqa: BLE001 -- a standard must never break the turn
                pass

        # A reply may not deny a change this turn's own tool calls made. The account is code-written from the execution facts, and
        # the judge (one structured contradiction verdict, no keywords) swaps in the account when the reply denies it. Set before
        # the nets below, which would otherwise read a denial ("I can't add it") as work still to execute.
        rs.landed_account = tool_landed_account(exec_record)
        rs.landed_receipt = False
        rs.keep_landed = lambda text: self.keep_landed_work_reported(rs, text)    # the final rewrites (the figure pass) hold their output to the same account
        rs.text = self.keep_landed_work_reported(rs, rs.text)

        # If deferred_deep is set, also run the deep task now, synchronously, right after the
        # answer text is produced (same turn; nothing is saved for later)
        # OR if planner explicitly flagged answer_contains_work_to_execute
        # OR auto-detect false claims (fallback for broken prompts)
        # BRAINSTORM MODE: this entire block is an escalation net (it can only ADD execution to
        # an answer turn), so while the latch is held it is skipped wholesale: no deferred deep,
        # no work-to-execute flag, no described-work net, no message-intent fallback (planner
        # verdict OR LLM judgment). Describing possible work IS the product in brainstorm.
        # A reply that declines the act on the records (``act_declined``) queues nothing: declining and queuing contradict.
        if getattr(rs, "no_attachment", False) and rs.plan.action == "answer" and not rs.text.startswith(NO_ATTACHMENT_LINE):
            rs.text = f"{NO_ATTACHMENT_LINE} {rs.text}".strip()     # code-written: the fact is the turn's own (no attachment object), not the model's wording
        rs.act_declined = bool(rs.plan.act_declined or rs.found_constraint)
        rs.should_defer_deep = (None if (rs.brainstorm_active or rs.act_declined) else rs.plan.deferred_deep)
        if rs.should_defer_deep and tool_guards.escalation_route(
                rs.plan, rs.turn_user_intent, self.tools, bool(getattr(rs, "deep_to_tool_runs", 0)),
                tool_guards.mutating_tool_ran(gathered, self.tools)) != tool_guards.ROUTE_DEEP:
            log.info("The planner's deferred deep run is not accepted (tool-sized or reply-sized work); the answer stands.")
            rs.should_defer_deep = None
        # Capability for these nets = inline execution OR a wired deferred queue: everything they
        # can set flows through the deferred block below, which reaches the queue runner by explicit
        # override, so a queue-only consumer (no default runner, no classifier) is capable here.
        # Every net below can only ADD execution to an answer turn, so the human telling us to hold
        # off ("don't create a task", "just answer here", "I haven't given you an instruction yet",
        # "kill those runs") turns the whole block off. That is the planner's structured
        # ``user_intent == "hold_off"`` verdict on a typed message (``nets_intent``), never a
        # keyword reading: those messages carry change verbs, and the keyword net that preceded
        # this escalated them most reliably of all.
        # An answer that DECLINES the act on the records it read (``act_declined``: a stored
        # constraint or conflict, with the nearest option that works) is the complete response, not
        # a proposal to execute, so none of the nets below may turn it into a deep run.
        deep_accepted = tool_guards.escalation_route(
            rs.plan, nets_intent, self.tools, bool(getattr(rs, "deep_to_tool_runs", 0)),
            tool_guards.mutating_tool_ran(gathered, self.tools)) == tool_guards.ROUTE_DEEP
        if (not rs.should_defer_deep and not rs.brainstorm_active
                and nets_intent != USER_INTENT_HOLD_OFF
                and not rs.act_declined and deep_accepted
                and (self._has_deep_execution_capability()
                     or self._has_deferred_queue_capability())):
            # Primary: trust planner's explicit flag
            if rs.plan.answer_contains_work_to_execute:
                rs.should_defer_deep = {"goal": f"Execute what the answer describes: {rs.user_message}",
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
            elif _answer_describes_unexecuted_work(rs.text) and user_ordered_work(nets_intent):
                rs.should_defer_deep = {"goal": f"Execute the work the answer describes: {rs.user_message}",
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
                _is_directive = rs.brainstorm_released_this_turn or user_ordered_work(nets_intent)
                _directive_reason = ("brainstorm release: the user lifted the hold and told us to act"
                                     if rs.brainstorm_released_this_turn
                                     else "message-intent fallback (planner user_intent)")
                if not _is_directive and nets_intent is None:
                    _is_directive, _llm_reason = self.judge_execution_directive(rs.user_message, rs.text)
                    _directive_reason = f"message-intent fallback (LLM judgment: {_llm_reason})"
                if _is_directive:
                    log.info("Escalating answer->deep: user message requests a change but the turn "
                             "only produced a proposal; running it now (%s).", _directive_reason)
                    _proposal = (rs.text or "").strip()
                    _brief = rs.user_message if not _proposal else (
                        f"{rs.user_message}\n\nThe assistant proposed this approach. APPLY it (make the "
                        f"actual code/file/data changes, do not just describe them):\n{_proposal}"
                    )
                    rs.should_defer_deep = {
                        "goal": f"Carry out the user's request: {rs.user_message}",
                        "brief": _brief,
                        "rationale": "user message requests a change but turn only proposed it (message-intent fallback)",
                    }
                    if emit is not None:
                        emit.status("You asked for a change, making it now…")

        # True once a deferred deep run has produced substantive output that we folded back into the
        # final answer. Gates the post-deep goal-verification loop below so a deferred-deep turn is
        # still held to the overall goal (grounded in what the deep run actually produced).
        rs._deferred_deep_grounded = False
        # The landed deferred run's own code-written account; see ``keep_landed_work_reported``.
        # (A turn whose tool calls changed something starts with that account; a deferred run replaces it with its own.)
        rs.landed_account = tool_landed_account(exec_record)
        # True once a QUEUED deployment's deferred hand-off is CONFIRMED (the deep runner enqueued
        # the work and returned a deferred met result). Gates the goal-verification loop below off
        # for this turn: the deferred contract trusts met and never re-verifies a hand-off sentinel
        # against the user's goal (that would always fail and could relaunch, double-enqueueing).
        rs._deferred_handoff_confirmed = False
        # True once this turn's deferred deep work resolved to a PARKED approval decision
        # (``DeepResult.decision_id``) with nothing landed. Gates the goal-verification loop off
        # for this turn, same reasoning as ``_deferred_handoff_confirmed``: the goal is
        # intentionally not met yet (a human still has to answer the card), so regenerating
        # toward "met" would only invite a rewrite that claims the parked change already happened.
        rs._decision_parked = False
        # True once this turn's deferred deep work neither landed nor parked (every run failed or
        # could not be verified), so the reply is the runs' own honest text. Gates the
        # goal-verification loop off, same reasoning as ``_decision_parked``.
        rs.deferred_unconfirmed = False
        # The parked result's own decision_id, carried onto the final OrchestratorResult below so
        # a consumer can tell structurally (never by scanning the reply text) that this turn ended
        # on a pending approval card, not a completed answer.
        rs._parked_decision_id: Optional[str] = None
        # The queue pin is for the PLANNER'S OWN hand-off (``deferred_deep``): it decided the work
        # belongs in the background. The escalation nets above only infer that the user asked for
        # a change the turn did not make ("you asked for a change, making it now"), so their work
        # is routed like any deep action (the classifier picks inline or queue) instead of being
        # forced into the background queue. Found 2026-10-07 in Quest's chat: "work out how far
        # over budget I am ... then add a goal" ended in a background task for a goal the chat can
        # add inline in a second.
        _queued_mode = bool(self.cfg.deferred_deep_queued)
        rs._queued_mode = _queued_mode
        # A queue-only wiring (no inline runner) still pins: the queue is the only place the
        # inferred work can go (settled: test_queue_only_wiring_can_still_hand_off).
        # A hand-off the planner itself labelled a quick change to the person's OWN Quest records
        # (``deep_target`` quest_data, difficulty simple or unrated) is not background work: it is routed
        # like any deep action, so the classifier runs it inline in a second instead of queueing it
        # (REL-MUL-07: "log Dana ... and tell me how many asked for lavender" ended as a queued task).
        quick_records_change = (getattr(rs.plan, "deep_target", None) == "quest_data"
                                and getattr(rs.plan, "deep_difficulty", None) in (None, "simple"))
        pin_queue = _queued_mode and ((bool(rs.plan.deferred_deep) and not quick_records_change)
                                      or not self._has_deep_execution_capability())
        rs.pin_queue = pin_queue
