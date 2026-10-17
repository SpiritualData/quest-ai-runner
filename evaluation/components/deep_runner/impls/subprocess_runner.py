"""CURRENT (selected): the reference ``core.goal_runner.SubprocessGoalRunner``, run through ``run_goal`` with
``subprocess.Popen`` replaced by a scripted peer (exit code, stdout, stderr, a missing binary, a crash).
Only the process edge is faked. Zero model calls."""
import subprocess

from deep_runner.support import normalise
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner


class ScriptedPopen:
    def __init__(self, scenario):
        self.scenario = scenario
        self.returncode = scenario.get("exit", 0)
        self.stdin = None
        self.pid = 0

    def communicate(self, input=None, timeout=None):  # noqa: A002
        if self.scenario.get("crash"):
            raise RuntimeError("worker died")
        return (self.scenario.get("output", "").encode(), self.scenario.get("stderr", "").encode())

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        return None


def build(meter):  # noqa: ARG001
    def work(kind, args):
        if kind != "run_worker":
            raise ValueError(kind)
        scenario = args["scenario"]

        def fake_popen(cmd, **kw):  # noqa: ARG001
            if scenario.get("spawn_error") == "missing":
                raise FileNotFoundError(2, "No such file or directory", cmd[0])
            return ScriptedPopen(scenario)

        real = subprocess.Popen
        subprocess.Popen = fake_popen
        try:
            runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/work", claude_path="/usr/bin/claude"))
            try:
                result = runner.run_goal(goal="update the docs", brief="do it", max_turns=3)
            except Exception:  # noqa: BLE001
                return normalise(None, args.get("want"), raised=True)
        finally:
            subprocess.Popen = real
        return normalise(result, args.get("want"))
    return work
