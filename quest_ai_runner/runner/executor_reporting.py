"""Reporting a run's outcome: progress, done, incomplete, failed, receipts.

A mixin of ``TaskExecutor`` (see ``executor.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.orchestrator import OrchestratorResult, _strip_future_context
from hubofhubs.ports.deep import ProgressEvent
from .context_updates import parse_manifest, parse_usage_notes, render_receipt, strip_usage_block
from .executor_support import (
    ExecutionOutcome,
    _model_used_note,
    client_accepts_named_param,
    client_accepts_session_id,
    metric_facts,
    metric_report_data,
    terminal_session_id,
)
from .executor_contracts import log


class ReportingMixin:
    """Reporting a run's outcome: progress, done, incomplete, failed, receipts."""

    def report(self, task_id: str, result: OrchestratorResult,
               conv_id: Optional[str] = None, *,
               request_text: Optional[str] = None,
               rep_preamble: Optional[str] = None,
               card_id: Optional[str] = None,
               autopilot_composed: bool = False) -> ExecutionOutcome:
        """Public: map an ALREADY-PRODUCED OrchestratorResult onto the Quest callback + chat.

        ``execute()`` runs the brain and then reports; but an integrator whose deep run executes
        ASYNCHRONOUSLY *outside* ``execute()`` (e.g. a host application spawns a ``/goal`` subprocess
        and only learns the outcome later, on a different thread) needs to report that finished
        outcome through the SAME three-way policy — done / needs_you / failed — and the same
        post-back-into-chat behaviour. They build the OrchestratorResult from the finished run and
        call ``report(...)`` so async and in-loop runs report IDENTICALLY. Thin, deliberate
        wrapper over the internal ``_report`` so the policy lives in exactly one place.

        ``request_text`` (optional) is the task's original instruction text; when given, a fully
        met deep result's done message is folded through the report synthesis + claim check (see
        ``_compose_done_report``). ``rep_preamble`` rides into that synthesis. ``card_id``
        (optional, reserved) is stamped on the conversation progress posts. ``autopilot_composed``
        (default False) tells the context-updates receipt whether ``request_text`` is trustworthy
        as the record of what was offered -- true only when the caller knows this task's text was
        actually built by the autopilot pass (its ``task_kind``), never inferred from the text
        itself. A caller with no such task to check defaults to False, the safe choice: the receipt
        then falls back to this executor's own context bundle, when one is wired, rather than
        risking a false receipt from text that merely quotes a prior brief."""
        # When the run's work ended and reporting began: a leftover message sent before this moment
        # arrived WHILE the run was working, one sent after it arrived after the run finished.
        report_began = datetime.now(timezone.utc)
        outcome = self._report(task_id, result, conv_id, request_text=request_text,
                               rep_preamble=rep_preamble, card_id=card_id,
                               autopilot_composed=autopilot_composed)
        # Only after the terminal report: a steer that landed after the run's last drain point
        # has to be claimed now, or it sits on a finished task forever. See the helper.
        if outcome.status != "cancelled":
            self._start_run_for_leftover_messages(task_id, run_ended_at=report_began)
        return outcome

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
                   task_id: Optional[str] = None,
                   card_id: Optional[str] = None) -> None:
        """Best-effort: append a live progress message into the originating chat, if one is linked.

        ``task_id``, when given, is stamped on the posted message so the frontend can correlate it
        back to the task's own lifecycle. ``card_id`` (reserved, no behavior yet) rides on the
        progress-post body when the task carries one, for future per-idea threading; it is only
        forwarded when set, so clients without the parameter are untouched. Never raises and never
        affects the task's success/failure: if the conversation post fails (network, conversation
        gone), the task still reports its result normally via PATCH.

        ORDERING RULE: post the CLOSING message here BEFORE reporting the terminal status. A
        consumer backend may run its own guaranteed-delivery net on the terminal-status update
        (post the task's result into the chat if the runner never closed the loop itself), and
        that net can only look at what is already stored. Reporting the status first therefore
        made its check run too early every single time, so it posted the result and then this
        call posted the identical text again: the same answer twice in one chat. Ordering is the
        half of the fix that lives on this side; a consumer that needs to be safe against an older
        runner has to make its own append idempotent per (task, kind) as well."""
        if not conv_id or not content:
            return
        post = getattr(self._client, "post_conversation_message", None)
        if callable(post):
            extra = {"card_id": card_id} if card_id else {}
            self._safe(lambda: post(conv_id, content, kind=kind, task_id=task_id, **extra))

    # --- result -> Quest callback -------------------------------------------

    def _report(self, task_id: str, result: OrchestratorResult,
                conv_id: Optional[str] = None, *,
                request_text: Optional[str] = None,
                rep_preamble: Optional[str] = None,
                card_id: Optional[str] = None,
                autopilot_composed: bool = False) -> ExecutionOutcome:
        # Cooperative cancellation: ``result.kind == "cancelled"`` is the orchestrator's OWN
        # cooperative signal (its ``cancel_check`` returned True mid-run); the extra
        # ``_is_task_cancelled`` re-check covers the race where the run finished (or an async
        # caller reports through the public ``report()`` API) right as/after a human cancelled the
        # task, before we PATCH a terminal status that would just 409 anyway.
        if result.kind == "cancelled" or self._is_task_cancelled(task_id):
            return self._quiet_cancelled(task_id, result)
        # THE THREAD'S SESSION, reported with whatever terminal status this run lands on (done,
        # needs_you or failed). Reporting it on a failure matters as much as on a success: a run
        # that fell short is exactly the one a person replies to, and that reply should resume the
        # session holding the half-finished work rather than start cold. None when this run opened
        # no session at all, in which case nothing about a session is sent (see
        # ``terminal_session_id`` and ``QuestClient._with_session``).
        session_id = terminal_session_id(result)
        # THE METRIC FACTS, reported with whatever terminal status this run lands on. The record
        # is the goal loop's own (``OrchestratorResult.metric_results``, built by code from the
        # run's metric ledger), so what a person reads on the task and in the chat is not
        # something a model wrote; they ride in ``result_data`` next to the outcome stamp. Empty
        # for a run with neither a metric nor a gap, and then nothing below changes: the report
        # is sent exactly as it was before this existed.
        metric_data = metric_report_data(result)
        with_metrics = {"result_data": metric_data} if metric_data else {}
        done_metrics = {"metric_data": metric_data} if metric_data else {}
        if result.kind == "answer":
            text = result.text or "(no answer produced)"
            # BROKEN-PROMISE GUARD: if the orchestrator rewrote this answer to be honest about a
            # claimed action that did NOT actually complete (claim_corrected), the task is NOT done
            # — surface it as needs_you so a human picks it up, rather than marking it complete on a
            # reply that says the work was not finished. (A plain ``partial`` best-effort answer,
            # from the read-budget cap, is still a legitimate informational answer and stays done.)
            if getattr(result, "claim_corrected", False):
                self._report_progress(task_id, "done", text="Paused. Needs you.", output=text)
                # CHAT FIRST, then the terminal status: see _post_conv's note on ordering.
                self._post_conv(conv_id, text, kind="needs_you", task_id=task_id, card_id=card_id)
                self.dispatch_report("report_needs_you", task_id, text, "",
                                     session_id=session_id, **with_metrics)
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
            done_text = self._with_context_receipt(done_text, request_text,
                                                   autopilot_composed=autopilot_composed)
            if exit_reason == "max_turns" and goal_verdict:
                # The run WORKED but its goal was never verified as met: "incomplete", not "done"
                # (which would overstate it) and not "failed" (which reads as a technical error).
                self._report_progress(task_id, "done", text="Ran, but the goal is not fully met.",
                                      output=done_text)
                self._post_conv(conv_id, done_text, kind="incomplete", task_id=task_id,
                                card_id=card_id)
                self.dispatch_incomplete(task_id, done_text, session_id=session_id, **with_metrics)
                return ExecutionOutcome(task_id, "incomplete", done_text)
            self._report_progress(task_id, "done", text="Done.", output=done_text)
            # CHAT FIRST, then the terminal status: see _post_conv's note on ordering.
            self._post_conv(conv_id, done_text, kind="done", task_id=task_id, card_id=card_id)
            record = getattr(result, "execution_record", None)
            worked = bool(record is not None and record.any_success)
            self.dispatch_done(task_id, done_text, "done" if worked else "nothing_done",
                               session_id=session_id, **done_metrics)
            return ExecutionOutcome(task_id, "done", done_text)

        if result.kind == "confirm":
            summary = result.question or "A human decision is required before proceeding."
            # needs_you is a terminal-but-paused state; close the live stream with a 'done' tick
            # noting it now needs a human, so the stream doesn't hang open.
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
            self._post_conv(conv_id, summary, kind="decision", task_id=task_id, card_id=card_id)
            if result.decision_id:
                self.dispatch_report("report_needs_you", task_id, summary, result.decision_id,
                                     session_id=session_id, **with_metrics)
                return ExecutionOutcome(task_id, "needs_you", summary, result.decision_id)
            # No decision id (no escalation sink wired) — surface as needs_you without an id.
            self.dispatch_report("report_needs_you", task_id, summary, "",
                                 session_id=session_id, **with_metrics)
            return ExecutionOutcome(task_id, "needs_you", summary)

        # deep
        deep = result.deep_results
        if deep and all(d.met for d in deep):
            summary = "\n\n".join(d.output for d in deep if d.output) or "Goal(s) met."
            # FOLD-BACK: the raw deep output is a worker transcript tail; the done post into the
            # chat should read as the AI reporting its own finished work. The synthesized report
            # is CLAIM-CHECKED against the run's execution record before it replaces the raw
            # summary; any doubt keeps the raw (already goal-verified) output.
            done_report = self._compose_done_report(request_text, summary, result, rep_preamble)
            # Read the usage lines from the RAW summary, not the fold-back: the receipt is the
            # run's own account of what it did with the person's material, and the rewrite that
            # makes a transcript tail read as a report is free to drop it.
            done_report = self._with_context_receipt(done_report, request_text, run_output=summary,
                                                      autopilot_composed=autopilot_composed)
            done_report += _model_used_note(deep)
            _unconfirmed = [d.unconfirmed_note for d in deep if getattr(d, "unconfirmed_note", "")]
            if _unconfirmed:
                done_report += ("\n\nNot independently confirmed: " + "; ".join(_unconfirmed))
            self._report_progress(task_id, "done", text="Done.", output=done_report)
            # CHAT FIRST, then the terminal status: see _post_conv's note on ordering.
            self._post_conv(conv_id, done_report, kind="done", task_id=task_id,
                            card_id=card_id)
            self.dispatch_done(task_id, done_report, "done", session_id=session_id, **done_metrics)
            return ExecutionOutcome(task_id, "done", done_report)
        # A deep run that raised a human decision instead of finishing.
        decision_id = next((d.decision_id for d in deep if d.decision_id), None)
        if decision_id:
            summary = "A human decision is required to finish this task."
            # A confirm-before-act run carries the prepared output (e.g. the code awaiting review).
            chat_text = next((d.output for d in deep if d.output), None) or summary
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
            # CHAT FIRST, then the terminal status: see _post_conv's note on ordering.
            self._post_conv(conv_id, chat_text, kind="decision", task_id=task_id,
                            card_id=card_id)
            self.dispatch_report("report_needs_you", task_id, summary, decision_id,
                                 session_id=session_id, **with_metrics)
            return ExecutionOutcome(task_id, "needs_you", summary, decision_id)
        # The verifier found the goal unmet for a reason another attempt cannot fix: only a person
        # can supply the missing input. Ask that one question (task paused at needs_you; the reply
        # resumes the thread) instead of burning attempts and then reporting a bare shortfall.
        asks = [d.needs_person for d in deep if getattr(d, "needs_person", "")]
        if asks:
            work = "\n\n".join(_strip_future_context(d.output).strip()
                                for d in deep if (d.output or "").strip()).strip()
            question = " ".join(asks)
            summary = f"I need one thing from you to finish this: {question}"
            chat_text = (f"{work}\n\n{summary}" if work else summary)
            self._report_progress(task_id, "done", text=f"Paused, needs you: {question}")
            self._post_conv(conv_id, chat_text, kind="decision", task_id=task_id, card_id=card_id)
            self.dispatch_report("report_needs_you", task_id, summary, "", session_id=session_id,
                                 **with_metrics)
            return ExecutionOutcome(task_id, "needs_you", summary)
        # A tool or service the work needs kept failing and the goal loop stopped retrying it (see
        # ``deep_tool_failure_attempts``). Say which one and what it answered, plainly, so a person
        # fixes the cause; the task stays non-done ("incomplete") and is not billed further.
        broken = [d.tool_failure for d in deep if getattr(d, "tool_failure", "") and not d.met]
        if broken:
            work = "\n\n".join(_strip_future_context(d.output).strip()
                                for d in deep if (d.output or "").strip()).strip()
            msg = ("I stopped because something this task needs is failing, and trying again would "
                   "not change that: " + " ".join(broken) + " Nothing further was run. Once that is "
                   "fixed, ask me to run this again.")
            if work:
                msg = work + "\n\n" + msg
            self._report_progress(task_id, "done", text="Stopped: a tool this task needs is failing.",
                                  output=msg)
            self._post_conv(conv_id, msg, kind="incomplete", task_id=task_id, card_id=card_id)
            self.dispatch_incomplete(task_id, msg, session_id=session_id, **with_metrics)
            return ExecutionOutcome(task_id, "incomplete", msg)
        # The goal loop saw two attempts in a row end at the same point with nothing new done and
        # stopped (``DeepResult.stalled``): say so, plainly, rather than a bare failure.
        stalled = [d.stalled for d in deep if getattr(d, "stalled", "") and not d.met]
        if stalled:
            work = "\n\n".join(_strip_future_context(d.output).strip()
                                for d in deep if (d.output or "").strip()).strip()
            msg = ("I stopped because two attempts in a row ended at the same point with nothing new "
                   "done, so trying again would not change it: " + " ".join(stalled) + " Nothing "
                   "further was run.")
            if work:
                msg = work + "\n\n" + msg
            self._report_progress(task_id, "done", text="Stopped: no further progress was possible.",
                                  output=msg)
            self._post_conv(conv_id, msg, kind="incomplete", task_id=task_id, card_id=card_id)
            self.dispatch_incomplete(task_id, msg, session_id=session_id, **with_metrics)
            return ExecutionOutcome(task_id, "incomplete", msg)
        # UNVERIFIED is never reported as done, and never as a bare failure either: the work RAN
        # but its verification could not (LLM outage, no verify tier, parse failure), so the
        # outcome is genuinely unknown. The chat message must say that plainly, presenting any
        # work output as unconfirmed, and the task stays non-done ("incomplete": it ran, its
        # outcome is unknown) so a human checks it. It is not "failed": nothing broke.
        unverified = [d for d in deep if not d.met and (d.error or "").startswith("Unverified")]
        if unverified:
            reasons = "; ".join((d.error or "").strip() for d in unverified)
            work = "\n\n".join(d.output for d in deep if d.output).strip()
            disclosure = ("Important: this work ran but I could NOT verify the result as "
                          f"complete ({reasons}). Treat it as unconfirmed until someone checks "
                          "it. It is not marked done.")
            msg = (work + "\n\n" + disclosure) if work else disclosure
            self._report_progress(task_id, "done", text="Ran, but the result could not be verified.",
                                  output=msg)
            # CHAT FIRST, then the terminal status: see _post_conv's note on ordering.
            self._post_conv(conv_id, msg, kind="incomplete", task_id=task_id, card_id=card_id)
            self.dispatch_incomplete(task_id, msg, session_id=session_id, **with_metrics)
            return ExecutionOutcome(task_id, "incomplete", msg)
        # Otherwise the run hit a limit / errored.
        errs = "; ".join(d.error for d in deep if d.error) or "the goal was not met"
        if not deep:                 # deep requested but no runner wired -> needs human/runner
            errs = "deep work required but no deep-runner is configured: " + "; ".join(result.goals)
        # A run that fell short almost never did NOTHING: it read, edited, tested and left real work
        # on disk before the turn budget or the verifier stopped it. Reporting the bare error threw
        # that away, so the person saw "the task failed" with no way to find what had been done and
        # no way to pick it up. The work account travels WITH the failure, under a heading that says
        # plainly it is unfinished.
        work = "\n\n".join(_strip_future_context(d.output).strip()
                            for d in deep if (d.output or "").strip()).strip()
        failed_text = errs
        # A verdict-driven stop says what is LEFT, not only that the goal was not met, and that a
        # reply continues the same session. The heading below is matched by string elsewhere
        # (quest-backend's FAILED_WORK_MARKER), so it must not change.
        left = [d for d in deep if getattr(d, "verdict_reason", "") and not d.met]
        if left and work:
            lead = ["I got part of the way. What is still open: "
                    + " ".join(d.verdict_reason.strip() for d in left)]
            nxt = " ".join(d.verdict_next_action.strip() for d in left if d.verdict_next_action)
            if nxt:
                lead.append("Next step: " + nxt)
            lead.append("Reply here and I will pick this up in the same session.")
            errs = "\n".join(lead)
            failed_text = errs
        if work:
            failed_text = (f"{errs}\n\n--- WHAT THE RUN DID BEFORE IT STOPPED (unfinished, not "
                           f"verified) ---\n{work}")
        if work:
            # The run did real work and then stopped short of its goal (verifier said not met, turn
            # or token budget spent): it RAN, so this is "incomplete", with the work attached.
            # "failed" is reserved for a run that produced nothing because it broke or never ran.
            self._report_progress(task_id, "done", text="Ran, but the goal is not fully met.",
                                  output=failed_text)
            self._post_conv(conv_id, failed_text if left else
                            f"I got part of the way but did not fully reach the goal: {failed_text}",
                            kind="incomplete", task_id=task_id,
                            card_id=card_id)
            self.dispatch_incomplete(task_id, failed_text, session_id=session_id, **with_metrics)
            return ExecutionOutcome(task_id, "incomplete", failed_text)
        self._report_progress(task_id, "error", text=errs, output=failed_text)
        # CHAT FIRST, then the terminal status: see _post_conv's note on ordering.
        self._post_conv(conv_id, f"I couldn't complete this: {failed_text}", kind="failed",
                        task_id=task_id, card_id=card_id)
        self.dispatch_report("report_failed", task_id, failed_text, session_id=session_id,
                             **with_metrics)
        return ExecutionOutcome(task_id, "failed", failed_text)

    def _with_context_receipt(self, reported: str, request_text: Optional[str],
                              run_output: Optional[str] = None,
                              autopilot_composed: bool = False) -> str:
        """``reported`` with a context-updates receipt appended, when this task carried updates.

        The task's OWN text is the record of what was offered when an autopilot pass composed it
        (see ``context_updates.parse_manifest``), which is what lets the receipt be rendered here
        without a bundle object travelling with the task through the queue; a task that asked the
        engine itself has the bundle on this executor. Every offered ref is listed, including the
        ones the run said nothing about: a person reads this to find out whether what they wrote
        reached the work, and a silent ref is the answer they most need.

        ``autopilot_composed`` gates whether ``request_text`` is trusted as that record: only the
        caller who knows (from the task's routing metadata, never from scanning the text itself)
        that this task's text was actually built by the autopilot pass may pass True. Without it,
        a task whose text merely QUOTES a prior brief -- someone pasting a previous run's output
        into chat, which the backend files as its own, brand-new task -- would otherwise have its
        stale, quoted block read as this run's own manifest, rendering a receipt for refs the run
        never saw. A task that is not autopilot-composed falls back to this executor's own context
        bundle (populated when this task asked the engine itself), same as before.

        ``run_output`` names where the run's own account is, when that is not the text being
        reported. A task that carried no updates gets its text back untouched.
        """
        try:
            # Trust the task's own text only when the caller has confirmed (via routing metadata)
            # that the autopilot pass actually composed it; otherwise go straight to this
            # executor's own context view, exactly like a task with no updates at all.
            manifest = (parse_manifest(request_text or "") if autopilot_composed else None) or (
                self._context_bundle.manifest() if self._context_bundle is not None else [])
            if not manifest:
                return reported
            account = reported if run_output is None else run_output
            receipt = render_receipt(manifest, parse_usage_notes(account))
            # The run has just said what it did with each thing, in a listed vocabulary. Record it
            # before rendering: the receipt shows the person this run's account, the ledger is what
            # carries it to the NEXT run, which is the whole difference between an assistant that
            # remembers being asked and one that starts every morning from nothing.
            self._record_dispositions(account)
            if not receipt:
                return reported
            return strip_usage_block(reported).rstrip() + "\n\n" + receipt
        except Exception:  # noqa: BLE001 -- a receipt never costs a finished run its result
            log.warning("context-updates receipt could not be rendered", exc_info=True)
            return reported

    def _record_dispositions(self, account: str) -> None:
        """Fold this run's own account into the ledger, so the next run inherits it.

        Best-effort and silent on failure, like every other piece of bookkeeping around a finished
        run: the work is done and reported either way, and a ledger that could not be written costs
        a repeated question, never a lost result.
        """
        # getattr, not attribute access: this runs from the receipt path, which a consumer or a
        # test may reach on an object that never went through this executor's own __init__.
        bundle = getattr(self, "_context_bundle", None)
        ledger = getattr(bundle, "_ledger", None) if bundle is not None else None
        card_id = getattr(bundle, "card_id", "") if bundle is not None else ""
        offered = bundle.offered_keys() if ledger is not None else []
        if ledger is None:
            # A brief the autopilot pass composed: the pass collected its updates, so this
            # executor holds no bundle. The pass left the ref-to-item map with the ledger, keyed
            # by this task, and that is what the receipt is recorded against. Without it every
            # item the run closed was re-offered as "still owed" on every later pass.
            engine = getattr(self, "_update_engine", None)
            ledger = getattr(engine, "_ledger", None) if engine is not None else None
            remembered = (ledger.offer_for(getattr(self, "_receipt_task_id", ""))
                          if ledger is not None and hasattr(ledger, "offer_for") else None)
            if not remembered:
                return
            card_id, offered = remembered
        try:
            from .context_updates import parse_dispositions
            from .feedback_ledger import record_run_account
            moved = record_run_account(
                ledger, card_id=card_id, offered=offered,
                dispositions=parse_dispositions(account),
                guidance_writer=self._guidance_writer())
            if moved:
                log.info("feedback ledger: %d item(s) moved by this run (%s)", len(moved),
                         ", ".join(f"{m.item_id}->{m.state}" for m in moved[:6]))
        except Exception:  # noqa: BLE001 -- never cost a finished run its result
            log.warning("feedback ledger: this run's account could not be recorded", exc_info=True)

    def _guidance_writer(self):
        """How a standing rule reaches future runs, or None when this deployment has nowhere.

        A standing instruction belongs in guidance, which is the machinery that already puts the
        few relevant rules in front of every run. Wired from whatever the deployment configured;
        absent one, a standing item is still TRACKED, it just does not get a card.
        """
        manager = getattr(self, "_guidance_manager", None)
        if manager is None:
            return None
        from .feedback_ledger import guidance_writer_for
        return guidance_writer_for(manager)

    def _compose_done_report(self, request_text: Optional[str], raw_summary: str,
                             result: OrchestratorResult,
                             rep_preamble: Optional[str] = None) -> str:
        """Fold a fully met deep run's raw output into the done message posted back to the user.

        Uses the orchestrator's report synthesis (same prompt shape as the interactive after-deep
        fold-back, worker tier) so the message reads as the AI reporting its own finished work,
        then CLAIM-CHECKS the rewrite against the run's execution record (claims_unexecuted): a
        rewrite that claims work the record does not back, or that cannot be checked at all, is
        discarded for the raw summary, which is the goal-verified output itself. Never raises and
        never loses the substance: any failure returns ``raw_summary`` unchanged.
        """
        request = (request_text or "").strip()
        if not request or not (raw_summary or "").strip():
            return raw_summary
        synthesize = getattr(self._orch, "synthesize_task_report", None)
        claim_check = getattr(self._orch, "report_claims_unbacked", None)
        if not callable(synthesize) or not callable(claim_check):
            return raw_summary        # older/stub brains: keep the raw verified output
        try:
            synthesized = (synthesize(request, raw_summary,
                                      rep_preamble=rep_preamble) or "").strip()
            if not synthesized or synthesized == raw_summary:
                return raw_summary
            unbacked = claim_check(request, synthesized,
                                   getattr(result, "execution_record", None))
            if unbacked is False:
                return synthesized
            # True (unbacked claim) or None (check could not run): never post an unchecked
            # rewrite; the raw summary is the output the goal loop already verified.
            log.info("done-report rewrite discarded (claim check: %s); posting raw output",
                     "unbacked claim" if unbacked else "not checkable")
        except Exception:  # noqa: BLE001 — report composition must never affect the outcome
            log.warning("done-report composition failed; posting raw output", exc_info=True)
        return raw_summary

    # --- safety wrappers (reporting must not crash the poller) ---------------

    def dispatch_report(self, method_name: str, task_id: str, *args,
                        session_id: Optional[str] = None,
                        result_data: Optional[Dict[str, Any]] = None) -> None:
        """Call one of the client's TERMINAL report methods, carrying this run's session id.

        One place decides how the session id travels, so every terminal status reports it the same
        way. The id is added only when there IS one and only when the client's method accepts it
        (see ``client_accepts_session_id``), so a consumer's older client, or a test double written
        before session continuity existed, is called with exactly the arguments it always was.
        ``result_data`` (the run's metric facts) travels the same way, and only when the method
        declares that parameter itself: a client that has never heard of it is called as before.
        Wrapped in ``_safe`` exactly like the direct calls it replaces: a reporting failure is
        logged, never raised at the poller.
        """
        fn = getattr(self._client, method_name, None)
        if not callable(fn):
            return
        kwargs: Dict[str, Any] = {}
        if session_id and client_accepts_session_id(fn):
            kwargs["session_id"] = session_id
        if result_data and client_accepts_named_param(fn, "result_data"):
            kwargs["result_data"] = result_data
        self._safe(lambda: fn(task_id, *args, **kwargs))

    def dispatch_done(self, task_id: str, text: str, outcome: str, *,
                      session_id: Optional[str] = None,
                      metric_data: Optional[Dict[str, Any]] = None) -> None:
        """Report done, stamping ``result_data.outcome`` (``done`` or ``nothing_done``) when the
        client's ``report_done`` declares a ``result_data`` parameter; otherwise the plain call.
        The run's metric facts (``metric_data``, see ``metric_report_data``) go in the same
        ``result_data`` beside the outcome."""
        fn = getattr(self._client, "report_done", None)
        if not callable(fn):
            return
        kwargs: Dict[str, Any] = {}
        if session_id and client_accepts_session_id(fn):
            kwargs["session_id"] = session_id
        if client_accepts_named_param(fn, "result_data"):
            kwargs["result_data"] = {"outcome": outcome, **(metric_data or {})}
        self._safe(lambda: fn(task_id, text, **kwargs))

    def dispatch_incomplete(self, task_id: str, result: str, *,
                            session_id: Optional[str] = None,
                            result_data: Optional[Dict[str, Any]] = None) -> None:
        """Report "ran, goal not fully met". A client with no ``report_incomplete`` (an older
        consumer client or a test double) gets ``report_failed`` instead, so the outcome is never
        silently dropped; it just keeps the coarser old status.
        """
        name = ("report_incomplete" if callable(getattr(self._client, "report_incomplete", None))
                else "report_failed")
        self.dispatch_report(name, task_id, result, session_id=session_id, result_data=result_data)

    def _safe(self, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.error("report failed", exc_info=True)

    def _safe_report_failed(self, task_id: str, msg: str):
        self._safe(lambda: self._client.report_failed(task_id, msg))

    def _on_milestone(self, task_id: str, conv_id: Optional[str], event: ProgressEvent,
                      card_id: Optional[str] = None) -> None:
        """Surface a real milestone on the live task-detail stream, and ONLY there.

        Milestones are interim: a deep attempt's "Worker output" (posted BEFORE the goal verifier
        rules on it, so it can be a "fixed" claim the verifier then rejects), a sub-goal marked
        complete, a retry. They belong on the task's progress feed. They used to be posted into
        the originating chat as well, and on 2026-09-27 that put a rejected attempt's wrong
        "fixed" claim and three contradicting updates into the reporter's conversation within
        eight minutes, while the one message that mattered (the verified result) was the only
        one they could trust. The chat now hears a task twice: when it starts and when it ends
        (``_report``'s closing post, written only after verification). ``conv_id``/``card_id``
        stay in the signature so a caller need not change. Best-effort: a dropped progress event
        must never affect the task outcome.

        A milestone that carries the run's metric result (the finished goal's "Completed: ..."
        line: ``data["metrics"]`` / ``data["metric_missing"]``) passes exactly those facts on as the
        post's ``data``, so the task page can show what the metric was before and after next to
        the line itself. Any other milestone is posted as before."""
        if event.text:
            facts = metric_facts((event.data or {}).get("metrics"),
                                 (event.data or {}).get("metric_missing"))
            if facts:
                self._report_progress(task_id, "exec", text=event.text, data=facts)
            else:
                self._report_progress(task_id, "exec", text=event.text)

    @staticmethod
    def _leftover_arrival(claimed: List[Dict[str, Any]],
                          run_ended_at: Optional[datetime]) -> str:
        """When the leftover messages arrived relative to the run: ``"during"``, ``"after"`` or
        ``"unknown"``.

        Decided from each message's own ``at`` timestamp against the moment the run's work ended,
        never guessed. ``"during"`` only when every message was sent before the run ended,
        ``"after"`` only when every one was sent after, and ``"unknown"`` for a mix or when a time
        is missing or unreadable, so the copy never claims more than is known."""
        if run_ended_at is None:
            return "unknown"
        verdicts = set()
        for message in claimed:
            try:
                sent = datetime.fromisoformat(str(message.get("at") or "").replace("Z", "+00:00"))
                if sent.tzinfo is None:
                    sent = sent.replace(tzinfo=timezone.utc)
            except ValueError:
                return "unknown"
            verdicts.add("during" if sent < run_ended_at else "after")
        return verdicts.pop() if len(verdicts) == 1 else "unknown"

    def _start_run_for_leftover_messages(self, task_id: str,
                                         run_ended_at: Optional[datetime] = None) -> None:
        """Turn steering messages the run never folded in into a new run on the same task.

        A person can send a message while the task is in_progress. The run only claims messages at
        its own drain points (the start, and each planning, retry or verification boundary), so one
        that arrives after the last of those, or during a deep run's single subprocess, is still
        unclaimed when the run reports. Before this, such a message was left on a terminal task:
        the chat showed it as "missed" and asked the person to send it again.

        Now it is claimed (which also stamps it delivered, so it cannot be folded in twice) and
        opened as a run on the thread via ``open_run_on_task``. The thread goes back in the queue
        and the runner produces a second output for it, with no resend. If that open fails, the
        person is told on the task's feed, since the claim already marked the message delivered.
        Best-effort like the other reads: never raises, and nothing happens when nothing is pending.
        """
        claimed = self._claim_task_messages(task_id)
        leftover = self._message_texts(claimed)
        if not leftover:
            return
        # What the feed says about WHEN the message arrived must be true, so it is worked out from
        # the message times and not assumed: a steer sent while the run was working could not be
        # folded in (the worker was mid-step), and that is not "after the run finished".
        arrival = self._leftover_arrival(claimed, run_ended_at)
        many = len(leftover) > 1
        if arrival == "during":
            when = ("were sent while the run was still working" if many
                    else "was sent while the run was still working")
        elif arrival == "after":
            when = "arrived after the run finished"
        else:
            when = ("were not picked up by the run" if many else "was not picked up by the run")
        subject = f"Your {len(leftover)} messages" if many else "Your message"
        text = "\n\n".join(leftover)
        open_run = getattr(self._client, "open_run_on_task", None)
        try:
            if not callable(open_run):
                raise RuntimeError("this client cannot open a run on a task")
            open_run(task_id, text)
        except Exception as e:  # noqa: BLE001 -- the report already went out; never break it
            log.warning("could not start a run for %d late message(s) on task %s: %s",
                        len(leftover), task_id, e)
            self._report_progress(
                task_id, "error",
                text=(f"{subject} {when}, and {'they' if many else 'it'} could not start a new "
                      "run. Send again to try once more."))
            return
        self._report_progress(
            task_id, "status",
            text=f"{subject} {when}, so {'they' if many else 'it'} started a new run.")
        self._mark_messages_seen(task_id, claimed, "new_run")
