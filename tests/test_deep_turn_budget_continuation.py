"""A deep run that runs out of TURNS is continued, not started over.

The live failure this covers: a task on the SD lane ran a Claude Code deep worker, used its whole
turn budget mid-flight, and was reported as a flat failure. Its real work (two finished modules,
uncommitted) was on disk, its session held everything it had read and decided, and the goal loop's
answer was to run the SAME goal again from a cold start with the SAME budget. So every attempt
paid again for the discovery the last one had already done, stopped in the same place, and the
person was told only "the task failed" with no account of the work.

Three things are pinned here, in the order a run meets them:

  1. ``SubprocessGoalRunner`` reports HOW it ended (``limit_hit``) and WHICH session it used, and
     resumes that session when asked (``--resume``, no second ``--session-id``).
  2. The goal loop, seeing ``limit_hit``, continues the same session with a LARGER budget and a
     short continuation brief, instead of re-running the goal cold. A runner that cannot resume
     keeps exactly the old behaviour.
  3. A run that still falls short reports WHAT IT DID alongside the error, so unfinished work is
     findable instead of thrown away.

Fully offline: no binary is spawned, no network, no session monitor.
"""
from __future__ import annotations

import json
import subprocess as _sp
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider


# --- 1. the runner ------------------------------------------------------------------------------

def envelope(subtype: str, result: str = "", *, is_error: bool = True) -> bytes:
    return json.dumps({
        "type": "result", "subtype": subtype, "is_error": is_error, "result": result,
        "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01,
    }).encode()


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Intercept the spawn; return the list every launched command lands in."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    cmds: List[List[str]] = []

    class MockPopen:
        stdin = None
        returncode = 1

        def communicate(self, input=None, timeout=None):
            return (envelope("error_max_turns", "Wired the engine; tests not written yet."), b"")

    def fake_popen(cmd, **kw):
        cmds.append(list(cmd))
        return MockPopen()

    monkeypatch.setattr(_sp, "Popen", fake_popen)
    return cmds


def _runner(tmp_path) -> SubprocessGoalRunner:
    return SubprocessGoalRunner(
        SubprocessConfig(working_dir=str(tmp_path), claude_path="/usr/bin/claude"))


def test_turn_exhaustion_reports_limit_hit_and_the_session_to_continue(spawned, tmp_path):
    res = _runner(tmp_path).run_goal(goal="ship it", brief="do it", max_turns=3)

    assert res.met is False
    assert res.limit_hit is True, "the envelope said error_max_turns; that is not a guess"
    # The session id is the one the worker was launched with, so a continuation can resume it.
    launched = spawned[0][spawned[0].index("--session-id") + 1]
    assert res.session_id == launched
    assert "Wired the engine" in (res.output or "")


def test_a_crash_is_not_a_turn_limit(spawned, monkeypatch, tmp_path):
    """Only the worker's own ``error_max_turns`` sets ``limit_hit``. Resuming a crashed session
    would be guessing, and the two cases call for opposite responses."""
    class Crashed:
        stdin = None
        returncode = 1

        def communicate(self, input=None, timeout=None):
            return (envelope("error_during_execution", "partial"), b"")

    monkeypatch.setattr(_sp, "Popen", lambda cmd, **kw: Crashed())
    res = _runner(tmp_path).run_goal(goal="ship it", brief="do it", max_turns=3)

    assert res.met is False
    assert res.limit_hit is False


def test_a_met_run_carries_its_session_id_too(monkeypatch, tmp_path):
    class Ok:
        stdin = None
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return (envelope("success", "done", is_error=False), b"")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    monkeypatch.setattr(_sp, "Popen", lambda cmd, **kw: Ok())
    res = _runner(tmp_path).run_goal(goal="ship it", brief="do it", max_turns=3)

    assert res.met is True
    assert res.session_id


def test_resuming_reuses_the_session_instead_of_opening_a_new_one(spawned, tmp_path):
    _runner(tmp_path).run_goal(goal="ship it", brief="continue", max_turns=6,
                               resume_session_id="sess-abc")
    cmd = spawned[0]

    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "sess-abc"
    assert "--session-id" not in cmd, "a resumed run must not also open a fresh session"
    assert cmd[cmd.index("--max-turns") + 1] == "6"


def test_without_a_resume_id_a_fresh_session_is_opened_exactly_as_before(spawned, tmp_path):
    _runner(tmp_path).run_goal(goal="ship it", brief="do it", max_turns=3)
    cmd = spawned[0]

    assert "--session-id" in cmd
    assert "--resume" not in cmd


# --- 2. the goal loop ---------------------------------------------------------------------------

class ResumableRunner:
    """A DeepRunner that can be continued, recording every call including the resume id."""

    def __init__(self, results: List[DeepResult]):
        self._results = list(results)
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None,
                 resume_session_id: Optional[str] = None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "model": model, "max_turns": max_turns,
                           "resume_session_id": resume_session_id})
        if self._results:
            return self._results.pop(0)
        return DeepResult(met=True, output="finished")


class OldRunner:
    """A DeepRunner with the pre-continuation signature: it cannot resume anything."""

    def __init__(self, results: List[DeepResult]):
        self._results = list(results)
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "max_turns": max_turns})
        if self._results:
            return self._results.pop(0)
        return DeepResult(met=True, output="finished")


PLAN = {"action": "deep", "goal": "Do the work",
        "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
        "rationale": "deep"}


def _orch(provider, runner, **cfg):
    cfg.setdefault("deep_goal_max_iterations", 3)
    return Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        deep_runner=runner,
        config=OrchestratorConfig(deep_max_turns=30, **cfg),
    )


def test_a_turn_exhausted_attempt_is_continued_in_its_own_session_with_more_room():
    provider = ScriptedProvider(plans=[PLAN],
                                verdicts=[{"met": False, "reason": "tests are missing",
                                           "next_action": "write the tests"},
                                          {"met": True}])
    runner = ResumableRunner([
        DeepResult(met=False, output="engine wired", limit_hit=True, session_id="sess-1"),
        DeepResult(met=True, output="engine wired and tested"),
    ])

    res = _orch(provider, runner).run("build feature X")

    assert res.kind == "deep"
    assert len(runner.calls) == 2
    first, second = runner.calls
    assert first["resume_session_id"] is None and first["max_turns"] == 30
    # The continuation picks up THAT session, with a budget grown so it is not cut off again in
    # the same place.
    assert second["resume_session_id"] == "sess-1"
    assert second["max_turns"] == 60
    # ...and it is told it was cut off, not that it failed, plus what is still owed.
    assert "YOU RAN OUT OF TURNS" in second["brief"]
    assert "write the tests" in second["brief"]
    # A continuation must NOT re-send the cold-start augmentation: the worker already holds all of
    # this, and re-reading it is exactly what burned the budget the first time.
    assert "PREVIOUS ATTEMPT DID NOT YET MEET THE GOAL" not in second["brief"]


def test_the_model_tier_is_not_escalated_by_running_out_of_turns():
    """Running out of room says nothing about the model being too weak, and a continuation runs in
    the worker's existing session. Escalating here would just make the retry cost more."""
    provider = ScriptedProvider(plans=[PLAN],
                                verdicts=[{"met": False, "reason": "not finished"}, {"met": True}])
    runner = ResumableRunner([
        DeepResult(met=False, output="half done", limit_hit=True, session_id="sess-1"),
        DeepResult(met=True, output="done"),
    ])

    _orch(provider, runner, deep_model_ladder=["haiku", "sonnet", "opus"]).run("build X")

    assert [c["model"] for c in runner.calls] == ["haiku", "haiku"]


def test_a_verified_goal_is_not_continued_just_because_turns_ran_out():
    """A worker can finish the work and then run out of turns while writing up. The verifier is
    still the authority: met is met, and no second run is spawned."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = ResumableRunner([
        DeepResult(met=False, output="all the work, then cut off", limit_hit=True,
                   session_id="sess-1"),
    ])

    _orch(provider, runner).run("build X")

    assert len(runner.calls) == 1


def test_a_runner_that_cannot_resume_keeps_the_old_cold_retry():
    provider = ScriptedProvider(plans=[PLAN],
                                verdicts=[{"met": False, "reason": "not finished",
                                           "next_action": "finish it"}, {"met": True}])
    runner = OldRunner([
        DeepResult(met=False, output="half done", limit_hit=True, session_id="sess-1"),
        DeepResult(met=True, output="done"),
    ])

    _orch(provider, runner).run("build X")

    assert len(runner.calls) == 2
    assert runner.calls[1]["max_turns"] == 30
    assert "PREVIOUS ATTEMPT DID NOT YET MEET THE GOAL" in runner.calls[1]["brief"]


def test_the_token_budget_still_stops_a_continuation():
    """The continuation is a cheaper way to finish, never a way around the budget."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": False, "reason": "not finished"}])
    runner = ResumableRunner([
        DeepResult(met=False, output="half done", limit_hit=True, session_id="sess-1",
                   tokens=5000),
        DeepResult(met=True, output="done"),
    ])

    _orch(provider, runner, deep_goal_token_budget=1000).run("build X")

    assert len(runner.calls) == 1


# --- 3. the report ------------------------------------------------------------------------------

def test_a_run_that_falls_short_still_reports_what_it_did():
    """The whole point of the live failure: the work existed, and the person was told only that the
    task failed. The account of the work travels WITH the failure now, labelled unfinished."""
    from .conftest import StubProvider
    from .test_runner import MockQuestClient
    from quest_ai_runner.runner.executor import TaskExecutor

    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "do X", "deep_brief": "x", "rationale": "work"},
        {"met": False, "reason": "the tests were never written"},
        {"met": False, "reason": "the tests were never written"},
        {"met": False, "reason": "the tests were never written"},
    ])
    runner = ResumableRunner([
        DeepResult(met=False, output="Wrote engine.py and wired the poller.",
                   error="The worker used its entire 30-turn budget", limit_hit=True,
                   session_id="sess-1"),
        DeepResult(met=False, output="Wrote engine.py and wired the poller.",
                   error="The worker used its entire 60-turn budget", limit_hit=False,
                   session_id="sess-1"),
    ])
    client = MockQuestClient([])
    brain = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                         registry=ModelRegistry(provider), deep_runner=runner,
                         config=OrchestratorConfig(deep_goal_max_iterations=2))
    out = TaskExecutor(client, brain).execute({"id": "t9", "text": "build X"})

    assert out.status == "failed"
    reported = client.reports[0][2]
    assert "Wrote engine.py and wired the poller." in reported
    assert "unfinished" in reported            # never presented as done work
    assert "the tests were never written" in reported or "turn budget" in reported


def test_the_grown_budget_is_capped():
    """A worker going in circles must not talk its way into an unbounded run one continuation at a
    time: the growth stops at the cap, it does not keep doubling."""
    from quest_ai_runner.core.orchestrator import DEEP_CONTINUATION_TURN_MULTIPLIER_CAP as CAP

    provider = ScriptedProvider(plans=[PLAN],
                                verdicts=[{"met": False, "reason": "still not done"}] * 8)
    runner = ResumableRunner([
        DeepResult(met=False, output=f"pass {i}", limit_hit=True, session_id="sess-1")
        for i in range(8)
    ])

    _orch(provider, runner, deep_goal_max_iterations=8).run("build X")

    budgets = [c["max_turns"] for c in runner.calls]
    assert budgets[:3] == [30, 60, 90]
    assert max(budgets) == 30 * CAP


# --- 4. the shape a REAL worker produces when it runs out of turns -------------------------------
#
# Everything above this section gives its turn-exhausted DeepResult a non-empty ``output``. The real
# runner never produces that: Claude Code is cut off BEFORE the worker writes its final message, so
# its ``error_max_turns`` envelope has no ``result`` at all. Testing only the fabricated shape is
# why the continuation could ship, pass, and then never once fire in production: the guard for
# "a hard failure with NO output" claimed every real limit hit before the continuation was reached.


def envelope_without_result(subtype: str) -> bytes:
    """The real max-turns envelope: a ``result`` KEY THAT IS NOT THERE, not an empty string."""
    return json.dumps({
        "type": "result", "subtype": subtype, "is_error": True,
        "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01,
    }).encode()


def test_the_real_max_turns_envelope_carries_no_output_but_still_reports_limit_hit(
        monkeypatch, tmp_path):
    """``limit_hit`` must not depend on the worker having said anything. It is the ENVELOPE's
    statement, and on a real cut-off run the envelope is all there is."""
    class OutOfTurns:
        stdin = None
        returncode = 1

        def communicate(self, input=None, timeout=None):
            return (envelope_without_result("error_max_turns"), b"")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    monkeypatch.setattr(_sp, "Popen", lambda cmd, **kw: OutOfTurns())
    res = _runner(tmp_path).run_goal(goal="ship it", brief="do it", max_turns=60)

    assert res.limit_hit is True
    assert not (res.output or "").strip(), "a cut-off worker never writes a final message"
    assert res.session_id, "and the session is still resumable, which is the whole point"
    assert "60-turn budget" in (res.error or "")


def test_a_turn_exhausted_attempt_with_NO_output_is_still_continued():
    """The regression. This is the exact shape of the live 2026-09-21 failure: the worker had
    already sent the brief and posted its goal note, hit turn 61 of 60, and printed nothing. It was
    reported to its owner as an unconfirmed failure and its session was thrown away."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = ResumableRunner([
        DeepResult(met=False, output="", limit_hit=True, session_id="sess-cut-off",
                   error="The worker used its entire 60-turn budget without declaring the goal "
                         "met, so the goal is UNCONFIRMED rather than failed."),
        DeepResult(met=True, output="brief sent, goal note posted"),
    ])

    res = _orch(provider, runner).run("write today's brief")

    assert res.kind == "deep"
    assert len(runner.calls) == 2, "an empty output is not a reason to give up on a live session"
    first, second = runner.calls
    assert first["resume_session_id"] is None
    assert second["resume_session_id"] == "sess-cut-off"
    assert second["max_turns"] == 60           # deep_max_turns=30, grown once
    assert "YOU RAN OUT OF TURNS" in second["brief"]


def test_the_no_output_continuation_does_not_spend_a_verifier_call():
    """Nothing was said, so there is no claim to check. Verifying an empty string costs a call and
    returns "it did nothing", which would then be handed to the worker as what it still owes."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = ResumableRunner([
        DeepResult(met=False, output="", limit_hit=True, session_id="sess-1"),
        DeepResult(met=True, output="done"),
    ])

    _orch(provider, runner).run("build X")

    assert provider.verify_calls == 1, "only the attempt that actually produced output is verified"


def test_an_empty_failure_that_is_NOT_a_turn_limit_is_still_terminal():
    """The silent-no-op safety net must keep firing. A worker that crashed, timed out, or never ran
    leaves limit_hit False, and resuming that session would be a guess."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = ResumableRunner([
        DeepResult(met=False, output="", limit_hit=False, session_id="sess-1",
                   error="worker binary not found"),
        DeepResult(met=True, output="done"),
    ])

    _orch(provider, runner).run("build X")

    assert len(runner.calls) == 1, "a crash is not a continuation"


def test_a_continuation_is_never_offered_to_a_runner_that_cannot_resume():
    """Unchanged for the empty-output path too: with no way to resume, a continuation would be a
    cold re-run wearing the word 'continue', which is what this whole mechanism exists to avoid."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = OldRunner([
        DeepResult(met=False, output="", limit_hit=True, session_id="sess-1",
                   error="The worker used its entire 60-turn budget"),
        DeepResult(met=True, output="done"),
    ])

    _orch(provider, runner).run("build X")

    assert len(runner.calls) == 1


def test_the_token_budget_stops_a_NO_OUTPUT_continuation_too():
    """The new early path must not become a way around the budget either. It stops rather than
    resuming, exactly as the with-output path above does."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = ResumableRunner([
        DeepResult(met=False, output="", limit_hit=True, session_id="sess-1", tokens=5000,
                   error="The worker used its entire 60-turn budget"),
        DeepResult(met=True, output="done"),
    ])

    _orch(provider, runner, deep_goal_token_budget=1000).run("build X")

    assert len(runner.calls) == 1, "the budget is spent, so there is no second attempt"
    assert runner.calls[0]["resume_session_id"] is None
