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


def test_a_reading_is_checked_the_same_way_whatever_reads_it():
    assert checked_reading('{"value": 7, "as_of": "2026-11-03", "source": "A"}')["value"] == 7
    for text in ("not json", '{"as_of": "2026-11-03", "source": "A"}', '{"value": 1, "as_of": "yesterday", "source": "A"}', '{"value": 1, "as_of": "2026-11-03"}'):
        try:
            checked_reading(text)
        except MetricSourceError:
            continue
        raise AssertionError(f"{text!r} was accepted as a reading")
    assert checked_reading('{"value": 1, "as_of": "2026-11-03"}', "Label")["source"] == "Label"
