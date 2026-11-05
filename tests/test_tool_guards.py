"""Checks the code makes on a tool call before it runs: an id the turn never saw, a change nobody ordered, a read that owes its
companion. Offline; no model."""
from types import SimpleNamespace

from hubofhubs.api import ExecutionRecord

from quest_ai_runner.the_world.tools import tool_guards
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

from .conftest import StubProvider, StubRetrieval

LISTING = ("The person's quests:\n"
           "- q_fit: Sub-50 10K. Outcome: Run a sub-50 10K at the Riverside 10K on 22 Nov 2026.\n"
           "- q_biz: Tiny Blue Mug candle shop. Outcome: Launch the candle shop and reach 100 orders.\n"
           "- q_home: Bathroom renovation. Outcome: Finish the retile by 20 Dec.\n"
           "Note: Wax 210 (materials).")


def quest_tool(name, mutates=False, companions=()):
    return ToolSpec(name=name, description="d", mutates=mutates, handler=lambda args, ctx: ToolResult(ok=True, text=f"{name} ran"),
                    parameters={"type": "object", "properties": {"quest_id": {"type": "string"}}},
                    context_args={"quest_id": "quest_id"}, read_companions=companions)


class Silent:
    def status(self, *a, **k): pass
    def emit(self, *a, **k): pass


def orchestrator(specs):
    registry = ToolRegistry(specs)
    return Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]), registry=ModelRegistry(StubProvider([])), tools=registry)


def run_calls(orch, calls, evidence=LISTING, order_gate=None, gathered=None, signatures=None):
    gathered = [] if gathered is None else gathered
    orch.run_tool_calls(calls, gathered=gathered, exec_record=ExecutionRecord(), tool_ok_signatures={} if signatures is None else signatures,
                        emit=Silent(), steps=1, ctx=ToolContext(), evidence=lambda: evidence, order_gate=order_gate)
    return gathered


def test_an_invented_id_is_mapped_to_the_one_listed_quest_its_words_name():
    spec = quest_tool("list_quest_goals")
    assert tool_guards.check_quest_ids(spec, {"quest_id": "q_riverside_10k"}, LISTING).args["quest_id"] == "q_fit"
    assert tool_guards.check_quest_ids(spec, {"quest_id": "q_candle_launch"}, LISTING).args["quest_id"] == "q_biz"


def test_an_invented_id_that_names_nothing_listed_is_refused_with_the_valid_ids():
    verdict = tool_guards.check_quest_ids(quest_tool("list_quest_goals"), {"quest_id": "q_garden_beds"}, LISTING)
    assert verdict.refusal and "q_fit" in verdict.refusal and "q_biz" in verdict.refusal and "q_home" in verdict.refusal


def test_an_id_the_turn_saw_the_pinned_id_and_an_id_with_nothing_to_judge_against_pass():
    spec = quest_tool("list_quest_goals")
    assert tool_guards.check_quest_ids(spec, {"quest_id": "q_biz"}, LISTING).refusal is None
    assert tool_guards.check_quest_ids(spec, {"quest_id": "q_zzz"}, "no ids here at all", pinned="q_zzz").refusal is None
    verdict = tool_guards.check_quest_ids(spec, {"quest_id": "q_zzz"}, "no ids here at all")
    assert verdict.refusal is None and verdict.args["quest_id"] == "q_zzz"


def test_a_tied_match_is_refused_not_guessed():
    both = LISTING + "\n- q_cafe: Candle cafe. Outcome: Open a candle cafe."
    assert tool_guards.resolve_unknown("q_candle_shop", {"q_biz": "Tiny Blue Mug candle shop", "q_cafe": "Candle shop cafe"}) is None
    assert tool_guards.check_quest_ids(quest_tool("t"), {"quest_id": "q_candle"}, both).refusal


def test_the_run_maps_an_invented_id_before_the_tool_runs_and_refuses_one_it_cannot_map():
    orch = orchestrator([quest_tool("list_quest_goals")])
    seen = []
    orch.tools.get("list_quest_goals").handler = lambda args, ctx: seen.append(args["quest_id"]) or ToolResult(ok=True, text="goals")
    gathered = run_calls(orch, [{"name": "list_quest_goals", "args": {"quest_id": "q_riverside_10k"}},
                                {"name": "list_quest_goals", "args": {"quest_id": "q_garden_beds"}}])
    assert seen == ["q_fit"]
    assert gathered[-1]["tool_status"] == "refused" and "q_home" in gathered[-1]["text"]


def test_an_echoed_invented_id_in_a_receipt_header_is_not_evidence():
    orch = orchestrator([quest_tool("list_quest_goals")])
    rs = SimpleNamespace(user_message="m", transcript="", context_view=LISTING, rep_preamble="", context_meta={},
                         gathered=[{"locator": "tool:list_quest_goals", "text": 'TOOL x REFUSED (args {"quest_id": "q_made_up"}):\nbody'}])
    text = orch.turn_evidence(rs)
    assert "q_made_up" not in text and "body" in text and "q_fit" in text


def test_a_read_with_a_companion_runs_the_companion_once_per_quest():
    orch = orchestrator([quest_tool("list_quest_goals", companions=("list_quest_notes",)), quest_tool("list_quest_notes")])
    gathered = run_calls(orch, [{"name": "list_quest_goals", "args": {"quest_id": "q_biz"}},
                                {"name": "list_quest_goals", "args": {"quest_id": "q_biz", "open_only": True}}])
    assert [g["locator"] for g in gathered].count("tool:list_quest_notes") == 1
    assert 'q_biz' in [g for g in gathered if g["locator"] == "tool:list_quest_notes"][0]["text"]


def test_a_companion_already_read_is_not_read_again():
    orch = orchestrator([quest_tool("list_quest_goals", companions=("list_quest_notes",)), quest_tool("list_quest_notes")])
    earlier = [{"locator": "tool:list_quest_notes", "text": 'TOOL list_quest_notes SUCCEEDED (args {"quest_id": "q_biz"}):\nnotes'}]
    gathered = run_calls(orch, [{"name": "list_quest_goals", "args": {"quest_id": "q_biz"}}], gathered=earlier)
    assert [g["locator"] for g in gathered].count("tool:list_quest_notes") == 1


def test_a_write_is_refused_when_the_gate_says_nothing_was_ordered_and_the_repeat_is_not_rerun():
    ran = []
    write = quest_tool("create_quest_habit", mutates=True)
    write.handler = lambda args, ctx: ran.append(1) or ToolResult(ok=True, text="done")
    orch = orchestrator([write])
    signatures = {}
    call = {"name": "create_quest_habit", "args": {"quest_id": "q_fit"}}
    gathered = run_calls(orch, [call], order_gate=lambda: False, signatures=signatures)
    gathered = run_calls(orch, [call], order_gate=lambda: False, gathered=gathered, signatures=signatures)
    assert ran == [] and "did not order" in gathered[0]["text"] and "not run again" in gathered[1]["text"]


def test_the_gate_lets_a_write_through_when_it_says_ordered_or_cannot_tell():
    ran = []
    write = quest_tool("create_quest_habit", mutates=True)
    write.handler = lambda args, ctx: ran.append(1) or ToolResult(ok=True, text="done")
    orch = orchestrator([write])
    run_calls(orch, [{"name": "create_quest_habit", "args": {"quest_id": "q_fit"}}], order_gate=lambda: True)
    run_calls(orch, [{"name": "create_quest_habit", "args": {"quest_id": "q_home"}}], order_gate=lambda: None)
    assert ran == [1, 1]


def test_the_gate_judges_only_a_typed_question_once_and_fails_open():
    orch = orchestrator([quest_tool("t")])
    asked = []
    orch.directive_verdict = lambda message, answer: (asked.append(message), (False, "asks"))[1]
    question = SimpleNamespace(user_message="How do I add a habit?", message_is_user_turn=True)
    assert orch.change_was_ordered(question) is False and orch.change_was_ordered(question) is False and len(asked) == 1
    assert orch.change_was_ordered(SimpleNamespace(user_message="Add a habit.", message_is_user_turn=True)) is None
    assert orch.change_was_ordered(SimpleNamespace(user_message="How do I?", message_is_user_turn=False)) is None
    orch.directive_verdict = lambda message, answer: (None, "no judgment")
    assert orch.change_was_ordered(SimpleNamespace(user_message="Can you add it?", message_is_user_turn=True)) is None


def test_the_message_names_the_quests_whose_listing_shares_its_words():
    message = "How many days until my Riverside 10K, and how much of my candle launch budget is left?"
    assert sorted(tool_guards.named_quests(message, LISTING)) == ["q_biz", "q_fit"]
    assert tool_guards.named_quests("What is on my plate today?", LISTING) == []
    assert "Note" not in tool_guards.listed_quests(LISTING)


def test_detail_reads_are_the_companions_a_registry_declares():
    registry = ToolRegistry([quest_tool("list_quest_goals", companions=("list_quest_notes",)), quest_tool("list_quest_notes")])
    assert tool_guards.detail_reads(registry) == ["list_quest_notes"]


def run_turn(message, decisions, specs, context_view=LISTING, quest_id=None, order_verdict=None):
    from quest_ai_runner.the_intelligence.mind.orchestrator_types import OrchestratorConfig
    provider = StubProvider(decisions)
    config = OrchestratorConfig()
    config.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config,
                        tools=ToolRegistry(specs))
    orch.directive_verdict = lambda m, a: (order_verdict, "stub")
    result = orch.run(message, quest_id=quest_id, context_view=context_view)
    return orch, provider, result


def test_an_answer_about_a_named_quest_reads_its_notes_first_by_code():
    ran = []
    notes = quest_tool("list_quest_notes")
    notes.handler = lambda args, ctx: ran.append(args["quest_id"]) or ToolResult(ok=True, text="Budget cap 2,000 USD")
    goals = quest_tool("list_quest_goals", companions=("list_quest_notes",))
    answer = {"action": "answer", "user_intent": "ask", "rationale": "r", "pending_read": []}
    run_turn("How much of my candle launch budget is left?", [answer, answer], [goals, notes])
    assert ran == ["q_biz"]


def test_a_question_that_names_no_listed_quest_reads_no_notes():
    ran = []
    notes = quest_tool("list_quest_notes")
    notes.handler = lambda args, ctx: ran.append(args["quest_id"]) or ToolResult(ok=True, text="n")
    goals = quest_tool("list_quest_goals", companions=("list_quest_notes",))
    answer = {"action": "answer", "user_intent": "ask", "rationale": "r", "pending_read": []}
    run_turn("What is the capital of France?", [answer], [goals, notes])
    assert ran == []


def test_a_how_do_i_question_that_proposes_a_write_changes_nothing():
    ran = []
    write = quest_tool("create_quest_habit", mutates=True)
    write.handler = lambda args, ctx: ran.append(1) or ToolResult(ok=True, text="done")
    propose = {"action": "tool", "user_intent": "act", "rationale": "r",
               "tool_calls": [{"name": "create_quest_habit", "args": {"quest_id": "q_fit"}}]}
    answer = {"action": "answer", "user_intent": "ask", "rationale": "r", "pending_read": []}
    run_turn("How do I add a stretching habit to my fitness quest?", [propose, answer], [write], order_verdict=False)
    assert ran == []
    run_turn("Add a stretching habit to my fitness quest.", [propose, answer], [write], order_verdict=False)
    assert ran == [1]


def test_tool_calls_with_no_action_are_run_not_dropped():
    from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import normalize_decision
    from quest_ai_runner.the_intelligence.mind.orchestrator_types import OrchestratorConfig
    raw = {"action": None, "tool_calls": [{"name": "create_quest_goal", "args": {"quest_id": "q_home"}}]}
    assert normalize_decision(raw, OrchestratorConfig(), tools_enabled=True).action == "tool"
    assert normalize_decision(raw, OrchestratorConfig(), tools_enabled=False).action == "answer"
    assert normalize_decision({"action": None}, OrchestratorConfig(), tools_enabled=True).action == "answer"
    assert normalize_decision(raw, OrchestratorConfig(), tools_enabled=True, infer_tool_action=False).action == "answer"


def test_a_planner_about_to_read_elsewhere_reads_the_named_quests_notes_first():
    ran = []
    notes = quest_tool("list_quest_notes")
    notes.handler = lambda args, ctx: ran.append(args["quest_id"]) or ToolResult(ok=True, text="Budget cap 2,000 USD")
    goals = quest_tool("list_quest_goals", companions=("list_quest_notes",))
    search = {"action": "read", "user_intent": "ask", "rationale": "r", "reads": [{"web": "candle launch budget"}]}
    answer = {"action": "answer", "user_intent": "ask", "rationale": "r", "pending_read": []}
    run_turn("How much of my candle launch budget is left?", [search, answer], [goals, notes])
    assert ran == ["q_biz"]


# --- near-duplicate writes in one step ------------------------------------------------------------------------------------------

def write_tool(name):
    return quest_tool(name, mutates=True)


def merged(calls, mutating=("add_quest_note", "create_quest_goal"), pinned="q_fit"):
    return tool_guards.merge_near_duplicates(calls, lambda n: n in mutating, pinned)


def note(text, quest="q_fit", name="add_quest_note"):
    return {"name": name, "args": {"quest_id": quest, "text": text}}


def test_a_note_saved_twice_in_other_words_runs_once():
    first = note("Physio cleared easy cycling from Monday; no running until the 10 days are up.")
    second = note("The physio cleared easy cycling from Monday, no running until the 10 days are up.")
    kept, dropped = merged([first, second])
    assert kept == [first] and len(dropped) == 1 and dropped[0][0] == "add_quest_note"


def test_two_different_goals_in_one_message_are_both_written():
    plumbing = {"name": "create_quest_goal", "args": {"quest_id": "q_home", "title": "Plumbing visit", "period": "2026_11"}}
    tiling = {"name": "create_quest_goal", "args": {"quest_id": "q_home", "title": "Tile the walls", "period": "2026_12"}}
    assert merged([plumbing, tiling])[0] == [plumbing, tiling]


def test_goals_that_share_a_template_or_differ_in_a_figure_stay_two():
    run3 = {"name": "create_quest_goal", "args": {"quest_id": "q_fit", "title": "Run three times this week"}}
    swim3 = {"name": "create_quest_goal", "args": {"quest_id": "q_fit", "title": "Swim three times this week"}}
    assert merged([run3, swim3])[0] == [run3, swim3]
    five = note("Run the long route on Sunday for about 5 km at an easy pace")
    ten = note("Run the long route on Sunday for about 10 km at an easy pace")
    assert merged([five, ten])[0] == [five, ten]


def test_the_same_text_on_another_quest_another_tool_or_a_read_is_not_merged():
    text = "Physio cleared easy cycling from Monday; no running until the 10 days are up."
    other_quest = note(text, quest="q_home")
    assert len(merged([note(text), other_quest])[0]) == 2
    assert len(merged([note(text), note(text, name="create_quest_goal")])[0]) == 2
    assert len(merged([note(text, name="list_notes"), note(text + " ok", name="list_notes")])[0]) == 2


def test_a_text_too_short_to_judge_is_compared_for_equality_only():
    assert len(merged([note("Buy shoes"), note("Buy new shoes")])[0]) == 2


def test_the_run_writes_a_near_identical_second_call_once_and_two_different_goals_twice():
    ran = []
    goal = write_tool("create_quest_goal")
    goal.parameters = {"type": "object", "properties": {"quest_id": {"type": "string"}, "title": {"type": "string"}}}
    goal.handler = lambda args, ctx: ran.append(args["title"]) or ToolResult(ok=True, text="made")
    orch = orchestrator([goal])
    run_calls(orch, [{"name": "create_quest_goal", "args": {"quest_id": "q_fit", "title": "Run three times this week, easy pace"}},
                     {"name": "create_quest_goal", "args": {"quest_id": "q_fit", "title": "Run three times this week at an easy pace"}}])
    assert ran == ["Run three times this week, easy pace"]
    ran.clear()
    run_calls(orch, [{"name": "create_quest_goal", "args": {"quest_id": "q_home", "title": "Plumbing visit"}},
                     {"name": "create_quest_goal", "args": {"quest_id": "q_home", "title": "Tile the walls"}}])
    assert ran == ["Plumbing visit", "Tile the walls"]


# --- a question about the world right now is read from the web ------------------------------------------------------------------

WORLD = {"reach": "world", "covered_by": None}


def test_a_world_verdict_with_web_configured_and_no_web_read_owes_a_search():
    owed = tool_guards.web_read_owed(WORLD, True, [], "How much does a Duolingo Super subscription cost right now?")
    assert owed == {"web": "How much does a Duolingo Super subscription cost right now?"}


def test_no_search_is_owed_without_web_without_a_world_verdict_or_after_a_web_read():
    message = "What does it cost right now?"
    assert tool_guards.web_read_owed(WORLD, False, [], message) is None
    assert tool_guards.web_read_owed(None, True, [], message) is None
    assert tool_guards.web_read_owed({"reach": "inside"}, True, [], message) is None
    assert tool_guards.web_read_owed({"reach": "outside", "covered_by": None}, True, [], message) is None
    assert tool_guards.web_read_owed(WORLD, True, [{"kind": "query", "rel_path": "web_search:price"}], message) is None
    assert tool_guards.web_read_owed(WORLD, True, [{"locator": "web extract: https://x.example"}], message) is None
    assert tool_guards.web_read_owed(WORLD, True, [], "   ") is None


def test_the_loop_searches_the_web_before_an_answer_the_reach_judge_called_world_and_not_when_it_did_not():
    from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorConfig

    from .test_web_reads import FakeWeb

    def run(verdict, decisions):
        web = FakeWeb()
        config = OrchestratorConfig()
        config.overseer = False
        provider = StubProvider(decisions)
        orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, web=web)
        orch.reach_verdict = lambda message: verdict
        orch.run("How much does the Super plan cost right now?")
        return web.search_calls

    answer = {"action": "answer", "user_intent": "ask", "rationale": "from memory", "pending_read": []}
    assert len(run(WORLD, [answer, answer])) == 1
    assert run({"reach": "inside"}, [answer]) == []
    assert run(None, [answer]) == []
    searched = {"action": "read", "reads": [{"web": "super plan price"}], "user_intent": "ask", "rationale": "look it up"}
    assert len(run(WORLD, [searched, answer])) == 1    # the planner searched itself: the code adds none


def test_the_reach_judge_prompt_names_its_keys_so_a_provider_without_forced_tool_use_answers_in_them():
    from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorConfig

    provider = StubProvider([{"reach": "world"}])
    config = OrchestratorConfig()
    config.read_reach_summary = "Reads reach the person's quests."
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config)
    assert orch.judge_reach("What does it cost right now?")["reach"] == "world"
    assert '"reach"' in provider.last_plan_prompt and "exactly these properties" in provider.last_plan_prompt


# --- an inside verdict vetoes a web read -----------------------------------------------------------------------------------------

def test_an_inside_verdict_drops_web_reads_and_keeps_the_rest():
    reads = [{"web": "race date"}, {"tool": "list_quest_notes"}, {"web_page": "https://x.example"}]
    kept, dropped = tool_guards.drop_web_reads_for_own_records({"reach": "inside"}, reads)
    assert kept == [{"tool": "list_quest_notes"}]
    assert len(dropped) == 2


def test_other_verdicts_and_no_verdict_keep_every_web_read():
    reads = [{"web": "price"}]
    for verdict in (None, {}, WORLD, {"reach": "outside", "covered_by": None}):
        assert tool_guards.drop_web_reads_for_own_records(verdict, reads) == (reads, [])
