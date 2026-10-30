"""Value objects the brain passes around: events, observations, plan decisions, deep results, escalations, deadlines.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Value objects passed across the boundary (plain data, no behavior).
# ---------------------------------------------------------------------------

from hubofhubs.ports.retrieval import Observation  # noqa: F401  (moved to the hub; same class, old import path)


@dataclass
class PlanDecision:
    """The planner's structured next-step decision (one iteration of the loop)."""
    action: str                                   # "read" | "answer" | "deep" | "confirm" | "clarify" | "tool"
    reads: List[Dict[str, Any]] = field(default_factory=list)
    goal: Optional[str] = None
    deep_brief: Optional[str] = None
    confirm_question: Optional[str] = None
    model_tier: Optional[str] = None              # "haiku" | "sonnet" | "opus" | None
    # The planner's rating of DEEP work, judged on the same planning call (zero extra LLM calls):
    # "simple" | "normal" | "hard" | None. Picks the deep run's STARTING model when nothing pinned
    # one (see core/deep_model_selection.py). None = not assessed; the ladder is then unchanged.
    deep_difficulty: Optional[str] = None
    deep_difficulty_reason: Optional[str] = None  # one short clause, logged with the choice
    # WHAT a "deep" (or deferred_deep) change lands on, judged on the same planning call (zero
    # extra LLM calls): "quest_data" | "code_or_files" | "other" | None. "quest_data" gates the
    # deep-runner ladder (see ladder_for_quest_data in core/orchestrator.py) so a quest-data
    # change never lands on a file-editing rung. None = not assessed; the ladder is unchanged.
    deep_target: Optional[str] = None
    subquestions: List[str] = field(default_factory=list)
    # Flat task dicts; with OrchestratorConfig.staged_plans also lists (sequences) of dicts or of
    # lists of dicts. See core/orchestrator_staged_plan.py.
    deep_subtasks: List[Any] = field(default_factory=list)
    rationale: str = ""
    # Optional deferred deep work: when action="answer", can also queue a deep task to run after
    deferred_deep: Optional[Dict[str, Any]] = None  # {"goal": "...", "brief": "...", "rationale": "..."}
    # Explicit signal: this answer contains work that needs execution (instead of regex guessing)
    answer_contains_work_to_execute: bool = False  # Set True if answer describes work the AI should do
    # The planner chose "answer" for a message that asks it to ACT because what it read shows the act
    # cannot or should not go ahead as asked (a stored constraint, a conflict); the reply says so and
    # gives the nearest option that works. That reply IS the complete response, so the escalation nets
    # that turn an answer to an "act" message into a deep run must not fire on it.
    act_declined: bool = False
    # User clarification/input needed: when action="clarify"
    clarification: Optional[Dict[str, Any]] = None  # {"question": "...", "options": [...], "allow_free_input": bool}
    # EXPLICIT execution-mode signal the planner detected in the user's message (LLM judgment on
    # the planning call it already makes; never phrase matching). "enter_brainstorm" |
    # "exit_brainstorm" | None. Anything else the model returns is normalized to None (fail-safe:
    # a detection failure never changes the mode).
    mode_signal: Optional[str] = None
    # PER-IDEA THREADING (opt-in, OrchestratorConfig.card_thread_enabled): which TOPIC CARD this
    # message belongs to, judged on the SAME planning call (zero extra LLM calls; see
    # core/card_thread.py). Raw field, exactly one of "continue" | "switch_to:<card_id>" |
    # "new:<label>"; anything else is normalized to None and the turn CONTINUES the active card
    # (the standing fail-safe: an ambiguous topic assignment must never cost a turn). A topic
    # switch is NOT a mode signal and never touches the brainstorm latch.
    card_thread: Optional[str] = None
    # The escalation this confirm/clarify raises, all optional and all defaulting to the same
    # behavior as before they existed: a decision KIND (Quest's ``^[a-z0-9_:-]{1,64}$``, e.g.
    # "approve", "explicit:spend", "notice:prod-ops" -- validated/defaulted again at the Quest
    # boundary in ``QuestClient.create_decision``, so a hallucinated value here degrades rather
    # than losing the escalation); a deadline the decision should be resolved by, as the planner
    # wrote it (an ISO datetime, or a short relative form like "in 48h" -- parsed by
    # ``core.adapters_values.parse_deadline``, unparseable or absent means no deadline); and
    # "hold" | "proceed" for what happens if nobody answers by the deadline ("proceed" auto-
    # resolves the decision server-side; anything else, including no deadline at all, "hold"s).
    confirm_kind: Optional[str] = None
    confirm_deadline: Optional[str] = None
    # OPTIONAL http(s) link to the thing the human should look at to answer (see Escalation.review_url).
    confirm_review_url: Optional[str] = None
    confirm_default_on_silence: Optional[str] = None
    # action="tool": the direct tool calls to run in-process, in order, each
    # ``{"name": "<tool>", "args": {...}}`` (see core/tools.py). Only ever populated when the
    # orchestrator was given a tool registry; otherwise "tool" is not an action the planner has.
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    # HOW SURE the planner is of THIS decision, judged on the same planning call (zero extra LLM
    # calls): "high" | "medium" | "low" | None. Present only when the consumer opted into the
    # cascade (OrchestratorConfig.planner_cascade), because a field nobody reads is input tokens
    # on every call for nothing. It is a STRUCTURED self-report the planner fills in, which is why
    # it is an acceptable escalation signal where scanning the planner's own prose would not be
    # (see CLAUDE.md hard rule #3). None = not assessed, and the cascade then leaves the decision
    # alone rather than escalating everything.
    confidence: Optional[str] = None
    # WHAT THE USER'S CURRENT MESSAGE ASKS OF THE ASSISTANT, judged on the same planning call (zero
    # extra LLM calls): "act" (do, produce or change something now) | "ask" (a question, or asking
    # to be told something) | "inform" (news, context, or the user's own plan) | "hold_off" (do
    # not open work, answer here, stop or cancel runs). The orchestrator's escalation nets honor
    # this structured verdict instead of reading the message with a keyword list. None = not given
    # (the nets then fall back to the one-shot intent judge, never to a regex).
    user_intent: Optional[str] = None
    # THE READS THIS DECISION STILL OWES, declared by the planner on the call it already makes (zero
    # extra LLM calls): read specs (same shape as ``reads``) for sources it has located but not
    # opened, which the answer it is giving depends on. Empty = the answer is complete. When an
    # ``answer`` decision carries any, the loop runs them first instead of answering (the planner's
    # own structured declaration, never a reading of its prose; see the pending-read gate in
    # ``orchestrator_run_loop``).
    pending_read: List[Dict[str, Any]] = field(default_factory=list)
    # THE FACTS THIS DECISION RESTS ON, declared by the planner on the call it already makes (zero extra
    # LLM calls): each fact in CONTEXT and GATHERED that bears on the request, as a short quote with its
    # source. The planner is the one reader that sees the newest reads in full beside the request, so what it
    # lists is carried on: appended to a deep brief (a change must respect a stored constraint the code
    # writer never read) and shown to the answer (which reads every gathered page of a long turn).
    facts_read: List[str] = field(default_factory=list)


@dataclass
class Escalation:
    """A human-only step the brain refuses to take autonomously."""
    summary: str
    kind: str = "approve"
    quest_id: Optional[str] = None
    assignee: Optional[str] = None              # consumer-defined routing key
    default_on_silence: str = "hold"
    # WHEN this decision should be resolved by, an aware datetime or None for no deadline. A
    # sink that supports deadlines (``QuestDecisionSink``) applies its own configured default
    # (``QAR_DECISION_DEFAULT_DEADLINE_HOURS``) when this is left None -- callers that never
    # think about deadlines still get one if the deployment wants that; an explicit value here
    # always wins.
    deadline: Optional[datetime] = None
    # OPTIONAL http(s) page the human should open to decide: the draft, preview, doc or PR the ask
    # is about. Shown as a prominent link on the ask in Quest's daily reflection and Asks for you.
    review_url: Optional[str] = None


# A short relative form the planner can emit instead of computing a real timestamp itself:
# "in 48h", "in 2 days", "in 30m". Kept intentionally small (hours/days/minutes) -- a deadline
# on a human decision is never usefully more precise than that.
RELATIVE_DEADLINE_RE = re.compile(
    r"^in\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|days?|d|minutes?|mins?|m)$",
    re.IGNORECASE,
)


def parse_deadline(raw: Optional[str], *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a planner-supplied deadline string into an aware UTC datetime, or None.

    Accepts either a full ISO datetime (``2026-09-25T18:00:00Z`` or with a numeric UTC offset) or
    the short relative form above. Anything else -- missing, empty, unparseable -- returns None,
    exactly as if no deadline had been given: a malformed deadline must degrade to "no deadline",
    never raise into the escalation path.
    """
    text = (raw or "").strip()
    if not text:
        return None
    m = RELATIVE_DEADLINE_RE.match(text)
    if m:
        amount = float(m.group(1))
        unit = m.group(2).lower()
        base = now if now is not None else datetime.now(timezone.utc)
        if unit.startswith("h"):
            return base + timedelta(hours=amount)
        if unit.startswith("d"):
            return base + timedelta(days=amount)
        return base + timedelta(minutes=amount)
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
