"""The hub's frame on the model's own tool loop as a deep runner, QAR's binding of the hub's (``hubofhubs.api.NativeToolLoopRunner``).

One lean Claude Code session with its own trained tools, prepared (files in view, what is left out, a short plan, discover before asking) and checked
(the code check of written text) by the hub; stop, steer and resume are the stream runner's. ``NativeLoopRunner`` here is the hub's class with QAR's
settings (``goal_runner_contract.goal_settings``), named ``native_loop``. It is on trial: the hub never hands it out as its pick, so a lane runs it only
when its configuration names it (``selections["delegated-agent-sessions"] = "native_loop"``, or the ``ESOI_SELECT_QUEST_AI_RUNNER_DELEGATED_AGENT_SESSIONS``
variable).

What the session has and lacks next to ``hub_loop``: it has its own Bash, Read, Edit, Write, Glob, Grep, WebSearch and WebFetch (so the lane's web
search backend is not used), and it reaches the lane's operations the way the Claude Code arms do, through ``python -m quest_ai_runner`` run in Bash. It
lacks the in-process ``USE:`` tools (``create_decision``, ``add_quest_note`` run with the run's requester behind them), because a native session takes
tools only as MCP servers and no lane MCP server exists; it also loads no skills of its own (no settings sources), so the frame names the skills folder
for it to read. A decision raised through the command line is still reported by the ``QAR-ESCALATED`` marker the session ends with.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from esoi import implementation

from hubofhubs.api import GoalSettings, NativeToolLoopRunner as HubNativeToolLoopRunner

from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessConfig, SubprocessGoalRunner, goal_settings
from quest_ai_runner.the_world.tools.delegated_agent_sessions.hub_loop_runner import skills_folder


def skills_note(env: Any) -> str:
    """The frame's sentence about the lane's skills (runbooks, one folder per skill holding ``SKILL.md``), or "" when the lane has none."""
    folder = skills_folder(env)
    if folder is None:
        return ""
    return (f"This lane keeps skills (runbooks for kinds of task, a person or a system) in {folder}: one folder per skill with a SKILL.md. "
            "List them with Bash (ls) and read the ones the goal ties to before you do that kind of work.")


@implementation("delegated-agent-sessions", "native_loop")
class NativeLoopRunner(HubNativeToolLoopRunner, SubprocessGoalRunner):
    """Deep runner that works a goal in one hub-framed Claude Code session on its own tools (with QAR's settings)."""

    config_class = SubprocessConfig

    def __init__(self, config: Optional[SubprocessConfig] = None, **arguments: Any):
        arguments.setdefault("notes", skills_note(os.environ))
        super().__init__(config, **arguments)

    def goal_settings(self) -> GoalSettings:
        """QAR's goal settings, read from the environment at call time (QAR is the consumer that owns its settings)."""
        return goal_settings()
