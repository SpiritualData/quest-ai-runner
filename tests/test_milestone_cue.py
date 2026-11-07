"""Which milestone a short cue concerns, from synthetic spec records: the right one, the first-listed one when two are close, none when nothing fits."""
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue

TABLES = {
    "solutions": {
        "esr_a": {"name": "P1 Onboarding that captivates", "milestones": "Onboarding test passes on web and mobile.", "purpose": "Owners: Ann (rep_1), Bo (rep_2)."},
        "esr_b": {"name": "P2 Mobile app live on the Play Store", "milestones": "App live in the Play Store.", "purpose": "Owners: Cy (rep_3)."},
        "esr_c": {"name": "P3 Mobile app live on the Apple Store", "milestones": "App live in the App Store.", "purpose": "Owners: Cy (rep_3), Di (rep_4)."},
        "esr_d": {"name": "P5 Foundational product videos published", "milestones": "The agreed videos are published.", "purpose": "Owners: Ann (rep_1), Ed (rep_5)."},
        "esr_x": {"name": "Ann: daily check", "milestones": "", "purpose": ""},
    },
    "metrics": {
        "esr_ma": {"name": "P1 Onboarding completion", "record": "50 percent", "solution_ids": ["esr_a"]},
        "esr_mb": {"name": "P2 Play Store listing live", "record": "No", "solution_ids": ["esr_b"]},
        "esr_mc": {"name": "P3 App Store listing live", "record": "No. Apple lookup returned 0.", "solution_ids": ["esr_c"]},
        "esr_md": {"name": "P5 Foundational videos published", "record": "0 of 6", "solution_ids": ["esr_d"]},
    },
}


def test_a_cue_finds_its_milestone_and_the_facts_carry_the_keys():
    picked = cue.select(TABLES, "lets get the apple store release moving")
    assert picked and picked[0] == "esr_c"
    block = cue.fact_block(TABLES, "lets get the apple store release moving", monday="2026-11-02")
    assert "esr_c" in block and "esr_mc" in block and "Owners: Cy, Di" in block and "P3 next step: week of 2026-11-02" in block and "Current value: No." in block


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


def test_the_turn_is_handed_the_facts_through_the_registry_tool():
    from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec
    spec = ToolSpec(name="read_spec_records", description="d", mutates=False,
                    handler=lambda args, ctx: ToolResult(ok=True, data={"quest_id": args["quest_id"], "records": TABLES}, text="read"))
    block = cue.facts_for_turn(ToolRegistry([spec]), "q1", "lets get the apple store release moving")
    assert "esr_c" in block and "next step: week of" in block
    assert cue.facts_for_turn(ToolRegistry([]), "q1", "apple store") == "" and cue.facts_for_turn(None, "q1", "x") == ""


def test_a_pinned_turn_shows_the_planner_the_cue_facts_and_an_unpinned_one_does_not():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec
    from .conftest import StubProvider, StubRetrieval
    spec = ToolSpec(name="read_spec_records", description="d", mutates=False,
                    handler=lambda args, ctx: ToolResult(ok=True, data={"quest_id": args["quest_id"], "records": TABLES}, text="read"))

    def run(**kw):
        provider = StubProvider([{"action": "answer", "user_intent": "ask", "rationale": "r", "pending_read": []}])
        config = OrchestratorConfig()
        config.overseer = False
        Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config,
                     tools=ToolRegistry([spec])).run("lets get the apple store release moving", **kw)
        return provider.plan_prompts[0]
    assert "CUE FACTS" in run(quest_id="q1") and "CUE FACTS" not in run()


def test_the_cue_facts_are_read_as_a_milestone_measure_for_the_status_message():
    from quest_ai_runner.the_intelligence.mind.response_writer import act_status
    block = cue.fact_block(TABLES, "lets get the apple store release moving", monday="2026-11-02")
    reading = act_status.pick_reading(act_status.readings_in([{"text": block}]), "apple store release")
    assert reading.milestone.startswith("P3 Mobile app live on the Apple Store") and reading.value.startswith("No") and "App Store" in reading.done_when
