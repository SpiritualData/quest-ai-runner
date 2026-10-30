"""Metric values: the two operations a run uses to draft a value for a metric.

Role: ``get_metric_card`` reads the metric card (what the metric is, its questions and scales, recent values, the period's entries of the quest's
other habits and collections, the quest's notes since the last update); ``propose_metric_value`` raises the draft as an ask for the person. A run
NEVER writes the value itself: the person confirms or corrects the proposal, and only then is an entry written. That is why this module does not
use ``update_outcome_progress``, which writes with no approval.

Each is a :class:`~quest_ai_runner.core.tools.ToolSpec` named after the ``QuestClient`` method it wraps, registered into the standard catalog when
this module loads, like ``tools_quest_ops``.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

from .tools import (STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, data_of, quest_client_from_env,
                    quest_credentials_present, write_failed)
from .tools_status import ToolStatus
from .tools_quest_ops import trim

COLLECTION_PARAM = {"type": "string", "description": "The metric's collection id, named in the task."}
EVIDENCE_PARAM = {
    "type": "array", "description": "The specific things this value rests on: a note, an entry, a goal or a habit, each with what it says.",
    "items": {"type": "object", "required": ["summary"], "properties": {
        "kind": {"type": "string", "enum": ["note", "entry", "goal", "habit"]},
        "summary": {"type": "string", "description": "What it says, at least a few words, never a generic remark."},
        "date": {"type": "string", "description": "YYYY-MM-DD when known."},
        "link": {"type": "string", "description": "Where it can be found, when known."}}}}


def get_metric_card_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        collection = str(args.get("collection_id") or "").strip()
        if not collection:
            return ToolResult(ok=False, text="Needs collection_id, the metric's collection id named in the task.")
        try:
            card = factory().get_metric_card(collection)
        except Exception as e:  # the client's "Not done: <reason>" names what to change
            return ToolResult(ok=False, text=str(e))
        questions = "; ".join(f"{q.get('name')} ({q.get('type')}{_scale(q)})" for q in card.get("questions") or [])
        recent = ", ".join(f"{v.get('value'):g} on {str(v.get('at'))[:10]}" for v in (card.get("recentValues") or []) if v.get("value") is not None)
        return ToolResult(ok=True, data=card, text=(
            f"{card.get('name')}: questions {questions or 'none'}. Recent values: {recent or 'none yet'}. "
            f"{len(card.get('notesSinceBaseline') or [])} quest notes and {len(card.get('otherEntriesInPeriod') or [])} other habits or collections "
            f"with entries since the last update. New context on the quest: {'yes' if card.get('hasNewContext') else 'no'}. "
            f"Outcome: {trim((card.get('outcome') or {}).get('text') or 'none linked', 100)}."))

    return ToolSpec(
        name="get_metric_card",
        handler=handler,
        description=("Read one metric's card: its questions and scales, recent values, the period's entries of the quest's other habits and "
                     "collections, the quest's notes since the last update, and the linked outcome. Read only."),
        when_to_use="You were asked to draft a value for a metric. Read the card first, then the evidence it points to, before proposing.",
        when_not_to_use="Writing a value: a value is proposed with propose_metric_value and confirmed by the person.",
        parameters={"type": "object", "required": ["collection_id"], "properties": {"collection_id": COLLECTION_PARAM}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("metric", "card", "value", "update", "questions", "scale", "measure"),
        context_args={}, timeout_seconds=45.0)


def propose_metric_value_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        collection = str(args.get("collection_id") or "").strip()
        values = args.get("values")
        evidence = args.get("evidence")
        if not collection or not isinstance(values, dict) or not values or not isinstance(evidence, list) or not evidence:
            return ToolResult(ok=False, text="Needs collection_id, values (a value for each question, by question name) and evidence "
                                             "(at least one specific item). If the evidence is thin, propose nothing.")
        try:
            proposal = factory().propose_metric_value(
                collection, values, evidence, confidence=str(args.get("confidence") or "medium"),
                confidence_reason=str(args.get("confidence_reason") or ""), could_not_see=str(args.get("could_not_see") or ""),
                period_start=args.get("period_start"), note=str(args.get("note") or ""))
        except Exception as e:  # a refusal says why: fix the values or the evidence, never retry unchanged
            return write_failed(e, "")
        proposal = data_of(proposal)
        # A proposal is an ask for the person: nothing is recorded until they confirm it.
        return ToolResult(ok=False, status=ToolStatus.HELD, data=proposal, text=(
            f"Proposed. It is an ask for the person ({proposal.get('decisionId')}); nothing is recorded until they confirm. "
            f"Do not report the value as recorded."))

    return ToolSpec(
        name="propose_metric_value",
        handler=handler,
        description=("Propose a value for one metric's period, with the specific evidence behind it. The person confirms or corrects it; "
                     "nothing is written into their collection."),
        when_to_use=("You read the metric card and the quest's own data and the evidence supports a value. Answer every question with a "
                     "value inside its scale, give a confidence (low, medium, high) with the reason, and say what you could not see."),
        when_not_to_use=("The evidence is thin or generic (propose nothing and say why in a quest note), or the metric does not allow the Quest AI Runner."),
        parameters={
            "type": "object", "required": ["collection_id", "values", "evidence"],
            "properties": {
                "collection_id": COLLECTION_PARAM,
                "values": {"type": "object", "description": "The answer to each question, keyed by the question's name."},
                "evidence": EVIDENCE_PARAM,
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "confidence_reason": {"type": "string", "description": "Why, for example 'low: one note since the last update'."},
                "could_not_see": {"type": "string", "description": "What you could not see that would change the value."},
                "period_start": {"type": "string", "description": "YYYY-MM-DD, the period the card names (defaults to the current one)."},
                "note": {"type": "string", "description": "One line for the person, if the evidence needs context."},
            },
        },
        mutates=True, workflow_step=False, origin="standard",
        keywords=("metric", "propose", "proposal", "value", "update", "draft", "measure", "confirm"),
        context_args={}, timeout_seconds=60.0)


def _scale(question: Mapping[str, Any]) -> str:
    low, high = question.get("min"), question.get("max")
    return f", {low:g} to {high:g}" if low is not None and high is not None else ""


def with_credentials(build):
    return lambda env: build(env) if quest_credentials_present(env) else None


#: Registered into the standard catalog (``tools.STANDARD_TOOL_BUILDERS``) when this module loads.
METRIC_VALUE_BUILDERS = {
    "get_metric_card": with_credentials(get_metric_card_spec),
    "propose_metric_value": with_credentials(propose_metric_value_spec),
}
STANDARD_TOOL_BUILDERS.update(METRIC_VALUE_BUILDERS)
