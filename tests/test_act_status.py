"""Status first, then the work, then the metric before and after (``act_status``): an order that takes work beyond one reply sends
two messages in order, a question or a read-only order sends one, and a repeat finds what exists. Offline, scripted."""
from __future__ import annotations

from typing import Any, Dict, List

from hubofhubs.api import EVENT_INTERIM, EVENT_RESULT
from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.response_writer import act_status
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

from .conftest import StubProvider, StubRetrieval
from .test_tools import make_spec

GOAL = "- g1 [open] due 2026-12-01 (month) for Sam: Launch the app release | Done when: 100 installs. Current value: {value}."


def registry(state: Dict[str, Any], writes: List[Dict[str, Any]], *, existing: bool = False) -> ToolRegistry:
    def read(args, ctx):  # noqa: ARG001
        return ToolResult(ok=True, text="1 matching goals on quest q1\n" + GOAL.format(value=state["value"]))

    def write(args, ctx):  # noqa: ARG001
        if existing:
            return ToolResult(ok=True, status=ToolStatus.NOOP, text="The weekly check-in is already on the quest")
        writes.append(args)
        state["value"] = args["value"]
        return ToolResult(ok=True, text="Updated the goal")

    props = {"type": "object", "properties": {"quest_id": {"type": "string"}, "value": {"type": "string"}}}
    return ToolRegistry([make_spec("list_quest_goals", handler=read, mutates=False, parameters=props),
                         make_spec("update_goal", handler=write, mutates=True, parameters=props)])


def act_plan(intent: str, *calls: Dict[str, Any], rationale: str = "Update the goal now") -> Dict[str, Any]:
    return {"action": "tool", "rationale": rationale, "user_intent": intent, "tool_calls": list(calls)}


READ = {"name": "list_quest_goals", "args": {"quest_id": "q1"}}
WRITE = {"name": "update_goal", "args": {"quest_id": "q1", "value": "15"}}


def run(decisions, tools, message="make progress on the app release", **cfg):
    provider = StubProvider(decisions=decisions)
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=tools,
                        config=OrchestratorConfig(interim_replies=True, overseer=False, min_seconds_between_interims=0.0, **cfg))
    events: List[Dict[str, Any]] = []
    orch.run(message, sink=StreamSink(events.append), quest_id="q1")
    return events


def test_an_act_request_sends_the_status_first_then_the_result_with_the_metric_before_and_after():
    state, writes = {"value": "12"}, []
    events = run([act_plan("act", READ), act_plan("act", WRITE), {"action": "answer", "rationale": "done", "user_intent": "act"}],
                 registry(state, writes))
    interim = [e for e in events if e["type"] == EVENT_INTERIM]
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert len(interim) == 1 and interim[0]["data"]["status_first"] is True and interim[0]["data"]["seq"] == 1
    assert "Current value: 12." in interim[0]["text"] and "Done when: 100 installs." in interim[0]["text"]
    assert interim[0]["text"].startswith("Where this stands:") and "\n\nNow: " in interim[0]["text"]
    assert events.index(interim[0]) < events.index(result) and writes == [WRITE["args"]]
    assert "was 12, now 15." in result["text"]            # the second reading is a read by code, after the write


def test_an_information_request_sends_one_message():
    state = {"value": "12"}
    events = run([act_plan("inform", READ), {"action": "answer", "rationale": "say it", "user_intent": "inform"}], registry(state, []))
    assert not [e for e in events if e["type"] == EVENT_INTERIM]
    assert "Metric (" not in [e for e in events if e["type"] == EVENT_RESULT][-1]["text"]


def test_an_act_request_that_only_reads_sends_one_message():
    events = run([act_plan("act", READ), {"action": "answer", "rationale": "say it", "user_intent": "act"}], registry({"value": "12"}, []))
    assert not [e for e in events if e["type"] == EVENT_INTERIM]


def test_a_repeated_act_request_reports_what_is_already_there_without_a_second_write():
    state, writes = {"value": "15"}, []
    events = run([act_plan("act", READ), act_plan("act", WRITE), {"action": "answer", "rationale": "done", "user_intent": "act"}],
                 registry(state, writes, existing=True))
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert writes == [] and "It's already set." in result["text"] and "The weekly check-in is already on the quest" not in result["text"]
    assert "unchanged, still 15." in result["text"]


def test_no_recorded_value_is_said_not_filled_in():
    reading = act_status.readings_in([{"text": "- g2 [open] due x (month) for Sam: Plan | Done when: 5 calls"}])[0]
    assert reading.value == act_status.NOT_RECORDED and not reading.recorded
    assert "Current value: not recorded yet." in act_status.status_texts(reading, [], "", evidence="")["text"]


def test_the_decision_is_structural():
    base = dict(enabled=True, user_turn=True, user_intent="act", action="tool", mutating_calls=1, already_sent=False)
    assert act_status.should_send_status(**base)
    for over in (dict(user_intent="inform"), dict(user_intent=None), dict(mutating_calls=0), dict(action="read"), dict(already_sent=True),
                 dict(enabled=False), dict(user_turn=False)):
        assert not act_status.should_send_status(**{**base, **over})
    assert act_status.should_send_status(**{**base, "action": "deep", "mutating_calls": 0})


def test_voice_keeps_each_message_to_a_line_and_a_figure_the_records_lack_is_dropped():
    reading = act_status.MetricReading("g1", "Launch", "100 installs", "12")
    short = act_status.status_texts(reading, ["a long fact"], "ship it", evidence="ship it", voice=True)
    assert "Done when" not in short["status"] and "fact" not in short["status"]
    kept = act_status.status_texts(reading, [], "Add 40 invented installs", evidence="the request")
    assert kept["next"] == act_status.GENERIC_NEXT


def test_every_request_measured_carries_the_metric_records_own_readings_into_both_messages():
    from quest_ai_runner.the_intelligence.critical_thinking.area import CaseIteration
    state = {"value": "0"}                      # no goal line states a figure: the readings come from the metric record
    provider = StubProvider(decisions=[act_plan("act", WRITE), {"action": "answer", "rationale": "done", "user_intent": "act"}])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=registry(state, []),
                        goal_iteration=CaseIteration({"has_metric": True, "before": 12.0, "after": 15.0}),
                        config=OrchestratorConfig(interim_replies=True, overseer=False, measure_every_request=True))
    events: List[Dict[str, Any]] = []
    orch.run("make progress on the app release", sink=StreamSink(events.append), quest_id="q1")
    first = [e for e in events if e["type"] == EVENT_INTERIM][0]
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert "Current value: 12." in first["text"] and "was 12, now 15." in result["text"]
    assert [m["after"] for m in result["data"].get("metrics") or [] if "after" in m] in ([15.0], [])      # the rows ride on the result


def test_the_closing_metric_line_names_the_metric_and_never_pastes_the_records_raw_text():
    raw = "Re-read 2026-10-31 22:01 UTC (Payal pass): still No. The listing was checked again and nothing has changed since the earlier pass."
    before = act_status.MetricReading("g1", "P3 Mobile app live", "App Store listing live", raw)
    gone = act_status.closing_lines(before, None, reread=True, already=[])
    assert gone == "Metric (App Store listing live): was No; it could not be read again just now."
    same = act_status.closing_lines(before, act_status.MetricReading("g1", "P3 Mobile app live", "App Store listing live", raw), reread=True, already=[])
    assert same == "Metric (App Store listing live): unchanged, still No."
    moved = act_status.closing_lines(before, act_status.MetricReading("g1", "", "", "Yes"), reread=True, already=[])
    assert moved == "Metric (App Store listing live): was No, now Yes." and "Payal" not in moved and "UTC" not in moved


def test_the_code_steps_own_receipt_is_not_listed_again_as_already_in_place():
    from quest_ai_runner.the_intelligence.mind.delegation.orchestrator_capabilities import receipt_observation
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import already_in_place
    code = receipt_observation("create_or_update_quest_goal", "RECEIPT written by code for X. This week's next step for X was already set.", "noop")
    other = {"kind": "query", "locator": "tool:update_goal", "tool_status": "noop", "text": "The weekly check-in is already on the quest"}
    assert already_in_place([code, other]) == ["It's already set."]


def test_an_already_there_workflow_is_said_in_one_plain_sentence_with_its_title_and_schedule_and_no_ids_or_tool_text():
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import already_in_place
    receipt = {"kind": "query", "locator": "tool:create_quest_workflow", "tool_status": "noop",
               "text": "TOOL create_quest_workflow: Already there: the workflow 'Weekly check-in' (qwf_d9df4f414728) on quest quest_e8257dd0aa3d already has every one of these steps",
               "tool_data": {"workflowId": "qwf_d9df4f414728", "title": "Weekly check-in: Thursday office hour",
                             "steps": [{"title": "s", "timing": {"repeat": {"freq": "weekly", "interval": 1, "byWeekday": [3]}}}]}}
    assert already_in_place([receipt]) == ["It's already set: Weekly check-in: Thursday office hour, weekly on Thursday."]
    goal = {"locator": "tool:create_or_update_quest_goal", "tool_status": "noop", "tool_data": {"state": "unchanged", "goal": {"name": "Ship the release next step"}}}
    assert already_in_place([goal]) == ["It's already set: Ship the release next step."]
    leaky = {"locator": "tool:x", "tool_status": "noop", "tool_data": {"title": "esr_fbcc56e182f7 thing"}}
    assert already_in_place([leaky]) == ["It's already set."]
    tail = act_status.closing_lines(None, None, reread=False, already=already_in_place([receipt]))
    assert tail == "It's already set: Weekly check-in: Thursday office hour, weekly on Thursday." and "qwf_" not in tail and "quest_" not in tail


def test_the_first_message_without_a_milestone_is_a_plain_line_of_what_the_turn_looked_at_never_the_planners_fact_dump():
    gathered = [{"locator": "tool:list_quest_workflows", "tool_status": "applied"}, {"locator": "tool:create_quest_workflow", "tool_status": "refused"}]
    found = act_status.read_summary(gathered)
    assert found == "I looked at quest workflows."
    texts = act_status.status_texts(None, ["s1 [pending] for Someone, window 2026-11-12T08:00:00+00:00 to 2026-11-12T09:00:00+00:00"], "Create the step esr_ab12cd34ef56",
                                    evidence="s1 [pending] 2026-11-12T08:00:00+00:00 esr_ab12cd34ef56", found=found)
    assert "2026-11-12T" not in texts["text"] and "esr_" not in texts["text"] and texts["status"] == "I looked at quest workflows."
    assert act_status.read_summary([]) == ""
