"""The deep worker's launch parameters (``core/deep_launch.py``): offline, no process, no model call.

The default settings must give byte for byte the argv the runner built before the lean flags became parameters; each parameter
adds exactly its flag; the environment names parse into the same value the runner config takes.
"""
from __future__ import annotations

import subprocess as _sp

import pytest

from quest_ai_runner.config_core_settings import deep_launch_from_env
from quest_ai_runner.core.deep_launch import DeepLaunchSettings, build_deep_argv, lean_flags
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from quest_ai_runner.core.goal_runner_contract import resolve_deep_launch
from quest_ai_runner.core.runtime_settings import CoreSettings

SID = "11111111-1111-4111-8111-111111111111"


def argv(**over):
    base = dict(streaming=False, skip_permissions=True, allowed_tools=None, disallowed_tools=None, mcp_config_path=None,
                model="sonnet", turns=30, goal_given=True, session_flag="--session-id", session_id=SID)
    base.update(over)
    return build_deep_argv("claude", **base)


# --- the default is today's launch, byte for byte --------------------------------------------------------------------

# These lists are what core/goal_runner.py and core/goal_runner_stream.py built before the builder existed.
TODAY_ONE_SHOT = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions", "--model", "sonnet",
                  "--max-turns", "30", "--session-id", SID]
TODAY_STREAM = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--replay-user-messages", "--dangerously-skip-permissions", "--model", "sonnet", "--max-turns", "30",
                "--session-id", SID]


def test_default_one_shot_argv_is_today_s():
    assert argv() == TODAY_ONE_SHOT
    assert argv(launch=DeepLaunchSettings()) == TODAY_ONE_SHOT


def test_default_stream_argv_is_today_s():
    assert argv(streaming=True) == TODAY_STREAM
    assert argv(streaming=True, launch=DeepLaunchSettings()) == TODAY_STREAM


def test_default_with_every_existing_option_is_today_s():
    got = argv(skip_permissions=False, allowed_tools=["Read", "Edit"], disallowed_tools=["WebFetch"],
               mcp_config_path="/etc/mcp.json", session_flag="--resume")
    assert got == ["claude", "-p", "--output-format", "json", "--allowed-tools", "Read,Edit", "--disallowed-tools", "WebFetch",
                   "--mcp-config", "/etc/mcp.json", "--model", "sonnet", "--max-turns", "30", "--resume", SID]


def test_an_empty_goal_passes_no_turn_cap_even_with_an_override():
    assert "--max-turns" not in argv(goal_given=False)
    assert "--max-turns" not in argv(goal_given=False, launch=DeepLaunchSettings(max_turns=6))


def test_the_default_settings_add_no_flag_and_say_so():
    assert lean_flags(DeepLaunchSettings()) == [] and DeepLaunchSettings().is_default()
    assert DeepLaunchSettings().describe() == "default"


# --- each parameter -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("settings, added", [
    (DeepLaunchSettings(tools=("Bash", "Read", "Edit", "Write")), ["--tools", "Bash,Read,Edit,Write"]),
    (DeepLaunchSettings(tools=()), ["--tools", ""]),
    (DeepLaunchSettings(setting_sources=""), ["--setting-sources", ""]),
    (DeepLaunchSettings(setting_sources="user"), ["--setting-sources", "user"]),
    (DeepLaunchSettings(strict_mcp=True), ["--strict-mcp-config"]),
    (DeepLaunchSettings(exclude_dynamic_sections=True), ["--exclude-dynamic-system-prompt-sections"]),
    (DeepLaunchSettings(max_budget_usd=2.5), ["--max-budget-usd", "2.5"]),
    (DeepLaunchSettings(max_budget_usd=3), ["--max-budget-usd", "3"]),
])
def test_each_parameter_adds_exactly_its_flag_before_the_model(settings, added):
    for streaming, today in ((False, TODAY_ONE_SHOT), (True, TODAY_STREAM)):
        got = argv(streaming=streaming, launch=settings)
        at = today.index("--model")
        assert got == today[:at] + added + today[at:], settings.describe()
        assert not settings.is_default()


def test_a_non_positive_budget_adds_no_flag():
    assert argv(launch=DeepLaunchSettings(max_budget_usd=0)) == TODAY_ONE_SHOT
    assert argv(launch=DeepLaunchSettings(max_budget_usd=-1)) == TODAY_ONE_SHOT


def test_max_turns_replaces_the_calls_budget_when_set():
    got = argv(launch=DeepLaunchSettings(max_turns=6))
    assert got[got.index("--max-turns") + 1] == "6"
    assert got.count("--max-turns") == 1
    unset = argv(launch=DeepLaunchSettings(max_turns=None), turns=12)
    assert unset[unset.index("--max-turns") + 1] == "12"


def test_all_lean_parameters_together_and_their_label():
    lean = DeepLaunchSettings(tools=("Bash", "Read"), setting_sources="", strict_mcp=True, exclude_dynamic_sections=True,
                              max_budget_usd=1.5, max_turns=6)
    assert lean_flags(lean) == ["--tools", "Bash,Read", "--setting-sources", "", "--strict-mcp-config",
                                "--exclude-dynamic-system-prompt-sections", "--max-budget-usd", "1.5"]
    assert lean.describe() == ("tools=Bash+Read,setting_sources=none,strict_mcp=1,exclude_dynamic_sections=1,"
                               "max_budget_usd=1.5,max_turns=6")


def test_the_builder_is_pure():
    lean = DeepLaunchSettings(tools=("Read",), strict_mcp=True)
    assert argv(launch=lean) == argv(launch=lean)
    with pytest.raises(Exception):
        lean.strict_mcp = False  # frozen


# --- the environment names -----------------------------------------------------------------------------------------

def test_unset_environment_is_today_s_launch():
    assert deep_launch_from_env({}) == DeepLaunchSettings()
    assert deep_launch_from_env({"QAR_DEEP_TOOLS": " ", "QAR_DEEP_SETTING_SOURCES": "", "QAR_DEEP_STRICT_MCP": "0",
                                 "QAR_DEEP_MAX_BUDGET_USD": "nope"}) == DeepLaunchSettings()


def test_environment_names_parse_into_the_settings():
    got = deep_launch_from_env({"QAR_DEEP_TOOLS": "Bash, Read,Edit", "QAR_DEEP_SETTING_SOURCES": "none",
                                "QAR_DEEP_STRICT_MCP": "1", "QAR_DEEP_EXCLUDE_DYNAMIC_SECTIONS": "true",
                                "QAR_DEEP_MAX_BUDGET_USD": "2"})
    assert got == DeepLaunchSettings(tools=("Bash", "Read", "Edit"), setting_sources="", strict_mcp=True,
                                     exclude_dynamic_sections=True, max_budget_usd=2.0)
    assert deep_launch_from_env({"QAR_DEEP_TOOLS": "none"}).tools == ()
    assert deep_launch_from_env({"QAR_DEEP_SETTING_SOURCES": "user,project"}).setting_sources == "user,project"


def test_runner_config_wins_over_the_environment_settings(monkeypatch):
    from quest_ai_runner.core import runtime_settings
    monkeypatch.setattr(runtime_settings, "_settings_source", CoreSettings(deep_launch=DeepLaunchSettings(strict_mcp=True)))
    assert resolve_deep_launch(None).strict_mcp is True
    pinned = DeepLaunchSettings(exclude_dynamic_sections=True)
    assert resolve_deep_launch(pinned) == pinned


# --- the runner uses the builder on both paths ----------------------------------------------------------------------

def capture(monkeypatch):
    seen = {}

    class FakePopen:
        returncode = 0
        stdin = None

        def communicate(self, input=None, timeout=None):
            return (b'{"result": "did it"}', b"")

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return FakePopen()
    monkeypatch.setattr(_sp, "Popen", fake)
    return seen


def test_the_one_shot_runner_default_launch_adds_no_lean_flag(monkeypatch):
    from quest_ai_runner.core import runtime_settings
    monkeypatch.setattr(runtime_settings, "_settings_source", CoreSettings())
    seen = capture(monkeypatch)
    SubprocessGoalRunner(SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude")).run_goal(
        goal="g", brief="b", max_turns=2)
    cmd = seen["cmd"]
    assert cmd[:5] == ["/usr/bin/claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
    for flag in ("--tools", "--setting-sources", "--strict-mcp-config", "--exclude-dynamic-system-prompt-sections",
                 "--max-budget-usd"):
        assert flag not in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "2"


def test_the_one_shot_runner_passes_the_configured_launch(monkeypatch):
    seen = capture(monkeypatch)
    cfg = SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude",
                           launch=DeepLaunchSettings(tools=("Read", "Edit"), strict_mcp=True, max_budget_usd=1.0, max_turns=6))
    SubprocessGoalRunner(cfg).run_goal(goal="g", brief="b", max_turns=30)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "Read,Edit"
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--max-budget-usd") + 1] == "1"
    assert cmd[cmd.index("--max-turns") + 1] == "6"
