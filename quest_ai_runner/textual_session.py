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
    verbosity: int = 0,
) -> None:
    """Launch the Textual UI immediately, build the InteractiveSession in a background worker."""
    from .textual_ui import QuestAITerminal

    try:
        # mouse=True (Textual's default) is REQUIRED for wheel scrolling. A Textual
        # app runs in the alternate-screen buffer, where the terminal has no
        # scrollback of its own — the app must consume wheel events itself, which
        # only happens when mouse reporting is on. Passing mouse=False (as an earlier
        # build did) disables ALL mouse input, so the scroll wheel does nothing and the
        # on_mouse_scroll_* handlers in textual_ui.py never fire.
        #
        # Text selection still works with mouse on — and without holding Shift:
        # Textual 3.0+ renders its OWN in-app selection on plain click-drag (it owns
        # the mouse, so the terminal's native plain-drag selection is suppressed, but
        # Textual reproduces it). Ctrl+C copies that selection via OSC-52 (works over
        # SSH/mobile too); see action_copy_or_quit in textual_ui.py. Shift+drag remains
        # available as the terminal-native selection fallback, and Ctrl+Y copies the
        # last AI reply. So scroll + selection + copy all work at once.
        QuestAITerminal(
            None,
            verbosity=verbosity,
            _config=config,
            _rep_name=rep_name,
            _persona=persona,
            _goal_id=goal_id,
        ).run(mouse=True)
    except KeyboardInterrupt:
        # Ctrl+C pressed — exit cleanly without traceback
        pass


def is_textual_available() -> bool:
    """True if Textual can be imported (the [tui] extra is installed)."""
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False
