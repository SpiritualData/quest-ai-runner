"""Offline tests: a status-shaped turn is answered from one code-composed, dated snapshot, with no planner loop, for the three turn shapes (a status question, a status
script to read out, a question about the asker's own priority)."""
import re
from types import SimpleNamespace
from typing import Any, List

from hubofhubs.api import Mode

from .conftest import StubProvider, StubRetrieval
from .test_answer_explanation import RecordingSink
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import status_snapshot
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import parse_goal_condition_reply, take_status_flags
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

TABLES = {"solutions": {"s_top": {"name": "Plan", "solution_ids": ["s_a"], "authoritative_metric_id": "m"}, "s_a": {"name": "First"}, "s_b": {"name": "Second"}},
          "metrics": {"m": {"name": "Paid subscribers", "solution_ids": ["s_top"], "source": "paid"}}}
READING = {"value": 0, "as_of": "2026-11-07", "source": "Revenue analytics", "detail": {"paid": 0, "trialing": 0}}
GOALS = [
    {"id": "g1", "title": "Second milestone next step: week of 2026-11-02", "text": "x\nCurrent value: 4 (as of 2026-11-06, from Source)", "due": "2026-11-08", "spec_solution_id": "s_b", "assignee_user_id": "u2"},
    {"id": "g2", "title": "First milestone next step: week of 2026-11-02", "text": "", "due": "2026-11-09", "spec_solution_id": "s_a", "assignee_user_id": "u1"},
    {"id": "g3", "title": "Unrelated task", "text": "", "due": "2026-10-01", "spec_solution_id": None, "assignee_user_id": "u1"},
]


class Registry:
    def __init__(self):
        self.calls: List[Any] = []

    def get(self, name):
        return object()

    def invoke(self, name, args, ctx):
        self.calls.append((name, dict(args)))
        if name == "read_spec_records":
            return SimpleNamespace(ok=True, data={"records": TABLES, "time_zone": ""}, text="")
        if name == "find_quest":
            return SimpleNamespace(ok=True, data=[{"quest_id": "q1", "outcome": "Reach the target"}, {"quest_id": "q2", "outcome": "Another aim"}], text="")
        if name == "read_metric_source":
            return SimpleNamespace(ok=True, data=READING, text="")
        rows = [g for g in GOALS if not args.get("assignee_user_id") or g["assignee_user_id"] == args["assignee_user_id"]]
        if args.get("text_contains"):
            rows = [g for g in rows if args["text_contains"] in g["title"]]
        return SimpleNamespace(ok=True, data=[dict(g, completed=False, abandoned=False) for g in rows], text="")


def test_the_snapshot_holds_the_live_key_measure_the_priorities_in_ladder_order_and_for_a_my_question_the_askers_goals():
    registry = Registry()
    snap = status_snapshot.build(registry, "q1", "u1", own=True)
    text = snap["text"]
    assert snap["locator"] == "status_snapshot" and "Paid subscribers: 0 (as of 2026-11-07, from Revenue analytics)" in text
    first, second = text.index("First milestone next step"), text.index("Second milestone next step")
    assert first < second and "Current value: 4 (as of 2026-11-06, from Source)" in text and "due 8 November" in text
    assert "Unrelated task" not in text.split("Active priorities")[1].split("Goals at a glance")[0]
    assert "The asker's own open goals" in text and "Unrelated task (due 1 October; " in text.split("The asker's own")[1] and "days overdue)" in text.split("The asker's own")[1]
    assert "The asker's own" not in status_snapshot.build(registry, "q1", "u1", own=False)["text"]
    assert status_snapshot.build(None, "q1") is None


def test_the_analyser_reports_the_status_shape_and_the_spoken_delivery_as_fields_not_filters():
    _, constraints = parse_goal_condition_reply('A script.\n{"asks_status": true, "read_aloud": true, "actor": "me"}')
    flags, rest = take_status_flags(constraints)
    assert flags == {"asks_status": True, "across_quests": False, "saves_to_records": False, "read_aloud": True, "writes_text": False, "plans_period": False} and rest == {"actor": "me"}
    assert take_status_flags(None)[0] == {"asks_status": False, "across_quests": False, "saves_to_records": False, "read_aloud": False, "writes_text": False, "plans_period": False}
    _, constraints = parse_goal_condition_reply('Which goal comes first.\n{"asks_status": true, "across_quests": true}')
    assert take_status_flags(constraints)[0]["across_quests"] is True
    _, constraints = parse_goal_condition_reply('An Instagram post about the product.\n{"writes_text": true}')
    flags, rest = take_status_flags(constraints)
    assert flags["writes_text"] is True and rest is None


class StatusProvider(StubProvider):
    def __init__(self, analyser):
        super().__init__(decisions=[{"action": "read", "reads": [{"rel_path": "n.md"}], "rationale": "must not run"}])
        self.analyser, self.planner_calls, self.writer_prompts = analyser, 0, []

    def plan(self, prompt, *, model, tool_schema):
        self.planner_calls += 1 if "You are the PLANNER" in prompt else 0      # a judge call also goes through plan()
        return super().plan(prompt, model=model, tool_schema=tool_schema)

    def answer(self, messages, *, model=None, system=None):
        text = "\n".join(str(m.get("content")) for m in messages) + (system or "")
        if "request analyser" in text:
            return self.analyser
        self.writer_prompts.append(text)
        return "There are 0 paid subscribers as of 7 November."


def run_status(analyser, message, quest_id="q1", placement=None):
    provider = StatusProvider(analyser)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), tools=_tools(), quest_placement=placement,
                        config=OrchestratorConfig(overseer=True))
    res = orch.run(message, mode=Mode.LIVE, sink=RecordingSink(), quest_id=quest_id, context_meta={"user_id": "u1"})
    return provider, res


def _tools():
    from quest_ai_runner.the_world.tools.tools import ToolResult, ToolRegistry, ToolSpec
    registry = Registry()

    def make(name):
        return ToolSpec(name=name, description="d", mutates=False, handler=lambda args, ctx: (lambda r: ToolResult(ok=r.ok, data=r.data, text=r.text))(registry.invoke(name, args, ctx)))
    return ToolRegistry([make(n) for n in ("read_spec_records", "read_metric_source", "list_quest_goals", "find_quest")])


def test_a_status_question_a_status_script_and_a_my_priority_question_each_get_the_snapshot_and_no_planner_loop():
    shapes = [("Say where the quest stands.", '{"asks_status": true}', "where are we with the big quest?"),
              ("A script to read out.", '{"asks_status": true, "read_aloud": true}', "what should i say on my call about where we are, keep it short"),
              ("What my priority is.", '{"asks_status": true, "actor": "me"}', "what is my priority right now")]
    for restated, flags, message in shapes:
        provider, res = run_status(restated + "\n" + flags, message)
        assert res.kind == "answer" and provider.planner_calls == 0
        assert any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
        assert provider.writer_prompts and "STATUS FROM A SNAPSHOT" in provider.writer_prompts[0]
        assert ("READ ALOUD" in provider.writer_prompts[0]) == ("read_aloud" in flags)
        assert ("The asker's own open goals" in provider.writer_prompts[0]) == ('"actor": "me"' in flags)


class Placement:
    def __init__(self, picked):
        self.picked, self.asked = picked, []

    def place(self, message, previous="", home_quest_id=None, max_quests=1):
        self.asked.append(message)
        return {"picked": self.picked, "ranked": self.picked}


def test_a_status_question_in_a_chat_with_no_quest_is_placed_on_a_quest_and_gets_its_snapshot_or_asks_one_short_question():
    placement = Placement(["q1"])
    provider, res = run_status('Where it stands.\n{"asks_status": true}', "where are we with the release?", quest_id=None, placement=placement)
    assert placement.asked and res.kind == "answer" and provider.planner_calls == 0
    assert any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
    # No quest fits: the question is answered from all the asker's quests together (the portfolio snapshot), never by asking which quest (2026-11-11).
    abstain = Placement([])
    provider, res = run_status('Where it stands.\n{"asks_status": true}', "where are we with the release?", quest_id=None, placement=abstain)
    assert abstain.asked and provider.planner_calls == 0 and any("QUESTS TOGETHER" in o.get("text", "") for o in res.gathered)
    assert res.kind == "answer" and "STATUS FROM A SNAPSHOT" in "".join(provider.writer_prompts)


class JudgeProvider(StatusProvider):
    """The request analyser leaves the status report out; the reach judge (a second, stronger structured report) makes it."""

    def __init__(self, verdict):
        super().__init__("What is the top priority.")
        self.verdict = verdict

    def plan(self, prompt, *, model, tool_schema, tier=None):
        if "Decide ONE thing" in prompt:
            return dict(self.verdict)
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def run_judged(verdict, message="whats our top priority right now?"):
    provider = JudgeProvider(verdict)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), tools=_tools(),
                        config=OrchestratorConfig(overseer=True, planner_reach_judge=True, read_reach_summary="the quest's own records"))
    res = orch.run(message, mode=Mode.LIVE, sink=RecordingSink(), quest_id="q1", context_meta={"user_id": "u1"})
    return provider, res


def test_a_status_question_the_analyser_did_not_flag_gets_the_snapshot_when_the_reach_judge_reports_the_status_shape():
    provider, res = run_judged({"reach": "inside", "asks_status": True, "asks_change": False})
    assert res.kind == "answer" and provider.planner_calls == 0
    assert any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
    assert provider.writer_prompts and "STATUS FROM A SNAPSHOT" in provider.writer_prompts[0]


def test_the_reach_judge_report_starts_no_snapshot_for_an_order_or_a_turn_it_did_not_flag():
    for verdict in ({"reach": "inside", "asks_status": True, "asks_change": True}, {"reach": "inside", "asks_status": False}, {"reach": "inside"}):
        provider, res = run_judged(verdict)
        assert not any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
        assert provider.planner_calls >= 1


def test_a_quest_with_hundreds_of_goals_keeps_its_priority_steps_past_the_first_page():
    class Paged(Registry):
        def invoke(self, name, args, ctx):
            if name != "list_quest_goals":
                return super().invoke(name, args, ctx)
            filler = [{"id": f"f{i}", "title": f"Filler {i}", "text": "", "due": "2026-10-01", "spec_solution_id": None, "completed": False, "abandoned": False} for i in range(250)]
            keyed = [{"id": "k1", "title": "First milestone next step: week of 2026-11-02", "text": "", "due": "2026-12-30", "spec_solution_id": "s_a", "completed": False, "abandoned": False}]
            rows = filler + keyed
            if args.get("text_contains"):
                rows = [g for g in rows if args["text_contains"] in g["title"]]
            return SimpleNamespace(ok=True, data=rows[args.get("offset", 0):args.get("offset", 0) + args["limit"]], text="")
    snap = status_snapshot.build(Paged(), "q1", "u1")
    assert "First milestone next step" in snap["text"]


def test_a_status_question_about_one_milestone_gets_its_records_blocker_owner_and_next_step():
    milestone_tables = {"solutions": {"s_top": TABLES["solutions"]["s_top"], "s_a": {"name": "P1 Store release", "milestones": "App live in the store.",
                                                                                   "purpose": "Ship to the store. Owners: Ann."},
                                      "s_b": {"name": "P2 Partner users", "milestones": "100 users from partners."}},
                        "metrics": {"m": TABLES["metrics"]["m"], "m_a": {"name": "Store listing live", "solution_ids": ["s_a"],
                                                                        "record": "Current value: 0 (as of 2026-11-06, from store search)\nBlocked: the store account is not verified."}}}
    registry = Registry()
    original = dict(TABLES)
    TABLES.clear(); TABLES.update(milestone_tables)
    try:
        asked = []
        snap = status_snapshot.build(registry, "q1", question="what is the status of the store release?",
                                     decide=lambda prompt: asked.append(prompt) or "It asks how the store release stands.\ns_a")
        none = status_snapshot.build(registry, "q1", question="what is the status of the store release?", decide=lambda prompt: "NONE")
    finally:
        TABLES.clear(); TABLES.update(original)
    text = snap["text"]
    assert "last line" in asked[0] and "Blocked: the store account is not verified." in text and "Owners: Ann." in text
    assert "First milestone next step" in text.split("The milestone the question is about")[1].split("Active priorities")[0]
    assert "The milestone the question is about" not in none["text"]


def test_when_the_key_measure_cannot_be_read_the_dated_reading_its_record_holds_leads_and_no_older_figure_does():
    class Unreadable(Registry):
        def invoke(self, name, args, ctx):
            if name == "read_metric_source":
                return SimpleNamespace(ok=False, data=None, text="no file source holds it")
            return super().invoke(name, args, ctx)
    TABLES["metrics"]["m"]["record"] = "Current value: 0 (as of 2026-11-07, from Revenue analytics)\nOlder: 0 on 2026-10-05."
    try:
        text = status_snapshot.build(Unreadable(), "q1")["text"]
    finally:
        TABLES["metrics"]["m"].pop("record")
    assert "KEY MEASURE AS LAST RECORDED" in text and "Paid subscribers: 0 (as of 2026-11-07, from Revenue analytics)" in text


def test_a_request_the_analyser_reads_as_status_but_the_reach_judge_reads_as_a_change_is_work_not_a_snapshot():
    class AnalyserSaysStatus(JudgeProvider):
        def __init__(self, verdict):
            super().__init__(verdict)
            self.analyser = 'Get the first item moving.\n{"asks_status": true}'
    provider = AnalyserSaysStatus({"reach": "inside", "asks_status": False, "asks_change": True})
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), tools=_tools(),
                        config=OrchestratorConfig(overseer=True, planner_reach_judge=True, read_reach_summary="the quest's own records"))
    res = orch.run("can we get the first item moving?", mode=Mode.LIVE, sink=RecordingSink(), quest_id="q1", context_meta={"user_id": "u1"})
    assert not any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered) and provider.planner_calls >= 1


def test_the_portfolio_snapshot_reads_the_open_goals_of_every_quest_soonest_due_first_and_none_when_nothing_is_open():
    registry = Registry()
    quests = status_snapshot.reachable_quests(registry)
    snap = status_snapshot.portfolio(registry, quests)
    text = snap["text"]
    assert snap["locator"] == "status_snapshot" and "OF THE ASKER'S QUESTS TOGETHER" in text
    assert "Quest: Reach the target" in text and "Quest: Another aim" in text
    assert re.search(r"Quest: Reach the target\n\d+ open goals? as of \d{4}-\d{2}-\d{2}, \d+ of them overdue\.", text)     # each quest's own dated counts
    assert text.index("Unrelated task") < text.index("Second milestone next step")      # due 2026-10-01 leads the later due days
    assert status_snapshot.portfolio(registry, []) is None and status_snapshot.portfolio(None, quests) is None
    GOALS_BACKUP = list(GOALS)
    GOALS.clear()
    try:
        assert status_snapshot.portfolio(registry, quests) is None      # no open goal anywhere: nothing to show
    finally:
        GOALS.extend(GOALS_BACKUP)


def test_a_question_across_the_askers_quests_reads_their_goals_whether_or_not_a_quest_is_open_and_never_asks_which_quest():
    message = "look at my quests and tell me which one goal i should work on first this week"
    for quest_id in (None, "q1"):
        abstain = Placement([])
        provider, res = run_status('Which goal comes first.\n{"asks_status": true, "across_quests": true}', message, quest_id=quest_id, placement=abstain)
        assert not abstain.asked and provider.planner_calls == 0 and res.kind == "answer"
        snapshot = next(o["text"] for o in res.gathered if "STATUS SNAPSHOT" in o.get("text", ""))
        assert "Quest: Reach the target" in snapshot and "Quest: Another aim" in snapshot
        assert provider.writer_prompts and "STATUS FROM A SNAPSHOT" in provider.writer_prompts[0] and "asker's quests together" in provider.writer_prompts[0]


def test_an_across_quests_question_whose_goals_cannot_be_read_goes_to_the_planner_not_to_a_question_about_which_quest():
    GOALS_BACKUP = list(GOALS)
    GOALS.clear()
    try:
        abstain = Placement([])
        provider, res = run_status('Which goal comes first.\n{"asks_status": true, "across_quests": true}', "which goal should i work on first?", quest_id=None, placement=abstain)
        assert not abstain.asked and provider.planner_calls >= 1
    finally:
        GOALS.extend(GOALS_BACKUP)


# --- Goals at a glance (2026-11-11): a new quest with no goals, and goals that are not linked to the plan -----------------------------------------------------------------

class EmptyRegistry(Registry):
    """A quest whose goal list reads fine and is empty, with no specification and no key measure."""

    def invoke(self, name, args, ctx):
        self.calls.append((name, dict(args)))
        if name == "list_quest_goals":
            return SimpleNamespace(ok=True, data=[], text="No goals on quest q1 match (0 goals in all, before filters).")
        return SimpleNamespace(ok=False, data=None, text="not here")


class FailingGoalsRegistry(EmptyRegistry):
    def invoke(self, name, args, ctx):
        self.calls.append((name, dict(args)))
        return SimpleNamespace(ok=False, data=None, text="The goals of quest q1 could not be read just now.")


def test_a_quest_with_no_goals_gets_a_snapshot_that_says_so_and_a_goal_read_that_failed_gets_none():
    snap = status_snapshot.build(EmptyRegistry(), "q1")
    assert snap is not None and "this quest has no goals yet" in snap["text"] and "never as a read that failed" in snap["text"]
    assert status_snapshot.build(FailingGoalsRegistry(), "q1") is None


def test_the_goals_at_a_glance_count_done_open_and_overdue_and_list_overdue_before_upcoming():
    from datetime import date
    rows = [{"id": "a", "title": "Book the plumber", "due": "2026-11-01", "completed": False},
            {"id": "b", "title": "Buy tiles", "due": "2026-11-13", "completed": False},
            {"id": "c", "title": "Demolition", "due": "2026-10-20", "completed": True},
            {"id": "d", "title": "Pick a colour", "due": None, "completed": False}]
    text = status_snapshot.overview(rows, date(2026, 11, 11))
    assert "4 goals: 1 done, 3 open, 1 overdue, 1 due by 18 November" in text
    assert text.index("Book the plumber") < text.index("Buy tiles") and "- Demolition" in text and text.index("Demolition") < text.index("Book the plumber") and text.index("Buy tiles") < text.index("Pick a colour")   # done goals are named under "Done so far" (2026-11-11: a status that never said what was achieved read as only a list of what is late)
    assert "Pick a colour" in status_snapshot.overview(rows, date(2026, 11, 11), plan=True)


def test_a_plan_my_week_turn_is_answered_from_the_planning_snapshot_with_the_plan_standard_and_no_planner_loop():
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import SHAPE_PLAN, STANDARDS
    provider, res = run_status('Lay out the week.\n{"plans_period": true}', "Plan my week for this quest")
    assert res.kind == "answer" and provider.planner_calls == 0
    assert any("PLANNING SNAPSHOT" in o.get("text", "") for o in res.gathered)
    assert provider.writer_prompts and "A PLAN FROM A PLANNING SNAPSHOT" in provider.writer_prompts[0] and "STATUS FROM A SNAPSHOT" not in provider.writer_prompts[0]
    assert STANDARDS[SHAPE_PLAN].max_list_lines is None      # a capped plan is cut mid-list: days go missing


def test_a_plan_snapshot_holds_the_days_of_the_period_so_day_labels_pass_the_date_check_and_a_date_outside_it_does_not():
    from datetime import date
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import SHAPE_PLAN, STANDARDS, measure
    rows = [{"id": "a", "title": "Book the plumber", "due": "2026-11-01", "completed": False}, {"id": "b", "title": "Buy tiles", "due": "2026-11-15", "completed": False}]
    draft = "- **Thu 12 Nov:** Book the plumber.\n- **Sun 15 Nov:** Buy tiles.\n- **Fri 20 Nov:** Paint."
    assert measure(draft, STANDARDS[SHAPE_PLAN], status_snapshot.overview(rows, date(2026, 11, 11), plan=True)).unsupported == ["20 Nov"]
    assert "12 Nov" in measure(draft, STANDARDS[SHAPE_PLAN], status_snapshot.overview(rows, date(2026, 11, 11))).unsupported


def test_overdue_goals_carry_their_days_overdue_and_the_snapshot_names_the_quest_never_its_id():
    from datetime import date
    rows = [{"id": "a", "title": "Book the plumber", "due": "2026-11-01", "completed": False}, {"id": "b", "title": "Buy tiles", "due": "2026-11-15", "completed": False}]
    text = status_snapshot.overview(rows, date(2026, 11, 11))
    assert "Book the plumber (due 1 November; 10 days overdue)" in text and "Buy tiles (due 15 November)" in text
    snap = status_snapshot.build(Registry(), "q1", quest_title="Kids' bathroom (Renovate it before the visit)")
    assert "The quest: Kids' bathroom (Renovate it before the visit)" in snap["text"] and "never write its id" in snap["text"]


def test_an_empty_quest_plan_snapshot_asks_for_the_proposals_written_out_and_a_portfolio_plan_lists_the_days():
    from datetime import date
    empty = status_snapshot.overview([], date(2026, 11, 11), plan=True)
    assert "write out, in this reply, up to three proposed first goals" in empty and "never only offer" in empty
    assert "The days of this period: Wednesday 11 November (2026-11-11)" in empty and "Wednesday 18 November (2026-11-18)" in empty     # the proposals' days are days the turn read
    registry = Registry()
    snap = status_snapshot.portfolio(registry, status_snapshot.reachable_quests(registry), plan=True)
    assert snap["text"].startswith("PLANNING SNAPSHOT READ JUST NOW OF THE ASKER'S QUESTS TOGETHER") and "The days of this period:" in snap["text"]
    layout = snap["text"].split("Day layout across these quests")[1]
    assert "[Reach the target]" in layout and "[Another aim]" in layout       # every quest's goals are placed, each naming its quest
    assert "Day layout" not in status_snapshot.portfolio(registry, status_snapshot.reachable_quests(registry))["text"]     # a status reply gets no layout


def test_the_day_layout_places_every_overdue_and_due_goal_on_or_before_its_due_day_two_a_day_then_fills_empty_days_with_undated_goals():
    from datetime import date
    rows = [{"title": f"Late {n}", "due": f"2026-11-0{n}"} for n in range(1, 6)] + [{"title": "Rough-in", "due": "2026-11-13"}, {"title": "Undated A"}, {"title": "Undated B"}]
    layout = status_snapshot.day_layout(rows, date(2026, 11, 11), window_days=4)
    lines = layout.splitlines()
    assert lines[0].startswith("- Wednesday 11 November: Late 1") and "Late 2" in lines[0]
    assert "Late 5" in lines[2] and "Rough-in" in lines[2] and lines[2].startswith("- Friday 13 November")      # due 13 November: on its day, with room
    assert "Undated A" in lines[3] and "Undated B" in lines[4] and len(lines) == 5
    assert status_snapshot.day_layout([], date(2026, 11, 11)) == ""


def test_the_status_lead_sentence_states_the_goal_counts_and_the_longest_overdue_goal_and_a_plan_gets_its_own_counts():
    from datetime import date
    today = date(2026, 11, 11)
    rows = [{"title": "A", "due": "2026-11-01"}, {"title": "B", "due": "2026-11-20"}, {"title": "C", "completed": True}]
    assert status_snapshot.lead_sentence(rows, today) == ('As of 11 November, 1 of the quest\'s 3 goals is done ("C"), and 1 is overdue. The longest overdue is "A", '
                                                          'due 1 November, so start there.')
    # nothing overdue: the goal due soonest is named by code, with its due day (a status once said "The first goal is due 15 November", unnamed)
    assert status_snapshot.lead_sentence(rows[1:], today) == ("As of 11 November, 1 of the quest's 2 goals is done (\"C\"), and none is overdue. "
                                                              "The soonest due is \"B\", due 20 November, so start there.")
    assert status_snapshot.lead_sentence([], today) == ""
    assert status_snapshot.lead_sentence([], today, quest_name="Learn the cello") == "\"Learn the cello\" has no goals yet. A good first step is to add one small goal for this week that starts it."
    # a soonest-due goal is not late: no days-late figure is read out of its lead
    assert status_snapshot.lead_days_late(status_snapshot.lead_sentence(rows[1:], today)) == set()
    snap = status_snapshot.build(Registry(), "q1")
    assert snap["lead"].startswith("As of ") and "ALREADY BEEN TOLD" in snap["text"]
    open_rows = [{"title": "L1", "due": "2026-11-01"}, {"title": "L2", "due": "2026-11-02"}, {"title": "S", "due": "2026-11-15"}, {"title": "U"}]
    assert status_snapshot.plan_lead(open_rows, today) == "As of 11 November, 2 open goals are overdue, and 1 more is due by 18 November."
    assert status_snapshot.plan_lead(open_rows[2:], today, across=3) == "As of 11 November, no open goal across your 3 quests is overdue, and 1 is due by 18 November."
    assert status_snapshot.plan_lead([], today) == ""
    plan = status_snapshot.build(Registry(), "q1", plan=True)
    assert plan["lead"].startswith("As of ") and "overdue" in plan["lead"] and "ALREADY BEEN TOLD" in plan["text"]
    registry = Registry()
    assert status_snapshot.portfolio(registry, status_snapshot.reachable_quests(registry), plan=True)["lead"].startswith("As of ")
    assert status_snapshot.portfolio(registry, status_snapshot.reachable_quests(registry))["lead"] == ""


def test_a_writer_sentence_that_only_restates_the_code_written_lead_is_dropped_and_one_with_new_facts_stays():
    lead = ('As of 11 November, 1 of the quest\'s 43 goals is done, and 8 are overdue. The longest overdue is "Choose the bank and account type", due 2 November.')
    text = ('Your quest "i want to become self reliant" is at 1 of 43 goals done, with 8 overdue. The longest overdue is "Choose the bank and account type", due '
            '2 November.\n\nDo this first: sit down for 30 minutes and list every source of income. It is 9 days overdue.')
    assert status_snapshot.without_restated_lead(text, lead) == "Do this first: sit down for 30 minutes and list every source of income. It is 9 days overdue."
    reno = 'As of 11 November, none of the quest\'s 25 goals is done yet, and 7 are overdue. The longest overdue is "Paint the walls", due 1 November.'
    kept = 'The most urgent item is booking the plumber. It was due 1 November and is now 10 days overdue.'
    assert status_snapshot.without_restated_lead(kept, reno) == kept      # one shared date and no goal the lead names: a new fact, kept
    plan_lines = "- **Wednesday 11 November:** Paint the walls (overdue since 1 November)"
    assert status_snapshot.without_restated_lead(plan_lines, reno) == plan_lines


def test_a_goal_carried_over_to_a_later_week_is_one_goal_in_the_counts_the_lead_and_the_day_layout():
    """Measured 2026-11-11 on a four-goal quest whose weekly goals Quest had carried into the next week: the status said "7 goals, 2 overdue" and a week plan
    put "Run one tempo session per week" on two days. A person counts the quest's goals as four."""
    from datetime import date
    rows = [{"id": "a1", "title": "Run one tempo session per week", "due": "2026-11-08", "completed": False},
            {"id": "a2", "title": "Run one tempo session per week", "due": "2026-11-15", "completed": False},
            {"id": "b", "title": "Do a 10K time trial", "due": "2026-11-15", "completed": False},
            {"id": "c1", "title": "Buy tiles", "due": "2026-11-01", "completed": True},
            {"id": "c2", "title": "Buy tiles", "due": "2026-11-15", "completed": False}]
    one = status_snapshot.collapse_carried(rows)
    assert [g["id"] for g in one] == ["a2", "b", "c1", "c2"] and one[0]["carried_from"] == "2026-11-08" and "carried_from" not in one[1]
    today = date(2026, 11, 11)
    assert status_snapshot.lead_sentence(one, today)== ("As of 11 November, 1 of the quest's 4 goals is done (\"Buy tiles\"), none is overdue, and 3 are due by 18 November. "
                                                       "The soonest due is \"Run one tempo session per week\", due 15 November, so start there.")
    text = status_snapshot.overview(one, today, plan=True)
    assert "due 8 November" not in text and "due 2026-11-08" not in text.replace("carried over from 2026-11-08", "") and "carried over from 2026-11-08" in text
    assert text.count("Run one tempo session per week (due 15 November") == 2      # once in the due list, once on its one day in the layout


def test_a_sentence_that_only_says_how_late_the_lead_named_goal_is_is_dropped():
    lead = 'As of 11 November, 1 of the quest\'s 4 goals is done, and 1 is overdue. The longest overdue is "Book the race entry", due 8 November.'
    text = 'The most overdue goal is "Book the race entry", due 8 November, now 3 days late.'
    assert status_snapshot.without_restated_lead(text, lead) == ""      # the lead says to start there itself
    assert status_snapshot.without_restated_lead(text + " The race fills in December.", lead) == "The race fills in December."


def test_the_code_written_lead_names_the_quest_and_a_reworded_copy_of_its_counts_is_dropped():
    """Measured 2026-11-11 on the dashboard's suggestions: replies on a three-quest account never named the tapped quest ("the quest's 2 goals"), and one
    said its counts twice ("None of your 2 goals is done yet, and none is overdue." after the lead said the same)."""
    from datetime import date
    today = date(2026, 11, 11)
    rows = [{"title": "Photograph the first ten pieces", "due": "2026-11-15"}, {"title": "Set up the shop page", "due": "2026-11-30"}]
    lead = status_snapshot.lead_sentence(rows, today, quest_name="Launch my pottery shop")
    assert lead == ('As of 11 November, none of the 2 goals in "Launch my pottery shop" is done yet, none is overdue, and 1 is due by 18 November. '
                    'The soonest due is "Photograph the first ten pieces", due 15 November, so start there.')
    assert status_snapshot.plan_lead(rows, today, quest_name="Launch my pottery shop") == (
        'As of 11 November, no open goal in "Launch my pottery shop" is overdue, and 1 is due by 18 November.')
    text = ('None of your 2 goals is done yet, and none is overdue. Your next step is "Photograph the first ten pieces," due Sunday, 15 November.')
    # the lead names that goal and its due day now, so the writer's sentence naming it again goes (measured live 2026-11-11: "The first goal is due 15 November")
    assert status_snapshot.without_restated_lead(text, lead) == ''
    rich = status_snapshot.lead_sentence([{"title": "Run 5 km", "completed": True}, {"title": "Book the race entry", "due": "2026-11-08"},
                                          {"title": "Long run", "due": "2026-11-14"}, {"title": "Shoes", "due": "2026-11-15"}], today, quest_name="Run my first half marathon")
    assert rich.startswith('As of 11 November, 1 of the 4 goals in "Run my first half marathon" is done ("Run 5 km"), 1 is overdue, and 2 are due by 18 November. '
                          'The longest overdue is "Book the race entry"')
    said = 'The running quest has 4 goals: 1 is done, 3 are open, and 1 is overdue. Running 5 km without stopping is already behind you.'
    assert status_snapshot.without_restated_lead(said, rich) == "Running 5 km without stopping is already behind you."      # 3 open is the lead's too
    named = 'Your quest "Run my first half marathon" is moving: the base is built.'
    assert status_snapshot.without_restated_lead(named, rich) == named      # naming the quest is not restating a count
    assert status_snapshot.lead_sentence(rows, today).startswith("As of 11 November, none of the quest's 2 goals is done yet, none is overdue, and 1 is due by 18 November.")   # no name read


def test_a_sentence_that_says_the_goal_counts_again_or_gives_a_percentage_is_dropped():
    lead = 'As of 11 November, none of the 2 goals in "Write a children\'s book" is done yet, and 1 is overdue. The longest overdue is "Finish the second draft", due 8 November.'
    text = 'Write a children\'s book is 0% done as of 11 November: none of its 2 goals is complete, and 1 is overdue. Finish that draft first.'
    assert status_snapshot.without_restated_lead(text, lead) == "Finish that draft first."
    pottery = 'As of 11 November, none of the 2 goals in "Launch my pottery shop" is done yet, and none is overdue.'
    assert status_snapshot.without_restated_lead('The shop has no goals done yet, and none is overdue. Photograph the first ten pieces is due soonest.', pottery) == "Photograph the first ten pieces is due soonest."
    assert status_snapshot.without_restated_lead("Both goals are done and the course is 40 percent paid.", pottery) == "Both goals are done and the course is 40 percent paid."


RICH_LEAD = ('As of 11 November, 1 of the 4 goals in "Run my first half marathon" is done ("Run 5 km without stopping"), 1 is overdue, and 2 are due by 18 November. '
             'The longest overdue is "Book the race entry", due 8 November, so start there.')


def test_a_number_inside_a_goal_name_is_not_a_count_so_the_counts_said_twice_over_are_dropped():
    # measured 2026-11-11, "How is this quest going?": the 5 of "Run 5 km without stopping" kept a sentence that said the lead's counts again
    text = ('The quest "Run my first half marathon" has 4 goals: 1 done, 3 open, and 1 overdue: 1 of its 4 goals is done ("Run 5 km without stopping"), '
            '3 are open, and 1 is overdue. "Buy proper running shoes" and the 12 km long run on Saturday are both due 15 November.')
    assert status_snapshot.without_restated_lead(text, RICH_LEAD) == '"Buy proper running shoes" and the 12 km long run on Saturday are both due 15 November.'


def test_the_lead_named_goal_said_again_becomes_that_one_and_a_bare_repeat_is_said_once_in_code_words():
    # measured 2026-11-11, "What should I do next?": "The longest overdue is "Book the race entry" ... Book the race entry first."
    text = "Book the race entry first.\n\nBuy proper running shoes and the 12 km long run on Saturday are both due 15 November, so they come next."
    assert status_snapshot.without_restated_lead(text, RICH_LEAD) == (
        "Buy proper running shoes and the 12 km long run on Saturday are both due 15 November, so they come next.")
    assert status_snapshot.without_restated_lead("Book the race entry today.", RICH_LEAD) == ""
    assert status_snapshot.without_restated_lead('Your next step is "Book the race entry", due 8 November.', RICH_LEAD) == ""
    assert status_snapshot.without_restated_lead("Book the race entry first, since entries can close before race day.", RICH_LEAD) == (
        "Do that one first, since entries can close before race day.")
    assert status_snapshot.without_restated_lead('I would start with "Book the race entry," because the race is in March.', RICH_LEAD) == (
        "I would start with that one, because the race is in March.")
    other = "Buy proper running shoes next, before the 12 km long run."
    assert status_snapshot.without_restated_lead(other, RICH_LEAD) == other      # a goal the lead does not name is left as it is


def test_a_plan_reply_keeps_its_day_lines_and_drops_the_writer_counts_before_them():
    text = ('Your week for "Launch my pottery shop" is short: 2 goals, 0 done and 2 still open, with 1 due from today through 2026-11-18. The one due soonest '
            'is "Photograph the first ten pieces," due Sunday, 15 November.\n\nPlan:\n- Wednesday 11 November: Photograph the first ten pieces (due 15 November)\n\n'
            'Nothing has been saved yet. Should I add this plan to your goals?')
    assert status_snapshot.without_plan_preamble(text) == ('- Wednesday 11 November: Photograph the first ten pieces (due 15 November)\n\n'
                                                           'Nothing has been saved yet. Should I add this plan to your goals?')
    prose = 'This week, start with "Finish the second draft," which is 3 days overdue. Put it on Wednesday 11 November.'
    assert status_snapshot.without_plan_preamble(prose) == prose      # no day line: nothing to keep in its place
    days = "**Wednesday 11 November**: Book the race entry\n**Thursday 12 November**: Buy proper running shoes"
    assert status_snapshot.without_plan_preamble(days) == days


def test_a_goal_whose_name_sets_its_day_is_placed_on_that_day():
    from datetime import date
    today = date(2026, 11, 11)      # a Wednesday
    rows = [{"title": "Book the race entry", "due": "2026-11-08"}, {"title": "Buy proper running shoes", "due": "2026-11-15"},
            {"title": "Long run of 12 km on Saturday", "due": "2026-11-15"}]
    layout = status_snapshot.day_layout(rows, today)
    assert "- Saturday 14 November: Long run of 12 km on Saturday (due 15 November)" in layout
    assert "- Wednesday 11 November: Book the race entry (overdue, due 8 November); Buy proper running shoes (due 15 November)" in layout
    late = [{"title": "Saturday swim", "due": "2026-11-12"}]      # its Saturday falls after its due day: the usual rule places it
    assert "- Wednesday 11 November: Saturday swim" in status_snapshot.day_layout(late, today)


def test_the_lead_named_goal_with_the_lead_figures_and_a_reason_keeps_the_reason_without_the_name():
    # measured live 2026-11-11, "What should I do next?"
    said = "Book the race entry first, since it is the overdue goal from 8 November."
    assert status_snapshot.without_restated_lead(said, RICH_LEAD) == ""


def test_a_that_one_sentence_goes_straight_after_the_lead_so_it_cannot_read_as_another_goal():
    # measured live 2026-11-11: "Done so far: Run 5 km without stopping. Start with that one."
    text = 'Done so far: "Run 5 km without stopping."\n\nBook the race entry first.\n\nThe long run is on Saturday.'
    # the done goal the lead already quotes goes too (run 18, 2026-11-11)
    assert status_snapshot.without_restated_lead(text, RICH_LEAD) == "The long run is on Saturday."
    said = '"Run my first half marathon" is at 1 of 4 goals done as of 11 November, with 1 overdue and 2 due this week.'
    assert status_snapshot.without_restated_lead(said, RICH_LEAD) == ""      # every figure is now the lead's


def test_a_plan_list_introduction_says_the_count_of_the_list_under_it():
    # measured live 2026-11-11: "Here are three proposed first goals" over one line, the other two cut for holding figures
    one = "Here are three proposed first goals, each one a starting point:\n- Proposed for Thursday 12 November: name the piece.\n\nNothing has been saved yet."
    assert status_snapshot.list_count_said_right(one).startswith("Here are proposed first goals, each one a starting point:\n- Proposed")
    two = "Here are three proposed first goals:\n- Thursday: tune it.\n- Friday: pick a piece.\n\nShould I add them?"
    assert status_snapshot.list_count_said_right(two).startswith("Here are two proposed first goals:\n")
    right = "Here are two proposed first goals:\n- Thursday: tune it.\n- Friday: pick a piece."
    assert status_snapshot.list_count_said_right(right) == right
    plain = "Wednesday 11 November: Book the race entry (overdue, due 8 November)"
    assert status_snapshot.list_count_said_right(plain) == plain


def test_the_lead_goal_name_alone_is_dropped_and_iso_dates_are_written_for_a_person():
    # measured live 2026-11-11: "Finish the second draft." became "That one.", and "(due 2026-11-08)" was copied from the snapshot
    lead = ('As of 11 November, none of the 2 goals in "Write a children\'s book" is done yet, and 1 is overdue. '
            'The longest overdue is "Finish the second draft", due 8 November, so start there.')
    text = "Finish the second draft.\n\nFinish the second draft first, since the June agent deadline needs a finished draft."
    assert status_snapshot.without_restated_lead(text, lead) == "Do that one first, since the June agent deadline needs a finished draft."
    assert status_snapshot.human_dates("3 days overdue (due 2026-11-08), next 2026-02-30") == "3 days overdue (due 8 November), next 2026-02-30"


def test_counts_said_again_beside_a_new_fact_are_dropped_and_an_empty_rest_says_where_to_start():
    # measured live 2026-11-11 (run 9), "How is this quest going?" and "What should I do next?"
    said = ('As of 11 November, the quest has 4 goals: "Run my first half marathon" has 4 goals, 1 done, 3 open, 1 overdue, and 2 due by 18 November, '
            'aimed at finishing a half marathon in under 2:15 by 14 March.\n\nThe shoes come before the long run.')
    assert status_snapshot.without_restated_lead(said, RICH_LEAD) == "The shoes come before the long run."
    assert status_snapshot.without_restated_lead("", RICH_LEAD) == ""
    pottery = 'As of 11 November, none of the 2 goals in "Launch my pottery shop" is done yet, none is overdue, and 1 is due by 18 November.'
    assert status_snapshot.without_restated_lead("", pottery) == ""      # no overdue goal named: nothing to start with
    assert status_snapshot.without_restated_lead("The race is on 14 March, 4 weeks after the 2 long runs.", RICH_LEAD) == (
        "The race is on 14 March, 4 weeks after the 2 long runs.")      # figures that are not about goals stay


def test_counts_spelled_out_are_counts():
    # measured live 2026-11-11 (run 10), "How is this quest going?"
    text = ('Three are still open, one of them overdue, and two are due by 18 November. The other two due soon are "Buy proper running shoes" and '
            '"Long run of 12 km on Saturday", both due 15 November.')
    assert status_snapshot.without_restated_lead(text, RICH_LEAD) == (
        'The other two due soon are "Buy proper running shoes" and "Long run of 12 km on Saturday", both due 15 November.')


def test_a_quest_with_no_goals_gets_a_code_written_opening_that_names_it_and_the_writer_does_not_say_it_again():
    # Measured live 2026-11-11: "What should I do next?" on a quest with only a name opened "This quest has no goals yet" and never named it.
    from datetime import date
    lead = status_snapshot.lead_sentence([], date(2026, 11, 11), quest_name="Learn to play the cello")
    assert lead == '"Learn to play the cello" has no goals yet. A good first step is to add one small goal for this week that starts it.'
    draft = "This quest has no goals yet, so there is no next step on the list. The first one to set is a goal for your first practice session."
    assert status_snapshot.without_restated_lead(draft, lead) == "The first one to set is a goal for your first practice session."
    plan = status_snapshot.no_goals_lead("Learn to play the cello", plan=True)
    assert plan.startswith('"Learn to play the cello" has no goals yet') and "proposed" in plan
    snap = status_snapshot.build(EmptyRegistry(), "q1", plan=True, quest_name="Learn to play the cello")
    assert snap["lead"] == plan and "Learn to play the cello" in snap["text"]


def test_a_plan_preamble_before_numbered_day_lines_is_dropped():
    # Measured live 2026-11-11: "Here are three proposed first goals, each one for this period:" sat above "1. Proposal, Wednesday 11 November: ...".
    text = "This quest has no goals yet. Here are three proposed first goals:\n\n1. Wednesday 11 November: Play one piece\n2. Thursday 12 November: Bowing"
    assert status_snapshot.without_plan_preamble(text).startswith("1. Wednesday 11 November")


def test_a_clause_saying_the_lead_goal_due_day_again_is_dropped_and_the_new_fact_stays():
    # Measured live 2026-11-11 after a lead ending 'The longest overdue is "Book the race entry", due 8 November, so start there.'
    lead = ('As of 11 November, 1 of the 4 goals in "Run my first half marathon" is done ("Run 5 km without stopping"), 1 is overdue, and 2 are due by '
            '18 November. The longest overdue is "Book the race entry", due 8 November, so start there.')
    draft = "It was due 8 November and is now 3 days overdue, and the race is 14 March, so an open entry is the one thing that could cost you the start."
    assert status_snapshot.without_restated_lead(draft, lead) == "The race is 14 March, so an open entry is the one thing that could cost you the start."
    assert status_snapshot.without_restated_lead("It was due 8 November.", lead) == ""
    # a clause with its own content keeps the date
    assert status_snapshot.without_restated_lead("Book it today, since it was due 8 November.", lead) == "Book it today, since it was due 8 November."


def test_a_sentence_that_only_points_at_the_lead_goal_is_dropped_and_a_count_clause_gives_way_to_its_new_fact():
    # Measured live 2026-11-11 (run 13): "The first thing to do is that one." and "Both goals in ... are still open, with one due by 18 November, and
    # photos are what you need before the shop can go live."
    lead = ('As of 11 November, 1 of the 4 goals in "Run my first half marathon" is done ("Run 5 km without stopping"), 1 is overdue, and 2 are due by '
            '18 November. The longest overdue is "Book the race entry", due 8 November, so start there.')
    assert status_snapshot.without_restated_lead('The first thing to do is "Book the race entry".', lead) == ""
    assert status_snapshot.without_restated_lead("Book the race entry first, since entries can close before race day.", lead) == \
        "Do that one first, since entries can close before race day."
    pottery = 'As of 11 November, none of the 2 goals in "Launch my pottery shop" is done yet, none is overdue, and 1 is due by 18 November.'
    draft = ('The next step is "Photograph the first ten pieces," due 15 November. Both goals in "Launch my pottery shop" are still open, with one due '
             'by 18 November, and photos are what you need before the shop can go live.')
    assert status_snapshot.without_restated_lead(draft, pottery) == \
        'The next step is "Photograph the first ten pieces," due 15 November. Photos are what you need before the shop can go live.'


def test_a_status_snapshot_holds_the_outcome_and_the_plan_strategies_so_progress_toward_it_can_be_said():
    # Measured live 2026-11-11: "How is this quest going?" on "Run my first half marathon" (outcome dated 14 March, one strategy) answered with the
    # counts and the overdue goal only. The snapshot now hands the writer the outcome and each strategy with its linked goals.
    tables = {"solutions": {"s1": {"name": "Build a weekly running base", "approach": "Add 10 percent a week"}, "s2": {"name": "Race-day kit"}}}
    rows = [{"title": "Run 5 km", "completed": True, "spec_solution_id": "s1"}, {"title": "Long run", "due": "2026-11-14", "spec_solution_id": "s1"}]
    part = status_snapshot.outcome_part("I finish a half marathon in under 2:15 by 14 March", tables, rows)
    assert '"I finish a half marathon in under 2:15 by 14 March"' in part
    assert "- Build a weekly running base (Add 10 percent a week): 2 goals linked, 1 done" in part
    assert "- Race-day kit: no goal is linked to it yet" in part
    assert status_snapshot.outcome_part("", {}, rows) == ""
    snap = status_snapshot.build(Registry(), "q1", quest_name="Run my first half marathon", quest_outcome="I finish a half marathon by 14 March")
    assert "I finish a half marathon by 14 March" in snap["text"]


def test_a_sentence_that_opens_on_the_lead_goal_keeps_only_what_comes_after_then():
    # Measured live 2026-11-11 (run 16), "How is this quest going?": after a lead naming "Book the race entry", due 8 November, the writer wrote
    # 'Start with that one, which is 3 days overdue, then "Long run ..." and "Buy proper running shoes", both due 15 November.'
    lead = ('As of 11 November, 1 of the 4 goals in "Run my first half marathon" is done ("Run 5 km without stopping"), 1 is overdue, and 2 are due by '
            '18 November. The longest overdue is "Book the race entry", due 8 November, so start there.')
    draft = ('Book the race entry first, which is 3 days overdue, then "Long run of 12 km on Saturday" and "Buy proper running shoes", both due 15 November. '
             'The race outcome is under 2:15 by 14 March.')
    out = status_snapshot.without_restated_lead(draft, lead)
    assert out.startswith('After that, "Long run of 12 km on Saturday" and "Buy proper running shoes", both due 15 November.')
    assert "3 days" not in out and "race entry" not in out and "14 March" in out


def test_the_status_opening_quotes_the_outcome_and_an_idle_strategy_and_a_bare_reply_is_followed_by_the_next_goal():
    # Measured live 2026-11-11 (runs 14 to 17): told to say progress toward the outcome, the writer did on some turns and on others wrote nothing past
    # the counts, or "The first sentence is written for you, so here is what the snapshot adds." The outcome and the next goal are now code-written.
    from datetime import date
    today = date(2026, 11, 11)
    tables = {"solutions": {"s1": {"name": "Build a weekly running base"}}}
    rows = [{"title": "Run 5 km", "completed": True}, {"title": "Book the race entry", "due": "2026-11-08"},
            {"title": "Long run of 12 km", "due": "2026-11-14"}, {"title": "Buy shoes", "due": "2026-11-15"}]
    aim = status_snapshot.aim_sentence("I finish a half marathon in under 2:15 by 14 March.", tables, rows)
    assert aim == ('The quest works toward "I finish a half marathon in under 2:15 by 14 March", and the plan\'s strategy "Build a weekly running base" '
                   'has no goal linked to it yet.')
    lead = status_snapshot.lead_sentence(rows, today, quest_name="Run my first half marathon", aim=aim)
    assert lead.index("is done") < lead.index("works toward") < lead.index("The longest overdue is \"Book the race entry\"")
    assert status_snapshot.lead_days_late(lead) == {"3"}        # the aim's 14 March is not read as the overdue goal's due day
    # the outcome said again goes; a sentence that adds a fact stays
    draft = "The outcome is to finish a half marathon in under 2:15 by 14 March. Running 5 km is the base the long runs build on."
    assert status_snapshot.without_restated_lead(draft, lead) == "Running 5 km is the base the long runs build on."
    follow = status_snapshot.follow_sentence(rows, today)
    assert follow == 'After that, "Long run of 12 km" is due 14 November.'
    assert status_snapshot.status_rest("", follow) == follow
    assert status_snapshot.status_rest("Running 5 km is the base. Would you like me to add a goal?", follow) == "Running 5 km is the base."
    assert status_snapshot.follow_sentence(rows[:2], today) == ""
    # a quest with no goals: the opening quotes the outcome it works toward
    assert status_snapshot.lead_sentence([], today, quest_name="Learn to play the cello",
                                         aim=status_snapshot.aim_sentence("I play a simple piece on the cello", {}, [])) == (
        '"Learn to play the cello" has no goals yet. It works toward "I play a simple piece on the cello". A good first step is to add one small goal for this week that starts it.')


def test_a_status_read_lists_every_open_goal_so_the_next_goal_and_its_date_are_in_evidence():
    # Measured live 2026-11-11 (run 18): "Set up the shop page", due 30 November, was "not listed in the status read", and the code-written
    # 'After that, "Choose the illustrator" is due 31 December.' was cut by the figure check as a date nothing read held.
    from datetime import date
    rows = [{"title": "Finish the second draft", "due": "2026-11-08"}, {"title": "Choose the illustrator", "due": "2026-12-31"}, {"title": "Pick a title"}]
    text = status_snapshot.overview(rows, date(2026, 11, 11))
    assert "- Choose the illustrator (due 31 December)" in text and "- Pick a title (due no date)" in text
    assert "Choose the illustrator" not in status_snapshot.overview(rows, date(2026, 11, 11), plan=True).split("Open goals with no date")[0].split("Day layout")[0]


def test_a_writer_sentence_quoting_the_lead_done_goal_or_strategy_goes():
    # Measured live 2026-11-11 (run 18), "How is this quest going?" on "Run my first half marathon"
    from datetime import date
    rows = [{"title": "Run 5 km without stopping", "completed": True}, {"title": "Book the race entry", "due": "2026-11-08"},
            {"title": "Long run of 12 km", "due": "2026-11-15"}]
    aim = status_snapshot.aim_sentence("I finish a half marathon in under 2:15 by 14 March", {"solutions": {"s": {"name": "Build a weekly running base"}}}, rows)
    lead = status_snapshot.lead_sentence(rows, date(2026, 11, 11), quest_name="Run my first half marathon", aim=aim)
    draft = ('The quest is at an early stage: 1 of its 3 goals is done ("Run 5 km without stopping"), and the outcome, a sub-2:15 half marathon by 14 March, '
             'is still ahead. The plan\'s first strategy, "Build a weekly running base," has no goal linked to it yet. Longer runs each week are what close the gap to race distance.')
    assert status_snapshot.without_restated_lead(draft, lead) == "Longer runs each week are what close the gap to race distance."


def test_a_status_reply_keeps_only_sentences_that_hold_a_fact_and_drops_a_leftover_fragment():
    # Measured live 2026-11-11 (run 19)
    stems = status_snapshot.record_words(["Write a children's book", "I send a finished manuscript to ten agents by June",
                                          "Finish the second draft", "Choose the illustrator", "Book the race entry"])
    meta = ("The opening line covers the goal counts and the overdue goal, so here is what stands between the quest and its outcome. "
            "\"Choose the illustrator\" follows, due 31 December.")
    assert status_snapshot.status_rest(meta, "", stems) == "\"Choose the illustrator\" follows, due 31 December."
    assert status_snapshot.holds_a_fact("Race entries often sell out, so book it today.", stems)      # "entries" shares the stem of "entry"
    fragment = "Your quest\n\n**Choose the simple piece you want to learn first.** Picking one piece gives every session a target."
    assert status_snapshot.status_rest(fragment, "", None).startswith("**Choose the simple piece")
    assert status_snapshot.status_rest("The opening line covers it all.", "After that, \"X\" is due 1 December.", stems) == 'After that, "X" is due 1 December.'


def test_a_plan_with_no_day_line_gets_the_code_layout_as_its_days():
    # Measured live 2026-11-11 (run 19): "Plan my week" answered with no day line and a question about something else
    from datetime import date
    layout = status_snapshot.day_layout([{"title": "Finish the second draft", "due": "2026-11-08"}], date(2026, 11, 11))
    text = 'Start with "Finish the second draft". The rest of this week can go to the draft.\n\nWant me to break it into smaller steps?'
    out = status_snapshot.plan_with_days(text, layout)
    assert out == "- Wednesday 11 November: Finish the second draft (overdue, due 8 November)\n\n" + status_snapshot.PLAN_CLOSE
    kept = "- Wednesday 11 November: Finish the second draft\n\nNothing is saved yet."
    assert status_snapshot.plan_with_days(kept, layout) == kept
    assert status_snapshot.plan_with_days(text, "") == text


def test_a_sentence_that_opens_on_the_lead_own_words_keeps_what_comes_after_then():
    # Measured live 2026-11-11 (run 20): '... so start there. Start there, then the long run of 12 km on Saturday (15 November) ... come next.'
    lead = ('As of 11 November, 1 of the 4 goals in "Run my first half marathon" is done ("Run 5 km without stopping"), 1 is overdue, and 2 are due by '
            '18 November. The longest overdue is "Book the race entry", due 8 November, so start there.')
    draft = "Start there, then the long run of 12 km on Saturday (15 November) and the running shoes (also 15 November) come next."
    assert status_snapshot.without_restated_lead(draft, lead) == ("After that, the long run of 12 km on Saturday (15 November) and the running shoes "
                                                                  "(also 15 November) come next.")
    keep = "Photos come first, then the shop page can go live."
    assert status_snapshot.without_restated_lead(keep, lead) == keep


def test_a_quest_with_no_goals_opens_without_a_promise_and_a_step_beside_the_claim_is_kept():
    # Measured live 2026-11-11 (run 22): the opening promised "here is a proposed first goal" and the writer's step was cut with the clause
    # "since ... has no goals yet", so the reply promised a goal and gave none.
    from datetime import date
    lead = status_snapshot.lead_sentence([], date(2026, 11, 11), quest_name="Learn to play the cello",
                                         aim=status_snapshot.aim_sentence("I play a simple piece on the cello", {}, []))
    assert lead == '"Learn to play the cello" has no goals yet. It works toward "I play a simple piece on the cello". A good first step is to add one small goal for this week that starts it.'
    draft = ('Your first step is to pick the simple piece you want to learn first, since "Learn to play the cello" has no goals yet. '
             'Its outcome is "I play a simple piece on the cello", and nothing has been recorded toward it so far.')
    assert status_snapshot.without_restated_lead(draft, lead) == "Your first step is to pick the simple piece you want to learn first."


def test_snapshot_records_name_the_outcome_and_the_plan_strategies():
    tables = {"solutions": {"esr_1": {"name": "Build a weekly running base"}, "esr_2": {"name": "  "}, "esr_3": {"name": "Taper  before race day"}}}
    rows = [{"id": "g1", "title": "Book the race entry"}]
    records = status_snapshot.snapshot_records("q1", "Run my first half marathon", rows, " I finish a half marathon\nin under 2:15 ", tables)
    assert [(r["ref"], r["label"], r["kind"]) for r in records] == [
        ("q1", "Run my first half marathon", "quest"), ("q1#outcome", "I finish a half marathon in under 2:15", "outcome"),
        ("esr_1", "Build a weekly running base", "strategy"), ("esr_3", "Taper before race day", "strategy"), ("g1", "Book the race entry", "goal")]
    assert [r["kind"] for r in status_snapshot.snapshot_records("q1", "Q", rows)] == ["quest", "goal"]      # no outcome, no tables: as before
