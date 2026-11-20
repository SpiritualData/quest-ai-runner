"""The delivery-history read leaves out each task's progress log: unfiltered it was 8.37 MB per call (2026-11-20)."""
from types import SimpleNamespace

from quest_ai_runner.body.intake_sources.context_sources_base import _runs_delivered


class Lister:
    def __init__(self):
        self.calls = []

    def list_tasks(self, **kw):
        self.calls.append(kw)
        return [{"status": "done", "result": "Sent.", "updated_at": "2026-11-19T10:00:00+00:00"}]


def test_delivery_history_asks_for_results_without_the_progress_log():
    client = Lister()
    request = SimpleNamespace(client=client, card_id="q1", cache={})
    assert len(_runs_delivered(request)) == 1
    assert client.calls == [{"goal_id": "q1", "without_progress": True}]
