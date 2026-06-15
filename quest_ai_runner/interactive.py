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


# ── Animated status line: a spinner + collapsing chatter on ONE updating line ─

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _StatusLine:
    """A single in-place terminal line that animates a spinner with chatter text.

    The spinner cycles on a background thread (~12fps). The main thread updates the
    accompanying text (the latest plan/read/status tick) via :meth:`set`. The whole
    thing lives on ONE line that is rewritten in place, so chatter never scrolls —
    it stays subtle. :meth:`stop` clears the line so real output can take over.

    No-op (prints nothing, spins nothing) when stdout is not a TTY, so piped/captured
    runs stay clean.
    """

    def __init__(self, console: _Console) -> None:
        self._console = console
        self._tty = sys.stdout.isatty()
        self._text = "thinking"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frame = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not self._tty:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def set(self, text: str) -> None:
        """Update the chatter text shown next to the spinner."""
        with self._lock:
            self._text = text or self._text

    def stop(self) -> None:
        """Stop the spinner and erase the status line."""
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

    def _render(self) -> None:
        with self._lock:
            frame = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
            self._frame += 1
            text = self._text
        # Dim spinner glyph + dim chatter, rewritten in place. Erase the whole line
        # first (\r + clear-to-EOL) so a shorter line never leaves stale characters.
        body = f"  {frame} {text}"
        sys.stdout.write(f"\r\033[2K{_DIM}{body}{_RESET}")
        sys.stdout.flush()

    def _erase(self) -> None:
        if not self._tty:
            return
        # Clear the whole line and return the cursor to column 0.
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()


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


class _TurnRenderer:
    """Renders one turn's event stream.

    Responsibilities, in order of visual prominence:
      * CHATTER (plan/read/replan/status/exec) → collapsed onto the spinner's
        single updating line; never scrolls.
      * MILESTONE → a printed green line (a real checkpoint worth keeping).
      * PARTIAL → typed out in place under the ``AI`` label, no per-chunk newline.
      * RESULT → printed in full under the ``AI`` label (when no partials streamed).
      * DECISION → a printed amber question.

    The spinner is paused around any real printed output so the two never fight for
    the line.
    """

    def __init__(self, console: _Console, status: _StatusLine) -> None:
        self._c = console
        self._status = status
        self._in_partial = False      # currently streaming partial chunks
        self._streamed_partial = False  # at least one partial chunk arrived
        self._ai_label_shown = False  # printed the "AI" speaker label yet
        self._spinning = False

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

    # -- the AI speaker label, lazily printed once content starts -----------
    def _ensure_ai_label(self) -> None:
        if not self._ai_label_shown:
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

        if t in (ev["plan"], ev["replan"]):
            self._status.set(text or "planning")
            self._resume_spinner()
        elif t == ev["status"]:
            self._status.set(text or "working")
            self._resume_spinner()
        elif t == ev["read"]:
            self._status.set(f"reading {event.action or text}")
            self._resume_spinner()
        elif t == ev["exec"]:
            self._status.set(text or "running")
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
