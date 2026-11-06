"""Pursuing an unmet goal before giving up: the orchestrator's last net, run through the hub's bounded replan (Critical Thinking's ``pursue``).

When the answer's goal is still unmet and no deep search was tried, the read-only deep search is the one untried means. The hub's ``BoundedReplan``
decides what happens, from structured facts only (the verifier's boolean, the means and their estimated seconds, the budget, what the attempt
returned): a short search runs before the reply is sent; a long one sends the current reply first with one plain "still working" line and then
continues, as a queued follow-up that posts to the same conversation when a queue runner is wired, else inline; a search that finds nothing, or a
budget that is spent, ends with the best reply and an honest line saying what was tried and what is missing. The only means offered is a read-only search (the brief forbids changes, and
no search starts once a change was attempted this turn), so a replan never adds a write. Reuses the verifier's verdict and the deep run's own goal verdict; nothing here reads the words of a reply.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Mapping

from hubofhubs.api import EVENT_MILESTONE, BoundedReplan, ProgressEvent, runner_starts_background_work

from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
from quest_ai_runner.the_intelligence.mind.orchestrator_constants import log
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decide_tool import OWN_ESCALATION_KEPT_STATUS, own_escalation_adds_nothing
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import _truncate_goal, user_ordered_work

SEARCH = "search the corpus, cards, notes and files"
READ_ONLY_BRIEF = ("Search the corpus, cards, notes and files thoroughly and give a definitive answer. Do not ask the user which file or quest to look in; "
                   "find it yourself. Only say something could not be found after actually searching, and name where you looked. "
                   "This is a read-only search: do not create, change or send anything.")


def background_runner_wired(orchestrator: Any) -> bool:
    runners = [orchestrator.deep_runner] + list((orchestrator.deep_runners or {}).values())
    return any(runner_starts_background_work(r) for r in runners)


def pursue_before_giving_up(orchestrator: Any, rs: SimpleNamespace, goal: str, exec_record: Any, ctx_meta: Dict[str, Any], emit: Any, cancel_check: Any,
                            take_resume_session: Any, working_dir_override: Any) -> Dict[str, Any]:
    """Runs the hub's bounded replan over the deep search. Returns ``pursuit`` (the hub's result), ``result`` (the deep OrchestratorResult that answers the
    turn, or None) and ``followup_started`` (the search continues as a queued follow-up)."""
    cfg = orchestrator.cfg
    verdict = rs._last_verdict or {}
    gap = verdict.get("next_action") or verdict.get("reason") or "the answer did not meet the goal"
    held: Dict[str, Any] = {"result": None, "plan": None}
    background = background_runner_wired(orchestrator)
    # A remark (``inform``) or a hold-off never starts a queued follow-up; a request (``act`` or ``ask``) that is still unmet may.
    ordered = rs.turn_user_intent in ("act", "ask")     # the planner's structured reading: the person asked for something, so the follow-up is theirs

    def check(outcome: Any) -> Mapping[str, Any]:
        if isinstance(outcome, Mapping):
            return {"met": bool(outcome.get("met")), "reason": str(outcome.get("reason") or gap)}
        return {"met": False, "reason": str(gap)}

    def announce(line: str) -> None:
        if emit is not None:
            emit.emit(ProgressEvent(type=EVENT_MILESTONE, text=((rs.text or "").rstrip() + "\n\n" + line).strip(), data={"still_working": True}))

    def attempt(step: Mapping[str, Any]) -> Mapping[str, Any]:
        plan = PlanDecision(
            action="deep", goal=_truncate_goal(goal),
            deep_brief=(f"{rs.user_message}\n\nNOTE: The quick answer did not meet the goal and no deep search has been done yet. What is missing: "
                        f"{step['missing'] or gap}. {READ_ONLY_BRIEF}")[:2000],
            rationale="goal not met: pursued before giving up",
            deep_difficulty=getattr(rs.plan, "deep_difficulty", None), deep_difficulty_reason=getattr(rs.plan, "deep_difficulty_reason", None))
        before = len(getattr(exec_record, "facts", None) or [])
        res = orchestrator._run_deep(
            plan, rs.user_message, orchestrator._answer_model(plan, "balanced", hint=rs.model_hint), emit=emit, rep_preamble=rs.rep_preamble,
            exec_record=exec_record, gathered=rs.gathered, quality_standards=rs.quality_standards, pending_inputs=rs.pending_inputs,
            model_hint=rs.model_hint, ctx_meta=ctx_meta, cancel_check=cancel_check, working_dir_override=working_dir_override,
            resume_session_id=take_resume_session(), self_initiated=True, user_ordered=(ordered and bool(step.get("background"))) or user_ordered_work(rs.turn_user_intent),
            run_control=rs.run_control, card_context=rs.card_context)
        writes: list = []     # the record counts every deep run as an action, so a write cannot be told from it: the brief is read-only and the search framing is the runner's
        if res.kind == "cancelled":
            held["result"] = res
            return {"outcome": "", "means_used": [SEARCH], "writes": writes}
        said_something = any((getattr(d, "output", "") or "").strip() or getattr(d, "decision_id", None) for d in (getattr(res, "deep_results", None) or []))
        if own_escalation_adds_nothing(res, exec_record, before) or not said_something:
            log.info("pursuit: the deep search added nothing; keeping the answer")
            if emit is not None:
                emit.status(OWN_ESCALATION_KEPT_STATUS)
            return {"outcome": "", "means_used": [SEARCH], "writes": writes}
        held["result"] = res
        res.exit_reason = "escalated_deep"
        res.goal_verdict = rs._last_verdict
        # A search that produced something is the better reply: it ran its own goal loop and says honestly what it could not find.
        return {"outcome": {"text": getattr(res, "text", "") or "answered", "met": True}, "means_used": [SEARCH], "writes": writes}

    means = [{"name": SEARCH, "kind": "read", "cost_seconds": cfg.pursue_deep_seconds}]
    budget = {"max_replans": cfg.pursue_max_replans, "wall_seconds": cfg.pursue_wall_seconds, "inline_seconds": cfg.pursue_inline_seconds, "background": background}
    pursuit = BoundedReplan().pursue(goal, "", rs.text, attempt, means, budget, announce, check)
    if pursuit["status"] == "continuing":
        # the current reply has gone out with its one "still working" line: the search continues as a queued follow-up that posts to this conversation
        attempt({"missing": pursuit["missing"], "background": True})
        started, held["result"] = held["result"], None
        return {"pursuit": pursuit, "result": None, "followup_started": started is not None}
    return {"pursuit": pursuit, "result": held["result"], "followup_started": False}
