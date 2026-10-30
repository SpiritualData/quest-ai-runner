"""A task whose run a person paused goes back to the queue holding its Claude session.

Before this, the stream-json worker ended a paused run as ``DeepResult(paused=True, cancelled=True)``,
the orchestrator turned that into ``kind="cancelled"``, and the executor read it as a cancel: it
posted "Stopped: this task was cancelled.", sent no PATCH, dropped the session id and left the task
in_progress with nothing to resume. These tests pin the replacement:

* a paused result is handed back with ``requeue_paused_task`` (queued plus the session), says plainly
  that it is paused, and is never reported done, failed, needs_you or incomplete, never verified,
  never posted into the chat, and never claims a message after the pause;
* a refused hand-back is reported failed instead of reading as success; a cancel stays a cancel;
* a message the run claimed but never confirmed goes back onto the task, and is quoted on the feed
  when even that write fails;
* the whole cycle against the scripted stream-json worker: pause, then resume the same session.

Offline: no real ``claude`` binary, no network.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from hubofhubs.ports.deep import DeepResult
from quest_ai_runner.core.goal_runner import SubprocessConfig
from quest_ai_runner.core.goal_runner_stream import StreamJsonGoalRunner
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig, OrchestratorResult
from quest_ai_runner.runner import executor as executor_module
from quest_ai_runner.runner.executor import TaskExecutor
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient

from .conftest import StubProvider, StubRetrieval
from .test_goal_runner_stream import make_claude, read_log
from .test_runner import MockQuestClient


class PauseClient(MockQuestClient):
    """A client that models the backend's pause contract and records what the executor did."""

    def __init__(self, *, accept_requeue: bool = True, accept_message: bool = True) -> None:
        super().__init__([])
        self.accept_requeue = accept_requeue
        self.accept_message = accept_message
        self.pause = False
        self.cancelled = False
        self.requeued: List[Dict[str, Any]] = []
        self.sent_messages: List[tuple] = []
        self.claim_calls = 0
        self.waiting: List[Dict[str, str]] = []     # messages steered in, not yet claimed

    def is_task_cancelled(self, task_id):
        return self.cancelled

    def is_task_pause_requested(self, task_id):
        return self.pause

    def claim_task_messages(self, task_id):
        self.claim_calls += 1
        out, self.waiting = self.waiting, []
        return out

    def requeue_paused_task(self, task_id, *, resume_session_id=None):
        if not self.accept_requeue:
            return None
        self.requeued.append({"task_id": task_id, "resume_session_id": resume_session_id})
        return {"task_id": task_id, "status": "queued"}

    def post_task_message(self, task_id, text):
        if not self.accept_message:
            raise QuestApiError("refused", status=409)
        self.sent_messages.append((task_id, text))
        return {"message_id": "m-back"}

    def progress_texts(self, kind: Optional[str] = None) -> List[str]:
        return [text for _, k, text, _ in self.progress if kind is None or k == kind]


class FixedOrchestrator:
    """Returns a scripted result and refuses to be asked anything else (no verifier, no planner)."""

    def __init__(self, result: OrchestratorResult, while_running=None) -> None:
        self.result = result
        self.while_running = while_running

    def run(self, *args, **kwargs):
        if self.while_running is not None:
            self.while_running()
        return self.result

    def __getattr__(self, name):
        # The executor probes its brain for optional attributes at construction; only the methods
        # that would judge or write up a result are off limits to a paused run.
        if name.startswith(("verify", "synthesize", "report_claims", "judge")):
            raise AssertionError(f"a paused run must not reach the brain's {name}")
        raise AttributeError(name)


def paused_result(session: str = "sess-9", unseen: Optional[List[str]] = None) -> OrchestratorResult:
    deep = DeepResult(met=False, session_id=session, cancelled=True, paused=True,
                      unseen_inputs=list(unseen or []),
                      error="The run was paused on request. Changes it had already finished are kept; "
                            "resume it to continue from the same session.")
    return OrchestratorResult(kind="cancelled", goals=["G"], deep_results=[deep],
                              exit_reason="cancelled")


def execute(client: PauseClient, result: OrchestratorResult, task: Optional[Dict[str, Any]] = None,
            while_running=None):
    return TaskExecutor(client, FixedOrchestrator(result, while_running)).execute(
        task or {"task_id": "atask_1", "text": "write the report", "conv_id": "conv_1"})


def test_a_paused_run_is_handed_back_queued_with_its_session():
    client = PauseClient()
    outcome = execute(client, paused_result("sess-9"))
    assert outcome.status == "paused"
    assert client.requeued == [{"task_id": "atask_1", "resume_session_id": "sess-9"}]


def test_a_paused_run_says_so_and_reports_nothing_else():
    client = PauseClient()
    outcome = execute(client, paused_result())
    assert client.reports == []                       # no done, failed, needs_you or incomplete
    assert [p for p in client.posts if p[2] != "started"] == []   # the chat hears nothing new
    paused_lines = [t for t in client.progress_texts("status") if t.startswith("Paused.")]
    assert len(paused_lines) == 1
    assert outcome.result == paused_lines[0]
    assert not any("cancel" in (t or "").lower() for t in client.progress_texts())


def test_a_message_steered_in_around_the_pause_is_left_unclaimed_for_the_resume():
    client = PauseClient()
    steered = {"message_id": "m2", "text": "also check the logs"}

    def person_types_while_it_stops():
        client.waiting = [steered]

    outcome = execute(client, paused_result(), while_running=person_types_while_it_stops)
    assert outcome.status == "paused"
    assert client.claim_calls == 1                    # only the opening drain, before the run
    assert client.waiting == [steered]                # still unclaimed: the resume's drain delivers it
    assert client.sent_messages == []                 # and nothing was re-posted in its place


def test_the_resumed_run_delivers_a_message_that_waited_through_the_pause():
    client = PauseClient()
    client.waiting = [{"message_id": "m2", "text": "also check the logs"}]
    seen: Dict[str, Any] = {}

    class Recording(FixedOrchestrator):
        def run(self, text, *args, **kwargs):
            seen["text"] = text
            seen["resume_session_id"] = kwargs.get("resume_session_id")
            return OrchestratorResult(kind="cancelled", exit_reason="cancelled")

    TaskExecutor(client, Recording(paused_result())).execute(
        {"task_id": "atask_1", "text": "write the report", "resume_session_id": "sess-9"})
    assert "also check the logs" in seen["text"]
    assert seen["resume_session_id"] == "sess-9"


def test_a_refused_hand_back_is_reported_failed_not_paused():
    client = PauseClient(accept_requeue=False)
    outcome = execute(client, paused_result())
    assert outcome.status == "failed"
    assert [r[1] for r in client.reports] == ["failed"]
    assert not any(t.startswith("Paused.") for t in client.progress_texts("status"))


def test_a_client_without_the_hand_back_is_reported_failed():
    class Old(PauseClient):
        requeue_paused_task = None

    client = Old()
    assert execute(client, paused_result()).status == "failed"
    assert [r[1] for r in client.reports] == ["failed"]


def test_a_cancel_is_still_a_cancel_and_is_not_requeued():
    client = PauseClient()
    stopped = OrchestratorResult(kind="cancelled", deep_results=[
        DeepResult(met=False, session_id="sess-9", cancelled=True)], exit_reason="cancelled")
    outcome = execute(client, stopped)
    assert outcome.status == "cancelled"
    assert client.requeued == [] and client.reports == []


def test_a_task_cancelled_while_it_was_pausing_stays_cancelled():
    client = PauseClient()
    client.cancelled = True
    outcome = execute(client, paused_result())
    assert outcome.status == "cancelled"
    assert client.requeued == [] and client.reports == []


def test_a_message_the_run_never_confirmed_goes_back_on_the_task():
    client = PauseClient()
    execute(client, paused_result(unseen=["skip step3", "skip step3", "and add a summary"]))
    assert client.sent_messages == [("atask_1", "skip step3"), ("atask_1", "and add a summary")]


def test_an_unconfirmed_message_that_cannot_be_returned_is_quoted_on_the_feed():
    client = PauseClient(accept_message=False)
    outcome = execute(client, paused_result(unseen=["skip step3"]))
    assert outcome.status == "paused"
    errors = client.progress_texts("error")
    assert len(errors) == 1 and "skip step3" in errors[0] and errors[0][0].isupper()


def test_the_session_falls_back_to_the_one_the_task_was_resumed_with():
    client = PauseClient()
    result = OrchestratorResult(kind="cancelled", deep_results=[
        DeepResult(met=False, cancelled=True, paused=True)], exit_reason="cancelled")
    execute(client, result, {"task_id": "atask_1", "text": "x", "resume_session_id": "older"})
    assert client.requeued[0]["resume_session_id"] == "older"


# --- the client's write ---------------------------------------------------------------------------

class WireClient(QuestClient):
    def __init__(self, reply: Any = None, error: Optional[Exception] = None) -> None:
        super().__init__(base_url="http://quest.invalid", api_key="k")
        self.reply, self.error, self.sent = reply, error, []

    def _request(self, method, path, *, params=None, body=None, timeout_override=None):
        self.sent.append((method, path, body))
        if self.error is not None:
            raise self.error
        return self.reply


def test_the_client_patches_queued_with_the_session_and_nothing_else():
    client = WireClient(reply={"task_id": "atask_1", "status": "queued"})
    assert client.requeue_paused_task("atask_1", resume_session_id=" sess-9 ")
    assert client.sent == [("PATCH", "/api/assistant-tasks/atask_1",
                            {"status": "queued", "resume_session_id": "sess-9"})]


def test_the_client_sends_no_session_key_when_it_has_none():
    client = WireClient(reply={"status": "queued"})
    client.requeue_paused_task("atask_1")
    assert client.sent[0][2] == {"status": "queued"}


def test_a_refused_hand_back_write_is_none_not_success():
    client = WireClient(error=QuestApiError("Quest API PATCH -> 409", status=409))
    assert client.requeue_paused_task("atask_1", resume_session_id="s") is None


def test_an_accepted_hand_back_with_an_empty_body_is_still_success():
    assert WireClient(reply=None).requeue_paused_task("atask_1", resume_session_id="s")


def test_posting_a_task_message_raises_when_refused():
    client = WireClient(error=QuestApiError("Quest API POST -> 404", status=404))
    with pytest.raises(QuestApiError):
        client.post_task_message("atask_1", "hello")


# --- the whole cycle against the scripted stream-json worker -------------------------------------

def deep_orchestrator(runner: StreamJsonGoalRunner) -> Orchestrator:
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": True, "reason": "done"},
    ])
    cfg = OrchestratorConfig(deep_goal_max_iterations=1)
    cfg.overseer = False
    brain = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                         deep_runner=runner, config=cfg)
    brain.stub_provider = provider
    return brain


def stream_runner(claude: str, workdir) -> StreamJsonGoalRunner:
    cfg = SubprocessConfig(working_dir=str(workdir), claude_path=claude, review_interval_seconds=0,
                           cancel_grace_seconds=0.5)
    runner = StreamJsonGoalRunner(cfg)
    runner.echo_wait_seconds = 0.4
    return runner


def test_pause_then_resume_continues_the_same_claude_session(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "RUN_CONTROL_POLL_SECONDS", 0.1)
    first = make_claude(tmp_path, {"steps": [{"name": "a", "seconds": 0.1},
                                             {"name": "b", "seconds": 30}]}, "claude")
    client = PauseClient()
    brain = deep_orchestrator(stream_runner(first, tmp_path))

    threading.Timer(1.0, lambda: setattr(client, "pause", True)).start()
    started = time.monotonic()
    outcome = TaskExecutor(client, brain).execute({"task_id": "atask_5", "text": "do the steps"})
    assert time.monotonic() - started < 15
    assert outcome.status == "paused"
    (requeued,) = client.requeued
    session = requeued["resume_session_id"]
    assert session
    assert client.reports == []
    assert brain.stub_provider.plan_calls == 1        # planned once; no verifier call after the pause

    # The resume: the backend cleared the flag and the claim hands the same session back.
    client.pause = False
    second = make_claude(tmp_path, {"steps": [{"name": "c", "seconds": 0.1}]}, "claude2")
    resumed_brain = deep_orchestrator(stream_runner(second, tmp_path))
    outcome = TaskExecutor(client, resumed_brain).execute(
        {"task_id": "atask_5", "text": "do the steps", "resume_session_id": session})
    assert outcome.status == "done"
    argv = [e for e in read_log(tmp_path, "claude2") if e["event"] == "argv"][0]["argv"]
    assert "--resume" in argv and session in argv      # the SAME session, on the same thread
    saved = json.loads((tmp_path / "sessions" / f"{session}.json").read_text())
    assert saved["ran"][0] == "a" and "b" not in saved["ran"]   # a was kept, b never ran
