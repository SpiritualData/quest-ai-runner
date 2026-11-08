"""The deep runner is the hub's pick among the agent-harness arms the deployment's declared providers allow.

``RunnerConfig.deep_providers`` names the providers a deployment holds credentials for; ``resolve_deep_runner`` asks the hub
(``hubofhubs.api.choose_agent_arm``) for the best arm among those and builds it. A harness QAR binds (the Claude Code runners) is built
as QAR's binding; any other harness is the hub's class. An explicit ``deep_runner`` or a ``selections`` entry still wins, and
``deep_runner=None`` still disables execution. Offline: fake programs stand in for the harness binaries and are never run.
"""
from __future__ import annotations

import os

import pytest

import hubofhubs.api as hub_api
from hubofhubs.api import ArmChoice, OpencodeRunner
from quest_ai_runner.the_body.configuration.config import RunnerConfig, resolve_deep_runner
from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessGoalRunner
from quest_ai_runner.the_world.tools.delegated_agent_sessions.goal_runner_stream import StreamJsonGoalRunner
from tests.conftest import StubProvider, StubRetrieval


@pytest.fixture
def programs(tmp_path, monkeypatch):
    """Executables named ``claude`` and ``opencode`` on PATH; resolution only has to find them."""
    for name in ("claude", "opencode"):
        binary = tmp_path / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    monkeypatch.setenv("QAR_CLAUDE_PATH", "claude")
    for name in ("ESOI_SELECT_QUEST_AI_RUNNER_DELEGATED_AGENT_SESSIONS", "ESOI_SELECT_DELEGATED_AGENT_SESSIONS"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def picks(monkeypatch):
    """The hub's choice, fixed per test: ``{frozenset(providers): ArmChoice}``; records what each call declared."""
    table, asked = {}, []

    def choose(declared):
        asked.append(tuple(declared))
        return table.get(frozenset(declared))
    monkeypatch.setattr(hub_api, "choose_agent_arm", choose)
    return table, asked


def arm(harness, provider, tier="fast", basis="measured"):
    return ArmChoice(f"{harness}@provider={provider},tier={tier}", harness, provider, tier, basis, "staged", 0.9, "test")


def cfg(**kwargs):
    return RunnerConfig(retrieval=StubRetrieval({"README.md": "hi"}), model_provider=StubProvider([]), corpus_root="/tmp", **kwargs)


def test_undeclared_providers_mean_the_claude_login_and_build_qars_binding(programs, picks):
    table, asked = picks
    table[frozenset({"anthropic"})] = arm("stream_json_goal_runner", "anthropic")
    runner = resolve_deep_runner(cfg())
    assert asked == [("anthropic",)]
    assert type(runner) is StreamJsonGoalRunner   # QAR's binding of the hub harness, so QAR's settings apply


def test_a_declared_provider_gets_a_harness_qar_does_not_bind(programs, picks):
    table, _ = picks
    table[frozenset({"anthropic", "gemini"})] = arm("opencode_runner", "gemini")
    runner = resolve_deep_runner(cfg(deep_providers=["anthropic", "gemini"]))
    assert isinstance(runner, OpencodeRunner) and runner.provider == "gemini" and runner.tier == "fast"
    assert runner.uses_deep_model is False   # the goal loop must not announce a Claude ladder model for it


def test_the_one_shot_claude_arm_gets_its_tier(programs, picks):
    table, _ = picks
    table[frozenset({"anthropic"})] = arm("subprocess_goal_runner", "anthropic", tier="best")
    runner = resolve_deep_runner(cfg())
    assert type(runner) is SubprocessGoalRunner and runner.tier_model


def test_an_explicit_choice_wins(programs, picks):
    table, asked = picks
    table[frozenset({"anthropic"})] = arm("stream_json_goal_runner", "anthropic")
    runner = resolve_deep_runner(cfg(selections={"delegated-agent-sessions": "subprocess"}))
    assert type(runner) is SubprocessGoalRunner and asked == []
    assert resolve_deep_runner(cfg(deep_runner=None)) is None


def test_no_eligible_arm_or_missing_program_falls_back_to_the_default(programs, picks, monkeypatch):
    table, _ = picks
    assert type(resolve_deep_runner(cfg(deep_providers=["openai"]))) is SubprocessGoalRunner   # nothing eligible: the default
    table[frozenset({"gemini"})] = arm("opencode_runner", "gemini")
    os.remove(programs / "opencode")
    assert type(resolve_deep_runner(cfg(deep_providers=["gemini"]))) is SubprocessGoalRunner
