"""The stream-json deep runner, against a fake ``claude`` that speaks stream-json (no model, $0).

Each test starts a real child process (``tests/fakes/fake_claude_stream.py``) and drives it through
the real runner, so what is checked is the actual protocol handling: the lines written to stdin,
the replay that comes back, the interrupt, the signals and the exit. Times are short (tenths of a
second) and every wait has a bound.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.core import goal_runner_stream
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from quest_ai_runner.core.goal_runner_stream import StreamJsonGoalRunner
from quest_ai_runner.core.run_control import ControlMessage, RunControl

REPO_ROOT = str(Path(__file__).resolve().parents[1])


def make_claude(directory: Path, scenario: Dict[str, Any], name: str = "claude") -> str:
    """Write an executable that behaves as ``claude`` for this scenario; returns its path."""
    scenario = {"state_dir": str(directory / "sessions"), "log": str(directory / f"{name}.log"), **scenario}
    (directory / "sessions").mkdir(exist_ok=True)
    path = directory / name
    path.write_text(
        f"#!{sys.executable}\nimport json, sys\nsys.path.insert(0, {REPO_ROOT!r})\n"
        "from tests.fakes.fake_claude_stream import main\n"
        f"raise SystemExit(main(json.loads({json.dumps(scenario)!r})))\n")
    path.chmod(0o755)
    return str(path)


def read_log(directory: Path, name: str = "claude") -> List[Dict[str, Any]]:
    path = directory / f"{name}.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def make_runner(claude: str, workdir: Path, grace: float = 0.5) -> StreamJsonGoalRunner:
    cfg = SubprocessConfig(working_dir=str(workdir), claude_path=claude, review_interval_seconds=0,
                           cancel_grace_seconds=grace)
    runner = StreamJsonGoalRunner(cfg)
    runner.echo_wait_seconds = 0.4
    return runner


class ScriptedControl:
    """A RunControl whose messages become available at set times and whose flags are set by the test.

    ``claims`` records every message handed over, so a test can see that each is claimed once."""

    def __init__(self, messages: Optional[List[tuple]] = None, poll: float = 0.1) -> None:
        self.started = time.monotonic()
        self.scheduled = list(messages or [])     # (available_after_seconds, id, text)
        self.claims: List[str] = []
        self.seen: List[tuple] = []
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.lock = threading.Lock()
        self.control = RunControl(claim_inputs=self.claim, mark_seen=self.mark_seen,
                                  cancel_requested=self.cancel.is_set,
                                  pause_requested=self.pause.is_set, poll_seconds=poll)

    def claim(self) -> List[ControlMessage]:
        with self.lock:
            now = time.monotonic() - self.started
            ready = [m for m in self.scheduled if m[0] <= now]
            self.scheduled = [m for m in self.scheduled if m[0] > now]
            self.claims.extend(m[1] for m in ready)
            return [ControlMessage(id=m[1], text=m[2]) for m in ready]

    def mark_seen(self, ids: List[str], where: str) -> None:
        self.seen.append((list(ids), where))


def run_in_thread(runner, control: Optional[RunControl], **kwargs):
    box: Dict[str, Any] = {}

    def go() -> None:
        box["result"] = runner.run_goal(goal="do the steps", brief="steps", control=control, **kwargs)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, box


STEPS3 = [{"name": "step1", "seconds": 0.6}, {"name": "step2", "seconds": 0.6}, {"name": "step3", "seconds": 0.6}]


def test_steering_message_is_written_mid_tool_and_seen_at_the_next_turn(tmp_path):
    claude = make_claude(tmp_path, {"steps": STEPS3})
    runner = make_runner(claude, tmp_path)
    scripted = ScriptedControl([(0.3, "m1", "skip step3")])
    started = time.monotonic()
    res = runner.run_goal(goal="do the steps", brief="steps", control=scripted.control)
    assert res.met is True
    assert res.output == "ran step1,step2"            # the steer dropped step3
    assert scripted.claims == ["m1"]                   # claimed once
    assert scripted.seen == [(["m1"], "deep_worker")]  # seen once, by the worker
    assert res.unseen_inputs == []
    log = read_log(tmp_path)
    user_lines = [e for e in log if e["event"] == "stdin" and '"type": "user"' in e["line"]]
    assert len(user_lines) == 2                        # the prompt, then the one message
    order = [e["event"] + ":" + str(e.get("name", e.get("text", ""))) for e in log
             if e["event"] in ("tool_start", "tool_end", "replay")]
    # written while step1 ran; replayed after step1 finished; step3 never started
    assert order.index("replay:skip step3") > order.index("tool_end:step1")
    assert "tool_start:step3" not in order
    assert time.monotonic() - started < 6


def test_stop_interrupts_the_running_tool_and_keeps_the_session(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "quick", "seconds": 0.1}, {"name": "slow", "seconds": 30}]})
    runner = make_runner(claude, tmp_path)
    scripted = ScriptedControl()
    thread, box = run_in_thread(runner, scripted.control)
    time.sleep(0.5)          # the slow tool is running
    scripted.cancel.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    res = box["result"]
    assert res.cancelled is True and res.paused is False and res.met is False
    assert res.session_id
    events = [e["event"] for e in read_log(tmp_path)]
    assert "interrupt" in events and "tool_interrupted" in events    # the step was cut off at once
    session = json.loads((tmp_path / "sessions" / f"{res.session_id}.json").read_text())
    assert session["ran"] == ["quick"]                                # finished work is kept
    pid = [e for e in read_log(tmp_path) if e["event"] == "argv"][0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                               # the process was released


def test_pause_releases_the_process_and_resume_continues_the_same_session(tmp_path):
    first = make_claude(tmp_path, {"steps": [{"name": "a", "seconds": 0.1}, {"name": "b", "seconds": 30}]}, "claude")
    runner = make_runner(first, tmp_path)
    scripted = ScriptedControl()
    thread, box = run_in_thread(runner, scripted.control)
    time.sleep(0.5)
    scripted.pause.set()
    thread.join(timeout=10)
    paused = box["result"]
    assert paused.paused is True and paused.cancelled is True
    sid = paused.session_id
    assert sid
    # later: the same thread resumes with the feedback as the new brief
    second = make_claude(tmp_path, {"steps": [{"name": "c", "seconds": 0.1}]}, "claude2")
    resumed = make_runner(second, tmp_path).run_goal(
        goal="carry on", brief="feedback: finish up", resume_session_id=sid, control=ScriptedControl().control)
    assert resumed.met is True
    assert resumed.session_id == sid
    assert resumed.output == "resumed after a; ran a,c"               # a was kept, b never ran
    argv = [e for e in read_log(tmp_path, "claude2") if e["event"] == "argv"][0]["argv"]
    assert "--resume" in argv and sid in argv


def test_a_resumed_worker_is_watched_from_where_the_last_one_stopped(tmp_path, monkeypatch):
    """A nudge or a retry resumes the session; its earlier output must not appear on screen a second time."""
    started: List[Dict[str, Any]] = []

    def watch(*args, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(goal_runner_stream, "_monitor_claude_session", watch)
    monkeypatch.setattr(goal_runner_stream, "session_file_size", lambda working_dir, session_id: 321)
    first = make_claude(tmp_path, {"steps": [{"name": "a", "seconds": 0.1}]}, "claude")
    fresh = make_runner(first, tmp_path).run_goal(goal="g", brief="b", emit=lambda event: None,
                                                  control=ScriptedControl().control)
    second = make_claude(tmp_path, {"steps": [{"name": "c", "seconds": 0.1}]}, "claude2")
    make_runner(second, tmp_path).run_goal(goal="g", brief="b", resume_session_id=fresh.session_id,
                                           emit=lambda event: None, control=ScriptedControl().control)

    assert [k["start_offset"] for k in started] == [0, 321]
    assert started[1]["session_id"] == fresh.session_id


def test_an_interrupt_the_worker_ignores_ends_in_terminate(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "slow", "seconds": 30}], "ignore_interrupt": True})
    runner = make_runner(claude, tmp_path, grace=0.4)
    scripted = ScriptedControl()
    thread, box = run_in_thread(runner, scripted.control)
    time.sleep(0.5)
    asked = time.monotonic()
    scripted.cancel.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert box["result"].cancelled is True
    assert time.monotonic() - asked < 5                               # bounded by the grace, not the tool
    log = read_log(tmp_path)
    assert "interrupt_ignored" in [e["event"] for e in log]
    pid = [e for e in log if e["event"] == "argv"][0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_worker_that_ignores_interrupt_and_terminate_is_killed(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "slow", "seconds": 30}],
                                    "ignore_interrupt": True, "ignore_sigterm": True})
    runner = make_runner(claude, tmp_path, grace=0.3)
    scripted = ScriptedControl()
    thread, box = run_in_thread(runner, scripted.control)
    time.sleep(0.5)
    scripted.cancel.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert box["result"].cancelled is True
    pid = [e for e in read_log(tmp_path) if e["event"] == "argv"][0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                               # SIGKILL is not ignorable


def test_a_message_landing_as_the_run_finishes_is_delivered_once(tmp_path):
    # The last tool ends at 0.5s; the final turn then waits 0.6s before it reports. The message is
    # available at 0.8s, inside that window: written once, replayed in a new turn, never twice.
    claude = make_claude(tmp_path, {"steps": [{"name": "a", "seconds": 0.5}], "late_window": 0.6})
    runner = make_runner(claude, tmp_path)
    scripted = ScriptedControl([(0.8, "late", "add b")])
    res = runner.run_goal(goal="do the steps", brief="steps", control=scripted.control)
    assert res.met is True
    assert res.output == "ran a,b"
    assert scripted.claims == ["late"]
    assert scripted.seen == [(["late"], "deep_worker")]
    user_lines = [e for e in read_log(tmp_path) if e["event"] == "stdin" and '"type": "user"' in e["line"]]
    assert len(user_lines) == 2


def test_a_message_after_the_run_ended_is_never_claimed(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "a", "seconds": 0.2}]})
    runner = make_runner(claude, tmp_path)
    scripted = ScriptedControl([(30.0, "later", "too late")])
    res = runner.run_goal(goal="do the steps", brief="steps", control=scripted.control)
    assert res.met is True and res.output == "ran a"
    assert scripted.claims == []                  # still waiting for the caller's follow-up path
    assert len(scripted.scheduled) == 1


def test_two_concurrent_runs_never_cross_deliver(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "s1", "seconds": 0.5}, {"name": "s2", "seconds": 0.5},
                                             {"name": "s3", "seconds": 0.5}]})
    runner = make_runner(claude, tmp_path)         # ONE runner serves both runs
    a = ScriptedControl([(0.2, "a1", "skip s3")])
    b = ScriptedControl([(0.2, "b1", "add extra")])
    thread_a, box_a = run_in_thread(runner, a.control)
    thread_b, box_b = run_in_thread(runner, b.control)
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert box_a["result"].output == "ran s1,s2"          # a's message steered a only
    assert box_b["result"].output == "ran s1,s2,s3,extra"  # b's message steered b only
    assert a.claims == ["a1"] and b.claims == ["b1"]
    assert a.seen == [(["a1"], "deep_worker")] and b.seen == [(["b1"], "deep_worker")]


def test_a_written_message_that_is_never_replayed_comes_back_unseen_not_lost(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "a", "seconds": 0.6}], "no_echo": True})
    runner = make_runner(claude, tmp_path)
    scripted = ScriptedControl([(0.2, "m1", "add b")])
    res = runner.run_goal(goal="do the steps", brief="steps", control=scripted.control)
    assert scripted.claims == ["m1"]
    assert scripted.seen == []                    # no replay, so it is not reported as seen
    assert res.unseen_inputs == ["add b"]         # and it is handed back, not dropped


def test_a_stop_hands_back_a_message_that_was_written_but_not_replayed(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "slow", "seconds": 30}], "no_echo": True})
    runner = make_runner(claude, tmp_path)
    scripted = ScriptedControl([(0.2, "m1", "please also check x")])
    thread, box = run_in_thread(runner, scripted.control)
    time.sleep(0.8)
    scripted.cancel.set()
    thread.join(timeout=10)
    assert box["result"].cancelled is True
    assert box["result"].unseen_inputs == ["please also check x"]


def test_cancel_check_alone_also_stops_the_run(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "slow", "seconds": 30}]})
    runner = make_runner(claude, tmp_path)
    flag = threading.Event()
    thread, box = run_in_thread(runner, None, cancel_check=flag.is_set)
    time.sleep(0.5)
    flag.set()
    thread.join(timeout=10)
    assert box["result"].cancelled is True


def test_a_claude_too_old_to_report_an_interrupt_runs_one_shot(tmp_path, monkeypatch):
    claude = make_claude(tmp_path, {"version": "2.0.9 (Claude Code)"})
    runner = make_runner(claude, tmp_path)
    called: Dict[str, Any] = {}

    def fake_once(self, **kwargs):
        called.update(kwargs)
        from quest_ai_runner.core.adapters import DeepResult
        return DeepResult(met=True, output="one-shot")

    monkeypatch.setattr(SubprocessGoalRunner, "run_goal_once", fake_once)
    res = runner.run_goal(goal="g", brief="b", control=ScriptedControl().control)
    assert res.output == "one-shot" and called["goal"] == "g"


def test_the_runner_is_declared_and_not_selected():
    from quest_ai_runner.core import resolve
    assert StreamJsonGoalRunner.config_class is SubprocessConfig
    assert goal_runner_stream.MIN_STREAM_CONTROL_VERSION == (2, 1, 219)
    assert "stream_json" in resolve.names("run-deep-work")
    assert resolve.resolve("run-deep-work", "stream_json") is StreamJsonGoalRunner
    assert resolve.default_name("run-deep-work") != "stream_json"   # an alternative until Phase 4 measures it


def test_the_wall_clock_cap_kills_the_session(tmp_path):
    claude = make_claude(tmp_path, {"steps": [{"name": "slow", "seconds": 30}]})
    cfg = SubprocessConfig(working_dir=str(tmp_path), claude_path=claude, review_interval_seconds=0,
                           timeout_seconds=0.6, cancel_grace_seconds=0.5)
    res = StreamJsonGoalRunner(cfg).run_goal(goal="do the steps", brief="steps",
                                             control=ScriptedControl().control)
    assert res.met is False and res.cancelled is False
    assert "wall-clock timeout" in (res.error or "")
    pid = [e for e in read_log(tmp_path) if e["event"] == "argv"][0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
