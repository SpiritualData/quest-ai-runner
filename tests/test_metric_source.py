"""A metric's current value read from the source its record names: the tool, the lane config that maps a name to a command, the one in-place ``Current value:`` line of the keyed
next step, and the owners taken from ``owner_user_ids``. Offline: the source commands are tiny python programs printed inline, and Quest is a dictionary."""
import datetime
import json
import sys

import pytest

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import tools_metric_source as source_tool
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec, build_tool_registry

NOW = datetime.datetime(2026, 11, 4, 18, 0, tzinfo=datetime.timezone.utc)
PRINTS = 'import json;print(json.dumps({"value": 7, "as_of": "2026-11-03", "source": "A funnel report", "detail": {"stages": 2}}))'


def sources(**entries):
    return json.dumps({name: {"command": command, "timeout_seconds": 20} for name, command in entries.items()})


def env_with(**entries):
    return {source_tool.ENV_VAR: sources(**entries)}


def test_the_tool_runs_the_mapped_command_and_returns_exactly_what_it_printed():
    env = env_with(funnel=[sys.executable, "-I", "-c", PRINTS])
    spec = source_tool.read_metric_source_spec(env)
    result = spec.handler({"name": "funnel"}, None)
    assert result.ok and result.data["value"] == 7 and result.data["as_of"] == "2026-11-03" and result.data["source"] == "A funnel report"
    assert result.data["detail"] == {"stages": 2} and "7 (as of 2026-11-03, from A funnel report)" in result.text
    assert spec.mutates is False


def test_no_configured_source_means_no_tool_and_a_registry_offers_it_only_when_configured():
    assert source_tool.read_metric_source_spec({}) is None
    assert build_tool_registry({}).get("read_metric_source") is None
    assert build_tool_registry(env_with(funnel=[sys.executable, "-c", PRINTS])).get("read_metric_source") is not None


@pytest.mark.parametrize("program,why", [
    ("raise SystemExit('boom')", "failed"),
    ("print('not json')", "no JSON object"),
    ('import json;print(json.dumps({"as_of": "2026-11-03", "source": "x"}))', "no value"),
    ('import json;print(json.dumps({"value": 1, "as_of": "yesterday", "source": "x"}))', "as_of"),
    ('import json;print(json.dumps({"value": 1, "as_of": "2026-11-03"}))', "source label"),
])
def test_a_failing_or_malformed_source_is_a_gap_in_its_own_words_never_a_value(program, why):
    spec = source_tool.read_metric_source_spec(env_with(funnel=[sys.executable, "-I", "-c", program]))
    result = spec.handler({"name": "funnel"}, None)
    assert not result.ok and why in result.text and "Record no value" in result.text and result.data is None


def test_an_unknown_name_lists_what_is_configured():
    result = source_tool.read_metric_source_spec(env_with(funnel=[sys.executable, "-c", PRINTS])).handler({"name": "other"}, None)
    assert not result.ok and "funnel" in result.text


def test_the_lane_config_file_maps_a_source_name_to_a_command(tmp_path, monkeypatch):
    from quest_ai_runner.the_body.configuration.config import RunnerConfig
    from quest_ai_runner.the_body.configuration.config_environment import apply_config_environment
    monkeypatch.delenv(source_tool.ENV_VAR, raising=False)
    path = tmp_path / "lane.toml"
    path.write_text('[metric_sources.funnel]\ncommand = ["python3", "-I", "/somewhere/read.py"]\nlabel = "A funnel report"\ntimeout_seconds = 30\n')
    apply_config_environment(RunnerConfig.from_file(path))
    mapped = source_tool.configured_sources()
    assert mapped == {"funnel": {"command": ["python3", "-I", "/somewhere/read.py"], "label": "A funnel report", "timeout_seconds": 30.0}}
    monkeypatch.delenv(source_tool.ENV_VAR, raising=False)


def test_a_dry_run_lets_only_the_configured_command_start(monkeypatch):
    from quest_ai_runner.the_world.tools import dry_run_writes
    monkeypatch.setenv(dry_run_writes.ENV, "1")
    monkeypatch.setitem(dry_run_writes.STATE, "read_commands", set())
    with pytest.raises(dry_run_writes.DryRunBlocked):
        dry_run_writes.check_process(["python3", "-I", "/x.py"])
    seen = []
    source_tool.read_source("funnel", env_with(funnel=["python3", "-I", "/x.py"]), run=lambda argv, **kw: seen.append(argv) or type("P", (), {"returncode": 0, "stdout": PRINTS_JSON, "stderr": ""})())
    dry_run_writes.check_process(["python3", "-I", "/x.py"])
    assert seen == [["python3", "-I", "/x.py"]]
    with pytest.raises(dry_run_writes.DryRunBlocked):
        dry_run_writes.check_process(["python3", "-I", "/other.py"])


PRINTS_JSON = json.dumps({"value": 1, "as_of": "2026-11-03", "source": "s"})

# --- the keyed next step -----------------------------------------------------------------------------------------------------------------------------------

TABLES = {
    "solutions": {"esr_p": {"name": "P4 Funnel stats", "milestones": "Real data at every funnel point.", "purpose": "Owners: Ann.", "owner_user_ids": ["u_ann", "u_bo"]}},
    "metrics": {"esr_mp": {"name": "Funnel coverage", "record": "0 of 6", "solution_ids": ["esr_p"], "source": "funnel", "measured_through": "2026-10-31"}},
}
CUE = "compute and record the funnel stats current value"


class Quest:
    """Goals by title, and the tools over them: the listing, the ensure-one write (description replaced when it differs) and the source."""

    def __init__(self, readings):
        self.goals, self.readings, self.writes = {}, list(readings), []

    def registry(self, with_source=True):
        def listing(args, ctx):
            return ToolResult(ok=True, data=[{"id": f"g{i}", "title": t, "text": g["description"], "spec_solution_id": "esr_p", "completed": False, "abandoned": False}
                                              for i, (t, g) in enumerate(self.goals.items())], text="listed")

        def write(args, ctx):
            self.writes.append(dict(args))
            state = "updated" if args["title"] in self.goals else "created"
            self.goals[args["title"]] = {"description": args["description"], "assignee": args.get("assignee")}
            return ToolResult(ok=True, data={"state": state, "goal": {"id": "g0"}}, text=f"{state} goal")

        def read(args, ctx):
            item = self.readings.pop(0)
            if item is None:
                return ToolResult(ok=False, text="Not read: the funnel source did not answer within 20 seconds. Record no value for it.")
            return ToolResult(ok=True, data={"name": args["name"], **item}, text="read")

        def spec_read(args, ctx):
            return ToolResult(ok=True, data={"records": TABLES, "time_zone": "UTC"}, text="read")
        specs = [ToolSpec(name="read_spec_records", description="d", mutates=False, handler=spec_read),
                 ToolSpec(name="list_quest_goals", description="d", mutates=False, handler=listing),
                 ToolSpec(name="create_or_update_quest_goal", description="d", handler=write)]
        if with_source:
            specs.append(ToolSpec(name="read_metric_source", description="d", mutates=False, handler=read))
        return ToolRegistry(specs)

    def run(self, with_source=True):
        registry = self.registry(with_source)
        resolved = cue.resolve(registry, "q1", CUE, NOW)
        return resolved, resolved.carry_out(registry)[0]


def value_lines(description):
    return [line for line in description.split("\n") if line.startswith("Current value:")]


def reading(value, as_of="2026-11-04"):
    return {"value": value, "as_of": as_of, "source": "A funnel report", "detail": {}}


def test_the_value_is_one_line_updated_in_place_on_a_repeat_never_a_second_line_or_goal():
    quest = Quest([reading("0 of 6 stages"), reading("0 of 6 stages"), reading("2 of 6 stages", "2026-11-05")])
    first, receipt = quest.run()
    assert list(quest.goals) == ["P4 Funnel stats next step: week of 2026-11-02"]
    title = next(iter(quest.goals))
    assert value_lines(quest.goals[title]["description"]) == ["Current value: 0 of 6 stages (as of 2026-11-04, from A funnel report)"]
    assert "0 of 6 stages" in receipt and "2026-11-04" in receipt and "A funnel report" in receipt
    quest.run()
    assert len(quest.goals) == 1 and value_lines(quest.goals[title]["description"]) == ["Current value: 0 of 6 stages (as of 2026-11-04, from A funnel report)"]
    quest.run()
    assert len(quest.goals) == 1 and value_lines(quest.goals[title]["description"]) == ["Current value: 2 of 6 stages (as of 2026-11-05, from A funnel report)"]


def test_a_source_that_cannot_be_read_keeps_the_measured_line_and_says_so():
    quest = Quest([reading(41), None])
    quest.run()
    title = next(iter(quest.goals))
    kept = value_lines(quest.goals[title]["description"])
    resolved, receipt = quest.run()
    assert value_lines(quest.goals[title]["description"]) == kept and "could not be read" in receipt and "state no value" in receipt
    assert resolved.reading is None and "did not answer" in resolved.reading_gap


def test_a_lane_without_the_tool_says_which_source_it_could_not_read():
    quest = Quest([])
    resolved, receipt = quest.run(with_source=False)
    assert "read_metric_source" in resolved.reading_gap and "could not be read" in receipt


def test_with_value_line_replaces_or_adds_exactly_one_line():
    assert cue.with_value_line("Next step.", "Current value: 1 (as of 2026-11-01, from s)") == "Next step.\nCurrent value: 1 (as of 2026-11-01, from s)"
    twice = cue.with_value_line("Next step.\nCurrent value: old\nCurrent value: older\nDraft: x", "Current value: 2 (as of 2026-11-02, from s)")
    assert twice == "Next step.\nCurrent value: 2 (as of 2026-11-02, from s)\nDraft: x"


def test_the_step_is_assigned_to_a_user_in_owner_user_ids_and_the_facts_name_the_source():
    quest = Quest([reading(5)])
    resolved, _ = quest.run()
    assert resolved.owners == ["u_ann", "u_bo"] and resolved.source == "funnel"
    assert quest.writes[0]["assignee"] == "u_ann"
    assert "owner_user_ids: u_ann, u_bo" in resolved.facts and "Metric source: funnel" in resolved.facts and "covers through 2026-10-31" in resolved.facts
