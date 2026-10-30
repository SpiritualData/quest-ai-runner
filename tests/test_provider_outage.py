"""A model-provider outage is a WAIT for the queued task, not a failure.

Before the hub's ``hubofhubs.models.outage``, a queued task that hit a provider that was overloaded or down
(a 503, a 529 "overloaded_error") ran out its few seconds of provider retries and was reported
failed ("failed after 1 attempt"), so the person had to run it again by hand. These tests pin the
replacement:
* (recognising an outage and the retry wrapper recording one are tested in the hub: ``tests/test_models.py``);
* the executor putting the task back with ``hold_reason=provider_unavailable`` and a retry time (not
  failed), for a raised outage and for an empty run;
* never pausing a run that succeeded, never blaming a concurrent run's outage, and reporting the task
  failed (never a plain requeue that would be claimed again at once) when the backend refuses.
Offline: no provider is called, no sleep is slept.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from hubofhubs.models import outage as provider_outage
from quest_ai_runner.runner import executor_pauses
from hubofhubs.ports.deep import DeepResult
from quest_ai_runner.core.orchestrator import OrchestratorResult

OVERLOADED = "Error code: 529 - overloaded_error: Overloaded"
UNAVAILABLE = "503 Service Unavailable: the model is temporarily unavailable"


class ServerError(Exception):
    """Named like the Gemini SDK's error so ``is_transient_error`` treats it as retryable."""


@pytest.fixture(autouse=True)
def fresh_note():
    provider_outage.reset_for_tests()
    yield
    provider_outage.reset_for_tests()


def test_resume_time_is_a_few_minutes_ahead():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    assert executor_pauses.next_try_at(now) == now + executor_pauses.RETRY_INTERVAL


# --- the executor ----------------------------------------------------------------------------------

class RecordingClient:
    def __init__(self, refuse_hold=False):
        self.calls = []
        self.refuse_hold = refuse_hold

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "requeue_for_provider_outage":
                return None if self.refuse_hold else {"task_id": args[0], "status": "queued"}
            if name == "get_task":
                return {"status": "in_progress"}
            return {}
        return record

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class FixedOrchestrator:
    def __init__(self, result=None, raises=None, records=None):
        self.result, self.raises, self.records = result, raises, records

    def run(self, *args, **kwargs):
        if self.records is not None:
            provider_outage.note(self.records)
        if self.raises is not None:
            raise self.raises
        return self.result


def execute(orch, client=None):
    from quest_ai_runner.runner.executor import TaskExecutor

    client = client or RecordingClient()
    outcome = TaskExecutor(client, orch).execute(
        {"task_id": "atask_9", "text": "draft the summary", "resume_session_id": "sess-1"})
    return outcome, client


def test_a_run_that_raises_on_an_outage_is_requeued_to_retry_not_failed():
    outcome, client = execute(FixedOrchestrator(raises=RuntimeError(OVERLOADED)))
    assert outcome.status == "waiting"
    (_, args, kwargs), = client.named("requeue_for_provider_outage")
    assert args == ("atask_9",)
    assert kwargs["start_at"] > datetime.now(timezone.utc) + timedelta(minutes=5)
    assert kwargs["resume_session_id"] == "sess-1"
    assert not client.named("report_failed")


def test_an_empty_run_after_an_outage_is_requeued_to_retry():
    result = OrchestratorResult(kind="answer", text="")
    outcome, client = execute(FixedOrchestrator(result, records=RuntimeError(UNAVAILABLE)))
    assert outcome.status == "waiting"
    assert client.named("requeue_for_provider_outage")


def test_an_unmet_deep_run_after_an_outage_is_requeued_to_retry():
    result = OrchestratorResult(kind="deep", deep_results=[DeepResult(met=False, error="no result")])
    outcome, client = execute(FixedOrchestrator(result, records=RuntimeError(OVERLOADED)))
    assert outcome.status == "waiting"
    assert client.named("requeue_for_provider_outage")


def test_a_run_that_succeeded_is_reported_even_if_an_outage_happened_meanwhile():
    result = OrchestratorResult(kind="deep", deep_results=[DeepResult(met=True, output="Done.")])
    outcome, client = execute(FixedOrchestrator(result, records=RuntimeError(OVERLOADED)))
    assert outcome.status == "done"
    assert not client.named("requeue_for_provider_outage")


def test_an_answer_with_text_is_reported_even_if_an_outage_happened_meanwhile():
    result = OrchestratorResult(kind="answer", text="Here is the summary.")
    outcome, client = execute(FixedOrchestrator(result, records=RuntimeError(OVERLOADED)))
    assert outcome.status == "done"
    assert not client.named("requeue_for_provider_outage")


def test_an_ordinary_failure_is_still_reported_failed():
    outcome, client = execute(FixedOrchestrator(raises=RuntimeError("KeyError: 'goal_id'")))
    assert outcome.status == "failed"
    assert not client.named("requeue_for_provider_outage")


def test_a_refused_hold_is_reported_failed_never_put_back_to_claim_at_once():
    outcome, client = execute(
        FixedOrchestrator(raises=RuntimeError(OVERLOADED)),
        client=RecordingClient(refuse_hold=True))
    assert outcome.status == "failed"
    assert client.named("report_failed")
    assert not client.named("update_task")
