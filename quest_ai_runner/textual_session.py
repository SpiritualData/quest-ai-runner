"""Session launcher for the Textual-based interactive (attended) mode.

Usage:
    from quest_ai_runner.textual_session import start_textual_interactive
    start_textual_interactive(config, rep_name="My AI")

This is the Textual replacement for ``interactive.start_interactive()``. It
builds a real :class:`~quest_ai_runner.interactive.InteractiveSession` (which
constructs the orchestrator, restores persisted chat state, and prepares the
model-tier menu), then drives it through the Textual UI in
:class:`~quest_ai_runner.textual_ui.QuestAITerminal`. All session logic and
state live in the InteractiveSession; the Textual app only renders and reads.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import RunnerConfig


def start_textual_interactive(
    config: "RunnerConfig",
    *,
    rep_name: str = "Assistant",
    persona: Optional[str] = None,
    goal_id: Optional[str] = None,
) -> None:
    """Build an InteractiveSession and run it under the Textual UI until quit."""
    from .interactive import InteractiveSession
    from .textual_ui import QuestAITerminal

    session = InteractiveSession(
        config, rep_name=rep_name, persona=persona, goal_id=goal_id
    )
    QuestAITerminal(session).run()


def is_textual_available() -> bool:
    """True if Textual can be imported (the [tui] extra is installed)."""
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False
