"""What a goal write needs beyond a title and a period: an owner, and the links to the quest's specification.

Role: shared by the ``create_quest_goal`` tool (plain creation) and the ``create_or_update_quest_goal`` tool (ensure one goal by exact title).
It turns a person's name into a member of the quest, builds the optional link arguments the Quest client sends, and carries out the
ensure-one rule: look for a goal with exactly that title on the quest, create it when absent, change only what differs when present, and say which
of created, updated or unchanged happened. It imports no tool registry, so both tool modules can use it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

#: The optional arguments that link a goal to the quest's specification, and the schema the planner sees for them.
LINK_KEYS = ("spec_solution_id", "spec_metric_id", "metric_target_value")
LINK_PARAMS: Dict[str, Any] = {
    "assignee": {"type": "string",
                 "description": ("Who owns the goal: a member of this quest by name (or part of it), email or user id. "
                                 "A name that matches nobody, or more than one person, creates nothing and lists the members.")},
    "spec_solution_id": {"type": "string",
                         "description": ("The quest strategy (specification solution record id, esr_...) this goal serves, "
                                         "from get_spec or read_spec_records. Quest refuses an id that is not on the quest.")},
    "spec_metric_id": {"type": "string",
                       "description": "The metric record id (esr_...) of that strategy which this goal moves; a milestone only."},
    "metric_target_value": {"type": "number",
                            "description": "The metric's target value by this goal's date; needs spec_metric_id."},
}
#: Fields the ensure form may change on a goal that already exists (each is sent to ``PUT /api/planning/goals/{id}``).
UPDATABLE = ("description", "criteria", "period", "target_value", "target_unit", *LINK_KEYS)


def link_fields(args: Mapping[str, Any]) -> Dict[str, Any]:
    """The optional goal fields the person-facing arguments name, with empty ones dropped."""
    names = ("description", "criteria", "target_value", "target_unit", *LINK_KEYS)
    return {k: args[k] for k in names if args.get(k) not in (None, "")}


def resolve_assignee(client: Any, quest_id: str, who: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(user_id, None)`` for a member of the quest named by id, email or name, else ``(None, why)`` listing who is on it."""
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import resolve_member
    members = client.list_assignable_members(quest_id) or []
    if not members:
        return None, f"the members of quest {quest_id} could not be read, so {who!r} could not be matched to a person"
    user_id, why = resolve_member(who, members)
    return user_id, ("assignee: " + why) if why else None


def existing_goals(client: Any, quest_id: str, title: str) -> Optional[List[Dict[str, Any]]]:
    """The goals on the quest titled exactly ``title`` and not abandoned; ``None`` when the quest's goals could not be read (so nothing may be assumed)."""
    listing = client.list_quest_goals(quest_id)
    groups = listing.get("period_groups") if isinstance(listing, dict) else None
    if groups is None:
        return None
    return [g for group in groups for g in group.get("goals") or []
            if str(g.get("name") or "").strip() == title and not g.get("abandoned")]


def goal_changes(goal: Mapping[str, Any], wanted: Mapping[str, Any]) -> Dict[str, Any]:
    """The wanted fields that differ from the goal as the quest lists it. A field the listing does not carry counts as different (sending it is safe)."""
    return {k: v for k, v in wanted.items() if k in UPDATABLE and goal.get(k) != v}


def ensure_goal(client: Any, quest_id: str, title: str, period: str, fields: Mapping[str, Any],
                assignee_id: Optional[str]) -> Dict[str, Any]:
    """Make exactly one goal titled ``title`` exist on the quest with these fields. Returns ``{state, goal_id, changed, writes, copies}`` where ``state`` is
    ``created``, ``updated``, ``unchanged`` or ``unknown`` (the quest's goals could not be read, nothing was written). ``writes`` are the client results,
    for the caller to read a held or refused one from. A client that raises on a refused write propagates; the caller reports it."""
    found = existing_goals(client, quest_id, title)
    if found is None:
        return {"state": "unknown", "goal_id": None, "changed": [], "writes": [], "copies": 0}
    if not found:
        write = client.create_goal(title, period=period, quest_id=quest_id, assigned_to_user_id=assignee_id, **fields)
        goal_id = getattr(write, "id", "") or (getattr(write, "data", None) or {}).get("id")
        return {"state": "created", "goal_id": goal_id, "changed": [], "writes": [write], "copies": 0}
    goal = found[0]
    goal_id = str(goal.get("id"))
    changes = goal_changes(goal, {**fields, "period": period})
    writes: List[Any] = []
    changed = sorted(changes)
    if changes:
        writes.append(client.update_goal(goal_id, changes))
    if assignee_id and goal.get("assigned_to_user_id") != assignee_id:
        writes.append(client.assign_goal(goal_id, assignee_id))
        changed.append("assignee")
    return {"state": "updated" if writes else "unchanged", "goal_id": goal_id, "changed": changed, "writes": writes,
            "copies": len(found) - 1}
