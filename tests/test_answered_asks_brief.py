"""An autopilot run's brief tells it to clear this quest's answered asks, only while some remain, from a live read."""
from quest_ai_runner.body.intake_sources import ask_settling
from quest_ai_runner.body.intake_sources.context_update_bundle import ContextUpdates
from quest_ai_runner.body.internal_context.quest.client.quest_client_support import WriteResult

from .autopilot_effective import AutopilotPass
from .test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _now, _quest

ROWS = [
    {"decision_id": "teamdec_aaa", "quest_id": "q1", "summary": "Send the grant", "resolution": "approved",
     "response_text": "Yes, but cc Dana", "resolved_by_name": "Zee", "resolved_at": "2026-11-02T00:00:00Z"},
    {"decision_id": "teamdec_bbb", "quest_id": "q1", "summary": "Rename the course", "resolution": "declined",
     "resolved_at": "2026-11-03T00:00:00Z"},
    {"decision_id": "teamdec_park", "quest_id": "q1", "summary": "Parked", "ai_action_parked": True, "resolved_at": "2026-11-04T00:00:00Z"},
    {"decision_id": "teamdec_other", "quest_id": "q2", "summary": "Elsewhere", "resolved_at": "2026-11-04T00:00:00Z"},
]


class Client(FakeAutopilotClient):
    def __init__(self, rows=ROWS, mode="act", **kw):
        q1 = _quest("q1", mode=mode)
        super().__init__(quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}, **kw)
        self.pending = [dict(r) for r in rows]
        self.marked = []

    def list_pending_answers(self):
        return list(self.pending)

    def mark_decisions_acted(self, ids, task_id=None):
        self.marked.append(list(ids))
        self.pending = [r for r in self.pending if r["decision_id"] not in ids]
        return WriteResult({"acted": list(ids)})


def _brief(client):
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    return client.created_tasks[-1]["text"] if client.created_tasks else ""


def setup_function(_):
    ask_settling.UNSETTLED.clear()


def test_brief_lists_this_quests_answered_asks_with_verbatim_answers():
    text = _brief(Client())
    assert "ANSWERED ASKS TO CLEAR" in text and "NO ACTION NEEDED: <id> <reason>" in text
    assert "teamdec_aaa" in text and "Yes, but cc Dana" in text and "teamdec_bbb" in text
    assert "teamdec_park" not in text and "teamdec_other" not in text


def test_brief_has_no_ask_text_when_none_pending():
    text = _brief(Client(rows=[ROWS[3]]))
    assert "ANSWERED ASKS" not in text and "NO ACTION NEEDED" not in text and "teamdec_" not in text


def test_autopilot_off_quest_composes_nothing():
    client = Client(mode="off")
    assert _brief(client) == "" and client.marked == []


def test_compose_then_settle_clears_the_asks_and_the_next_brief_omits_them():
    client = Client()
    first = _brief(client)
    assert "teamdec_aaa" in first and "teamdec_bbb" in first
    run_text = "Acted on teamdec_aaa.\nNO ACTION NEEDED: teamdec_bbb declined, nothing to do"
    sent = ContextUpdates(card_id="q1", relay_client=client).settle_answered_asks("t1", run_text)
    assert sorted(sent) == ["teamdec_aaa", "teamdec_bbb"]
    second = _brief(client)
    assert "ANSWERED ASKS" not in second and "teamdec_aaa" not in second
