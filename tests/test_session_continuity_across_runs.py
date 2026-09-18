"""One thread, many runs: a deep session survives the run that opened it.

A Claude session used to exist only inside one call: the deep runner opened it, the goal loop could
continue it while that call lasted, and the id was thrown away when the call returned. So a task
that a person replied to tomorrow, or a recurring brief that ran again the next day, started from
nothing and paid again for everything the last run had already read and decided. It could not say
what had changed since, because it had no memory of what it said.

Two additive halves, both optional on the wire (see ``user_stories/one_thread_many_runs.md``):

  1. OUTBOUND: a finished run reports the session it leaves behind, with whatever terminal status it
     lands on, so a backend can hand it to the next run on the same thread.
  2. INBOUND: a task carrying ``resume_session_id`` opens its first deep attempt with ``--resume``,
     through the SAME plumbing the within-run turn-budget continuation uses.

Backward compatibility is the third thing under test, in both directions: a lane running this code
against a backend that never heard of either field behaves exactly as it did before, and a client or
deep runner that cannot carry a session is simply never asked to.

Fully offline: stub provider/retrieval, capturing deep runners, an intercepted ``subprocess.Popen``.
"""
from __future__ import annotations

import json
import subprocess as _sp
from pathlib import Path
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.goal_runner import (SubprocessConfig, SubprocessGoalRunner,
                                              resume_target_missing)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.runner.executor import TaskExecutor, terminal_session_id
from quest_ai_runner.runner.quest_client import QuestClient

from .conftest import StubDeepRunner, StubProvider, StubRetrieval
from .test_runner import MockQuestClient


# --- doubles ------------------------------------------------------------------------------------

class SessionAwareClient(MockQuestClient):
    """A client that knows about session continuity: it records the session id of every report."""

    def __init__(self):
        super().__init__([])
        self.report_sessions: List[Optional[str]] = []

    def report_done(self, task_id, result, session_id=None):
        self.report_sessions.append(session_id)
        self.reports.append((task_id, "done", result, None))

    def report_needs_you(self, task_id, result, decision_id, session_id=None):
        self.report_sessions.append(session_id)
        self.reports.append((task_id, "needs_you", result, decision_id))

    def report_failed(self, task_id, result, session_id=None):
        self.report_sessions.append(session_id)
        self.reports.append((task_id, "failed", result, None))


class ResumeCapturingRunner:
    """A DeepRunner that accepts a resume id and records what each call was handed."""

    def __init__(self, met: bool = True, output: str = "deep done",
                 session_id: Optional[str] = "sess-new"):
        self._met = met
        self._output = output
        self._session_id = session_id
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None,
                 working_dir=None, resume_session_id=None) -> DeepResult:
        self.calls.append({"goal": goal, "resume_session_id": resume_session_id})
        return DeepResult(met=self._met, output=self._output, session_id=self._session_id)


def deep_brain(deep_runner, decisions=None):
    provider = StubProvider(decisions=decisions or [
        {"action": "deep", "goal": "do the work", "deep_brief": "x", "rationale": "work"},
        {"met": True, "reason": "did it"},
    ])
    return Orchestrator(retrieval=StubRetrieval({"README.md": "fact"}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=deep_runner)


# --- 1. outbound: the run reports the session it leaves behind -----------------------------------

def test_a_finished_deep_run_reports_its_session_id():
    client = SessionAwareClient()
    ex = TaskExecutor(client, deep_brain(ResumeCapturingRunner(session_id="sess-today")))

    ex.execute({"id": "t1", "text": "do the work"})

    assert client.reports[0][1] == "done"
    assert client.report_sessions == ["sess-today"], (
        "the session this run opened was not reported, so tomorrow's run cannot resume it"
    )


def test_a_failed_run_reports_its_session_too():
    """A run that fell short is exactly the one a person replies to, and that reply should pick up
    the session holding the half-finished work rather than start cold."""
    client = SessionAwareClient()
    runner = ResumeCapturingRunner(met=False, output="half done", session_id="sess-partial")
    brain = deep_brain(runner, decisions=[
        {"action": "deep", "goal": "do the work", "deep_brief": "x", "rationale": "work"},
        {"met": False, "reason": "not finished"},
        {"met": False, "reason": "not finished"},
    ])
    ex = TaskExecutor(client, brain)

    ex.execute({"id": "t2", "text": "do the work"})

    assert client.reports[-1][1] == "failed"
    assert client.report_sessions[-1] == "sess-partial"


def test_a_run_with_no_session_reports_none_rather_than_an_empty_string():
    client = SessionAwareClient()
    ex = TaskExecutor(client, deep_brain(ResumeCapturingRunner(session_id=None)))

    ex.execute({"id": "t3", "text": "do the work"})

    assert client.report_sessions == [None]


def test_a_plain_answer_turn_carries_no_session():
    client = SessionAwareClient()
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    ex = TaskExecutor(client, Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                                           registry=ModelRegistry(provider)))

    ex.execute({"id": "t4", "text": "say hi"})

    assert client.report_sessions == [None]


def test_the_last_session_is_the_threads_session_when_a_run_fanned_out():
    """Several subgoals mean several live sessions and the thread can continue only one. The last
    is chosen because it is the one whose transcript ends nearest to where a follow-up picks up."""
    class Result:
        def __init__(self, deep_results):
            self.deep_results = deep_results

    picked = terminal_session_id(Result([
        DeepResult(met=True, output="a", session_id="sess-first"),
        DeepResult(met=True, output="b", session_id=""),          # a subgoal that opened none
        DeepResult(met=True, output="c", session_id="sess-last"),
    ]))
    assert picked == "sess-last"


def test_a_client_that_never_heard_of_sessions_is_called_exactly_as_before():
    """BACK-COMPAT, outbound. ``MockQuestClient.report_done`` has the pre-continuity signature
    ``(task_id, result)``. Passing a session id to it would be a TypeError, which ``_safe`` would
    swallow into a task that silently never reports at all."""
    client = MockQuestClient([])
    ex = TaskExecutor(client, deep_brain(ResumeCapturingRunner(session_id="sess-today")))

    out = ex.execute({"id": "t5", "text": "do the work"})

    assert out.status == "done"
    assert client.reports and client.reports[0][1] == "done", (
        "the terminal report never landed: the old client was called with an argument it does "
        "not accept"
    )


def test_the_wire_body_carries_the_session_only_when_there_is_one():
    """BACK-COMPAT, outbound, at the wire. A backend that knows nothing about ``session_id``
    receives byte-for-byte the body it received before."""
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    bodies: List[Dict[str, Any]] = []
    client._request = lambda m, p, *, params=None, body=None: (bodies.append(body) or {})

    client.report_done("t1", "all done")
    client.report_done("t2", "all done", session_id="sess-1")
    client.report_failed("t3", "fell short", session_id="   ")
    client.report_needs_you("t4", "your call", "dec_1", session_id="sess-2")

    assert bodies[0] == {"status": "done", "result": "all done"}
    assert bodies[1]["session_id"] == "sess-1"
    assert "session_id" not in bodies[2], "a blank id must send nothing, not an empty string"
    assert bodies[3]["session_id"] == "sess-2"


# --- 2. inbound: a task's resume_session_id opens the first attempt -------------------------------

def test_a_task_carrying_a_resume_session_id_resumes_it_on_the_first_attempt():
    runner = ResumeCapturingRunner()
    ex = TaskExecutor(SessionAwareClient(), deep_brain(runner))

    ex.execute({"id": "t6", "text": "carry on", "resume_session_id": "sess-yesterday"})

    assert runner.calls and runner.calls[0]["resume_session_id"] == "sess-yesterday"


def test_a_task_without_one_starts_cold_exactly_as_before():
    runner = ResumeCapturingRunner()
    ex = TaskExecutor(SessionAwareClient(), deep_brain(runner))

    ex.execute({"id": "t7", "text": "do the work"})

    assert runner.calls and runner.calls[0]["resume_session_id"] is None


def test_an_empty_resume_session_id_is_not_a_session():
    runner = ResumeCapturingRunner()
    ex = TaskExecutor(SessionAwareClient(), deep_brain(runner))

    ex.execute({"id": "t8", "text": "do the work", "resume_session_id": "  "})

    assert runner.calls and runner.calls[0]["resume_session_id"] is None


def test_a_deep_runner_that_cannot_resume_simply_does_not_resume():
    """BACK-COMPAT, inbound. The conftest stub's ``run_goal`` has no ``resume_session_id``
    parameter, so the capability gate must never forward it. The run still happens."""
    runner = StubDeepRunner(met=True, output="ok")
    ex = TaskExecutor(SessionAwareClient(), deep_brain(runner))

    out = ex.execute({"id": "t9", "text": "carry on", "resume_session_id": "sess-yesterday"})

    assert out.status == "done"


def test_a_fanned_out_run_does_not_point_several_workers_at_one_session():
    runner = ResumeCapturingRunner()
    brain = deep_brain(runner, decisions=[
        {"action": "deep", "goal": "do the work",
         "deep_subtasks": [{"goal": "part one", "brief": "one"},
                           {"goal": "part two", "brief": "two"}],
         "rationale": "work"},
        {"met": True, "reason": "did it"},
        {"met": True, "reason": "did it"},
    ])
    ex = TaskExecutor(SessionAwareClient(), brain)

    ex.execute({"id": "t10", "text": "carry on", "resume_session_id": "sess-yesterday"})

    assert len(runner.calls) == 2
    assert [c["resume_session_id"] for c in runner.calls] == [None, None]


# --- 3. resume is best effort: a session the worker no longer holds --------------------------------

def envelope(result: str, *, subtype: str = "success", is_error: bool = False) -> bytes:
    return json.dumps({
        "type": "result", "subtype": subtype, "is_error": is_error, "result": result,
        "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01,
    }).encode()


# Measured against the real binary (2026-09-18): exit 1, empty stdout, this exact line on stderr.
SESSION_GONE_STDERR = b"No conversation found with session ID: sess-gone\n"


def test_resume_target_missing_is_narrow():
    assert resume_target_missing(1, "", "No conversation found with session ID: x") is True
    # A run that resumed fine and then failed is never mistaken for a missing session.
    assert resume_target_missing(1, "did half the work", "No conversation found") is False
    assert resume_target_missing(0, "", "No conversation found") is False
    assert resume_target_missing(1, "", "the model refused") is False
    assert resume_target_missing(1, "", None) is False


def test_a_resume_of_a_session_the_worker_no_longer_holds_runs_cold_and_completes(monkeypatch,
                                                                                  tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    cmds: List[List[str]] = []

    class Proc:
        stdin = None

        def __init__(self, resuming: bool):
            self.returncode = 1 if resuming else 0
            self._resuming = resuming

        def communicate(self, input=None, timeout=None):
            if self._resuming:
                return (b"", SESSION_GONE_STDERR)
            return (envelope("wrote the thing"), b"")

    def fake_popen(cmd, **kw):
        cmds.append(list(cmd))
        return Proc("--resume" in cmd)

    monkeypatch.setattr(_sp, "Popen", fake_popen)
    runner = SubprocessGoalRunner(
        SubprocessConfig(working_dir=str(tmp_path), claude_path="/usr/bin/claude"))

    res = runner.run_goal(goal="ship it", brief="carry on", max_turns=5,
                          resume_session_id="sess-gone")

    assert res.met is True, "a missing session must degrade to a cold start, never fail the run"
    assert res.output == "wrote the thing"
    assert len(cmds) == 2, "exactly one cold retry, and the retry can never come back here"
    assert "--resume" in cmds[0]
    assert "--resume" not in cmds[1] and "--session-id" in cmds[1]
    # The cold run's own session is what gets reported onward, so the thread continues from here.
    assert res.session_id == cmds[1][cmds[1].index("--session-id") + 1]


def test_a_resumed_run_that_fails_for_any_other_reason_is_not_retried_cold(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    cmds: List[List[str]] = []

    class Proc:
        stdin = None
        returncode = 1

        def communicate(self, input=None, timeout=None):
            return (envelope("got partway", subtype="error_max_turns", is_error=True), b"")

    def fake_popen(cmd, **kw):
        cmds.append(list(cmd))
        return Proc()

    monkeypatch.setattr(_sp, "Popen", fake_popen)
    runner = SubprocessGoalRunner(
        SubprocessConfig(working_dir=str(tmp_path), claude_path="/usr/bin/claude"))

    res = runner.run_goal(goal="ship it", brief="carry on", max_turns=5,
                          resume_session_id="sess-live")

    assert len(cmds) == 1
    assert res.limit_hit is True and res.session_id == "sess-live"
