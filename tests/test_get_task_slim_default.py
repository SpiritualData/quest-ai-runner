"""A bare get_task(id) is the control-fields read: an ad-hoc status loop over ten tasks every
30 s pulled 1.3 GB of whole tasks on 2026-11-19. A caller that needs the brief or the result
asks for it with slim=False."""
from quest_ai_runner.body.internal_context.quest.client.quest_client_tasks import TasksMixin


class _Client(TasksMixin):
    def __init__(self):
        self.calls = []

    def _request(self, method, path, params=None, **_kw):
        self.calls.append((method, path, params))
        return {"status": "done"}


def test_bare_get_task_is_slim():
    c = _Client()
    c.get_task("atask_1")
    assert c.calls[-1][2] == {"slim": "true"}


def test_full_read_is_explicit():
    c = _Client()
    c.get_task("atask_1", slim=False)
    assert c.calls[-1][2] is None
