"""Textual UI widgets: prompt placeholders, the RichLog console and log handler, and the activity, context, deep-run and future-context panels.

Split out of ``textual_ui.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from textual.app import ComposeResult
from typing import Dict, List, Optional
from textual.widgets import RichLog, Static
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.containers import VerticalScroll

from .chat_conversations import chat_log_path
from .interactive_session import _Console
from .interactive_session_tracker import format_elapsed
from .textual_ui_helpers import _build_future_context_text, ellipsize, notice_line


# Prompt placeholder shown while a turn is waiting on the user's reply to a decision question
# raised mid-turn (EVENT_DECISION) — makes it obvious the AI is paused for input, not just idle.
_AWAITING_DECISION_PLACEHOLDER = "Type your answer to the question above…"


# Prompt placeholder shown while a turn is running — makes it explicit that typing now is safe
# and queues, rather than looking like the input is simply dead until the AI finishes.
_BUSY_PLACEHOLDER = "Type to queue a message for the next step…"


def _busy_placeholder(queued: int) -> str:
    if not queued:
        return _BUSY_PLACEHOLDER
    return f"{_BUSY_PLACEHOLDER} ({queued} queued)"


def newline_hint(environ=os.environ) -> str:
    """The newline keys to advertise in this terminal.

    VTE terminals (GNOME Terminal, Ptyxis, Tilix, Terminator; all set ``VTE_VERSION``) send
    Shift+Enter as the same carriage return as Enter and speak neither the kitty keyboard
    protocol nor modifyOtherKeys, so Shift+Enter cannot reach the prompt there. Advertise only
    the keys that do.
    """
    if environ.get("VTE_VERSION"):
        return "Ctrl+J or Alt+Enter=newline"
    return "Shift+Enter or Ctrl+J=newline"


_READY_PLACEHOLDER = (
    f"Ask anything…   Enter=send, {newline_hint()}   (/help, Esc=cancel, Alt+D=expand, Tab=cycle)"
)


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
# Python logging used to be copied into the transcript record by record (time, level, logger name,
# the whole message), so a library's chatty warning or a long internal one buried the conversation
# under wrapped lines. The transcript now gets one short line per distinct problem; the full record
# always goes to a log file.

# The longest warning line the transcript shows, and how many distinct ones before the rest go to
# the log file only (a handful is information, a dozen is noise).
LOG_LINE_CHARS = 110
LOG_LINES_SHOWN = 8
LOG_FILE_BYTES = 1_000_000
LOG_FILE_BACKUPS = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Records from these loggers are this package's own; every other logger is a third-party library.
OWN_LOGGER_PREFIXES = ("quest_ai_runner", "quest-ai-runner")


def is_own_logger(name: str) -> bool:
    """True for a logger of this package, False for a library's."""
    return name.startswith(OWN_LOGGER_PREFIXES)


def short_log_text(message: str, limit: int = LOG_LINE_CHARS) -> str:
    """The first sentence of a log message on one line, at most ``limit`` characters.

    A sentence that is still too long loses a trailing parenthesis first, then is cut at its last
    ``;`` or ``:`` when that keeps most of it (a whole clause reads better than a chopped word),
    else on a word boundary, with an ellipsis.
    """
    lines = [ln for ln in str(message).splitlines() if ln.strip()]
    first = " ".join(lines[0].split()) if lines else ""
    end = re.search(r"[.!?](?=\s|$)", first)
    if end:
        first = first[:end.start()]
    first = first.strip()
    if len(first) > limit:
        first = re.sub(r"\s*\([^()]*\)$", "", first)
    if len(first) <= limit:
        return first
    clause = max(first.rfind(sep, 0, limit) for sep in (";", ":"))
    if clause >= limit * 0.6:
        return first[:clause].rstrip() + "…"
    return ellipsize(first, limit)


class QuietRotatingFileHandler(RotatingFileHandler):
    """A rotating log file whose own failures (full disk, unwritable folder) never reach the screen."""

    def handleError(self, record: logging.LogRecord) -> None:
        pass


def open_log_file(path: Path) -> Optional[logging.Handler]:
    """The capped log file handler for ``path``, or None when its folder cannot be made. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = QuietRotatingFileHandler(
            path, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8", delay=True,
        )
    except Exception:  # noqa: BLE001
        return None
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


class _RichLogHandler(logging.Handler):
    """Routes Python logging for the chat: the full record to a file, a short line to the transcript.

    Every record reaching this handler is written in full to ``log_path`` (rotated, size capped).
    The transcript gets at most one dim ``⚠`` line per distinct problem from this package's own
    loggers: the first sentence, no time, level or logger name, with one hint to the file the first
    time. A library's records (``huggingface_hub`` and the like) never appear there. With ``-v``
    (``verbosity`` 1 or more) every record is written in full to the transcript, as before.
    """

    def __init__(self, app: "QuestAITerminal", log: RichLog, verbosity: int = 0,
                 log_path: Optional[Path] = None) -> None:
        super().__init__()
        self._app = app
        self._tlog = log
        self.verbosity = verbosity
        self.log_path = Path(log_path) if log_path is not None else chat_log_path()
        self.file_handler = open_log_file(self.log_path)
        self.shown: Dict[str, int] = {}  # short line -> times seen, for the lines already shown
        self.hinted = False
        self.quieted = False
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def close(self) -> None:
        if self.file_handler is not None:
            self.file_handler.close()
        super().close()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.file_handler is not None:
                self.file_handler.handle(record)
            lines = self.transcript_lines(record)
            if lines:
                self.show(lines)
        except Exception:  # noqa: BLE001
            # A logging problem must never reach the screen; the file already has the record.
            pass

    def transcript_lines(self, record: logging.LogRecord) -> List[Text]:
        """What this record adds to the transcript: nothing, one short line, or the verbose line."""
        if self.verbosity >= 1:
            text = Text.from_ansi(self.format(record))
            text.no_wrap = False
            return [text]
        if not is_own_logger(record.name):
            return []
        short = short_log_text(record.getMessage(), self.line_limit())
        if not short:
            return []
        if short in self.shown:
            self.shown[short] += 1
            return []
        if self.quieted:
            return []
        if len(self.shown) >= LOG_LINES_SHOWN:
            self.quieted = True
            return [notice_line("⚠ More warnings are in the log file only")]
        self.shown[short] = 1
        lines = [notice_line(f"⚠ {short}")]
        if not self.hinted:
            self.hinted = True
            lines.append(notice_line(self.hint()))
        return lines

    def line_limit(self) -> int:
        """Longest warning text that still fits one line of this terminal (``LOG_LINE_CHARS`` at most)."""
        try:
            width = self._app.size.width
        except Exception:  # noqa: BLE001
            return LOG_LINE_CHARS
        # The transcript's padding and scrollbar, then the "  ⚠ " that starts the line.
        return min(LOG_LINE_CHARS, width - 8) if width >= 40 else LOG_LINE_CHARS

    def hint(self) -> str:
        """Where the details are, once."""
        if self.file_handler is None:
            return "Details: run with -v"
        home = str(Path.home())
        shown = str(self.log_path)
        if shown.startswith(home + os.sep):
            shown = "~" + shown[len(home):]
        return f"Details: {shown}, or run with -v"

    def show(self, lines: List[Text]) -> None:
        """Write ``lines`` to the transcript on the UI thread (thread-safe)."""
        def write_all():
            for line in lines:
                self._tlog.write(line)

        thread_id = getattr(self._app, "_ui_thread_id", None)
        if thread_id is not None and threading.get_ident() != thread_id:
            self._app.call_from_thread(write_all)
        else:
            write_all()


# ── Live widgets ────────────────────────────────────────────────────────────

def with_elapsed(text: str, seconds: Optional[int]) -> str:
    """A status line followed by how long the wait has lasted (unchanged when ``seconds`` is None)."""
    if seconds is None:
        return text
    clock = format_elapsed(seconds)
    return f"{text} {clock}" if text.endswith("…") else f"{text}  · {clock}"


class ActivityBar(Static):
    """Three animated dots + status text in one line.

    Replaces the separate LoadingIndicator + StatusLine pair so we fully
    control dot count, inter-dot spacing, and the gap before the text.

    A turn's clock (``start_clock`` / ``stop_clock``) adds the elapsed seconds to the line once a
    wait passes ``SHOW_ELAPSED_AFTER``, so a long silent step (a deep run preparing) never looks
    frozen. It runs from the start of the turn, not from the last status change, and the existing
    interval timer repaints it, so it ticks once a second with no extra timer or flicker.
    """

    _INTERVAL = 0.45
    # A wait shorter than this shows no clock: a counter on every instant step would be noise.
    SHOW_ELAPSED_AFTER = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._text = "Thinking…"
        self._frame = 0
        self._timer = None
        self.display = False
        # Messages the user typed and queued while this turn runs. Tracked separately from
        # ``_text`` (which every status update below overwrites) so the count survives the
        # whole turn instead of vanishing the next time the AI's own status line changes.
        self._queued = 0
        self.wait_started: Optional[float] = None
        self.clock = time.monotonic

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._INTERVAL, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % 3
        self.refresh()

    def set_status(self, text: str) -> None:
        # Status lines start with an uppercase letter, whoever wrote them.
        self._text = text[:1].upper() + text[1:]
        self.refresh()

    def start_clock(self) -> None:
        """Start counting the wait from now (call once per turn; a status change does not reset it)."""
        self.wait_started = self.clock()
        self.refresh()

    def stop_clock(self) -> None:
        """Stop showing elapsed time (the deep dashboard took over, or the turn ended)."""
        if self.wait_started is not None:
            self.wait_started = None
            self.refresh()

    def elapsed_seconds(self) -> Optional[int]:
        """Whole seconds waited, or None while the clock is off or the wait is still short."""
        if self.wait_started is None:
            return None
        seconds = int(self.clock() - self.wait_started)
        return seconds if seconds >= self.SHOW_ELAPSED_AFTER else None

    def set_queued(self, n: int) -> None:
        self._queued = n
        self.refresh()

    def render(self):
        t = Text()
        for i in range(3):
            if i > 0:
                t.append(" ")
            style = "cyan bold" if i == self._frame else "cyan dim"
            t.append("●", style=style)
        t.append("  ")
        t.append(with_elapsed(self._text, self.elapsed_seconds()), style="dim")
        if self._queued:
            t.append("  ")
            t.append(
                f"· {self._queued} message{'s' if self._queued != 1 else ''} waiting",
                style="bold yellow",
            )
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
        self._line_map: Dict[int, str] = {}  # row (0-based) -> run_id, for click-to-expand
        self.display = False

    def show(self, dashboard: str, n_runs: int = 1, line_map: Optional[Dict[int, str]] = None) -> None:
        self._dashboard = dashboard or ""
        self._n_runs = n_runs
        self._line_map = line_map or {}
        self.display = bool(self._dashboard.strip())
        self.refresh()

    def hide(self) -> None:
        self._dashboard = ""
        self._n_runs = 0
        self._line_map = {}
        self.display = False
        self.refresh()

    def render(self):
        if not self._dashboard.strip():
            return Text("")
        hint = ("  [Alt+D/click] expand & scroll  [Tab] next agent" if self._n_runs > 1
                else "  [Alt+D/click] expand & scroll full output")
        t = Text.from_ansi(self._dashboard + f"\x1b[2m\n{hint}\x1b[0m")
        t.no_wrap = False
        return t

    def on_click(self, event) -> None:
        """Click a run's block to expand it (the same target Alt+D would open for it).

        DeepActivity renders every concurrent run as one shared block of text (no per-run
        sub-widgets), so a click is hit-tested by ROW against ``_line_map`` (built alongside the
        dashboard text in ``_DeepRunTracker._render_dashboard``) rather than routed to a child
        widget. A row with no mapped run (e.g. the trailing hint line) is a no-op and the click
        bubbles up to the App's default "refocus the prompt" handler.
        """
        run_id = self._line_map.get(event.y)
        if run_id is None:
            return
        event.stop()
        app = self.app
        app._cur_deep_run = run_id
        if app._deep_detail.display and app._deep_detail.active_run_id == run_id:
            app._deep_detail.hide()
            app._refresh_deep_dashboard()
        else:
            app._open_detail_for(run_id)


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

    @property
    def has_bullets(self) -> bool:
        """True when a turn loaded something for Alt+C to open (a deep run's context or a short
        answer's context cards)."""
        return bool(self._bullets)

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
