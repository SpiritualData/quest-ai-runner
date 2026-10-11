"""plan_reconcile: keep a quest's day and week goals current as the work moves.

Run once a day for a quest that has a daily/weekly plan. It makes these kinds of change, and only these:

0. CLASSIFY ASKS. An open ask nobody has classified yet (kind "unknown") is sent to ``classify``, which proposes a
   kind and, for a request, one assignee from the quest's own members. The AI decides; no person confirms it.
   A decision is applied only when it is structurally valid: a request needs an assignee who is a member of the
   quest and a confidence at or above ``MIN_CONFIDENCE``. Anything else stays unknown and is re-read tomorrow.
1. ROLL FORWARD. A day goal that is not completed and whose day has passed moves to today. A week goal that
   is not completed and whose week has passed moves to the current week. Each rolled goal's description
   starts with "Rolled forward from <old period>." so the history stays visible on the goal.
2. NEW WORK FROM ASKS. An open request on the quest that names an assignee becomes a day goal for that
   assignee today, under their current week goal. Creating it with the assignee also notifies them (the
   server does that on assignment). Each ask is turned into at most one goal, found again by its ask key.
   Every new goal is read back from Quest and must show the assignee, today's day and a scheduled date (the
   scheduled date is what puts it on the person's calendar). A goal that does not is re-assigned once; if it
   still does not verify, the action is reported as "failed" so it cannot pass silently.
3. NOTHING ELSE. A goal is never marked complete by this module, and no milestone date or name is changed.
   Completion is what the owner ticks in Quest.

The planner is pure (no network), so it is tested directly. ``reconcile_quest`` is the only part that calls
Quest or the model. Dry run is the default; ``write=True`` applies the plan.
"""
import datetime as dt
from typing import Any, Callable, Dict, List, Optional

# The feedback ledger states for a request nobody has finished (see runner/feedback_ledger.py).
OPEN_ASK_STATES = {"open", "in progress"}
ASK_KINDS = {"request"}
UNCLASSIFIED_KINDS = {"", "unknown"}
VALID_KINDS = {"request", "standing", "context", "question", "unknown"}
MIN_CONFIDENCE = 0.8
ROLL_NOTE = "Rolled forward from {old}."
ASK_NOTE = "Added from an ask by {author}. Ask id {ask_id}."

# classify(ask, roster) -> {"kind", "assignee_id", "confidence", "reason"} or None (no answer).
Classifier = Callable[[Dict[str, Any], List[Dict[str, Any]]], Optional[Dict[str, Any]]]
# judge_goal(goal) -> {"ai_doable", "confidence", "reason"} or None (no answer).
GoalJudge = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


def ask_key(ask: Dict[str, Any]) -> Optional[str]:
    """The ask's identifier as Quest's asks API names it. The list payload carries ``key``; ``id`` is
    accepted for older payloads. Reading only ``id`` was the bug that left every ask unplanned."""
    return ask.get("key") or ask.get("id")


def iso_week_key(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}_W{week:02d}"


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def next_week_key(day: dt.date) -> str:
    return iso_week_key(week_start(day) + dt.timedelta(days=7))


def validate_decision(raw: Optional[Dict[str, Any]], member_ids: set) -> Optional[Dict[str, Any]]:
    """Turn the model's structured answer into a decision the code will apply, or None.

    Checks structure only: the kind is one Quest knows, a request names a member of the quest, and the
    confidence is a number at or above the bar. The reason text is never read for control flow.
    """
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in VALID_KINDS or kind in UNCLASSIFIED_KINDS:
        return None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return None
    assignee = str(raw.get("assignee_id") or "").strip() or None
    if kind == "request":
        if confidence < MIN_CONFIDENCE or assignee not in member_ids:
            return None
    else:
        assignee = None
    return {"kind": kind, "assignee_id": assignee, "confidence": confidence,
            "reason": str(raw.get("reason") or "")[:300]}


def classify_asks(asks: List[Dict[str, Any]], roster: List[Dict[str, Any]], classify: Classifier) -> List[Dict[str, Any]]:
    """One decision per unclassified open ask the model answered validly. No side effects."""
    member_ids = {str(m.get("user_id")) for m in roster if m.get("user_id")}
    decisions = []
    for ask in asks:
        if (ask.get("kind") or "").lower() not in UNCLASSIFIED_KINDS:
            continue
        if (ask.get("state") or "").lower() not in OPEN_ASK_STATES:
            continue
        key = ask_key(ask)
        if not key:
            continue
        decision = validate_decision(classify(ask, roster), member_ids)
        if decision:
            decisions.append({"kind": "classify", "ask_key": key, "ask_kind": decision["kind"],
                              "assignee_id": decision["assignee_id"],
                              "confidence": decision["confidence"], "reason": decision["reason"]})
    return decisions


def validate_handoff(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """True only when the model says the work is AI-doable with confidence at or above the bar."""
    if not isinstance(raw, dict) or raw.get("ai_doable") is not True:
        return None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return None
    if confidence < MIN_CONFIDENCE:
        return None
    return {"confidence": confidence, "reason": str(raw.get("reason") or "")[:300]}


def plan_handoffs(goals: List[Dict[str, Any]], today: dt.date, judge: GoalJudge) -> List[Dict[str, Any]]:
    """Ask the model about each open, assigned, not-yet-judged day goal dated today or later.

    Only proposes. Whether the handoff happens is decided by the assignee's own AI-rep preference
    (see ``set_goal_ai_handling``), not here. Goals already judged (``ai_handling`` set) are skipped.
    """
    today_iso = today.isoformat()
    handoffs = []
    for g in goals:
        if g.get("completed") or not g.get("assigned_to_user_id") or g.get("ai_handling"):
            continue
        if g.get("time_scope") != "day" or (g.get("period") or "") < today_iso:
            continue
        decision = validate_handoff(judge(g))
        if decision:
            handoffs.append({"kind": "handoff", "goal_id": g["id"], "assignee_id": g["assigned_to_user_id"],
                             "name": g.get("name", ""), **decision})
    return handoffs


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
        key = ask_key(ask)
        assignee = ask.get("assignee_id")
        if not assignee or not key or key in covered_asks:
            continue
        parent = week_goal_for.get(assignee)
        if not parent:
            actions.append({"kind": "skip", "ask_key": key, "reason": "no current week goal for assignee"})
            continue
        actions.append({
            "kind": "create", "ask_key": key, "assignee_id": assignee, "parent_goal_id": parent,
            "title": "Ask: " + (ask.get("text") or "").strip()[:90],
            "description": ASK_NOTE.format(author=ask.get("author") or "a teammate", ask_id=key),
            "period": today_iso,
        })
    return actions


def _ask_ids(description: str) -> List[str]:
    marker = "Ask id "
    found = []
    for part in description.split(marker)[1:]:
        found.append(part.split(".")[0].strip())
    return found


def _goal_is_on_person_today(goal: Dict[str, Any], assignee: str, today_iso: str) -> bool:
    """A goal shows on the assignee's Today and calendar only when it is theirs, dated today, and scheduled."""
    return (goal.get("assigned_to_user_id") == assignee and goal.get("time_scope") == "day"
            and goal.get("period") == today_iso and str(goal.get("scheduled_date") or "")[:10] == today_iso)


def _find_goal(client, quest_id: str, goal_id: str) -> Optional[Dict[str, Any]]:
    """Read the goal back from the quest's goal list. ``get_goal`` has no single-goal route on any deployment."""
    payload = client.list_quest_goals(quest_id)
    for pg in payload.get("period_groups", []):
        for g in pg.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def _create_and_verify(client, quest_id: str, action: Dict[str, Any], today_iso: str) -> Dict[str, Any]:
    """Create one ask goal, then read it back. Re-assign once if it does not verify. Never raises."""
    created = client.create_goal(action["title"], period=action["period"], quest_id=quest_id,
                                 description=action["description"],
                                 criteria="Done when the ask is marked done in Quest.",
                                 parent_goal_id=action["parent_goal_id"],
                                 assigned_to_user_id=action["assignee_id"])
    goal_id = (created or {}).get("id")
    if not goal_id:
        return {**action, "kind": "failed", "reason": "Quest returned no goal id"}
    for attempt in range(2):
        goal = _find_goal(client, quest_id, goal_id)
        if goal and _goal_is_on_person_today(goal, action["assignee_id"], today_iso):
            return {**action, "goal_id": goal_id, "verified": True}
        if attempt == 0:
            client._request("PUT", f"/api/planning/goals/{goal_id}/assign",
                            body={"assigned_to_user_id": action["assignee_id"]})
    return {**action, "kind": "failed", "goal_id": goal_id,
            "reason": "goal did not verify as the assignee's dated, scheduled day goal"}


def reconcile_quest(client, quest_id: str, today: dt.date, write: bool = False,
                    classify: Optional[Classifier] = None,
                    roster: Optional[List[Dict[str, Any]]] = None,
                    judge_goal: Optional[GoalJudge] = None) -> List[Dict[str, Any]]:
    """Classify open asks, read goals, plan, and (if write) apply. Returns the planned actions."""
    goals_payload = client.list_quest_goals(quest_id)
    goals = [g for pg in goals_payload.get("period_groups", []) for g in pg.get("goals", [])]
    asks = client.list_asks(quest_id=quest_id, limit=200) or []

    actions: List[Dict[str, Any]] = []
    if classify is not None:
        roster = roster if roster is not None else client.list_assignable_members(quest_id)
        decisions = classify_asks(asks, roster, classify)
        for d in decisions:
            if write:
                fields = {"kind": d["ask_kind"], "assignee_id": d["assignee_id"] or ""}
                client.patch_ask(d["ask_key"], fields)
            for ask in asks:
                if ask_key(ask) == d["ask_key"]:
                    ask["kind"], ask["assignee_id"] = d["ask_kind"], d["assignee_id"]
        actions.extend(decisions)

    actions.extend(plan_actions(goals, asks, today))
    if judge_goal is not None:
        actions.extend(plan_handoffs(goals, today, judge_goal))
    if not write:
        return actions

    today_iso = today.isoformat()
    applied: List[Dict[str, Any]] = []
    for a in actions:
        if a["kind"] == "roll":
            client.update_goal(a["goal_id"], {
                "period": a["new_period"],
                "description": ROLL_NOTE.format(old=a["old"]) + " " + _description_of(goals, a["goal_id"]),
            })
            if a.get("parent_goal_id") and a["new_scope"] == "day":
                client._request("PUT", f"/api/planning/goals/{a['goal_id']}/parent",
                                body={"parent_goal_id": a["parent_goal_id"]})
            applied.append(a)
        elif a["kind"] == "create":
            applied.append(_create_and_verify(client, quest_id, a, today_iso))
        elif a["kind"] == "handoff":
            applied.append(_hand_off(client, a))
        else:
            applied.append(a)
    return applied


def _hand_off(client, action: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Quest to route the goal to the assignee's AI rep as an autopilot decision. The assignee's
    preference then decides: auto queues it, ask proposes it, never keeps it. Never raises."""
    try:
        resp = client._request("PUT", f"/api/planning/goals/{action['goal_id']}/ai-handling",
                               body={"handling": "rep", "decidedBy": "rep"}) or {}
    except Exception as exc:  # noqa: BLE001 -- one refused goal must not stop the rest of the run
        return {**action, "kind": "failed", "reason": f"handoff refused: {str(exc)[:200]}"}
    return {**action, "outcome": resp.get("aiHandling") or resp.get("ai_handling") or "none"}


def _description_of(goals: List[Dict[str, Any]], goal_id: str) -> str:
    for g in goals:
        if g["id"] == goal_id:
            return g.get("description") or ""
    return ""
