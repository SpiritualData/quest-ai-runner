"""Offline tests for QAR's binding of the hub's keyless ``claude_cli`` arm.

The arm's own behaviour (flags, JSON extraction, usage accounting, the thinking caps) is tested in the hub
(``tests/test_models_claude_cli.py``). These cover what QAR binds: the live newest-model lookup, the tier registry, the
environment settings it reads, and the lane-wide usage limit. Nothing spawns the real ``claude`` binary.
"""
import json
import subprocess

import pytest

from hubofhubs.models import claude_cli as hub
from quest_ai_runner.adapters import ClaudeCliProvider
from quest_ai_runner.adapters import claude_cli_provider as ccp
from quest_ai_runner.core.goal_runner import _is_claude_model
from quest_ai_runner.core.model_registry import ModelRegistry


def test_the_provider_is_the_hubs_arm_with_qars_collaborators_bound():
    provider = ClaudeCliProvider()
    assert isinstance(provider, hub.ClaudeCliProvider)
    assert provider.usage_limits is ccp.usage_limit and provider.newest_family_id is ccp.newest_claude_id


def test_cli_model_uses_the_newest_live_id_of_a_family(monkeypatch):
    monkeypatch.setattr(ccp, "newest_claude_id", lambda family: f"claude-{family}-9-9")
    assert ccp.cli_model("claude-opus-4-8") == "claude-opus-9-9"
    assert ccp.cli_model("gemini-3.5-flash") is None


def test_cli_model_and_is_claude_model_accept_fable(monkeypatch):
    monkeypatch.setattr(ccp, "newest_claude_id", lambda family: None)
    assert ccp.cli_model("fable") == "fable"
    assert ccp.cli_model("claude-fable-5-1") == "fable"
    assert _is_claude_model("fable") is True
    assert _is_claude_model("claude-fable-5-1") is True
    assert _is_claude_model("gemini-3.5-flash") is False


def test_every_tier_resolves_to_a_cli_runnable_model():
    """No tier may resolve to a model the CLI cannot run (a Gemini id on a CLI-only lane killed every task at its first planner call)."""
    registry = ModelRegistry(ClaudeCliProvider())
    for tier in ("fast", "balanced", "quality", "best"):
        resolved = registry.resolve_tier(tier)
        assert "claude" in resolved.lower(), f"tier {tier} resolved to non-Claude {resolved!r}"
        assert ccp.cli_model(resolved) is not None, f"tier {tier} gave the CLI an unusable {resolved!r}"


def test_explicit_tier_override_still_wins():
    registry = ModelRegistry(ClaudeCliProvider(), fallback={"balanced": "claude-haiku-4-5"})
    assert registry.resolve_tier("balanced") == "claude-haiku-4-5"


def test_build_env_strips_billing_and_session_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = ClaudeCliProvider()._build_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env and "CLAUDECODE" not in env


def captured_thinking_env(monkeypatch, model):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}

        class P:
            returncode = 0
            stdout = json.dumps({"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
            stderr = b""
        return P()

    monkeypatch.setattr(hub, "_supported_flags", lambda binary: frozenset(hub._ONE_SHOT_FLAGS.keys()))
    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCliProvider()._invoke("hi", model=model)
    return captured["env"].get("MAX_THINKING_TOKENS")


def test_the_haiku_thinking_setting_is_read_from_the_environment_when_a_call_is_made(monkeypatch):
    monkeypatch.setenv("QAR_CLI_HAIKU_THINKING_TOKENS", "0")
    assert captured_thinking_env(monkeypatch, "haiku") == "0"
    assert captured_thinking_env(monkeypatch, "sonnet") is None
    monkeypatch.delenv("QAR_CLI_HAIKU_THINKING_TOKENS")
    assert captured_thinking_env(monkeypatch, "haiku") is None


def test_the_effort_setting_is_read_from_the_environment(monkeypatch):
    argv = {}

    def fake_run(cmd, **kwargs):
        argv["cmd"] = cmd

        class P:
            returncode = 0
            stdout = b'{"result": "ok"}'
            stderr = b""
        return P()

    monkeypatch.setattr(hub, "_supported_flags", lambda binary: frozenset(hub._ONE_SHOT_FLAGS.keys()))
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("QAR_CLI_EFFORT", "low")
    ClaudeCliProvider()._invoke("hi", model="haiku")
    assert argv["cmd"][argv["cmd"].index("--effort") + 1] == "low"


def test_the_lanes_usage_limit_is_the_typed_error_of_core():
    from quest_ai_runner.core import usage_limit
    provider = ClaudeCliProvider()
    assert isinstance(provider.usage_limit_error(usage_limit.UsageLimit(message="weekly limit")), usage_limit.UsageLimitError)
    with pytest.raises(usage_limit.UsageLimitError):
        raise provider.usage_limit_error(usage_limit.UsageLimit(message="weekly limit"))
