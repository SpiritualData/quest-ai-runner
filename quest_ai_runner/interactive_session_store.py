"""ChatSessionStore compatibility path: the class moved to ``adapters/conversation_session_store.py``.

Kept so ``interactive_session`` and existing imports keep working; it re-exports the moved name.
"""
from .adapters.conversation_session_store import ChatSessionStore  # noqa: F401
