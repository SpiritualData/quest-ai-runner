"""Session launcher for Textual-based interactive mode.

Usage:
    from quest_ai_runner.textual_session import start_textual_interactive
    start_textual_interactive(config, rep_name="My AI")
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .textual_ui import QuestAITerminal
from .core.orchestrator import Orchestrator

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import OrchestratorResult


def start_textual_interactive(
    config: RunnerConfig,
    *,
    rep_name: str = "Quest AI",
    persona: Optional[str] = None,
    goal_id: Optional[str] = None,
) -> None:
    """Start an interactive Textual-based session (CLI entry point).

    This is the Textual replacement for interactive.start_interactive().
    Runs the event loop until user quits.

    Args:
        config: RunnerConfig for the session
        rep_name: Name of the AI rep
        persona: Path to persona/skill file (optional)
        goal_id: Optional goal ID to attach session to
    """
    app = QuestAITerminal(rep_name=rep_name)

    # Create orchestrator with config
    app.orchestrator = Orchestrator(config)

    # TODO: Handle persona loading if provided
    # if persona:
    #     app.load_persona(persona)

    # Run the Textual app (blocking until user quits)
    app.run()


def is_textual_available() -> bool:
    """Check if Textual is available and can be used.

    Returns:
        True if Textual can be imported and terminal supports it
    """
    try:
        from textual.app import App  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    from .config import RunnerConfig

    # Example usage
    config = RunnerConfig()
    start_textual_interactive(config, rep_name="Demo AI")
