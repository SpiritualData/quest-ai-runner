"""ChatSessionStore: the ConversationStore for an active chat session (shared by the terminal and live channels).

Lives in the adapters layer so both the attended terminal session and the channel lane can import it without a
layer violation. ``interactive_session_store.py`` and ``interactive_session.py`` re-export it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple


class ChatSessionStore:
    """ConversationStore for an active chat session.

    current_slice  — TF-DF-IDF selection over the in-memory _session_history (the current session).
    related_slices — delegates to SessionFileConversationStore over the QAR conversations dir so
                     prior QAR sessions are also reachable for anaphora resolution.

    This is NOT a RetrievalAdapter and does NOT duplicate the ClaudeConversationsAdapter.
    The orchestrator's Step 1 (anaphora resolution) calls this protocol specifically to expand
    short/context-dependent messages into self-contained goal conditions before planning.
    """

    def __init__(self, session_history: "List[Tuple[str, str]]",
                 conv_dir: Optional["Path"] = None) -> None:
        self._history = session_history
        self._file_store: Optional[Any] = None
        self._conv_dir = conv_dir

    def _get_file_store(self) -> Optional[Any]:
        if self._file_store is None and self._conv_dir is not None:
            try:
                from .session_file_conversation_store import SessionFileConversationStore
                self._file_store = SessionFileConversationStore(sessions_dir=str(self._conv_dir))
            except Exception:
                pass
        return self._file_store

    def current_slice(self, conv_id: str, query: str, **kwargs) -> Any:
        try:
            from hubofhubs.ports.context import ConversationContext
            from .conversation_format import select_current_slice
            messages = []
            for u, a in self._history:
                messages.append({"role": "user", "content": u})
                messages.append({"role": "assistant", "content": a})
            if not messages:
                return ConversationContext(scanned=0)
            recent_turns = kwargs.get("recent_turns", 4)
            max_chars = kwargs.get("max_chars", 6000)
            text, turns_meta, truncated = select_current_slice(
                messages, query, recent_turns=recent_turns, max_chars=max_chars
            )
            return ConversationContext(
                text=text, turns=turns_meta,
                sources=[{"conv_id": conv_id, "label": "current session"}],
                scanned=len(messages), truncated=truncated,
            )
        except Exception:
            from hubofhubs.ports.context import ConversationContext
            return ConversationContext(scanned=0)

    def related_slices(self, query: str, scope: Any, **kwargs) -> Any:
        try:
            store = self._get_file_store()
            if store is None:
                from hubofhubs.ports.context import ConversationContext
                return ConversationContext(scanned=0)
            return store.related_slices(query, scope or {}, **kwargs)
        except Exception:
            from hubofhubs.ports.context import ConversationContext
            return ConversationContext(scanned=0)
