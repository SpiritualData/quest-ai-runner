"""Conversation input inbox — the generic abstraction for mid-run user messages.

A goal loop can run for a while. If the user sends more messages while it works, the next process
should act on them. Rather than make every interface (the terminal chat, the Quest frontend, any
other UI on top of QAR) hand-wire a ``pending_inputs`` callable, QAR owns a small, generic inbox:

  * The interface PUSHES each new user message as it arrives, keyed by a conversation id.
  * The orchestrator DRAINS that conversation's pending messages between goal-loop steps,
    automatically, when an inbox is wired (see ``Orchestrator.run``).

This keeps the mechanism uniform and app-agnostic: no Quest/chat specifics live here, so the same
inbox serves the chat, the Quest frontend, and anything else built on QAR. The only per-interface
integration is the one-line ``inbox.push(conversation_id, message)`` when a message comes in.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Protocol, runtime_checkable


@runtime_checkable
class InputInbox(Protocol):
    """A conversation-keyed inbox of user messages that arrived mid-run."""

    def push(self, conversation_id: str, message: str) -> None:
        """Record a new user message for ``conversation_id`` (interface side)."""

    def drain(self, conversation_id: str) -> List[str]:
        """Return and CLEAR the pending messages for ``conversation_id`` (orchestrator side)."""


class InMemoryInbox:
    """Thread-safe, in-process ``InputInbox``. The default wired by ``build_orchestrator``.

    Suitable when the interface and the orchestrator share a process (the chat, a single-process
    backend). A distributed deployment can supply its own ``InputInbox`` (e.g. Redis-backed) with
    the same two methods; the orchestrator only depends on the protocol.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_conv: Dict[str, List[str]] = {}

    def push(self, conversation_id: str, message: str) -> None:
        if not conversation_id or not (message or "").strip():
            return
        with self._lock:
            self._by_conv.setdefault(str(conversation_id), []).append(str(message).strip())

    def drain(self, conversation_id: str) -> List[str]:
        if not conversation_id:
            return []
        with self._lock:
            return self._by_conv.pop(str(conversation_id), [])
