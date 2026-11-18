"""Cancel and pause checks of one run share a single task read per window (a deep run polled twice a second, ~100 KB each)."""
from quest_ai_runner.body.internal_context.quest.client.quest_client_tasks import TasksMixin


class _Client(TasksMixin):
    def __init__(self):
        self.reads = 0

    def get_task(self, task_id, *, slim=False):
        self.reads += 1
        return {"status": "running", "pause_requested": False, "cancel_requested": False}


def test_cancel_and_pause_checks_share_one_read():
    c = _Client()
    for _ in range(5):
        assert c.is_task_cancelled("atask_x") is False
        assert c.is_task_pause_requested("atask_x") is False
    assert c.reads == 1


def test_cancel_seen_after_window(monkeypatch):
    c = _Client()
    clock = [100.0]
    monkeypatch.setattr("quest_ai_runner.body.internal_context.quest.client.quest_client_tasks._control_now", lambda: clock[0])
    assert c.is_task_cancelled("atask_x") is False
    clock[0] += 30
    c.get_task = lambda task_id, *, slim=False: {"status": "cancelled"}
    assert c.is_task_cancelled("atask_x") is True
