"""Recovery of work a killed runner left claimed.

Claiming a task PATCHes it to ``in_progress``, which is what stops a second worker taking it.
That is correct until the worker dies: the row then says someone is running it and nobody is, and
the only thing that eventually moves it is a backend staleness sweeper -- hours later, and to
``failed``. For an autopilot quest a failed row is not mailable, so the day produces no output and
no error. These tests pin the runner-side close: it remembers what it was holding, and a fresh
process hands it back.
"""
from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner.poller import Poller
from quest_ai_runner.runner.state_store import StateStore

from .conftest import StubProvider, StubRetrieval


class FakeRecoveryClient:
    configured = True

    def __init__(self, statuses):
        self.statuses = dict(statuses)
        self.updates = []
        self.gets = []

    def get_task(self, task_id):
        self.gets.append(task_id)
        if task_id not in self.statuses:
            raise RuntimeError("no such task")
        return {"id": task_id, "status": self.statuses[task_id]}

    def update_task(self, task_id, fields):
        self.updates.append((task_id, fields))
        self.statuses[task_id] = fields.get("status", self.statuses.get(task_id))
        return {"id": task_id, **fields}


def _poller(client, state_path):
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({}), model_provider=StubProvider(decisions=[]),
    )
    return Poller(cfg, state_path=state_path, client=client)


def test_a_task_abandoned_in_progress_is_requeued(tmp_path):
    state_path = str(tmp_path / "state.json")
    # A runner claims a task, then the process dies before reporting anything.
    StateStore(state_path).claim_in_flight("atask_killed")

    client = FakeRecoveryClient({"atask_killed": "in_progress"})
    _poller(client, state_path)._recover_orphans()

    # Handed back to the queue so it runs again, rather than waiting hours to be failed.
    assert client.updates == [("atask_killed", {"status": "queued"})]


def test_a_task_that_actually_finished_is_left_alone(tmp_path):
    # The runner can die AFTER reporting but BEFORE releasing its record. Re-queueing a finished
    # task would re-run completed work (and, for an autopilot quest, mail a second copy).
    state_path = str(tmp_path / "state.json")
    StateStore(state_path).claim_in_flight("atask_done")

    client = FakeRecoveryClient({"atask_done": "done"})
    _poller(client, state_path)._recover_orphans()

    assert client.updates == []


def test_orphans_are_consumed_so_recovery_cannot_loop(tmp_path):
    state_path = str(tmp_path / "state.json")
    StateStore(state_path).claim_in_flight("atask_stuck")

    client = FakeRecoveryClient({"atask_stuck": "in_progress"})
    _poller(client, state_path)._recover_orphans()
    assert len(client.updates) == 1

    # A second process must not reconcile the same id again: an id that cannot be recovered would
    # otherwise be retried on every single restart, forever.
    client2 = FakeRecoveryClient({"atask_stuck": "in_progress"})
    _poller(client2, state_path)._recover_orphans()
    assert client2.updates == []


def test_recovery_survives_an_unreadable_task(tmp_path):
    # One bad id must never stop the others being reconciled, nor break the scan.
    state_path = str(tmp_path / "state.json")
    store = StateStore(state_path)
    store.claim_in_flight("atask_gone")
    store.claim_in_flight("atask_ok")

    client = FakeRecoveryClient({"atask_ok": "in_progress"})   # atask_gone raises
    _poller(client, state_path)._recover_orphans()

    assert client.updates == [("atask_ok", {"status": "queued"})]


def test_a_released_task_is_not_an_orphan(tmp_path):
    # The normal path: claim, finish, release. Nothing to recover on the next start.
    state_path = str(tmp_path / "state.json")
    store = StateStore(state_path)
    store.claim_in_flight("atask_normal")
    store.release_in_flight("atask_normal")

    client = FakeRecoveryClient({"atask_normal": "in_progress"})
    _poller(client, state_path)._recover_orphans()

    assert client.updates == []
    assert client.gets == []
