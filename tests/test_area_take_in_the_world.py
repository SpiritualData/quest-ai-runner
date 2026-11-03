"""The take-in-the-world area: its members answer offline by the strict definition, and the arm routes and hands over what comes in."""
import pytest
from esoi.entry import Pending, targets

import quest_ai_runner
from quest_ai_runner.area_take_in_the_world import RequestTakingIn
from quest_ai_runner.core.adapters_channels import InboundBatch, OutboundReply, SendResult
from quest_ai_runner.core.inbox import InMemoryInbox
from tests.conftest import StubProvider

AREA_IDS = ("take-in-the-world", "route-a-message", "talk-over-a-channel", "receive-input-mid-run", "feed-context-sources",
            "describe-an-attachment", "operate-from-a-terminal", "assist-a-terminal-session", "expose-the-public-api")


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
    for solution_id in ("receive-input-mid-run", "feed-context-sources", "operate-from-a-terminal", "expose-the-public-api"):
        assert strict_outcome(solution_id) == "ok"


def test_the_channel_pends_on_its_token_file_and_program():
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("talk-over-a-channel", {"chat_ref": "c"}, environ={})
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
    return quest_ai_runner.run(RequestTakingIn, environ={}, provided={"call-a-model": provider, **provided})


def test_take_in_routes_the_message_and_names_the_sources_that_feed_its_context():
    provider = ScriptedModel([{"action": "answer", "rationale": "known", "model_tier": "sonnet"}])
    got = area_over(provider).take_in("What should I work on first this week?")
    assert got["route"]["route"] == "answer" and "quest_notes" in got["sources"]
    assert provider.plan_calls == 1


def test_each_way_in_is_handed_to_the_member_that_owns_it():
    provider = ScriptedModel([], answer_text='{"ai_doable": true, "confidence": 0.9, "reason": "text only"}')
    channel, inbox = FakeChannel(), InMemoryInbox()
    area = area_over(provider, **{"talk-over-a-channel": channel, "receive-input-mid-run": inbox})
    assert area.reply(OutboundReply(chat_ref="c", text="hello")).ok and channel.sent[0].text == "hello"
    area.hold("conversation-1", "one more thing")
    assert inbox.peek("conversation-1") == ["one more thing"]
    prepared = area.prepare([{"filename": "notes.txt", "mime_type": "text/plain", "data": b"Call the mentor on Friday."}])
    assert "Call the mentor on Friday." in prepared.text_context
    ran = area.operate(["--help"])
    assert ran.code == 0 and "usage" in ran.output
    assert area.judge_goal({"name": "Draft the launch note"})["ai_doable"] is True
    assert area.front_door("run") is quest_ai_runner.run
    with pytest.raises(KeyError):
        area.front_door("not_offered")
