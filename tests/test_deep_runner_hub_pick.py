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
    assert type(runner) is HubLoopRunner
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


def test_the_hubs_hard_work_pick_no_longer_moves_the_start_and_sub_agents_follow_the_hubs_tier(programs, picks, monkeypatch):
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser import deep_model_selection as selection
    table, _ = picks
    table[frozenset({"anthropic"})] = arm("stream_json_goal_runner", "anthropic")
    hard = arm("subprocess_goal_runner", "anthropic", tier="best")
    hard.kind = "hard"
    table[("hard", frozenset({"anthropic"}))] = hard
    config = cfg(model_fallback={"fast": "haiku", "balanced": "sonnet", "quality": "opus", "best": "opus"})
    config.orchestrator.deep_model_ladder = ["haiku", "sonnet", "opus"]
    try:
        resolve_deep_runner(config)
        assert config.orchestrator.deep_difficulty_models == {}   # nothing is written for the start: the hub rule decides per run
        assert selection.sub_agent_environment({}).get(selection.SUB_AGENT_ENV) == "haiku"   # the hub's sub-agent choice, through the registry
    finally:
        selection.install_sub_agent_model(None)


def test_the_native_tool_loop_runs_only_when_named(programs, tmp_path, monkeypatch):
    from hubofhubs.api import NativeToolLoopRunner, choose_agent_arm
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import native_loop_runner
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("description: a demo\n")
    monkeypatch.setenv("QAR_SKILLS_DIR", str(tmp_path / "skills"))
    runner = resolve_deep_runner(cfg(selections={"delegated-agent-sessions": "native_loop"}))
    assert type(runner) is native_loop_runner.NativeLoopRunner and isinstance(runner, NativeToolLoopRunner)
    assert runner.goal_settings().escalation_marker                 # QAR's settings, not the hub's empty defaults
    assert str(tmp_path / "skills") in runner.notes                 # the frame names the lane's skills folder (a native session loads none of its own)


def test_the_native_tool_loop_note_is_empty_without_a_skills_folder(tmp_path):
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import native_loop_runner
    assert native_loop_runner.skills_note({"QAR_SKILLS_DIR": str(tmp_path / "none"), "HOME": str(tmp_path / "nohome")}) == ""


def make_skills(tmp_path, monkeypatch):
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: demo\ndescription: a demo skill\n---\nbody\n")
    monkeypatch.setenv("QAR_SKILLS_DIR", str(tmp_path / "skills"))
    return tmp_path / "skills"


def test_the_hubs_solution_agents_pick_builds_the_binding_with_the_skills_folder(programs, picks, tmp_path, monkeypatch):
    from hubofhubs.api import SolutionAgentsNativeRunner
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import solution_agents_loop_runner
    folder = make_skills(tmp_path, monkeypatch)
    table, asked = picks
    table[frozenset({"anthropic"})] = arm("solution_agents_native_runner", "anthropic", tier="balanced")
    runner = resolve_deep_runner(cfg())
    assert type(runner) is solution_agents_loop_runner.SolutionAgentsLoopRunner and isinstance(runner, SolutionAgentsNativeRunner)
    assert runner.tier == "balanced" and runner.goal_settings().escalation_marker
    assert runner.skills_dir == str(folder)


def test_the_solution_agents_loop_runs_when_named(programs, tmp_path, monkeypatch):
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import solution_agents_loop_runner
    folder = make_skills(tmp_path, monkeypatch)
    runner = resolve_deep_runner(cfg(selections={"delegated-agent-sessions": "solution_agents_loop"}))
    assert type(runner) is solution_agents_loop_runner.SolutionAgentsLoopRunner
    assert runner.skills_dir == str(folder)                 # the index is the hub's section, not a note
    assert str(folder) not in runner.notes                 # the plain skills sentence of native_loop is not written


def test_the_hubs_tier_reaches_the_native_loop_binding(programs, picks):
    """``NativeLoopRunner`` names only ``**arguments``; the pick's provider, tier and model translator must still reach the hub class behind it."""
    from quest_ai_runner.the_world.tools.delegated_agent_sessions import native_loop_runner
    table, _ = picks
    table[frozenset({"anthropic"})] = arm("native_tool_loop_runner", "anthropic", tier="balanced")
    runner = resolve_deep_runner(cfg())
    assert type(runner) is native_loop_runner.NativeLoopRunner
    assert runner.tier == "balanced" and runner.provider == "anthropic" and runner.model_translator is not None
