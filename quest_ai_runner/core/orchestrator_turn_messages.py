"""The goal loop's two extra messages: an interim reply while work continues, and an ask when a person must act.

Glue between the loop in ``orchestrator_deep_run`` and the pure policies (``interim_policy``, ``ask_policy``). Kept
out of the loop so the loop only calls ``maybe_send_interim`` and ``file_blocked_ask``. See
``docs/explanation/incremental-replies-and-asks.md``.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from hubofhubs.ports.deep import ProgressEvent
from hubofhubs.ports.events import EVENT_DECISION, EVENT_INTERIM

from .ask_policy import ask_event_data, ask_text, blocks_on_person, file_ask, resolve_assignee
from .interim_policy import (
    InterimLedger, InterimState, SEND, decide, done_text, interim_event_data, interim_text_of, progress_signature)

LEDGER_KEY = "interim_ledger"


def interim_ledger_for(ctx_meta: Optional[Dict[str, Any]]) -> InterimLedger:
    """The turn's ledger of sent messages, kept on the turn's meta so sub-goals share the caps (a fresh one without meta)."""
    if ctx_meta is None:
        return InterimLedger()
    ledger = ctx_meta.get(LEDGER_KEY)
    if not isinstance(ledger, InterimLedger):
        ledger = ctx_meta[LEDGER_KEY] = InterimLedger()
    return ledger


def maybe_send_interim(orch: Any, emit: Any, ctx_meta: Optional[Dict[str, Any]], res: Any, verdict: Mapping[str, Any], *,
                       blocker: str, reason: str, attempt: int, loop_runs_again: bool, now: Optional[float] = None) -> str:
    """Emit an interim event when the policy says so; return the policy's reason (for the log and tests)."""
    cfg = orch.cfg
    if emit is None or not getattr(cfg, "interim_replies", False):
        return "disabled"
    ledger = interim_ledger_for(ctx_meta)
    receipts: List[str] = list(getattr(res, "observations", None) or []) if getattr(res, "observations_reported", False) else []
    coverage = getattr(res, "coverage", None)
    output = (res.output or "")
    signature = progress_signature(output=output, receipts=receipts, coverage=coverage)
    flagged = bool(verdict.get("claims_unexecuted") or verdict.get("claims_unsupported")
                   or getattr(res, "claim_corrected", False))
    done = done_text(receipts=receipts, coverage=coverage, output=output, output_flagged=flagged)
    clock = time.monotonic() if now is None else now
    next_step = str(verdict.get("next_action") or "").strip()
    decision = decide(InterimState(
        enabled=True, met=bool(verdict.get("met")), blocker=blocker, loop_runs_again=loop_runs_again,
        ask_open=bool(getattr(res, "decision_id", None)), has_why=bool(reason.strip()), has_content=bool(done.strip()),
        sent_count=ledger.sent_count, last_sent_at=ledger.last_sent_at, now=clock, signature=signature,
        last_signature=ledger.last_signature, max_per_turn=int(getattr(cfg, "max_interims_per_turn", 4)),
        min_seconds_between=float(getattr(cfg, "min_seconds_between_interims", 15.0))))
    if decision.reason != SEND:
        return decision.reason
    seq = ledger.record(clock, signature)
    data = interim_event_data(done=done, why_more=reason, next_step=next_step, coverage=coverage, attempt=attempt, seq=seq)
    emit.emit(ProgressEvent(type=EVENT_INTERIM, text=interim_text_of(data), data=data))
    return decision.reason


def file_blocked_ask(orch: Any, emit: Any, ctx_meta: Optional[Dict[str, Any]], res: Any, verdict: Mapping[str, Any], *,
                     question: str, failures: Sequence[Any] = ()) -> bool:
    """A block only a person can lift becomes ONE deduplicated decision-request, shown as an ask.

    True when the ask was filed (``res.decision_id`` is set, ``res.output`` is the plain ask text, an ``EVENT_DECISION``
    went out). False when the verdict is not a block, there is no sink, or filing failed; the caller then ends the turn
    as a final, and ``res.output`` says the question could not be filed rather than claiming an ask exists.
    """
    if not blocks_on_person(str(verdict.get("blocker") or ""), question, failures):
        return False
    meta = ctx_meta or {}
    asker = str(meta.get("asker_user_id") or meta.get("requester_user_id") or meta.get("user_id") or "")
    assignee = resolve_assignee(str(verdict.get("asked_of") or ""), str(verdict.get("member_ref") or ""),
                                meta.get("participants") or [], asker)
    receipts = list(getattr(res, "observations", None) or []) if getattr(res, "observations_reported", False) else []
    flagged = bool(verdict.get("claims_unexecuted") or verdict.get("claims_unsupported"))
    done = done_text(receipts=receipts, coverage=getattr(res, "coverage", None), output=res.output or "", output_flagged=flagged)
    why_only_you = str(verdict.get("reason") or "").strip()
    options = None
    outcome = file_ask(
        getattr(orch, "escalation", None), question=question, why_only_you=why_only_you, done=done, options=options,
        quest_id=meta.get("quest_id"), conv_id=str(meta.get("conv_id") or ""), turn_id=str(meta.get("turn_id") or ""),
        asker_user_id=asker, assignee=assignee, summary=orch._concise_decision_summary(question))
    if not outcome.filed:
        res.output = ((done + "\n\n") if done else "") + (
            "I need one thing from you to finish this, but I could not file the question for you: " + question)
        return False
    res.decision_id = outcome.decision_id
    teammate = ""
    if assignee.asked_of == "member":
        teammate = next((str(p.get("name") or "").strip() for p in (meta.get("participants") or [])
                         if isinstance(p, dict) and str(p.get("user_id") or "") == assignee.user_id), "")
    res.output = ask_text(done, question, teammate)
    data = ask_event_data(question=question, why_only_you=why_only_you, done=done, options=options, outcome=outcome,
                          asker_user_id=asker)
    if emit is not None:
        emit.emit(ProgressEvent(type=EVENT_DECISION, text=res.output, decision_id=outcome.decision_id,
                                result_kind="ask", data=data))
    return True


def turn_outcome(res: Any) -> Dict[str, Any]:
    """The turn's end in the section 10 vocabulary, read from the result's structure only.

    ``asked`` (a decision-request is waiting on a person), ``met``, or ``stopped`` with a ``stop_reason``
    (``budget``, ``stalled``, ``attempt_cap``, ``tool_fault``). A turn with no unmet verdict is ``met``.
    """
    deep = list(getattr(res, "deep_results", None) or [])
    if any(getattr(d, "decision_id", None) and getattr(d, "needs_person", "") for d in deep):
        return {"outcome": "asked"}   # a block filed as an ask; a parked approval card keeps its existing shape
    if deep:
        unmet = [d for d in deep if not getattr(d, "met", False)]
        if not unmet:
            return {"outcome": "met"}
        reason = next((getattr(d, "stop_reason", "") for d in unmet if getattr(d, "stop_reason", "")), "") or "attempt_cap"
        return {"outcome": "stopped", "stop_reason": reason}
    verdict = getattr(res, "goal_verdict", None)
    if isinstance(verdict, dict) and not verdict.get("met"):
        return {"outcome": "stopped", "stop_reason": "attempt_cap"}
    return {"outcome": "met"}
