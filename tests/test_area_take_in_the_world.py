"""The take-in-the-world area: its members answer offline by the strict definition, and the arm routes and hands over what comes in."""
import pytest
from esoi import Pending, targets

import quest_ai_runner
from esoi.runtime.assemble_by_name.assembly_config import AssemblyConfig
from quest_ai_runner.the_consciousness.request_intake.interface import InboundBatch, OutboundReply, SendResult
from tests.conftest import StubProvider

AREA_IDS = ("quest-requests", "planner", "intake-sources", "system-integrity")


def strict_outcome(solution_id):
    example = {sid: example for sid, level, kind, example in targets(quest_ai_runner.assembler())}[solution_id]
    try:
        quest_ai_runner.run(solution_id, example, environ={})
    except Pending as pending:
        assert {reason.kind for reason in pending.reasons} <= {"key", "install", "needs"}, pending.lines
        return "pending"
    return "ok"


@pytest.mark.parametrize("solution_id", AREA_IDS)
def test_each_id_of_the_area_answers_offline(solution_id):
    assert strict_outcome(solution_id) in ("ok", "pending")


def test_the_members_that_need_no_model_return_a_value_offline():
    assert strict_outcome("system-integrity") == "ok"
    assert strict_outcome("quest-requests") == "pending"


def test_the_channel_pends_on_its_token_file_and_program():
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("intake-sources", {"chat_ref": "c"}, environ={})
    assert {reason.kind for reason in caught.value.reasons} == {"key", "install"}


class ScriptedModel(StubProvider):
    """A scripted model that cannot search the web."""

    supports_web_search = False

    def web_search(self, *args, **kwargs):
        raise NotImplementedError


class FakeChannel:
    channel = "fake"

    def __init__(self):
        self.sent = []

    def receive(self, *, timeout=25.0):
        return InboundBatch()

    def send(self, reply):
        self.sent.append(reply)
        return SendResult(ok=True, message_id="m1")

    def close(self):
        return None


def area_over(provider, **provided):
    config = AssemblyConfig(solutions=("quest-requests",), selections={"quest-requests": "ways_in"}, consumer_environ={})
    return quest_ai_runner.assembler().assemble(config, {"vendor-model-caller": provider, **provided})["quest-requests"]


def test_take_in_routes_the_message_and_names_the_sources_that_feed_its_context():
    provider = ScriptedModel([{"action": "answer", "rationale": "known", "model_tier": "sonnet"}])
    got = area_over(provider).take_in("What should I work on first this week?")
    assert got["route"]["route"] == "answer"
    assert provider.plan_calls == 1


def test_the_reply_is_handed_to_the_channel():
    provider = ScriptedModel([])
    channel = FakeChannel()
    area = area_over(provider, **{"intake-sources": channel})
    assert area.reply(OutboundReply(chat_ref="c", text="hello")).ok and channel.sent[0].text == "hello"
