"""Which milestone a short cue concerns, from synthetic spec records: the right one, the first-listed one when two are close, none when nothing fits."""
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue

TABLES = {
    "solutions": {
        "esr_a": {"name": "P1 Onboarding that captivates", "milestones": "Onboarding test passes on web and mobile.", "owner_user_ids": ["Ann", "Bo"]},
        "esr_b": {"name": "P2 Mobile app live on the Play Store", "milestones": "App live in the Play Store.", "owner_user_ids": ["Cy"]},
        "esr_c": {"name": "P3 Mobile app live on the Apple Store", "milestones": "App live in the App Store.", "owner_user_ids": ["Cy", "Di"]},
        "esr_d": {"name": "P5 Foundational product videos published", "milestones": "The agreed videos are published.", "owner_user_ids": ["Ann", "Ed"]},
        "esr_x": {"name": "Ann: daily check", "milestones": "", "purpose": ""},
    },
    "metrics": {
        "esr_ma": {"name": "P1 Onboarding completion", "record": "50 percent", "solution_ids": ["esr_a"]},
        "esr_mb": {"name": "P2 Play Store listing live", "record": "No", "solution_ids": ["esr_b"]},
        "esr_mc": {"name": "P3 App Store listing live", "record": "No. Apple lookup returned 0.", "solution_ids": ["esr_c"]},
        "esr_md": {"name": "P5 Foundational videos published", "record": "0 of 6", "solution_ids": ["esr_d"]},
    },
}


import datetime
import json

import pytest

from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

CUE = "lets get the apple store release moving"


@pytest.fixture(autouse=True)
def cheap_model(monkeypatch):
    """The turn's one cheap model call, answered offline: the milestone judgment picks the first candidate listed, any other question is answered NONE."""
    import re
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator

    def fake(self, prompt):
        found = re.search(r"^(esr_\w+):", prompt, re.M) if "These are milestones of the quest" in prompt else None
        return found.group(1) if found else "NONE"
    monkeypatch.setattr(Orchestrator, "cheap_decision", fake)
GOALS = [{"id": "g1", "title": "esr_c next step: week of 2026-10-26", "spec_solution_id": "esr_c", "completed": False, "abandoned": False, "due": "2026-11-01",
          "assignee_name": "Cy"},
         {"id": "g2", "title": "Unrelated", "spec_solution_id": None, "completed": False, "abandoned": False, "due": None}]


def read_spec(zone="America/Los_Angeles", fail=False):
    def handler(args, ctx):
        if fail:
            return ToolResult(ok=False, text="the specification could not be read just now (HTTP 503)")
        return ToolResult(ok=True, data={"quest_id": args["quest_id"], "records": TABLES, "time_zone": zone}, text="read")
    return ToolSpec(name="read_spec_records", description="d", mutates=False, handler=handler)


def list_goals(goals=GOALS):
    return ToolSpec(name="list_quest_goals", description="d", mutates=False, handler=lambda args, ctx: ToolResult(ok=True, data=goals, text="listed"))


def write_goal(calls, refuse=()):
    def handler(args, ctx):
        calls.append(dict(args))
        if args.get("assignee") in refuse:
            return ToolResult(ok=False, text=f"Nothing was written: assignee: {args['assignee']} is not on the quest")
        return ToolResult(ok=True, status=ToolStatus.APPLIED, text=f"Created the goal {args['title']!r}.")
    return ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True, handler=handler)


def registry(calls=None, **kw):
    return ToolRegistry([read_spec(**kw), list_goals(), write_goal(calls if calls is not None else [])])


def test_a_cue_finds_its_milestone_and_the_facts_carry_the_keys_owners_and_goals_already_there():
    picked = cue.select(TABLES, CUE)
    assert picked and picked[0] == "esr_c"
    block = cue.fact_block(TABLES, CUE, monday="2026-11-02", goals=GOALS, zone="America/Los_Angeles")
    assert "esr_c" in block and "esr_mc" in block and "owner_user_ids: Cy, Di" in block and "P3 Mobile app live on the Apple Store next step: week of 2026-11-02" in block
    assert "Current value: No." in block and "Open goals on the quest that may be" in block and "already there" in block and "esr_c next step: week of 2026-10-26" in block
    assert "Unrelated" not in block and "America/Los_Angeles" in block


def test_the_facts_give_the_increment_order_and_the_full_purpose_text_for_the_model_to_assign_from():
    block = cue.fact_block(TABLES, CUE, monday="2026-11-02")
    assert block.index("1. a deliverable") < block.index("2. else a metric value") < block.index("3. else an owned next step")
    tables = {**TABLES, "solutions": {**TABLES["solutions"], "esr_c": {**TABLES["solutions"]["esr_c"], "owner_user_ids": [], "purpose": "Owners: Zed."}}}
    block = cue.fact_block(tables, CUE)
    assert "Owners: Zed." in block and "owner_user_ids: empty" in block and "never ask who should own it" in block


def test_another_cue_finds_another_milestone():
    assert cue.select(TABLES, "can we get the first product video moving?")[0] == "esr_d"
    assert cue.select(TABLES, "the onboarding test")[0] == "esr_a"


def test_a_close_call_goes_to_the_one_the_quest_lists_first():
    picked = cue.select(TABLES, "get the mobile app live")
    assert picked[0] == "esr_b" and picked[2] == ["esr_c"]
    assert "Close runners-up" in cue.fact_block(TABLES, "get the mobile app live")


def test_no_match_gives_nothing_and_a_personal_strategy_is_not_a_milestone():
    assert cue.select(TABLES, "write me a poem about tea") is None and cue.fact_block(TABLES, "hello") == ""
    assert cue.select(TABLES, "daily check") is None


def test_the_week_is_read_in_the_quests_own_time_zone():
    late_sunday_utc = datetime.datetime(2026, 11, 9, 3, 0, tzinfo=datetime.timezone.utc)      # still Sunday 2026-11-08 in Los Angeles
    resolved = cue.resolve(registry(), "q1", CUE, late_sunday_utc)
    assert resolved.week.monday == "2026-11-02" and resolved.week.due == "2026-11-08" and resolved.week.period == "2026_W45"
    assert cue.resolve(registry(zone=""), "q1", CUE, late_sunday_utc).week.monday == "2026-11-09"       # no zone recorded: UTC


def test_resolving_reads_the_records_and_the_goals_and_holds_facts_for_the_planner():
    resolved = cue.resolve(registry(), "q1", CUE, datetime.datetime(2026, 11, 4, 18, 0, tzinfo=datetime.timezone.utc))
    assert resolved.rid == "esr_c" and resolved.owners == ["Cy", "Di"] and "CUE FACTS" in resolved.facts and "week of 2026-11-02" in resolved.facts
    assert cue.resolve(ToolRegistry([]), "q1", CUE) is None and cue.resolve(None, "q1", "x") is None and cue.resolve(registry(), "q1", "write me a poem about tea") is None


def test_a_failed_read_is_a_lane_gap_not_an_empty_hint():
    with pytest.raises(cue.LaneGap, match="could not be read"):
        cue.resolve(registry(fail=True), "q1", CUE)
    with pytest.raises(cue.LaneGap, match="goals"):
        cue.resolve(ToolRegistry([read_spec()]), "q1", CUE)


def test_the_flow_step_writes_the_keyed_next_step_to_an_owner_and_returns_the_receipt():
    calls = []
    resolved = cue.resolve(registry(calls), "q1", CUE, datetime.datetime(2026, 11, 4, 18, 0, tzinfo=datetime.timezone.utc))
    receipt, status = resolved.carry_out(registry(calls))
    assert status == "applied" and "RECEIPT written by code" in receipt and "do not offer to do it" in receipt
    assert calls[0]["title"] == "P3 Mobile app live on the Apple Store next step: week of 2026-11-02" and calls[0]["assignee"] == "Cy" and calls[0]["spec_solution_id"] == "esr_c"
    assert calls[0]["spec_metric_id"] == "esr_mc" and calls[0]["period"] == "2026_W45" and calls[0]["due_date"] == "2026-11-08"


def test_an_owner_quest_does_not_know_is_skipped_and_none_accepted_writes_it_unassigned_and_says_so():
    calls = []
    resolved = cue.resolve(registry(calls), "q1", CUE, datetime.datetime(2026, 11, 4, 18, 0, tzinfo=datetime.timezone.utc))
    receipt, _ = resolved.carry_out(ToolRegistry([write_goal(calls, refuse=("Cy",))]))
    assert [c.get("assignee") for c in calls] == ["Cy", "Di"] and "Created" in receipt
    calls.clear()
    receipt, _ = resolved.carry_out(ToolRegistry([write_goal(calls, refuse=("Cy", "Di"))]))
    assert "assignee" not in calls[-1] and "owner could not be set" in receipt


def test_a_write_the_tool_refuses_or_a_lane_without_the_tool_is_a_lane_gap():
    resolved = cue.resolve(registry(), "q1", CUE)
    refusing = ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True,
                        handler=lambda args, ctx: ToolResult(ok=False, status=ToolStatus.HELD, text="Not applied yet: waiting for a person to approve it."))
    with pytest.raises(cue.LaneGap, match="waiting for a person"):
        resolved.carry_out(ToolRegistry([refusing]))
    with pytest.raises(cue.LaneGap, match="no create_or_update_quest_goal tool"):
        resolved.carry_out(ToolRegistry([]))


def test_the_third_rung_runs_only_for_an_act_turn_that_took_no_higher_rung():
    from hubofhubs.api import PlanDecision
    resolved = cue.resolve(registry(), "q1", CUE)
    tools = registry()
    assert resolved.routes_to_code(PlanDecision(action="deep"), "act", [], tools)
    assert resolved.routes_to_code(PlanDecision(action="confirm"), "act", [], tools)          # no permission asking for an action cue
    assert not resolved.routes_to_code(PlanDecision(action="answer"), "ask", [], tools)       # a question changes nothing
    assert resolved.routes_to_code(PlanDecision(action="answer", deep_target="draft_in_chat"), "act", [], tools)      # a deliverable meant for the reply does not exempt the kept step
    assert not resolved.routes_to_code(PlanDecision(action="tool"), "act", [], tools)
    assert not resolved.routes_to_code(PlanDecision(action="answer"), "act", [{"locator": "tool:create_or_update_quest_goal"}], tools)   # rung 2 ran: a write is done


def test_a_pinned_turn_skips_the_broad_retrieval_and_the_code_records_the_step_and_the_plan_becomes_the_answer():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    calls = []
    provider = StubProvider([{"action": "deep", "user_intent": "act", "rationale": "r", "pending_read": [], "deep_brief": "do it"}])
    config = OrchestratorConfig()
    config.overseer = False
    result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config,
                          tools=registry(calls)).run(CUE, quest_id="q1")
    assert calls and calls[0]["title"].startswith("P3 Mobile app live on the Apple Store next step: week of ") and result.kind != "deep"
    assert "CUE FACTS (found by code" in provider.plan_prompts[0]


def test_a_lane_gap_is_shown_to_the_reply_not_swallowed():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    provider = StubProvider([{"action": "answer", "user_intent": "act", "rationale": "r", "pending_read": []}])
    config = OrchestratorConfig()
    config.overseer = False
    result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config,
                          tools=registry(fail=True)).run(CUE, quest_id="q1")
    assert "LANE GAP" in provider.plan_prompts[0] and "could not be read" in provider.plan_prompts[0] and "CUE FACTS (found by code" not in provider.plan_prompts[0]


def test_the_cue_facts_are_read_as_a_milestone_measure_for_the_status_message():
    from quest_ai_runner.the_intelligence.mind.response_writer import act_status
    block = cue.fact_block(TABLES, CUE, monday="2026-11-02")
    reading = act_status.pick_reading(act_status.readings_in([{"text": block}]), "apple store release")
    assert reading.milestone.startswith("P3 Mobile app live on the Apple Store") and reading.value.startswith("No") and "App Store" in reading.done_when


def test_a_replan_drops_card_context_reads_when_the_turn_holds_its_context():
    from quest_ai_runner.the_intelligence.mind.delegation.orchestrator_capabilities import split_context_reassembly
    reads = [{"cards": "anything"}, {"grep": "x"}]
    assert split_context_reassembly(reads, True) == ([{"grep": "x"}], [{"cards": "anything"}])
    assert split_context_reassembly(reads, False) == (reads, [])


def test_reading_a_pinned_quests_records_goes_through_no_placement_and_carries_the_quests_time_zone(monkeypatch):
    import quest_ai_runner.api as api
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_intents import read_spec_records_spec
    from quest_ai_runner.the_world.tools.tools import ToolContext

    class Client:
        def get_spec(self, subject_type, subject):
            return {"records": [{"type": "solution", "recordId": "esr_1", "fields": {"name": "A", "owner_user_ids": ["Ann"]}}]}

        def get_quest_autopilot(self, quest_id):
            return {"autopilot": {"run_timezone": "Asia/Kolkata"}}
    monkeypatch.setattr(api, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the assembly (and quest placement) must not run for a pinned read")))
    spec = read_spec_records_spec({}, client_factory=Client)
    done = spec.handler({}, ToolContext(quest_id="q1"))
    assert done.ok and done.data["time_zone"] == "Asia/Kolkata" and done.data["records"]["solutions"]["esr_1"]["owner_user_ids"] == ["Ann"]


def test_the_default_arm_of_place_on_a_quest_has_a_module_that_registers_it():
    import importlib
    import json
    from pathlib import Path
    entry = json.loads((Path(cue.__file__).parents[3] / "esoi_selection.json").read_text())["place-on-a-quest"]
    assert entry["default"] in entry["modules"], "the shipped default names no module that registers it"
    module = importlib.import_module(entry["modules"][entry["default"]])
    assert getattr(module.HostFunction, "__name__", "") == "HostFunction"


OLD_GOAL = {"id": "g9", "title": "P3 next step: week of 2026-11-02", "text": "send the listing", "spec_solution_id": None, "completed": False, "abandoned": False, "due": "2026-11-08"}


def test_an_old_format_goal_sharing_the_milestones_code_and_words_is_listed_as_a_candidate_and_finished_or_unrelated_ones_are_not():
    name = "P3 Mobile app live on the Apple Store"
    done = dict(OLD_GOAL, id="g8", completed=True)
    other = dict(OLD_GOAL, id="g7", title="Plan the offsite")
    found = cue.candidate_goals([OLD_GOAL, done, other, *GOALS], "esr_c", name)
    assert [g["id"] for g in found] == ["g1", "g9"]      # g1 is linked by the record and comes first; the finished, unrelated and unlinked ones are not listed
    assert "P3 next step: week of 2026-11-02" in cue.fact_block(TABLES, CUE, monday="2026-11-09", goals=[OLD_GOAL])


def test_the_model_decides_the_existing_goal_and_the_owner_from_the_record_text_and_the_goal_is_updated_in_place():
    tables = {**TABLES, "solutions": {**TABLES["solutions"], "esr_c": {**TABLES["solutions"]["esr_c"], "owner_user_ids": [], "purpose": "Owners: Cy and Di."}}}

    class Tools(ToolRegistry):
        pass
    calls = []
    read = ToolSpec(name="read_spec_records", description="d", mutates=False,
                    handler=lambda args, ctx: ToolResult(ok=True, data={"records": tables, "time_zone": "UTC"}, text="read"))
    tools = ToolRegistry([read, list_goals([OLD_GOAL]), write_goal(calls)])
    resolved = cue.resolve(tools, "q1", CUE, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
    asked = []

    def decide(prompt):
        asked.append(prompt)
        return json.dumps({"owner": "Cy", "goal_id": "g9", "draft": ""})
    receipt, _ = resolved.carry_out(tools, decide)
    assert len(asked) == 1 and calls[0]["adopt_goal_id"] == "g9" and calls[0]["assignee"] == "Cy" and calls[0]["title"] == "P3 Mobile app live on the Apple Store next step: week of 2026-11-02"
    calls.clear()
    resolved.receipt = ""
    resolved.carry_out(tools, lambda prompt: "NONE")
    assert "adopt_goal_id" not in calls[0] and "assignee" not in calls[0]       # no owner named, no same goal: created unassigned


def test_adopting_an_existing_goal_renames_links_and_moves_it_and_a_repeat_is_already_there():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links

    class Client:
        def __init__(self):
            self.goals = [{"id": "g9", "name": "P3 next step: week of 2026-11-02", "period": "2026_W45", "description": "x"}]
            self.updates = []

        def list_quest_goals(self, quest_id):
            return {"period_groups": [{"goals": self.goals}]}

        def update_goal(self, goal_id, fields):
            self.updates.append((goal_id, fields))
            self.goals[0]["name"] = fields.get("title", self.goals[0]["name"])
            self.goals[0].update({k: v for k, v in fields.items() if k != "title"})
            return object()
    client = Client()
    fields = {"description": "d", "spec_solution_id": "esr_c"}
    first = goal_links.ensure_goal(client, "q1", "esr_c next step: week of 2026-11-09", "2026_W46", fields, None, "g9")
    assert first["state"] == "updated" and client.updates[0][0] == "g9" and client.updates[0][1]["title"] == "esr_c next step: week of 2026-11-09"
    assert client.updates[0][1]["spec_solution_id"] == "esr_c" and len(client.goals) == 1
    again = goal_links.ensure_goal(client, "q1", "esr_c next step: week of 2026-11-09", "2026_W46", fields, None, "g9")
    assert again["state"] == "unchanged" and len(client.updates) == 1


def test_the_title_starts_with_the_milestones_code_and_never_carries_the_record_id_and_the_old_unlinked_goal_is_found_and_adopted():
    calls = []
    tools = ToolRegistry([read_spec(), list_goals([OLD_GOAL]), write_goal(calls)])
    resolved = cue.resolve(tools, "q1", CUE, datetime.datetime(2026, 11, 4, 18, 0, tzinfo=datetime.timezone.utc))
    assert [g["id"] for g in resolved.candidates] == ["g9"] and "esr_c next step" not in resolved.facts.split("Keys:")[1].split("\n")[0]
    resolved.carry_out(tools, lambda prompt: json.dumps({"goal_id": "g9"}))
    assert calls[0]["title"].startswith("P3 ") and "esr_" not in calls[0]["title"] and calls[0]["adopt_goal_id"] == "g9" and calls[0]["spec_solution_id"] == "esr_c"


def test_settle_plan_turns_a_deep_run_or_a_permission_question_into_the_answer_and_keeps_only_work_beyond_the_write():
    from hubofhubs.api import PlanDecision
    resolved = cue.resolve(registry(), "q1", CUE)
    deep = PlanDecision(action="deep", goal="g", deep_brief="b", deferred_deep={"goal": "x"})
    resolved.settle_plan(deep, "RECEIPT")
    assert deep.action == "answer" and deep.deferred_deep is None
    ask = PlanDecision(action="confirm", confirm_question="Do you want me to?")
    resolved.settle_plan(ask, "RECEIPT")
    assert ask.action == "answer" and ask.confirm_question is None
    read = PlanDecision(action="read")
    resolved.settle_plan(read, "RECEIPT")
    assert read.action == "read"
    code = PlanDecision(action="deep", goal="g", deep_brief="build the page", deep_target="code_or_files")
    resolved.settle_plan(code, "RECEIPT: step recorded")
    assert code.action == "deep" and code.deep_brief.startswith("Already done by the runner before this run") and "RECEIPT: step recorded" in code.deep_brief


def test_the_first_message_is_composed_from_the_records_before_the_write_and_no_deep_run_is_launched():
    from hubofhubs.api import EVENT_INTERIM
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    order = []
    calls = []
    inner = write_goal(calls)

    def recording(args, ctx):
        order.append("write")
        return inner.handler(args, ctx)
    tools = ToolRegistry([read_spec(), list_goals(), ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True, handler=recording)])
    provider = StubProvider([{"action": "deep", "user_intent": "act", "rationale": "r", "pending_read": [], "deep_brief": "carry out the request for real", "goal": "carry it out"}])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=tools,
                        config=OrchestratorConfig(interim_replies=True, overseer=False, min_seconds_between_interims=0.0))
    events = []
    result = orch.run(CUE, sink=StreamSink(lambda e: (order.append(e["type"]) if e["type"] == EVENT_INTERIM else None, events.append(e))), quest_id="q1")
    assert order[:2] == [EVENT_INTERIM, "write"]            # the first message, then the write
    first = next(e for e in events if e["type"] == EVENT_INTERIM)
    assert "Where this stands" in first["text"] and "P3 Mobile app live on the Apple Store" in first["text"] and "blocked" not in first["text"].lower()
    assert "Recording this week's next step" in first["text"]
    assert result.kind != "deep" and len(calls) == 1


class GoalClient:
    """A Quest client over a goal list: records every write so a test can say a create (POST) never happened."""

    def __init__(self, goals):
        self.goals = goals
        self.posts, self.puts, self.assigns = [], [], []

    def list_quest_goals(self, quest_id):
        return {"period_groups": [{"goals": self.goals}]}

    def list_assignable_members(self, quest_id):
        return [{"user_id": "u1", "name": "Cy Owner"}]

    def create_goal(self, title, **kw):
        self.posts.append((title, kw))
        goal = {"id": "new", "name": title, "assigned_to_user_id": kw.get("assigned_to_user_id"), "spec_solution_id": kw.get("spec_solution_id"),
                "spec_metric_id": kw.get("spec_metric_id"), "period": kw.get("period")}
        self.goals.append(goal)
        return type("W", (), {"id": "new", "data": {"id": "new"}, "status": None})()

    def update_goal(self, goal_id, fields):
        self.puts.append((goal_id, fields))
        goal = next(g for g in self.goals if g["id"] == goal_id)
        goal.update({("name" if k == "title" else k): v for k, v in fields.items()})
        return type("W", (), {"status": None})()

    def assign_goal(self, goal_id, user_id):
        self.assigns.append((goal_id, user_id))
        next(g for g in self.goals if g["id"] == goal_id)["assigned_to_user_id"] = user_id
        return type("W", (), {"status": None})()


KEYED = "P3 Mobile app live on the Apple Store next step: week of 2026-11-02"


def test_a_goal_already_linked_or_already_titled_is_updated_in_place_and_never_created_again():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    fields = {"spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "description": "d"}
    # the keyed goal exists next to a legacy unlinked one: nothing is created
    client = GoalClient([{"id": "k", "name": KEYED, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "description": "Kept by Quest AI: d", "period": "2026_W45"},
                         {"id": "old", "name": "P3 next step: week of 2026-11-02", "spec_solution_id": None, "period": "2026_W45"}])
    done = goal_links.ensure_goal(client, "q1", KEYED, "2026_W45", fields, None, None, True)
    assert done["state"] == "unchanged" and client.posts == [] and client.puts == []
    # only a linked goal under another title exists (the model picked nothing): it is renamed in place, by code
    client = GoalClient([{"id": "l", "name": "P3 Mobile app live on the Apple Store next step: week of 2026-10-26", "spec_solution_id": "esr_c", "period": "2026_W44"}])
    done = goal_links.ensure_goal(client, "q1", KEYED, "2026_W45", fields, None, None, True)
    assert done["state"] == "updated" and client.posts == [] and client.puts[0][0] == "l" and client.puts[0][1]["title"] == KEYED
    # a finished linked goal is history, not the goal
    client = GoalClient([{"id": "d", "name": "older", "spec_solution_id": "esr_c", "completed": True}])
    assert goal_links.ensure_goal(client, "q1", KEYED, "2026_W45", fields, None, None, True)["state"] == "created" and len(client.posts) == 1


def test_the_receipt_states_the_assignee_name_solution_and_metric_as_read_back_after_the_write(monkeypatch):
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_goals import create_or_update_quest_goal_spec
    from quest_ai_runner.the_world.tools.tools import ToolContext
    client = GoalClient([{"id": "old", "name": "P3 next step: week of 2026-11-02", "spec_solution_id": None, "period": "2026_W45"}])
    spec = create_or_update_quest_goal_spec({}, client_factory=lambda: client)
    client.members = [{"user_id": "u1", "name": "Cy Owner", "email": "x"}]
    import quest_ai_runner.the_body.hands.quest_operations.goal_links as gl
    monkeypatch.setattr(gl, "resolve_assignee", lambda c, q, who: ("u1", None))      # undone at the end of the test, so no other test sees it
    result = spec.handler({"quest_id": "q1", "title": KEYED, "assignee": "Cy", "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "match_by_link": True,
                           "adopt_goal_id": "old", "user_asked_for_this_field": True}, ToolContext(quest_id="q1"))
    assert result.ok and "assigned to Cy Owner" in result.text and "solution esr_c" in result.text and "metric esr_mc" in result.text and client.posts == []
    again = spec.handler({"quest_id": "q1", "title": KEYED, "assignee": "Cy", "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "match_by_link": True,
                          "user_asked_for_this_field": True}, ToolContext(quest_id="q1"))
    assert "already" in again.text and client.posts == []


def test_the_code_step_is_in_the_execution_record_so_no_net_re_runs_the_work_as_a_deep_run():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    provider = StubProvider([{"action": "answer", "user_intent": "act", "rationale": "r", "pending_read": []}])
    config = OrchestratorConfig()
    config.overseer = False
    result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=registry([])).run(CUE, quest_id="q1")
    record = result.execution_record
    assert record is not None and record.any_success and any("create_or_update_quest_goal" in f.goal for f in record.facts)


def test_the_receipt_leads_with_created_updated_or_already_set_from_the_tools_typed_state():
    resolved = cue.resolve(registry(), "q1", CUE)
    leads = {state: resolved.lead_sentence(state) for state in ("created", "updated", "unchanged")}
    assert "already set" in leads["unchanged"] and "nothing was changed" in leads["unchanged"] and "updated" in leads["updated"] and "created" in leads["created"]
    for state, lead in leads.items():
        noop = ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True,
                        handler=lambda args, ctx, state=state: ToolResult(ok=True, status=ToolStatus.NOOP if state == "unchanged" else ToolStatus.APPLIED, data={"state": state}, text="done"))
        receipt, _ = resolved.carry_out(ToolRegistry([noop]))
        assert f'Open the reply with exactly this sentence: "{lead}"' in receipt
        resolved.receipt = ""


def test_a_milestone_act_cue_sends_two_messages_the_status_first_and_the_result():
    from hubofhubs.api import EVENT_INTERIM, EVENT_RESULT
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    provider = StubProvider([{"action": "answer", "user_intent": "act", "rationale": "r", "pending_read": []}])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=registry([]),
                        config=OrchestratorConfig(interim_replies=True, overseer=False, min_seconds_between_interims=0.0))
    events = []
    orch.run(CUE, sink=StreamSink(events.append), quest_id="q1")
    kinds = [e["type"] for e in events if e["type"] in (EVENT_INTERIM, EVENT_RESULT)]
    assert kinds[:1] == [EVENT_INTERIM] and kinds.count(EVENT_INTERIM) == 1 and EVENT_RESULT in kinds


def test_the_first_message_names_no_record_id_and_none_of_the_planners_fact_blob():
    from hubofhubs.api import EVENT_INTERIM
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    provider = StubProvider([{"action": "answer", "user_intent": "act", "rationale": "r", "pending_read": [],
                              "facts_read": ["Metric record esr_mc (P3 App Store listing live)", "Owners named: Cy, Di"]}])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=registry([]),
                        config=OrchestratorConfig(interim_replies=True, overseer=False, min_seconds_between_interims=0.0))
    events = []
    orch.run(CUE, sink=StreamSink(events.append), quest_id="q1")
    first = next(e for e in events if e["type"] == EVENT_INTERIM)["text"]
    assert "esr_" not in first and "Owners named" not in first and "Metric record" not in first
    assert "P3 Mobile app live on the Apple Store" in first and "App Store" in first and "Current value" in first and "Now:" in first


class StaleListingClient(GoalClient):
    """A Quest that accepts a write and reports success but whose goal keeps no links: the read-back must say so."""

    def update_goal(self, goal_id, fields):
        self.puts.append((goal_id, fields))
        goal = next(g for g in self.goals if g["id"] == goal_id)
        goal.update({("name" if k == "title" else k): v for k, v in fields.items() if k not in ("spec_solution_id", "spec_metric_id")})
        return type("W", (), {"status": None})()


def run_tool(client, **extra):
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_goals import create_or_update_quest_goal_spec
    from quest_ai_runner.the_world.tools.tools import ToolContext
    spec = create_or_update_quest_goal_spec({}, client_factory=lambda: client)
    args = {"quest_id": "q1", "title": KEYED, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "match_by_link": True, "user_asked_for_this_field": True, **extra}
    return spec.handler(args, ToolContext(quest_id="q1"))


def test_a_link_the_quest_does_not_show_is_never_reported_as_linked_and_the_link_is_retried_alone_once():
    client = StaleListingClient([{"id": "old", "name": "P3 next step: week of 2026-11-02", "spec_solution_id": None, "spec_metric_id": None, "period": "2026_W45"}])
    result = run_tool(client, adopt_goal_id="old")
    assert "not linked to a solution" in result.text and "NOT confirmed" in result.text and "solution link is not on the goal" in result.text
    assert "linked to solution esr_c" not in result.text
    assert client.puts[-1][1] == {"spec_solution_id": "esr_c", "spec_metric_id": "esr_mc"}       # the links alone, once, so a refusal would carry its reason


def test_updated_is_said_only_when_the_read_back_differs_from_before_and_a_clean_read_has_no_could_not_be_read_line():
    held = {"id": "k", "name": KEYED, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "period": "2026_W45", "assigned_to_user_id": None, "description": "Kept by Quest AI:"}
    client = GoalClient([dict(held)])
    result = run_tool(client)
    assert "nothing was changed" in result.text and "linked to solution esr_c" in result.text and "could not be read" not in result.text and client.puts == []
    client = GoalClient([dict(held, spec_solution_id=None, spec_metric_id=None)])
    result = run_tool(client)
    assert "updated in place" in result.text and "linked to solution esr_c" in result.text and "metric esr_mc" in result.text and "NOT confirmed" not in result.text


def test_a_failed_read_is_the_only_time_the_receipt_says_the_goals_could_not_be_read():
    class Unreadable(GoalClient):
        calls = 0

        def list_quest_goals(self, quest_id):
            Unreadable.calls += 1
            return {"period_groups": [{"goals": self.goals}]} if Unreadable.calls == 1 else {}
    result = run_tool(Unreadable([{"id": "k", "name": KEYED, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc"}]))
    assert "could not be read again" in result.text


class CachedListingClient(GoalClient):
    """The backend's per-process cache: a listing right after a write still shows the old goal, but the write's own response returns the saved goal."""

    def __init__(self, goals):
        super().__init__(goals)
        self.stale = [dict(g) for g in goals]
        self.saved = {g["id"]: dict(g) for g in goals}

    def list_quest_goals(self, quest_id):
        return {"period_groups": [{"goals": self.stale}]}

    def update_goal(self, goal_id, fields):
        self.puts.append((goal_id, fields))
        goal = self.saved[goal_id]
        goal.update({("name" if k == "title" else k): v for k, v in fields.items()})
        return type("W", (), {"status": None, "data": {"id": goal_id, "title": goal["name"], "specSolutionId": goal.get("spec_solution_id"),
                                                       "specMetricId": goal.get("spec_metric_id"), "assignedToUserId": goal.get("assigned_to_user_id"),
                                                       "period": goal.get("period")}})()


def test_a_found_goal_without_the_links_gets_them_written_and_the_write_response_is_the_read_back_not_a_cached_listing():
    client = CachedListingClient([{"id": "k", "name": KEYED, "spec_solution_id": None, "spec_metric_id": None, "period": "2026_W45", "assigned_to_user_id": "u1"}])
    result = run_tool(client)
    assert any(p[1].get("spec_solution_id") == "esr_c" and p[1].get("spec_metric_id") == "esr_mc" for p in client.puts)
    assert "linked to solution esr_c" in result.text and "metric esr_mc" in result.text and "NOT confirmed" not in result.text
    assert result.data["state"] == "updated"
    # a Quest that answers the write but returns the goal without the links is reported as such, with no claim of linking
    client = CachedListingClient([{"id": "k", "name": KEYED, "spec_solution_id": None, "spec_metric_id": None, "period": "2026_W45"}])
    client.update_goal = lambda goal_id, fields: type("W", (), {"status": None, "data": {"id": goal_id, "title": KEYED, "specSolutionId": None, "specMetricId": None}})()
    result = run_tool(client)
    assert "not linked to a solution" in result.text and "NOT confirmed" in result.text


def test_the_receipt_entry_of_the_code_step_carries_the_goal_as_the_tool_read_it_back():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    client = CachedListingClient([{"id": "k", "name": KEYED, "spec_solution_id": None, "spec_metric_id": None, "period": "2026_W45", "assigned_to_user_id": "u1"}])
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_goals import create_or_update_quest_goal_spec
    tools = ToolRegistry([read_spec(), ToolSpec(name="list_quest_goals", description="d", mutates=False,
                                                handler=lambda a, c: ToolResult(ok=True, data=[{"id": "k", "title": KEYED, "spec_solution_id": None, "completed": False, "abandoned": False}], text="l")),
                          create_or_update_quest_goal_spec({}, client_factory=lambda: client)])
    provider = StubProvider([{"action": "answer", "user_intent": "act", "rationale": "r", "pending_read": []}])
    config = OrchestratorConfig()
    config.overseer = False
    result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=tools).run(CUE, quest_id="q1")
    entries = [g for g in (result.gathered or []) if isinstance(g, dict) and g.get("after_state")]
    assert entries and entries[0]["after_state"]["spec_solution_id"] == "esr_c" and entries[0]["after_state"]["spec_metric_id"] == "esr_mc"
    assert entries[0]["after_state"]["assignee_name"] == "Cy Owner"


NEUTRAL_CUES = ("what is the weather like this week", "remind me to call the dentist on friday", "help me prepare for tomorrow's call with the new advisor",
                "write a short post about gratitude for our newsletter", "how are we doing on the quest overall?", "set up a recurring weekly team meeting on thursdays",
                "summarize the notes from yesterday")


def test_the_judgment_says_none_for_a_message_about_something_else_and_the_resolver_returns_nothing():
    asked = []

    def decide(prompt):
        asked.append(prompt)
        return "NONE"
    assert cue.resolve(registry(), "q1", CUE, decide=decide) is None
    assert len(asked) == 1 and "NONE" in asked[0] and "a recurring meeting" in asked[0] and "esr_c" in asked[0]


def test_the_judgment_names_the_milestone_only_from_the_candidates_offered():
    assert cue.resolve(registry(), "q1", CUE, decide=lambda prompt: "I think it is esr_c").rid == "esr_c"
    assert cue.resolve(registry(), "q1", CUE, decide=lambda prompt: "esr_not_offered") is None
    assert cue.resolve(registry(), "q1", CUE, decide=lambda prompt: "") is None


def test_an_answer_naming_no_option_is_asked_once_more_and_an_id_without_its_prefix_or_the_name_label_counts():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.milestone_cue import JUDGE_ASKS, judged_option
    ids = ["esr_aaaa1111bbbb", "esr_cccc2222dddd"]
    names = {"esr_aaaa1111bbbb": "P4 Onboarding test", "esr_cccc2222dddd": "P5 Foundational product videos"}
    assert judged_option("cccc2222dddd", ids) == (True, "esr_cccc2222dddd")
    assert judged_option("P5", ids, names) == (True, "esr_cccc2222dddd") and judged_option("p5.", ids, names) == (True, "esr_cccc2222dddd")
    assert judged_option("NONE", ids, names) == (True, None)
    assert judged_option("fenix", ids, names) == (False, None) and judged_option("P6", ids, names) == (False, None)
    asked = []
    assert cue.resolve(registry(), "q1", CUE, decide=lambda prompt: asked.append(prompt) or "fenix") is None and len(asked) == JUDGE_ASKS


def test_the_judgment_sees_only_a_few_model_free_candidates_and_no_call_is_made_when_none_clears_the_gate():
    asked = []
    assert cue.resolve(registry(), "q1", "write me a poem about tea", decide=lambda prompt: asked.append(prompt) or "esr_c") is None and asked == []
    prompt_ids = []
    cue.resolve(registry(), "q1", "get the mobile app live", decide=lambda prompt: prompt_ids.append(prompt) or "NONE")
    assert prompt_ids and prompt_ids[0].count("\nesr_") <= cue.CANDIDATES


def test_a_failed_judgment_is_a_lane_gap_not_a_guess():
    def down(prompt):
        raise RuntimeError("model unavailable")
    with pytest.raises(cue.LaneGap, match="deciding which milestone"):
        cue.resolve(registry(), "q1", CUE, decide=down)


def test_neutral_messages_of_other_kinds_never_resolve_a_milestone_when_the_judge_says_none():
    for message in NEUTRAL_CUES:
        assert cue.resolve(registry(), "q1", message, decide=lambda prompt: "NONE") is None, message


def test_a_none_judgment_leaves_the_turn_without_cue_facts_or_a_write():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    import pytest as pt
    calls = []
    provider = StubProvider([{"action": "answer", "user_intent": "act", "rationale": "r", "pending_read": []}])
    config = OrchestratorConfig(interim_replies=True)
    config.overseer = False
    with pt.MonkeyPatch.context() as patch:
        patch.setattr(Orchestrator, "cheap_decision", lambda self, prompt: "NONE")
        result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=registry(calls)).run(CUE, quest_id="q1")
    assert calls == [] and "CUE FACTS (found by code" not in provider.plan_prompts[0] and result is not None


def test_neutral_messages_of_other_kinds_do_not_even_reach_the_judge_on_these_records():
    for message in NEUTRAL_CUES:
        assert cue.candidates(TABLES, message) == [], message


# ---- alternatives written for a quest are kept by it, keyed; the reply says which already were there

ALT_GOALS = [{"id": "g1", "title": "Launch push post A", "text": "Kept by Quest AI: Post one", "spec_solution_id": "esr_c", "completed": False, "abandoned": False},
             {"id": "g2", "title": "Unrelated thing", "spec_solution_id": None, "completed": False, "abandoned": False}]


def same_post_of(prompt, ids):
    """The model-judged answer of Resolve a Reference for one version: the goal id whose scripted text the prompt's version holds, else NONE."""
    import json
    version = prompt.split("The phrase:\n", 1)[1].split("\n\nCandidates", 1)[0] if "The phrase:\n" in prompt else prompt
    return json.dumps({"id": next((goal for text, goal in ids.items() if text in version), "NONE")})


def alt_answer(*alts):
    import json
    return json.dumps({"alternatives": [dict(a) for a in alts]})


A_EXISTING = {"title": "Launch push post A", "goal_id": "g1", "text": "Post one in full", "hypothesis": "A story hook gets more clicks", "solution_id": "esr_c", "metric_id": "esr_mc"}
B_NEW = {"title": "Launch push post B", "goal_id": "", "text": "Post two in full", "hypothesis": "A question hook gets more signups", "solution_id": "esr_c", "metric_id": "esr_mc"}


def alt_registry(calls, goals=ALT_GOALS):
    def write(args, ctx):
        calls.append(dict(args))
        state = "unchanged" if args.get("title") == "Launch push post A" else "created"
        return ToolResult(ok=True, status=ToolStatus.NOOP if state == "unchanged" else ToolStatus.APPLIED, data={"state": state}, text=state)
    return ToolRegistry([read_spec(), list_goals(goals), ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True, handler=write)])


def test_alternatives_in_a_reply_are_kept_by_the_quest_one_goal_each_and_the_tail_says_which_were_already_tracked():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    calls, asked = [], []
    tail = alternatives_flow.run(alt_registry(calls), "q1", "write a couple of posts for the push", "the two posts...",
                                 lambda prompt: asked.append(prompt) or alt_answer(A_EXISTING, B_NEW))
    assert len(asked) == 1 and "Launch push post A" in asked[0] and "esr_c | P3 Mobile app live on the Apple Store" in asked[0]
    assert calls[0]["title"] == "Launch push post A" and "adopt_goal_id" not in calls[0] and calls[0]["spec_solution_id"] == "esr_c" and calls[0]["spec_metric_id"] == "esr_mc"
    assert "adopt_goal_id" not in calls[1] and calls[1]["title"] == "Launch push post B" and "Hypothesis: A question hook gets more signups" in calls[1]["description"]
    assert "Compared by: P3 App Store listing live" in calls[1]["description"]
    assert tail == ("On the quest:\n- Launch push post A (already tracked): A story hook gets more clicks\n- Launch push post B (created): A question hook gets more signups\n"
                    "Compared by: P3 App Store listing live.")
    assert "g1" not in tail and "esr_" not in tail


def test_ids_the_model_was_not_offered_are_dropped_and_fewer_than_two_alternatives_record_nothing():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    calls = []
    forged = dict(B_NEW, goal_id="g_not_offered", solution_id="esr_not_offered", metric_id="esr_made_up")
    tail = alternatives_flow.run(alt_registry(calls), "q1", "cue", "reply", lambda prompt: alt_answer(A_EXISTING, forged))
    assert "adopt_goal_id" not in calls[1] and calls[1].get("spec_solution_id") != "esr_not_offered" and calls[1].get("spec_metric_id") != "esr_made_up" and tail
    calls.clear()
    assert alternatives_flow.run(alt_registry(calls), "q1", "cue", "reply", lambda prompt: alt_answer(A_EXISTING)) == "" and calls == []
    assert alternatives_flow.run(alt_registry(calls), "q1", "cue", "reply", lambda prompt: "not json at all") == "" and calls == []
    assert alternatives_flow.run(alt_registry(calls), "q1", "cue", "reply", lambda prompt: alt_answer()) == ""


def test_a_refused_write_still_leaves_the_versions_their_hypotheses_and_the_metric_in_the_reply_and_says_which_were_not_recorded():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import track_alternatives
    refusing = ToolRegistry([read_spec(), list_goals(ALT_GOALS), ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True,
                                                                         handler=lambda a, c: ToolResult(ok=False, text="Nothing was written: not allowed"))])
    orch = SimpleNamespace(tools=refusing, cheap_decision=lambda prompt: alt_answer(A_EXISTING, B_NEW))
    rs = SimpleNamespace(draft_in_chat=True, turn_user_intent="act", quest_id="q1", cue_resolved=None, gathered=[], user_message="write posts for the launch push")
    res = SimpleNamespace(kind="answer", text="the posts", deep_results=[])
    assert track_alternatives(orch, rs, res) is True
    assert res.text.startswith("the posts") and "(not recorded on the quest: Nothing was written: not allowed)" in res.text
    assert "A story hook gets more clicks" in res.text and "A question hook gets more signups" in res.text and "Compared by: P3 App Store listing live." in res.text


def test_the_refusals_own_words_reach_the_reply_without_ids():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import track_alternatives
    refusing = ToolRegistry([read_spec(), list_goals(ALT_GOALS), ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True,
                                                                         handler=lambda a, c: ToolResult(ok=False, text="Nothing was written: Quest refused it (esr_fbcc56e182f7 is not a strategy of this quest)"))])
    orch = SimpleNamespace(tools=refusing, cheap_decision=lambda prompt: alt_answer(A_EXISTING, B_NEW))
    rs = SimpleNamespace(draft_in_chat=True, turn_user_intent="act", quest_id="q1", cue_resolved=None, gathered=[], user_message="write posts for the push")
    res = SimpleNamespace(kind="answer", text="the posts", deep_results=[])
    track_alternatives(orch, rs, res)
    assert "is not a strategy of this quest" in res.text and "esr_" not in res.text


def test_the_tracking_step_runs_only_for_a_drafted_deliverable_ordered_by_the_person():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import track_alternatives
    calls = []
    orch = SimpleNamespace(tools=alt_registry(calls), cheap_decision=lambda prompt: alt_answer(A_EXISTING, B_NEW))
    base = dict(draft_in_chat=True, turn_user_intent="act", quest_id="q1", cue_resolved=None, gathered=[], user_message="m")
    for change in ({"draft_in_chat": False}, {"turn_user_intent": "ask"}, {"quest_id": None}, {"cue_resolved": object()},
                   {"gathered": [{"locator": "tool:create_or_update_quest_goal"}]}):
        res = SimpleNamespace(kind="answer", text="posts", deep_results=[])
        assert track_alternatives(orch, SimpleNamespace(**{**base, **change}), res) is False and res.text == "posts"
    assert calls == []
    res = SimpleNamespace(kind="answer", text="posts", deep_results=[])
    assert track_alternatives(orch, SimpleNamespace(**base), res) is True and "On the quest:" in res.text and len(calls) == 2


def test_message_one_for_a_tracked_deliverable_says_what_the_quest_already_holds_and_never_the_empty_status():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    from quest_ai_runner.the_intelligence.mind.response_writer import act_status
    line = alternatives_flow.status_line(alt_registry([], [dict(ALT_GOALS[0]), dict(ALT_GOALS[0], id="g3", title="Launch push post C")]), "q1", "write two posts for the launch push")
    assert line == ("The quest already tracks 2 goals like this (Launch push post A, Launch push post C), under P3 Mobile app live on the Apple Store, "
                    "which moves P3 App Store listing live.")
    unlinked = [dict(ALT_GOALS[0], spec_solution_id=None)]
    assert alternatives_flow.status_line(alt_registry([], unlinked), "q1", "write two posts for the launch push") == "The quest already tracks 1 goal like this (Launch push post A)."
    assert alternatives_flow.status_line(alt_registry([], [ALT_GOALS[1]]), "q1", "write two posts for the launch push") == ""
    texts = act_status.status_texts(None, [], "", evidence="", found=line)
    assert texts["status"] == line and "have not found" not in act_status.status_texts(None, [], "", evidence="")["text"]


def test_the_reply_voice_keeps_prices_and_optional_questions_out_of_drafted_content():
    from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import REPLY_VOICE_SYSTEM
    assert "Never state a price, a plan name, a tier" in REPLY_VOICE_SYSTEM and "no question about an optional choice" in REPLY_VOICE_SYSTEM and "no list of what you could not confirm" in REPLY_VOICE_SYSTEM


def test_a_drafted_deliverable_is_shown_once_in_the_final_message_with_no_milestone_copy_and_no_deep_run():
    from hubofhubs.api import EVENT_RESULT
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from .test_done_evidence import Provider, Runner
    from .test_incremental_replies import build, NOT_MET
    plan = {"action": "answer", "rationale": "write them", "user_intent": "act", "deep_target": "draft_in_chat", "work_shape": "lookup", "done_evidence": ["x"]}
    runner = Runner([])
    provider = Provider([plan], verdicts=[dict(NOT_MET), dict(NOT_MET), {"met": True}])
    events = []
    build(provider, runner).run("write two posts", sink=StreamSink(events.append))
    assert runner.briefs == [] and not [e for e in events if e["type"] == "milestone" and (e.get("data") or {}).get("goal_not_met")]
    assert [e for e in events if e["type"] == EVENT_RESULT]


def test_message_one_for_a_drafted_deliverable_is_the_code_status_and_not_the_planners_plan():
    from hubofhubs.api import EVENT_INTERIM
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    goals = [dict(ALT_GOALS[0])]
    plan_read = {"action": "tool", "rationale": "I will read the priority ladder and the quest sync file", "user_intent": "act", "deep_target": "draft_in_chat",
                 "tool_calls": [{"name": "list_quest_goals", "args": {"quest_id": "q1"}}]}
    provider = StubProvider([plan_read, {"action": "answer", "rationale": "r", "user_intent": "act", "deep_target": "draft_in_chat"}])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=alt_registry([], goals),
                        config=OrchestratorConfig(interim_replies=True, overseer=False, min_seconds_between_interims=0.0))
    events = []
    orch.run("write a couple of posts for the launch push", sink=StreamSink(events.append), quest_id="q1")
    first = next(e for e in events if e["type"] == EVENT_INTERIM)["text"]
    assert "The quest already tracks 1 goal like this (Launch push post A)" in first and "Now: Writing the draft now." in first
    assert "priority ladder" not in first and "starting on this now" not in first


# ---- safety: a person's goal is never taken over, renamed, re-described or re-dated

HUMAN = {"id": "h1", "name": "Submit the app to the store and advance the written feature test pass", "spec_solution_id": "esr_c", "spec_metric_id": None,
         "period": "2026_Q4", "description": "Kim's own plan, written by a person", "assigned_to_user_id": "u9", "completed": False}


def test_a_goal_with_any_other_title_is_never_adopted_by_a_model_pick_or_by_the_link_and_nothing_is_written_to_it():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    fields = {"spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "description": "Next step ..."}
    for kwargs in ({"adopt_goal_id": "h1"}, {"link_match": True}, {"adopt_goal_id": "h1", "link_match": True}):
        client = GoalClient([dict(HUMAN)])
        done = goal_links.ensure_goal(client, "q1", KEYED, "2026_W45", fields, None, kwargs.get("adopt_goal_id"), kwargs.get("link_match", False))
        assert done["state"] == "created" and len(client.posts) == 1 and client.puts == [] and client.assigns == []
        assert client.goals[0]["name"] == HUMAN["name"] and client.goals[0]["description"] == HUMAN["description"] and client.goals[0]["period"] == "2026_Q4"


def test_only_a_keyed_next_step_goal_can_be_taken_over_and_the_pattern_is_the_one_the_flow_writes():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    for title in ("P3 next step: week of 2026-11-02", "P3 Mobile app live on the Apple Store next step: week of 2026-11-09", "X next step: week of 2026-11-02 "):
        assert goal_links.is_keyed_next_step(title), title
    for title in ("Submit the app to the store", "Next step review", "P3 next step: this week", "next step: week of soon", "", None):
        assert not goal_links.is_keyed_next_step(title), title
    client = GoalClient([{"id": "k", "name": "P3 next step: week of 2026-11-02", "spec_solution_id": None, "period": "2026_W45"}, dict(HUMAN)])
    done = goal_links.ensure_goal(client, "q1", KEYED, "2026_W45", {"spec_solution_id": "esr_c"}, None, "k", True)
    assert done["state"] == "updated" and [p[0] for p in client.puts] == ["k"] and client.posts == []


def test_the_milestone_step_offers_only_keyed_goals_for_adoption_and_names_the_persons_goal_in_its_description():
    keyed = {"id": "k", "title": "P3 next step: week of 2026-11-02", "text": "", "spec_solution_id": None, "completed": False, "abandoned": False}
    human = {"id": "h1", "title": "Submit the app to the store and advance the written feature test pass", "text": "", "spec_solution_id": "esr_c", "completed": False, "abandoned": False}
    tools = ToolRegistry([read_spec(), list_goals([keyed, human]), write_goal([])])
    resolved = cue.resolve(tools, "q1", CUE, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
    asked = []

    def decide(prompt):
        asked.append(prompt)
        return json.dumps({"goal_id": "k"})
    assert resolved.choices(decide)["goal_id"] == "k" and "h1:" not in asked[0] and "k:" in asked[0]
    assert resolved.choices(lambda prompt: json.dumps({"goal_id": "h1"}))["goal_id"] == ""
    description = resolved.goal_arguments()["description"]
    assert "Related goals on the quest: Submit the app to the store" in description and "k" not in description.split("Related goals")[1]


# ---- the group fixes of the milestone baseline

def test_one_cheap_call_chooses_the_owner_the_existing_goal_and_the_draft_and_a_repeat_reuses_the_kept_draft():
    calls, asked = [], []
    tables = {**TABLES, "solutions": {**TABLES["solutions"], "esr_c": {**TABLES["solutions"]["esr_c"], "owner_user_ids": [], "purpose": "Owners: Cy and Di."}}}
    read = ToolSpec(name="read_spec_records", description="d", mutates=False, handler=lambda args, ctx: ToolResult(ok=True, data={"records": tables, "time_zone": "UTC"}, text="read"))
    tools = ToolRegistry([read, list_goals([]), write_goal(calls)])
    now = datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc)
    resolved = cue.resolve(tools, "q1", CUE, now)
    receipt, _ = resolved.carry_out(tools, lambda prompt: asked.append(prompt) or json.dumps({"owner": "Cy", "draft": "Opening, three product points, call to action on the signup page"}))
    assert len(asked) == 1 and "human_step" in asked[0] and "never a plan to write it" in asked[0] and "distinct alternatives" in asked[0] and "few neutral questions, each a full sentence ready to send" in asked[0] and "no em dashes" in asked[0]
    assert calls[0]["assignee"] == "Cy" and calls[0]["description"].endswith("\nDraft: Opening, three product points, call to action on the signup page")
    assert "The draft kept in the goal" in receipt and "Opening, three product points" in receipt
    kept = {"id": "k", "title": "P3 Mobile app live on the Apple Store next step: week of 2026-11-02", "text": "Next step ...\nDraft: Opening, three product points, call to action on the signup page",
            "spec_solution_id": "esr_c", "completed": False, "abandoned": False}
    again = cue.resolve(ToolRegistry([read, list_goals([kept]), write_goal(calls)]), "q1", CUE, now)
    asked.clear()
    again.carry_out(ToolRegistry([read, list_goals([kept]), write_goal(calls)]), lambda prompt: asked.append(prompt) or json.dumps({"owner": "Cy"}))
    assert "draft" not in asked[0].split("Return JSON")[1] and calls[-1]["description"].endswith("Draft: Opening, three product points, call to action on the signup page")


def test_a_cue_that_names_a_word_family_of_the_milestone_resolves_it():
    tables = {"solutions": {"esr_a": {"name": "P7 Quest AI reliable yet cheap", "milestones": "Offline pass rate holds.", "owner_user_ids": ["Ann"]},
                            "esr_b": {"name": "P8 100 Quest users from YouTube", "milestones": "100 new users.", "owner_user_ids": ["Bo"]}},
              "metrics": {"esr_ma": {"name": "P7 pass rate", "record": "80 percent", "solution_ids": ["esr_a"]}, "esr_mb": {"name": "P8 users", "record": "0", "solution_ids": ["esr_b"]}}}
    assert cue.candidates(tables, "lets make progress on quest ai reliability")[0] == "esr_a"
    assert cue.candidates(tables, "get the niche youtube users going")[0] == "esr_b"
    assert cue.candidates(tables, "announcements of integrations") == []


def test_the_code_step_runs_after_a_write_that_was_refused_but_not_after_one_that_was_applied():
    from hubofhubs.api import PlanDecision
    resolved = cue.resolve(registry(), "q1", CUE)
    tools = ToolRegistry([ToolSpec(name="apply_spec_edit", description="d", mutates=True, handler=lambda a, c: ToolResult(ok=True))])
    blocked = {"locator": "tool:apply_spec_edit", "tool_status": "refused"}
    applied = {"locator": "tool:apply_spec_edit", "tool_status": "applied"}
    plan = PlanDecision(action="clarify")
    assert resolved.routes_to_code(plan, "act", [blocked], tools) is True and resolved.routes_to_code(plan, "act", [applied], tools) is False


def link_client(refuse_links):
    class Client(GoalClient):
        def create_goal(self, title, **kw):
            if refuse_links and (kw.get("spec_solution_id") or kw.get("spec_metric_id")):
                raise RuntimeError("Creating the goal was refused by Quest (400): esr_x is not a strategy of this quest.")
            return super().create_goal(title, **kw)
    return Client([])


def test_a_refused_strategy_link_is_retried_once_without_the_links_and_the_milestone_is_named_in_the_description():
    client = link_client(True)
    result = run_tool(client, description="Next step ...", spec_metric_id="esr_mc")
    assert result.ok and len(client.posts) == 1 and "spec_solution_id" not in client.posts[0][1] and "spec_metric_id" not in client.posts[0][1]
    assert "Milestone record: esr_c" in client.posts[0][1]["description"]
    assert "Quest did not accept the link to the milestone" in result.text and "is not a strategy of this quest" in result.text and "linked to solution esr_c" not in result.text
    assert result.data["links_refused"] and result.data["state"] == "created"


def test_other_refusals_are_not_retried_and_an_accepted_link_is_untouched():
    class Refusing(GoalClient):
        def create_goal(self, title, **kw):
            raise RuntimeError("Creating the goal was refused by Quest (403): you may not edit this quest")
    result = run_tool(Refusing([]))
    assert not result.ok and "may not edit" in result.text
    accepted = link_client(False)
    done = run_tool(accepted)
    assert done.ok and accepted.posts[0][1]["spec_solution_id"] == "esr_c" and not done.data.get("links_refused")


def test_the_first_message_for_a_resolved_milestone_is_read_from_its_record_fields_not_parsed_out_of_a_reads_text():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import send_status_first
    resolved = cue.resolve(registry(), "q1", CUE, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
    sent = []
    emit = SimpleNamespace(emit=lambda event: sent.append(event), status=lambda text: None)
    garbled = {"kind": "query", "locator": "tool:read_spec_records", "text": "Milestone 'P3', solution esr_c, metric esr_mc, Current value: 12 percent of OUTSIDE sign-ups in the last 3. Owners named: Joshua (rep_f7eb490263d2)."}
    orch = SimpleNamespace(cfg=SimpleNamespace(interim_replies=True), tools=registry())
    rs = SimpleNamespace(plan=SimpleNamespace(action="answer", goal="", rationale="plan dump", facts_read=["Owners named: Joshua (rep_f7eb490263d2)"], tool_calls=[]),
                         turn_user_intent="act", user_message=CUE, _ctx_meta={}, gathered=[garbled], context_view="", status_first=None, cue_resolved=resolved, quest_id="q1",
                         draft_in_chat=False)
    assert send_status_first(orch, emit, rs, [garbled], user_turn=True, forced=True)
    text = sent[0].text
    assert "Milestone: P3 Mobile app live on the Apple Store" in text and "Current value: No." in text and "esr_" not in text and "rep_" not in text and "OUTSIDE" not in text
    assert "Now: Recording this week's next step" in text


def test_the_reply_is_told_to_ask_nothing_and_never_to_move_the_milestones_own_date():
    calls = []
    resolved = cue.resolve(registry(calls), "q1", CUE)
    receipt, _ = resolved.carry_out(registry(calls))
    assert "ask the person nothing, propose no new date" in receipt and "state no count of its items" in receipt
    assert "never means rescheduling the milestone's own date" in resolved.facts


# ---- run 3 of the milestone baseline

def test_a_draft_loses_every_line_that_states_a_figure_the_records_do_not_hold_and_keeps_numbering_and_brackets():
    allowed = "P12 Announce integrations. Done when: 0 integrations announced. Target 2026-12-31."
    draft = "Short description (66 characters)\n1. Tested and ready: new integrations\n2. Click-through of 4.2 percent expected\n3. Built for your workflow [link]\nTarget 2026-12-31 stays."
    kept = cue.supported_draft(draft, allowed)
    assert kept == "1. Tested and ready: new integrations\n3. Built for your workflow [link]\nTarget 2026-12-31 stays." and "66" not in kept and "4.2" not in kept
    assert cue.supported_draft("Only 77 percent", allowed) == ""


def test_the_kept_draft_never_carries_an_unsupported_figure_even_when_the_model_writes_one():
    calls = []
    resolved = cue.resolve(registry(calls), "q1", CUE)
    resolved.carry_out(registry(calls), lambda prompt: json.dumps({"draft": "Headline A\nHeadline B (about 9 words)\nHeadline C"}))
    assert calls[0]["description"].endswith("Draft: Headline A\nHeadline C") and "9 words" not in calls[0]["description"]


def test_a_deliverable_goal_the_planner_wrote_under_its_own_title_does_not_stand_in_for_the_milestones_next_step():
    from hubofhubs.api import PlanDecision
    resolved = cue.resolve(registry(), "q1", CUE)
    tools = ToolRegistry([ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True, handler=lambda a, c: ToolResult(ok=True))])
    deliverable = {"locator": "tool:create_or_update_quest_goal", "tool_status": "applied", "call": {"name": "create_or_update_quest_goal", "args": {"title": "P3 Mobile app listing copy"}}}
    keyed = {"locator": "tool:create_or_update_quest_goal", "tool_status": "applied", "call": {"name": "create_or_update_quest_goal", "args": {"title": "P3 next step: week of 2026-11-02"}}}
    plan = PlanDecision(action="answer")
    assert resolved.routes_to_code(plan, "act", [deliverable], tools) is True and resolved.routes_to_code(plan, "act", [keyed], tools) is False


def test_each_owner_the_record_names_is_tried_in_turn_and_a_team_name_is_not_asked_for():
    calls, asked = [], []
    tables = {**TABLES, "solutions": {**TABLES["solutions"], "esr_c": {**TABLES["solutions"]["esr_c"], "owner_user_ids": [], "purpose": "Owners: Cy, Di."}}}
    read = ToolSpec(name="read_spec_records", description="d", mutates=False, handler=lambda args, ctx: ToolResult(ok=True, data={"records": tables, "time_zone": "UTC"}, text="read"))
    tools = ToolRegistry([read, list_goals([]), write_goal(calls, refuse=("Cy",))])
    resolved = cue.resolve(tools, "q1", CUE, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
    resolved.carry_out(tools, lambda prompt: asked.append(prompt) or json.dumps({"owners": ["Cy", "Di"]}))
    assert [c.get("assignee") for c in calls] == ["Cy", "Di"] and "names only, no team names" in asked[0]


def test_the_closing_line_reports_unchanged_from_the_read_when_nothing_wrote_the_metric():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer import act_status
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import close_with_metric, metric_written
    before = act_status.MetricReading("esr_c", "P3 Mobile app live on the Apple Store", "App live", "0 of 6 on the working list (niche videos 1 to 6), read 2026-")
    rs = SimpleNamespace(status_first={"before": before, "voice": False}, gathered=[], turn_metrics=None, quest_id="q1")
    res = SimpleNamespace(kind="answer", text="I recorded the step.", deep_results=[])
    close_with_metric(SimpleNamespace(tools=None), rs, res)
    assert res.text.endswith("Metric (P3 Mobile app live on the Apple Store): unchanged: 0 of 6.") and "niche" not in res.text
    written = SimpleNamespace(status_first={"before": before, "voice": False}, gathered=[{"locator": "tool:apply_spec_edit", "tool_status": "applied"}], turn_metrics=None, quest_id="q1")
    assert metric_written(SimpleNamespace(tools=None), written) is True
    changed = SimpleNamespace(kind="answer", text="Recorded.", deep_results=[])
    close_with_metric(SimpleNamespace(tools=None), written, changed)
    assert "Metric" not in changed.text            # a metric that was written and not read again is not claimed unchanged


def test_the_alternatives_are_cut_from_the_reply_by_where_each_begins_so_the_models_answer_stays_short():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    reply = ("**Post 1** Spiritual growth does not have to be vague. What if you could set an intention and track it?\n\n**Post 2** Most people quit because nobody walks it with them. "
             "Quest gives you small daily steps.")
    answer = json.dumps({"alternatives": [{"title": "Push post A", "starts_with": "Spiritual growth does not have", "hypothesis": "intention hook"},
                                          {"title": "Push post B", "starts_with": "Most people quit because", "hypothesis": "company hook"}]})
    alts = alternatives_flow.parse(answer, set(), [], reply)
    assert [a.title for a in alts] == ["Push post A", "Push post B"]
    assert alts[0].text.startswith("Spiritual growth") and "Most people" not in alts[0].text and alts[1].text.startswith("Most people") and alts[1].text.endswith("small daily steps.")
    assert alternatives_flow.parse('{"alternatives": [{"title": "A", "starts_with": "nowhere in the reply"}, {"title": "B", "starts_with": "also nowhere"}]}', set(), [], reply) == []
    assert alternatives_flow.parse('{"alternatives": [{"title": "A", "starts_wi', set(), [], reply) == []


# ---- run 4 of the milestone baseline

def test_an_alternative_is_found_in_the_reply_through_curly_quotes_markdown_and_spacing():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    reply = "**Post 1**\n\nSpiritual growth doesn’t have to mean taking things on faith.  What if you could\nexplore it?\n\n**Post 2**\n\nAI can’t do your inner work for you."
    cut = alternatives_flow.segment(reply, ["Spiritual growth doesn't have to mean", "AI can't do your inner work"])
    assert cut[0].startswith("Spiritual growth") and "AI can" not in cut[0] and cut[1].startswith("AI can")
    short = alternatives_flow.segment(reply, ["Spiritual growth doesn't have to mean something else entirely"])
    assert short[0].startswith("Spiritual growth")          # the first words still find it when the model's tail differs
    assert alternatives_flow.segment(reply, ["nothing like this"]) == [""]


def test_message_one_shows_a_clean_current_value_never_a_fragment_or_a_link():
    from quest_ai_runner.the_intelligence.mind.response_writer import act_status
    assert act_status.status_value("public listing https://play.google.com/store/apps/details?id=com.example.app returned HTTP 404. More text.") == "public listing a link returned HTTP 404"
    assert act_status.status_value("50.0 percent (5 of 10) of OUTSIDE new sign-ups in the last 30 days") == "50.0 percent"
    assert act_status.status_value("0 integrations announced, 0 recorded as passed testing (Quest checked it)") == "0 integrations announced, 0 recorded as passed testing"
    assert act_status.status_value("") == act_status.NOT_RECORDED


def test_message_one_says_the_step_already_exists_when_this_weeks_goal_is_on_the_quest():
    keyed = {"id": "k", "title": "P3 Mobile app live on the Apple Store next step: week of 2026-11-02", "text": "", "spec_solution_id": "esr_c", "completed": False, "abandoned": False}
    tools = ToolRegistry([read_spec(), list_goals([keyed]), write_goal([])])
    now = datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc)
    assert "which is already on the quest" in cue.resolve(tools, "q1", CUE, now).doing()
    assert cue.resolve(registry(), "q1", CUE, now).doing().startswith("Recording this week's next step")


def test_when_no_named_owner_can_be_set_the_goal_is_left_unassigned_and_the_reply_says_who_should_own_it():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    existing = {"id": "k", "name": KEYED, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "period": "2026_W45", "assigned_to_user_id": "team-account"}
    client = GoalClient([dict(existing)])
    client.assign_goal = lambda goal_id, user_id: client.assigns.append((goal_id, user_id)) or type("W", (), {"status": None})()
    done = goal_links.ensure_goal(client, "q1", KEYED, "2026_W45", {"spec_solution_id": "esr_c", "spec_metric_id": "esr_mc"}, None, None, False, True)
    assert client.assigns == [("k", None)] and "assignee cleared" in done["changed"]
    untouched = GoalClient([dict(existing)])
    untouched.assign_goal = lambda goal_id, user_id: untouched.assigns.append((goal_id, user_id)) or type("W", (), {"status": None})()
    goal_links.ensure_goal(untouched, "q1", KEYED, "2026_W45", {"spec_solution_id": "esr_c", "spec_metric_id": "esr_mc"}, None, None, False, False)
    assert untouched.assigns == []           # without the flag a goal keeps whoever it has
    calls = []
    tools = ToolRegistry([read_spec(), list_goals([]), write_goal(calls, refuse=("Cy", "Di"))])
    resolved = cue.resolve(tools, "q1", CUE, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
    receipt, _ = resolved.carry_out(tools)
    assert calls[-1].get("clear_assignee") is True and "assignee" not in calls[-1]
    assert "left unassigned. Say plainly that the record names Cy, Di but the quest cannot assign the goal to them, say who should own it, and ask nothing" in receipt


def test_the_closing_line_reads_the_milestones_metric_record_again_after_the_write():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer import act_status
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import close_with_metric
    now_tables = {**TABLES, "metrics": {**TABLES["metrics"], "esr_mc": {**TABLES["metrics"]["esr_mc"], "record": "Yes. Listing live since this morning."}}}
    read = ToolSpec(name="read_spec_records", description="d", mutates=False, handler=lambda a, c: ToolResult(ok=True, data={"records": now_tables, "time_zone": "UTC"}, text="read"))
    tools = ToolRegistry([read_spec(), list_goals([]), write_goal([])])
    resolved = cue.resolve(tools, "q1", CUE, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
    before = act_status.MetricReading("esr_c", resolved.name, resolved.done_when, resolved.value)
    rs = SimpleNamespace(status_first={"before": before, "voice": False}, gathered=[], turn_metrics=None, quest_id="q1", cue_resolved=resolved)
    res = SimpleNamespace(kind="answer", text="Recorded.", deep_results=[])
    close_with_metric(SimpleNamespace(tools=ToolRegistry([read])), rs, res)
    assert res.text.endswith("Metric (P3 Mobile app live on the Apple Store): was No, now Yes.")


def test_the_draft_prompt_names_sendable_questions_and_a_short_pitch_and_limits_a_checklist_to_when_nothing_else_fits():
    asked = []
    cue.resolve(registry([]), "q1", CUE).carry_out(registry([]), lambda prompt: asked.append(prompt) or "{}")
    text = asked[0]
    assert "few neutral questions, each a full sentence ready to send" in text and "a short message to that party stating what they get, the ask" in text
    assert "a checklist of at most 5 steps only when none of these fits" in text and "under 120 words" in text and "Leave no bracket slot" in text


def test_the_planners_own_goal_writes_are_taken_out_for_a_milestone_cue_and_the_deliverable_stays_in_the_one_keyed_goal():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    calls = []
    extra = {"name": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title": "P3 Mobile app live on the Apple Store outreach message", "description": "x"}}
    provider = StubProvider([{"action": "tool", "user_intent": "act", "rationale": "r", "tool_calls": [extra]}])
    config = OrchestratorConfig()
    config.overseer = False
    Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=registry(calls)).run(CUE, quest_id="q1")
    assert [c["title"] for c in calls] == ["P3 Mobile app live on the Apple Store next step: week of " + calls[0]["title"].rsplit(" ", 1)[1]] and len(calls) == 1
    assert "outreach" not in calls[0]["title"]


def test_the_draft_prompt_forbids_first_person_unsourced_dates_and_notes_in_place_of_a_script():
    asked = []
    cue.resolve(registry([]), "q1", CUE).carry_out(registry([]), lambda prompt: asked.append(prompt) or "{}")
    assert 'no first person ("I", "we", "our")' in asked[0] and "never a due date or deadline that is not in the facts above" in asked[0] and "the words to be spoken, not notes about it" in asked[0]


# ---- SAFETY run 6: a person's goal that merely shares words with a request is never updated

REAL_GOALS = [{"id": "r1", "name": "Every lead pool has received a subscription offer, and paid subscribers from leads are counted weekly", "description": "All 1,369 distinct leads (Facebook interest-form, webinar)",
               "spec_solution_id": None, "period": "2026_Q4", "completed": False, "abandoned": False, "assigned_to_user_id": "u1"},
              {"id": "r2", "name": "A named project owner for the 1000 subscribers project is confirmed, after trialing candidates", "description": "Joshua's execution lead goal",
               "spec_solution_id": None, "period": "2026_Q4", "completed": False, "abandoned": False, "assigned_to_user_id": "u2"}]


def test_a_goal_that_is_not_one_quest_ai_keeps_is_left_exactly_as_it_is_even_when_the_call_carries_its_own_title():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    for goal in REAL_GOALS:
        client = GoalClient([dict(goal)])
        done = goal_links.ensure_goal(client, "q1", goal["name"], "2026_W45", {"description": "Post copy ...", "spec_solution_id": "esr_c"}, "u9", goal["id"], True, True)
        assert done["state"] == "left_alone" and client.puts == [] and client.posts == [] and client.assigns == []
        assert client.goals[0]["description"] == goal["description"] and client.goals[0]["period"] == "2026_Q4"
    result = run_tool(GoalClient([dict(REAL_GOALS[0])]), title=REAL_GOALS[0]["name"], description="Post copy")
    assert result.ok and "left exactly as it is" in result.text and result.data["state"] == "left_alone"


def test_what_the_tool_creates_is_marked_as_kept_by_quest_ai_and_only_such_a_goal_is_updated_later():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    client = GoalClient([])
    goal_links.ensure_goal(client, "q1", "Launch push post A", "2026_W45", {"description": "Post one"}, None)
    assert client.posts[0][1]["description"] == "Kept by Quest AI: Post one"
    kept = {"id": "m1", "name": "Launch push post A", "description": "Kept by Quest AI: Post one", "period": "2026_W45", "completed": False, "abandoned": False}
    again = GoalClient([dict(kept)])
    done = goal_links.ensure_goal(again, "q1", "Launch push post A", "2026_W45", {"description": "Post one, revised"}, None)
    assert done["state"] == "updated" and again.puts[0][1]["description"] == "Kept by Quest AI: Post one, revised"
    assert goal_links.is_owned(kept) and goal_links.is_owned({"name": "P3 next step: week of 2026-11-02"}) and not goal_links.is_owned(REAL_GOALS[1])
    assert not goal_links.is_owned({"name": "P3 Mobile app live on the Apple Store mentor ad draft", "description": ""})        # sharing the milestone's title is not enough


def test_the_alternatives_flow_never_offers_or_changes_a_goal_that_merely_shares_words_with_the_request():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    rows = [{"id": g["id"], "title": g["name"], "text": g["description"], "spec_solution_id": None, "completed": False, "abandoned": False} for g in REAL_GOALS]
    rows.append({"id": "k1", "title": "Launch push post A", "text": "Kept by Quest AI: Post one", "spec_solution_id": "esr_c", "completed": False, "abandoned": False})
    assert [g["id"] for g in alternatives_flow.goals_kept(rows)] == ["k1"]
    calls, asked = [], []
    forged = [dict(A_EXISTING, goal_id="r1", title=REAL_GOALS[0]["name"]), dict(B_NEW, goal_id="r2", title=REAL_GOALS[1]["name"])]
    tail = alternatives_flow.run(alt_registry(calls, rows), "q1", "write a couple of facebook posts for the 1000 subscribers push", "reply",
                                 lambda prompt: asked.append(prompt) or alt_answer(*forged))
    assert "r1 |" not in asked[0] and "r2 |" not in asked[0] and "k1 | Launch push post A" in asked[0]          # a person's goal is never offered with an id to change
    assert "belong to people and are never changed" in asked[0]
    # the model named the persons goals by title: the tool is asked, and it leaves them alone (this fake tool stands in for it)
    assert all(c.get("adopt_goal_id") is None for c in calls)
    assert tail


def test_a_milestone_cue_never_offers_a_persons_goal_for_adoption():
    person = {"id": "h1", "title": "P3 Mobile app live on the Apple Store mentor ad draft", "text": "a person's plan", "spec_solution_id": "esr_c", "completed": False, "abandoned": False}
    tools = ToolRegistry([read_spec(), list_goals([person]), write_goal([])])
    resolved = cue.resolve(tools, "q1", CUE, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
    asked = []
    picked = resolved.choices(lambda prompt: asked.append(prompt) or json.dumps({"goal_id": "h1"}))
    assert picked["goal_id"] == "" and "h1:" not in asked[0]


# ---- full run on 3a5550bf

def test_a_goal_that_cannot_be_read_blocks_the_write_but_is_a_lane_gap_not_an_unexpected_write_and_does_not_stop_the_run():
    import sys
    from pathlib import Path
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import QuestApiError
    from quest_ai_runner.the_world.tools import dry_run_writes as dry
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation/components/chat_quality/impls"))
    import prod_qar_readonly as arm
    import os
    os.environ[dry.ENV] = "1"
    try:
        dry.RECORDER.clear()
        dry.set_allowances([{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title~": "P1 "}}])
        dry.set_goal_title_lookup(lambda goal_id: None)
        with dry.allow_scope(True):
            try:
                dry.quest_exchange("PUT", "/api/planning/goals/f9166b94", {"title": "x"})
                raised = None
            except QuestApiError as error:
                raised = str(error)
        assert raised and "could not be read" in raised
        assert dry.RECORDER.snapshot()["unexpected"] == []              # not an unexpected write
        arm.RUN_STOPPED["why"] = ""
        assert arm.stop_after_unexpected_write({"case_id": "M-01", "unexpected_writes": []}, dry) == ""        # and the run goes on
    finally:
        os.environ.pop(dry.ENV, None)
        dry.set_allowances([])
        dry.set_goal_title_lookup(None)
        dry.RECORDER.clear()


def test_the_figure_pass_does_not_touch_a_drafted_deliverable_or_a_reply_whose_figures_were_read():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    provider = StubProvider([{"action": "answer", "user_intent": "ask", "rationale": "r", "pending_read": []}])
    config = OrchestratorConfig()
    config.overseer = False
    result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config).run("what is 12 plus 3?")
    assert result.text


def test_a_reference_goal_the_quest_holds_is_shown_by_title_and_reusing_its_title_leaves_it_as_it_is():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    rows = [{"id": "p1", "title": "FB push post A", "text": "a person's own goal", "spec_solution_id": None, "completed": False},
            {"id": "p2", "title": "FB push post B", "text": "a person's own goal", "spec_solution_id": None, "completed": False},
            {"id": "x", "title": "Budget review", "text": "", "spec_solution_id": None, "completed": False}]
    calls, asked = [], []

    class Guarded(ToolRegistry):
        pass

    def write(args, ctx):
        calls.append(dict(args))
        owned = False          # these goals are unmarked: the real tool leaves them exactly as they are
        return ToolResult(ok=True, status=ToolStatus.NOOP, data={"state": "left_alone"}, text="left")
    tools = ToolRegistry([read_spec(), list_goals(rows), ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True, handler=write)])
    same_titles = [dict(A_EXISTING, title="FB push post A", goal_id=""), dict(B_NEW, title="FB push post B", goal_id="")]
    tail = alternatives_flow.run(tools, "q1", "write a couple of facebook posts for the push", "reply", lambda prompt: asked.append(prompt) or (alt_answer(*same_titles) if "Other goals on the quest" in prompt else same_post_of(prompt, {"Post one in full": "p1", "Post two in full": "p2"})))
    assert "FB push post A" in asked[0].split("Other goals on the quest that look like these")[1] and "Budget review" not in asked[0]
    assert "(already on the quest, left exactly as it is)" in tail and "A story hook gets more clicks" in tail and "Compared by:" in tail
    assert calls == [] and "- FB push post A (already on the quest" in tail and "- FB push post B (already on the quest" in tail          # nothing is created beside a person's series


def test_a_tool_that_leaves_an_unmarked_goal_alone_is_reported_as_already_on_the_quest():
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    person = {"id": "p1", "name": "FB push post A", "description": "a person's own goal", "period": "2026_W45", "completed": False, "abandoned": False}
    result = run_tool(GoalClient([dict(person)]), title="FB push post A", description="New copy", match_by_link=False)
    assert result.ok and result.data["state"] == "left_alone" and "left exactly as it is" in result.text


def test_the_reply_voice_forbids_tool_chatter_offers_and_unread_figures_in_every_reply_and_the_draft_status_does_not_claim_a_save():
    from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import REPLY_VOICE_SYSTEM
    for rule in ("Never mention a file, tool, report, search or read you could or could not use", "no offer of further work", "Never recall a number from memory"):
        assert rule in REPLY_VOICE_SYSTEM
    from quest_ai_runner.the_intelligence.mind.response_writer import act_status
    assert "keeping them" not in act_status.status_texts(None, [], "Writing the draft now.", evidence="", found="")["text"]


def test_a_drafting_turn_stops_reading_after_two_rounds_and_has_no_overseer():
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    read = {"action": "tool", "user_intent": "act", "deep_target": "draft_in_chat", "rationale": "r", "tool_calls": [{"name": "list_quest_goals", "args": {"quest_id": "q1"}}]}
    provider = StubProvider([read, read, read, read, {"action": "answer", "user_intent": "act", "deep_target": "draft_in_chat", "rationale": "r"}])
    config = OrchestratorConfig()
    config.overseer = True
    Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=alt_registry([])).run("write a caption", sink=StreamSink(lambda e: None), quest_id="q1")
    assert len([n for n in provider.plan_tool_schemas if n.get("name") == "decide"]) <= 3          # no fourth round of reading


def test_look_alike_goals_that_are_a_persons_mean_nothing_is_created_even_under_other_titles():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    rows = [{"id": "p1", "title": "FB push post A", "text": "a person's own goal", "spec_solution_id": None, "completed": False},
            {"id": "p2", "title": "FB push post B", "text": "a person's own goal", "spec_solution_id": None, "completed": False}]
    calls = []
    new_titles = [dict(A_EXISTING, title="Facebook post A for the 1000 subscribers push", goal_id=""), dict(B_NEW, title="Facebook post B for the 1000 subscribers push", goal_id="")]
    tail = alternatives_flow.run(alt_registry(calls, rows), "q1", "write a couple of facebook posts for the push", "reply",
                                 lambda prompt: alt_answer(*new_titles) if "Other goals on the quest" in prompt else '{"id": "NONE"}')
    assert calls == []
    assert "Already on the quest: FB push post A, FB push post B (left as they are)." in tail and "Post A: A story hook gets more clicks" in tail       # each hypothesis stays with its own post
    assert "Facebook post A for the 1000" not in tail and "Compared by: P3 App Store listing live." in tail


def test_a_question_that_already_read_what_it_concerns_never_goes_to_a_deep_background_run():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.critical_thinking.unmet_goal import pursue_before_giving_up
    rs = SimpleNamespace(turn_user_intent="ask", gathered=[{"kind": "query", "locator": "tool:list_quest_goals", "text": "595 matching goals"}], text="The quest has 595 goals.", _last_verdict={"met": False})
    done = pursue_before_giving_up(SimpleNamespace(cfg=SimpleNamespace()), rs, "g", None, {}, None, None, None, None)
    assert done["result"] is None and done["followup_started"] is False and done["pursuit"]["status"] == "unmet_reported" and done["pursuit"]["message"] == ""


def test_a_question_asked_inside_a_quests_chat_is_not_answered_from_the_question_alone():
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    reads = {"action": "tool", "user_intent": "ask", "rationale": "count them", "tool_calls": [{"name": "list_quest_goals", "args": {"quest_id": "q1"}}]}
    provider = StubProvider([reads, {"action": "answer", "user_intent": "ask", "rationale": "r"}])
    config = OrchestratorConfig()
    config.overseer = False
    events = []
    Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=alt_registry([])).run("how many goals does the quest have", sink=StreamSink(events.append), quest_id="q1")
    assert any("list_quest_goals" in str(e.get("text") or "") for e in events)           # the count was read before it was answered


def test_the_reply_voice_asks_for_short_attributed_answers_with_the_core_fact_first():
    from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import REPLY_VOICE_SYSTEM
    for rule in ("in as few sentences as it needs", "never tell the person what you would need or could do for them", 'Never an unattributed "I", "we" or "our"', "open with the core fact asked about"):
        assert rule in REPLY_VOICE_SYSTEM


def test_a_draft_turn_on_a_quest_with_many_goals_finds_the_look_alikes_past_the_first_page_and_names_only_them():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    everything = ([{"id": f"j{i}", "title": f"Grow subscribers task {i}", "text": "", "spec_solution_id": None, "completed": False} for i in range(150)]
                  + [{"id": "p1", "title": "FB push post A", "text": "a person's goal", "spec_solution_id": None, "completed": False},
                     {"id": "p2", "title": "FB push post B", "text": "a person's goal", "spec_solution_id": None, "completed": False}])

    def paged(args, ctx):
        needle = str(args.get("text_contains") or "").lower()
        rows = [g for g in everything if needle in f"{g['title']} {g['text']}".lower()] if needle else everything
        return ToolResult(ok=True, data=rows[:100], text="one page")      # a listing shows one page; the person's goals sit past it
    calls = []
    tools = ToolRegistry([read_spec(), ToolSpec(name="list_quest_goals", description="d", mutates=False, handler=paged), write_goal(calls)])
    versions = [dict(A_EXISTING, title="Facebook post A", goal_id=""), dict(B_NEW, title="Facebook post B", goal_id="")]
    tail = alternatives_flow.run(tools, "q1", "write a couple of facebook posts for the push", "reply",
                                 lambda prompt: alt_answer(*versions) if "Other goals on the quest" in prompt else '{"id": "NONE"}')
    assert calls == []
    assert tail.count("Already on the quest:") == 1 and "FB push post A, FB push post B (left as they are)." in tail and "Grow subscribers task" not in tail
    assert "Post A: A story hook gets more clicks" in tail and "Post B: A question hook gets more signups" in tail and "Compared by:" in tail


