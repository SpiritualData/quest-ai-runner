"""The hub's session contract, for the terminal: the events of a deep run (any runner) as session events, and the lines the terminal writes for them.

``TurnSession`` is kept for one turn. ``take(event)`` reads one ``ProgressEvent`` of the run (a ``session`` event the hub loop reports, a raw
``stream`` line, a question) through the hub's adapters, keeps the hub's ``SessionView`` current and returns the transcript lines that event adds: the
solution and milestone the run serves, a helper starting and ending under its parent, a step that was blocked and why, a question the run waits on.
``closing_lines()`` is the helper tree and what was blocked, written once before the turn's closing line. ``support_of(runner)`` says which controls a
runner honours, and ``unsupported_line`` words, in one plain line, what a control cannot do with this one. Standard library and the hub's names only."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, List, Optional

from hubofhubs.api import (
    AgentEnded, AgentStarted, Finished, RunStarted, SessionView, Step, StreamJson, Waiting, progress_events)


@dataclass(frozen=True)
class RunSupport:
    """What a runner lets a person do while it works."""
    stop: bool = True
    steer: bool = False
    pause: bool = False
    helpers: bool = False


def support_of(runner: Any) -> RunSupport:
    """Which controls ``runner`` honours, read from how it is built: it takes a ``control`` (messages and pause reach it live), reports the contract's own
    events (``emits_session``: helpers and steps as they happen), or has a ``steer`` of its own."""
    try:
        takes_control = "control" in inspect.signature(runner.run_goal).parameters
    except (AttributeError, TypeError, ValueError):
        takes_control = False
    return RunSupport(stop=True, steer=takes_control or callable(getattr(runner, "steer", None)), pause=takes_control,
                      helpers=bool(getattr(runner, "emits_session", False)))


def unsupported_line(action: str, support: RunSupport) -> Optional[str]:
    """One plain line saying a control cannot reach this runner while it works, or None when it can."""
    if action == "steer" and not support.steer:
        return "This worker reads your note when its current step ends; it cannot take it while it works."
    if action == "pause" and not support.pause:
        return "This worker cannot be paused; Esc stops it and keeps what it did."
    if action == "helpers" and not support.helpers:
        return "This worker does not report its helpers, so none are shown."
    return None


class TurnSession:
    """The session contract for one turn of the terminal."""

    def __init__(self) -> None:
        self.view = SessionView()
        self.stream = StreamJson()

    def take(self, event: Any) -> List[str]:
        lines: List[str] = []
        for item in progress_events(event, stream=self.stream):
            self.view.apply(item)
            lines.extend(self.lines_for(item))
        return lines

    def lines_for(self, item: Any) -> List[str]:
        if isinstance(item, RunStarted):
            if item.solution or item.milestone:
                return [f"Serves {item.solution or 'a solution'}" + (f": {item.milestone}" if item.milestone else "")]
            return []
        if isinstance(item, AgentStarted):
            depth = self.depth(item.agent_id)
            return [f"{'  ' * depth}↳ helper: {item.task}" + (f" ({item.solution})" if item.solution else "")]
        if isinstance(item, AgentEnded):
            agent = self.view.agents.get(item.agent_id)
            task = agent.task if agent else item.agent_id
            mark = {"met": "✓", "unmet": "~", "stopped": "~", "failed": "✗", "not_started": "·"}.get(item.state, "~")
            return [f"{'  ' * self.depth(item.agent_id)}{mark} helper {item.state.replace('_', ' ')}: {task}"]
        if isinstance(item, Step) and item.state == "blocked":
            return [f"⊘ blocked: {item.label}" + (f" ({item.detail})" if item.detail else "")]
        if isinstance(item, Waiting):
            return [f"? {item.question}" + (f"  It will change: {item.preview}" if item.preview else "")]
        return []

    def depth(self, agent_id: str) -> int:
        depth, seen = 0, set()
        agent = self.view.agents.get(agent_id)
        while agent is not None and agent.parent_id in self.view.agents and agent.parent_id not in seen:
            seen.add(agent.parent_id)
            depth += 1
            agent = self.view.agents[agent.parent_id]
        return depth

    def closing_lines(self) -> List[str]:
        """The helper tree and what was blocked, for the end of the turn; empty when the run had neither."""
        out: List[str] = []
        if self.view.final is None:      # the turn is over: the tree must not say the main agent still runs
            self.view.apply(Finished())
        if self.view.agents:
            out.append("Helpers:")
            out.extend("  " + line for line in self.view.tree_lines(glyphs=True))
        blocked = self.view.blocked()
        if blocked:
            out.append("Blocked:")
            out.extend("  " + row for row in blocked)
        return out
