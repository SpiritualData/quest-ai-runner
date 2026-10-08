"""plan_reconcile: keep a quest's day and week goals current as the work moves.

Run once a day for a quest that has a daily/weekly plan. It makes three kinds of change, and only these:

1. ROLL FORWARD. A day goal that is not completed and whose day has passed moves to today. A week goal that
   is not completed and whose week has passed moves to the current week. Each rolled goal's description
   starts with "Rolled forward from <old period>." so the history stays visible on the goal.
2. NEW WORK FROM ASKS. An open request on the quest that names an assignee becomes a day goal for that
   assignee today, under their current week goal. Creating it with the assignee also notifies them (the
   server does that on assignment). Each ask is turned into at most one goal, found again by its ask id.
3. NOTHING ELSE. A goal is never marked complete by this module, and no milestone date or name is changed.
   Completion is what the owner ticks in Quest.

The planner is pure (no network), so it is tested directly. ``reconcile_quest`` is the only part that calls
Quest. Dry run is the default; ``write=True`` applies the plan.
"""
import datetime as dt
from typing import Any, Dict, List, Optional

# The feedback ledger states for a request nobody has finished (see runner/feedback_ledger.py).
OPEN_ASK_STATES = {"open", "in progress"}
ASK_KINDS = {"request"}
ROLL_NOTE = "Rolled forward from {old}."
ASK_NOTE = "Added from an ask by {author}. Ask id {ask_id}."


def iso_week_key(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}_W{week:02d}"


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def next_week_key(day: dt.date) -> str:
    return iso_week_key(week_start(day) + dt.timedelta(days=7))


def plan_actions(goals: List[Dict[str, Any]], asks: List[Dict[str, Any]], today: dt.date) -> List[Dict[str, Any]]:
    """Decide the changes for one quest on one day. Returns a list of action dicts, no side effects."""
    actions: List[Dict[str, Any]] = []
    today_iso = today.isoformat()
    this_week = iso_week_key(today)
    covered_asks = {a_id for g in goals for a_id in _ask_ids(g.get("description") or "")}

    # The current week goal for each assignee, the parent for new day goals and for rolled day goals.
    week_goal_for: Dict[Optional[str], str] = {}
    for g in goals:
        if g.get("time_scope") == "week" and g.get("period") == this_week and not g.get("completed"):
            week_goal_for.setdefault(g.get("assigned_to_user_id"), g["id"])

    for g in goals:
        if g.get("completed"):
            continue
        scope, period = g.get("time_scope"), g.get("period") or ""
        if scope == "day" and period < today_iso:
            actions.append({
                "kind": "roll", "goal_id": g["id"], "name": g.get("name", ""), "old": period,
                "new_period": today_iso, "new_scope": "day",
                "parent_goal_id": week_goal_for.get(g.get("assigned_to_user_id")),
            })
        elif scope == "week" and period < this_week:
            actions.append({
                "kind": "roll", "goal_id": g["id"], "name": g.get("name", ""), "old": period,
                "new_period": this_week, "new_scope": "week", "parent_goal_id": None,
            })

    for ask in asks:
        if (ask.get("kind") or "").lower() not in ASK_KINDS:
            continue
        if (ask.get("state") or "").lower() not in OPEN_ASK_STATES:
            continue
        assignee = ask.get("assignee_id")
        if not assignee or ask["id"] in covered_asks:
            continue
        parent = week_goal_for.get(assignee)
        if not parent:
            actions.append({"kind": "skip", "ask_id": ask["id"], "reason": "no current week goal for assignee"})
            continue
        actions.append({
            "kind": "create", "ask_id": ask["id"], "assignee_id": assignee, "parent_goal_id": parent,
            "title": "Ask: " + (ask.get("text") or "").strip()[:90],
            "description": ASK_NOTE.format(author=ask.get("author") or "a teammate", ask_id=ask["id"]),
            "period": today_iso,
        })
    return actions


def _ask_ids(description: str) -> List[str]:
    marker = "Ask id "
    found = []
    for part in description.split(marker)[1:]:
        found.append(part.split(".")[0].strip())
    return found


def reconcile_quest(client, quest_id: str, today: dt.date, write: bool = False) -> List[Dict[str, Any]]:
    """Read the quest's goals and asks, plan, and (if write) apply. Returns the planned actions."""
    goals_payload = client.list_quest_goals(quest_id)
    goals = [g for pg in goals_payload.get("period_groups", []) for g in pg.get("goals", [])]
    asks = client.list_asks(quest_id=quest_id, limit=200) or []
    actions = plan_actions(goals, asks, today)
    if not write:
        return actions
    for a in actions:
        if a["kind"] == "roll":
            client.update_goal(a["goal_id"], {
                "period": a["new_period"],
                "description": ROLL_NOTE.format(old=a["old"]) + " " + _description_of(goals, a["goal_id"]),
            })
            if a.get("parent_goal_id") and a["new_scope"] == "day":
                client._request("PUT", f"/api/planning/goals/{a['goal_id']}/parent",
                                body={"parent_goal_id": a["parent_goal_id"]})
        elif a["kind"] == "create":
            client.create_goal(a["title"], period=a["period"], quest_id=quest_id,
                               description=a["description"],
                               criteria="Done when the ask is marked done in Quest.",
                               parent_goal_id=a["parent_goal_id"],
                               assigned_to_user_id=a["assignee_id"])
    return actions


def _description_of(goals: List[Dict[str, Any]], goal_id: str) -> str:
    for g in goals:
        if g["id"] == goal_id:
            return g.get("description") or ""
    return ""
