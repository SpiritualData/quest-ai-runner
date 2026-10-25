"""The ``create_quest_workflow`` tool: people resolved by name, the wire body, refusals reported
honestly, and that it is a registered write."""
from quest_ai_runner.core import tools_quest_ops as ops
from quest_ai_runner.core.tools import ToolContext, build_tool_registry

CREDS = {"QUEST_BASE_URL": "https://example.invalid", "QUEST_API_KEY": "key"}
MEMBERS = [{"user_id": "u1", "name": "Ann Example", "email": "ann@example.invalid"},
           {"user_id": "u2", "name": "Bo Example", "email": "bo@example.invalid"}]
STEP = {"title": "Pick roles", "assignee": "Ann", "starts_at": "2026-10-26T08:00:00+00:00",
        "ends_at": "2026-10-26T09:00:00+00:00", "repeat": "weekly", "by_weekday": [0, 1, 2, 3, 4]}


class FakeClient:
    def __init__(self, refuse=None):
        self.calls, self.refuse = [], refuse

    def list_assignable_members(self, quest_id):
        return MEMBERS

    def create_quest_workflow(self, quest_id, title, steps, *, edges=None, status=None):
        if self.refuse:
            raise RuntimeError(self.refuse)
        self.calls.append((quest_id, title, steps, edges, status))
        return {"workflowId": "qwf_1"}


def call(client, **args):
    spec = ops.create_quest_workflow_spec(CREDS, client_factory=lambda: client)
    return spec.handler(args, ToolContext(quest_id="q1"))


def test_creates_with_names_resolved_and_chained_steps():
    client = FakeClient()
    ai = {"title": "Draft", "rep_id": "rep1", "run_at": "2026-10-26T07:00:00+00:00"}
    result = call(client, title="Applications", steps=[STEP, ai], chain=True, status="draft")
    assert result.ok and "qwf_1" in result.text
    quest_id, title, steps, edges, status = client.calls[0]
    assert (quest_id, title, status) == ("q1", "Applications", "draft")
    assert steps[0]["actor"] == {"kind": "human", "userId": "u1"}
    assert steps[0]["timing"]["repeat"] == {"freq": "weekly", "interval": 1, "byWeekday": [0, 1, 2, 3, 4]}
    assert steps[1]["actor"] == {"kind": "ai", "repId": "rep1"} and steps[1]["timing"]["kind"] == "at"
    assert edges == [{"fromStepId": "s1", "toStepId": "s2"}]


def test_unknown_person_or_missing_times_create_nothing():
    client = FakeClient()
    bad = call(client, title="W", steps=[{**STEP, "assignee": "Nobody"}])
    assert not bad.ok and "Members: Ann Example, Bo Example" in bad.text
    assert not call(client, title="W", steps=[{"title": "x", "assignee": "Ann"}]).ok
    assert not call(client, title="", steps=[STEP]).ok
    assert client.calls == []


def test_a_refusal_is_reported_not_swallowed():
    result = call(FakeClient(refuse="403: You don't have permission to edit this quest"), title="W", steps=[STEP])
    assert not result.ok and "Nothing was created" in result.text and "permission" in result.text


def test_registered_as_a_write():
    spec = build_tool_registry(CREDS).get("create_quest_workflow")
    assert spec is not None and spec.mutates is True
