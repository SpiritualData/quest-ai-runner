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

    def choose(declared, difficulty=""):
        if difficulty:
            return table.get((difficulty, frozenset(declared)))
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


def test_the_hubs_own_loop_runs_only_when_named(programs):
    from hubofhubs.api import choose_agent_arm
    from quest_ai_runner.the_world.tools.delegated_agent_sessions.hub_loop_runner import HubLoopRunner
    runner = resolve_deep_runner(cfg(selections={"delegated-agent-sessions": "hub_loop"}))
    assert type(runner) is HubLoopRunner and HubLoopRunner.on_trial
    assert runner.goal_settings().escalation_marker                 # QAR's settings, not the hub's empty defaults
    for difficulty in ("", "hard"):                                  # the hub's real standings never hand a consumer a harness on trial
        assert choose_agent_arm(["anthropic"], difficulty=difficulty).harness != "solution_loop_runner"


def test_the_hubs_own_loop_is_handed_the_web_through_the_lanes_search_backend(monkeypatch):
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import hub_loop_runner
    from quest_ai_runner.the_world.tools.web_search import web_research

    class Research:
        def search(self, query):
            return type("O", (), {"error": None, "text": f"results for {query}", "hits": []})()

        def fetch(self, url, focus=None):
            return type("O", (), {"error": "blocked", "text": None, "hits": []})()
    # adapted: the builder is also called with ``provider=`` for the login fallback, so the stand-ins accept it (nothing at all stays empty)
    monkeypatch.setattr(web_research, "build_web_research_from_env", lambda env, provider=None: None)
    assert hub_loop_runner.web_tools({}) == {}
    monkeypatch.setattr(web_research, "build_web_research_from_env", lambda env, provider=None: Research())
    tools = hub_loop_runner.web_tools({})
    assert tools["web_search"].run(" tide pools ") == "results for tide pools" and tools["read_page"].run("https://example.org why") == "error: blocked"


def test_with_no_search_backend_the_loop_searches_through_the_claude_code_login(monkeypatch):
    from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.claude_cli_provider import ClaudeCliProvider
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import hub_loop_runner
    asked = []

    def web_search(self, query, *, model="", max_results=5, instruction=None):
        asked.append(query)
        return {"answer": "", "results": [{"title": "Python", "url": "https://www.python.org/downloads/", "snippet": "Download", "date": ""}]}
    monkeypatch.setattr(ClaudeCliProvider, "web_search", web_search)
    tools = hub_loop_runner.web_tools({})                      # no search key in this environment
    assert set(tools) == {"web_search", "read_page"}
    assert "https://www.python.org/downloads/" in tools["web_search"].run("latest python") and asked == ["latest python"]
    assert hub_loop_runner.web_tools({"WEB_SEARCH_ENABLED": "false"}) == {}      # the existing switch turns the fallback off too
    assert hub_loop_runner.web_tools({"QAR_WEB_SEARCH_BACKEND": "claude_login", "WEB_SEARCH_ENABLED": "off"}) == {}


def test_the_hubs_own_loop_is_handed_the_lanes_skills(tmp_path):
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import hub_loop_runner
    assert hub_loop_runner.skill_tools({"QAR_SKILLS_DIR": str(tmp_path / "none")}) == {}
    (tmp_path / "send-mail").mkdir()
    (tmp_path / "send-mail" / "SKILL.md").write_text("---\nname: send-mail\ndescription: How to send a mail.\n---\nUse the wrapper.\n")
    tools = hub_loop_runner.skill_tools({"QAR_SKILLS_DIR": str(tmp_path)})
    assert tools["list_skills"].run("") == "send-mail: How to send a mail." and "Use the wrapper." in tools["read_skill"].run("send-mail")
    assert tools["read_skill"].run("../etc").startswith("error") and tools["read_skill"].run("missing").startswith("error")


def test_no_eligible_arm_or_missing_program_falls_back_to_the_default(programs, picks, monkeypatch):
    table, _ = picks
    assert type(resolve_deep_runner(cfg(deep_providers=["openai"]))) is SubprocessGoalRunner   # nothing eligible: the default
    table[frozenset({"gemini"})] = arm("opencode_runner", "gemini")
    os.remove(programs / "opencode")
    assert type(resolve_deep_runner(cfg(deep_providers=["gemini"]))) is SubprocessGoalRunner


def test_hard_work_starts_on_the_tier_measured_best_for_hard_work(programs, picks, monkeypatch):
    table, _ = picks
    table[frozenset({"anthropic"})] = arm("stream_json_goal_runner", "anthropic")
    hard = arm("subprocess_goal_runner", "anthropic", tier="best")
    hard.kind = "hard"
    table[("hard", frozenset({"anthropic"}))] = hard
    monkeypatch.delenv("QAR_DEEP_MODEL_HARD", raising=False)
    config = cfg(model_fallback={"fast": "haiku", "balanced": "sonnet", "best": "opus"})
    config.orchestrator.deep_model_ladder = ["haiku", "sonnet", "opus"]
    resolve_deep_runner(config)
    assert config.orchestrator.deep_difficulty_models["hard"] == "opus"
    off_ladder = cfg(model_fallback={"fast": "haiku", "balanced": "sonnet", "best": "opus"})
    off_ladder.orchestrator.deep_model_ladder = ["haiku", "sonnet"]
    resolve_deep_runner(off_ladder)
    assert off_ladder.orchestrator.deep_difficulty_models["hard"] == "sonnet"   # not on the ladder: the configured start stays
    monkeypatch.setenv("QAR_DEEP_MODEL_HARD", "sonnet")
    pinned = cfg(model_fallback={"best": "opus"})
    pinned.orchestrator.deep_model_ladder = ["haiku", "sonnet", "opus"]
    resolve_deep_runner(pinned)
    assert pinned.orchestrator.deep_difficulty_models["hard"] == "sonnet"   # the operator's choice wins
