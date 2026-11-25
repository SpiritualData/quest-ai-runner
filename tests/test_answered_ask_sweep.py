"""The answered-ask sweep queues ONE task per quest with answers waiting for the AI, never two."""
from types import SimpleNamespace

from quest_ai_runner.intelligence.proactivity.poller_answers import (
    ANSWERED_ASKS_TITLE, AnswersMixin)


class Client:
    configured = True

    def __init__(self, pending, tasks=None):
        self.pending, self.tasks, self.created = pending, tasks or {}, []

    def list_pending_answers(self):
        return self.pending

    def list_tasks_or_none(self, *, goal_id=None, slim=False, **kw):
        return self.tasks.get(goal_id)

    def owning_team_for(self, quest_id):
        return "team_x"

    def create_task(self, text, **kw):
        self.created.append(kw)
        return SimpleNamespace(status="applied")


def _lane(client, **cfg):
    class Lane(AnswersMixin):
        pass
    lane = Lane()
    lane.client = client
    lane.cfg = SimpleNamespace(lane_user_id="u1", **cfg)
    return lane


def test_one_task_per_quest_with_pending_answers():
    client = Client([{"quest_id": "q1"}, {"quest_id": "q1"}, {"quest_id": "q2"}],
                    {"q1": [], "q2": []})
    assert _lane(client)._ensure_answered_ask_tasks() == ["q1", "q2"]
    assert [c["goal_id"] for c in client.created] == ["q1", "q2"]
    assert client.created[0]["title"] == ANSWERED_ASKS_TITLE and client.created[0]["team_id"] == "team_x"


def test_an_open_task_is_reused_and_a_failed_read_creates_nothing():
    open_task = {"status": "queued", "title": ANSWERED_ASKS_TITLE}
    client = Client([{"quest_id": "q1"}, {"quest_id": "q2"}, {"quest_id": "q3"}],
                    {"q1": [open_task], "q2": None, "q3": [{"status": "done", "title": ANSWERED_ASKS_TITLE}]})
    assert _lane(client)._ensure_answered_ask_tasks() == ["q3"]


def test_throttled_and_switchable():
    client = Client([{"quest_id": "q1"}], {"q1": []})
    lane = _lane(client)
    assert lane._ensure_answered_ask_tasks() == ["q1"]
    assert lane._ensure_answered_ask_tasks() == []          # inside the interval
    off = _lane(Client([{"quest_id": "q1"}], {"q1": []}), answered_asks_sweep=False)
    assert off._ensure_answered_ask_tasks() == []
