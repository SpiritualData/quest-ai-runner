"""Every place QAR starts ``claude`` passes the child an environment without API-billing credentials (fake processes, no model)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GONE = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "ANTHROPIC_BASE_URL"}
GATEWAY = "http://127.0.0.1:18764"


def dirty(monkeypatch, base_url="http://localhost:18770"):
    for name in GONE:
        monkeypatch.setenv(name, base_url if name == "ANTHROPIC_BASE_URL" else "x")


def test_the_deep_runner_child_environment_lacks_the_credentials(monkeypatch):
    from quest_ai_runner.intelligence.mind.delegation.goal_runner import SubprocessConfig, SubprocessGoalRunner
    dirty(monkeypatch)
    env = SubprocessGoalRunner(SubprocessConfig(working_dir="/w")).build_env()
    assert not GONE & set(env)


def test_the_deep_runner_keeps_a_gateway_base_url(monkeypatch):
    from quest_ai_runner.intelligence.mind.delegation.goal_runner import SubprocessConfig, SubprocessGoalRunner
    dirty(monkeypatch, GATEWAY)
    env = SubprocessGoalRunner(SubprocessConfig(working_dir="/w")).build_env()
    assert env["ANTHROPIC_BASE_URL"] == GATEWAY and not (GONE - {"ANTHROPIC_BASE_URL"}) & set(env)


def test_the_provider_child_environment_lacks_the_credentials(monkeypatch):
    from quest_ai_runner.world.model_plumbing.vendor_model_caller.claude_cli_provider import ClaudeCliProvider
    from hubofhubs.api import claude_cli
    dirty(monkeypatch)
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")

        class Done:
            stdout = b'{"result": "ok", "usage": {}}'
            stderr = b""
            returncode = 0
        return Done()

    monkeypatch.setattr(claude_cli, "run_in_group", fake_run)
    monkeypatch.setattr(claude_cli, "_supported_flags", lambda binary: frozenset(claude_cli._ONE_SHOT_FLAGS))
    ClaudeCliProvider().answer([{"role": "user", "content": "hi"}], model="haiku")
    assert seen["env"] and not GONE & set(seen["env"])


def test_the_insitu_runner_child_environment_lacks_the_credentials(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
    from esoi_behavior import insitu
    dirty(monkeypatch)
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")

        class Done:
            returncode = 0
            stdout = b'{"result": "ok", "usage": {}}'
            stderr = b""
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)

    class Meter:
        tokens_in = tokens_out = 0

        def record(self):
            pass
    try:
        insitu.build_runner(Meter(), "haiku")("p")
    except Exception:  # noqa: BLE001  (the reply is a stub; only the environment handed to the process matters here)
        pass
    assert seen["env"] and not GONE & set(seen["env"])


def test_the_chat_judge_version_probe_child_environment_lacks_the_credentials(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
    from chat_quality import judge
    dirty(monkeypatch)
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")

        class Done:
            stdout = "1.0"
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(judge, "JUDGE_CLI_VERSION", {})
    judge.judge_cli_version()
    assert seen["env"] and not GONE & set(seen["env"])
