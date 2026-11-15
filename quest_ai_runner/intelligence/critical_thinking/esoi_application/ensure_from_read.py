"""The idempotent write that confirms what a read already shows, found by code (a step of ESOI Application's ensure flow).

Role: an ensure-style order ("make sure the Thursday check-in exists") is answered by a read that finds the thing, and a planner that sees it there answers without calling
the tool whose job is to report "already there" with structured data. The write tool is idempotent, so calling it with what the read found changes nothing and returns that
state (title, schedule, next occurrence) as data the reply is composed from. This module builds that call from the receipts of the turn's reads, never from any model text:
for the workflows of a quest, the one workflow the read listed, with its steps as listed. Nothing else is built (a read that lists several workflows, or none, builds no call).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

READ_TOOL, ENSURE_TOOL = "list_quest_workflows", "create_quest_workflow"


def step_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """One listed step in the shape the create tool takes, with its schedule exactly as listed."""
    step: Dict[str, Any] = {"title": row.get("title") or "", "assignee": row.get("assignee_name") or row.get("assignee_user_id") or "",
                            "starts_at": row.get("starts_at") or "", "ends_at": row.get("ends_at") or ""}
    repeat = row.get("repeat") or {}
    if repeat.get("freq"):
        step["repeat"] = str(repeat["freq"])
        if repeat.get("byWeekday"):
            step["by_weekday"] = list(repeat["byWeekday"])
        if repeat.get("tz"):
            step["tz"] = str(repeat["tz"])
    return step


def call_from_reads(gathered: Iterable[Mapping[str, Any]], registry: Any, quest_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """``{"name", "args"}`` of the ensure call for the one workflow a read of this turn listed, else None."""
    if registry is None or registry.get(ENSURE_TOOL) is None or not quest_id:
        return None
    rows = [r for g in gathered if isinstance(g, Mapping) and g.get("locator") == f"tool:{READ_TOOL}" and isinstance(g.get("tool_data"), list)
            for r in g["tool_data"] if isinstance(r, Mapping)]
    titles = {r.get("workflow_id"): r.get("workflow_title") for r in rows if r.get("workflow_id")}
    if len(titles) != 1:
        return None
    workflow_id, title = next(iter(titles.items()))
    steps = [step_from_row(r) for r in rows if r.get("workflow_id") == workflow_id]
    return {"name": ENSURE_TOOL, "args": {"quest_id": quest_id, "title": title, "steps": steps}} if title and steps else None
