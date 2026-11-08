"""A status snapshot, composed by code before the writer, for any turn that asks where things stand (a step of ESOI Application's ensure flow).

Role: a status question ("where are we", "what is my priority", a summary to read out on a call) is answered from one dated snapshot of the quest, not from whatever the
planner happens to read or from older notes. The snapshot holds, each from one read: (1) the quest's key measure read live (``status_reading``); (2) the active priorities: the
quest's open goals that are a keyed next step or are linked to a solution of the quest's plan, ordered by the order the plan's solution records give and then by due day, each
with its due day and its ``Current value:`` line when it holds one; (3) for a question about the asker's own work, the asker's open goals on the quest, soonest due first
(overdue ones lead). Whether a turn is status-shaped is the request analyser's structured report (``asks_status``), never words. Nothing here estimates a value; a part that
cannot be read is left out, and a snapshot with no part at all is None (the turn then goes the ordinary way).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue, status_reading

log = logging.getLogger("quest-ai-runner.status-snapshot")
LOCATOR = "status_snapshot"
PRIORITY_MAX = 5
OWN_MAX = 8
PAGE = 100
HEAD = "STATUS SNAPSHOT READ JUST NOW (every figure carries its own date; nothing older is part of it)"


def reachable_quests(registry: Any) -> List[Dict[str, str]]:
    """The quests the asker can reach, as the placement judge takes them (``{"quest_id", "title", "state"}``), from the lane's ``find_quest`` tool. [] when the lane has none."""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    if registry is None or registry.get("find_quest") is None:
        return []
    try:
        result = registry.invoke("find_quest", {"name": ""}, ToolContext())
    except Exception as error:      # noqa: BLE001
        log.info("quests not listed: %s: %s", type(error).__name__, error)
        return []
    rows = (result.data or []) if result.ok else []
    return [{"quest_id": str(r.get("quest_id") or r.get("id")), "title": str(r.get("outcome") or ""), "state": ""} for r in rows if isinstance(r, Mapping) and (r.get("quest_id") or r.get("id"))]


def goal_rows(registry: Any, quest_id: str, **args: Any) -> List[Mapping[str, Any]]:
    from quest_ai_runner.the_world.tools.tools import ToolContext
    if registry is None or registry.get("list_quest_goals") is None:
        return []
    try:
        result = registry.invoke("list_quest_goals", {"quest_id": quest_id, "limit": PAGE, **args}, ToolContext(quest_id=quest_id))
    except Exception as error:      # noqa: BLE001
        log.info("goals not read: %s: %s", type(error).__name__, error)
        return []
    return [g for g in (result.data or []) if isinstance(g, Mapping)] if result.ok else []


def ladder(tables: Mapping[str, Any]) -> Dict[str, int]:
    """``{solution id: rank}`` in the order the quest's solution records list them (the plan's own priority order)."""
    return {rid: i for i, rid in enumerate((tables.get("solutions") or {}))}


def priorities(registry: Any, quest_id: str, tables: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The open goals that are a keyed next step or are linked to a solution of the plan, in ladder order then soonest due; at most ``PRIORITY_MAX``."""
    from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_keyed_next_step
    rows = goal_rows(registry, quest_id, open_only=True) + goal_rows(registry, quest_id, open_only=True, text_contains="next step")
    seen, kept = set(), []
    for g in rows:
        key = str(g.get("id"))
        if key in seen or g.get("completed") or g.get("abandoned"):
            continue
        seen.add(key)
        if g.get("spec_solution_id") or is_keyed_next_step(g.get("title")):
            kept.append(g)
    rank = ladder(tables)
    kept.sort(key=lambda g: (rank.get(str(g.get("spec_solution_id") or ""), len(rank)), g.get("due") is None, str(g.get("due") or "")))
    return kept[:PRIORITY_MAX]


def goal_line(g: Mapping[str, Any]) -> str:
    value = milestone_cue.value_line_of(str(g.get("text") or ""))
    return f"- {g.get('title')} (due {g.get('due') or 'no date'}" + (f"; {value}" if value else "") + ")"


def build(registry: Any, quest_id: str, asker_user_id: str = "", own: bool = False) -> Optional[Dict[str, Any]]:
    """The snapshot as one gathered read (locator ``status_snapshot``), or None when no part could be read. Never raises."""
    if registry is None or not quest_id:
        return None
    try:
        parts: List[str] = []
        tables: Mapping[str, Any] = {}
        try:
            tables, _ = milestone_cue.read_through(registry, quest_id)
        except milestone_cue.LaneGap:
            tables = {}
        measure = status_reading.observation(registry, quest_id)
        if measure is not None:
            parts.append(measure["text"])
        active = priorities(registry, quest_id, tables)
        if active:
            parts.append("Active priorities, most important first:\n" + "\n".join(goal_line(g) for g in active))
        if own and asker_user_id:
            mine = [g for g in goal_rows(registry, quest_id, open_only=True, assignee_user_id=asker_user_id) if not g.get("completed") and not g.get("abandoned")][:OWN_MAX]
            if mine:
                parts.append("The asker's own open goals on this quest, soonest due first (overdue ones lead):\n" + "\n".join(goal_line(g) for g in mine))
        if not parts:
            return None
        return {"kind": "query", "locator": LOCATOR, "text": HEAD + ":\n\n" + "\n\n".join(parts)}
    except Exception as error:      # noqa: BLE001  a snapshot is an addition; its failure never stops the turn
        log.info("status snapshot not built: %s: %s", type(error).__name__, error)
        return None
