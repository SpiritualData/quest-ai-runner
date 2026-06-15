"""Interactive (attended) session: a multi-turn REPL over the orchestrator brain.

quest-ai-runner solves a specific set of problems with long-running AI sessions:

* **Context bloat**: instead of loading every file and past message into the window,
  the brain retrieves only what the current request needs — from your corpus, from
  the transcript, from the rep's learned knowledge — so each turn is fast and cheap
  regardless of how long the conversation has been running.

* **Model routing**: the brain auto-selects the cheapest model that can handle each
  step (haiku for planning/searching, sonnet for answering, opus for deep/review),
  so you never need to think about which model to use.

* **Rep identity**: the session runs *as* a specific AI representative — a named
  persona with a skill file and learned corrections — so the AI already knows who
  it is and what it knows without you having to explain anything at the start.

* **Persistent memory without token cost**: conversation context is managed through
  retrieval, not accumulation. The AI acts like a human colleague who can pick up
  where you left off without needing the full prior conversation pasted in.

The terminal shows all of this working: you can watch the context panel accumulate
real sources from your corpus before the answer, see which model tier ran, and read
the per-turn summary (steps · sources · model · elapsed) — the same format Claude
Code uses for (tool uses · tokens · time).

Install the [tui] extra for the full experience:
    pip install quest-ai-runner[tui]

Usage:
    quest-ai-runner chat
    quest-ai-runner chat --rep "Joshua's AI" --persona-file path/to/skill.md
    quest-ai-runner chat --goal-id <quest-goal-id>
"""
from __future__ import annotations

import sys
import threading
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import Orchestrator, OrchestratorResult, ProgressEvent

# ── Optional [tui] dependencies ───────────────────────────────────────────────

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

try:
    from rich.console import Console as _RichConsole
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

# ── ANSI helpers ──────────────────────────────────────────────────────────────

_RESET   = "\033[0m"
_BOLD    = "\033[1m"
_DIM     = "\033[2m"
_CYAN    = "\033[36m"
_GREEN   = "\033[32m"
_YELLOW  = "\033[33m"
_MAGENTA = "\033[35m"
_BLUE    = "\033[34m"

def _a(code: str, s: str) -> str:
    return f"{code}{s}{_RESET}"


# ── Console wrapper ───────────────────────────────────────────────────────────

class _Console:
    """Thin render surface — rich when available, ANSI otherwise, plain on non-TTY."""

    def __init__(self) -> None:
        self._rich = _RichConsole(highlight=False, soft_wrap=True) if _HAS_RICH else None
        self._color = sys.stdout.isatty()

    def write(self, s: str) -> None:
        sys.stdout.write(s); sys.stdout.flush()

    def line(self, s: str = "") -> None:
        sys.stdout.write(s + "\n"); sys.stdout.flush()

    def dim(self, s: str) -> None:
        if self._rich:    self._rich.print(s, style="dim", highlight=False)
        elif self._color: self.line(_a(_DIM, s))
        else:             self.line(s)

    def speaker(self, label: str, color: str, text: str) -> None:
        _ansi = {"cyan": _CYAN, "green": _GREEN, "yellow": _YELLOW,
                 "magenta": _MAGENTA, "blue": _BLUE}
        if self._rich:
            self._rich.print(f"[bold {color}]{label}[/]  {text}", highlight=False)
        elif self._color:
            self.line(f"{_BOLD}{_ansi.get(color, '')}{label}{_RESET}  {text}")
        else:
            self.line(f"{label}  {text}")

    def rule(self) -> None:
        w = self._width()
        s = "─" * w
        if self._rich:    self._rich.print(s, style="dim", highlight=False)
        elif self._color: self.line(_a(_DIM, s))
        else:             self.line(s)

    def _width(self) -> int:
        try:
            import shutil
            return max(8, min(shutil.get_terminal_size((80, 24)).columns, 100))
        except Exception:  # noqa: BLE001
            return 72


# ── Context panel ─────────────────────────────────────────────────────────────
#
# The gather phase is the heart of the value proposition: the AI is consulting
# your corpus in real time before answering. We make that *visible*: an animated
# spinner above an accumulating list of source paths that grow with each READ
# event. Sources appear as they arrive; older ones fold into "… and N more"
# once the list exceeds the cap. The whole panel is erased when the answer starts.

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_MAX_SOURCES_SHOWN = 5


class _ContextPanel:
    """Animated, bounded in-place display for the gather phase.

    Thread-safe: main thread calls ``add_sources`` / ``set_phase``;
    background thread calls ``_spin`` at ~12 fps.
    """

    def __init__(self, console: _Console) -> None:
        self._c = console
        self._tty = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frame = 0
        self._phase = "thinking…"
        self._sources: List[str] = []    # visible (most recent)
        self._overflow = 0               # folded off the top
        self._total_sources = 0          # total sources seen (for footer)
        self._replans = 0
        self._last_line_count = 0

    def start(self) -> None:
        if not self._tty:
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def set_phase(self, text: str) -> None:
        with self._lock:
            self._phase = text

    def inc_replans(self) -> None:
        with self._lock:
            self._replans += 1

    def add_sources(self, paths: List[str], count: int) -> None:
        """Called when a READ event arrives with its source paths."""
        with self._lock:
            self._total_sources += count
            for p in paths:
                self._sources.append(p)
            # Keep only the most recent _MAX_SOURCES_SHOWN visible.
            if len(self._sources) > _MAX_SOURCES_SHOWN:
                dropped = len(self._sources) - _MAX_SOURCES_SHOWN
                self._overflow += dropped
                self._sources = self._sources[dropped:]
        if not self._tty:
            for p in paths:
                self._c.line(f"  ↗  {p}")

    def stop(self) -> None:
        self._stop_ev.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._tty:
            self._erase()

    def summary(self) -> Tuple[int, int]:
        """(total_sources, replans) for the turn footer."""
        with self._lock:
            return self._total_sources, self._replans

    # -- rendering internals --------------------------------------------------

    def _spin(self) -> None:
        while not self._stop_ev.is_set():
            self._render()
            time.sleep(0.08)

    def _render(self) -> None:
        with self._lock:
            frame = _SPINNER[self._frame % len(_SPINNER)]
            self._frame += 1
            phase = self._phase
            sources = list(self._sources)
            overflow = self._overflow

        lines: List[str] = [f"  {frame} {phase}"]
        if sources:
            lines.append("")
            for src in sources:
                label = src if len(src) <= 62 else "…" + src[-59:]
                lines.append(f"  ↗  {label}")
            if overflow:
                lines.append(f"     … and {overflow} more")

        # Erase previous render then draw new lines.
        n = self._last_line_count
        if n:
            sys.stdout.write(f"\033[{n}A")
        for i, ln in enumerate(lines):
            sys.stdout.write(f"\r\033[2K{_DIM}{ln}{_RESET}")
            if i < len(lines) - 1:
                sys.stdout.write("\n")
        sys.stdout.flush()
        self._last_line_count = len(lines)

    def _erase(self) -> None:
        n = self._last_line_count
        if not n:
            return
        sys.stdout.write(f"\033[{n}A")
        for i in range(n):
            sys.stdout.write("\r\033[2K")
            if i < n - 1:
                sys.stdout.write("\n")
        sys.stdout.write("\r")
        sys.stdout.flush()
        self._last_line_count = 0


# ── ESC watcher ───────────────────────────────────────────────────────────────

class _EscWatcher:
    """Raw-stdin ESC detector running on a background thread."""

    def __init__(self, cancelled: threading.Event) -> None:
        self._cancelled = cancelled
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._enabled = False
        self._fd = self._old = None
        try:
            import termios, tty  # noqa: F401, E401
            self._enabled = sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "_EscWatcher":
        self.start(); return self

    def __exit__(self, *_) -> None:
        self.stop()

    def start(self) -> None:
        if not self._enabled:
            return
        import termios, tty  # noqa: E401
        self._fd = sys.stdin.fileno()
        try:
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:  # noqa: BLE001
            self._enabled = False; return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        import select
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
            except Exception:  # noqa: BLE001
                return
            if r and sys.stdin.read(1) == "\x1b":
                self._cancelled.set(); return

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        if self._enabled and self._fd is not None and self._old is not None:
            import termios
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:  # noqa: BLE001
                pass


# ── Turn renderer ─────────────────────────────────────────────────────────────

def _model_label(model_id: Optional[str]) -> str:
    if not model_id:
        return ""
    m = model_id.lower()
    for tier in ("haiku", "sonnet", "opus", "fable"):
        if tier in m:
            return tier
    return model_id.split("-")[0]


class _TurnRenderer:
    """Routes ProgressEvents to the context panel and console for one turn."""

    def __init__(self, console: _Console, panel: _ContextPanel,
                 rep_name: str) -> None:
        self._c = console
        self._panel = panel
        self._rep_name = rep_name
        self._in_partial = False
        self._partial_started = False
        self._ai_label_printed = False
        self._ev = None

    def _types(self):
        if self._ev is None:
            from .core.adapters import (
                EVENT_STATUS, EVENT_PLAN, EVENT_READ, EVENT_REPLAN,
                EVENT_PARTIAL, EVENT_EXEC, EVENT_RESULT, EVENT_DECISION,
                EVENT_MILESTONE, EVENT_DONE,
            )
            self._ev = dict(
                status=EVENT_STATUS, plan=EVENT_PLAN, read=EVENT_READ,
                replan=EVENT_REPLAN, partial=EVENT_PARTIAL, exec=EVENT_EXEC,
                result=EVENT_RESULT, decision=EVENT_DECISION,
                milestone=EVENT_MILESTONE, done=EVENT_DONE,
            )
        return self._ev

    def begin(self) -> None:
        self._panel.start()

    def _ensure_ai_label(self) -> None:
        if not self._ai_label_printed:
            self._c.speaker(self._rep_name, "cyan", "")
            self._ai_label_printed = True

    def render(self, event: "ProgressEvent") -> None:
        ev = self._types()
        t = event.type
        text = (event.text or "").rstrip()

        if t == ev["partial"]:
            if not self._partial_started:
                self._panel.stop()
                self._ensure_ai_label()
                self._partial_started = True
                self._in_partial = True
            self._c.write(text)
            return

        if self._in_partial:
            self._c.line(""); self._in_partial = False

        if t == ev["plan"]:
            self._panel.set_phase("planning…")
        elif t == ev["replan"]:
            self._panel.inc_replans()
            self._panel.set_phase("replanning with context…")
        elif t == ev["status"]:
            self._panel.set_phase(text or "thinking…")
        elif t == ev["read"]:
            data = event.data or {}
            paths = data.get("sources") or []
            count = data.get("reads", len(paths))
            self._panel.add_sources(paths, count or len(paths))
            total = self._panel._total_sources
            self._panel.set_phase(
                f"gathering context  "
                f"({total} source{'s' if total != 1 else ''} so far)"
            )
        elif t == ev["exec"]:
            self._panel.set_phase(text or "running…")
        elif t == ev["milestone"]:
            self._panel.stop()
            c = self._c
            if c._rich:    c._rich.print(f"  [green]✓[/] {text}", highlight=False)
            elif c._color: c.line(f"  {_a(_GREEN, '✓')} {text}")
            else:          c.line(f"  ✓ {text}")
            self._panel.start()
        elif t == ev["result"]:
            if not self._partial_started and text:
                self._panel.stop()
                self._ensure_ai_label()
                self._c.line(text)
        elif t == ev["decision"]:
            self._panel.stop()
            c = self._c
            if c._rich:    c._rich.print(f"\n  [yellow]?[/] {text}", highlight=False)
            elif c._color: c.line(f"\n  {_a(_YELLOW, '?')} {text}")
            else:          c.line(f"\n  ? {text}")
        # done: terminal signal only; result/decision already surfaced

    def finish(self, *, cancelled: bool = False) -> None:
        if self._in_partial:
            self._c.line(""); self._in_partial = False
        self._panel.stop()
        if cancelled:
            self._c.dim("  cancelled.")


# ── Input prompt ──────────────────────────────────────────────────────────────

def _make_prompt_session():
    if not _HAS_PROMPT_TOOLKIT:
        return None
    kb = KeyBindings()

    @kb.add("c-c")
    def _cc(event):
        event.app.current_buffer.reset()

    return PromptSession(history=InMemoryHistory(), key_bindings=kb,
                         enable_history_search=True)


def _read_line(session, prompt_str: str) -> Optional[str]:
    try:
        if session is not None:
            return session.prompt(ANSI(f"{_CYAN}  ❯ {_RESET}"))
        return input(prompt_str)
    except EOFError:
        return None


# ── Session ───────────────────────────────────────────────────────────────────

_HELP = """\
Commands:
  /help              show this help
  /clear             reset the conversation transcript
  /rep <name>        change the AI representative for this session
  /persona <file>    load a persona/skill file  (overrides prior persona)
  /goal <id>         attach session to a Quest goal id
  /whoami            show what this AI knows about itself and this session
  /quit  /q          exit

Keys:
  ESC        cancel the current turn while it is streaming
  Ctrl+C     clear the input line (stays in the session)
  Ctrl+D     exit
"""

_BANNER = """\
{B}{C}quest-ai-runner{R}  grounded AI that acts like a colleague

  What makes this different from a plain chat window:
  · reads your corpus for every request — no "look at this file" needed
  · routes to the right model automatically  (haiku → sonnet → opus)
  · each turn retrieves context, never accumulates — conversation runs forever
  · runs AS a named AI rep with its own persona and learned knowledge

  {D}ESC cancel turn  ·  Ctrl+D exit  ·  /help for commands{R}
"""


class InteractiveSession:
    """Multi-turn interactive session over a RunnerConfig's orchestrator."""

    def __init__(self, cfg: "RunnerConfig", *, rep_name: str = "AI",
                 persona: Optional[str] = None, goal_id: Optional[str] = None) -> None:
        from .config import build_orchestrator
        self._orch: "Orchestrator" = build_orchestrator(cfg)
        self._cfg = cfg
        self._rep_name = rep_name
        self._persona = persona
        self._goal_id = goal_id
        self._transcript: List[str] = []
        self._console = _Console()
        self._cancelled = threading.Event()

    @property
    def _transcript_text(self) -> str:
        return "\n".join(self._transcript)

    # -- header ----------------------------------------------------------------

    def _print_header(self) -> None:
        c = self._console
        banner = _BANNER.format(B=_BOLD, C=_CYAN, R=_RESET, D=_DIM)
        c.line(banner)
        # Session identity line.
        rep_line = f"  rep: {self._rep_name}"
        if self._persona:
            kb = max(1, len(self._persona.encode()) // 1024)
            rep_line += f"  ·  persona: {kb}KB loaded"
        corpus = getattr(self._cfg, "corpus_root", None)
        if corpus:
            rep_line += f"  ·  corpus: {corpus}"
        if self._goal_id:
            rep_line += f"  ·  goal: {self._goal_id}"
        c.dim(rep_line)
        c.line("")
        if not _HAS_PROMPT_TOOLKIT:
            c.dim("  tip: pip install quest-ai-runner[tui] for history + key handling\n")

    # -- one turn --------------------------------------------------------------

    def _run_turn(self, user_text: str) -> None:
        from .core.orchestrator import OrchestratorResult

        self._cancelled.clear()
        self._console.speaker("You", "green", user_text)
        self._console.line("")

        panel = _ContextPanel(self._console)
        renderer = _TurnRenderer(self._console, panel, self._rep_name)
        renderer.begin()

        t0 = time.monotonic()
        final: Optional[OrchestratorResult] = None

        try:
            with _EscWatcher(self._cancelled):
                for item in self._orch.run_stream(
                    user_text,
                    transcript=self._transcript_text,
                    quest_id=self._goal_id,
                    rep_preamble=self._persona,
                ):
                    if self._cancelled.is_set():
                        break
                    if isinstance(item, OrchestratorResult):
                        final = item
                    else:
                        renderer.render(item)
        except KeyboardInterrupt:
            self._cancelled.set()
        finally:
            renderer.finish(cancelled=self._cancelled.is_set())

        elapsed = time.monotonic() - t0

        if not self._cancelled.is_set() and final is not None:
            self._transcript.append(f"User: {user_text}")
            self._transcript.append(f"Assistant: {final.text or ''}")
            self._console.line("")
            self._print_turn_footer(final, panel, elapsed)

        self._console.line("")
        self._console.rule()
        self._console.line("")

    def _print_turn_footer(self, result: "OrchestratorResult",
                           panel: _ContextPanel, elapsed: float) -> None:
        """Dim one-liner: N steps · M sources · [replans] · model · Xs"""
        src_count, replan_count = panel.summary()
        parts: List[str] = []
        steps = getattr(result, "steps", 0)
        if steps:
            parts.append(f"{steps} step{'s' if steps != 1 else ''}")
        if src_count:
            parts.append(f"{src_count} source{'s' if src_count != 1 else ''}")
        if replan_count:
            parts.append(f"{replan_count} replan{'s' if replan_count != 1 else ''}")
        model_lbl = _model_label(getattr(result, "model", None))
        if model_lbl:
            parts.append(model_lbl)
        parts.append(f"{elapsed:.1f}s")
        self._console.dim("  " + "  ·  ".join(parts))

    # -- REPL ------------------------------------------------------------------

    def run(self) -> None:
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
                self._console.line(""); continue

            if line is None:
                break

            line = line.strip()
            if not line:
                continue

            if line in ("/quit", "/q", "quit", "exit"):
                break
            if line == "/help":
                self._console.line(_HELP); continue
            if line == "/clear":
                self._transcript.clear()
                self._console.dim("  transcript cleared"); continue
            if line.startswith("/rep "):
                self._rep_name = line[5:].strip()
                self._console.dim(f"  rep: {self._rep_name!r}"); continue
            if line.startswith("/persona "):
                path = line[9:].strip()
                try:
                    self._persona = open(path).read()  # noqa: WPS515
                    kb = max(1, len(self._persona.encode()) // 1024)
                    self._console.dim(f"  persona loaded: {path} ({kb}KB)")
                except OSError as e:
                    self._console.dim(f"  could not read {path!r}: {e}")
                continue
            if line.startswith("/goal "):
                self._goal_id = line[6:].strip()
                self._console.dim(f"  goal: {self._goal_id!r}"); continue
            if line == "/whoami":
                self._print_whoami(); continue
            if line.startswith("/"):
                self._console.dim(f"  unknown: {line!r}  (/help for list)"); continue

            self._run_turn(line)

        self._console.dim("Bye.")

    def _print_whoami(self) -> None:
        c = self._console
        c.line("")
        c.speaker(self._rep_name, "cyan", "")
        corpus = getattr(self._cfg, "corpus_root", None)
        if corpus:
            c.dim(f"  corpus:    {corpus}")
        if self._persona:
            kb = max(1, len(self._persona.encode()) // 1024)
            c.dim(f"  persona:   {kb}KB loaded")
        else:
            c.dim("  persona:   none  "
                  "(use /persona <file> or --persona-file to load one)")
        if self._goal_id:
            c.dim(f"  goal:      {self._goal_id}")
        turns = sum(1 for t in self._transcript if t.startswith("User:"))
        c.dim(f"  turns:     {turns} in this session")
        c.line("")


# ── Public entry point ────────────────────────────────────────────────────────

def start_interactive(cfg: "RunnerConfig", *, rep_name: str = "AI",
                      persona: Optional[str] = None,
                      goal_id: Optional[str] = None) -> None:
    """Build an InteractiveSession from cfg and run it until the user quits."""
    InteractiveSession(cfg, rep_name=rep_name, persona=persona, goal_id=goal_id).run()
