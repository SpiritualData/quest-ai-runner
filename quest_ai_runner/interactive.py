"""Interactive (attended) session: a polished multi-turn REPL over the brain.

Streams every ``ProgressEvent`` to the terminal in real time. The design goal is a
terminal experience as nice to use as a first-class coding assistant:

* an animated spinner on a background thread while the brain is thinking,
* partial reply chunks that type out in place,
* genuinely dim, single-line "chatter" (plan/read/status) that updates rather than
  scrolls, so the transcript stays scannable,
* clean per-turn separators and distinct ``You`` / ``AI`` speaker labels,
* ESC to cancel the current turn while it streams, Ctrl+C to clear the input line
  (never exit), Ctrl+D to exit.

It keeps a rolling transcript so follow-up messages share context.

Install the [tui] extra for the full experience (prompt_toolkit input handling +
rich rendering); it degrades to plain ``input()``/``print`` when neither is present::

    pip install quest-ai-runner[tui]

Usage (via CLI)::

    quest-ai-runner chat
    quest-ai-runner chat --goal-id <id>   # attach results to a Quest goal
"""
from __future__ import annotations

import sys
import threading
import time
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import Orchestrator, ProgressEvent

# ── Optional dependencies ([tui] extra) ──────────────────────────────────────
#
# Both are optional. The session degrades gracefully:
#   * no prompt_toolkit  → plain input(), Ctrl+C/ESC handling is best-effort,
#   * no rich            → ANSI escapes via the small helpers below.

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    _HAS_PROMPT_TOOLKIT = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_PROMPT_TOOLKIT = False

try:
    from rich.console import Console
    _HAS_RICH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_RICH = False

try:  # rich.Live is in the same package, but guard it independently.
    from rich.live import Live as _RichLive  # noqa: F401
    from rich.text import Text as _RichText  # noqa: F401
    _HAS_RICH_LIVE = _HAS_RICH
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_RICH_LIVE = False


# ── Plain-ANSI fallbacks (used when rich is absent) ───────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_BLUE = "\033[34m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"


def _ansi(code: str, s: str) -> str:
    return f"{code}{s}{_RESET}"


# ── A small console wrapper so the rest of the file is render-engine-agnostic ─


class _Console:
    """Thin print surface. Uses rich when available, ANSI escapes otherwise.

    The whole session talks to this; swapping in rich is purely a quality bump.
    """

    def __init__(self) -> None:
        self._rich = Console(highlight=False, soft_wrap=True) if _HAS_RICH else None
        # Best-effort colour detection for the plain path.
        self._color = sys.stdout.isatty()

    # -- low-level ----------------------------------------------------------
    def write(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def line(self, s: str = "") -> None:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()

    # -- styled lines -------------------------------------------------------
    def dim(self, s: str) -> None:
        if self._rich:
            self._rich.print(s, style="dim", highlight=False)
        elif self._color:
            self.line(_ansi(_DIM, s))
        else:
            self.line(s)

    def speaker(self, label: str, color: str, text: str) -> None:
        """Print a speaker-prefixed line: bold coloured ``label``, then ``text``."""
        if self._rich:
            self._rich.print(f"[bold {color}]{label}[/]  {text}", highlight=False)
        elif self._color:
            ansi = _GREEN if color == "green" else (_CYAN if color == "cyan" else _BLUE)
            self.line(f"{_BOLD}{ansi}{label}{_RESET}  {text}")
        else:
            self.line(f"{label}  {text}")

    def rule(self) -> None:
        """A faint horizontal separator between turns."""
        width = self._term_width()
        if self._rich:
            self._rich.print("─" * width, style="dim", highlight=False)
        elif self._color:
            self.line(_ansi(_DIM, "─" * width))
        else:
            self.line("─" * width)

    def _term_width(self) -> int:
        try:
            import shutil
            return max(8, min(shutil.get_terminal_size((80, 24)).columns, 100))
        except Exception:  # noqa: BLE001  # pragma: no cover
            return 60


# ── Animated status line: a spinner + a live, accumulating context panel ──────
#
# The context panel is the centrepiece of the attended experience: as the brain
# gathers grounding (reads, greps, source/operation lookups) it streams a READ
# event per batch, and we render each as a freshly-discovered source appearing at
# the BOTTOM of a bounded, in-place list — so the user literally watches the AI
# consult their corpus before it answers. The spinner line sits ABOVE the list
# and names the current phase.

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _term_height() -> int:
    try:
        import shutil
        return max(8, shutil.get_terminal_size((80, 24)).lines)
    except Exception:  # noqa: BLE001  # pragma: no cover
        return 24


class _StatusLine:
    """A spinner line plus a bounded, in-place "context" panel, redrawn together.

    The spinner cycles on a background thread (~12fps). The main thread updates the
    phase text via :meth:`set` and appends discovered sources via :meth:`add_source`.
    Both the spinner line and the accumulating source list are rewritten in place
    (cursor-up rewrite) so the panel updates without scrolling the terminal.

    Rendering backends, in order of preference:
      * ``rich.Live`` when available (clean, flicker-free in-place redraw),
      * a plain ANSI cursor-up rewrite otherwise.

    No-op (prints nothing, spins nothing) when stdout is not a TTY, so piped/captured
    runs stay clean — the caller prints plain lines on that path instead.

    The thread/stop pattern is preserved: :meth:`start` spins, :meth:`stop` halts and
    erases everything the panel drew.
    """

    # How many source rows the panel may show at once (excluding spinner + overflow).
    _MAX_VISIBLE = 5

    def __init__(self, console: _Console) -> None:
        self._console = console
        self._tty = sys.stdout.isatty()
        self._text = "thinking"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frame = 0
        # Accumulated source labels (most-recent kept; overflow becomes a counter).
        self._sources: List[str] = []
        self._source_total = 0
        # Number of terminal lines the panel currently occupies (for the rewrite).
        self._drawn_lines = 0
        self._live = None  # a rich.Live handle when that backend is active

    # -- footprint ----------------------------------------------------------
    def _max_rows(self) -> int:
        """Total panel height cap: never exceed min(8, terminal_height // 3) lines."""
        cap = min(8, max(1, _term_height() // 3))
        # Reserve one line for the spinner; the rest is the source list (+ overflow).
        return max(1, cap - 1)

    def _visible_count(self) -> int:
        # Visible source rows = the smaller of the soft cap and the height budget,
        # leaving one row for the "… and N more" line when there's overflow.
        budget = self._max_rows()
        return max(1, min(self._MAX_VISIBLE, budget - 1 if self._source_total else budget))

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not self._tty:
            return
        self._stop.clear()
        if _HAS_RICH_LIVE and self._console._rich is not None and self._live is None:
            try:
                self._live = _RichLive(
                    self._render_rich_with(_SPINNER_FRAMES[0]),
                    console=self._console._rich,
                    refresh_per_second=12,
                    transient=True,
                    auto_refresh=False,
                )
                self._live.start()
            except Exception:  # noqa: BLE001  # pragma: no cover - rich edge cases
                self._live = None
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def set(self, text: str) -> None:
        """Update the phase text shown next to the spinner."""
        with self._lock:
            self._text = text or self._text

    def add_source(self, label: str) -> None:
        """Record a freshly-discovered grounding source; it appears at the bottom."""
        if not label:
            return
        with self._lock:
            self._source_total += 1
            self._sources.append(label)
            # Keep only the most-recent visible rows; the rest collapse into the
            # overflow counter, so the panel stays bounded regardless of read count.
            keep = self._visible_count()
            if len(self._sources) > keep:
                self._sources = self._sources[-keep:]

    def source_total(self) -> int:
        with self._lock:
            return self._source_total

    def stop(self) -> None:
        """Stop the spinner and erase everything the panel drew."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None
        self._erase()

    # -- internals ----------------------------------------------------------
    def _spin(self) -> None:
        while not self._stop.is_set():
            self._render()
            time.sleep(0.08)

    def _spinner_glyph(self) -> str:
        glyph = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
        self._frame += 1
        return glyph

    def _panel_lines(self, glyph: str) -> List[str]:
        """Compose the panel as a list of plain (unstyled) text lines."""
        lines = [f"  {glyph} {self._text}"]
        if self._sources:
            lines.append("")
            hidden = self._source_total - len(self._sources)
            for src in self._sources:
                lines.append(f"  ↗  {src}")
            if hidden > 0:
                lines.append(f"     … and {hidden} more")
        return lines

    # -- plain ANSI backend -------------------------------------------------
    def _render(self) -> None:
        with self._lock:
            if self._live is not None:
                try:
                    glyph = self._spinner_glyph()
                    self._live.update(self._render_rich_with(glyph), refresh=True)
                    return
                except Exception:  # noqa: BLE001  # pragma: no cover
                    self._live = None
            glyph = self._spinner_glyph()
            lines = self._panel_lines(glyph)
        out = []
        # Move the cursor back up over whatever we drew last frame, then redraw each
        # line clearing to end-of-line so a shorter frame leaves no stale characters.
        if self._drawn_lines:
            out.append(f"\033[{self._drawn_lines}A")
        out.append("\r")
        for i, ln in enumerate(lines):
            out.append("\033[2K")
            out.append(f"{_DIM}{ln}{_RESET}")
            if i < len(lines) - 1:
                out.append("\n")
        # If this frame is shorter than the last, blank the leftover lines below.
        leftover = self._drawn_lines - len(lines)
        if leftover > 0:
            for _ in range(leftover):
                out.append("\n\033[2K")
            out.append(f"\033[{leftover}A")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._drawn_lines = len(lines)

    def _render_rich_with(self, glyph: str):
        # Build the rich renderable for a given (already-advanced) glyph.
        body = _RichText()
        body.append(f"  {glyph} {self._text}", style="dim")
        if self._sources:
            body.append("\n")
            hidden = self._source_total - len(self._sources)
            for src in self._sources:
                body.append(f"\n  ↗  {src}", style="dim cyan")
            if hidden > 0:
                body.append(f"\n     … and {hidden} more", style="dim")
        return body

    def _erase(self) -> None:
        if not self._tty:
            return
        if self._live is not None:
            try:
                self._live.stop()  # transient=True clears its drawn region
            except Exception:  # noqa: BLE001  # pragma: no cover
                pass
            self._live = None
            self._drawn_lines = 0
            return
        # Plain path: walk back to the top of the panel and clear each line.
        out = []
        if self._drawn_lines > 1:
            out.append(f"\033[{self._drawn_lines - 1}A")
        out.append("\r")
        for i in range(self._drawn_lines):
            out.append("\033[2K")
            if i < self._drawn_lines - 1:
                out.append("\n")
        if self._drawn_lines > 1:
            out.append(f"\033[{self._drawn_lines - 1}A")
        out.append("\r")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._drawn_lines = 0


# ── Event rendering ───────────────────────────────────────────────────────────

# Lazy import — these live in core.adapters; pulled in at render time so the module
# never hard-imports core at import time (avoids circular-import surprises).
_EVENT_TYPES = None


def _event_types():
    global _EVENT_TYPES
    if _EVENT_TYPES is None:
        from .core.adapters import (
            EVENT_DECISION, EVENT_DONE, EVENT_EXEC, EVENT_MILESTONE,
            EVENT_PARTIAL, EVENT_PLAN, EVENT_READ, EVENT_REPLAN, EVENT_RESULT,
            EVENT_STATUS,
        )
        _EVENT_TYPES = {
            "status": EVENT_STATUS, "plan": EVENT_PLAN, "read": EVENT_READ,
            "replan": EVENT_REPLAN, "partial": EVENT_PARTIAL, "exec": EVENT_EXEC,
            "result": EVENT_RESULT, "decision": EVENT_DECISION,
            "milestone": EVENT_MILESTONE, "done": EVENT_DONE,
        }
    return _EVENT_TYPES


def _source_label(event: "ProgressEvent", phase_text: str) -> str:
    """Derive a human-readable grounding-source label for one READ batch.

    A READ event carries only a batch count in ``event.data["reads"]`` (the brain
    doesn't ship per-file paths over the event boundary), so we build a descriptive
    label from the latest phase the planner announced: a ``grep``/``search`` phase
    becomes a search line, an ``explore`` phase a source-catalog line, and a plain
    read a "reading sources" line. The batch count makes multi-read steps read like
    several sources landing at once.
    """
    n = 0
    try:
        n = int((event.data or {}).get("reads") or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        n = 0
    phase = (phase_text or "").lower()
    if "search" in phase or "grep" in phase:
        verb = "search"
    elif "explor" in phase:
        verb = "catalog"
    else:
        verb = "read"

    if verb == "search":
        return f"grep → {n} match{'es' if n != 1 else ''}" if n else "grep → matched"
    if verb == "catalog":
        return f"catalog → {n} source{'s' if n != 1 else ''}" if n else "catalog"
    if n > 1:
        return f"read {n} sources"
    return "read source"


class _TurnRenderer:
    """Renders one turn's event stream, foregrounding the context-gather phase.

    The differentiator of this runner is that every answer is grounded in a real
    corpus. So the gather phase gets a first-class, live "context" panel: as READ
    events arrive, each is rendered as a freshly-found source dropping into the
    bottom of a bounded in-place list (older ones shift up; overflow collapses into
    a "… and N more" counter). The user watches the AI consult their corpus, then —
    the moment the answer starts streaming — the panel collapses to a single dim
    footer ("context: N sources · M replans") so the link between "I gathered" and
    "now I'm answering" is explicit.

    Responsibilities, in order of visual prominence:
      * READ → a live source row in the context panel (the headline experience).
      * PLAN/REPLAN/STATUS/EXEC → the spinner's phase label above the panel.
      * MILESTONE → a printed green line (a real checkpoint worth keeping).
      * PARTIAL → typed out in place under the ``AI`` label, no per-chunk newline.
      * RESULT → printed in full under the ``AI`` label (when no partials streamed).
      * DECISION → a printed amber question.

    The spinner/panel is paused around any real printed output so the two never
    fight for the line. On a non-TTY (piped/logged) stdout, the panel is inert: each
    read prints as a plain ``  ↗ label`` line and the summary footer is omitted, so
    captured runs stay clean.
    """

    def __init__(self, console: _Console, status: _StatusLine) -> None:
        self._c = console
        self._status = status
        self._tty = sys.stdout.isatty()
        self._in_partial = False      # currently streaming partial chunks
        self._streamed_partial = False  # at least one partial chunk arrived
        self._ai_label_shown = False  # printed the "AI" speaker label yet
        self._spinning = False
        # Gather-phase bookkeeping for the context summary footer.
        self._phase_text = "thinking"   # latest phase the planner announced
        self._source_count = 0          # READ batches gathered this turn
        self._replans = 0               # REPLAN events seen this turn
        self._summary_shown = False     # printed the dim context footer yet

    # -- spinner gating -----------------------------------------------------
    def begin(self) -> None:
        self._status.set("thinking")
        self._status.start()
        self._spinning = True

    def _pause_spinner(self) -> None:
        if self._spinning:
            self._status.stop()
            self._spinning = False

    def _resume_spinner(self) -> None:
        if not self._spinning and not self._in_partial:
            self._status.start()
            self._spinning = True

    def _set_phase(self, text: str) -> None:
        self._phase_text = text or self._phase_text
        self._status.set(self._phase_text)

    # -- context summary footer, printed once the answer begins -------------
    def _show_context_summary(self) -> None:
        """Collapse the live panel and print the one-line grounding footer.

        Made evident exactly once, when the answer starts: it ties the gathered
        context to the answer that follows. Omitted on non-TTY and when nothing was
        gathered (no corpus consulted → no claim to make).
        """
        if self._summary_shown:
            return
        self._summary_shown = True
        self._pause_spinner()  # collapses the live context panel
        if not self._tty or self._source_count == 0:
            return
        n = self._source_count
        parts = [f"context: {n} source{'s' if n != 1 else ''}"]
        if self._replans:
            parts.append(f"{self._replans} replan{'s' if self._replans != 1 else ''}")
        footer = "  ✓ " + "  ·  ".join(parts)
        if self._c._rich:
            self._c._rich.print(footer, style="dim", highlight=False)
        elif self._c._color:
            self._c.line(_ansi(_DIM, footer))
        else:
            self._c.line(footer)
        self._c.line("")

    # -- the AI speaker label, lazily printed once content starts -----------
    def _ensure_ai_label(self) -> None:
        if not self._ai_label_shown:
            self._show_context_summary()
            self._pause_spinner()
            # Bold "AI" prefix; content follows on the same line.
            if self._c._rich:
                self._c._rich.print("[bold cyan]AI[/]  ", end="")
            elif self._c._color:
                self._c.write(f"{_BOLD}{_CYAN}AI{_RESET}  ")
            else:
                self._c.write("AI  ")
            self._ai_label_shown = True

    # -- render one event ---------------------------------------------------
    def render(self, event: "ProgressEvent") -> None:
        t = event.type
        text = (event.text or "").rstrip()
        ev = _event_types()

        if t == ev["partial"]:
            # Stream the chunk in place, typing it out under the AI label.
            chunk = event.text or ""
            if not self._in_partial:
                self._ensure_ai_label()
                self._in_partial = True
            self._streamed_partial = True
            self._c.write(chunk)
            return

        # Any non-partial event ends an in-progress partial stream.
        if self._in_partial:
            self._c.line("")
            self._in_partial = False

        if t == ev["plan"]:
            self._set_phase(text or "planning…")
            self._resume_spinner()
        elif t == ev["replan"]:
            self._replans += 1
            self._set_phase("replanning with context…")
            self._resume_spinner()
        elif t == ev["status"]:
            # Status ticks describe the *next* gather phase (reading/searching/…); keep
            # them as the phase label so the READ that follows is labelled correctly.
            self._set_phase(text or "working")
            self._resume_spinner()
        elif t == ev["read"]:
            # The headline: a freshly-discovered source lands in the context panel.
            self._source_count += 1
            label = _source_label(event, self._phase_text)
            if self._tty:
                self._status.add_source(label)
                self._status.set(
                    f"gathering context  ({self._source_count} source"
                    f"{'s' if self._source_count != 1 else ''} so far)"
                )
                self._resume_spinner()
            else:
                # Non-TTY: no cursor magic — print each read as a plain line.
                self._c.line(f"  ↗ {label}")
        elif t == ev["exec"]:
            self._set_phase(text or "running")
            self._resume_spinner()
        elif t == ev["milestone"]:
            self._pause_spinner()
            if self._c._rich:
                self._c._rich.print(f"  [green]✓[/] {text}", highlight=False)
            elif self._c._color:
                self._c.line(f"  {_GREEN}✓{_RESET} {text}")
            else:
                self._c.line(f"  ✓ {text}")
            self._resume_spinner()
        elif t == ev["result"]:
            # When partials already streamed the body live, the result event is the
            # same text again — don't re-print it. Otherwise (no live streaming, e.g.
            # a deep/answer that arrived whole) print the result under the AI label.
            if not self._streamed_partial and text:
                self._ensure_ai_label()
                self._c.line(text)
        elif t == ev["decision"]:
            self._show_context_summary()
            self._pause_spinner()
            if self._c._rich:
                self._c._rich.print(f"  [yellow]?[/] {text}", highlight=False)
            elif self._c._color:
                self._c.line(f"  {_YELLOW}?{_RESET} {text}")
            else:
                self._c.line(f"  ? {text}")
        elif t == ev["done"]:
            pass  # result/decision already surfaced; done is a terminal signal.

    # -- close out the turn -------------------------------------------------
    def finish(self, *, cancelled: bool = False) -> None:
        if self._in_partial:
            self._c.line("")
            self._in_partial = False
        self._pause_spinner()
        if cancelled:
            self._c.dim("  cancelled.")


# ── ESC-to-cancel: a raw-stdin reader that flips a threading.Event ────────────


class _EscWatcher:
    """Watch stdin for an ESC keypress on a background thread, in raw mode.

    Used while a turn streams: pressing ESC sets the shared ``cancelled`` event,
    which the stream loop checks between events. Falls back to a no-op when stdin
    is not a real TTY (piped input, no termios), so non-interactive runs are safe.

    This is intentionally minimal: it only needs to notice ESC. We restore the
    terminal mode on stop.
    """

    def __init__(self, cancelled: threading.Event) -> None:
        self._cancelled = cancelled
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._enabled = False
        self._fd = None
        self._old = None
        try:
            import termios  # noqa: F401
            import tty  # noqa: F401
            self._enabled = sys.stdin.isatty()
        except Exception:  # noqa: BLE001  # pragma: no cover - non-posix / no tty
            self._enabled = False

    def __enter__(self) -> "_EscWatcher":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if not self._enabled:
            return
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        try:
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:  # noqa: BLE001  # pragma: no cover
            self._enabled = False
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        import select
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
            except Exception:  # noqa: BLE001  # pragma: no cover
                return
            if r:
                ch = sys.stdin.read(1)
                if ch == "\x1b":  # ESC
                    self._cancelled.set()
                    return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
            self._thread = None
        if self._enabled and self._fd is not None and self._old is not None:
            import termios
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:  # noqa: BLE001  # pragma: no cover
                pass


# ── Input prompt ──────────────────────────────────────────────────────────────


def _make_prompt_session():
    if not _HAS_PROMPT_TOOLKIT:
        return None
    kb = KeyBindings()

    @kb.add("c-c")
    def _ctrl_c(event):
        # Ctrl+C at the prompt clears the line; it does NOT exit the session.
        event.app.current_buffer.reset()

    return PromptSession(
        history=InMemoryHistory(),
        key_bindings=kb,
        enable_history_search=True,
    )


def _prompt_text_ansi() -> str:
    """The muted cyan input chevron, as an ANSI string."""
    return f"{_CYAN}  ❯ {_RESET}"


def _read_line(session, plain_prompt: str) -> Optional[str]:
    """Read one line of input. Returns None on EOF (Ctrl+D)."""
    try:
        if session is not None:
            return session.prompt(ANSI(_prompt_text_ansi()))
        return input(plain_prompt)
    except EOFError:
        return None


# ── Session ───────────────────────────────────────────────────────────────────

_HELP = """\
Commands:
  /help          show this help
  /clear         reset the conversation transcript
  /goal <id>     attach this session to a Quest goal id
  /quit  /q      exit
Keys:
  ESC            cancel the current turn while it is streaming
  Ctrl+C         clear the input line (stays in the session)
  Ctrl+D         exit
"""


class InteractiveSession:
    """Multi-turn interactive session over a RunnerConfig's orchestrator."""

    def __init__(self, cfg: "RunnerConfig", *, goal_id: Optional[str] = None):
        from .config import build_orchestrator
        self._orch: "Orchestrator" = build_orchestrator(cfg)
        self._goal_id = goal_id
        self._transcript: List[str] = []
        self._console = _Console()
        self._cancelled = threading.Event()
        self._turn_count = 0

    @property
    def _transcript_text(self) -> str:
        return "\n".join(self._transcript)

    # -- header -------------------------------------------------------------
    def _print_header(self) -> None:
        c = self._console
        if c._rich:
            c._rich.print("[bold]Quest AI[/]  [dim]interactive session[/]", highlight=False)
        elif c._color:
            c.line(f"{_BOLD}Quest AI{_RESET}  {_DIM}interactive session{_RESET}")
        else:
            c.line("Quest AI  interactive session")
        c.dim("  ESC cancel turn · Ctrl+C clear line · Ctrl+D exit · /help for commands")
        if self._goal_id:
            c.dim(f"  goal: {self._goal_id}")
        if not _HAS_PROMPT_TOOLKIT:
            c.dim("  tip: pip install quest-ai-runner[tui] for history + key handling")
        c.line("")

    # -- one turn -----------------------------------------------------------
    def _run_turn(self, user_text: str) -> None:
        """Stream one turn to the terminal. ESC mid-stream cancels it cleanly."""
        from .core.orchestrator import OrchestratorResult

        self._cancelled.clear()
        self._turn_count += 1

        # Echo the user's message back with a distinct speaker label.
        self._console.speaker("You", "green", user_text)
        self._console.line("")

        renderer = _TurnRenderer(self._console, _StatusLine(self._console))
        renderer.begin()
        final_text = ""

        try:
            with _EscWatcher(self._cancelled):
                for item in self._orch.run_stream(
                    user_text,
                    transcript=self._transcript_text,
                    quest_id=self._goal_id,
                ):
                    if self._cancelled.is_set():
                        break
                    if isinstance(item, OrchestratorResult):
                        final_text = item.text or ""
                    else:
                        renderer.render(item)
        except KeyboardInterrupt:
            self._cancelled.set()
        finally:
            renderer.finish(cancelled=self._cancelled.is_set())

        if not self._cancelled.is_set():
            # Persist the turn so follow-ups share context.
            self._transcript.append(f"User: {user_text}")
            self._transcript.append(f"Assistant: {final_text}")

        # A faint separator closes the turn so history is scannable.
        self._console.line("")
        self._console.rule()
        self._console.line("")

    # -- REPL ---------------------------------------------------------------
    def run(self) -> None:
        """Start the interactive REPL. Returns when the user quits."""
        self._print_header()
        session = _make_prompt_session()

        while True:
            try:
                if _HAS_PROMPT_TOOLKIT:
                    with patch_stdout():
                        line = _read_line(session, "  ❯ ")
                else:
                    line = _read_line(session, "  ❯ ")
            except KeyboardInterrupt:
                # Ctrl+C at the prompt (plain-input path): just start a fresh line.
                self._console.line("")
                continue

            if line is None:  # Ctrl+D / EOF
                break

            line = line.strip()
            if not line:
                continue

            # --- built-in commands ---
            if line in ("/quit", "/q", "quit", "exit"):
                break
            if line == "/help":
                self._console.line(_HELP)
                continue
            if line == "/clear":
                self._transcript.clear()
                self._console.dim("  transcript cleared")
                continue
            if line.startswith("/goal "):
                self._goal_id = line[6:].strip()
                self._console.dim(f"  goal set to {self._goal_id!r}")
                continue
            if line.startswith("/"):
                self._console.dim(f"  unknown command: {line!r}  (/help for the list)")
                continue

            # --- run a turn ---
            self._run_turn(line)

        self._console.dim("Bye.")


# ── Public entry point ────────────────────────────────────────────────────────


def start_interactive(cfg: "RunnerConfig", *, goal_id: Optional[str] = None) -> None:
    """Build an InteractiveSession from cfg and run it until the user quits."""
    InteractiveSession(cfg, goal_id=goal_id).run()
