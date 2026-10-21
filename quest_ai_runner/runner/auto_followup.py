"""Automatic follow-up runs: finish what a task's deep run left undone, without being asked.

A deep run can end with the original ask clearly unfinished: the verifier judged the goal not met and
said what is still open, or the worker stopped on its turn budget. Before this, the task ended there
with "what is left" text and a person had to reply to get the rest done. When nothing is waiting on a
person, that is a wasted round trip, so the task executor (``TaskExecutor.execute``) starts another
deep run for the remaining items. It is origin-agnostic: every queued task goes through the executor,
whether it came from chat, the task modal, an autopilot pass or an API call.

This module holds the two pure pieces so the policy is testable on its own:

* ``assess`` decides, from STRUCTURED fields of the finished result only (never from the wording of
  anything the model wrote, hard rule #3), whether to start a follow-up, and returns a plain reason
  either way so the decision can be logged and audited.
* ``followup_brief`` writes the brief for the next run: the original ask, what the previous run did,
  and the explicit remaining items.

A follow-up starts only when ALL of these hold: the result is a deep run (or a verified-not-met
answer turn) that is not complete; at least one unmet part NAMES what remains (the verifier's reason
or next action, or the worker's own turn-limit stop); no part is blocked on a person (a decision, a
question only a person can answer, a failing tool, a stalled loop, a runner out of moves, a usage
limit, an unverified result); the run was not cancelled, paused or handed off; the chain is under
its run cap and token budget; and no new decision appeared for a person while it ran.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..core.orchestrator_constants import (
    AUTO_FOLLOWUP_ASK_MAX_CHARS,
    AUTO_FOLLOWUP_DONE_MAX_CHARS,
    AUTO_FOLLOWUP_REMAINING_MAX_CHARS,
)

# The verifier's blockers that mean a person (not another run) has to act. ``more_work`` and
# ``evidence_only`` are not here: the first is exactly what a follow-up is for.
_PERSON_BLOCKERS = ("needs_person", "tool_failure")


@dataclass
class FollowUpDecision:
    """Whether to start another run, why, and the remaining items it should finish."""
    follow_up: bool
    reason: str
    remaining: List[str] = field(default_factory=list)


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def _cap(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " (cut)"


def result_tokens(result: Any) -> int:
    """The tokens the worker(s) of ``result`` reported, summed. 0 when a runner reports none."""
    total = 0
    for d in getattr(result, "deep_results", None) or []:
        try:
            total += max(0, int(getattr(d, "tokens", 0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _deep_block_reason(d: Any) -> Optional[str]:
    """Why this UNMET deep result cannot be followed up, or None when it can."""
    if getattr(d, "decision_id", None):
        return "a decision is open for a person"
    if getattr(d, "needs_person", ""):
        return "only a person can supply what is missing"
    if getattr(d, "tool_failure", ""):
        return "a tool the work needs is failing"
    if getattr(d, "stalled", ""):
        return "two attempts in a row ended at the same point"
    if getattr(d, "usage_limited", False):
        return "the usage limit is reached"
    if getattr(d, "launch_failed", False):
        return "the worker could not start"
    if getattr(d, "cancelled", False) or getattr(d, "paused", False):
        return "the run was stopped or paused on request"
    if getattr(d, "deferred", False) or getattr(d, "declined_background", False):
        return "the work was handed off elsewhere"
    if getattr(d, "exhausted", False):
        return "the runner has no new move to try"
    if (getattr(d, "error", None) or "").startswith("Unverified"):
        return "the result could not be verified, so what remains is unknown"
    return None


def _deep_remaining(d: Any) -> str:
    """What this unmet deep result says remains, from structured fields. '' when it names nothing."""
    reason = _clean(getattr(d, "verdict_reason", ""))
    nxt = _clean(getattr(d, "verdict_next_action", ""))
    if reason or nxt:
        return " ".join(part for part in (reason, ("Next: " + nxt) if nxt else "") if part)
    if getattr(d, "limit_hit", False):
        return ("The previous run used its whole turn budget before finishing; finish whatever "
                "it had not completed yet.")
    return ""


def remaining_items(result: Any) -> List[str]:
    """The remaining items a result names, one string per unmet part. Empty when it names none."""
    kind = getattr(result, "kind", None)
    items: List[str] = []
    if kind == "deep":
        for d in getattr(result, "deep_results", None) or []:
            if not getattr(d, "met", False):
                item = _deep_remaining(d)
                if item:
                    items.append(item)
    elif kind == "answer":
        verdict = getattr(result, "goal_verdict", None) or {}
        reason = _clean(verdict.get("reason"))
        nxt = _clean(verdict.get("next_action"))
        if reason or nxt:
            items.append(" ".join(p for p in (reason, ("Next: " + nxt) if nxt else "") if p))
    return items


def assess(result: Any, *, followups_done: int, max_followups: int,
           tokens_used: int, token_budget: Optional[int],
           new_decision_ids: Any = ()) -> FollowUpDecision:
    """Decide whether ``result`` earns an automatic follow-up run. Pure: reads structured fields."""
    if max_followups <= 0:
        return FollowUpDecision(False, "automatic follow-up runs are turned off")
    kind = getattr(result, "kind", None)
    if kind not in ("deep", "answer"):
        return FollowUpDecision(False, f"nothing to follow up: the run ended as {kind or 'unknown'}")
    if getattr(result, "decision_id", None) or getattr(result, "question", None):
        return FollowUpDecision(False, "blocked: the run raised a decision or question for a person")
    if new_decision_ids:
        return FollowUpDecision(False, "blocked: a new decision for a person appeared while it ran")

    if kind == "deep":
        deep = list(getattr(result, "deep_results", None) or [])
        if not deep:
            return FollowUpDecision(False, "nothing to follow up: the run produced no deep result")
        unmet = [d for d in deep if not getattr(d, "met", False)]
        if not unmet:
            return FollowUpDecision(False, "complete: every goal was verified met")
        for d in unmet:
            why = _deep_block_reason(d)
            if why:
                return FollowUpDecision(False, "blocked: " + why)
    else:
        verdict = getattr(result, "goal_verdict", None) or {}
        if getattr(result, "exit_reason", "") != "max_turns" or verdict.get("met") is not False:
            return FollowUpDecision(False, "complete: the reply is not an unmet goal")
        if getattr(result, "claim_corrected", False):
            return FollowUpDecision(False, "blocked: the reply was corrected to say the work did not happen")
        if verdict.get("blocker") in _PERSON_BLOCKERS or _clean(verdict.get("question")):
            return FollowUpDecision(False, "blocked: only a person can supply what is missing")

    remaining = remaining_items(result)
    if not remaining:
        return FollowUpDecision(False, "no remaining items are named, so the ask is not clearly incomplete")
    if followups_done >= max_followups:
        return FollowUpDecision(False, f"cap reached: {followups_done} follow-up run(s) already made "
                                       f"(limit {max_followups})", remaining)
    if token_budget is not None and tokens_used >= token_budget:
        return FollowUpDecision(False, f"token budget reached: {tokens_used} of {token_budget}", remaining)
    return FollowUpDecision(True, f"incomplete, unblocked and under the cap: {len(remaining)} item(s) remain",
                            remaining)


def followup_brief(original_ask: str, result: Any, remaining: List[str], *,
                   number: int, limit: int) -> str:
    """The brief for follow-up run ``number`` of at most ``limit``.

    Carries the original ask, what the previous run did (its own report, which may be partial) and
    the explicit remaining items, so the worker finishes those and does not redo finished work.
    """
    from ..core.orchestrator import _strip_future_context

    outputs: List[str] = []
    if getattr(result, "kind", None) == "deep":
        for d in getattr(result, "deep_results", None) or []:
            out = _strip_future_context(getattr(d, "output", "") or "").strip()
            if out:
                outputs.append(out)
    else:
        text = _strip_future_context(getattr(result, "text", "") or "").strip()
        if text:
            outputs.append(text)
    done = _cap("\n\n".join(outputs), AUTO_FOLLOWUP_DONE_MAX_CHARS) or "(the previous run reported nothing)"
    items = "\n".join(f"- {_cap(item, AUTO_FOLLOWUP_REMAINING_MAX_CHARS)}" for item in remaining)
    return (
        f"--- FOLLOW-UP RUN {number} OF AT MOST {limit}: THE ORIGINAL ASK IS NOT FINISHED ---\n"
        "An earlier run on this task stopped before the ask was complete, and nothing is waiting on "
        "a person, so you are finishing it now. If a session is resumed for you, everything it read "
        "and changed is still yours: do not start over and do not redo finished work.\n\n"
        f"ORIGINAL ASK:\n{_cap(original_ask, AUTO_FOLLOWUP_ASK_MAX_CHARS)}\n\n"
        f"WHAT THE PREVIOUS RUN DID (its own report, which may be partial):\n{done}\n\n"
        f"WHAT REMAINS (finish exactly these):\n{items}\n\n"
        "Finish the remaining items and verify them yourself. End with a concrete summary of what "
        "is now done across the whole ask. If something still cannot be finished, say precisely what "
        "is left and why."
    )
