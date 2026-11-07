"""Which milestone a short cue concerns, from synthetic spec records: the right one, the first-listed one when two are close, none when nothing fits."""
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue

TABLES = {
    "solutions": {
        "esr_a": {"name": "P1 Onboarding that captivates", "milestones": "Onboarding test passes on web and mobile.", "tracking_owner": "Ann; Bo"},
        "esr_b": {"name": "P2 Mobile app live on the Play Store", "milestones": "App live in the Play Store.", "tracking_owner": "Cy"},
        "esr_c": {"name": "P3 Mobile app live on the Apple Store", "milestones": "App live in the App Store.", "tracking_owner": "Cy, Di"},
        "esr_d": {"name": "P5 Foundational product videos published", "milestones": "The agreed videos are published.", "tracking_owner": "Ann, Ed"},
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

import pytest

from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

CUE = "lets get the apple store release moving"
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
    assert "esr_c" in block and "esr_mc" in block and "tracking_owner: Cy, Di" in block and "P3 Mobile app live on the Apple Store next step: week of 2026-11-02" in block
    assert "Current value: No." in block and "Open goals on the quest that may be" in block and "already there" in block and "esr_c next step: week of 2026-10-26" in block
    assert "Unrelated" not in block and "America/Los_Angeles" in block


def test_the_facts_give_the_increment_order_and_the_full_purpose_text_for_the_model_to_assign_from():
    block = cue.fact_block(TABLES, CUE, monday="2026-11-02")
    assert block.index("1. a deliverable") < block.index("2. else a metric value") < block.index("3. else an owned next step")
    tables = {**TABLES, "solutions": {**TABLES["solutions"], "esr_c": {**TABLES["solutions"]["esr_c"], "tracking_owner": "", "purpose": "Owners: Zed."}}}
    block = cue.fact_block(tables, CUE)
    assert "Owners: Zed." in block and "tracking_owner: empty" in block and "never ask who should own it" in block


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
    assert not resolved.routes_to_code(PlanDecision(action="answer", deep_target="draft_in_chat"), "act", [], tools)   # rung 1: a deliverable in the reply
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
            return {"records": [{"type": "solution", "recordId": "esr_1", "fields": {"name": "A", "tracking_owner": "Ann"}}]}

        def get_quest_autopilot(self, quest_id):
            return {"autopilot": {"run_timezone": "Asia/Kolkata"}}
    monkeypatch.setattr(api, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the assembly (and quest placement) must not run for a pinned read")))
    spec = read_spec_records_spec({}, client_factory=Client)
    done = spec.handler({}, ToolContext(quest_id="q1"))
    assert done.ok and done.data["time_zone"] == "Asia/Kolkata" and done.data["records"]["solutions"]["esr_1"]["tracking_owner"] == "Ann"


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
    tables = {**TABLES, "solutions": {**TABLES["solutions"], "esr_c": {**TABLES["solutions"]["esr_c"], "tracking_owner": "", "purpose": "Owners: Cy and Di."}}}

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
        return "g9" if "Open goals" in prompt else "Cy"
    receipt, _ = resolved.carry_out(tools, decide)
    assert len(asked) == 2 and calls[0]["adopt_goal_id"] == "g9" and calls[0]["assignee"] == "Cy" and calls[0]["title"] == "P3 Mobile app live on the Apple Store next step: week of 2026-11-02"
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
    resolved.carry_out(tools, lambda prompt: "g9" if "Open goals" in prompt else "NONE")
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
    client = GoalClient([{"id": "k", "name": KEYED, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "description": "d", "period": "2026_W45"},
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


def test_the_receipt_states_the_assignee_name_solution_and_metric_as_read_back_after_the_write():
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_goals import create_or_update_quest_goal_spec
    from quest_ai_runner.the_world.tools.tools import ToolContext
    client = GoalClient([{"id": "old", "name": "P3 next step: week of 2026-11-02", "spec_solution_id": None, "period": "2026_W45"}])
    spec = create_or_update_quest_goal_spec({}, client_factory=lambda: client)
    client.members = [{"user_id": "u1", "name": "Cy Owner", "email": "x"}]
    import quest_ai_runner.the_body.hands.quest_operations.goal_links as gl
    gl.resolve_assignee = lambda c, q, who: ("u1", None)
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
    held = {"id": "k", "name": KEYED, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc", "period": "2026_W45", "assigned_to_user_id": None}
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
