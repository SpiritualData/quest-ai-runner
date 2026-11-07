"""The ``create_quest_workflow`` tool: people resolved by name, the wire body, refusals reported
honestly, and that it is a registered write."""
from quest_ai_runner.the_body.hands.quest_operations import tools_quest_ops as ops
from quest_ai_runner.the_world.tools.tools import ToolContext, build_tool_registry

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


class ListingClient(FakeClient):
    def __init__(self, existing):
        super().__init__()
        self.existing = existing

    def list_quest_workflows(self, quest_id):
        return self.existing


def existing_weekly():
    return [{"workflowId": "qwf_9", "title": "Check progress", "steps": [
        {"title": "Check how it is going", "timing": {"repeat": {"freq": "weekly", "interval": 1, "byWeekday": [0]}}}]}]


def test_the_same_routine_under_another_title_is_already_there_and_nothing_is_created():
    client = ListingClient(existing_weekly())
    step = {**STEP, "title": "Check how it is going", "repeat": "weekly", "by_weekday": [0]}
    result = call(client, title="Monday check", steps=[step])
    assert result.ok and "Already there" in result.text and "qwf_9" in result.text and client.calls == []


def test_a_different_schedule_or_an_unreadable_listing_is_not_a_match():
    step = {**STEP, "title": "Check how it is going", "repeat": "weekly", "by_weekday": [3]}
    client = ListingClient(existing_weekly())
    assert call(client, title="Thursday check", steps=[step]).ok and len(client.calls) == 1
    unreadable = ListingClient(None)
    assert not call(unreadable, title="W", steps=[STEP]).ok and unreadable.calls == []


def test_a_weekly_monday_step_with_the_shapes_the_planner_sends_is_created():
    members = MEMBERS
    for weekday in (0, [0], "0"):
        client = ListingClient([])
        step = {"title": "Check how it is going", "assignee": "Ann", "starts_at": "2026-11-09T08:00:00+00:00",
                "ends_at": "2026-11-09T12:00:00+00:00", "repeat": "weekly", "by_weekday": weekday, "tz": "UTC"}
        result = call(client, title="Monday check", steps=[step])
        assert result.ok and client.calls, (weekday, result.text)
        assert client.calls[0][2][0]["timing"]["repeat"]["byWeekday"] == [0]


def test_a_tool_that_raises_on_argument_shape_reports_a_plain_message():
    from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolSpec
    def boom(args, ctx):
        raise TypeError("'int' object is not iterable")
    registry = ToolRegistry([ToolSpec(name="t", description="d", mutates=False, handler=boom)])
    result = registry.invoke("t", {}, ToolContext())
    assert not result.ok and "could not use the arguments as given" in result.text and "iterable" not in result.text


THURSDAY = {"title": "Run the weekly check-in", "assignee": "Ann", "starts_at": "", "ends_at": "", "repeat": "weekly", "by_weekday": [3]}


class ZonedClient(ListingClient):
    def get_quest_autopilot(self, quest_id):
        return {"autopilot": {"run_timezone": "America/Los_Angeles"}}


def test_a_weekly_step_with_its_weekday_and_empty_times_is_created_as_the_whole_day_in_the_quests_zone():
    import datetime
    client = ZonedClient([])
    result = call(client, title="Weekly check-in", steps=[THURSDAY])
    assert result.ok and client.calls and "one or two short sentences" in result.text
    timing = client.calls[0][2][0]["timing"]
    assert timing["kind"] == "window" and timing["repeat"]["byWeekday"] == [3] and timing["repeat"]["tz"] == "America/Los_Angeles"
    start, end = datetime.datetime.fromisoformat(timing["startsAt"]), datetime.datetime.fromisoformat(timing["endsAt"])
    assert start.weekday() == 3 and end.weekday() == 3 and (start.hour, start.minute, end.hour, end.minute) == (0, 0, 23, 59) and start.utcoffset() is not None


def test_the_all_day_window_is_the_next_such_weekday_today_counting():
    now = ops.dt.datetime(2026, 11, 4, 10, 0, tzinfo=ops.dt.timezone.utc)         # a Wednesday
    assert ops.all_day_window(3, "", now)[0].startswith("2026-11-05T00:00")
    assert ops.all_day_window(2, "", now)[0].startswith("2026-11-04T00:00")
    late_sunday_utc = ops.dt.datetime(2026, 11, 9, 3, 0, tzinfo=ops.dt.timezone.utc)   # still Sunday in Los Angeles
    assert ops.all_day_window(6, "America/Los_Angeles", late_sunday_utc)[0].startswith("2026-11-08T00:00")


def test_a_repeat_creates_nothing_the_second_time_and_a_step_with_no_weekday_still_needs_its_times():
    client = ListingClient([])
    first = call(client, title="Weekly check-in", steps=[THURSDAY])
    assert first.ok and len(client.calls) == 1
    client.existing = [{"workflowId": "qwf_1", "title": "Weekly check-in", "steps": []}]
    again = call(client, title="Weekly check-in", steps=[THURSDAY])
    assert again.ok and len(client.calls) == 1 and "Already there" in again.text
    refused = call(ListingClient([]), title="W", steps=[{"title": "x", "assignee": "Ann", "starts_at": "", "ends_at": ""}])
    assert not refused.ok and "needs starts_at and ends_at" in refused.text and "a weekly step may give repeat weekly and by_weekday" in refused.text


def test_the_planner_is_told_a_missing_optional_detail_never_blocks_a_create_request():
    from quest_ai_runner.the_intelligence.mind.planner.planner_prompt import DEEP_ACTION
    assert "A MISSING OPTIONAL DETAIL NEVER BLOCKS" in DEEP_ACTION and "one or two short" in DEEP_ACTION and "change the call" in DEEP_ACTION
