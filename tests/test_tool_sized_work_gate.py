"""A deep run is for files, code, long research or many steps. A change to the person's Quest records that a registered tool performs is planned as
tool steps: the planner's structured deep_target and the tool set decide, never a word of the request (hard rule #3)."""
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_world.tools import tool_guards
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec

from .conftest import StubProvider, StubRetrieval

RAN = []


def spec(name, mutates):
    def handler(args, ctx):
        RAN.append((name, args))
        return ToolResult(ok=True, text=f"{name} ok")
    return ToolSpec(name=name, description="d", mutates=mutates, handler=handler)


def orch(provider, tools):
    config = OrchestratorConfig()
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=tools)


def deep(target="quest_data"):
    return {"action": "deep", "goal": "set up the reminder", "deep_brief": "b", "deep_target": target, "user_intent": "act", "rationale": "act", "pending_read": []}


LIST = {"action": "tool", "tool_calls": [{"name": "list_quest_workflows", "args": {}}], "user_intent": "act", "rationale": "read first", "pending_read": []}
WRITE = {"action": "tool", "tool_calls": [{"name": "create_quest_workflow", "args": {"title": "Monday check"}}], "user_intent": "act", "rationale": "write", "pending_read": []}
DONE = {"action": "answer", "user_intent": "act", "rationale": "done", "pending_read": []}


def registry():
    return ToolRegistry([spec("list_quest_workflows", False), spec("create_quest_workflow", True)])


def test_an_act_with_a_tool_available_is_planned_as_tool_steps_not_a_deep_run():
    RAN.clear()
    provider = StubProvider([deep(), LIST, WRITE, DONE])
    res = orch(provider, registry()).run("remind me every monday morning to check the numbers")
    assert [name for name, _ in RAN] == ["list_quest_workflows", "create_quest_workflow"]
    assert res.kind != "deep"
    assert "TOOL-SIZED WORK" in provider.plan_prompts[1]


def test_the_planner_that_still_chooses_deep_after_the_note_is_not_asked_again():
    provider = StubProvider([deep(), deep(), DONE])
    res = orch(provider, registry()).run("remind me every monday morning to check the numbers")
    assert provider.plan_prompts[1].count("TOOL-SIZED WORK") == 1
    assert res is not None and sum("TOOL-SIZED WORK" in p for p in provider.plan_prompts) == len(provider.plan_prompts) - 1


def test_a_code_or_file_job_still_escalates_without_a_second_plan():
    RAN.clear()
    provider = StubProvider([deep("code_or_files"), DONE])
    orch(provider, registry()).run("fix the back button in the app")
    assert RAN == [] and not any("TOOL-SIZED WORK" in p for p in provider.plan_prompts)


def test_guard_facts():
    from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import normalize_decision
    plan = normalize_decision(deep(), OrchestratorConfig())
    assert tool_guards.deep_is_tool_sized(plan, registry())
    assert not tool_guards.deep_is_tool_sized(plan, None)
    assert not tool_guards.deep_is_tool_sized(plan, ToolRegistry([spec("list_quest_workflows", False)]))
    plan.deep_subtasks = [{"goal": "a", "brief": "b"}, {"goal": "c", "brief": "d"}]
    assert not tool_guards.deep_is_tool_sized(plan, registry())
    assert not tool_guards.deep_is_tool_sized(normalize_decision(deep(None), OrchestratorConfig()), registry())
    code = normalize_decision(deep("code_or_files"), OrchestratorConfig())
    assert not tool_guards.deep_is_tool_sized(code, registry())


def answered(**over):
    return {"action": "answer", "user_intent": "act", "rationale": "describe", "pending_read": [], **over}


def test_an_order_answered_with_a_description_is_handed_back_once_to_plan_tool_steps():
    RAN.clear()
    provider = StubProvider([answered(), LIST, WRITE, DONE])
    orch(provider, registry()).run("make progress on the funnel")
    assert [name for name, _ in RAN] == ["list_quest_workflows", "create_quest_workflow"]
    assert "ORDER NOT CARRIED OUT" in provider.plan_prompts[1]


def test_a_question_back_before_acting_on_an_order_is_handed_back_too():
    provider = StubProvider([answered(asks_first=True), LIST, DONE])
    orch(provider, registry()).run("make progress on the funnel")
    assert "ORDER NOT CARRIED OUT" in provider.plan_prompts[1]


def test_an_order_answered_twice_stands_and_a_question_or_decline_is_not_handed_back():
    provider = StubProvider([answered(), answered()])
    orch(provider, registry()).run("make progress on the funnel")
    assert not any("ORDER NOT CARRIED OUT" in p for p in provider.plan_prompts[:1])
    assert provider.plan_prompts[-1].count("ORDER NOT CARRIED OUT: your user_intent") <= 1
    for plan in (answered(user_intent="ask"), answered(act_declined=True), answered(reply_shape="decline")):
        provider = StubProvider([plan])
        orch(provider, registry()).run("what is the funnel?")
        assert not any("ORDER NOT CARRIED OUT" in p for p in provider.plan_prompts)
    provider = StubProvider([answered()])
    orch(provider, None).run("make progress on the funnel")
    assert not any("ORDER NOT CARRIED OUT" in p for p in provider.plan_prompts)
