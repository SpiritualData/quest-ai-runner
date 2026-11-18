"""An ask raised by the runner carries the stakes its raiser named, so Quest need not judge it afterwards."""
from __future__ import annotations

from quest_ai_runner.body.internal_context.adapters_values import Escalation
from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient
from quest_ai_runner.consciousness.judgment_calls.ask_policy import Assignee, file_ask


def make_client(posted):
    client = QuestClient.__new__(QuestClient)
    client.team_id = "team_1"
    client.default_assignee_user_id = None
    client.list_open_decisions_for_quest = lambda quest_id: []

    def fake_write(method, path, body=None, label="", idempotency_key=None):
        posted.append(body)
        return {"id": "dec_1"}
    client._write = fake_write
    return client


def test_named_stakes_reach_the_create_route():
    posted = []
    make_client(posted).create_decision("Pay the invoice?", stakes="money", stakes_reason="Invoice due Friday")
    assert posted[0]["stakes"] == "money"
    assert posted[0]["stakes_reason"] == "Invoice due Friday"


def test_unknown_or_missing_stakes_are_left_for_quests_own_judge():
    posted = []
    client = make_client(posted)
    client.create_decision("A?", stakes="urgent!!")
    client.create_decision("B?")
    assert all("stakes" not in body for body in posted)


def test_file_ask_hands_stakes_to_the_sink():
    raised = []

    class Sink:
        def escalate(self, escalation: Escalation):
            raised.append(escalation)
            return "dec_1"

    outcome = file_ask(Sink(), question="Q?", why_only_you="w", done="", options=None, quest_id="q", conv_id="c",
                       turn_id="t", asker_user_id="u", assignee=Assignee("u", "asker"), summary="s",
                       stakes="people", stakes_reason="A teammate is waiting")
    assert outcome.filed
    assert raised[0].stakes == "people" and raised[0].stakes_reason == "A teammate is waiting"
