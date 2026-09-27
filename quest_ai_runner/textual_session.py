"""Session launcher for the Textual-based interactive (attended) mode.

Usage:
    from quest_ai_runner.textual_session import start_textual_interactive
    start_textual_interactive(config, rep_name="My AI")

This is the only entry point for an attended chat session. It builds a real
:class:`~quest_ai_runner.interactive_session.InteractiveSession` (which
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
    rep_specified: bool = True,
    persona_specified: bool = True,
    resume=None,
) -> Optional[str]:
    """Launch the Textual UI immediately, build the InteractiveSession in a background worker.

    Returns the id of the conversation the session wrote to (for the CLI's "resume this
    session" hint), or None when nothing was saved, e.g. a session closed before any turn.
    """
    from .textual_ui import QuestAITerminal

    app = None
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
        app = QuestAITerminal(
            None,
            verbosity=verbosity,
            _config=config,
            _rep_name=rep_name,
            _persona=persona,
            _goal_id=goal_id,
            _rep_specified=rep_specified,
            _persona_specified=persona_specified,
            resume=resume,
        )
        app.run(mouse=True)
    except KeyboardInterrupt:
        # Ctrl+C pressed — exit cleanly without traceback
        pass
    return saved_conversation_id(app, resume)


def saved_conversation_id(app, resume=None) -> Optional[str]:
    """The conversation id to offer for `--resume`, only if its file really exists on disk."""
    session = getattr(app, "sess", None) if app is not None else None
    path = getattr(session, "_session_file", None) if session is not None else None
    if path is None and resume is not None:
        path = resume.path  # exited while still loading a resumed conversation
    try:
        if path is not None and path.exists():
            return path.stem
    except OSError:
        pass
    return None


def is_textual_available() -> bool:
    """True if Textual can be imported. It is a core dependency, so a False here
    means a broken or incompletely synced install, not an opted-out extra."""
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False
