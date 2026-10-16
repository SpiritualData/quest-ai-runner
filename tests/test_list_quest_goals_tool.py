"""The read-only ``list_quest_goals`` tool: filters, paging caps, honest failure, and that a
workflow-dispatched task's tool set offers it (while the workflow action catalog still withholds it)."""
from datetime import date

import pytest

from quest_ai_runner.core import tools_quest_ops as ops
from quest_ai_runner.core.tools import ToolContext, build_tool_registry
from quest_ai_runner.runner.action_catalog import is_publishable_tool

CREDS = {"QUEST_BASE_URL": "https://example.invalid", "QUEST_API_KEY": "key"}
TODAY = date(2026, 10, 16)


def goal(idx, **kw):
    base = {"id": f"g{idx}", "name": f"Goal {idx}", "description": None, "time_scope": "week",
            "period": "2026_W42", "deadline": None, "scheduled_date": None, "completed": False,
            "parent_goal_id": None, "assigned_to_user_id": None, "abandoned": False}
    base.update(kw)
    return base


class FakeClient:
    def __init__(self, goals, payload=None):
        self.payload = payload if payload is not None else {
            "quest_id": "q1", "period_groups": [{"time_scope": "week", "goals": goals}]}

    def list_quest_goals(self, quest_id):
        return self.payload

    def list_assignable_members(self, quest_id):
        return [{"user_id": "u1", "name": "Zee Godswill"}, {"user_id": "u2", "name": "Other"}]


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(ops, "today_date", lambda: TODAY)


def call(goals, **args):
    spec = ops.list_quest_goals_spec(CREDS, client_factory=lambda: FakeClient(goals))
    return spec.handler(args, ToolContext(quest_id="q1"))


def ids(result):
    return [r["id"] for r in result.data]


def test_rows_carry_text_assignee_name_status_and_due():
    res = call([goal(1, description="Owner: Zee Godswill.\nFix it", assigned_to_user_id="u1",
                     deadline="2026-10-18")])
    row = res.data[0]
    assert row["text"].startswith("Owner: Zee Godswill.") and row["assignee_name"] == "Zee Godswill"
    assert row["due"] == "2026-10-18" and row["completed"] is False
    assert "Owner: Zee Godswill." in res.text and "Zee Godswill" in res.text


def test_scheduled_day_beats_period_deadline():
    row = call([goal(1, deadline="2026-12-31", scheduled_date="2026-10-17")]).data[0]
    assert row["due"] == "2026-10-17"


def test_open_only_is_the_default_and_can_be_turned_off():
    goals = [goal(1), goal(2, completed=True), goal(3, abandoned=True)]
    assert ids(call(goals)) == ["g1"]
    assert sorted(ids(call(goals, open_only=False))) == ["g1", "g2", "g3"]


def test_assignee_and_text_filters():
    goals = [goal(1, assigned_to_user_id="u1"), goal(2, assigned_to_user_id="u2"),
             goal(3, description="Owner: Zee Godswill.")]
    assert ids(call(goals, assignee_user_id="u2")) == ["g2"]
    assert ids(call(goals, text_contains="owner: zee")) == ["g3"]


def test_due_within_days_window_and_overdue():
    goals = [goal(1, deadline="2026-10-15"), goal(2, deadline="2026-10-16"),
             goal(3, deadline="2026-10-23"), goal(4, deadline="2026-10-24"), goal(5)]
    assert ids(call(goals, due_within_days=7)) == ["g2", "g3"]
    assert ids(call(goals, due_within_days=7, include_overdue=True)) == ["g1", "g2", "g3"]


def test_output_is_paged_and_capped():
    goals = [goal(f"{i:03d}", deadline="2026-10-20") for i in range(300)]
    first = call(goals)
    assert len(first.data) == ops.GOALS_DEFAULT_LIMIT and "offset=25" in first.text
    assert len(call(goals, limit=9999).data) == ops.GOALS_MAX_LIMIT
    assert ids(call(goals, limit=2, offset=2)) == ["g002", "g003"]


def test_long_text_is_trimmed_in_the_rendering():
    res = call([goal(1, description="Owner: A.\n" + "x" * 5000)])
    assert len(res.text) < 900


def test_unreadable_quest_is_an_error_not_an_empty_list():
    spec = ops.list_quest_goals_spec(CREDS, client_factory=lambda: FakeClient([], payload={}))
    res = spec.handler({}, ToolContext(quest_id="q1"))
    assert not res.ok and "could not be read" in res.text


def test_no_quest_asks_for_one():
    spec = ops.list_quest_goals_spec(CREDS, client_factory=lambda: FakeClient([]))
    assert not spec.handler({}, ToolContext()).ok


def test_read_only_registered_and_not_published_to_the_workflow_catalog():
    registry = build_tool_registry(CREDS)
    spec = registry.get("list_quest_goals")
    assert spec is not None and spec.mutates is False
    assert not is_publishable_tool(spec)
    assert registry.get("send_quest_email") is not None


def test_workflow_dispatched_task_text_is_offered_the_goals_tool_with_send_email():
    registry = build_tool_registry(CREDS)
    text = ("Send Zee Godswill their weekly reminder by email: call send_quest_email on quest q1 listing "
            "their open goals on this quest due in the next 7 days. Their goals are the goals on the "
            "quest whose text begins with the line 'Owner: Zee Godswill.'")
    shown = {s.name for s in registry.select(text)}
    assert {"list_quest_goals", "send_quest_email"} <= shown
