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

from .adapters import PlanDecision

#: The confidence values a planner may report, weakest first.
CONFIDENCE_LEVELS = ("low", "medium", "high")

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


#: The actions an operator may name in ``planner_cascade_escalate_on`` as ``action:<name>``.
ESCALATABLE_ACTIONS = ("read", "answer", "deep", "confirm", "clarify", "tool")


def escalation_spec(spec: Optional[str]) -> Dict[str, FrozenSet[str]]:
    """Parse ``planner_cascade_escalate_on`` into the two signals it may name.

    Entries are a comma list of confidence levels ("low", "medium") and of actions written
    ``action:read``. An unrecognised entry is dropped rather than raising: a typo in an operator's
    environment variable should narrow what escalates, never break planning.

    WHY THERE ARE TWO SIGNALS, and why the ACTION one is the default. Self-reported confidence is
    the obvious signal and it is the one that did not work. Measured 2026-10-05 on 86 labelled
    hand-off cases: with the field required, gemini-2.5-flash-lite answered "high" on 84 of 86
    decisions, including 36 it got wrong, so confidence gated nothing (57 percent of its "high"
    decisions were correct, against 100 percent of its two "medium" ones, on an n of 2). A model
    that cannot tell a reachable request from an unreachable one also cannot tell that it cannot
    tell. The ACTION the planner chose is not a self-report, it is the decision itself, and it
    separates the error class cleanly: nearly every miss in that group is a "read" issued for work
    no read can reach, while a wrongly skipped read is rare. So the cheap, honest signal is
    structural. Confidence is kept because it costs one enum on a call already being made and
    might be calibrated on another model, but it is not what the default relies on.
    """
    levels: set = set()
    actions: set = set()
    for part in (spec or "").split(","):
        entry = part.strip().lower()
        if not entry:
            continue
        if entry.startswith("action:"):
            name = entry.split(":", 1)[1].strip()
            if name in ESCALATABLE_ACTIONS:
                actions.add(name)
        elif entry in CONFIDENCE_LEVELS:
            levels.add(entry)
    return {"levels": frozenset(levels), "actions": frozenset(actions)}


def escalation_levels(spec: Optional[str]) -> FrozenSet[str]:
    """The confidence levels named in ``spec``. Kept as its own helper for readability."""
    return escalation_spec(spec)["levels"]


def should_escalate(decision: PlanDecision, spec: Any) -> bool:
    """True when this decision matches a signal the operator asked to have reviewed.

    ``spec`` is either the dict from ``escalation_spec`` or a bare set of confidence levels.
    No reported confidence means NO confidence-based escalation. That is the fail-safe that keeps
    a model which ignores the field, or a response that failed to parse, from escalating
    everything. The action signal has no such hole: a decision always has an action.
    """
    if isinstance(spec, dict):
        levels = spec.get("levels") or frozenset()
        actions = spec.get("actions") or frozenset()
    else:
        levels, actions = (spec or frozenset()), frozenset()
    if decision.action in actions:
        return True
    if not levels:
        return False
    return bool(decision.confidence) and decision.confidence in levels


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
    fixed = REVIEW_PROMPT.format(rubric=rubric, user_message=user_message[:2000],
                                 context="", gathered=gathered_text, decision=decision_text,
                                 web_note=web_note)
    room = max(0, max_chars - len(fixed))
    context = truncate_keep_both_ends(
        (context_view or "").strip(), room, "\n(middle of the grounding omitted)\n")
    return REVIEW_PROMPT.format(rubric=rubric, user_message=user_message[:2000],
                                context=context or "(none)", gathered=gathered_text,
                                decision=decision_text, web_note=web_note)


def truncate_keep_both_ends(text: str, limit: int, marker: str = "\n...\n") -> str:
    """``text`` cut to ``limit`` characters by removing the MIDDLE, not either end.

    Both ends of a grounding block carry decision-relevant content (what exists at the top, what
    is reachable at the bottom), and a cut that silently drops one of them is worse than a
    smaller budget honestly spent.
    """
    if limit <= 0 or len(text) <= limit:
        return text if len(text) <= max(limit, 0) else ""
    room = limit - len(marker)
    if room <= 0:
        return text[:limit]
    head = room // 2
    return text[:head].rstrip() + marker + text[len(text) - (room - head):].lstrip()


def apply_review(cheap: PlanDecision, raw: Optional[Dict[str, Any]]) -> PlanDecision:
    """Fold a reviewer's verdict back onto the cheap decision.

    A reviewer that agrees, says nothing usable, or fails outright leaves the cheap decision
    EXACTLY as it was, reads included. Only a genuine correction replaces it, and even then the
    fields the reviewer did not speak to (the model tier, the deep difficulty rating, the target)
    are carried over rather than dropped.
    """
    if not isinstance(raw, dict):
        return cheap
    action = (raw.get("action") or "").strip().lower()
    if action not in ("read", "answer", "deep", "confirm"):
        return cheap
    rationale = (raw.get("rationale") or "").strip() or cheap.rationale
    if action == cheap.action and not (action == "answer" and bool(raw.get("hand_off"))
                                      and not cheap.deferred_deep):
        return cheap
    if action == "read":
        # The reviewer wants a read and the cheap model did not plan one, so there are no reads to
        # run. Answering is the honest fallback rather than an empty read step.
        if not cheap.reads:
            return PlanDecision(action="answer", rationale=rationale,
                                model_tier=cheap.model_tier, confidence=cheap.confidence)
        return PlanDecision(action="read", reads=cheap.reads, rationale=rationale,
                            model_tier=cheap.model_tier, confidence=cheap.confidence)
    goal = (raw.get("goal") or cheap.goal
            or (cheap.deferred_deep or {}).get("goal") or user_goal_fallback(cheap))
    brief = raw.get("deep_brief") or cheap.deep_brief or (cheap.deferred_deep or {}).get("brief")
    if action == "deep":
        return PlanDecision(action="deep", goal=goal, deep_brief=brief, rationale=rationale,
                            model_tier=cheap.model_tier, deep_difficulty=cheap.deep_difficulty,
                            deep_difficulty_reason=cheap.deep_difficulty_reason,
                            deep_target=cheap.deep_target, confidence=cheap.confidence)
    if action == "answer":
        deferred = None
        if raw.get("hand_off"):
            deferred = {"goal": goal, "brief": brief, "rationale": rationale}
        return PlanDecision(action="answer", rationale=rationale, deferred_deep=deferred,
                            model_tier=cheap.model_tier, deep_difficulty=cheap.deep_difficulty,
                            deep_difficulty_reason=cheap.deep_difficulty_reason,
                            deep_target=cheap.deep_target, confidence=cheap.confidence)
    question = (raw.get("confirm_question") or cheap.confirm_question
                or "Which of these did you mean?")
    return PlanDecision(action="confirm", confirm_question=question, rationale=rationale,
                        model_tier=cheap.model_tier, confidence=cheap.confidence)


def user_goal_fallback(cheap: PlanDecision) -> str:
    """A goal for a reviewer that corrected the action without writing one.

    Never empty: a deep decision with no done-standard is rejected downstream, so a correction
    that would produce one is worse than no correction at all.
    """
    return (cheap.goal or cheap.deep_brief
            or (cheap.deferred_deep or {}).get("goal")
            or "Carry out what the request asked for.")
