"""The goal loop's two extra messages: an interim reply while work continues, and an ask when a person must act.

Glue between the loop in ``orchestrator_deep_run`` and the pure policies (``interim_policy``, ``ask_policy``). Kept
out of the loop so the loop only calls ``maybe_send_interim`` and ``file_blocked_ask``. See
``docs/explanation/incremental-replies-and-asks.md``.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from hubofhubs.api import render_coverage_line
from hubofhubs.api import ProgressEvent
from hubofhubs.api import EVENT_DECISION, EVENT_INTERIM
from hubofhubs.api import STEP_SUMMARIZE, plan_with_step

from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import INTERIM_PROGRESS_TOOL
from quest_ai_runner.the_intelligence.subconscious.context_retriever.card_filter import _extract_json
from quest_ai_runner.the_self.integrity.prompts.prompt_specs import render_prompt
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import found_constraint_head, turn_holds_records
from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import (
    Assignee, adopted_assignee, ask_event_data, ask_text, blocks_on_person, file_ask, resolve_assignee, why_only_you_of)
from quest_ai_runner.the_intelligence.mind.response_writer.interim_policy import (
    InterimLedger, InterimState, SEND, SKIP_NO_WHY, decide, done_text, interim_event_data, interim_text_of, progress_signature, ungrounded_terms)

from quest_ai_runner.the_intelligence.mind.response_writer import act_status
from quest_ai_runner.the_world.tools.tools import ToolContext

LEDGER_KEY = "interim_ledger"
GOAL_READ_TOOL = "list_quest_goals"      # the read-only tool that returns a quest's goal lines, used when the first reading came from context


def interim_ledger_for(ctx_meta: Optional[Dict[str, Any]]) -> InterimLedger:
    """The turn's ledger of sent messages, kept on the turn's meta so sub-goals share the caps (a fresh one without meta)."""
    if ctx_meta is None:
        return InterimLedger()
    ledger = ctx_meta.get(LEDGER_KEY)
    if not isinstance(ledger, InterimLedger):
        ledger = ctx_meta[LEDGER_KEY] = InterimLedger()
    return ledger


def write_progress(orch: Any, goal: str, receipts: Sequence[str], coverage: Any) -> Dict[str, str]:
    """The interim's "why more" and "next", written from the REQUEST, the receipts and the declared coverage only.

    The worker's output never reaches this call. Whatever comes back is checked by code (``ungrounded_terms``) against the same three
    stored texts: a figure or name that is in none of them empties the result, and so does any failure of the call, so the message
    is then not sent. Returns ``{"gap": "", "next": ""}`` when nothing safe can be said."""
    empty = {"gap": "", "next": ""}
    if not (goal or "").strip():
        return empty
    line = render_coverage_line(coverage)
    record = "\n".join(f"- {r}" for r in receipts) or "(nothing recorded)"
    try:
        prompt = render_prompt("reply-in-steps.interim-progress", goal=goal[:1000], receipts=record,
                               coverage=(line + "\n\n") if line else "")
        model = orch.registry.resolve_tier(getattr(orch.cfg, "explain_tier", None) or "fast")
        raw = plan_with_step(orch.get_provider_for_model(model), prompt, model=model, tool_schema=INTERIM_PROGRESS_TOOL,
                             step=STEP_SUMMARIZE)
        if isinstance(raw, str):
            raw = json.loads(_extract_json(raw) or "{}")
    except Exception:  # noqa: BLE001 -- a progress note must never break a turn
        return empty
    if not isinstance(raw, dict):
        return empty
    got = {"gap": str(raw.get("gap") or "").strip(), "next": str(raw.get("next") or "").strip()}
    return got if got["gap"] and got["next"] else empty


def grounded(text: str, goal: str, receipts: Sequence[str], coverage: Any) -> str:
    """``text`` if every figure and name in it is in the request, the receipts or the coverage line; otherwise empty."""
    allowed = "\n".join([goal or "", *[str(r) for r in receipts], render_coverage_line(coverage)])
    return text if text and not ungrounded_terms(text, allowed) else ""


def maybe_send_interim(orch: Any, emit: Any, ctx_meta: Optional[Dict[str, Any]], res: Any, verdict: Mapping[str, Any], *,
                       blocker: str, reason: str, attempt: int, loop_runs_again: bool, now: Optional[float] = None,
                       goal: str = "") -> str:
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
    done = done_text(receipts=receipts, coverage=coverage)
    clock = time.monotonic() if now is None else now

    def state(has_why: bool) -> InterimState:
        return InterimState(
            enabled=True, met=bool(verdict.get("met")), blocker=blocker, loop_runs_again=loop_runs_again,
            ask_open=bool(getattr(res, "decision_id", None)), has_why=has_why, has_content=bool(done.strip()),
            sent_count=ledger.sent_count, last_sent_at=ledger.last_sent_at, now=clock, signature=signature,
            last_signature=ledger.last_signature, max_per_turn=int(getattr(cfg, "max_interims_per_turn", 4)),
            min_seconds_between=float(getattr(cfg, "min_seconds_between_interims", 15.0)))
    # Every other condition first, with the reason assumed, so the writing call below is made only for a message that would be sent.
    pre = decide(state(not flagged))
    if pre.reason != SEND:
        return pre.reason
    # why and next are written WITHOUT the worker's output (``write_progress``) and checked against the request, the receipts and the
    # coverage. A verdict that still carries its own progress fields (a verifier or a test double that fills them) is used only when
    # the writer says nothing, and passes the same check. A flagged claim sent nothing above: there is nothing safe to say.
    written = write_progress(orch, goal, receipts, coverage)
    why = grounded(written["gap"] or str(verdict.get("progress_gap") or "").strip(), goal, receipts, coverage)
    next_step = grounded(written["next"] or str(verdict.get("progress_next") or "").strip(), goal, receipts, coverage)
    if not why or not next_step:
        return SKIP_NO_WHY
    seq = ledger.record(clock, signature)
    data = interim_event_data(done=done, why_more=why, next_step=next_step, coverage=coverage, attempt=attempt, seq=seq)
    emit.emit(ProgressEvent(type=EVENT_INTERIM, text=interim_text_of(data), data=data))
    return pre.reason


def ensure_found_constraint_head(orch: Any, ctx_meta: Optional[Dict[str, Any]], *, request: str, records: str, question: str) -> None:
    """An ask built from a verifier's question has no answer step behind it, so the found_constraint judge may not have run this
    turn. Run it once here on the question and, when the person's records conflict with the request, store the plain head
    (``found_constraint_head``) that ``file_blocked_ask`` opens the question with. Structured verdict only; never raises."""
    if ctx_meta is None or ctx_meta.get("found_constraint_head") or ctx_meta.get("found_constraint_judged"):
        return
    ctx_meta["found_constraint_judged"] = True
    try:
        verdict = orch.found_constraint_verdict(request, records, question)
        if verdict and verdict.get("conflict"):
            ctx_meta["found_constraint_head"] = found_constraint_head(str(verdict.get("fact") or ""), str(verdict.get("consequence") or ""))
    except Exception:  # noqa: BLE001 -- a standard must never break the turn
        pass


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
    # FOUND_CONSTRAINT standard (core/reply_standards.py): when this turn's verdict found that the person's records conflict with
    # the request, the fact and its consequence open the question, so the shown ask AND the filed decision-request carry them.
    head = str(meta.get("found_constraint_head") or "").strip()
    if head and head not in question:
        question = head + " " + question
    asker = str(meta.get("asker_user_id") or meta.get("requester_user_id") or meta.get("user_id") or "")
    assignee = resolve_assignee(str(verdict.get("asked_of") or ""), str(verdict.get("member_ref") or ""),
                                meta.get("participants") or [], asker)
    receipts = list(getattr(res, "observations", None) or []) if getattr(res, "observations_reported", False) else []
    done = done_text(receipts=receipts, coverage=getattr(res, "coverage", None))
    why_only_you = why_only_you_of(str(verdict.get("blocker") or ""), assignee, meta.get("participants") or [])
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


def records_text(context_view: Any, gathered: Any, view_chars: int = 3500, gathered_chars: int = 2500) -> str:
    """The turn's records as text for a judge: the rendered context, then what the turn gathered (a list of reads, each a dict or text)."""
    parts = []
    for item in gathered or []:
        parts.append(str(item.get("text") or item.get("hits") or "") if isinstance(item, dict) else str(item))
    return str(context_view or "")[:view_chars] + "\n" + "\n".join(p for p in parts if p)[:gathered_chars]


def adopt_turn_ask(orch: Any, emit: Any, ctx_meta: Optional[Dict[str, Any]], res: Any, turn_asks: Sequence[Mapping[str, Any]]) -> bool:
    """The turn's ask belongs to the runner even when the worker raised it with its own create_decision call. When the turn's ledger holds a
    decision-request, it becomes THE ask: shown as the ending (an ``EVENT_DECISION``), its assignee checked against the thread's participants
    (a stranger or nobody is the asker), the asker named as the one told the answer, and the reply text is the code-written ask text, so
    the reply cannot claim a request that no record backs. True when adopted."""
    entries = [e for e in turn_asks or [] if isinstance(e, Mapping) and e.get("decision_id")]
    if not entries:
        return False
    meta = ctx_meta or {}
    asker = str(meta.get("asker_user_id") or meta.get("requester_user_id") or meta.get("user_id") or "")
    participants = meta.get("participants") or []
    first = entries[0]
    assignee = adopted_assignee(first, participants, asker)
    teammate = next((str(p.get("name") or "").strip() for p in participants if isinstance(p, dict)
                     and str(p.get("user_id") or "") == assignee.user_id), "") if assignee.asked_of == "member" else ""
    question = str(first.get("summary") or "").strip()
    receipts = list(getattr(res, "observations", None) or []) if getattr(res, "observations_reported", False) else []
    done = done_text(receipts=receipts, coverage=getattr(res, "coverage", None))
    res.decision_id = str(first["decision_id"])
    res.output = ask_text(done, question, teammate)
    from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import AskOutcome
    data = ask_event_data(question=question, why_only_you=why_only_you_of("needs_person", assignee, participants), done=done, options=None,
                          outcome=AskOutcome(decision_id=res.decision_id, assignee=assignee), asker_user_id=asker)
    if emit is not None:
        emit.emit(ProgressEvent(type=EVENT_DECISION, text=res.output, decision_id=res.decision_id, result_kind="ask", data=data))
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
        # The verifier's own enum: the reply is complete but part of it could not be confirmed. Not a failure to try enough.
        return {"outcome": "stopped", "stop_reason": "unverified" if (
            verdict.get("blocker") == "evidence_only" or verdict.get("states_requested_facts")) else "attempt_cap"}
    return {"outcome": "met"}


# ---- status first, then the work, then the metric before and after (``act_status``)

def mutating_call_count(orch: Any, calls: Sequence[Mapping[str, Any]]) -> int:
    """How many of the planned tool calls change something (a name the registry does not know counts as changing)."""
    tools = getattr(orch, "tools", None)
    count = 0
    for call in calls or []:
        spec = tools.get(call.get("name")) if tools is not None else None
        if spec is None or getattr(spec, "mutates", True):
            count += 1
    return count


def entries_of(rs: Any, gathered: Sequence[Any]) -> List[Any]:
    """What the turn has read so far, as records to look a measure up in: the gathered reads, then the assembled context."""
    return [*gathered, str(getattr(rs, "context_view", "") or "")]


def send_status_first(orch: Any, emit: Any, rs: Any, gathered: Sequence[Any], *, user_turn: bool, doing: str = "", forced: bool = False) -> bool:
    """MESSAGE 1 of an act request: where it stands (the milestone's measure read from the records, or what the planner listed from
    its reads), then what happens now. Sent once, as an interim message, before the planned work runs. True when sent.

    The decision is ``act_status.should_send_status``: the planner's ``user_intent`` and a plan that is more than a read."""
    plan = getattr(rs, "plan", None)
    cfg = orch.cfg
    if emit is None or plan is None:
        return False
    if not act_status.should_send_status(
            enabled=bool(getattr(cfg, "interim_replies", False)), user_turn=user_turn, user_intent=getattr(rs, "turn_user_intent", None),
            action="tool" if forced else plan.action, mutating_calls=1 if forced else mutating_call_count(orch, plan.tool_calls or []),
            already_sent=getattr(rs, "status_first", None) is not None):
        return False
    meta = getattr(rs, "_ctx_meta", None) or {}
    try:
        records = entries_of(rs, gathered)
        reading = act_status.pick_reading(act_status.readings_in(records), rs.user_message)
        measured = begin_turn_metrics(orch, rs)
        if measured is not None:                      # the metric record's own reading outranks a line parsed from a goal's text
            reading = act_status.MetricReading(record_id=reading.record_id if reading else "", milestone=reading.milestone if reading else "",
                                               done_when=str(measured.get("name") or (reading.done_when if reading else "")),
                                               value=act_status.format_figure(measured.get("before")), call=reading.call if reading else None)
        evidence = "\n".join([rs.user_message or "", *[str(g.get("text") or "") for g in gathered if isinstance(g, dict)],
                              str(getattr(rs, "context_view", "") or "")])
        doing = doing or ((plan.goal or "") if plan.action == "deep" else (plan.rationale or ""))
        voice = meta.get("channel") == "voice"
        texts = act_status.status_texts(reading, list(getattr(plan, "facts_read", None) or []), doing, evidence=evidence, voice=voice)
        ledger = interim_ledger_for(meta if meta else None)
        clock = time.monotonic()
        seq = ledger.record(clock, progress_signature(output=texts["text"], receipts=[], coverage=None))
        data = interim_event_data(done=texts["status"], why_more="", next_step=texts["next"], coverage=None, attempt=0, seq=seq)
        data["status_first"] = True
        emit.emit(ProgressEvent(type=EVENT_INTERIM, text=texts["text"], data=data))
        rs.status_first = {"before": reading, "voice": voice}
        return True
    except Exception:  # noqa: BLE001 -- a status note must never break a turn
        return False


def begin_turn_metrics(orch: Any, rs: Any) -> Optional[Mapping[str, Any]]:
    """``every_request_measured``: the turn's metric record starts (the slice found, the cheap meter reads the metric before the work).
    The row of that reading, or None when the arm is off or nothing was read."""
    if not getattr(orch.cfg, "measure_every_request", False) or getattr(orch, "goal_iteration", None) is None:
        return None
    from quest_ai_runner.the_intelligence.critical_thinking.metric_translation.turn_metrics import TurnMetrics
    meta = getattr(rs, "_ctx_meta", None) or {}
    rs.turn_metrics = TurnMetrics(getattr(orch, "tools", None), orch.goal_iteration, str(meta.get("turn_id") or meta.get("task_id") or id(rs)),
                                  request=rs.user_message or "")
    return rs.turn_metrics.begin()


def reread_metric(orch: Any, rs: Any, before: "act_status.MetricReading") -> Optional["act_status.MetricReading"]:
    """The same record read again by CODE: the read-only tool call that returned it first (else the goal listing), run now."""
    tools = getattr(orch, "tools", None)
    if tools is None:
        return None
    call = dict(before.call or {})
    name = str(call.get("name") or GOAL_READ_TOOL)
    spec = tools.get(name)
    if spec is None or getattr(spec, "mutates", True):
        return None
    meta = getattr(rs, "_ctx_meta", None) or {}
    args = dict(call.get("args") or ({"quest_id": rs.quest_id} if getattr(rs, "quest_id", None) else {}))
    if not args:
        return None
    result = tools.invoke(name, args, ToolContext(quest_id=getattr(rs, "quest_id", None), user_id=meta.get("user_id"),
                                                  task_id=meta.get("task_id"), meta=dict(meta)))
    if not getattr(result, "ok", False):
        return None
    found = act_status.readings_in([{"text": result.text}])
    return act_status.matching(found, before)


def already_in_place(gathered: Sequence[Any]) -> List[str]:
    """Receipts of calls that found the change already there (status ``noop``) or were not run again (same call, same turn)."""
    out: List[str] = []
    for g in gathered:
        if not isinstance(g, dict) or not str(g.get("locator") or "").startswith("tool:"):
            continue
        if g.get("tool_status") == "noop":
            first = str(g.get("tool_text") or g.get("text") or "").strip().splitlines()
            out.append((first[0] if first else str(g["locator"])[5:])[:140].rstrip("."))
    return out


def close_with_metric(orch: Any, rs: Any, res: Any) -> None:
    """MESSAGE 2 of an act request: the reply ends with the metric before and after (read again by code) and anything already in place.
    Nothing is added to a turn that sent no first message, so an information request or a plain reply stays one message."""
    sent = getattr(rs, "status_first", None)
    if not sent or getattr(res, "kind", "") not in ("answer", "deep"):
        return
    try:
        before = sent.get("before")
        after = None
        tm = getattr(rs, "turn_metrics", None)
        if tm is not None and before is not None:     # the record's own second reading (the deep goal's code), else the goal line read again
            changed = bool(getattr(rs, "tool_calls_ran", False) or getattr(res, "deep_results", None))
            rows = tm.finish(res, changed=changed)
            row = next((r for r in rows if r.get("after") is not None), None)
            if row is not None:
                after = act_status.MetricReading(before.record_id, before.milestone, before.done_when, act_status.format_figure(row.get("after")))
        if after is None and before is not None:
            after = reread_metric(orch, rs, before)
        tail = act_status.closing_lines(before, after, reread=before is not None, already=already_in_place(rs.gathered),
                                        voice=bool(sent.get("voice")))
    except Exception:  # noqa: BLE001 -- the closing lines must never lose the reply
        return
    if not tail:
        return
    if (res.text or "").strip():
        res.text = res.text.rstrip() + "\n\n" + tail
    elif getattr(res, "deep_results", None) and isinstance(getattr(res.deep_results[-1], "output", None), str):
        res.deep_results[-1].output = (res.deep_results[-1].output or "").rstrip() + "\n\n" + tail
    else:
        res.text = tail
