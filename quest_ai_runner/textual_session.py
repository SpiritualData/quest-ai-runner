"""Session launcher for Textual-based interactive mode.

Usage:
    from quest_ai_runner.textual_session import run_textual_session
    result = run_textual_session(config, rep_name="My AI")
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .textual_ui import QuestAITerminal
from .core.orchestrator import Orchestrator

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import OrchestratorResult


async def run_textual_session(
    config: RunnerConfig,
    rep_name: str = "Quest AI",
    goal_id: Optional[str] = None,
) -> Optional[OrchestratorResult]:
    """Run an interactive Textual-based session.

    Args:
        config: RunnerConfig for the session
        rep_name: Name of the AI rep
        goal_id: Optional goal ID to run from

    Returns:
        The orchestrator result, or None if session was cancelled
    """
    # Create orchestrator
    orchestrator = Orchestrator(config)

    # Create and run Textual app
    app = QuestAITerminal(rep_name=rep_name)

    # Run the app and stream orchestrator output
    async def stream_goal():
        if goal_id:
            return await orchestrator.run_goal(goal_id)
        else:
            return await orchestrator.run_turn(goal_id=None)

    # Mount the orchestrator and run
    async with app.run_test() as pilot:
        result = app._stream_orchestrator()

    return result


def run_textual_repl(
    config: RunnerConfig,
    rep_name: str = "Quest AI",
) -> None:
    """Run an interactive Textual REPL for conversational AI interaction.

    Args:
        config: RunnerConfig for the session
        rep_name: Name of the AI rep
    """
    app = QuestAITerminal(rep_name=rep_name)

    # TODO: Integrate with REPL input handling
    # For now, just run the app
    app.run()


if __name__ == "__main__":
    import asyncio
    from .config import RunnerConfig

    # Example usage
    config = RunnerConfig()
    result = asyncio.run(run_textual_session(config))
    print(f"Session result: {result}")
