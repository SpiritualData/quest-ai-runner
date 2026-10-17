"""The ``DeepRunner`` port promises ``run_goal`` never raises. A worker process that dies mid-run (the pipe
breaks, ``communicate`` raises) must come back as a not-met ``DeepResult`` with a reason, not an exception."""
import subprocess

from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner


class DyingPopen:
    returncode = 0
    stdin = None

    def communicate(self, input=None, timeout=None):  # noqa: A002
        raise BrokenPipeError("worker died")


def test_a_worker_that_dies_mid_run_is_a_not_met_result_not_an_exception(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: DyingPopen())
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude"))
    result = runner.run_goal(goal="fix the date bug", brief="do it", max_turns=3)
    assert result.met is False
    assert "BrokenPipeError" in (result.error or "")
