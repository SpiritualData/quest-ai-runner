"""The read-only ``list_quest_workflows`` tool: names resolved, open and person filters, honest
failure, and that a workflow-dispatched task's tool set offers it."""
import pytest

from quest_ai_runner.core import tools_quest_ops as ops
from quest_ai_runner.core.tools import ToolContext, build_tool_registry
from quest_ai_runner.runner.action_catalog import is_publishable_tool

CREDS = {"QUEST_BASE_URL": "https://example.invalid", "QUEST_API_KEY": "key"}


def step(step_id, user="u1", status="pending", ends="2026-10-26T19:00:00+00:00", title=None, text=""):
    actor = {"kind": "human", "userId": user, "repId": None, "seat": None, "label": None}
    return {"stepId": step_id, "title": title or f"Step {step_id}", "instructions": text, "actor": actor,
            "status": status, "window": {"startsAt": "2026-10-25T20:00:00+00:00", "endsAt": ends},
            "completedAt": None}


WORKFLOW = {"workflowId": "wf1", "title": "Weekly flow", "steps": [
    step("approve_a", title="A approves the plans", status="pending", text="Approve or deny each plan."),
    step("agree_a", status="done", ends="2026-10-20T00:00:00+00:00"),
    step("late_b", user="u2", status="late", ends="2026-10-18T00:00:00+00:00"),
    step("rep_step", user=None, status="open"),
]}
WORKFLOW["steps"][3]["actor"] = {"kind": "ai", "userId": None, "repId": "rep1", "seat": None, "label": None}


class FakeClient:
    def __init__(self, workflows):
        self.workflows = workflows

    def list_quest_workflows(self, quest_id):
        return self.workflows

    def list_assignable_members(self, quest_id):
        return [{"user_id": "u1", "name": "Ann Example"}, {"user_id": "u2", "name": "Bo Other"}]


def call(workflows=None, **args):
    wf = [WORKFLOW] if workflows is None else workflows
    spec = ops.list_quest_workflows_spec(CREDS, client_factory=lambda: FakeClient(wf))
    return spec.handler(args, ToolContext(quest_id="q1"))


def ids(result):
    return [r["step_id"] for r in result.data]


def test_rows_name_the_person_window_status_and_due():
    row = next(r for r in call().data if r["step_id"] == "approve_a")
    assert row["assignee_name"] == "Ann Example" and row["status"] == "pending"
    assert row["due"] == "2026-10-26T19:00:00+00:00" and row["window_starts"].startswith("2026-10-25")
    assert "approve_a [pending] for Ann Example" in call().text


def test_done_hidden_by_default_and_soonest_due_first():
    assert ids(call()) == ["late_b", "approve_a", "rep_step"]
    assert "agree_a" in ids(call(open_only=False))


def test_filter_by_person_name_status_and_words():
    assert ids(call(assignee="ann")) == ["approve_a"]
    assert ids(call(assignee="u2")) == ["late_b"]
    assert ids(call(status="late,missed")) == ["late_b"]
    assert ids(call(text_contains="approv", assignee="Ann")) == ["approve_a"]


def test_ai_step_is_labelled_not_blank():
    assert next(r for r in call().data if r["step_id"] == "rep_step")["assignee_name"] == "an AI rep"


def test_no_matches_and_no_workflows_are_ok_but_a_failed_read_is_an_error():
    assert call(assignee="nobody").ok and "No workflow steps" in call(assignee="nobody").text
    assert call(workflows=[]).ok and "no workflows" in call(workflows=[]).text
    failed = ops.list_quest_workflows_spec(
        CREDS, client_factory=lambda: FakeClient(None)).handler({}, ToolContext(quest_id="q1"))
    assert not failed.ok and "could not be read" in failed.text


def test_paging_is_capped():
    many = {"workflowId": "w", "title": "t", "steps": [step(f"s{i:03d}") for i in range(300)]}
    assert len(call([many]).data) == ops.WORKFLOW_STEPS_DEFAULT_LIMIT
    assert len(call([many], limit=9999).data) == ops.WORKFLOW_STEPS_MAX_LIMIT


def test_registered_read_only_and_offered_to_a_reminder_task():
    registry = build_tool_registry(CREDS)
    spec = registry.get("list_quest_workflows")
    assert spec is not None and spec.mutates is False and not is_publishable_tool(spec)
    text = ("Send a weekly reminder by email covering the plan approvals waiting for him in the "
            "workflow on this quest, with the steps and their status.")
    assert "list_quest_workflows" in {s.name for s in registry.select(text)}
