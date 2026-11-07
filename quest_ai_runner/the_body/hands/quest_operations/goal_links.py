"""What a goal write needs beyond a title and a period: an owner, and the links to the quest's specification.

Role: shared by the ``create_quest_goal`` tool (plain creation) and the ``create_or_update_quest_goal`` tool (ensure one goal by exact title).
It turns a person's name into a member of the quest, builds the optional link arguments the Quest client sends, and carries out the
ensure-one rule: look for a goal with exactly that title on the quest, create it when absent, change only what differs when present, and say which
of created, updated or unchanged happened. It imports no tool registry, so both tool modules can use it.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: The optional arguments that link a goal to the quest's specification, and the schema the planner sees for them.
LINK_KEYS = ("spec_solution_id", "spec_metric_id", "metric_target_value")
LINK_PARAMS: Dict[str, Any] = {
    "assignee": {"type": "string",
                 "description": ("Who owns the goal: a member of this quest by name (or part of it), email or user id. "
                                 "A name that matches nobody, or more than one person, creates nothing and lists the members.")},
    "due_date": {"type": "string", "description": "The day it is due, YYYY-MM-DD (inside the period); optional."},
    "spec_solution_id": {"type": "string",
                         "description": ("The quest strategy (specification solution record id, esr_...) this goal serves, "
                                         "from get_spec or read_spec_records. Quest refuses an id that is not on the quest.")},
    "spec_metric_id": {"type": "string",
                       "description": "The metric record id (esr_...) of that strategy which this goal moves; a milestone only."},
    "metric_target_value": {"type": "number",
                            "description": "The metric's target value by this goal's date; needs spec_metric_id."},
}
#: Fields the ensure form may change on a goal that already exists (each is sent to ``PUT /api/planning/goals/{id}``).
UPDATABLE = ("description", "criteria", "period", "target_value", "target_unit", "scheduled_date", *LINK_KEYS)


def normalize_period(value: Any, today: Optional[datetime.date] = None) -> str:
    """A period in Quest's formats. A bare scope word ("week", "this week", "day", "today", "month", "quarter", "year") or nothing becomes the current
    one; a day or any valid period passes through unchanged. Empty input means this week: a next step is a week's goal."""
    day = today or datetime.date.today()
    text = str(value or "").strip().lower().replace("_", " ")
    iso = day.isocalendar()
    scopes = {"": f"{iso[0]}_W{iso[1]:02d}", "week": f"{iso[0]}_W{iso[1]:02d}", "this week": f"{iso[0]}_W{iso[1]:02d}",
              "day": day.isoformat(), "today": day.isoformat(), "month": f"{day.year}_{day.month:02d}", "this month": f"{day.year}_{day.month:02d}",
              "quarter": f"{day.year}_Q{(day.month - 1) // 3 + 1}", "year": str(day.year)}
    return scopes.get(text, str(value).strip())


def link_fields(args: Mapping[str, Any]) -> Dict[str, Any]:
    """The optional goal fields the person-facing arguments name, with empty ones dropped."""
    names = ("description", "criteria", "target_value", "target_unit", *LINK_KEYS)
    out = {k: args[k] for k in names if args.get(k) not in (None, "")}
    if args.get("due_date"):
        out["scheduled_date"] = str(args["due_date"]).strip()
    return out


def resolve_assignee(client: Any, quest_id: str, who: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(user_id, None)`` for a member of the quest named by id, email or name, else ``(None, why)`` listing who is on it."""
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import resolve_member
    members = client.list_assignable_members(quest_id) or []
    if not members:
        return None, f"the members of quest {quest_id} could not be read, so {who!r} could not be matched to a person"
    user_id, why = resolve_member(who, members)
    return user_id, ("assignee: " + why) if why else None


def all_goals(client: Any, quest_id: str) -> Optional[List[Dict[str, Any]]]:
    """Every goal on the quest as the listing carries it (with its ``spec_solution_id``, ``spec_metric_id`` and assignee); ``None`` when the goals could not be read."""
    listing = client.list_quest_goals(quest_id)
    groups = listing.get("period_groups") if isinstance(listing, dict) else None
    if groups is None:
        return None
    return [g for group in groups for g in group.get("goals") or []]


def existing_goals(client: Any, quest_id: str, title: str) -> Optional[List[Dict[str, Any]]]:
    """The goals on the quest titled exactly ``title`` and not abandoned; ``None`` when the quest's goals could not be read (so nothing may be assumed)."""
    goals = all_goals(client, quest_id)
    if goals is None:
        return None
    return [g for g in goals if str(g.get("name") or "").strip() == title and not g.get("abandoned")]


def linked_open_goal(goals: List[Dict[str, Any]], solution_id: str, title: str) -> Optional[str]:
    """The id of the open goal already linked to ``solution_id``, the one whose name shares the longest start with ``title`` when several are: code decides this, no model."""
    import os.path
    linked = [g for g in goals if str(g.get("spec_solution_id") or "") == solution_id and not g.get("completed") and not g.get("abandoned")]
    if not linked:
        return None
    best = max(linked, key=lambda g: len(os.path.commonprefix([str(g.get("name") or ""), title])))
    return str(best.get("id"))


def read_back(client: Any, quest_id: str, goal_id: Optional[str], title: str) -> str:
    """What the quest holds for the goal after the write, read again: its assignee (by name), the strategy and the metric it is linked to. Words that say so when it could not be read."""
    goals = all_goals(client, quest_id)
    if goals is None:
        return "Read back: the goal could not be read again, so its owner and links are not confirmed."
    goal = next((g for g in goals if goal_id and str(g.get("id")) == str(goal_id)), None) or next((g for g in goals if str(g.get("name") or "").strip() == title), None)
    if goal is None:
        return "Read back: the goal was not found when read again."
    assignee = goal.get("assigned_to_user_id")
    names = {str(m.get("user_id")): str(m.get("name") or "") for m in (client.list_assignable_members(quest_id) or []) if m.get("user_id")}
    who = (names.get(str(assignee)) or f"user {assignee}") if assignee else "nobody (unassigned)"
    return (f"Read back from the quest: goal {goal.get('name')!r} is assigned to {who}, linked to solution {goal.get('spec_solution_id') or 'none'} "
            f"and metric {goal.get('spec_metric_id') or 'none'}, period {goal.get('period') or 'none'}.")


def goal_changes(goal: Mapping[str, Any], wanted: Mapping[str, Any]) -> Dict[str, Any]:
    """The wanted fields that differ from the goal as the quest lists it. A field the listing does not carry counts as different (sending it is safe)."""
    return {k: v for k, v in wanted.items() if k in UPDATABLE and goal.get(k) != v}


def ensure_goal(client: Any, quest_id: str, title: str, period: str, fields: Mapping[str, Any],
                assignee_id: Optional[str], adopt_goal_id: Optional[str] = None, link_match: bool = False) -> Dict[str, Any]:
    """Make exactly one goal titled ``title`` exist on the quest with these fields. Returns ``{state, goal_id, changed, writes, copies}`` where ``state`` is
    ``created``, ``updated``, ``unchanged`` or ``unknown`` (the quest's goals could not be read, nothing was written). ``writes`` are the client results,
    for the caller to read a held or refused one from. A client that raises on a refused write propagates; the caller reports it."""
    goals = all_goals(client, quest_id)
    if goals is None:
        return {"state": "unknown", "goal_id": None, "changed": [], "writes": [], "copies": 0}
    found = [g for g in goals if str(g.get("name") or "").strip() == title and not g.get("abandoned")]
    if not found and link_match and fields.get("spec_solution_id"):
        # Idempotent in code: a goal already linked to this strategy is the goal, updated in place, before any create. The model's pick below is only for older goals with no link.
        adopt_goal_id = linked_open_goal(goals, str(fields["spec_solution_id"]), title) or adopt_goal_id
    if not found and adopt_goal_id:
        # The same work already exists under an older title: it is updated in place (renamed to the key, linked, moved to the period), never duplicated.
        write = client.update_goal(str(adopt_goal_id), {"title": title, "period": period, **{k: v for k, v in fields.items() if k in UPDATABLE}})
        writes = [write]
        changed = ["title", "period", *sorted(k for k in fields if k in UPDATABLE)]
        if assignee_id:
            writes.append(client.assign_goal(str(adopt_goal_id), assignee_id))
            changed.append("assignee")
        return {"state": "updated", "goal_id": str(adopt_goal_id), "changed": changed, "writes": writes, "copies": 0}
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
