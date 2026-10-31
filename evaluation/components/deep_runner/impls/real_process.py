"""REAL PROCESS (the single-case arm of ``run-deep-work``): the same ``core.goal_runner.SubprocessGoalRunner``, run through
``run_goal`` as in ``subprocess_runner``, but the worker is a REAL child process that the operating system starts. ``claude_path``
names a small executable script, written to a scratch folder, that prints the scenario's output and stderr and exits with its exit
code; a crash is the child killing itself with a signal, and a missing binary is a path that does not exist. Nothing in the runner is
replaced, not even ``subprocess.Popen``, so spawning, pipes, exit codes and signals are the real ones. Zero model calls, zero
network, a few milliseconds per case: the worker is a script, not Claude Code."""
import json
import os
import stat
import sys
import tempfile

from deep_runner.support import normalise
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

WORKER = '''#!{python}
import json, os, signal, sys
scenario = json.load(open({scenario_path!r}))
if scenario.get("crash"):
    os.kill(os.getpid(), signal.SIGKILL)
sys.stdout.write(scenario.get("output", ""))
sys.stderr.write(scenario.get("stderr", ""))
sys.exit(scenario.get("exit", 0))
'''


def write_worker(folder, scenario):
    scenario_path = os.path.join(folder, "scenario.json")
    with open(scenario_path, "w", encoding="utf-8") as handle:
        json.dump(scenario, handle)
    worker = os.path.join(folder, "worker")
    with open(worker, "w", encoding="utf-8") as handle:
        handle.write(WORKER.format(python=sys.executable, scenario_path=scenario_path))
    os.chmod(worker, os.stat(worker).st_mode | stat.S_IXUSR)
    return worker


def build(meter):  # noqa: ARG001
    def work(kind, args):
        if kind != "run_worker":
            raise ValueError(kind)
        scenario = args["scenario"]
        with tempfile.TemporaryDirectory() as folder:
            if scenario.get("spawn_error") == "missing":
                claude_path = os.path.join(folder, "no-such-binary")
            else:
                claude_path = write_worker(folder, scenario)
            runner = SubprocessGoalRunner(SubprocessConfig(working_dir=folder, claude_path=claude_path))
            try:
                result = runner.run_goal(goal="update the docs", brief="do it", max_turns=3)
            except Exception:  # noqa: BLE001
                return normalise(None, args.get("want"), raised=True)
        return normalise(result, args.get("want"))
    return work
