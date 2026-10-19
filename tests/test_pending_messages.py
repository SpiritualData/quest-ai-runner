"""Mid-task steering messages -- the executor side (task #2501, quest-ai-runner half).

Covers:
  * The ONE initial drain in ``TaskExecutor.execute``: a message claimed BEFORE the run starts
    (sent while the task sat queued) is folded into the FIRST prompt the orchestrator sees, and a
    short ``status`` progress tick names the fold.
  * ``TaskExecutor._build_pending_inputs``: the throttled callable passed EXPLICITLY into
    ``Orchestrator.run(pending_inputs=...)`` -- it returns claimed message texts, is throttled the
    same way ``_build_cancel_check`` is, and posts a ``status`` tick when it hands messages over.

No network: ``MessageClaimingClient`` (a ``MockQuestClient`` from test_runner.py, plus an
in-memory ``claim_task_messages``) and the real ``Orchestrator``/``TaskExecutor`` with a
``StubProvider``. Honest limitation this exercises but does not hide: these tests prove the
FOLD-IN mechanics, not real subprocess latency -- a deep run's ``claude -p`` subprocess still
cannot be re-prompted mid-flight; a message that arrives during one is only visible at the next
attempt/verification boundary (see ``core/orchestrator.py`` deep retry loop, :5403).
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.runner.executor import TaskExecutor

from .conftest import StubProvider, StubRetrieval
from .test_runner import MockQuestClient


def _brain(provider, **kw):
    return Orchestrator(retrieval=StubRetrieval({"README.md": "fact: yes"}),
                        provider=provider, registry=ModelRegistry(provider), **kw)


class MessageClaimingClient(MockQuestClient):
    """``MockQuestClient`` + an in-memory FIFO ``claim_task_messages`` per task_id.

    Mirrors the real ``QuestClient.claim_task_messages`` contract: each call returns and CONSUMES
    every currently queued message for that task (a stand-in for the backend's ``delivered_at``
    stamping), so an immediate second call sees nothing new. ``claim_calls`` records every task_id
    a claim was attempted for, so a test can assert throttling.
    """

    def __init__(self, due_tasks: Optional[list] = None):
        super().__init__(due_tasks or [])
        self._queued: Dict[str, List[Dict[str, Any]]] = {}
        self.claim_calls: List[str] = []

    def queue_message(self, task_id: str, text: str, *, message_id: str = "amsg_test") -> None:
        self._queued.setdefault(task_id, []).append({
            "message_id": message_id, "text": text, "author_user_id": "u1",
            "at": "2026-07-24T00:00:00Z", "delivered_at": None,
        })

    def claim_task_messages(self, task_id: str) -> List[Dict[str, Any]]:
        self.claim_calls.append(task_id)
        msgs = self._queued.get(task_id, [])
        self._queued[task_id] = []
        return msgs


# --- (a) the initial drain folds a claimed message into the FIRST prompt -------------------

def test_initial_drain_folds_claimed_message_into_first_prompt():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MessageClaimingClient()
    client.queue_message("t1", "also check the staging env")
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "t1", "text": "run the checks"})

    assert out.status == "done"
    assert client.claim_calls.count("t1") >= 1
    assert "also check the staging env" in provider.plan_prompts[0]
    # Same rendering the orchestrator's own mid-run _drain_pending uses -- one voice regardless
    # of which drain point picked the message up.
    assert "NEW MESSAGES FROM THE USER SINCE YOU STARTED" in provider.plan_prompts[0]


def test_initial_drain_posts_a_status_tick_naming_the_fold():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MessageClaimingClient()
    client.queue_message("t2", "one more thing")
    ex = TaskExecutor(client, _brain(provider))
    ex.execute({"id": "t2", "text": "do it"})

    status_texts = [t for (_tid, k, t, _o) in client.progress if k == "status"]
    assert any("Picked up your message" in (t or "") for t in status_texts)
    # The "started" tick still carries the ORIGINAL text, unfolded -- the fold is a separate tick.
    started_texts = [t for (_tid, k, t, _o) in client.progress if k == "started"]
    assert started_texts == ["Started working on this: do it"]


def test_initial_drain_no_op_when_nothing_queued():
    """No queued message -> no fold, no extra status tick, byte-identical to before this feature."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MessageClaimingClient()
    ex = TaskExecutor(client, _brain(provider))
    ex.execute({"id": "t3", "text": "plain task, nothing queued"})

    assert "NEW MESSAGES FROM THE USER SINCE YOU STARTED" not in provider.plan_prompts[0]
    status_texts = [t for (_tid, k, t, _o) in client.progress if k == "status"]
    assert not any("Picked up" in (t or "") for t in status_texts)


def test_initial_drain_no_op_when_client_lacks_claim_task_messages():
    """An older client without ``claim_task_messages`` must leave a normal run unaffected."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])  # the plain mock, no claim_task_messages at all
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "t4", "text": "say hi"})
    assert out.status == "done"


# --- (b) the pending_inputs callable: claimed texts, throttled -----------------------------

def test_build_pending_inputs_returns_claimed_message_texts():
    client = MessageClaimingClient()
    client.queue_message("t5", "first message")
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    poll = ex._build_pending_inputs("t5", interval=0.0)
    assert poll() == ["first message"]


def test_build_pending_inputs_throttles_repeat_calls():
    """A second immediate call must NOT re-hit the client (mirrors _build_cancel_check's 15s
    throttle): a message queued inside the throttle window is delayed, never lost."""
    client = MessageClaimingClient()
    client.queue_message("t6", "first message")
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    poll = ex._build_pending_inputs("t6")  # default interval

    first = poll()
    assert first == ["first message"]

    client.queue_message("t6", "second message (should not show up yet)")
    second = poll()
    assert second == []
    assert client.claim_calls.count("t6") == 1


def test_build_pending_inputs_rechecks_after_interval_elapses():
    """With interval=0 every call is past the (zero-length) window, proving the throttle is
    TIME-based, not a one-shot cache."""
    client = MessageClaimingClient()
    client.queue_message("t7", "m1")
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    poll = ex._build_pending_inputs("t7", interval=0.0)

    assert poll() == ["m1"]
    client.queue_message("t7", "m2")
    assert poll() == ["m2"]
    assert client.claim_calls.count("t7") == 2


def test_build_pending_inputs_does_not_repeat_the_backends_pickup_line():
    """The backend's claim already writes "Picked up your message." (a ``message_ack`` tick) to
    the task feed. The runner posting it again as a status tick showed it twice."""
    client = MessageClaimingClient()
    client.queue_message("t8", "steer this")
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    poll = ex._build_pending_inputs("t8", interval=0.0)
    assert poll() == ["steer this"]

    assert not any("Picked up" in (t or "") for (_tid, _k, t, _o) in client.progress)


def test_build_pending_inputs_no_status_tick_when_nothing_claimed():
    client = MessageClaimingClient()
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    poll = ex._build_pending_inputs("t9", interval=0.0)
    assert poll() == []
    assert [k for (_tid, k, _t, _o) in client.progress if k == "status"] == []


def test_build_pending_inputs_never_raises_when_client_lacks_claim_task_messages():
    client = MockQuestClient([])  # no claim_task_messages
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    poll = ex._build_pending_inputs("t10", interval=0.0)
    assert poll() == []


def test_pending_inputs_is_passed_explicitly_into_orchestrator_run(monkeypatch):
    """``execute`` must pass ``pending_inputs=`` explicitly into ``Orchestrator.run`` (not rely on
    auto-wiring), since a goal-only task carries no quest_id/conv identity for run()'s own
    ``_conv_key`` resolution to find."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MessageClaimingClient()
    orch = _brain(provider)
    ex = TaskExecutor(client, orch)

    captured = {}
    real_run = orch.run

    def capturing_run(*args, **kwargs):
        captured["pending_inputs"] = kwargs.get("pending_inputs")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(orch, "run", capturing_run)
    ex.execute({"id": "t11", "text": "goal-only task, no quest_id"})
    assert callable(captured["pending_inputs"])


# --- (c) a message that arrives AFTER the run's last drain point starts a new run ----------

class LateMessageClient(MessageClaimingClient):
    """A steer that lands in the window between the run's last drain and the terminal report.

    Queues ``late_text`` for the task at the moment the terminal "done" write happens, which is
    exactly when a message from the person could land on a task still shown as in progress. Also
    records every ``open_run_on_task`` call, the way the real client would open a run on the thread.
    """

    def __init__(self, late_text: str, *, fail_open: bool = False):
        super().__init__([])
        self.late_text = late_text
        self.fail_open = fail_open
        self.opened_runs: List[tuple] = []

    def report_done(self, task_id, result, *args, **kwargs):
        super().report_done(task_id, result)
        self.queue_message(task_id, self.late_text, message_id="amsg_late")

    def report_done_with_data(self, task_id, result, result_data=None):
        out = super().report_done_with_data(task_id, result, result_data)
        self.queue_message(task_id, self.late_text, message_id="amsg_late")
        return out

    def open_run_on_task(self, task_id, text):
        if self.fail_open:
            raise RuntimeError("backend refused the run")
        self.opened_runs.append((task_id, text))
        return {"id": task_id, "status": "queued"}


def test_late_message_after_done_opens_a_second_run_with_its_text():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = LateMessageClient("also send the summary to the team")
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "late1", "text": "write the report"})

    assert out.status == "done"
    assert client.opened_runs == [("late1", "also send the summary to the team")]
    status_texts = [t for (_tid, k, t, _o) in client.progress if k == "status"]
    assert any("started a new run" in (t or "") for t in status_texts)


def test_late_message_is_claimed_once_so_it_is_not_folded_twice():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = LateMessageClient("one late note")
    ex = TaskExecutor(client, _brain(provider))
    ex.execute({"id": "late2", "text": "do it"})

    # The late note was claimed by the post-report step; a second claim finds nothing left.
    assert client.claim_task_messages("late2") == []
    assert client.opened_runs == [("late2", "one late note")]


def test_no_late_message_opens_no_run():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MessageClaimingClient()
    ex = TaskExecutor(client, _brain(provider))
    ex.execute({"id": "late3", "text": "plain task"})

    assert not hasattr(client, "opened_runs")


def test_failed_open_is_reported_on_the_task_and_the_outcome_still_stands():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = LateMessageClient("please also check X", fail_open=True)
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "late4", "text": "do it"})

    # The run's own result is untouched; the failure to start a follow-up is said out loud.
    assert out.status == "done"
    error_texts = [t for (_tid, k, t, _o) in client.progress if k == "error"]
    assert any("could not start a new run" in (t or "") for t in error_texts)


# --- (d) "seen" is reported when the text reaches a model turn, not when it is claimed -------------

class SeenRecordingClient(MessageClaimingClient):
    def __init__(self):
        super().__init__([])
        self.seen_calls: List[tuple] = []

    def mark_task_messages_seen(self, task_id, message_ids, where="planner"):
        self.seen_calls.append((task_id, list(message_ids), where))
        return []


def test_a_message_folded_into_the_first_prompt_is_reported_seen_by_the_planner():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = SeenRecordingClient()
    client.queue_message("s1", "look at staging", message_id="amsg_a")
    TaskExecutor(client, _brain(provider)).execute({"id": "s1", "text": "run the checks"})

    assert ("s1", ["amsg_a"], "planner") in client.seen_calls


def test_a_message_handed_over_mid_run_is_reported_seen_for_the_next_step():
    client = SeenRecordingClient()
    client.queue_message("s2", "steer", message_id="amsg_b")
    poll = TaskExecutor(client, _brain(StubProvider(decisions=[])))._build_pending_inputs(
        "s2", interval=0.0)
    assert poll() == ["steer"]

    assert client.seen_calls == [("s2", ["amsg_b"], "next_step")]


def test_nothing_is_reported_seen_when_nothing_was_claimed():
    client = SeenRecordingClient()
    poll = TaskExecutor(client, _brain(StubProvider(decisions=[])))._build_pending_inputs(
        "s3", interval=0.0)
    assert poll() == []
    assert client.seen_calls == []
