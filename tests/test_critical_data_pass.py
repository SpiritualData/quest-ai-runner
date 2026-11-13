"""Critical data: what a request depends on is read before the mind plans, an unrelated request reads nothing, and a turn is met only after the check passed."""
import json
from types import SimpleNamespace

from quest_ai_runner.the_body.internal_context.quest import quest_ask_cards
from quest_ai_runner.the_intelligence.critical_thinking import critical_data_pass

QUEST = "quest_x1"
SOURCE = quest_ask_cards.asks_card_id(QUEST)
ASKS = [{"decision_id": "d1", "summary": "Where should the prep brief be saved?", "kind": "blocker", "assignee_name": "Ana", "created_at": "2026-11-05T14:10:21", "overdue": False},
        {"decision_id": "d2", "summary": "Approve the webinar page copy", "kind": "approve", "assignee_name": "Ben", "created_at": "2026-11-06T09:00:00", "overdue": True}]


class Store:
    def __init__(self):
        self.cards = {}

    def write_card(self, card_id, card):
        self.cards[card_id] = card
        return True

    def get_card(self, card_id):
        return self.cards.get(card_id)

    get_listed_card = get_card


class Client:
    def __init__(self, rows):
        self.rows = rows

    def open_decisions_or_none(self, quest_id):
        return self.rows


def orchestrator(store, asks=None, judge=None):
    """Answers the hub's two narrow questions: which offered source the request depends on, and whether the reply addresses it."""
    asked = []

    def cheap(prompt):
        asked.append(prompt)
        if "These sources are available" in prompt:      # the two views of the hub's declaration, both naming the source for a request about blockers
            named = [SOURCE] if "blocker" in prompt.split("A person asked:")[1].split("These sources")[0] else []
            return json.dumps({"needed": named} if "WITHOUT reading" in prompt else {"about": named})
        return json.dumps(judge(prompt) if judge else {"answers_request": True})
    assembler = SimpleNamespace(write_card=store.write_card, get_card=store.get_card)
    return SimpleNamespace(cheap_decision=cheap, context_assembler=assembler, asked=asked)


def turn(message, text="A reply."):
    rs = SimpleNamespace(user_message=message, goal_condition=message, gathered=[], context_view="", message_is_user_turn=True, text=text, critical_data=[])
    rs._gen_answer = lambda steer: "Rewritten from the asks."
    return rs


META = {"critical_sources": [{"id": SOURCE, "what": quest_ask_cards.ASKS_SOURCE_WHAT}]}


def test_the_card_names_each_open_ask_and_a_failed_read_leaves_it_alone():
    store = Store()
    assert quest_ask_cards.sync_open_asks_card(store, Client(ASKS), QUEST, "Growth", force=True)
    text = store.cards[SOURCE]["content"][0]["locator"]["text"]
    assert "waiting on Ana" in text and "Approve the webinar page copy" in text and "OVERDUE" in text
    assert store.cards[SOURCE]["scope_tags"] == ["quest:" + QUEST]
    before = dict(store.cards[SOURCE])
    assert not quest_ask_cards.sync_open_asks_card(store, Client(None), QUEST, "Growth", force=True)      # a failed read: None, never "nothing is waiting"
    assert store.cards[SOURCE] == before
    quest_ask_cards.sync_open_asks_card(store, Client([]), QUEST, "Growth", force=True)
    assert "nothing is waiting on a person" in store.cards[SOURCE]["content"][0]["locator"]["text"]


def test_a_request_that_depends_on_the_asks_reads_them_into_the_turn():
    store = Store()
    quest_ask_cards.sync_open_asks_card(store, Client(ASKS), QUEST, "Growth", force=True)
    rs = turn("What is the primary human blocker right now?")
    critical_data_pass.surface(orchestrator(store), rs, META)
    assert rs.context_view.startswith("WHAT THIS ANSWER DEPENDS ON") and "Approve the webinar page copy" in rs.context_view
    assert [i["consulted"] for i in rs.critical_data] == [True] and any(o["locator"].startswith("critical-data(") for o in rs.gathered)
    assert all(isinstance(o, dict) for o in rs.gathered)      # what the turn gathered is dicts: the loop sizes it with ``o.get``


def test_an_unrelated_request_on_the_same_quest_surfaces_no_ask():
    store = Store()
    quest_ask_cards.sync_open_asks_card(store, Client(ASKS), QUEST, "Growth", force=True)
    rs = turn("When is the mentorship landing page goal due?")
    critical_data_pass.surface(orchestrator(store), rs, META)
    assert rs.critical_data == [] and rs.context_view == "" and rs.gathered == []


def test_a_source_that_cannot_be_read_is_marked_unreadable_not_empty():
    rs = turn("What is the primary human blocker?")
    critical_data_pass.surface(orchestrator(Store()), rs, META)
    assert rs.critical_data[0]["unreadable"] is True and rs.critical_data[0]["consulted"] is False and rs.context_view == ""


def test_a_fluent_reply_that_does_not_address_the_request_is_not_met_and_is_rewritten_once_the_gap_is_named():
    store = Store()
    quest_ask_cards.sync_open_asks_card(store, Client(ASKS), QUEST, "Growth", force=True)
    verdicts = iter([{"answers_request": False, "missing": "who is blocking the work"}, {"answers_request": True}])
    orch = orchestrator(store, judge=lambda prompt: next(verdicts))
    rs = turn("What is the primary human blocker?", text="85 of 536 goals are done.")
    critical_data_pass.surface(orch, rs, META)
    verdict = critical_data_pass.check(orch, rs)
    assert verdict["met"] is True and rs.text == "Rewritten from the asks."
    assert [m for m in orch.asked if "Does the reply itself answer" in m].__len__() == 2


def test_a_reply_that_never_addresses_the_request_ends_not_met():
    orch = orchestrator(Store(), judge=lambda prompt: {"answers_request": False, "missing": "who is blocking the work"})
    rs = turn("What is the primary human blocker?", text="Goals: 85 done.")
    verdict = critical_data_pass.check(orch, rs)
    assert verdict["met"] is False and "does not answer" in verdict["reason"]


def test_an_unread_declared_source_leaves_the_turn_open_whatever_the_reply_says():
    store = Store()
    quest_ask_cards.sync_open_asks_card(store, Client(ASKS), QUEST, "Growth", force=True)
    rs = turn("What is the primary human blocker?", text="Ana is blocking.")
    rs.critical_data = [{"source": SOURCE, "why": "asks", "consulted": False, "unreadable": False}]
    verdict = critical_data_pass.check(orchestrator(store), rs)
    assert rs.critical_data[0]["consulted"] is True and verdict["met"] is True      # the stop decision read the named source and the reply was written again from it


def test_an_offer_may_carry_its_own_reader_for_a_consumer_whose_records_are_not_cards():
    menu = [{"id": SOURCE, "what": quest_ask_cards.ASKS_SOURCE_WHAT, "read": lambda: "Open asks: one waits on Ana."}]
    rs = turn("What is the primary human blocker?")
    critical_data_pass.surface(orchestrator(Store()), rs, {"critical_sources": menu})
    assert "one waits on Ana" in rs.context_view
    broken = [{"id": SOURCE, "what": quest_ask_cards.ASKS_SOURCE_WHAT, "read": lambda: None}]
    rs2 = turn("What is the primary human blocker?")
    critical_data_pass.surface(orchestrator(Store()), rs2, {"critical_sources": broken})
    assert rs2.critical_data[0]["unreadable"] is True
