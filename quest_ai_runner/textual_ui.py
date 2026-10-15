"""Textual-based interactive terminal UI for Quest AI Runner.

The one supported chat UI: a full multi-turn REPL over the orchestrator brain,
built on Textual so the display stays calm and flicker-free (no manual cursor
math, no spinner thread). Every slash command, the live activity strip,
ESC-to-cancel, the per-turn footer, session save/load, model-tier selection, and
the quest/goal pickers live here, in a Claude-Code-like layout: a scrolling
transcript on top, a live activity strip that updates in place while the AI
works, and a prompt docked at the bottom.

Design note: the heavy lifting (building the orchestrator, loading/persisting
session state, the non-rendering command handlers, the model-tier menu data, the
Quest client) lives in
:class:`~quest_ai_runner.interactive_session.InteractiveSession`. This module
reuses that object as its state + logic backend, swapping the session's stdout
``_Console`` for a ``RichLog``-backed adapter so those handlers render into the
Textual transcript without change. The turn streaming, activity strip,
cancellation, footer, and the three interactive pickers (``/models``, ``/reps``,
``/quests``) are implemented here in Textual-native terms.

``textual`` is a core dependency of this package; there is no alternative
renderer to fall back to.
"""
from __future__ import annotations

# Imports below stay importable from here for existing callers, although the code now lives in
# the textual_ui_<role> modules re-exported at the bottom of this block.
import enum
import os
import shutil
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from textual import work
from textual._ansi_sequences import ANSI_SEQUENCES_KEYS
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll  # Horizontal kept for layout elsewhere if needed
from textual.geometry import Offset
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Footer, Header, Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
import logging

from .adapters.retry_utils import format_provider_error
from .interactive_session import (
    InteractiveSession,
    _BANNER,
    _DeepRunTracker,
    _HELP,
    _SLASH_COMMANDS,
    _Console,
    _model_display,
    _model_label,
    _parse_skill_frontmatter,
    _BOLD, _CYAN, _DIM, _RESET,
)

from .textual_ui_helpers import (  # noqa: F401
    _CLIPBOARD_COMMANDS,
    _build_future_context_text,
    _build_shallow_context_bullets,
    _copy_to_clipboard_tool,
    _missing_model_provider_message,
)
from .textual_ui_widgets import (  # noqa: F401
    ActivityBar,
    ContextPanel,
    DeepActivity,
    DeepDetailPanel,
    FutureContextPanel,
    _AWAITING_DECISION_PLACEHOLDER,
    _BUSY_PLACEHOLDER,
    _READY_PLACEHOLDER,
    _RichLogConsole,
    _RichLogHandler,
    _busy_placeholder,
    newline_hint,
)
from .textual_ui_transcript import (  # noqa: F401
    TranscriptLog,
)
from .textual_ui_prompt import (  # noqa: F401
    PromptTextArea,
    _ALT_ENTER_SEQUENCE,
    _QarKeys,
)
from .textual_ui_startup import StartupMixin
from .textual_ui_commands import CommandsMixin
from .textual_ui_turn import TurnMixin
from .textual_ui_deep import DeepMixin
from .textual_ui_actions import ActionsMixin


if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import OrchestratorResult




# ── Main app ──────────────────────────────────────────────────────────────────

class QuestAITerminal(StartupMixin, CommandsMixin, TurnMixin, DeepMixin, ActionsMixin, App):
    """Textual REPL over an `InteractiveSession`'s orchestrator brain."""

    CSS = """
    Screen { background: $surface; }

    #transcript {
        height: 1fr;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }

    #context {
        height: auto;
        max-height: 16;
        border-left: thick $accent 30%;
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
        overflow: hidden hidden;
    }

    #deep {
        height: auto;
        max-height: 12;
        border-left: thick $warning 40%;
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
        overflow: hidden hidden;
    }

    #deep-detail {
        height: 2fr;
        border-left: thick $warning 80%;
        border-top: dashed $warning 40%;
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
        scrollbar-size-vertical: 1;
    }

    #deep-detail-body {
        height: auto;
    }

    #future-context {
        height: auto;
        max-height: 10;
        border-left: thick $success 60%;
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
        scrollbar-size-vertical: 1;
    }

    #future-context-body {
        height: auto;
    }

    #bottom-bar {
        dock: bottom;
        height: auto;
    }

    #activity {
        height: 1;
        padding: 0 1;
        margin: 0 1;
    }

    #command-menu {
        height: auto;
        max-height: 14;
        margin: 0 1;
        border: none;
        background: $panel;
        display: none;
    }

    #prompt {
        margin: 0 1 1 1;
        border: tall $accent 60%;
        background: $panel;
        height: 3;
        max-height: 10;
    }
    #prompt:focus { border: tall $accent; }
    """

    BINDINGS = [
        Binding("ctrl+c", "copy_or_quit", "Copy / Quit", priority=True),
        Binding("escape", "cancel", "Cancel turn"),
        Binding("ctrl+l", "clear_log", "Clear screen"),
        Binding("ctrl+y", "copy_last", "Copy last reply", show=True),
        Binding("alt+d", "toggle_deep_detail", "Expand agent", show=True),
        Binding("alt+c", "toggle_future_context", "Context used", show=True),
        # priority=True: Textual's Screen has its OWN built-in "tab" binding (app.focus_next,
        # screen.py) which, being closer to the focused PromptTextArea in the DOM chain, would
        # otherwise intercept every Tab press before it ever reaches this action (the textarea
        # itself doesn't consume Tab since tab_behavior="focus"). Priority bindings are checked in
        # a separate App-down pass BEFORE that normal walk, so this wins regardless of focus.
        Binding("tab", "cycle_deep_run", "Next agent", show=True, priority=True),
        Binding("pageup", "scroll_up_or_agent", "Scroll up", show=True, priority=True),
        Binding("pagedown", "scroll_down_or_agent", "Scroll down", show=True, priority=True),
    ]

    def __init__(
        self,
        session: Optional[InteractiveSession],
        verbosity: int = 0,
        *,
        _config=None,
        _rep_name: str = "Assistant",
        _persona: Optional[str] = None,
        _goal_id: Optional[str] = None,
        _rep_specified: bool = True,
        _persona_specified: bool = True,
        resume=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.sess = session  # None until _finish_startup when built lazily
        self.rep_name = session._rep_name if session is not None else _rep_name
        self.title = "Quest AI Runner"
        self.verbosity = verbosity
        # Deferred-init args (used when session=None; cleared after startup).
        self._deferred_config = _config
        self._deferred_rep_name = _rep_name
        self._deferred_persona = _persona
        self._deferred_goal_id = _goal_id
        self._deferred_rep_specified = _rep_specified
        self._deferred_persona_specified = _persona_specified
        self.deferred_resume = resume

        # Per-turn streaming state (reset by _begin_turn).
        self._turn_active = False
        self._cancel = threading.Event()
        self._t0 = 0.0
        self._partial_started = False
        self._ai_label_shown = False
        self._cur_deep_run: Optional[str] = None
        self._answer_parts: List[str] = []
        # Last narration beat written to the feed, to drop exact consecutive repeats.
        self._last_narration = ""
        self._deep_plan_shown = False
        self._deep = _DeepRunTracker()
        self._deep_seen: set = set()
        self._deep_event_count = 0
        # Deep runs whose full output has already been written to the scrollback
        # transcript, so we persist each task's output exactly once.
        self._deep_flushed: set = set()
        # Counts how many auto-execute passes have fired for the current user turn
        # (0 = the user's turn itself; 1+ = chained auto passes). Reset on each real user message.
        self._auto_pass: int = 0
        # Archive of FINISHED deep runs (run_id -> snapshot), kept ACROSS turns so the Alt+D detail
        # panel can replay a task's full actions even after the turn ends and after later turns
        # rebuild the live ``_deep`` tracker. Deliberately NOT reset by _begin_turn; capped to the
        # most recent runs so it can't grow without bound.
        self._deep_archive: "OrderedDict[str, dict]" = OrderedDict()
        self._DEEP_ARCHIVE_MAX = 20

        # Future context captured from the last deep result event (if any).
        # Shown in the FutureContextPanel after the turn ends.
        self._future_context: str = ""

        # True while the AI is awaiting the user's reply to an EVENT_DECISION question raised
        # mid-turn. Drives the prompt placeholder + a visually distinct rendering of the
        # question; cleared as soon as the user submits their next message.
        self._awaiting_decision: bool = False

        # When set, the next submitted line is a menu selection, not a turn.
        self._pending_select: Optional[Callable[[str], None]] = None

        # Stable session key for the input inbox so mid-turn messages can be queued.
        self._session_id = str(id(self))

        # Event-type constants, resolved lazily on first event.
        self._ev: Optional[dict] = None
        self._console: Optional[_RichLogConsole] = None
        # Messages typed before the session finishes initializing (deferred-init path only).
        self._pre_session_queue: List[str] = []

    # -- layout ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # min_width=1: RichLog's default (78) is a floor under the shrink-to-fit width
        # computed from the widget's own content region, so on a narrower terminal (a
        # phone-width SSH session) every write still wraps at 78 columns and the extra
        # width is then cropped off rather than ever reaching a second visual line.
        yield TranscriptLog(id="transcript", max_lines=20000, wrap=True, min_width=1,
                            highlight=True, markup=True, auto_scroll=True)
        yield ContextPanel(id="context")
        yield DeepActivity(id="deep")
        yield DeepDetailPanel(id="deep-detail")
        yield FutureContextPanel(id="future-context")
        # ActivityBar + the prompt are wrapped in one docked container (rather than each
        # docking to the Screen individually) so the input box always stays pinned to the
        # bottom regardless of how tall the panels above grow. A single dock:bottom widget
        # here, not two, avoids a layout quirk where a second independently-docked bottom
        # sibling alongside Footer (also dock:bottom) lands on the same row as Footer instead
        # of stacking above it -- Footer's own space reservation only correctly accounts for
        # one additional bottom-docked widget.
        with Vertical(id="bottom-bar"):
            yield ActivityBar(id="activity")
            yield OptionList(id="command-menu")
            yield PromptTextArea(
                id="prompt",
                soft_wrap=True,
                tab_behavior="focus",
                show_line_numbers=False,
                compact=True,
                placeholder=_READY_PLACEHOLDER,
            )
        yield Footer()

    def on_mount(self) -> None:
        self._ui_thread_id = threading.get_ident()
        self._tlog = self.query_one("#transcript", TranscriptLog)
        self._ctx = self.query_one("#context", ContextPanel)
        self._deep_view = self.query_one("#deep", DeepActivity)
        self._deep_detail = self.query_one("#deep-detail", DeepDetailPanel)
        self._future_ctx_panel = self.query_one("#future-context", FutureContextPanel)
        self._activity = self.query_one("#activity", ActivityBar)
        self._ctx.reset()
        self._deep_view.hide()
        self._activity.display = False

        # Capture Python logging output into the RichLog (not stderr) for proper wrapping.
        root_logger = logging.getLogger()
        root_logger.handlers.clear()  # Remove stderr handler from basicConfig
        log_handler = _RichLogHandler(self, self._tlog)
        # Verbosity: 0=WARNING, 1=INFO, 2+=DEBUG
        if self.verbosity >= 2:
            log_level = logging.DEBUG
        elif self.verbosity >= 1:
            log_level = logging.INFO
        else:
            log_level = logging.WARNING
        log_handler.setLevel(log_level)
        root_logger.addHandler(log_handler)
        root_logger.setLevel(log_level)

        if self.sess is not None:
            # Session was pre-built (direct call) — wire console and show header immediately.
            self._console = _RichLogConsole(self, self._tlog)
            self.sess._console = self._console
            self._print_header()
            self.query_one("#prompt", PromptTextArea).focus()
        else:
            # Deferred init: show the static banner immediately; session line follows once ready.
            self._console = _RichLogConsole(self, self._tlog)
            self._print_static_banner()
            # A resumed conversation was already read from disk before the app started, so
            # show it NOW instead of after the (seconds-long) session build; otherwise the
            # screen looks like a brand-new conversation until then.
            if self.deferred_resume is not None:
                conv = self.deferred_resume
                self.replay_resumed_history(
                    conv, conv.meta.get("rep_name") or self._deferred_rep_name)
                self._console.dim("  Loading the rest of the session. You can type now; your "
                                  "message is sent once it is ready.")
                self.sub_title = "Resuming conversation..."
            self.query_one("#prompt", PromptTextArea).focus()
            self.run_worker(self._build_session_worker, exclusive=True, thread=True)


if __name__ == "__main__":
    # Manual smoke test requires a configured RunnerConfig; see textual_session.py.
    raise SystemExit("Run via: Quest AI Runner chat")
