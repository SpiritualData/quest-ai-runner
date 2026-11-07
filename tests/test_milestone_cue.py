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
GOALS = [{"id": "g1", "title": "esr_c next step: week of 2026-10-26", "spec_solution_id": "esr_c", "completed": True, "abandoned": False, "due": "2026-11-01",
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
    assert "esr_c" in block and "esr_mc" in block and "Owners the record lists: Cy, Di" in block and "esr_c next step: week of 2026-11-02" in block
    assert "Current value: No." in block and "Goals already on the quest for this milestone" in block and "already there" in block and "esr_c next step: week of 2026-10-26" in block
    assert "Unrelated" not in block and "America/Los_Angeles" in block


def test_the_facts_give_the_increment_order_and_no_owner_is_taken_from_the_purpose_text():
    block = cue.fact_block(TABLES, CUE, monday="2026-11-02")
    assert block.index("1. a deliverable") < block.index("2. else a metric value") < block.index("3. else an owned next step")
    tables = {**TABLES, "solutions": {**TABLES["solutions"], "esr_c": {**TABLES["solutions"]["esr_c"], "tracking_owner": "", "purpose": "Owners: Zed."}}}
    block = cue.fact_block(tables, CUE)
    assert "Zed" not in block and "none recorded on the milestone" in block


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
    assert calls[0]["title"] == "esr_c next step: week of 2026-11-02" and calls[0]["assignee"] == "Cy" and calls[0]["spec_solution_id"] == "esr_c"
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
    assert calls and calls[0]["title"].startswith("esr_c next step: week of ") and result.kind != "deep"
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
