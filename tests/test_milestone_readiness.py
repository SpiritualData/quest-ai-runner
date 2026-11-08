"""The readiness stage of a milestone's metric (counted from records only), the slot check on a kept draft, the owner's name, the closing line and the rule that
the reply never ends by asking the chat user. Synthetic records and a fake tool registry stand in for Quest."""
import datetime
import json
import re
from types import SimpleNamespace

import pytest

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_readiness as readiness
from quest_ai_runner.the_intelligence.mind.response_writer import act_status
from quest_ai_runner.the_intelligence.mind.response_writer import orchestrator_turn_messages as messages
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

@pytest.fixture(autouse=True)
def cheap_model(monkeypatch):
    """The turn's one cheap model call, answered offline: the milestone judgment picks the first candidate listed, any other question is answered NONE."""
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator

    def fake(self, prompt):
        found = re.search(r"^(esr_\w+):", prompt, re.M) if "These are milestones of the quest" in prompt else None
        return found.group(1) if found else "NONE"
    monkeypatch.setattr(Orchestrator, "cheap_decision", fake)


TABLES = {
    "solutions": {"esr_c": {"name": "P3 Partner users", "milestones": "100 new users come through a partner.", "owner_user_ids": ["u_ops"],
                            "purpose": "Travis runs partner outreach.", "outputs": "A partner pitch."}},
    "metrics": {"esr_mc": {"name": "P3 users via partner", "record": "0 of 100", "solution_ids": ["esr_c"]}},
}
NOW = datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc)
FULL = {"title": "t", "assignee_user_id": "u_ops", "due": "2026-11-08", "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc",
        "text": "Next.\nHuman step: Send the pitch.\nDraft: Hello partner, here is what your audience gets."}


def measure(goal):
    return readiness.measure(goal, owners=["u_ops"], solution_id="esr_c", metric_ids=["esr_mc"], monday="2026-11-02", due="2026-11-08")


def test_the_stage_counts_each_item_from_the_goal_and_names_it():
    assert measure(None).met == () and measure(FULL).met == tuple(key for key, _ in readiness.ITEMS)
    assert measure(dict(FULL, assignee_user_id="u_other")).met == ("due", "links", "deliverable", "human_step")
    assert "due" not in measure(dict(FULL, due="2026-11-20")).met and "links" not in measure(dict(FULL, spec_metric_id=None)).met
    assert "human_step" not in measure(dict(FULL, text="Draft: Hello.")).met
    assert measure(FULL).text().startswith("5 of 5 (an owner from the milestone's record; a due date this week;")
    assert measure(None).text() == "0 of 5 (none held yet)"
    assert measure(None).describe() == "none held"
    assert measure(FULL).describe() == ("all held: an owner from the milestone's record, a due date this week, the solution and metric links, a concrete kept draft, the human step named")
    partial = measure(dict(FULL, due="2026-11-20", text="Draft: Hello."))
    assert partial.describe() == ("held: an owner from the milestone's record, the solution and metric links, a concrete kept draft; "
                                  "still missing: a due date this week, the human step named")
    assert not any(ch.isdigit() for ch in partial.describe())


def test_a_goal_as_a_write_returns_it_reads_the_same_as_one_as_the_listing_carries_it():
    written = {"assigned_to_user_id": "u_ops", "scheduled_date": "2026-11-08", "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "description": FULL["text"]}
    assert measure(written) == measure(FULL)


def test_slots_are_found_structurally_and_checkboxes_and_links_are_not_slots():
    assert readiness.placeholder_slots("Try [Integration name] at [link] or {{name}}.") == ("[Integration name]", "[link]", "{{name}}")
    assert readiness.placeholder_slots("- [ ] one\n- [x] two\nSee [the page](https://example.invalid).") == ()
    assert not measure(dict(FULL, text="Draft: Try [Integration name] now.")).met.count("deliverable")


def tools(calls, goals=(), member="SpiritOps"):
    def write(args, ctx):
        calls.append(dict(args))
        goal = {"id": "g1", "name": args["title"], "assigned_to_user_id": "u_ops", "assignee_name": member, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc",
                "period": args["period"], "scheduled_date": args["due_date"]}
        return ToolResult(ok=True, status=ToolStatus.APPLIED, data={"state": "created", "goal": goal}, text="Created.")
    return ToolRegistry([
        ToolSpec(name="read_spec_records", description="d", mutates=False, handler=lambda a, c: ToolResult(ok=True, data={"records": TABLES, "time_zone": "UTC"}, text="r")),
        ToolSpec(name="list_quest_goals", description="d", mutates=False, handler=lambda a, c: ToolResult(ok=True, data=list(goals), text="l")),
        ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True, handler=write)])


def resolved(registry):
    return cue.resolve(registry, "q1", "get the partner users moving", NOW, decide=None)


def test_a_draft_with_a_slot_is_sent_back_once_and_the_fixed_one_is_kept():
    calls, asked = [], []
    answers = iter([json.dumps({"draft": "Pitch for [partner name]", "human_step": "Send the pitch to the candidate partner."}),
                    json.dumps({"draft": "Pitch for the candidate partner: here is what your audience gets."})])
    step = resolved(tools(calls))
    step.carry_out(tools(calls), lambda prompt: asked.append(prompt) or next(answers))
    asked = [a for a in asked if "Return JSON" in a]
    assert len(asked) == 2 and "[partner name]" in asked[1] and "Leave no bracket slot" in asked[1]
    assert calls[0]["description"].endswith("Draft: Pitch for the candidate partner: here is what your audience gets.") and "Human step: Send the pitch" in calls[0]["description"]
    assert step.stage_before.met == () and step.stage_after.met == tuple(key for key, _ in readiness.ITEMS)


def test_a_draft_still_holding_a_slot_after_the_fix_is_not_kept_and_the_stage_says_so():
    calls = []
    step = resolved(tools(calls))
    step.carry_out(tools(calls), lambda prompt: json.dumps({"draft": "Pitch for [partner name]", "human_step": "Send the pitch."}))
    assert "Draft:" not in calls[0]["description"] and "deliverable" not in step.stage_after.met and "human_step" in step.stage_after.met


def test_a_kept_draft_with_a_slot_is_written_again_and_a_complete_repeat_reuses_everything():
    calls, asked = [], []
    title = "P3 Partner users next step: week of 2026-11-02"
    goal = dict(FULL, id="k", title=title, text="Next.\nHuman step: Send the pitch.\nDraft: Pitch for [partner name]", completed=False, abandoned=False)
    step = resolved(tools(calls, [goal]))
    step.carry_out(tools(calls, [goal]), lambda prompt: asked.append(prompt) or json.dumps({"draft": "A pitch with no slot.", "human_step": "x"}))
    assert '"draft"' in asked[0].split("Return JSON")[1] and '"human_step"' not in asked[0].split("Return JSON")[1]
    done = dict(goal, text="Next.\nHuman step: Send the pitch.\nDraft: A pitch with no slot.")
    asked.clear()
    again = resolved(tools(calls, [done]))
    again.carry_out(tools(calls, [done]), lambda prompt: asked.append(prompt) or "{}")
    keys = "".join(a.split("Return JSON")[1] for a in asked if "Return JSON" in a)
    assert '"draft"' not in keys and '"human_step"' not in keys
    assert again.stage_before.met == again.stage_after.met and len(again.stage_after.met) == 5


def test_the_owner_is_named_as_the_record_names_them_with_the_account_beside_it_when_it_differs():
    calls = []
    step = resolved(tools(calls))
    step.carry_out(tools(calls), lambda prompt: "Travis" if "Which name in this text" in prompt else json.dumps({"draft": "Pitch.", "human_step": "Send it."}))
    assert step.owner_label == "Travis (account SpiritOps)" and step.lead_sentence("created").endswith("Travis (account SpiritOps) owns it.")
    same = resolved(tools(calls, member="Travis Example"))
    same.carry_out(tools(calls, member="Travis Example"), lambda prompt: json.dumps({"draft": "Pitch.", "human_step": "Send it."}))
    assert same.owner_label == "Travis Example"
    invented = resolved(tools(calls))
    invented.carry_out(tools(calls), lambda prompt: "Zed" if "Which name in this text" in prompt else json.dumps({"draft": "Pitch.", "human_step": "Send it."}))
    assert invented.owner_label == "SpiritOps"


def test_the_closing_line_gives_both_stages_and_says_why_an_unchanged_value_did_not_move():
    calls = []
    step = resolved(tools(calls))
    step.carry_out(tools(calls), lambda prompt: json.dumps({"draft": "Pitch.", "human_step": "Send it."}))
    line = step.closing("0 of 100", "0 of 100")
    assert line.startswith("Readiness of the next step: before, none held; after, all held: an owner from the milestone's record") and "Milestone metric (P3 Partner users): unchanged at 0 of 100 (the metric record), because" in line
    assert "was 0 of 100, now 7 of 100 (the metric record)" in step.closing("0 of 100", "7 of 100") and "\n" not in line


def test_the_first_message_says_the_readiness_the_quest_holds():
    doing = resolved(tools([])).doing()
    assert "Readiness of the step now: none held." in doing and "of 5" not in doing


def test_the_reply_loses_a_closing_question_but_keeps_the_questions_of_the_draft():
    calls = []
    step = resolved(tools(calls))
    step.carry_out(tools(calls), lambda prompt: json.dumps({"draft": "- What would you expect from a partner?\n- Who else should we ask?", "human_step": "Ask them."}))
    text = "I recorded the step.\n\nThe kept draft:\n\n- What would you expect from a partner?\n- Who else should we ask?\n\nWhich partner do you want first?"
    out = step.without_questions(text)
    assert "Which partner" not in out and "What would you expect" in out and out.startswith("I recorded the step.")
    assert step.without_questions("Only a question?") == "Only a question?"


def test_close_with_metric_ends_a_recorded_step_with_the_one_line_and_no_question():
    calls = []
    step = resolved(tools(calls))
    step.carry_out(tools(calls), lambda prompt: json.dumps({"draft": "Pitch.", "human_step": "Send it."}))
    before = act_status.MetricReading(step.rid, step.name, step.done_when, "0 of 100")
    rs = SimpleNamespace(status_first={"before": before, "voice": False}, cue_resolved=step, gathered=[], quest_id="q1", turn_metrics=None, tool_calls_ran=True)
    res = SimpleNamespace(kind="answer", text="Step recorded.\n\nDo you want me to pick a partner?", deep_results=[])
    messages.close_with_metric(SimpleNamespace(tools=None), rs, res)
    assert "Do you want" not in res.text and res.text.endswith("Milestone metric (P3 Partner users): unchanged at 0 of 100 (the metric record), because it changes only when a person completes the step above.")


def test_a_blocked_ask_is_not_filed_once_the_step_is_recorded_in_the_goal():
    res = SimpleNamespace(output="the reply")
    verdict = {"blocker": "needs_person"}
    assert messages.file_blocked_ask(SimpleNamespace(), None, {"milestone_step_recorded": True}, res, verdict, question="Which source?") is False
    assert res.output == "the reply"


SOURCED = {"solutions": TABLES["solutions"],
           "metrics": {"esr_mc": dict(TABLES["metrics"]["esr_mc"], record="Provisional 83.3 percent", source="cov_source", measured_through="2026-10-31")}}


def sourced_tools(calls, reads, goals=(), value="73.8 percent", label="the coverage report"):
    base = tools(calls, goals)
    def read(args, ctx):
        reads.append(dict(args))
        return ToolResult(ok=True, data={"value": value, "as_of": "2026-11-07", "source": label, "detail": ""}, text="read")
    registry = ToolRegistry([base.get("read_spec_records"), base.get("list_quest_goals"), base.get("create_or_update_quest_goal"),
                             ToolSpec(name="read_metric_source", description="d", mutates=False, handler=read)])
    registry.get("read_spec_records").handler = lambda a, c: ToolResult(ok=True, data={"records": SOURCED, "time_zone": "UTC"}, text="r")
    return registry


def answer_for(prompt):
    return json.dumps({"draft": "Pitch.", "human_step": "Send it."})


def test_without_an_earlier_line_the_record_reading_is_labelled_and_never_the_before():
    calls, reads = [], []
    step = resolved(sourced_tools(calls, reads))
    step.carry_out(sourced_tools(calls, reads), answer_for)
    line = step.closing("Provisional 83.3 percent", "73.8 percent")
    assert "now 73.8 percent (as of 2026-11-07, from the coverage report)" in line and "the step had no earlier reading of this measure" in line
    assert "the metric record's last reading: Provisional 83.3 percent (2026-10-31)" in line and "was Provisional" not in line


def test_with_an_earlier_line_from_the_same_source_before_is_that_line_and_an_unchanged_value_says_so():
    calls, reads = [], []
    title = "P3 Partner users next step: week of 2026-11-02"
    goal = dict(FULL, id="k", title=title, completed=False, abandoned=False,
                text="Next.\nCurrent value: 70.0 percent (as of 2026-11-03, from the coverage report)\nHuman step: Send it.\nDraft: Pitch.")
    step = resolved(sourced_tools(calls, reads, [goal]))
    step.carry_out(sourced_tools(calls, reads, [goal]), answer_for)
    assert "was 70.0 percent (as of 2026-11-03), now 73.8 percent (as of 2026-11-07), both from the coverage report" in step.closing("x", "y")
    same = dict(goal, text=goal["text"].replace("70.0", "73.8"))
    step = resolved(sourced_tools(calls, reads, [same]))
    step.carry_out(sourced_tools(calls, reads, [same]), answer_for)
    assert "unchanged at 73.8 percent (as of 2026-11-07, from the coverage report)" in step.closing("x", "y")
    other = dict(goal, text=goal["text"].replace("the coverage report", "an older report"))
    step = resolved(sourced_tools(calls, reads, [other]))
    step.carry_out(sourced_tools(calls, reads, [other]), answer_for)
    assert "another source (an older report), so it is not compared" in step.closing("x", "y")


def test_the_planners_source_read_is_taken_out_so_the_source_is_read_once():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    calls, reads = [], []
    provider = StubProvider([{"action": "tool", "user_intent": "act", "rationale": "r", "tool_calls": [{"name": "read_metric_source", "args": {}}]}])
    config = OrchestratorConfig()
    config.overseer = False
    Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=sourced_tools(calls, reads)).run("get the partner users moving", quest_id="q1")
    assert reads == [{"name": "cov_source"}] and len(calls) == 1


def test_the_first_message_never_carries_the_planners_fact_text_even_when_the_plan_still_reads():
    from hubofhubs.api import EVENT_INTERIM
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    leak = "CUE FACTS: milestone 'P3', solution esr_c, metric source cov_source. Code reads it via read_metric_source"
    provider = StubProvider([{"action": "tool", "user_intent": "act", "rationale": "r", "facts_read": [leak],
                              "tool_calls": [{"name": "list_quest_goals", "args": {"quest_id": "q1"}}]}])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=sourced_tools([], []),
                        config=OrchestratorConfig(interim_replies=True, overseer=False, min_seconds_between_interims=0.0))
    events = []
    orch.run("get the partner users moving", sink=StreamSink(events.append), quest_id="q1")
    first = next(e for e in events if e["type"] == EVENT_INTERIM)["text"]
    assert "CUE FACTS" not in first and "esr_" not in first and "read_metric_source" not in first and "Current value" in first


def test_the_reply_names_the_owner_as_the_record_names_them_wherever_it_writes_the_account_alone():
    calls = []
    step = resolved(tools(calls))
    step.carry_out(tools(calls), lambda prompt: "Travis" if "Which name in this text" in prompt else answer_for(prompt))
    assert step.apply_owner_label("It was updated in place, and SpiritOps owns it. Only Travis can send it.") == (
        "It was updated in place, and Travis (account SpiritOps) owns it. Only Travis can send it.")
    assert step.apply_owner_label("Travis (account SpiritOps) owns it.") == "Travis (account SpiritOps) owns it."
    assert step.apply_owner_label("Nobody is named.") == "Nobody is named."
    rs = SimpleNamespace(status_first={"before": act_status.MetricReading(step.rid, step.name, step.done_when, "0 of 100"), "voice": False}, cue_resolved=step, gathered=[],
                         quest_id="q1", turn_metrics=None, tool_calls_ran=True)
    res = SimpleNamespace(kind="answer", text="Updated, and SpiritOps owns it.", deep_results=[])
    messages.close_with_metric(SimpleNamespace(tools=None), rs, res)
    assert res.text.startswith("Updated, and Travis (account SpiritOps) owns it.") and "of 5" not in res.text


def test_a_draft_is_given_the_fresh_reading_as_current_and_never_states_the_older_figure():
    calls, reads, asked = [], [], []
    def decide(prompt):
        asked.append(prompt)
        return json.dumps({"draft": "Confirm the current pass rate of 83.3 percent.\nConfirm the subset pass rate of 73.8 percent with the owner.", "human_step": "Confirm the subset."})
    step = resolved(sourced_tools(calls, reads))
    step.carry_out(sourced_tools(calls, reads), decide)
    ask = next(a for a in asked if "Return JSON" in a)
    assert "Current value: 73.8 percent (as of 2026-11-07, from the coverage report)" in ask
    assert "history only and not the current value: Provisional 83.3 percent" in ask and "never state a figure that contradicts the current value" in ask
    assert "83.3" not in calls[0]["description"].split("Draft:")[1] and "73.8 percent" in calls[0]["description"].split("Draft:")[1]


def test_what_the_code_step_read_joins_the_turns_gathered_reads():
    calls, reads = [], []
    step = resolved(sourced_tools(calls, reads))
    step.carry_out(sourced_tools(calls, reads), answer_for)
    entries = step.read_entries()
    by_tool = {e["locator"]: e["text"] for e in entries}
    assert all(e["by_code"] and e["kind"] == "query" for e in entries)
    assert "Provisional 83.3 percent" in by_tool["tool:read_spec_records"] and "Measured through: 2026-10-31" in by_tool["tool:read_spec_records"]
    assert "Current value: 73.8 percent (as of 2026-11-07, from the coverage report)" in by_tool["tool:read_metric_source"]
    assert any(e["text"].startswith("After the write:") for e in entries)


def test_record_text_is_never_cut_inside_a_word_or_an_identifier():
    text = "none 344, botswana 14 (existing pool, not the new source), mentee-launch 0, and more words after that"
    for limit in range(20, len(text)):
        cut = cue.clip(text, limit)
        assert len(cut) <= limit and all(token in text.replace(",", " ").replace("(", " ").replace(")", " ").split() or token.rstrip(",)") in text for token in cut.split())
        assert not text.startswith(cut) or text[len(cut):len(cut) + 1] in ("", " ", ",", ";", ".", ":", "(")
    assert cue.first_sentence(text, 60) == "none 344, botswana 14 (existing pool"
    assert cue.first_sentence("Short value. More.") == "Short value"
    assert cue.clip("fits", 10) == "fits"


def test_the_prompt_for_a_draft_gets_the_whole_record_value_and_a_cut_identifier_is_rejected():
    long_record = "Reading: " + "none 344, botswana 14 (existing pool, not the new source), " * 3 + "mentee-launch 0"
    tables = {"solutions": TABLES["solutions"], "metrics": {"esr_mc": dict(TABLES["metrics"]["esr_mc"], record=long_record)}}
    base = tools([])
    base.get("read_spec_records").handler = lambda a, c: ToolResult(ok=True, data={"records": tables, "time_zone": "UTC"}, text="r")
    asked, calls = [], []
    registry = ToolRegistry([base.get("read_spec_records"), base.get("list_quest_goals"), tools(calls).get("create_or_update_quest_goal")])
    step = cue.resolve(registry, "q1", "get the partner users moving", NOW, decide=None)
    assert step.value_full == long_record
    def decide(prompt):
        asked.append(prompt)
        return json.dumps({"draft": "Test the source mentee-la first.\nTest the source mentee-launch first.", "human_step": "Pick it."})
    step.carry_out(registry, decide)
    assert "mentee-launch 0" in next(a for a in asked if "Return JSON" in a)
    kept = calls[0]["description"].split("Draft:")[1]
    assert "mentee-la " not in kept and "mentee-launch" in kept
    assert cue.truncated_tokens("pick mentee-la now", "the mentee-launch source") == ["mentee-la"]
    assert cue.truncated_tokens("sources and users", "source users") == []


def test_a_name_the_draft_call_made_up_does_not_stop_the_one_named_owner_from_being_the_label():
    tables = {"solutions": {"esr_c": dict(TABLES["solutions"]["esr_c"], purpose="A new source tested on its own. Owners: Ann.")}, "metrics": TABLES["metrics"]}
    base = tools([], member="Ops Account")
    base.get("read_spec_records").handler = lambda a, c: ToolResult(ok=True, data={"records": tables, "time_zone": "UTC"}, text="r")

    def decide(prompt):
        assert "Which name in this text" not in prompt
        return json.dumps({"draft": "Pitch.", "human_step": "Send it.", "people": ["Ann", "Zed Contact"]})
    step = cue.resolve(base, "q1", "get the partner users moving", NOW, decide=None)
    step.carry_out(base, decide)
    assert step.owner_label == "Ann (account Ops Account)"


def test_one_listed_owner_and_one_person_named_in_the_text_make_the_label_without_another_call():
    tables = {"solutions": {"esr_c": dict(TABLES["solutions"]["esr_c"], purpose="Travis runs partner outreach for Spiritual Data.")}, "metrics": TABLES["metrics"]}
    base = tools([], member="SpiritOperations")
    base.get("read_spec_records").handler = lambda a, c: ToolResult(ok=True, data={"records": tables, "time_zone": "UTC"}, text="r")
    asked = []
    def decide(prompt):
        asked.append(prompt)
        assert "Which name in this text" not in prompt
        return json.dumps({"draft": "Pitch.", "human_step": "Send it.", "people": ["Travis"]})
    step = cue.resolve(base, "q1", "get the partner users moving", NOW, decide=None)
    step.carry_out(base, decide)
    assert step.owner_label == "Travis (account SpiritOperations)"
