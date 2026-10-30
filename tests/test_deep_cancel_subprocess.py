"""Cancelling a task stops the running Claude Code process, promptly.

Before this, ``run_goal`` took no cancel signal. A Stop pressed during a deep run was read only
between attempts, so ``claude -p`` kept editing files until it exited, until the hour-long timeout,
or until the liveness review decided to kill it. And that kill never worked: the helper it used,
``kill_process_group``, referenced ``os`` without importing it and raised ``NameError``, which the
runner's catch-all turned into "the deep worker failed unexpectedly" with the process still alive.

These tests spawn a REAL child process (a tiny stand-in for ``claude`` that sleeps), so they pin
what matters: the process is gone, how fast, and in what order the signals escalate. No model, no
network, no cost.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest

from quest_ai_runner.core import goal_runner_contract
from quest_ai_runner.core.adapters_values import DeepResult, ProgressEvent
from quest_ai_runner.core.adapters_events import EVENT_EXEC
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from quest_ai_runner.core.goal_runner_contract import kill_process_group
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider

FAKE_CLAUDE = """#!{python}
import os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
IGNORE = {ignore!r}
signal.signal(signal.SIGINT, signal.SIG_IGN if "int" in IGNORE else signal.default_int_handler)
if "term" in IGNORE:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.path.join(here, "pid"), "w") as f:
    f.write(str(os.getpid()))
sys.stdin.read()
with open(os.path.join(here, "started"), "w") as f:
    f.write("1")
time.sleep(120)
"""


def make_fake_claude(tmp_path: Path, ignore: tuple = ()) -> str:
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable, ignore=ignore))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def runner_for(tmp_path: Path, ignore: tuple = (), grace: float = 0.4) -> SubprocessGoalRunner:
    return SubprocessGoalRunner(SubprocessConfig(
        working_dir=str(tmp_path), claude_path=make_fake_claude(tmp_path, ignore),
        review_interval_seconds=0, cancel_grace_seconds=grace))


@pytest.fixture(autouse=True)
def quick_poll(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    monkeypatch.setattr("quest_ai_runner.core.goal_runner.CANCEL_POLL_SECONDS", 0.1, raising=False)


def cancel_once_started(tmp_path: Path):
    """A cancel signal that turns True a moment after the stand-in has read its prompt."""
    started = tmp_path / "started"
    state = {"calls": 0}

    def check() -> bool:
        state["calls"] += 1
        return started.exists()
    check.state = state
    return check


def test_a_cancel_stops_the_running_process_and_keeps_the_session(tmp_path):
    runner = runner_for(tmp_path)
    t0 = time.time()
    res = runner.run_goal(goal="long job", brief="do it", max_turns=3,
                          cancel_check=cancel_once_started(tmp_path))
    took = time.time() - t0

    assert res.cancelled is True
    assert res.met is False
    assert res.session_id, "the session id is what a later reply resumes"
    assert took < 8, f"stop must be prompt, took {took:.1f}s"
    pid = int((tmp_path / "pid").read_text())
    assert wait_until(lambda: not pid_alive(pid)), "the worker process must be gone"


def test_a_worker_that_ignores_the_interrupt_is_terminated_after_the_grace(tmp_path):
    runner = runner_for(tmp_path, ignore=("int",), grace=0.4)
    t0 = time.time()
    res = runner.run_goal(goal="long job", brief="do it", max_turns=3,
                          cancel_check=cancel_once_started(tmp_path))
    took = time.time() - t0

    assert res.cancelled is True
    assert took < 8
    pid = int((tmp_path / "pid").read_text())
    assert wait_until(lambda: not pid_alive(pid))


def test_a_worker_that_ignores_interrupt_and_terminate_is_killed(tmp_path):
    runner = runner_for(tmp_path, ignore=("int", "term"), grace=0.4)
    t0 = time.time()
    res = runner.run_goal(goal="long job", brief="do it", max_turns=3,
                          cancel_check=cancel_once_started(tmp_path))
    took = time.time() - t0

    assert res.cancelled is True
    assert took < 10
    pid = int((tmp_path / "pid").read_text())
    assert wait_until(lambda: not pid_alive(pid))


def test_a_run_nobody_cancels_is_untouched(tmp_path, monkeypatch):
    """A cancel signal that never fires changes nothing: the worker finishes on its own."""
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\nsys.stdin.read()\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
        " 'result': 'all done', 'usage': {'input_tokens': 1, 'output_tokens': 1},"
        " 'total_cost_usd': 0.0}))\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    runner = SubprocessGoalRunner(SubprocessConfig(
        working_dir=str(tmp_path), claude_path=str(script), review_interval_seconds=0))

    res = runner.run_goal(goal="quick job", brief="do it", max_turns=3,
                          cancel_check=lambda: False)

    assert res.met is True and res.cancelled is False
    assert "all done" in res.output


def test_kill_process_group_actually_kills_the_group():
    """The liveness-review and timeout kills went through this helper, which raised NameError."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    kill_process_group(proc)
    assert wait_until(lambda: proc.poll() is not None), "the process must be dead"


def test_stop_process_group_reports_how_far_it_had_to_escalate():
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    time.sleep(0.3)   # let the child install its handler
    _, _, how = goal_runner_contract.stop_process_group(proc, 0.4)
    assert how == "terminated"


# --- the orchestrator hands the worker its cancel signal -----------------------------------------

PLAN = {"action": "deep", "goal": "Do the work",
        "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
        "rationale": "deep"}


class CancellableRunner:
    """Accepts ``cancel_check`` and reports a stop, as a real worker does after a Stop."""

    def __init__(self, stops: bool = True) -> None:
        self.calls: List[dict] = []
        self.stops = stops

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None, cancel_check=None) -> DeepResult:
        self.calls.append({"cancel_check": cancel_check})
        if not self.stops:
            return DeepResult(met=True, output="done")
        return DeepResult(met=False, output="half of it", cancelled=True, session_id="sess-1")


class OldRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None) -> DeepResult:
        self.calls += 1
        return DeepResult(met=True, output="done")


def build(runner, verdict=None) -> Orchestrator:
    provider = ScriptedProvider(plans=[PLAN],
                                verdicts=[verdict or {"met": False, "reason": "unused"}])
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=runner,
                        config=OrchestratorConfig(deep_max_turns=30, deep_goal_max_iterations=3))


def test_the_orchestrator_passes_its_cancel_signal_to_a_runner_that_accepts_it():
    runner = CancellableRunner(stops=False)
    sentinel = {"cancelled": False}

    build(runner, verdict={"met": True}).run("build X",
                                             cancel_check=lambda: sentinel["cancelled"])

    assert len(runner.calls) == 1
    handed = runner.calls[0]["cancel_check"]
    assert callable(handed)
    assert handed() is False
    sentinel["cancelled"] = True
    assert handed() is True


def test_a_stopped_worker_ends_the_run_without_a_verifier_call_or_a_retry():
    """The caller's own check is throttled and may still read False when the worker reports the
    stop; the result itself must end the run."""
    runner = CancellableRunner()

    res = build(runner).run("build X", cancel_check=lambda: False)

    assert len(runner.calls) == 1, "a stopped worker is not retried"
    assert res.kind == "cancelled"


def test_a_runner_without_the_parameter_is_called_exactly_as_before():
    runner = OldRunner()

    build(runner).run("build X", cancel_check=lambda: False)

    assert runner.calls >= 1


# --- terminal Esc: run_stream cancels the run instead of abandoning it ----------------------------

class BlockingRunner:
    """Streams one event, then works until its cancel signal fires (a stand-in for a long
    ``claude -p``). ``stopped_at`` records when it noticed, ``events_after`` anything it did after."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.noticed = threading.Event()
        self.finished = threading.Event()

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None, emit=None, cancel_check=None) -> DeepResult:
        if emit is not None:
            emit(ProgressEvent(type=EVENT_EXEC, text="working", data={"run_id": "r1"}))
        self.started.set()
        deadline = time.time() + 20
        while time.time() < deadline:
            if cancel_check is not None and cancel_check():
                self.noticed.set()
                break
            time.sleep(0.02)
        self.finished.set()
        return DeepResult(met=False, output="", cancelled=self.noticed.is_set(),
                          session_id="sess-1")


def test_closing_run_stream_stops_the_run_and_waits_for_it():
    """Esc in the terminal breaks out of the stream. That must cancel the worker and not return
    until it has stopped, so queued messages cannot start a second turn beside a live one."""
    runner = BlockingRunner()
    stream = build(runner).run_stream("build X")
    for item in stream:
        if runner.started.is_set():
            break
    stream.close()

    assert runner.noticed.is_set(), "the worker must be told to stop"
    assert runner.finished.is_set(), "close() must wait until the worker has stopped"


def test_run_stream_forwards_a_cancel_check_to_the_run():
    runner = BlockingRunner()
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()

    items = list(build(runner).run_stream("build X", cancel_check=flag.is_set))

    assert runner.noticed.is_set()
    assert items[-1].kind == "cancelled"
