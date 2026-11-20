"""Every PATCH of a task asks for ?slim=true: the reply was the whole task (~0.3 MB with a long run's log) and the runner only
checks that the write landed (2026-11-20, a capped home line). An older backend ignores the flag and sends the whole task."""
from datetime import datetime, timezone

import pytest

from quest_ai_runner.body.internal_context.quest.client.quest_client_tasks import TasksMixin


class _Client(TasksMixin):
    def __init__(self):
        self.calls = []

    @staticmethod
    def utc_stamp(dt):
        return dt.isoformat()

    def _request(self, method, path, params=None, body=None, **_kw):
        self.calls.append((method, path, params))
        return {"task_id": "t1", "status": "queued"}

    def _write(self, method, path, *, label, body=None, params=None, **_kw):
        self.calls.append((method, path, params))
        return {"task_id": "t1", "status": "done"}


NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize("call", [
    lambda c: c.claim("t1", handler="h"),
    lambda c: c.update_task("t1", {"status": "queued"}),
    lambda c: c.report_done("t1", "ok"),
    lambda c: c.report_failed("t1", "bad"),
    lambda c: c.requeue_with_hold("claude_limit", "t1", start_at=NOW, detail="d"),
    lambda c: c.requeue_paused_task("t1", resume_session_id="s"),
    lambda c: c.hold_for_usage_limit("t1", start_at=NOW, detail="d"),
])
def test_every_task_patch_asks_for_the_slim_reply(call):
    c = _Client()
    call(c)
    patches = [x for x in c.calls if x[0] == "PATCH"]
    assert patches and all(p[2] == {"slim": "true"} for p in patches), patches


def test_update_task_can_ask_for_the_whole_task():
    c = _Client()
    c.update_task("t1", {"status": "queued"}, slim=False)
    assert c.calls[-1][2] is None
