"""The OVERSEER CASCADE for routing decisions: a strong model, on a few decisions, with few tokens.

The problem it solves, measured rather than assumed (2026-10-05, 765 labelled messages, dev half):
a cheap model can be made to route most requests correctly, but one class resists every prompt
change. Asked to do something that lives outside what a read can reach (a machine, a repository, a
document held elsewhere), a small model acknowledges the rule in its own rationale and then issues
the forbidden read anyway, roughly four times in five, while a model one tier up gets the same
cases right nine times in ten. Three prompt rewrites moved that number inside the noise floor.
That is a capability gap, not a wording gap, and the cheap way to close a capability gap is to buy
the stronger model's judgment ONLY where it is needed.

The shape:

  1. The planner reports, on the call it already makes, how sure it is of the decision it just
     made (one enum field, zero extra calls, about 40 input tokens). This is a STRUCTURED
     self-report, which is why it is a legitimate escalation signal where scanning the planner's
     own prose would not be (CLAUDE.md hard rule #3: never gate control flow on keywords in the
     model's own output, and never second-guess a structured decision by reading its wording).
  2. A decision the planner is not sure of goes to a STRONGER model, which sees a DIGEST and not
     the planner prompt again: the request, the cheap model's choice and reasoning, the ordered
     decision rubric, and a bounded slice of the grounding. Capped in characters, because the only
     reason the cascade is affordable is that the second call is small.
  3. The reviewer answers with a small schema of its own (an action, and the few fields that
     action needs), and ``apply_review`` folds that back onto the cheap decision, keeping
     everything the reviewer did not speak to. A reviewer that agrees costs nothing further: the
     cheap decision stands as it was, including the reads it chose.

Everything here is off unless ``OrchestratorConfig.planner_cascade`` is set, and a decision with
NO confidence reported is never escalated. Both are deliberate fail-safes: a model that does not
fill the field, or a response that fails to parse, must not silently escalate every decision and
turn the cheap-model deployment into an expensive one.

Generic: no org, product, or deployment names.
"""
from __future__ import annotations

import json
from typing import Any, Dict, FrozenSet, List, Optional

from hubofhubs.orchestrate.cascade import (  # noqa: F401  (the hub's structural half of the cascade)
    CONFIDENCE_LEVELS,
    ESCALATABLE_ACTIONS,
    apply_review as hub_apply_review,
    escalation_spec,
    should_escalate,
    truncate_keep_both_ends,
    user_goal_fallback as hub_user_goal_fallback,
)

from .adapters_values import PlanDecision
from .prompt_specs import declare, render_prompt

#: The one extra field the decide schema carries when the cascade is on.
CONFIDENCE_TOOL_FIELD: Dict[str, Any] = {
    "type": ["string", "null"],
    "enum": ["high", "medium", "low", None],
    "description": "How sure you are that THIS decision is the right next step. 'low' when the "
                   "request could reasonably belong to a different action, when you cannot tell "
                   "whether what it needs is reachable, or when you are guessing at a source. "
                   "'high' only when the choice is clear-cut. A decision marked below 'high' is "
                   "reviewed by a stronger model, so marking honestly costs nothing and guessing "
                   "confidently is what causes a wrong route to stand.",
}

#: Appended to the planner prompt when the cascade is on, so the field is explained where the
#: decision is made and not only in the schema.
PLANNER_CONFIDENCE_INSTRUCTION = """\
CONFIDENCE (`confidence`): also report how sure you are of this decision: "high", "medium" or
  "low". Say "low" or "medium" whenever the request could reasonably belong to a different
  action, you cannot tell whether what it needs is reachable from here, or you are guessing at a
  source, a path or a scope rather than naming one you can see. Anything below "high" is reviewed
  by a stronger model before it takes effect, so an honest "low" is free and a confident guess is
  what makes a wrong route stand.
"""

#: What the reviewer fills in. Deliberately small: it is the whole second call's output contract,
#: and every property is input tokens on a call whose entire point is being cheap.
REVIEW_TOOL: Dict[str, Any] = {
    "name": "review",
    "description": "Confirm or correct the proposed next step.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "answer", "deep", "confirm"]},
            "hand_off": {
                "type": "boolean",
                "description": "With action 'answer': also queue the work, because it lives "
                               "somewhere a read cannot reach. False to answer and queue nothing.",
            },
            "goal": {"type": ["string", "null"],
                     "description": "With 'deep' or a hand off: the short checkable done-standard."},
            "deep_brief": {"type": ["string", "null"],
                           "description": "With 'deep' or a hand off: the self-contained brief."},
            "confirm_question": {"type": ["string", "null"]},
            "rationale": {"type": "string", "description": "One sentence on why."},
        },
        "required": ["action", "rationale"],
    },
}

REVIEW_PROMPT = """\
You are the OVERSEER of a cheaper model's routing decision. It has already decided; your job is to
confirm that decision or correct it, and nothing else. You do not answer the request and you do not
do the work.

{rubric}
--- THE REQUEST ---
{user_message}

--- THE GROUNDING THE DECIDER HAD (abridged) ---
{context}

--- WHAT IT OBSERVED SO FAR (empty means it has read nothing yet) ---
{gathered}

--- ITS DECISION ---
{decision}

Apply the rules above to the request. If the decision already follows them, return the SAME action
and say so in one sentence. If it does not, return the action the rules require, and fill the few
fields that action needs. The most common error to look for: work that lives outside what a read
can reach, sent to a read anyway, often with an invented path or scope. Correct that to a hand off
("answer" with hand_off true) when the grounding names somewhere that covers the work, or to a
plain "answer" that says nothing attached can reach it when it does not.{web_note}
"""

#: Appended to ``REVIEW_PROMPT`` when a live web adapter is wired (``Orchestrator.web``), so the
#: reviewer corrects a genuinely public case to a web read instead of a hand off. Default ""
#: leaves the prompt byte-for-byte unchanged (the cascade ships OFF, this only matters if an
#: operator turns it on with web also configured).
WEB_REVIEW_NOTE = (
    " A public web page or document is reachable too: correct such a case to a read with a "
    "{\"web\": ...} or {\"web_page\": ...} spec instead of a hand off."
)


#: What QAR says when a reviewer corrects the action without writing a goal, or asks to confirm without a question.
DEFAULT_REVIEW_GOAL = "Carry out what the request asked for."
DEFAULT_REVIEW_QUESTION = "Which of these did you mean?"


def describe_decision(decision: PlanDecision) -> str:
    """The cheap model's decision, as the few lines a reviewer actually needs."""
    lines = [f"action: {decision.action}"]
    if decision.confidence:
        lines.append(f"its own confidence: {decision.confidence}")
    if decision.reads:
        lines.append("reads it wants to run: " + json.dumps(decision.reads)[:600])
    if decision.goal:
        lines.append(f"goal: {decision.goal[:300]}")
    if decision.deep_brief:
        lines.append(f"brief: {decision.deep_brief[:600]}")
    if decision.deferred_deep:
        lines.append("it also wants to queue: "
                     + str(decision.deferred_deep.get("goal") or "")[:300])
    if decision.confirm_question:
        lines.append(f"question it wants to ask: {decision.confirm_question[:300]}")
    if decision.rationale:
        lines.append(f"its reasoning: {decision.rationale[:400]}")
    return "\n".join(lines)


def build_review_digest(user_message: str, decision: PlanDecision, rubric: str,
                        context_view: str = "", gathered: Optional[List[Dict[str, Any]]] = None,
                        max_chars: int = 6000, *, web_configured: bool = False) -> str:
    """The whole second call's prompt, held under ``max_chars``.

    The cap is spent in priority order: the rubric and the decision are what the reviewer judges
    with, so they are never trimmed; the grounding slice absorbs whatever budget is left.

    THE GROUNDING SLICE KEEPS BOTH ENDS, and that was measured. Truncating it from the front alone
    made the cascade actively harmful (2026-10-05): the decisive sentences in a real grounding
    block, what the attached environments can and cannot do, and the statement of how far a read
    reaches, sit near its END, so head-truncation handed the reviewer the part that locates things
    and cut the part that says what is reachable. It then corrected correct decisions, scoring
    5 of 18 on cases where nothing attached could reach the work, against 16 of 18 with no
    cascade at all. This repo has made the same mistake once before, in deep-run activity
    truncation, where cutting from the start hid the filename behind a long shared path.

    ``web_configured`` (default False, byte-for-byte unchanged, matching every other reach-aware
    flag in this repo) appends ``WEB_REVIEW_NOTE`` so a reviewer that corrects a hand off also
    knows a public web page or document is reachable via a read, not just a hand off.
    """
    decision_text = describe_decision(decision)
    gathered_text = json.dumps(gathered or [])[:800]
    web_note = WEB_REVIEW_NOTE if web_configured else ""
    fixed = render_prompt("route-a-message.cascade-review", rubric=rubric, user_message=user_message[:2000],
                                 context="", gathered=gathered_text, decision=decision_text,
                                 web_note=web_note)
    room = max(0, max_chars - len(fixed))
    context = truncate_keep_both_ends(
        (context_view or "").strip(), room, "\n(middle of the grounding omitted)\n")
    return render_prompt("route-a-message.cascade-review", rubric=rubric, user_message=user_message[:2000],
                                context=context or "(none)", gathered=gathered_text,
                                decision=decision_text, web_note=web_note)


def apply_review(cheap: PlanDecision, raw: Optional[Dict[str, Any]]) -> PlanDecision:
    """Fold a reviewer's verdict back onto the cheap decision (the hub's rule, with QAR's own default sentences)."""
    return hub_apply_review(cheap, raw, default_goal=DEFAULT_REVIEW_GOAL, default_question=DEFAULT_REVIEW_QUESTION)


def user_goal_fallback(cheap: PlanDecision) -> str:
    """A goal for a reviewer that corrected the action without writing one (the hub's rule, QAR's default sentence)."""
    return hub_user_goal_fallback(cheap, default_goal=DEFAULT_REVIEW_GOAL)


REVIEW_SPEC = declare(
    "route-a-message.cascade-review", solution="route-a-message", metric="metric-route-a-message",
    tier="caller", cache_layer="none", template=REVIEW_PROMPT)
