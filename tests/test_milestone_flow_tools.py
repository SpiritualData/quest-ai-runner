"""The milestone flow's tools over a host's own Quest client and metric reader: a host that runs the brain inside its service (no QAR_BASE_URL, no key) gets the same flow."""
import datetime

from quest_ai_runner.api import MetricSourceError, ToolRegistry, WriteResult, checked_reading, milestone_flow_tools
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue

NOW = datetime.datetime(2026, 11, 4, 18, 0, tzinfo=datetime.timezone.utc)
SPEC = {"records": [
    {"recordId": "sol_1", "type": "solution", "revision": 1, "fields": {
        "name": "P1 Onboarding that captivates", "milestones": "Onboarding test passes on web and mobile.", "measures_of_success": "Completion rate",
        "owner_user_ids": ["u_ann"], "source": "funnel"}},
    {"recordId": "met_1", "type": "metric", "revision": 1, "fields": {"name": "P1 Onboarding completion", "record": "50 percent", "solution_ids": ["sol_1"], "source": "funnel"}},
]}


class Client:
    """A host's in-process Quest client: only the calls the tools make."""

    def __init__(self):
        self.goals, self.created = [], []

    def get_spec(self, subject_type, subject_id):
        return SPEC

    def get_quest_autopilot(self, quest_id):
        return {"autopilot": {"run_timezone": "UTC"}}

    def list_quest_goals(self, quest_id, **kw):
        return {"period_groups": [{"goals": list(self.goals)}]}

    def list_assignable_members(self, quest_id):
        return [{"user_id": "u_ann", "name": "Ann", "email": "ann@example.test"}]

    def create_goal(self, title, *, period, quest_id=None, **fields):
        goal = {"id": "g1", "name": title, "period": period, "assigned_to_user_id": fields.get("assigned_to_user_id"), "description": fields.get("description"),
                "spec_solution_id": fields.get("spec_solution_id"), "spec_metric_id": fields.get("spec_metric_id"), "scheduled_date": fields.get("scheduled_date")}
        self.goals.append(goal)
        self.created.append(goal)
        return WriteResult({"id": "g1", "title": title, "assignedToUserId": goal["assigned_to_user_id"], "specSolutionId": goal["spec_solution_id"],
                            "specMetricId": goal["spec_metric_id"], "period": period, "description": goal["description"]}, id="g1")


def decide(prompt):
    return "sol_1" if "These are milestones of the quest" in prompt else "{}"


def flow(reader):
    client = Client()
    registry = ToolRegistry(milestone_flow_tools(lambda: client, reader))
    resolved = cue.resolve(registry, "q1", "get the onboarding test moving", NOW, decide)
    return client, registry, resolved


def test_the_four_tools_are_built_over_the_hosts_client_and_the_source_tool_only_with_a_reader():
    assert sorted(spec.name for spec in milestone_flow_tools(lambda: Client(), lambda name: {})) == [
        "create_or_update_quest_goal", "list_quest_goals", "read_metric_source", "read_spec_records"]
    assert "read_metric_source" not in [spec.name for spec in milestone_flow_tools(lambda: Client())]


def test_the_flow_runs_on_a_hosts_tools_and_the_value_comes_from_the_hosts_reader():
    reading = {"value": 61, "as_of": "2026-11-03", "source": "Funnel report", "detail": {}}

    def read(name):
        if name != "funnel":
            raise MetricSourceError("unknown")
        return reading
    client, registry, resolved = flow(read)
    receipt, status = resolved.carry_out(registry, decide)
    goal = client.created[0]
    assert status == "applied" and goal["name"] == "P1 Onboarding that captivates next step: week of 2026-11-02"
    assert goal["assigned_to_user_id"] == "u_ann" and goal["spec_solution_id"] == "sol_1" and goal["spec_metric_id"] == "met_1"
    assert "Current value: 61 (as of 2026-11-03, from Funnel report)" in goal["description"]


def test_a_source_the_reader_cannot_read_leaves_no_figure_on_the_goal_and_the_receipt_says_so():
    def unreadable(name):
        raise MetricSourceError("no file source holds it")
    client, registry, resolved = flow(unreadable)
    receipt, status = resolved.carry_out(registry, decide)
    description = client.created[0]["description"]
    assert "Current value: not read, the funnel source could not be read just now" in description
    assert "50" not in description and "as of" not in description
    assert "could not be read" in receipt and "state no value" in receipt.lower().replace("and state", "state")


def flow_with_record(reader, record):
    client = Client()
    registry = ToolRegistry(milestone_flow_tools(lambda: client, reader))
    original = SPEC["records"][1]["fields"]["record"]
    SPEC["records"][1]["fields"]["record"] = record
    try:
        resolved = cue.resolve(registry, "q1", "get the onboarding test moving", NOW, decide)
    finally:
        SPEC["records"][1]["fields"]["record"] = original
    return client, registry, resolved


def test_the_dated_reading_the_metric_record_holds_is_read_first_and_a_zero_stands_when_the_source_cannot_be_read():
    def unreadable(name):
        raise MetricSourceError("no file source holds it")
    client, registry, resolved = flow_with_record(unreadable, "Current value: 0 (as of 2026-11-02, from Admin user list)\nNo tagged accounts yet.")
    receipt, status = resolved.carry_out(registry, decide)
    assert "Current value: 0 (as of 2026-11-02, from Admin user list)" in client.created[0]["description"]
    assert "not read" not in client.created[0]["description"] and "recorded reading stands" in receipt


def test_a_source_reading_as_recent_or_newer_replaces_the_records_reading():
    newer = {"value": 3, "as_of": "2026-11-03", "source": "Funnel report", "detail": {}}
    client, registry, resolved = flow_with_record(lambda name: newer, "Current value: 0 (as of 2026-11-02, from Admin user list)")
    resolved.carry_out(registry, decide)
    assert "Current value: 3 (as of 2026-11-03, from Funnel report)" in client.created[0]["description"]


def test_a_reading_is_checked_the_same_way_whatever_reads_it():
    assert checked_reading('{"value": 7, "as_of": "2026-11-03", "source": "A"}')["value"] == 7
    for text in ("not json", '{"as_of": "2026-11-03", "source": "A"}', '{"value": 1, "as_of": "yesterday", "source": "A"}', '{"value": 1, "as_of": "2026-11-03"}'):
        try:
            checked_reading(text)
        except MetricSourceError:
            continue
        raise AssertionError(f"{text!r} was accepted as a reading")
    assert checked_reading('{"value": 1, "as_of": "2026-11-03"}', "Label")["source"] == "Label"


def test_the_value_recorded_in_the_hosts_store_is_read_before_the_named_source_and_a_missing_one_falls_through():
    asked = []
    recorded = {"value": 70, "as_of": "2026-11-02", "source": "Admin dashboard", "detail": {}}

    def recorded_reader(quest_id, metric_id):
        asked.append((quest_id, metric_id))
        if not recorded:
            raise MetricSourceError("no value has been recorded for this metric yet")
        return recorded
    source_reading = {"value": 61, "as_of": "2026-11-03", "source": "Funnel report", "detail": {}}
    client = Client()
    registry = ToolRegistry(milestone_flow_tools(lambda: client, lambda name: source_reading, recorded_reader=recorded_reader))
    resolved = cue.resolve(registry, "q1", "get the onboarding test moving", NOW, decide)
    resolved.carry_out(registry, decide)
    assert asked == [("q1", "met_1")] and "Current value: 70 (as of 2026-11-02, from Admin dashboard)" in client.created[0]["description"]
    recorded.clear()
    client2 = Client()
    registry2 = ToolRegistry(milestone_flow_tools(lambda: client2, lambda name: source_reading, recorded_reader=recorded_reader))
    resolved2 = cue.resolve(registry2, "q1", "get the onboarding test moving", NOW, decide)
    resolved2.carry_out(registry2, decide)
    assert "Current value: 61 (as of 2026-11-03, from Funnel report)" in client2.created[0]["description"]


def test_goal_write_and_metric_reads_are_not_offered_to_the_planner():
    """The flow's code step owns the goal write and the metric reads: the planner of an ordinary turn is never shown them, the two plain reads stay."""
    registry = ToolRegistry(milestone_flow_tools(lambda: None, lambda name: {}, recorded_reader=lambda quest, metric: {}))
    offered = {spec.name for spec in registry.offered()}
    hidden = {name for name in registry.names() if registry.get(name).on_request_only}
    assert hidden == {"create_or_update_quest_goal", "read_metric_source", "read_recorded_metric_value"}
    assert {name for name in registry.names() if registry.get(name).flow_owned} == hidden
    assert registry.get("list_quest_goals").on_request_only is False and registry.get("read_spec_records").on_request_only is False
    assert offered == {"list_quest_goals", "read_spec_records"}


def test_a_planner_call_to_a_flow_owned_tool_is_refused_and_writes_nothing():
    from types import SimpleNamespace

    from quest_ai_runner.the_intelligence.mind.context_intake.orchestrator_reads import ReadsMixin
    from quest_ai_runner.the_world.tools.tools import ToolContext

    written = []

    class Client:
        def __getattr__(self, name):
            written.append(name)
            raise AssertionError(f"the flow-owned tool must not run: {name}")

    class Emit:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

        def status(self, text):
            self.events.append(text)

    host = SimpleNamespace(tools=ToolRegistry(milestone_flow_tools(lambda: Client())), cfg=SimpleNamespace(max_reads_per_step=4))
    gathered = []
    ReadsMixin.run_tool_calls(host, [{"name": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title": "IG post"}}], gathered=gathered, exec_record=SimpleNamespace(facts=[]),
                              tool_ok_signatures=set(), emit=Emit(), steps=1, ctx=ToolContext(quest_id="q1"))
    assert written == []
    assert gathered and gathered[0]["tool_status"] == "refused" and "NOT RUN" in gathered[0]["text"]


def test_a_write_that_reads_first_runs_its_read_in_its_place_and_runs_once_the_read_is_in():
    from types import SimpleNamespace

    from quest_ai_runner.the_intelligence.mind.context_intake.orchestrator_reads import ReadsMixin
    from quest_ai_runner.the_world.tools.tools import ToolContext, ToolResult, ToolSpec

    ran = []

    def read(args, ctx):
        ran.append("read")
        return ToolResult(ok=True, text="1 workflow: Weekly check-in")

    def write(args, ctx):
        ran.append("write")
        return ToolResult(ok=True, text="created")

    registry = ToolRegistry([ToolSpec(name="list_things", handler=read, description="list"),
                             ToolSpec(name="make_thing", handler=write, description="make", mutates=True, reads_first=("list_things",))])

    class Emit:
        def emit(self, event):
            pass

        def status(self, text):
            pass

    host = SimpleNamespace(tools=registry, cfg=SimpleNamespace(max_reads_per_step=4))
    gathered = []
    call = {"name": "make_thing", "args": {"quest_id": "q1", "title": "Weekly check-in"}}
    ReadsMixin.run_tool_calls(host, [call], gathered=gathered, exec_record=SimpleNamespace(facts=[]), tool_ok_signatures=set(), emit=Emit(), steps=1,
                              ctx=ToolContext(quest_id="q1"))
    assert ran == ["read"], "the write waits for its read; the planner decides again from it"
    assert any("NOT RUN YET" in str(g.get("text")) for g in gathered)
    ReadsMixin.run_tool_calls(host, [call], gathered=gathered, exec_record=SimpleNamespace(facts=[]), tool_ok_signatures=set(), emit=Emit(), steps=2,
                              ctx=ToolContext(quest_id="q1"))
    assert ran == ["read", "write"]


def test_a_workflow_worded_nearly_the_same_on_the_same_schedule_is_already_there():
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import workflow_already_there
    weekly_monday = {"repeat": {"freq": "weekly", "interval": 1, "byWeekday": [0], "tz": "UTC"}}
    existing = [{"workflowId": "w1", "title": "Monday check: how the 1000 subscribers are doing",
                 "steps": [{"title": "Check how the 1000 subscribers are doing", "timing": weekly_monday}]}]
    new = [{"title": "Check how the 1000 paying subscribers are doing", "timing": {"repeat": {"freq": "weekly", "interval": 1, "byWeekday": [0], "tz": "America/Los_Angeles"}}}]
    assert workflow_already_there(existing, "Monday check: 1000 paying subscribers", new)["workflowId"] == "w1"
    other = [{"title": "Monday standup with the team", "timing": weekly_monday}]
    assert workflow_already_there(existing, "Standup", other) is None
    thursday = [{"title": "Check how the 1000 subscribers are doing", "timing": {"repeat": {"freq": "weekly", "interval": 1, "byWeekday": [3]}}}]
    assert workflow_already_there(existing, "Thursday check", thursday) is None
