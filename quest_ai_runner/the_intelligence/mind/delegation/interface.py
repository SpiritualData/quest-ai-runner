"""The interfaces of DeepSessions, StallDetecting, InputInbox: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping
from esoi import solution
from typing import List, Protocol, runtime_checkable
from hubofhubs.api import AgentSessions


@solution("delegated-agent-sessions", kind="event", entry="run_goal",
          example={"goal": "read notes.txt and tell me which city the meeting is in", "brief": "read the file, then report the city"})
@typing.runtime_checkable
class DeepSessions(AgentSessions, typing.Protocol):
    """The hub's ``delegated-agent-sessions`` interface (one goal run in an agent's own session, reported back as a ``DeepResult``) over the
    runners QAR selects between: the one-shot subprocess, the stream-json and ACP sessions, and the fast-edit and MCP-write runners."""


@solution("detect-a-stalled-run", kind="event", entry="review", example={"elapsed": "12 minutes", "idle": "9 minutes", "tail": "(no output)"})
@typing.runtime_checkable
class StallDetecting(typing.Protocol):
    """Tells a stuck run from a slow one: from the elapsed time, the idle time and the tail of the session, whether the worker should still run."""

    def review(self, elapsed: str, idle: str, tail: str) -> Mapping[str, Any]: ...


@solution("receive-input-mid-run", kind="event", entry="peek", example="conversation-1")
@runtime_checkable
class InputInbox(Protocol):
    """A conversation-keyed inbox of user messages that arrived mid-run."""

    def push(self, conversation_id: str, message: str) -> None:
        """Record a new user message for ``conversation_id`` (interface side)."""

    def drain(self, conversation_id: str) -> List[str]:
        """Return and CLEAR the pending messages for ``conversation_id`` (orchestrator side)."""

    def peek(self, conversation_id: str) -> List[str]:
        """Return the pending messages for ``conversation_id`` WITHOUT clearing them.

        For an interface that wants to show "N messages queued" while a run is still in
        flight (a busy indicator, a placeholder count) without competing with the
        orchestrator's own ``drain`` for the same messages.
        """
