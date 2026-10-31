"""Safety review of the lean deep launch, offline (no process, no model): what the lean arm changes and what it must not.

The lean arm (``evaluation/components/deep_runner/launch_experiment/decide.ARMS["B"]``) differs from today's launch in the argv
and nothing else: the prompt, the environment, the working directory, the resume flag, the permission flag, the allowlists and
the MCP config are the same bytes. The worker's needed built-in tools stay allowed; the tools it drops are named here, so a drop
is a decision someone reads, not an accident.
"""
from __future__ import annotations

import json
import subprocess as sp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
import common  # noqa: E402,F401
from deep_runner.launch_experiment import decide  # noqa: E402

from quest_ai_runner.core.deep_launch import DeepLaunchSettings, build_deep_argv  # noqa: E402
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner  # noqa: E402
from quest_ai_runner.core.goal_runner_contract import goal_settings  # noqa: E402

SID = "22222222-2222-4222-8222-222222222222"

# What a deep worker does with its hands: run commands, read, write and edit files, find files and text. Quest operations go
# through the shell (a tool command with the run id in the environment), so no MCP server is needed for them.
NEEDED_TOOLS = {"Bash", "Read", "Edit", "Write", "Glob", "Grep"}
# Built-in tools of the CLI that the lean tool list does NOT carry. Each is a conscious cost of the lean arm.
DROPPED_BY_LEAN_B = {"WebSearch", "WebFetch", "Task", "TodoWrite", "NotebookEdit", "Skill"}
WEB_TOOLS = {"WebSearch", "WebFetch"}


def arm(name):
    spec = dict(decide.ARMS[name])
    spec.pop("cards_in_brief", None)
    if "tools" in spec:
        spec["tools"] = tuple(spec["tools"])
    return DeepLaunchSettings(**spec)


def argv(launch, **over):
    base = dict(streaming=False, skip_permissions=True, allowed_tools=None, disallowed_tools=None, mcp_config_path=None,
                model="sonnet", turns=30, goal_given=True, session_flag="--session-id", session_id=SID, launch=launch)
    base.update(over)
    return build_deep_argv("claude", **base)


def lean_only(cmd_default, cmd_lean):
    """The tokens present in the lean argv and absent from the default one, in order, as one list; and whether removing them
    gives the default argv exactly."""
    extra = []
    rest = list(cmd_lean)
    out = []
    i = 0
    j = 0
    while j < len(rest):
        if i < len(cmd_default) and rest[j] == cmd_default[i]:
            out.append(rest[j]); i += 1; j += 1
        else:
            extra.append(rest[j]); j += 1
    return extra, out == list(cmd_default)


# --- argv: the only difference is the lean flags, byte for byte -------------------------------------------------------

@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("session_flag", ["--session-id", "--resume"])
@pytest.mark.parametrize("extra", [
    {}, {"skip_permissions": False, "allowed_tools": ["Bash", "Read", "WebFetch"], "disallowed_tools": ["NotebookEdit"],
         "mcp_config_path": "/x/mcp.json"}])
def test_lean_argv_equals_today_s_plus_exactly_the_lean_flags(streaming, session_flag, extra):
    today = argv(None, streaming=streaming, session_flag=session_flag, **extra)
    lean = argv(arm("B"), streaming=streaming, session_flag=session_flag, **extra)
    added, rest_equal = lean_only(today, lean)
    assert rest_equal, "a token of today's launch went missing or moved"
    assert added == ["--tools", "Bash,Read,Edit,Write,Glob,Grep", "--setting-sources", "", "--strict-mcp-config",
                     "--exclude-dynamic-system-prompt-sections", "--max-budget-usd", "3"]
    # the lean flags sit together, immediately before --model, so nothing that follows (turn cap, session flag) moves
    at = lean.index("--tools")
    assert lean[at:at + len(added)] == added and lean[at + len(added)] == "--model"
    assert lean[-2:] == [session_flag, SID]


def test_a_resumed_launch_repeats_the_lean_flags_because_resume_does_not_carry_them():
    first = argv(arm("B"))
    resumed = argv(arm("B"), session_flag="--resume")
    assert first[first.index("--tools"):first.index("--model")] == resumed[resumed.index("--tools"):resumed.index("--model")]
    assert resumed[-2:] == ["--resume", SID]


def test_the_lean_arm_never_changes_permission_allowlist_mcp_or_model_arguments():
    kw = dict(skip_permissions=False, allowed_tools=["Bash", "Read"], disallowed_tools=["WebFetch"], mcp_config_path="/x/m.json")
    today, lean = argv(None, **kw), argv(arm("B"), **kw)
    for flag in ("--allowed-tools", "--disallowed-tools", "--mcp-config", "--model", "--max-turns", "-p", "--output-format"):
        assert today[today.index(flag):today.index(flag) + 2] == lean[lean.index(flag):lean.index(flag) + 2], flag
    assert ("--dangerously-skip-permissions" in today) == ("--dangerously-skip-permissions" in lean)
    kw["skip_permissions"] = True
    assert "--dangerously-skip-permissions" in argv(arm("B"), **kw)


# --- the runner: argv is the ONLY thing that differs between the arms ----------------------------------------------------

def run_capturing(monkeypatch, launch, output=b'{"result": "done"}'):
    seen = {}

    class FakePopen:
        returncode = 0
        stdin = None

        def communicate(self, input=None, timeout=None):
            seen["stdin"] = input
            return (output, b"")

    def fake(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return FakePopen()
    monkeypatch.setattr(sp, "Popen", fake)
    monkeypatch.setenv("QAR_TEST_MARK", "same")
    cfg = SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude", launch=launch)
    res = SubprocessGoalRunner(cfg).run_goal(goal="make a file", brief="the brief", max_turns=7, run_id="run_1",
                                             requester_user_id="u_1")
    return seen, res


def test_prompt_environment_cwd_and_process_options_are_identical_between_arms(monkeypatch):
    a, _ = run_capturing(monkeypatch, DeepLaunchSettings())
    b, _ = run_capturing(monkeypatch, arm("B"))
    assert a["stdin"] == b["stdin"] and a["stdin"]
    assert a["kw"] == b["kw"]
    assert a["kw"]["env"].get("QAR_TEST_MARK") == "same"
    assert a["kw"]["cwd"] == "/w" and a["kw"]["start_new_session"] is True
    same_session = lambda cmd: [SID if t == cmd[-1] else t for t in cmd]  # the session id is a fresh uuid per run
    assert a["cmd"] != b["cmd"]
    assert lean_only(same_session(a["cmd"]), same_session(b["cmd"]))[1]


def test_the_escalation_marker_in_the_worker_output_is_read_the_same_under_the_lean_launch(monkeypatch):
    marker = goal_settings().escalation_marker
    if not marker:
        pytest.skip("this configuration sets no escalation marker")
    out = json.dumps({"result": f"Need a human. {marker} dec_0123456789"}).encode()
    a, ra = run_capturing(monkeypatch, DeepLaunchSettings(), output=out)
    b, rb = run_capturing(monkeypatch, arm("B"), output=out)
    assert ra.decision_id == rb.decision_id == "dec_0123456789"
    assert not ra.met and not rb.met


# --- tools: what is kept and what is dropped, named ---------------------------------------------------------------------

def test_every_tool_the_worker_needs_for_files_and_commands_stays_allowed_in_the_lean_arm():
    assert NEEDED_TOOLS <= set(arm("B").tools)


def test_the_tools_the_lean_arm_drops_are_exactly_the_named_ones():
    all_known = NEEDED_TOOLS | DROPPED_BY_LEAN_B
    assert set(arm("B").tools) | DROPPED_BY_LEAN_B == all_known
    assert not (set(arm("B").tools) & DROPPED_BY_LEAN_B)


def test_lean_arm_b_makes_the_web_tools_unreachable_while_the_config_still_advertises_web():
    """The honest gap (a finding, not a pass): today's default tool set has WebSearch and WebFetch and ``web_enabled()`` is
    True; arm B drops both, so a goal that needs the web would fail while the planner believes the worker can browse."""
    cfg = SubprocessConfig(working_dir="/w", launch=arm("B"))
    assert cfg.web_enabled() is True
    assert not (WEB_TOOLS & set(arm("B").tools))


def test_an_explicit_allowlist_naming_a_dropped_tool_is_a_conflict_the_caller_can_detect():
    cfg_allowed = ["Bash", "Read", "WebFetch"]
    missing = set(cfg_allowed) - set(arm("B").tools)
    assert missing == {"WebFetch"}


# --- setting sources: what "load none" removes -----------------------------------------------------------------------

def test_the_lean_arm_loads_no_setting_source_which_includes_user_hooks_and_environment():
    """``--setting-sources ""`` is user, project and local all removed. A deployment that keeps its safety hooks (a path guard, a
    destructive-delete guard) or its model overrides in the USER settings loses them under this arm; the deep worker runs with
    ``--dangerously-skip-permissions``, so those hooks are its only guard. Arm D keeps ``user`` for that reason."""
    assert arm("B").setting_sources == ""
    cmd = argv(arm("B"))
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--dangerously-skip-permissions" in cmd


# --- the selectable profile that keeps the guards ---------------------------------------------------------------------------

from quest_ai_runner.config_core_settings import deep_launch_from_env  # noqa: E402
from quest_ai_runner.core.deep_launch import LEAN_GUARDED, PROFILES  # noqa: E402


def test_no_profile_is_today_s_launch_and_the_profile_is_opt_in_by_name():
    assert deep_launch_from_env({}) == DeepLaunchSettings()
    assert deep_launch_from_env({"QAR_DEEP_LAUNCH_PROFILE": ""}) == DeepLaunchSettings()
    assert deep_launch_from_env({"QAR_DEEP_LAUNCH_PROFILE": "nonsense"}) == DeepLaunchSettings()
    assert deep_launch_from_env({"QAR_DEEP_LAUNCH_PROFILE": " Lean_Guarded "}) == LEAN_GUARDED
    assert PROFILES == {"lean_guarded": LEAN_GUARDED}


def test_a_set_qar_deep_name_overrides_the_profile_field_it_names():
    got = deep_launch_from_env({"QAR_DEEP_LAUNCH_PROFILE": "lean_guarded", "QAR_DEEP_SETTING_SOURCES": "user,project",
                                "QAR_DEEP_MAX_BUDGET_USD": "2", "QAR_DEEP_STRICT_MCP": "0"})
    assert got.setting_sources == "user,project" and got.max_budget_usd == 2.0 and got.strict_mcp is False
    assert got.tools == LEAN_GUARDED.tools


def test_the_profile_is_the_experiment_s_arm_e():
    assert arm("E") == LEAN_GUARDED


def test_the_profile_keeps_what_arm_b_lost():
    assert NEEDED_TOOLS <= set(LEAN_GUARDED.tools)
    assert WEB_TOOLS <= set(LEAN_GUARDED.tools)           # so web_enabled() stays true to what the worker can do
    assert LEAN_GUARDED.setting_sources == "user"          # the user settings' hooks and model overrides still load
    assert "user" in LEAN_GUARDED.setting_sources.split(",")
    assert LEAN_GUARDED.max_budget_usd is None             # no new way for a long goal to stop
    assert LEAN_GUARDED.max_turns is None                  # the call's own turn budget stands
    assert LEAN_GUARDED.strict_mcp and LEAN_GUARDED.exclude_dynamic_sections


def test_the_profile_argv_is_today_s_plus_exactly_its_flags():
    today = argv(None)
    lean = argv(LEAN_GUARDED)
    added, rest_equal = lean_only(today, lean)
    assert rest_equal
    assert added == ["--tools", "Bash,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch", "--setting-sources", "user",
                     "--strict-mcp-config", "--exclude-dynamic-system-prompt-sections"]


def test_the_profile_changes_nothing_but_argv_at_the_process_edge(monkeypatch):
    a, _ = run_capturing(monkeypatch, DeepLaunchSettings())
    b, _ = run_capturing(monkeypatch, LEAN_GUARDED)
    assert a["stdin"] == b["stdin"] and a["kw"] == b["kw"]
