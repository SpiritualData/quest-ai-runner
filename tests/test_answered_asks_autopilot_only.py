"""Answered asks are pending work for a quest's AUTOPILOT only; with no autopilot nothing is created."""
from types import SimpleNamespace

from quest_ai_runner.consciousness.terminal.cli_context import answered_asks_without_autopilot_report
from quest_ai_runner.intelligence.proactivity import quest_schedule
from quest_ai_runner.intelligence.proactivity.autopilot_pending import (
    answered_ask_work, answered_asks_by_quest, answered_asks_without_autopilot)
from quest_ai_runner.intelligence.proactivity.poller_passes import PassesMixin

ROWS = [{"decision_id": "d0", "quest_id": "q1", "resolved_at": "2026-11-02T00:00:00Z"},
        {"decision_id": "d1", "quest_id": "q1", "resolved_at": "2026-11-01T00:00:00Z"},
        {"decision_id": "d2", "quest_id": "q2", "resolved_at": "2026-11-05T00:00:00Z"}]


class Client:
    configured = True

    def __init__(self, autopilots):
        self.autopilots, self.created = autopilots, []

    def list_pending_answers(self):
        return ROWS

    def list_autopilots(self, quest_id):
        return {"autopilots": self.autopilots.get(quest_id, [])}

    def list_quests(self, team_id=None):
        return [{"quest_id": "q1"}, {"quest_id": "q2"}]

    def get_quest_autopilot(self, quest_id):
        return {"autopilot": {"mode": "act"} if quest_id == "q1" else {"mode": "off"}}

    def create_task(self, *a, **k):
        self.created.append(k)


def test_autopilot_quest_gets_answered_asks_as_pending_work():
    assert answered_asks_by_quest(ROWS) == {"q1": (2, "2026-11-01T00:00:00Z"), "q2": (1, "2026-11-05T00:00:00Z")}
    assert answered_ask_work("q1", ROWS).count == 2 and answered_ask_work("q3", ROWS) is None

    class Lane(PassesMixin):
        pass
    lane = Lane()
    lane.client = Client({})
    lane.cfg = SimpleNamespace(team_ids=[], team_id="t", autopilot_settings_refresh_seconds=0, autopilot_pass_time="09:00",
                               autopilot_pending_work=None, quest_folder_map={})
    lane._quest_schedule_cache, lane._quest_schedule_cache_at = {}, None
    snap = lane._quest_schedule_snapshot()
    assert snap["q1"]["pending_work"] == 2 and snap["q1"]["mode"] == "act"
    assert lane.client.created == []                      # the snapshot creates nothing


def test_reactive_gate_counts_answered_asks_until_settled():
    pending = [{"ai_action_status": "pending", "resolved_at": "2026-11-05T00:00:00Z"}]
    settled = [{"ai_action_status": "acted", "resolved_at": "2026-11-05T00:00:00Z"}]
    assert quest_schedule.awaiting_ai_rows(pending) and not quest_schedule.awaiting_ai_rows(settled)


def test_quest_without_autopilot_is_listed_and_creates_no_task():
    client = Client({"q1": [{"id": "ap1", "status": {"runs": True}}], "q2": [{"id": "ap2", "status": {"runs": False}}]})
    assert answered_asks_without_autopilot_report(client) == [("q2", 1, "2026-11-05T00:00:00Z")]
    assert answered_asks_without_autopilot(ROWS, lambda q: False)[0][0] == "q1"   # oldest first
    assert client.created == []
