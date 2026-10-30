"""Metric drafts: one bounded light turn, never a deep run.

Role: a task with ``source == "metric_values"`` asks for a draft value of one metric. The work is small and fully described by data the runner
can assemble up front: the metric card (questions, scales, recent values, the period's entries of the quest's other habits and collections, the
quest's notes, goals and answered Asks since the last update) and the quest's normal context view. So it never opens a deep run (no ``claude``
subprocess, no shell, no files): the card is read with ``get_metric_card``, ONE structured model call decides, and a proposal is raised with
``propose_metric_value``. Thin evidence proposes nothing. The person confirms any proposal; nothing is written into their collection here.

This matters most for a person who connected their own environment: a deep run would spend their compute on a few hundred tokens of reading.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional

from hubofhubs.ports.model import STEP_SUMMARIZE, plan_with_step
from ..core.prompt_specs import PromptSpec, register, render_prompt
from ..core.tools import ToolContext
from ..core.tools_metric_values import get_metric_card_spec, propose_metric_value_spec

SOURCE = "metric_values"
#: One model call decides; the card read and the proposal are plain tool calls around it.
MAX_MODEL_CALLS = 1
CONTEXT_CHARS = 6000
COLLECTION_RE = re.compile(r"metric collection\s+([A-Za-z0-9_\-]+)")

DRAFT_TOOL: Dict[str, Any] = {
    "name": "metric_value_draft",
    "description": ("Decide whether the quest's own data supports a value for this metric. If it does, give a value for each question with "
                    "the specific evidence behind it. If the evidence is thin, set propose to false and give no values."),
    "input_schema": {
        "type": "object",
        "required": ["propose"],
        "properties": {
            "propose": {"type": "boolean", "description": "False when the evidence is thin: nothing is proposed."},
            "reason": {"type": "string", "description": "When propose is false, why the evidence is thin, in one sentence."},
            "values": {"type": "object", "description": "A value for each question, keyed by the question's name, within its scale."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "confidence_reason": {"type": "string"},
            "could_not_see": {"type": "string", "description": "What would have made this more certain and was not in the data."},
            "evidence": {"type": "array", "description": "The specific things the values rest on.", "items": {
                "type": "object", "required": ["summary"], "properties": {
                    "kind": {"type": "string", "enum": ["note", "entry", "goal", "habit"]},
                    "summary": {"type": "string", "description": "What it says, at least a few words."},
                    "date": {"type": "string"}, "link": {"type": "string"}}}},
        },
    },
}


def is_metric_draft(task: Mapping[str, Any]) -> bool:
    return str(task.get("source") or "") == SOURCE


def collection_id_of(task: Mapping[str, Any]) -> Optional[str]:
    """The metric's collection id: a structured field when the task has one, else the id the draft text names."""
    explicit = str(task.get("metric_collection_id") or "").strip()
    if explicit:
        return explicit
    found = COLLECTION_RE.search(str(task.get("text") or task.get("description") or task.get("title") or ""))
    return found.group(1) if found else None


def card_block(card: Mapping[str, Any]) -> str:
    """The card as the prompt reads it: compact JSON of exactly what the draft may rely on.

    ``purpose`` is what the metric ensures and ``disadvantages`` what it cannot see or could reward wrongly."""
    keep = ("name", "purpose", "disadvantages", "questions", "period", "recentValues", "outcome", "notesSinceBaseline", "otherEntriesInPeriod",
            "goalsSinceBaseline", "answeredAsksSinceBaseline", "baseline")
    return json.dumps({k: card.get(k) for k in keep if card.get(k) not in (None, [], {})}, ensure_ascii=False, default=str)[:CONTEXT_CHARS]


DRAFT_SPEC = register(PromptSpec(
    id="draft-a-metric.value", solution="draft-a-metric", metric="metric-draft-a-metric",
    tier="caller", cache_layer="none", inputs=("task_text", "card", "context"),
    output_schema=DRAFT_TOOL,
    template=(
        "You draft one value for a person's metric from their quest's own data. You have no tools and no way to look anything else up.\n\n"
        "The task: {task_text}\n\n"
        "The metric card (what the metric is, and what changed since the last update):\n{card}\n\n"
        "The quest context:\n{context}\n\n"
        "Rules: use only evidence that is in the card or the quest context above. Give a value for every question, within its scale, keyed by the "
        "question's name. The card's purpose says what the metric ensures and its disadvantages say what it cannot see: do not claim more than it "
        "can show, and name in could_not_see anything its disadvantages warn about that the data leaves open. Cite the specific things each value rests on. If the data does not support a value, set propose to false and say why "
        "in one sentence: a missing value is better than an invented one. Never write a value as if it were recorded: the person confirms it.\n\n"
        "Answer with one JSON object only: {{\"propose\": true|false, \"reason\": \"<one sentence, only when propose is false>\", "
        "\"values\": {{\"<question name>\": <value>}}, \"confidence\": \"low|medium|high\", \"confidence_reason\": \"<short>\", "
        "\"could_not_see\": \"<short>\", \"evidence\": [{{\"kind\": \"note|entry|goal|habit\", \"summary\": \"<what it says, a few words at least>\", "
        "\"date\": \"<date>\"}}]}}"),
))


def parse_draft(raw: Any) -> Optional[Dict[str, Any]]:
    """The model's structured answer, or None when it proposes nothing (thin evidence, no values, no evidence, or an unreadable answer)."""
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        raw = raw[0]
    if not isinstance(raw, dict) or raw.get("propose") is not True:
        return None
    values, evidence = raw.get("values"), raw.get("evidence")
    if not isinstance(values, dict) or not values or not isinstance(evidence, list):
        return None
    cited = [e for e in evidence if isinstance(e, dict) and len(str(e.get("summary") or "").split()) >= 3]
    return {**raw, "evidence": cited} if cited else None


def reason_of(raw: Any) -> str:
    if isinstance(raw, dict) and str(raw.get("reason") or "").strip():
        return " ".join(str(raw["reason"]).split())[:300]
    return "the quest's data did not support a value"


class MetricDraft:
    """One light turn for one claimed task. ``client`` is the Quest client the executor already holds; ``provider`` and ``model`` make the call."""

    def __init__(self, client: Any, provider: Any, model: str, env: Optional[Mapping[str, str]] = None):
        self.client, self.provider, self.model = client, provider, model
        self.env = env or {}
        self.model_calls = 0

    def run(self, task: Mapping[str, Any], task_id: str, quest_id: Optional[str], context_view: str) -> Dict[str, Any]:
        """Returns ``{"status": "proposed" | "nothing" | "failed", "text": ..., "decision_id": ...}``. A provider error propagates so the executor
        can treat a usage limit or an outage as a wait, exactly as it does for a deep run."""
        collection = collection_id_of(task)
        if not collection:
            return {"status": "failed", "text": "the task did not name a metric collection"}
        ctx = ToolContext(quest_id=quest_id, task_id=task_id)
        read = get_metric_card_spec(self.env, client_factory=lambda: self.client).handler({"collection_id": collection}, ctx)
        if not read.ok:
            return {"status": "failed", "text": read.text}
        card = read.data if isinstance(read.data, dict) else {}
        raw = self.decide(str(task.get("text") or task.get("description") or ""), card, context_view)
        draft = parse_draft(raw)
        if draft is None:
            return {"status": "nothing", "text": f"No value proposed: {reason_of(raw)}."}
        sent = propose_metric_value_spec(self.env, client_factory=lambda: self.client).handler({
            "collection_id": collection, "values": draft["values"], "evidence": draft["evidence"],
            "confidence": draft.get("confidence") or "medium", "confidence_reason": draft.get("confidence_reason") or "",
            "could_not_see": draft.get("could_not_see") or "", "period_start": (card.get("period") or {}).get("start")}, ctx)
        if not sent.ok:
            return {"status": "failed", "text": sent.text}
        decision = (sent.data or {}).get("decisionId") if isinstance(sent.data, dict) else None
        return {"status": "proposed", "text": f"Proposed a value for the person to confirm ({decision}).", "decision_id": decision}

    def decide(self, task_text: str, card: Mapping[str, Any], context_view: str) -> Any:
        if self.model_calls >= MAX_MODEL_CALLS:
            return None
        self.model_calls += 1
        return plan_with_step(self.provider, render_prompt("draft-a-metric.value", task_text=task_text, card=card_block(card),
                                                           context=(context_view or "")[:CONTEXT_CHARS]), step=STEP_SUMMARIZE, model=self.model,
                              tool_schema=DRAFT_TOOL)
