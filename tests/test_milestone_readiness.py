"""The readiness stage of a milestone's metric (counted from records only), the slot check on a kept draft, the owner's name, the closing line and the rule that
the reply never ends by asking the chat user. Synthetic records and a fake tool registry stand in for Quest."""
import datetime
import json
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_readiness as readiness
from quest_ai_runner.the_intelligence.mind.response_writer import act_status
from quest_ai_runner.the_intelligence.mind.response_writer import orchestrator_turn_messages as messages
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

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
    assert line.startswith("Readiness of the next step: 0 of 5 (none held yet) before, 5 of 5 (") and "Milestone metric (P3 Partner users): unchanged at 0 of 100, because" in line
    assert "was 0 of 100, now 7 of 100" in step.closing("0 of 100", "7 of 100") and "\n" not in line


def test_the_first_message_says_the_readiness_the_quest_holds():
    assert "Its readiness is 0 of 5 (none held yet) now." in resolved(tools([])).doing()


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
    assert "Do you want" not in res.text and res.text.endswith("Milestone metric (P3 Partner users): unchanged at 0 of 100, because it changes only when a person completes the step above.")


def test_a_blocked_ask_is_not_filed_once_the_step_is_recorded_in_the_goal():
    res = SimpleNamespace(output="the reply")
    verdict = {"blocker": "needs_person"}
    assert messages.file_blocked_ask(SimpleNamespace(), None, {"milestone_step_recorded": True}, res, verdict, question="Which source?") is False
    assert res.output == "the reply"
