"""The pre-route settles only a sure, direct, non-confirming tool request; everything else goes to the planner."""
from quest_ai_runner.core.pre_route import PreRouter, parse


class Reply:
    def __init__(self, text):
        self.text = text

    def answer(self, messages, *, model, system=None, layers=None):
        if self.text is None:
            raise RuntimeError("provider down")
        return self.text


def settled(text, direct=("create_quest_goal",)):
    router = PreRouter(Reply(text), "m", direct)
    return router.settles(router.route("add a goal", "- create_quest_goal"))


SURE_GOAL = '{"route": "tool", "calls": [{"name": "create_quest_goal", "args": {"title": "18 km"}}], "sure": true}'


def test_sure_direct_tool_call_settles():
    assert settled(SURE_GOAL)


def test_unsure_answer_confirm_and_deep_fall_through():
    assert not settled(SURE_GOAL.replace("true", "false"))
    assert not settled('{"route": "answer", "calls": [], "sure": true}')
    assert not settled('{"route": "confirm", "calls": [], "sure": true}')
    assert not settled('{"route": "deep", "calls": [], "sure": true}')


def test_guarded_tool_or_confirming_argument_falls_through():
    assert not settled(SURE_GOAL, direct=("create_quest_habit",))
    risky = '{"route": "tool", "calls": [{"name": "create_quest_goal", "args": {"confirm_delete": true}}], "sure": true}'
    assert not settled(risky)


def test_bad_reply_or_provider_failure_is_never_worse_than_no_pre_route():
    assert parse("not json") is None and parse('{"route": "bogus"}') is None
    assert not settled("not json") and not settled(None)


# --- the orchestrator hook -------------------------------------------------------------------

from quest_ai_runner.core.model_registry import ModelRegistry  # noqa: E402
from quest_ai_runner.core.orchestrator import Orchestrator  # noqa: E402
from quest_ai_runner.core.pre_route import NoPreRoute  # noqa: E402
from quest_ai_runner.core.tools import ToolRegistry, ToolResult, ToolSpec  # noqa: E402

from .conftest import StubProvider, StubRetrieval  # noqa: E402


def orchestrator(reply, router_cls=PreRouter, direct=True):
    stub = StubProvider([{"action": "answer", "rationale": "planner decided"}], answer_text=reply)
    spec = ToolSpec(name="create_quest_goal", description="Create a goal", mutates=True, direct=direct,
                    handler=lambda args, ctx: ToolResult(ok=True, text="made"))
    orch = Orchestrator(retrieval=StubRetrieval(), provider=stub, registry=ModelRegistry(stub),
                        tools=ToolRegistry([spec]), pre_router_cls=router_cls)
    return orch, stub


def test_a_sure_direct_request_skips_the_planner():
    orch, stub = orchestrator(SURE_GOAL)
    plan = orch._plan("add a goal: 18 km", "", "", [])
    assert plan.action == "tool" and plan.tool_calls[0]["name"] == "create_quest_goal"
    assert stub.plan_calls == 0 and stub.answer_calls == 1


def test_an_unsure_request_goes_to_the_planner():
    orch, stub = orchestrator(SURE_GOAL.replace("true", "false"))
    assert orch._plan("add a goal: 18 km", "", "", []).rationale == "planner decided"
    assert stub.plan_calls == 1


def test_no_pre_router_and_no_direct_tool_cost_nothing():
    for kwargs in ({"router_cls": None}, {"router_cls": NoPreRoute}, {"direct": False}):
        orch, stub = orchestrator(SURE_GOAL, **kwargs)
        orch._plan("add a goal: 18 km", "", "", [])
        assert stub.plan_calls == 1 and stub.answer_calls == 0


def test_narration_and_later_steps_keep_the_planner():
    orch, stub = orchestrator(SURE_GOAL)
    orch._plan("add a goal", "", "", [], narrate=True)
    orch._plan("add a goal", "", "", [], step=1)
    assert stub.plan_calls == 2 and stub.answer_calls == 0
