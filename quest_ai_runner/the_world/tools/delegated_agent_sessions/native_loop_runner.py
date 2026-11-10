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
for it to read. The frame tells the session the exact command, the argument names and the ``QAR-ESCALATED:`` last-line rule (``lane_note``), so a decision raised through the command line is still reported by the marker the session ends with.
Ceilings, permission mode and sandbox are the hub class's own constructor settings; a lane sets them in its ``[deep_runner_options]`` table. A run a ceiling shaped is reported incomplete, never done (``mark_ceiling_stop``).
"""
from __future__ import annotations

import os
import shlex
import sys
from typing import Any, Mapping, Optional

from esoi import implementation

from hubofhubs.api import GoalSettings, NativeToolLoopRunner as HubNativeToolLoopRunner

from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import ESCALATION_MARKER, SubprocessConfig, SubprocessGoalRunner, goal_settings
from quest_ai_runner.the_world.tools.delegated_agent_sessions.hub_loop_runner import argument_shape, skills_folder


def skills_note(env: Any) -> str:
    """The frame's sentence about the lane's skills (runbooks, one folder per skill holding ``SKILL.md``), or "" when the lane has none."""
    folder = skills_folder(env)
    if folder is None:
        return ""
    return (f"This lane keeps skills (runbooks for kinds of task, a person or a system) in {folder}: one folder per skill with a SKILL.md. "
            "List them with Bash (ls) and read the ones the goal ties to before you do that kind of work.")


def lane_note(env: Mapping[str, str], registry: Any = None, python: Optional[str] = None) -> str:
    """The frame's text on the lane's own operations: how to add a note to a goal and raise a decision for a person from Bash, and the line a run ends with after
    it raised one. A native session has no in-process lane tools and loads no CLAUDE.md or skill of its own, so without this it would not know the command, the
    argument names or the ``QAR-ESCALATED`` convention. Built from the tool registry the lane itself uses (the exact argument names), and empty when the lane
    has neither operation (no Quest credentials): the frame never names a tool the session could not call."""
    try:
        from quest_ai_runner.the_world.tools.tools import build_tool_registry
        registry = registry or build_tool_registry(env)
    except Exception:  # noqa: BLE001 -- a lane without the catalog still runs deep work
        return ""
    note, decision = registry.get("add_quest_note"), registry.get("create_decision")
    if note is None and decision is None:
        return ""
    command = f"{shlex.quote(python or sys.executable)} -m quest_ai_runner tools"
    lines = [f"This lane's own operations run from Bash: `{command} call <name> --kw arg=value` (repeat --kw for each argument; `--args '<one JSON object>'` "
             f"for nested values). `{command} list --query '<what you need>'` finds more and `{command} show <name>` prints one tool's arguments. A call exits 0 when "
             "the request was taken and 1 when it was refused or failed (the reason is printed). When the brief names this run's quest or task id, add "
             "`--quest <id>` and `--task <id>` to the call."]
    if note is not None:
        lines.append(f"- Add a note to a goal or quest: `{command} call add_quest_note ...`, arguments {argument_shape(note.parameters)}. Write findings there.")
    if decision is not None:
        lines.append(f"- Raise a decision request for a person (payment, identity, anything irreversible, taste): `{command} call create_decision ...`, arguments "
                     f"{argument_shape(decision.parameters)}. It prints the decision id. Do not do the act that needs a person yourself.")
        lines.append(f"- After you raised a decision, the LAST line of your final answer must be exactly `{ESCALATION_MARKER} <decision id>` (the id the call printed), "
                     "with nothing after it. Without that line the run is reported as finished.")
    return "\n".join(lines)


@implementation("delegated-agent-sessions", "native_loop")
class NativeLoopRunner(HubNativeToolLoopRunner, SubprocessGoalRunner):
    """Deep runner that works a goal in one hub-framed Claude Code session on its own tools (with QAR's settings)."""

    config_class = SubprocessConfig

    def __init__(self, config: Optional[SubprocessConfig] = None, **arguments: Any):
        arguments.setdefault("notes", "\n\n".join(part for part in (skills_note(os.environ), lane_note(os.environ)) if part))
        super().__init__(config, **arguments)

    def goal_settings(self) -> GoalSettings:
        """QAR's goal settings, read from the environment at call time (QAR is the consumer that owns its settings)."""
        return goal_settings()
