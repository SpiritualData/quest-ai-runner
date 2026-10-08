"""Offline tests for runner/plan_reconcile.py: the pure planner, no Quest calls."""
import datetime as dt

from quest_ai_runner.runner.plan_reconcile import (
    iso_week_key,
    plan_actions,
    reconcile_quest,
)

TODAY = dt.date(2026, 10, 14)  # a Wednesday in ISO week 2026_W42
THIS_WEEK = "2026_W42"


def goal(gid, scope, period, owner="u1", completed=False, name="g", description=""):
    return {
        "id": gid, "time_scope": scope, "period": period, "completed": completed,
        "assigned_to_user_id": owner, "name": name, "description": description,
    }


def test_iso_week_key_matches_the_calendar():
    assert iso_week_key(TODAY) == THIS_WEEK
    assert iso_week_key(dt.date(2026, 10, 9)) == "2026_W41"


def test_unfinished_past_day_goal_rolls_to_today_under_current_week():
    goals = [
        goal("wk", "week", THIS_WEEK),
        goal("d1", "day", "2026-10-13"),
    ]
    actions = plan_actions(goals, [], TODAY)
    assert actions == [{
        "kind": "roll", "goal_id": "d1", "name": "g", "old": "2026-10-13",
        "new_period": "2026-10-14", "new_scope": "day", "parent_goal_id": "wk",
    }]


def test_completed_goal_is_never_touched():
    goals = [goal("d1", "day", "2026-10-12", completed=True), goal("wk", "week", THIS_WEEK)]
    assert plan_actions(goals, [], TODAY) == []


def test_todays_and_future_day_goals_stay_put():
    goals = [goal("d_today", "day", "2026-10-14"), goal("d_future", "day", "2026-10-20"),
             goal("wk", "week", THIS_WEEK)]
    assert plan_actions(goals, [], TODAY) == []


def test_unfinished_past_week_goal_rolls_to_the_current_week():
    goals = [goal("w_old", "week", "2026_W41")]
    actions = plan_actions(goals, [], TODAY)
    assert len(actions) == 1
    assert actions[0]["kind"] == "roll"
    assert actions[0]["new_period"] == THIS_WEEK
    assert actions[0]["new_scope"] == "week"


def test_open_request_becomes_a_day_goal_for_its_assignee():
    goals = [goal("wk_u2", "week", THIS_WEEK, owner="u2")]
    asks = [{"id": "ask1", "kind": "request", "state": "open", "assignee_id": "u2",
             "text": "Send the Payal variant links to Travis", "author": "Zee"}]
    actions = plan_actions(goals, asks, TODAY)
    assert len(actions) == 1
    a = actions[0]
    assert a["kind"] == "create"
    assert a["assignee_id"] == "u2"
    assert a["parent_goal_id"] == "wk_u2"
    assert a["period"] == "2026-10-14"
    assert a["title"].startswith("Ask: Send the Payal variant links")
    assert "Ask id ask1." in a["description"]


def test_an_ask_already_turned_into_a_goal_is_not_duplicated():
    goals = [goal("wk", "week", THIS_WEEK),
             goal("made", "day", "2026-10-13", description="Added from an ask by Zee. Ask id ask1.")]
    asks = [{"id": "ask1", "kind": "request", "state": "open", "assignee_id": "u1", "text": "x"}]
    actions = plan_actions(goals, asks, TODAY)
    assert [a["kind"] for a in actions] == ["roll"]  # only the roll, never a second copy


def test_done_asks_and_non_requests_are_ignored():
    goals = [goal("wk", "week", THIS_WEEK)]
    asks = [
        {"id": "a1", "kind": "request", "state": "done", "assignee_id": "u1", "text": "x"},
        {"id": "a2", "kind": "context", "state": "open", "assignee_id": "u1", "text": "x"},
        {"id": "a3", "kind": "request", "state": "open", "assignee_id": None, "text": "x"},
    ]
    assert plan_actions(goals, asks, TODAY) == []


def test_ask_without_a_current_week_goal_is_skipped_not_guessed():
    goals = [goal("wk_other", "week", THIS_WEEK, owner="u9")]
    asks = [{"id": "ask1", "kind": "request", "state": "open", "assignee_id": "u2", "text": "x"}]
    actions = plan_actions(goals, asks, TODAY)
    assert actions == [{"kind": "skip", "ask_id": "ask1", "reason": "no current week goal for assignee"}]


class FakeClient:
    def __init__(self, goals, asks):
        self.payload = {"period_groups": [{"goals": goals}]}
        self.asks = asks
        self.calls = []

    def list_quest_goals(self, quest_id):
        return self.payload

    def list_asks(self, *, quest_id, limit=50):
        return self.asks

    def update_goal(self, goal_id, fields):
        self.calls.append(("update", goal_id, fields))

    def create_goal(self, title, **kwargs):
        self.calls.append(("create", title, kwargs))
        return {"id": "new"}

    def _request(self, method, path, body=None):
        self.calls.append(("request", method, path, body))


def test_dry_run_makes_no_calls():
    client = FakeClient([goal("d1", "day", "2026-10-12")], [])
    actions = reconcile_quest(client, "q1", TODAY, write=False)
    assert len(actions) == 1
    assert client.calls == []


def test_write_applies_roll_with_note_and_reparent_and_creates_assigned_goal():
    goals = [goal("wk", "week", THIS_WEEK), goal("d1", "day", "2026-10-12", description="Send the list.")]
    asks = [{"id": "ask1", "kind": "request", "state": "open", "assignee_id": "u1",
             "text": "Do the thing", "author": "Zee"}]
    client = FakeClient(goals, asks)
    reconcile_quest(client, "q1", TODAY, write=True)
    kinds = [c[0] for c in client.calls]
    assert kinds == ["update", "request", "create"]
    update = client.calls[0]
    assert update[2]["period"] == "2026-10-14"
    assert update[2]["description"].startswith("Rolled forward from 2026-10-12.")
    assert update[2]["description"].endswith("Send the list.")
    create = client.calls[2]
    assert create[2]["assigned_to_user_id"] == "u1"
    assert create[2]["parent_goal_id"] == "wk"
