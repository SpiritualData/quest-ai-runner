"""The Quest user a piece of work is for reaches every process the runner spawns, as
``QAR_REQUESTER_USER_ID``; an unknown requester clears the variable instead of inheriting one."""
import subprocess

from quest_ai_runner.adapters.acp_deep_runner import AcpDeepRunner
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from quest_ai_runner.core.orchestrator_capabilities import run_goal_accepts_requester_user_id
from quest_ai_runner.core.requester import (REQUESTER_ENV, apply_requester_env,
                                            requester_from_meta, requester_from_task)

ENV = "QAR_REQUESTER_USER_ID"


class CapturingPopen:
    returncode = 0
    stdin = None
    seen = []

    def __init__(self, cmd, **kw):
        CapturingPopen.seen.append(kw.get("env") or {})

    def communicate(self, input=None, timeout=None):  # noqa: A002
        raise BrokenPipeError("stop after capturing the environment")


def test_the_variable_name_is_the_documented_one():
    assert REQUESTER_ENV == ENV


def test_task_requester_is_the_creator_else_the_owner():
    assert requester_from_task({"created_by": "u-creator", "user_id": "u-owner"}) == "u-creator"
    assert requester_from_task({"user_id": "u-owner"}) == "u-owner"
    assert requester_from_task({}) is None


def test_turn_requester_prefers_the_explicit_key():
    assert requester_from_meta({"requester_user_id": "u-a", "user_id": "u-b"}) == "u-a"
    assert requester_from_meta({"user_id": "u-b"}) == "u-b"
    assert requester_from_meta(None) is None


def test_apply_sets_and_clears():
    env = {ENV: "stale"}
    assert apply_requester_env(env, "u-1")[ENV] == "u-1"
    assert ENV not in apply_requester_env(env, None)


def test_deep_worker_environment_carries_the_requester(monkeypatch):
    monkeypatch.setenv(ENV, "inherited-from-the-runner-process")
    CapturingPopen.seen = []
    monkeypatch.setattr(subprocess, "Popen", CapturingPopen)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude"))
    runner.run_goal(goal="g", brief="b", max_turns=1, requester_user_id="u-requester")
    runner.run_goal(goal="g", brief="b", max_turns=1)
    assert CapturingPopen.seen[0][ENV] == "u-requester"
    assert ENV not in CapturingPopen.seen[1]


def test_acp_environment_carries_the_requester():
    runner = AcpDeepRunner.__new__(AcpDeepRunner)
    runner.cfg = type("Cfg", (), {"extra_path_dirs": [], "node_path": None})()
    assert runner.build_env("u-acp")[ENV] == "u-acp"


def test_runners_opt_in_by_signature():
    assert run_goal_accepts_requester_user_id(SubprocessGoalRunner(SubprocessConfig(working_dir="/w")))

    class Old:
        def run_goal(self, *, goal, brief):
            ...

    assert not run_goal_accepts_requester_user_id(Old())


def test_command_tool_environment_carries_the_requester():
    import sys

    from quest_ai_runner.core.tools import ToolContext, make_command_handler
    handler = make_command_handler(
        [sys.executable, "-c", "import os;print(os.environ.get('QAR_REQUESTER_USER_ID','none'))"], {})
    assert handler({}, ToolContext(user_id="u-tool")).text == "u-tool"
    assert handler({}, ToolContext(meta={"requester_user_id": "u-meta"})).text == "u-meta"
