"""The quest's key measure, read live for a question about where the quest stands (a step of ESOI Application's ensure flow).

Role: a status question is answered from the value the quest's outcome is measured by, not from a sync note that may be weeks old. The quest's top solution (the one no
other solution lists as a part of it) names its metric; when that metric's record names a ``source``, the lane's ``read_metric_source`` tool reads it (the same tool and
the same helpers the milestone flow uses) and the reading enters the turn as one read: value, the day it covers, the source label and the source's detail. A quest whose top
metric names no source, a lane without the tool, or a source that cannot be read adds nothing; nothing here estimates a value.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.tools_metric_source import shown

log = logging.getLogger("quest-ai-runner.status-reading")
LOCATOR = "tool:" + milestone_cue.READ_TOOL


def top_solution_ids(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> List[str]:
    """The solutions no other solution lists as one of its parts, in the quest's own order."""
    solutions = tables.get("solutions") or {}
    parts = {str(s) for row in solutions.values() for s in (row.get("solution_ids") or [])}
    return [rid for rid in solutions if rid not in parts]


def top_metric_source(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> Tuple[str, str]:
    """``(the metric's name, the name of its source)`` for the quest's top metric: the top solution's authoritative metric, else its first metric, that names a source. ``("", "")`` when none does."""
    for rid in top_solution_ids(tables):
        row = (tables.get("solutions") or {}).get(rid) or {}
        metrics = milestone_cue.metrics_of(tables, rid)
        first = str(row.get("authoritative_metric_id") or "")
        metrics.sort(key=lambda pair: pair[0] != first)
        for _, metric in metrics:
            if str(metric.get("source") or "").strip():
                return str(metric.get("name") or ""), str(metric["source"]).strip()
    return "", ""


def reading_text(metric: str, reading: Mapping[str, Any]) -> str:
    detail = reading.get("detail")
    return (f"KEY MEASURE READ LIVE JUST NOW: {metric}: {shown(reading['value'])} (as of {reading['as_of']}, from {reading.get('source') or 'its source'})."
            + (f" Detail of that reading: {json.dumps(detail, sort_keys=True)}." if detail else "")
            + " A reply about where the quest stands opens with this value and this date.")


def observation(registry: Any, quest_id: str) -> Optional[Dict[str, Any]]:
    """The gathered read for the quest's key measure, or None when the quest names no source for it or it cannot be read now. Never raises."""
    if registry is None or not quest_id or registry.get(milestone_cue.READ_TOOL) is None:
        return None
    try:
        tables, _ = milestone_cue.read_through(registry, quest_id)
        metric, source = top_metric_source(tables)
        if not source:
            return None
        from quest_ai_runner.the_world.tools.tools import ToolContext
        result = registry.invoke(milestone_cue.READ_TOOL, {"name": source}, ToolContext(quest_id=quest_id))
        if not (result.ok and isinstance(result.data, Mapping) and result.data.get("as_of") and "value" in result.data):
            log.info("key measure not read: %s", str(getattr(result, "text", ""))[:200])
            return None
        # ``intake``: handed to the turn by code before the planner's first step. It is one measure, not a read the planner chose, so the records gate does not count it as the records read.
        return {"kind": "query", "locator": LOCATOR, "text": reading_text(metric, result.data), "by_code": True, "intake": True}
    except Exception as error:      # noqa: BLE001  a status reading is an addition; its failure never stops the turn
        log.info("key measure not read: %s: %s", type(error).__name__, error)
        return None
