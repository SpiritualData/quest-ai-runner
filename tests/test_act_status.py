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
    assert len(interim) == 1 and interim[0]["data"]["status_first"] is True and interim[0]["data"]["announce"] is True and interim[0]["data"]["seq"] == 1
    assert "I am looking at quest goals first." in interim[0]["text"]       # sent as soon as the plan is known to be an act, before the read finishes
    assert interim[0]["text"].startswith("Where this stands:") and "\n\nNow: " in interim[0]["text"]
    assert events.index(interim[0]) < events.index(result) and writes == [WRITE["args"]]
    assert "was 12, now 15." in result["text"]            # the second reading is a read by code, after the write


def test_an_information_request_sends_one_message():
    state = {"value": "12"}
    events = run([act_plan("inform", READ), {"action": "answer", "rationale": "say it", "user_intent": "inform"}], registry(state, []))
    assert not [e for e in events if e["type"] == EVENT_INTERIM]
    assert "Metric (" not in [e for e in events if e["type"] == EVENT_RESULT][-1]["text"]


def test_an_act_request_that_only_reads_still_sends_its_status_first_before_the_read_finishes():
    events = run([act_plan("act", READ), {"action": "answer", "rationale": "say it", "user_intent": "act"}], registry({"value": "12"}, []))
    interim = [e for e in events if e["type"] == EVENT_INTERIM]
    assert len(interim) == 1 and "I am looking at quest goals first." in interim[0]["text"] and "?" not in interim[0]["text"]
    assert events.index(interim[0]) < events.index([e for e in events if e["type"] == EVENT_RESULT][-1])


def test_a_repeated_act_request_reports_what_is_already_there_without_a_second_write():
    state, writes = {"value": "15"}, []
    events = run([act_plan("act", READ), act_plan("act", WRITE), {"action": "answer", "rationale": "done", "user_intent": "act"}],
                 registry(state, writes, existing=True))
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert writes == [] and "It's already set." in result["text"] and "The weekly check-in is already on the quest" not in result["text"]
    assert "unchanged: 15." in result["text"]


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


def test_the_closing_metric_line_is_one_clean_line_or_none_and_never_a_piece_of_the_records_text():
    raw = "Re-read 2026-10-31 22:01 UTC (Pat pass): still No. The listing was checked again and nothing has changed since the earlier pass."
    sentence = "50.0 percent (5 of 10) of OUTSIDE new sign-ups in the last 30 days have created a first quest"
    fragment = "0 of 6 on the working list (niche videos 1 to 6), read 2026-"
    short = lambda value: act_status.MetricReading("g1", "P3 Mobile app live", "App Store listing live", value)
    assert act_status.closing_lines(short(raw), None, reread=True, already=[]) == ""                       # not read again: no line
    assert act_status.closing_lines(short(raw), short(raw), reread=True, already=[]) == ""                 # a sentence is not a value: no line
    assert act_status.closing_lines(short(sentence), short(sentence), reread=True, already=[]) == "Metric (P3 Mobile app live): unchanged: 50.0 percent."     # the leading figure, not the sentence
    assert act_status.closing_lines(short(fragment), short(fragment), reread=True, already=[]) == "Metric (P3 Mobile app live): unchanged: 0 of 6."          # never a cut-off fragment
    assert act_status.closing_lines(short("No"), short("No"), reread=True, already=[]) == "Metric (P3 Mobile app live): unchanged: No."
    assert act_status.closing_lines(short("still No"), short("Yes"), reread=True, already=[]) == "Metric (P3 Mobile app live): was No, now Yes."
    assert act_status.closing_lines(short("3"), short("5"), reread=True, already=["It's already set."]) == "Metric (P3 Mobile app live): was 3, now 5.\nIt's already set."


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


WORKFLOW_THERE = {"workflowId": "qwf_1", "title": "Weekly check-in: Thursday office hour", "steps": [{
    "stepId": "s1", "status": "pending", "timing": {"kind": "window", "startsAt": "2026-11-12T08:00:00+00:00", "endsAt": "2026-11-13T07:59:00+00:00",
                                                   "repeat": {"freq": "weekly", "interval": 1, "byWeekday": [3]}},
    "window": {"startsAt": "2026-11-12T08:00:00+00:00", "endsAt": "2026-11-13T07:59:00+00:00"}}]}


def test_the_already_there_sentence_carries_the_schedule_and_the_next_occurrence_from_the_structured_step():
    entry = {"locator": "tool:create_quest_workflow", "tool_status": "noop", "tool_data": WORKFLOW_THERE}
    assert act_status.already_phrase(entry) == "It's already set: Weekly check-in: Thursday office hour, weekly on Thursday (next: Thursday 12 November)."


def test_the_final_reply_of_an_ensure_turn_that_found_everything_there_is_exactly_the_code_sentence_and_nothing_else_changes_it():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import settle_already_set
    from quest_ai_runner.the_world.tools.tools import ToolRegistry
    from .test_tools import make_spec
    tools = ToolRegistry([make_spec("create_quest_workflow", handler=lambda a, c: ToolResult(ok=True), mutates=True),
                          make_spec("list_quest_workflows", handler=lambda a, c: ToolResult(ok=True), mutates=False)])
    orch = SimpleNamespace(tools=tools)
    there = {"kind": "query", "locator": "tool:create_quest_workflow", "tool_status": "noop", "tool_data": WORKFLOW_THERE}
    read = {"kind": "query", "locator": "tool:list_quest_workflows", "tool_status": "applied"}
    reply = "It's already on the quest as a weekly check-in, assigned to you, pending, and repeats every Thursday. The next one is Thursday, so I did not add a duplicate."
    rs, res = SimpleNamespace(gathered=[read, there], status_first=None), SimpleNamespace(kind="answer", text=reply)
    assert settle_already_set(orch, rs, res) is True
    assert res.text == "It's already set: Weekly check-in: Thursday office hour, weekly on Thursday (next: Thursday 12 November), on this quest's Workflows page."
    # a write that was applied, or refused, or made by the code step keeps the model's text
    for other in ({**there, "tool_status": "applied"}, {**there, "tool_status": "refused"}, {**there, "by_code": True}):
        kept = SimpleNamespace(kind="answer", text=reply)
        assert settle_already_set(orch, SimpleNamespace(gathered=[read, other], status_first=None), kept) is False and kept.text == reply
    # a read only, a non-answer, or a turn that reads a milestone's measure keep it too
    for rs_other, res_other in ((SimpleNamespace(gathered=[read], status_first=None), SimpleNamespace(kind="answer", text=reply)),
                                (SimpleNamespace(gathered=[read, there], status_first=None), SimpleNamespace(kind="confirm", text=reply)),
                                (SimpleNamespace(gathered=[read, there], status_first={"before": object()}), SimpleNamespace(kind="answer", text=reply))):
        assert settle_already_set(orch, rs_other, res_other) is False and res_other.text == reply


def test_a_planner_that_only_reads_an_existing_workflow_gets_the_idempotent_write_added_by_code_and_the_reply_is_the_code_sentence():
    created = []
    row = {"workflow_id": "qwf_1", "workflow_title": "Weekly check-in: Thursday office hour", "step_id": "s1", "title": "Thursday office hour: weekly check-in",
           "assignee_name": "Ann Example", "assignee_user_id": "u1", "starts_at": "2026-11-12T08:00:00+00:00", "ends_at": "2026-11-13T07:59:00+00:00",
           "repeats": "weekly on Thursday", "repeat": {"freq": "weekly", "interval": 1, "byWeekday": [3]}, "status": "pending", "due": "2026-11-13T07:59:00+00:00"}

    def read(args, ctx):  # noqa: ARG001
        return ToolResult(ok=True, data=[row], text="1 matching workflow steps")

    def ensure(args, ctx):  # noqa: ARG001
        created.append(args)
        return ToolResult(ok=True, status=ToolStatus.NOOP, data=dict(WORKFLOW_THERE), text="Already there: the workflow (qwf_1) already has every one of these steps")
    props = {"type": "object", "properties": {"quest_id": {"type": "string"}, "title": {"type": "string"}, "steps": {"type": "array"}}}
    tools = ToolRegistry([make_spec("list_quest_workflows", handler=read, mutates=False, parameters=props),
                          make_spec("create_quest_workflow", handler=ensure, mutates=True, parameters=props)])
    plan_read = {"action": "tool", "rationale": "look first", "user_intent": "act", "tool_calls": [{"name": "list_quest_workflows", "args": {"quest_id": "q1"}}]}
    events = run([plan_read, {"action": "answer", "rationale": "it exists", "user_intent": "act"}], tools, message="make sure the Thursday check-in exists")
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert created and created[0]["title"] == "Weekly check-in: Thursday office hour" and created[0]["steps"][0]["repeat"] == "weekly" and created[0]["steps"][0]["by_weekday"] == [3]
    assert result["text"] == "It's already set: Weekly check-in: Thursday office hour, weekly on Thursday (next: Thursday 12 November), on this quest's Workflows page."
    assert len([e for e in events if e["type"] == EVENT_INTERIM]) == 1


def test_no_ensure_call_is_built_from_a_read_of_several_workflows_or_none():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import ensure_from_read
    from .test_tools import make_spec
    tools = ToolRegistry([make_spec("create_quest_workflow", handler=lambda a, c: ToolResult(ok=True), mutates=True)])
    two = {"locator": "tool:list_quest_workflows", "tool_data": [{"workflow_id": "a", "workflow_title": "A", "title": "s"}, {"workflow_id": "b", "workflow_title": "B", "title": "s"}]}
    assert ensure_from_read.call_from_reads([two], tools, "q1") is None and ensure_from_read.call_from_reads([], tools, "q1") is None
    assert ensure_from_read.call_from_reads([two], ToolRegistry([]), "q1") is None


def test_the_already_set_sentence_says_where_it_is_with_the_quests_name_and_no_id():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import settle_already_set
    from .test_tools import make_spec
    tools = ToolRegistry([make_spec("create_quest_workflow", handler=lambda a, c: ToolResult(ok=True), mutates=True),
                          make_spec("list_quest_workflows", handler=lambda a, c: ToolResult(ok=True), mutates=False)])
    there = {"kind": "query", "locator": "tool:create_quest_workflow", "tool_status": "noop", "tool_data": WORKFLOW_THERE}
    read = {"kind": "query", "locator": "tool:list_quest_workflows", "tool_status": "applied", "tool_data": [{"workflow_id": "qwf_1", "quest_name": "Launch Signups"}]}
    res = SimpleNamespace(kind="answer", text="long model text")
    assert settle_already_set(SimpleNamespace(tools=tools), SimpleNamespace(gathered=[read, there], status_first=None), res)
    assert res.text == ("It's already set: Weekly check-in: Thursday office hour, weekly on Thursday (next: Thursday 12 November), "
                        "on the Launch Signups quest's Workflows page.")
    assert "quest_" not in res.text and "qwf_" not in res.text
    nameless = SimpleNamespace(kind="answer", text="x")
    settle_already_set(SimpleNamespace(tools=tools), SimpleNamespace(gathered=[{**read, "tool_data": [{"workflow_id": "qwf_1"}]}, there], status_first=None), nameless)
    assert nameless.text.endswith("(next: Thursday 12 November), on this quest's Workflows page.")


def test_the_workflow_listing_rows_carry_the_quests_plain_name():
    from quest_ai_runner.the_body.hands.quest_operations import tools_quest_ops as ops
    from quest_ai_runner.the_world.tools.tools import ToolContext
    from .test_create_quest_workflow_tool import PRODUCTION_SHAPED, ListingClient

    class Named(ListingClient):
        def get_my_quest(self, quest_id):
            return {"state": {"outcome": "Launch Signups: reach one thousand paying subscribers by spring"}}
    spec = ops.list_quest_workflows_spec({"QUEST_BASE_URL": "https://example.invalid", "QUEST_API_KEY": "k"}, client_factory=lambda: Named(PRODUCTION_SHAPED))
    rows = spec.handler({}, ToolContext(quest_id="q1")).data
    assert rows[0]["quest_name"] == "Launch Signups"


def test_a_repeated_call_with_no_data_of_its_own_does_not_add_a_second_already_set_sentence():
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import already_in_place
    full = {"locator": "tool:create_quest_workflow", "tool_status": "noop", "tool_data": {"title": "Weekly check-in"}}
    repeat = {"locator": "tool:create_quest_workflow", "tool_status": "noop", "text": "ALREADY SUCCEEDED earlier this turn"}
    phrases = already_in_place([full, repeat])
    assert len(phrases) == 1 and "Weekly check-in" in phrases[0]
