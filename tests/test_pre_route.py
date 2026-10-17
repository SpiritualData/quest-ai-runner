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
    return router.settles(router.route("add a goal", ["create_quest_goal"]))


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
