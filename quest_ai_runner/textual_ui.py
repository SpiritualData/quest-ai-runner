"""Textual-based interactive terminal UI for Quest AI Runner.

A full multi-turn REPL over the orchestrator brain, rebuilt on Textual so the
display stays calm and flicker-free (no manual cursor math, no spinner thread).
It carries the same feature set as the ANSI ``interactive.py`` session — every
slash command, the live context panel, ESC-to-cancel, the per-turn footer,
session save/load, model-tier selection, and the quest/goal pickers — but with a
Claude-Code-like layout: a scrolling transcript on top, a live activity strip
that updates in place while the AI works, and a prompt at the bottom.

Design note: the heavy lifting (building the orchestrator, loading/persisting
session state, the non-interactive command handlers, the model-tier menu data,
the Quest client) lives in :class:`~quest_ai_runner.interactive.InteractiveSession`.
This module reuses that object as its state + logic backend, swapping the
session's stdout ``_Console`` for a ``RichLog``-backed adapter so those handlers
render into the Textual transcript without change. The turn streaming, context
panel, cancellation, footer, and the three interactive pickers (``/models``,
``/reps``, ``/quests``) are implemented here in Textual-native terms.

Install the [tui] extra:
    pip install quest-ai-runner[tui]
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Callable, List, Optional, TYPE_CHECKING

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll  # Horizontal kept for layout elsewhere if needed
from textual.message import Message
from textual.widgets import Footer, Header, Input, RichLog, Static, TextArea

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
import logging

from .adapters.retry_utils import format_provider_error
from .interactive import (
    InteractiveSession,
    _BANNER,
    _DeepRunTracker,
    _HELP,
    _SLASH_COMMANDS,
    _Console,
    _model_label,
    _parse_skill_frontmatter,
    _BOLD, _CYAN, _DIM, _RESET,
)

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import OrchestratorResult


# ── RichLog-backed console adapter ─────────────────────────────────────────────
#
# InteractiveSession's command handlers print through a `_Console` (stdout +
# ANSI). We subclass it and redirect the two low-level sinks (`write`, `line`)
# into a Textual RichLog, converting the ANSI the parent produces into styled
# Rich Text. All the higher-level helpers (dim/bullet/speaker/rule) keep working
# because they ultimately call `line()`. `markdown()` is overridden to render
# real Markdown instead of the ANSI fallback.

class _RichLogConsole(_Console):
    """A `_Console` that writes into a Textual `RichLog` (thread-safe)."""

    def __init__(self, app: "QuestAITerminal", log: RichLog) -> None:
        # Deliberately do NOT call super().__init__ (it probes stdout/rich).
        self._rich = None          # force the ANSI code path in parent helpers
        self._color = True         # …which emits ANSI strings we convert below
        self._app = app
        self._tlog = log

    # -- low-level sinks ------------------------------------------------------

    def _emit(self, renderable) -> None:
        def _write_impl():
            # Ensure proper wrapping by explicitly setting overflow on Text
            if isinstance(renderable, Text):
                renderable.no_wrap = False
            self._tlog.write(renderable)

        thread_id = getattr(self._app, "_ui_thread_id", None)
        if thread_id is not None and threading.get_ident() != thread_id:
            # Called from a worker thread — marshal onto the UI thread.
            self._app.call_from_thread(_write_impl)
        else:
            _write_impl()

    def write(self, s: str) -> None:
        text = Text.from_ansi(s)
        text.no_wrap = False
        self._emit(text)

    def line(self, s: str = "") -> None:
        text = Text.from_ansi(s) if s else Text("")
        text.no_wrap = False
        self._emit(text)

    def markdown(self, text: str) -> None:
        try:
            self._emit(RichMarkdown(text, code_theme="monokai"))
        except Exception:  # noqa: BLE001
            self._emit(Text(text))

    def _width(self) -> int:
        try:
            return max(40, min(self._app.size.width - 6, 100))
        except Exception:  # noqa: BLE001
            return 88


# ── Logging handler for RichLog ────────────────────────────────────────────────
#
# Captures Python logging output and writes it to the RichLog with proper wrapping,
# so log messages don't get truncated at the terminal edge.

class _RichLogHandler(logging.Handler):
    """A logging handler that writes to a Textual RichLog (thread-safe)."""

    def __init__(self, app: "QuestAITerminal", log: RichLog) -> None:
        super().__init__()
        self._app = app
        self._tlog = log
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            text = Text.from_ansi(msg)
            text.no_wrap = False

            def _write_impl():
                self._tlog.write(text)

            thread_id = getattr(self._app, "_ui_thread_id", None)
            if thread_id is not None and threading.get_ident() != thread_id:
                self._app.call_from_thread(_write_impl)
            else:
                _write_impl()
        except Exception:  # noqa: BLE001
            self.handleError(record)


# ── Live widgets ────────────────────────────────────────────────────────────

class NarrationBar(Static):
    """Single line that shows the AI's current thinking beat, updating in place.

    Each new beat replaces the previous text rather than appending to the log,
    so beats appear one at a time as they arrive instead of all at once.
    Hidden when there is no active narration.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._text = ""
        self.display = False

    def say(self, text: str) -> None:
        self._text = text.strip()
        self.display = bool(self._text)
        self.refresh()

    def clear(self) -> None:
        self._text = ""
        self.display = False
        self.refresh()

    def render(self):
        t = Text()
        t.append("  " + self._text, style="italic dim")
        return t


class ActivityBar(Static):
    """Three animated dots + status text in one line.

    Replaces the separate LoadingIndicator + StatusLine pair so we fully
    control dot count, inter-dot spacing, and the gap before the text.
    """

    _INTERVAL = 0.45

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._text = "Thinking…"
        self._frame = 0
        self._timer = None
        self.display = False

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._INTERVAL, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % 3
        self.refresh()

    def set_status(self, text: str) -> None:
        self._text = text
        self.refresh()

    def render(self):
        t = Text()
        for i in range(3):
            if i > 0:
                t.append(" ")
            style = "cyan bold" if i == self._frame else "cyan dim"
            t.append("●", style=style)
        t.append("  ")
        t.append(self._text, style="dim")
        return t


class ContextPanel(Static):
    """In-place, calm view of context cards + sources gathered this turn.

    Updates the same widget rather than streaming lines into the transcript, so
    the gather phase reads as one quietly-growing block (the display standard:
    no flicker, no log spam).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cards: List[dict] = []
        self.sources: List[str] = []
        self.total_sources = 0
        self.replans = 0

    def reset(self) -> None:
        self.cards = []
        self.sources = []
        self.total_sources = 0
        self.replans = 0
        self.refresh()

    def set_cards(self, cards: List[dict]) -> None:
        self.cards = cards or []
        self.refresh()

    def add_sources(self, paths: List[str]) -> None:
        for p in paths:
            if p not in self.sources:
                self.sources.append(p)
                self.total_sources += 1
        self.refresh()

    def inc_replans(self) -> None:
        self.replans += 1
        self.cards = []
        self.sources = []
        self.refresh()

    def render(self) -> str:
        lines: List[str] = []
        if self.cards:
            lines.append("[bold cyan]\U0001F4C7 Context cards[/bold cyan]")
            for card in self.cards:
                cid = card.get("id", "?")
                title = card.get("title") or "(no title)"
                adapter = card.get("adapter", "")
                score = card.get("relevance_score", 0)
                fcount = card.get("file_count", len(card.get("files", [])))
                alabel = f"\\[{adapter}] " if adapter else ""
                sstr = f"score {score:.2f}" if score else "score ?"
                lines.append(f"  [cyan]●[/cyan] {alabel}[dim]{cid}[/dim]: {title}")
                lines.append(f"    [dim]{sstr} · {fcount} file{'s' if fcount != 1 else ''}[/dim]")
                for f in (card.get("files") or [])[:3]:
                    lines.append(f"      [dim]→ {f}[/dim]")
                extra = len(card.get("files") or []) - 3
                if extra > 0:
                    lines.append(f"      [dim]… and {extra} more[/dim]")
        if self.sources:
            if self.cards:
                lines.append("")
            lines.append("[bold cyan]⌕ Sources[/bold cyan]")
            for src in self.sources[-6:]:
                prefix = "⌕" if src.startswith("(searched") else "↗"
                label = src
                lines.append(f"  {prefix} [dim]{label}[/dim]")
            if len(self.sources) > 6:
                lines.append(f"  [dim]… and {len(self.sources) - 6} more[/dim]")
        self.display = bool(lines)
        return "\n".join(lines)


class DeepActivity(Static):
    """In-place view of concurrent deep-run progress.

    Deep execution emits a steady stream of low-level ticks (tool calls, file
    reads, agent steps). Appending each snapshot to the transcript buries the
    conversation under repeated "executing work…" lines. Instead we render the
    whole live dashboard into this single widget and update it in place — one
    calm block that grows and shrinks, never a scrolling pile of snapshots.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dashboard = ""
        self._n_runs = 0
        self.display = False

    def show(self, dashboard: str, n_runs: int = 1) -> None:
        self._dashboard = dashboard or ""
        self._n_runs = n_runs
        self.display = bool(self._dashboard.strip())
        self.refresh()

    def hide(self) -> None:
        self._dashboard = ""
        self._n_runs = 0
        self.display = False
        self.refresh()

    def render(self):
        if not self._dashboard.strip():
            return Text("")
        hint = "  [Alt+D] expand & scroll  [Tab] next agent" if self._n_runs > 1 else "  [Alt+D] expand & scroll full output"
        t = Text.from_ansi(self._dashboard + f"\x1b[2m\n{hint}\x1b[0m")
        t.no_wrap = False
        return t


class DeepDetailPanel(VerticalScroll):
    """Expanded, scrollable view of one deep run's full live exec output.

    Hidden by default. Press ``d`` to toggle, ``Tab`` to cycle runs. Unlike the
    calm inline dashboard (a few lines), this holds the run's ENTIRE output and
    grows to fill the screen so there's room to actually read it. New lines
    auto-follow the tail; page back through history with PgUp/PgDn (the body is a
    real scroll region). Scrolling back to the bottom resumes following.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._run_id: Optional[str] = None
        self._goal: str = ""
        self._lines: List[str] = []
        self._pos: int = 1
        self._total: int = 1
        self._follow: bool = True
        self.display = False
        # Inner Static holds the rendered text; this container scrolls it.
        self._body = Static(id="deep-detail-body")

    def compose(self) -> ComposeResult:
        yield self._body

    def open_for(self, run_id: str, goal: str, existing_lines: List[str],
                 pos: int = 1, total: int = 1) -> None:
        self._run_id = run_id
        self._goal = goal
        self._lines = [ln.strip() for ln in existing_lines if ln and ln.strip()]
        self._pos = pos
        self._total = total
        self._follow = True
        self.display = True
        self._rerender()

    def push_line(self, run_id: str, line: str) -> None:
        """Append a new output line if this run is the one currently displayed."""
        if self._run_id == run_id and line.strip():
            self._lines.append(line.strip())
            self._rerender()

    def hide(self) -> None:
        self._run_id = None
        self.display = False
        self._rerender()

    @property
    def active_run_id(self) -> Optional[str]:
        return self._run_id

    def page_back(self) -> None:
        """Scroll up a page and stop auto-following the tail."""
        self._follow = False
        self.scroll_page_up(animate=False)

    def page_forward(self) -> None:
        """Scroll down a page; resume following once we're back at the bottom."""
        self.scroll_page_down(animate=False)
        if self.scroll_offset.y >= self.max_scroll_y - 1:
            self._follow = True

    def on_mouse_scroll_up(self, event) -> None:
        """Mouse wheel up: stop auto-following the tail (same intent as page_back)."""
        self._follow = False

    def on_mouse_scroll_down(self, event) -> None:
        """Mouse wheel down: resume following if scrolled back to the tail."""
        def _check_tail() -> None:
            if self.scroll_y >= self.max_scroll_y - 1:
                self._follow = True
        self.call_after_refresh(_check_tail)

    def _rerender(self) -> None:
        if not self._run_id:
            self._body.update(Text(""))
            return
        pos_str = f"agent {self._pos}/{self._total}  " if self._total > 1 else ""
        if self._total > 1:
            nav_hint = "  scroll/PgUp/PgDn · [Tab] next · [Alt+D] close"
        else:
            nav_hint = "  scroll/PgUp/PgDn · [Alt+D] close"
        header = f"⎅ {pos_str}{self._goal[:60]}"
        body = "\n".join(f"  {ln}" for ln in self._lines)
        footer = f"\x1b[2m{nav_hint}\x1b[0m"
        t = Text.from_ansi(f"\x1b[1;36m{header}\x1b[0m\n{body}\n{footer}")
        t.no_wrap = False
        self._body.update(t)
        if self._follow:
            # Defer until after layout so virtual size reflects the new lines.
            self.call_after_refresh(self.scroll_end, animate=False)


def _build_future_context_text(bullets: str) -> Optional[Text]:
    """Build the Rich Text to display in the "context it used" panel.

    Returns ``None`` when ``bullets`` is empty so callers can gate visibility.
    This is a pure function with no Textual dependencies — testable offline.
    """
    cleaned = bullets.strip()
    if not cleaned:
        return None
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return None
    body = "\n".join(f"  {ln}" for ln in lines)
    hint = "\x1b[2m  [Alt+C] close\x1b[0m"
    t = Text.from_ansi(f"\x1b[1;32mContext it used\x1b[0m\n{body}\n{hint}")
    t.no_wrap = False
    return t


class FutureContextPanel(VerticalScroll):
    """Expandable panel showing the context the AI used and judged important for this run.

    From the user's view this is "what context it relied on / considered important for what it did,"
    not internal memory plumbing. Hidden by default. Press ``Alt+C`` to toggle after a deep run
    completes. Content is the FUTURE-CONTEXT section parsed from deep results: a plain newline-joined
    string of bullet lines (e.g. "- collection: Pricing tiers"). Never shown when the content is empty.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bullets: str = ""
        self.display = False
        self._body = Static(id="future-context-body")

    def compose(self) -> ComposeResult:
        yield self._body

    def load(self, bullets: str) -> None:
        """Store the bullet lines without changing visibility.

        Call ``display = True`` (or ``action_toggle_future_context``) after
        loading to make the panel visible.
        """
        self._bullets = bullets.strip()

    def hide(self) -> None:
        """Clear content and hide the panel."""
        self._bullets = ""
        self.display = False
        self._rerender()

    def _rerender(self) -> None:
        t = _build_future_context_text(self._bullets)
        self._body.update(t if t is not None else Text(""))


# ── Transcript widget ─────────────────────────────────────────────────────────
#
# Standard RichLog with auto_scroll=True yanks the view to the bottom on every
# write(), fighting any manual scroll the user makes during an active turn.
# TranscriptLog overrides write() to only auto-scroll when the view is already
# at the tail — so scrolling up actually sticks until the user returns to the bottom.

class TranscriptLog(RichLog):
    """RichLog that respects manual scroll position during streaming."""

    def write(self, content, width=None, expand=False, shrink=True,
              scroll_end=None, animate=False):
        if scroll_end is None and self.auto_scroll:
            # Checked BEFORE new content is added: if the user is at (or within 1
            # line of) the current tail, follow; otherwise leave them where they are.
            scroll_end = self.scroll_y >= self.max_scroll_y - 1
        return super().write(content, width=width, expand=expand, shrink=shrink,
                             scroll_end=scroll_end, animate=animate)


# ── Multi-line prompt input ────────────────────────────────────────────────────

class PromptTextArea(TextArea):
    """Auto-expanding multi-line input. Ctrl+Enter submits; Enter adds a newline."""

    class Submitted(Message):
        def __init__(self, textarea: "PromptTextArea", value: str) -> None:
            super().__init__()
            self.textarea = textarea
            self.value = value

    BINDINGS = [
        Binding("ctrl+enter", "submit_prompt", "Send", show=False, priority=True),
    ]

    MAX_LINES = 8

    def action_submit_prompt(self) -> None:
        self.post_message(self.Submitted(self, self.text))

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        line_count = self.text.count("\n") + 1
        # +2 for the tall CSS border (1 top + 1 bottom cell)
        new_height = min(max(line_count, 1), self.MAX_LINES) + 2
        self.styles.height = new_height


# ── Main app ──────────────────────────────────────────────────────────────────

class QuestAITerminal(App):
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

    #narration {
        height: 1;
        padding: 0 1;
        margin: 0 0;
    }

    #activity {
        height: 1;
        padding: 0 1;
        margin: 0 1;
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
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("escape", "cancel", "Cancel turn"),
        Binding("ctrl+l", "clear_log", "Clear screen"),
        Binding("ctrl+y", "copy_last", "Copy last reply", show=True),
        Binding("alt+d", "toggle_deep_detail", "Expand agent", show=True),
        Binding("alt+c", "toggle_future_context", "Context used", show=True),
        Binding("tab", "cycle_deep_run", "Next agent", show=True),
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

        # Per-turn streaming state (reset by _begin_turn).
        self._turn_active = False
        self._cancel = threading.Event()
        self._t0 = 0.0
        self._partial_started = False
        self._ai_label_shown = False
        self._cur_deep_run: Optional[str] = None
        self._answer_parts: List[str] = []
        self._deep_plan_shown = False
        self._deep = _DeepRunTracker()
        self._deep_seen: set = set()
        self._deep_event_count = 0
        # Deep runs whose full output has already been written to the scrollback
        # transcript, so we persist each task's output exactly once.
        self._deep_flushed: set = set()
        # Archive of FINISHED deep runs (run_id -> snapshot), kept ACROSS turns so the Alt+D detail
        # panel can replay a task's full actions even after the turn ends and after later turns
        # rebuild the live ``_deep`` tracker. Deliberately NOT reset by _begin_turn; capped to the
        # most recent runs so it can't grow without bound.
        self._deep_archive: "OrderedDict[str, dict]" = OrderedDict()
        self._DEEP_ARCHIVE_MAX = 20

        # Future context captured from the last deep result event (if any).
        # Shown in the FutureContextPanel after the turn ends.
        self._future_context: str = ""

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
        yield TranscriptLog(id="transcript", max_lines=20000, wrap=True,
                            highlight=True, markup=True, auto_scroll=True)
        yield ContextPanel(id="context")
        yield DeepActivity(id="deep")
        yield DeepDetailPanel(id="deep-detail")
        yield FutureContextPanel(id="future-context")
        yield NarrationBar(id="narration")
        yield ActivityBar(id="activity")
        yield PromptTextArea(
            id="prompt",
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            compact=True,
            placeholder="Ask anything…   Ctrl+Enter=send, Enter=newline   (/help, Esc=cancel, Alt+D=expand, Tab=cycle)",
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
        self._narration = self.query_one("#narration", NarrationBar)
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
            self.query_one("#prompt", PromptTextArea).focus()
            self.run_worker(self._build_session_worker, exclusive=True, thread=True)

    def _build_session_worker(self) -> None:
        """Worker thread: build InteractiveSession (loads embedder, Qdrant, etc.) then hand off."""
        try:
            from .interactive import InteractiveSession

            # Use a flag so we can stop forwarding notices the moment the session
            # is returned — background threads (e.g. the bootstrap indexer) fire
            # after that point and should not appear in the transcript.
            _active = [True]

            def _live_notice(msg: str) -> None:
                if _active[0]:
                    self.call_from_thread(setattr, self, "sub_title", msg)

            session = InteractiveSession(
                self._deferred_config,
                rep_name=self._deferred_rep_name,
                persona=self._deferred_persona,
                goal_id=self._deferred_goal_id,
                _startup_notify=_live_notice,
            )
            _active[0] = False  # suppress any background-thread notices from here on
            self.call_from_thread(self._finish_startup, session)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._startup_failed, exc)

    def _finish_startup(self, session: InteractiveSession) -> None:
        """Called on the UI thread once the session is ready."""
        self.sub_title = ""  # clear any loading status from the header
        self.sess = session
        self.rep_name = session._rep_name
        session._console = self._console
        self._deferred_config = None
        self._deferred_rep_name = None
        self._deferred_persona = None
        self._deferred_goal_id = None
        # Banner was already shown by _print_static_banner; just add the session line.
        c = self._console
        parts = [f"AI: {session._rep_name}"]
        corpus = getattr(session._cfg, "corpus_root", None)
        if corpus:
            parts.append(f"corpus: {corpus.split('/')[-1] if '/' in corpus else corpus}")
        if session._goal_id:
            parts.append(f"goal: {session._goal_id}")
        if session._model_hint:
            parts.append(f"model: {session._model_hint}")
        c.dim("  " + "  •  ".join(parts))
        c.line("")
        # Replay any messages the user typed before the session was ready.
        for queued_line in self._pre_session_queue:
            self._begin_turn(queued_line, echo=True)
        self._pre_session_queue.clear()

    def _startup_failed(self, exc: Exception) -> None:
        """Called on the UI thread when session init fails — show the error and quit."""
        if self._console:
            self._console.write(f"[red]Startup failed: {exc}[/red]")
        self.exit(1)

    def _print_static_banner(self) -> None:
        self._console.write(_BANNER.format(B=_BOLD, C=_CYAN, R=_RESET, D=_DIM))

    def _print_header(self) -> None:
        c = self._console
        c.write(_BANNER.format(B=_BOLD, C=_CYAN, R=_RESET, D=_DIM))
        parts = [f"AI: {self.sess._rep_name}"]
        corpus = getattr(self.sess._cfg, "corpus_root", None)
        if corpus:
            parts.append(f"corpus: {corpus.split('/')[-1] if '/' in corpus else corpus}")
        if self.sess._goal_id:
            parts.append(f"goal: {self.sess._goal_id}")
        if self.sess._model_hint:
            parts.append(f"model: {self.sess._model_hint}")
        c.dim("  " + "  •  ".join(parts))
        c.line("")

    # -- input dispatch --------------------------------------------------------

    def on_prompt_text_area_submitted(self, event: PromptTextArea.Submitted) -> None:
        line = (event.value or "").strip()
        event.textarea.clear()
        event.textarea.styles.height = 3
        if not line:
            return

        # Session still initializing — queue the message for replay once ready.
        if self.sess is None:
            self._pre_session_queue.append(line)
            return

        # Menu selection mode (a picker is awaiting a number).
        if self._pending_select is not None:
            cb = self._pending_select
            self._pending_select = None
            cb(line)
            return

        if self._turn_active:
            # Queue the message for the orchestrator to drain between goal-loop steps.
            inbox = getattr(self.sess._orch, "input_inbox", None)
            if inbox is not None:
                inbox.push(self._session_id, line)
            self._tlog.write(Text(f"  ↑ queued: {line}", style="dim"))
            return

        if line.startswith("/"):
            self._dispatch_command(line)
        else:
            self._begin_turn(line, echo=True)

    def _dispatch_command(self, line: str) -> None:
        c = self._console
        s = self.sess

        if line in ("/quit", "/q", "quit", "exit"):
            self.exit(); return
        if line == "/help":
            c.line(_HELP); return
        if line == "/clear":
            s._last_user = ""; s._last_assistant = ""
            s._session_history = []
            c.dim("  Transcript cleared."); return
        if line.startswith("/rep "):
            s._rep_name = line[5:].strip()
            self.rep_name = s._rep_name
            c.dim(f"  Representative: {s._rep_name}")
            s._persist_session_state(); return
        if line.startswith("/file "):
            path = line[6:].strip()
            try:
                s._persona = open(path).read()
                s._persona_file = path
                kb = max(1, len(s._persona.encode()) // 1024)
                c.dim(f"  Loaded: {path} ({kb}KB)")
                s._persist_session_state()
            except OSError as e:
                c.dim(f"  Could not read {path!r}: {e}")
            return
        if line == "/whoami":
            s._print_whoami(); return
        if line == "/status":
            s._print_status(); return
        if line == "/tasks":
            s._print_tasks(); return
        if line in ("/models", "/model"):
            self._cmd_models_menu(); return
        if line.startswith("/model"):
            s._cmd_model(line[6:].strip()); return
        if line.startswith("/depth"):
            s._cmd_depth(line[6:].strip()); return
        if line.startswith("/system"):
            s._cmd_system(line[7:].strip()); return
        if line == "/replan":
            s._replan_next = True
            c.dim("  Next turn will use opus for a fresh re-planning pass."); return
        if line.startswith("/save"):
            s._cmd_save(line[5:].strip()); return
        if line.startswith("/load "):
            s._cmd_load(line[6:].strip())
            self.rep_name = s._rep_name; return
        if line == "/sessions":
            s._cmd_sessions(); return
        if line == "/reps":
            self._cmd_reps(); return
        if line == "/quests":
            self._cmd_quests(); return
        if line.startswith("/goal"):
            self._cmd_goal(line[5:].strip()); return
        c.dim(f"  Unknown command: {line!r}  (/help for list)")

    # -- interactive pickers (Textual-native) ---------------------------------

    def _ask_select(self, prompt: str, handler: Callable[[str], None]) -> None:
        """Print a prompt and route the next submitted line to ``handler``."""
        self._console.dim(prompt)
        self._pending_select = handler

    def _cmd_models_menu(self) -> None:
        c = self._console
        s = self.sess
        current = s._model_hint or "auto"
        c.line("")
        c.dim("  Available models:")
        for i, (tier, desc) in enumerate(s._model_tiers, 1):
            marker = "●" if tier == current else " "
            c.dim(f"  {i}.  {marker} {tier:9s}  {desc}")
        c.dim(f"  0.  Cancel (keep: {current})")

        def _handle(raw: str) -> None:
            try:
                n = int(raw.strip())
            except ValueError:
                c.dim("  Cancelled."); return
            if n <= 0 or n > len(s._model_tiers):
                c.dim("  Cancelled."); return
            tier_name, _ = s._model_tiers[n - 1]
            if tier_name == "auto":
                s._model_hint = None
                c.dim("  Model set to auto (orchestrator decides).")
            else:
                s._model_hint = tier_name
                c.dim(f"  Model set to {tier_name}.")
            s._persist_session_state()

        self._ask_select("  select › ", _handle)

    def _cmd_reps(self) -> None:
        c = self._console
        s = self.sess
        skills_dir = s._skills_dir()
        if not skills_dir or not os.path.isdir(skills_dir):
            c.dim(f"  No SKILL.md files found in {skills_dir}")
            c.dim("  Create .claude/skills/<name>/SKILL.md, or use /rep <name> and /file <path>.")
            return
        reps: List[dict] = []
        for entry in sorted(os.scandir(skills_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            skill_file = os.path.join(entry.path, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue
            meta = _parse_skill_frontmatter(skill_file)
            reps.append({
                "name": entry.name,
                "display_name": meta.get("display_name") or entry.name,
                "skill_file": skill_file,
            })
        if not reps:
            c.dim(f"  No SKILL.md files found under {skills_dir}."); return
        c.line("")
        for i, r in enumerate(reps, 1):
            c.dim(f"  {i}.  {r['display_name']}")
        c.dim("  0.  Cancel")

        def _handle(raw: str) -> None:
            try:
                n = int(raw.strip())
            except ValueError:
                c.dim("  Cancelled."); return
            if n <= 0 or n > len(reps):
                c.dim("  Cancelled."); return
            r = reps[n - 1]
            s._rep_name = r["display_name"]
            self.rep_name = s._rep_name
            try:
                s._persona = open(r["skill_file"]).read()
                s._persona_file = r["skill_file"]
                kb = max(1, len(s._persona.encode()) // 1024)
                c.dim(f"  Representative: {s._rep_name}  (loaded {r['skill_file']}, {kb}KB)")
                s._persist_session_state()
            except OSError as e:
                c.dim(f"  Could not read {r['skill_file']!r}: {e}")

        self._ask_select("  select › ", _handle)

    def _cmd_goal(self, arg: str) -> None:
        c = self._console
        s = self.sess
        if arg and " " not in arg and len(arg) < 80:
            s._goal_id = arg
            c.dim(f"  Goal set to {arg!r} — use /quests to browse by name."); return
        self._cmd_quests()

    def _cmd_quests(self) -> None:
        c = self._console
        s = self.sess
        client = s._quest_client()
        if client is None:
            c.dim("  Quest credentials not configured. Set QUEST_BASE_URL, QUEST_API_KEY, QUEST_TEAM_ID.")
            return
        c.dim("  Fetching quests and goals…")
        try:
            quests = client.list_quests()
        except Exception as e:  # noqa: BLE001
            c.dim(f"  Could not fetch quests: {e}"); return
        if not quests:
            c.dim("  No quests attached to this team."); return

        SCOPE_ORDER = ["year", "quarter", "month", "week", "day", "custom", "quest", ""]

        def _scope_rank(x) -> int:
            try:
                return SCOPE_ORDER.index(str(x or ""))
            except ValueError:
                return len(SCOPE_ORDER)

        buckets: dict = {}
        for quest in quests:
            quest_id = quest.get("quest_id") or ""
            quest_outcome = quest.get("outcome") or quest_id or "untitled"
            if not quest_id:
                continue
            try:
                data = client.list_quest_goals(quest_id)
            except Exception:  # noqa: BLE001
                continue
            for group in (data.get("period_groups") or []):
                scope = group.get("time_scope") or "custom"
                period = group.get("period") or ""
                key = (scope, period)
                bucket = buckets.setdefault(key, {
                    "time_scope": scope, "period": period,
                    "period_label": group.get("period_label") or period or scope,
                    "goals": [],
                })
                for g in (group.get("goals") or []):
                    g["_quest_outcome"] = quest_outcome
                    bucket["goals"].append(g)

        if not buckets:
            c.dim("  No goals found."); return

        sorted_groups = sorted(buckets.values(),
                               key=lambda p: (_scope_rank(p["time_scope"]), p.get("period") or ""))
        flat_goals: List[dict] = []
        c.line("")
        n = 0
        for group in sorted_groups:
            goals = group.get("goals") or []
            if not goals:
                continue
            c.dim(f"       ── {group['period_label']} ──")
            for g in goals:
                n += 1
                name = g.get("name") or g.get("title") or g.get("id") or "untitled"
                ctx = g.get("_quest_outcome") or ""
                done = "  ✓" if g.get("completed") else ""
                suffix = f"  ({ctx}){done}" if ctx else done
                flat_goals.append(g)
                c.dim(f"  {n:2d}.  {name}{suffix}")
        c.dim("   0.  cancel")
        if not flat_goals:
            c.dim("  No goals found."); return

        def _handle(raw: str) -> None:
            try:
                pick = int(raw.strip())
            except ValueError:
                c.dim("  Cancelled."); return
            if pick <= 0 or pick > len(flat_goals):
                c.dim("  Cancelled."); return
            g = flat_goals[pick - 1]
            s._goal_id = g.get("id") or g.get("goal_id") or ""
            c.dim(f"  Attached to: {g.get('name') or g.get('title') or s._goal_id}")

        self._ask_select("  select › ", _handle)

    # -- turn lifecycle --------------------------------------------------------

    def _set_terminal_title(self, title: str) -> None:
        """Update both the Textual header and the terminal window/pane title."""
        self.title = title
        try:
            if hasattr(self, "_driver") and self._driver is not None:
                self._driver.write(f"\033]0;{title}\007")
        except Exception:
            pass

    def _types(self) -> dict:
        if self._ev is None:
            from .core.adapters import (
                EVENT_CONTEXT, EVENT_STATUS, EVENT_PLAN, EVENT_READ, EVENT_REPLAN,
                EVENT_PARTIAL, EVENT_EXEC, EVENT_RESULT, EVENT_DECISION,
                EVENT_MILESTONE, EVENT_DONE,
            )
            self._ev = dict(
                context=EVENT_CONTEXT, status=EVENT_STATUS, plan=EVENT_PLAN,
                read=EVENT_READ, replan=EVENT_REPLAN, partial=EVENT_PARTIAL,
                exec=EVENT_EXEC, result=EVENT_RESULT, decision=EVENT_DECISION,
                milestone=EVENT_MILESTONE, done=EVENT_DONE,
            )
        return self._ev

    def _begin_turn(self, user_text: str, *, echo: bool, auto: bool = False) -> None:
        if self._turn_active:
            return
        self._turn_active = True
        self._cancel.clear()
        self._partial_started = False
        self._ai_label_shown = False
        self._cur_deep_run = None
        self._answer_parts: List[str] = []
        self._t0 = time.monotonic()
        self._deep = _DeepRunTracker()
        self._deep_seen = set()
        self._deep_event_count = 0
        self._deep_flushed = set()
        self._future_context = ""
        self._ctx.reset()
        self._narration.clear()
        self._deep_view.hide()
        self._deep_detail.hide()
        self._future_ctx_panel.hide()

        _q = user_text.replace("\n", " ").strip()[:50]
        if len(user_text.strip()) > 50:
            _q += "…"
        self._set_terminal_title(_q or "Quest AI Runner")

        if echo:
            self._tlog.write(Text(f"❯ {user_text}", style="bold cyan"))
            self._tlog.write(Text(""))

        # Loading strip on; keep input enabled so mid-turn messages can be queued.
        self._activity.set_status("Thinking…")
        self._activity.display = True
        self.query_one("#prompt", PromptTextArea).placeholder = "Type to queue a message for the next step…"

        self._run_stream(user_text)

    @work(thread=True, exclusive=True)
    def _run_stream(self, user_text: str) -> None:
        """Iterate the orchestrator stream in a worker thread."""
        from .core.orchestrator import OrchestratorResult

        s = self.sess
        model_hint = s._model_hint
        if s._replan_next:
            model_hint = "opus"
            s._replan_next = False
            self.call_from_thread(self._console.dim, "  Replan mode: using opus for this turn.")

        final: Optional["OrchestratorResult"] = None
        error: Optional[Exception] = None
        try:
            _inbox = getattr(s._orch, "input_inbox", None)
            _sid = self._session_id
            _pending = (lambda: _inbox.drain(_sid)) if _inbox is not None else None
            for item in s._orch.run_stream(
                user_text,
                transcript=s._last_transcript(),
                quest_id=s._goal_id,
                rep_preamble=s._effective_preamble(),
                model_hint=model_hint,
                pending_inputs=_pending,
                conv_id=s._conv_id,
            ):
                if self._cancel.is_set():
                    break
                if isinstance(item, OrchestratorResult):
                    final = item
                else:
                    self.call_from_thread(self._handle_event, item)
        except Exception as e:  # noqa: BLE001
            error = e

        elapsed = time.monotonic() - self._t0
        self.call_from_thread(self._finish_turn, user_text, final, elapsed,
                              self._cancel.is_set(), error)

    def _handle_event(self, event) -> None:
        """Render one ProgressEvent (runs on the UI thread)."""
        if isinstance(event, dict):
            t = event.get("type", "")
            text = (event.get("text") or "").rstrip()
            action = event.get("action") or ""
            data = event.get("data") or {}
            result_kind = event.get("result_kind") or ""
        else:
            t = event.type
            text = (event.text or "").rstrip()
            action = getattr(event, "action", None) or ""
            data = event.data or {}
            result_kind = getattr(event, "result_kind", None) or ""
        ev = self._types()
        log = self._tlog

        if t == ev["partial"]:
            # Narration beats (the instant ack + the planner's conversational rationale) come as
            # EVENT_PARTIAL tagged data={"narration": True} (legacy: "ack"). Update the live
            # NarrationBar in place so each beat replaces the previous one — beats arrive one at a
            # time as the AI progresses rather than all stacking up at once.
            is_ack = isinstance(data, dict) and (data.get("narration") or data.get("ack"))
            if is_ack:
                if text:
                    self._narration.say(text.strip())
                return
            # Accumulate streamed answer; render once at the end (calm display).
            self._partial_started = True
            self._answer_parts.append(text)
            self._activity.set_status("Answering…")
            return

        if t == ev["status"]:
            self._activity.set_status(text or "Thinking…")

        elif t == ev["plan"]:
            if action == "deep":
                # Don't echo the internal routing rationale — just signal the mode.
                log.write(Text("  ▸ Deep execution", style="dim"))
            elif text:
                label = f"▸ {action}" if action else "▸ plan"
                log.write(Text(f"  {label}  {text}", style="dim"))
            self._activity.set_status("Planning…")

        elif t == ev["replan"]:
            self._ctx.inc_replans()
            if text:
                log.write(Text(f"  ↺ replan  {text}", style="dim"))
            self._activity.set_status("Re-planning…")

        elif t == ev["read"]:
            paths = data.get("sources") or []
            self._ctx.add_sources(paths)
            total = self._ctx.total_sources
            self._activity.set_status(
                f"gathering context  ({total} source{'s' if total != 1 else ''} so far)"
            )

        elif t == ev["context"]:
            card_meta = data.get("card_metadata") or []
            sources = data.get("sources") or []
            log.write(f"[dim]Context: {len(card_meta)} card{'s' if len(card_meta) != 1 else ''}, {len(sources)} source{'s' if len(sources) != 1 else ''}[/dim]")
            if card_meta:
                self._ctx.set_cards(card_meta)
                top = max(card_meta, key=lambda c: c.get("relevance_score", 0) or 0)
                top_title = top.get("title") or top.get("id") or ""
                if top_title:
                    self._set_terminal_title(top_title)
                log.write("[dim]Context cards selected:[/dim]")
                for card in card_meta:
                    cid = card.get("id", "?")
                    title = card.get("title") or "(no title)"
                    adapter = card.get("adapter", "unknown")
                    score = card.get("relevance_score", 0)
                    fcount = card.get("file_count", 0)
                    sstr = f"score: {score:.2f}" if score else "score: unknown"
                    log.write(f"  [cyan]●[/cyan] \\[{adapter}] [dim]{cid}[/dim]: {title}")
                    log.write(f"    [dim]{sstr}, {fcount} file{'s' if fcount != 1 else ''}[/dim]")
                    for f in (card.get("files") or [])[:5]:
                        log.write(f"      [dim]→ {f}[/dim]")
                    extra = len(card.get("files") or []) - 5
                    if extra > 0:
                        log.write(f"      [dim]… and {extra} more files[/dim]")
            if sources:
                log.write("[dim]Sources:[/dim]")
                for src in sources:
                    label = src.get("label", src.get("adapter", "?"))
                    items = src.get("items") or []
                    if items:
                        istr = ", ".join(str(x).split("/")[-1] for x in items[:3])
                        more = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
                        log.write(f"  [dim]• {label}: {istr}{more}[/dim]")

        elif t == ev["exec"]:
            run_id = data.get("run_id") or "default"
            goal = (data.get("goal") or "").strip()
            goal = goal[:1].upper() + goal[1:] if goal else ""
            if run_id not in self._deep_seen:
                self._deep_seen.add(run_id)
                self._deep.add_run(run_id, goal or "Executing work…")
            elif goal:
                # A later event may carry the real subgoal even if the first didn't; keep it current.
                self._deep.update_goal(run_id, goal)
            self._cur_deep_run = run_id
            if text:
                self._deep.update_run_output(run_id, text)
                # Push to detail panel live (it scrolls / holds full history).
                self._deep_detail.push_line(run_id, text)
            # Throttle dashboard redraws to every 10 events to avoid flicker.
            self._deep_event_count += 1
            if self._deep_event_count % 10 == 0:
                n = len(self._deep._runs)
                self._deep_view.show(self._deep.get_dashboard(), n_runs=n)
            self._activity.set_status("Executing…")
            # A terminal phase means this deep task is finished: persist its full
            # output to the scrollback transcript now, so it stays readable after
            # the live deep widgets are gone. Each task leaves a permanent record.
            from .core.guard import classify_exec_phase
            outcome = classify_exec_phase(data.get("phase") if isinstance(data, dict) else None)
            if outcome is not None:
                self._deep.set_run_status(run_id, "done" if outcome == "success" else "error")
                self._flush_deep_run(run_id)

        elif t == ev["milestone"]:
            # A completed deep subtask carries its full output; stash it on the run so the
            # scrollback record can show what the task actually produced (the result), not just
            # the trace of steps it took.
            if isinstance(data, dict):
                rid = data.get("run_id")
                deep_out = data.get("deep_output")
                if rid and deep_out:
                    self._deep.set_final_output(rid, deep_out)
            if text:
                # Show the first sentence of the goal as a clean completion line.
                first = text.split(".")[0].strip()
                label = first if first else text[:70]
                log.write(Text(""))
                log.write(f"  [green bold]✓[/green bold]  {label}")
                log.write(Text(""))

        elif t == ev["result"]:
            # Non-streamed answers arrive here; streamed ones are in _answer_parts.
            if not self._partial_started and text:
                self._answer_parts.append(text)
            # Capture future context from deep result events.
            if result_kind == "deep" and isinstance(data, dict):
                fc = (data.get("future_context") or "").strip()
                if fc:
                    self._future_context = fc

        elif t == ev["decision"]:
            log.write(Text(""))
            log.write(f"  [yellow]?[/yellow] {text}")
        # done: terminal signal only.

    def _finish_turn(self, user_text: str, final, elapsed: float,
                     cancelled: bool, error: Optional[Exception]) -> None:
        """Wrap up a turn on the UI thread: answer, footer, bookkeeping."""
        s = self.sess
        log = self._tlog
        self._activity.display = False
        self._narration.clear()
        self._ctx.display = False
        # Persist any deep task output that didn't already emit a terminal phase
        # (incl. cancelled/errored turns) before the live widgets disappear.
        self._flush_pending_deep_runs()
        self._deep_view.hide()
        self._deep_detail.hide()

        if error is not None:
            friendly = format_provider_error(error)
            log.write(f"  [red]Error:[/red] {friendly}")
        elif cancelled:
            self._console.dim("  Cancelled.")
            s._last_user = user_text
            s._last_assistant = "[cancelled by user]"
            s._session_history.append((user_text, "[cancelled by user]"))
            s._write_session_file()
            ctx = getattr(s._orch, "context_assembler", None)
            if ctx is not None:
                try:
                    ctx.record(user_text, {"kind": "cancelled",
                                           "response": "[turn was cancelled by user]"})
                except Exception:  # noqa: BLE001
                    pass
        elif final is not None:
            self._record_turn(final, elapsed)
            self._maybe_handle_deep_plan(final)   # prints planned changes (once)
            answer = final.text or ("\n".join(self._answer_parts).strip() or None)
            if answer:
                if not self._ai_label_shown:
                    log.write(Text(""))
                    log.write(Text(f"{self.rep_name} (AI):", style="bold cyan"))
                    self._ai_label_shown = True
                self._console.markdown(answer)
            s._last_user = user_text
            # For deep runs, signal completion clearly — goal strings look like unfinished TODOs
            if final.kind == "deep":
                deep_results = final.deep_results or []
                goals = final.goals or []
                all_met = bool(deep_results) and all(d.met for d in deep_results)
                goal_str = ("; ".join(goals))[:300] if goals else ""
                prefix = "Completed" if all_met else "Attempted"
                s._last_assistant = (f"{prefix}: {goal_str}" if goal_str else f"{prefix}.")
            else:
                # Use _answer_parts as fallback: what was displayed may differ from final.text
                # when the answer streamed via EVENT_PARTIAL or when goal-check text lands in
                # final.text instead of the actual user-facing response.
                s._last_assistant = final.text or "\n".join(self._answer_parts).strip()
            s._session_history.append((user_text, s._last_assistant))
            s._write_session_file()
            s._turn_count += 1
            log.write(Text(""))
            self._write_footer(final, elapsed)
            # If the deep run flagged the context it used, load the panel and show a
            # subtle hint in the transcript so the user knows it is available.
            if self._future_context:
                self._future_ctx_panel.load(self._future_context)
                count = sum(
                    1 for ln in self._future_context.splitlines() if ln.strip()
                )
                log.write(Text(
                    f"  [Alt+C] Context it used  ({count})",
                    style="dim",
                ))

        log.write(Text(""))
        self._console.rule()
        log.write(Text(""))

        self._turn_active = False
        inp = self.query_one("#prompt", PromptTextArea)
        inp.placeholder = "Ask anything…   Ctrl+Enter=send   (/help, Esc=cancel, Alt+D=expand, Tab=cycle)"
        inp.focus()

        # Auto-execute a planned-but-unexecuted deep turn (matches interactive.py).
        if not cancelled and error is None and final is not None:
            if self._maybe_handle_deep_plan(final, run=True):
                self._begin_turn(
                    "Execute it. No more planning, just code it and apply changes now.",
                    echo=False, auto=True,
                )

    def _maybe_handle_deep_plan(self, final, run: bool = False) -> bool:
        """Return True if this is a deep turn that planned work but didn't execute.

        When ``run`` is False, also prints the planned changes (once).
        """
        if getattr(final, "kind", None) != "deep":
            return False
        goals = final.goals or []
        if not goals:
            return False
        executed = any(
            getattr(d, "met", False) or (getattr(d, "output", "") or "").strip()
            for d in (final.deep_results or [])
        )
        if executed:
            return False
        if not getattr(self.sess._cfg, "deep_runner", None):
            if not run:
                self._console.dim("  (No deep executor configured; cannot auto-execute)")
            return False
        if not run and not getattr(self, "_deep_plan_shown", False):
            self._console.dim("  Task identified. Planned changes:")
            for i, g in enumerate(goals, 1):
                prefix = "▸ " if i == 1 else "  "
                self._console.dim(f"  {prefix}{g}")
            self._deep_plan_shown = True
        return True

    def _record_turn(self, final, elapsed: float) -> None:
        s = self.sess
        user = s._last_user or ""
        tok_in = getattr(final, "tokens_in", 0) or 0
        tok_out = getattr(final, "tokens_out", 0) or 0
        s._turns.append({
            "user": (user[:60] + "…") if len(user) > 60 else user,
            "model": _model_label(getattr(final, "model", None)),
            "tokens_in": tok_in,
            "tokens_out": tok_out,
            "elapsed": elapsed,
            "timestamp": time.time(),
        })
        self._deep_plan_shown = False

    def _write_footer(self, final, elapsed: float) -> None:
        def _k(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        parts: List[tuple] = []   # (text, style)

        if final.kind == "deep":
            deep_results = final.deep_results or []
            all_met = bool(deep_results) and all(d.met for d in deep_results)
            parts.append(("✓ Done" if all_met else "~ Partial", "green" if all_met else "yellow"))
            deep_tokens = sum(getattr(d, "tokens", 0) for d in deep_results)
            if deep_tokens:
                parts.append((f"{_k(deep_tokens)} tokens", "dim"))
        else:
            steps = getattr(final, "steps", 0)
            if steps:
                parts.append((f"{steps} step{'s' if steps != 1 else ''}", "dim"))
            src = self._ctx.total_sources
            if src:
                parts.append((f"{src} source{'s' if src != 1 else ''}", "dim"))
            if self._ctx.replans:
                parts.append((f"{self._ctx.replans} replan{'s' if self._ctx.replans != 1 else ''}", "dim"))
            tok_in = getattr(final, "tokens_in", 0) or 0
            tok_out = getattr(final, "tokens_out", 0) or 0
            if tok_in or tok_out:
                parts.append((f"↥ {_k(tok_in)} in · ↦ {_k(tok_out)} out", "dim"))

        model_lbl = _model_label(getattr(final, "model", None))
        if model_lbl:
            parts.append((model_lbl, "dim"))
        parts.append((f"{elapsed:.1f}s", "dim"))

        t = Text("  ")
        for i, (label, style) in enumerate(parts):
            if i > 0:
                t.append("  ·  ", style="dim")
            t.append(label, style=style)
        self._tlog.write(t)

    # -- actions ---------------------------------------------------------------

    def action_copy_last(self) -> None:
        """Copy the last AI response to the system clipboard (Ctrl+Y)."""
        text = "\n".join(self._answer_parts).strip() if self._answer_parts else ""
        if not text and self.sess._last_assistant:
            text = self.sess._last_assistant
        if not text:
            self._tlog.write(Text("  (nothing to copy)", style="dim"))
            return
        copied = False
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"],
                    ["wl-copy"]):
            try:
                import subprocess
                subprocess.run(cmd, input=text.encode(), check=True,
                               capture_output=True, timeout=2)
                copied = True
                break
            except Exception:  # noqa: BLE001
                continue
        if copied:
            preview = text[:60].replace("\n", " ")
            self._tlog.write(Text(f"  Copied: {preview}…" if len(text) > 60 else f"  Copied: {preview}", style="dim"))
        else:
            self._tlog.write(Text("  (clipboard tool not found — install xclip or xsel)", style="dim"))

    def action_cancel(self) -> None:
        if self._turn_active:
            self._cancel.set()
            self._activity.set_status("Cancelling…")
        elif self._pending_select is not None:
            self._pending_select = None
            self._console.dim("  Cancelled.")

    def action_clear_log(self) -> None:
        self._tlog.clear()

    def action_quit(self) -> None:
        self.exit()

    def _available_deep_runs(self) -> "OrderedDict[str, dict]":
        """Runs the Alt+D detail panel can open, in chronological order.

        While a turn has live/just-finished runs they win (the live tracker, not yet rebuilt for the
        next turn); otherwise we fall back to the cross-turn archive of finished runs, so Alt+D still
        works after the run is done and even after later (non-deep) turns.
        """
        with self._deep._lock:
            live = OrderedDict(self._deep._runs)
        if live:
            return live
        return OrderedDict(self._deep_archive)

    def _open_detail_for(self, run_id: str) -> None:
        """Open the detail panel for a specific run_id, computing pos/total."""
        runs = self._available_deep_runs()
        info = runs.get(run_id)
        if info is None:
            return
        run_ids = sorted(runs.keys())
        pos = run_ids.index(run_id) + 1 if run_id in run_ids else 1
        total = len(run_ids)
        existing = list(info.get("exec_lines", []))
        self._deep_detail.open_for(run_id, info["goal"], existing, pos=pos, total=total)

    @staticmethod
    def _summarize_exec_lines(lines: List[str]) -> tuple:
        """Condense a deep run's captured exec lines into (activity_summary, narration).

        The scrollback record is a SUMMARY, not a replay: instead of one line per read/write/command
        (a wall the user doesn't want), we roll the mechanical tool actions up into a single counts
        line ("12 reads · 3 edits · 2 commands") and return the worker's own narration separately so
        a run with no structured result can still show what it was doing in its own words.
        """
        reads = writes = cmds = searches = tools = 0
        narration: List[str] = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("Read:"):
                reads += 1
            elif s.startswith(("Write:", "Edit:")):
                writes += 1
            elif s.startswith("$ "):
                cmds += 1
            elif s.startswith(("WebSearch:", "WebFetch:")):
                searches += 1
            elif s.startswith("Using "):
                tools += 1
            elif s.startswith("[thinking]"):
                continue  # internal reasoning — not part of the "what it did" summary
            else:
                narration.append(s)

        def _plural(n: int, one: str, many: str) -> str:
            return f"{n} {one if n == 1 else many}"

        parts: List[str] = []
        if reads:
            parts.append(_plural(reads, "read", "reads"))
        if writes:
            parts.append(_plural(writes, "edit", "edits"))
        if cmds:
            parts.append(_plural(cmds, "command", "commands"))
        if searches:
            parts.append(_plural(searches, "search", "searches"))
        if tools:
            parts.append(_plural(tools, "tool call", "tool calls"))
        return " · ".join(parts), narration

    def _flush_deep_run(self, run_id: str) -> None:
        """Write one deep run's record into the scrollback transcript, once.

        The live deep widgets are ephemeral (hidden when the turn ends), so without this a finished
        task's output would vanish. We write, exactly once per run (tracked in ``_deep_flushed``), a
        compact SUMMARY (not a replay): the SUBGOAL it was assigned (header), a one-line activity
        roll-up of what it touched, and — most important — its FINAL OUTPUT (the worker's own
        summary of what it did). The full step-by-step trace stays available live and in the detail
        panel (Alt+D); the permanent record stays readable.
        """
        if run_id in self._deep_flushed:
            return
        with self._deep._lock:
            info = self._deep._runs.get(run_id)
            snap = dict(info) if info else None
        if not snap:
            return
        lines = [l.strip() for l in (snap.get("exec_lines") or []) if l and l.strip()]
        final_output = (snap.get("final_output") or "").strip()
        # Mark flushed regardless so we never reconsider this run; skip writing an
        # empty block when nothing at all was captured.
        self._deep_flushed.add(run_id)
        if not lines and not final_output:
            return
        # Archive this finished run (with its full captured actions) so Alt+D can replay it later,
        # even after the live tracker is rebuilt for the next turn. Only runs with actual actions are
        # worth keeping (the detail panel shows the step trace); cap to the most recent.
        if lines:
            archived = dict(snap)
            archived["exec_lines"] = list(lines)
            self._deep_archive[run_id] = archived
            while len(self._deep_archive) > self._DEEP_ARCHIVE_MAX:
                self._deep_archive.popitem(last=False)
        log = self._tlog
        goal = (snap.get("goal") or "").strip()
        status = snap.get("status") or "running"
        elapsed = time.time() - snap.get("started", time.time())
        mins, secs = divmod(int(elapsed), 60)
        time_str = f"{mins}m{secs}s" if mins else f"{secs}s"
        summary, narration = self._summarize_exec_lines(lines)

        log.write(Text(""))
        # 1) The subgoal this task was assigned — the most important orientation.
        log.write(Text(f"⎅ {goal}" if goal else "⎅ Deep task", style="bold cyan"))
        # 2) A one-line roll-up of activity, instead of every read/write/command.
        if summary:
            log.write(Text(f"  {summary}", style="dim"))
        if final_output:
            log.write(Text(""))
            log.write(Text("  result", style="bold green"))
            self._console.markdown(final_output)
        elif narration:
            # No structured result (e.g. an errored/incomplete run): fall back to the worker's own
            # words so the record still says what it was doing, capped so it never becomes a wall.
            tail = narration[-6:]
            if len(narration) > len(tail):
                log.write(Text(f"  … {len(narration) - len(tail)} earlier steps", style="dim"))
            for nl in tail:
                log.write(Text(f"  {nl}"))
        if status == "error":
            log.write(Text(f"  ✗ deep task ended with an error · {time_str}", style="red"))
        else:
            log.write(Text(f"  ✓ deep task complete · {time_str}", style="green"))
        # Point at the full per-action trace (the summary above is a roll-up). Only when there are
        # actions to replay; the panel stays available after the turn via the archive.
        if lines:
            log.write(Text("  Alt+D: see every action", style="dim"))
        log.write(Text(""))

    def _flush_pending_deep_runs(self) -> None:
        """Flush any deep runs that didn't already emit a terminal phase.

        Covers runs with no explicit done/error tick and the cancel/error paths.
        """
        with self._deep._lock:
            run_ids = sorted(self._deep._runs.keys())
        for rid in run_ids:
            self._flush_deep_run(rid)

    def action_toggle_deep_detail(self) -> None:
        """Toggle the expanded detail view for the current deep run (key: Alt+D).

        Alt+D (not a bare 'd') because the prompt Input consumes printable keys, so a plain 'd'
        would be typed into the message instead of toggling. Alt+D is not consumed and fires reliably.
        """
        if self._deep_detail.display:
            self._deep_detail.hide()
            return
        runs = self._available_deep_runs()
        if not runs:
            return
        # Prefer the run currently/last streaming this turn; otherwise open the most recent finished
        # one (last inserted), so Alt+D shows a deep task's actions even after it's done.
        run_id = self._cur_deep_run or self._deep.get_active_run()
        if run_id not in runs:
            run_id = next(reversed(runs))
        self._open_detail_for(run_id)

    def action_cycle_deep_run(self) -> None:
        """Cycle the detail panel to the next deep run (key: Tab).

        Cycles over whatever runs are available — the live tracker during a turn, or the finished-run
        archive afterwards — keyed off the panel's currently-shown run so it works post-turn too.
        """
        runs = self._available_deep_runs()
        if not runs:
            return
        run_ids = sorted(runs.keys())
        cur = self._deep_detail.active_run_id
        if cur in run_ids:
            run_id = run_ids[(run_ids.index(cur) + 1) % len(run_ids)]
        else:
            run_id = run_ids[0]
        self._open_detail_for(run_id)

    def action_toggle_future_context(self) -> None:
        """Toggle the "context it used" panel (key: Alt+C).

        Alt+C is used (not a bare letter) because the prompt Input consumes printable keys, so a
        plain 'c' would be typed into the message instead of toggling. Only opens when there is
        non-empty context from the most recent deep run. Pressing Alt+C again closes it; when there
        is nothing to show the action is a no-op.
        """
        if self._future_ctx_panel.display:
            self._future_ctx_panel.display = False
        elif self._future_context:
            self._future_ctx_panel._rerender()
            self._future_ctx_panel.display = True

    def action_scroll_up_or_agent(self) -> None:
        """PageUp: scroll transcript up; if agent detail panel is open, scroll that instead."""
        if self._deep_detail.display:
            self._deep_detail.page_back()
        else:
            self._tlog.scroll_page_up(animate=False)

    def action_scroll_down_or_agent(self) -> None:
        """PageDown: scroll transcript down; if agent detail panel is open, scroll that instead."""
        if self._deep_detail.display:
            self._deep_detail.page_forward()
        else:
            self._tlog.scroll_page_down(animate=False)

    def on_mouse_scroll_up(self, event) -> None:
        """Mouse wheel up — routes to the active scroll target, same logic as PageUp."""
        event.stop()
        delta = self.scroll_sensitivity_y
        if self._deep_detail.display:
            self._deep_detail._follow = False
            self._deep_detail.scroll_to(
                y=max(0, self._deep_detail.scroll_target_y - delta),
                animate=False,
            )
        else:
            self._tlog.scroll_to(
                y=max(0, self._tlog.scroll_target_y - delta),
                animate=False,
            )

    def on_mouse_scroll_down(self, event) -> None:
        """Mouse wheel down — routes to the active scroll target, same logic as PageDown."""
        event.stop()
        delta = self.scroll_sensitivity_y
        if self._deep_detail.display:
            self._deep_detail.scroll_to(
                y=self._deep_detail.scroll_target_y + delta,
                animate=False,
            )
            if self._deep_detail.scroll_y >= self._deep_detail.max_scroll_y - 1:
                self._deep_detail._follow = True
        else:
            self._tlog.scroll_to(
                y=self._tlog.scroll_target_y + delta,
                animate=False,
            )


if __name__ == "__main__":
    # Manual smoke test requires a configured RunnerConfig; see textual_session.py.
    raise SystemExit("Run via: Quest AI Runner chat")
