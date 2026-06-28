"""DEPRECATED: Use the Textual UI (textual_ui.py) instead.

This ANSI-based interactive.py is kept for backward compatibility only.
The Textual UI provides a better experience with proper display handling,
cleaner context management, and no message context mixing bugs.

Install the [tui] extra and use 'quest-ai-runner chat' which auto-detects
the Textual environment. For ANSI-only fallback, the system will use this
module, but Textual is recommended.

────────────────────────────────────────────────────────────────────────

Interactive (attended) session: a multi-turn REPL over the orchestrator brain.

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

Example workflow (task execution):
    ❯ implement markdown rendering for responses
    (AI identifies this as code work, shows planned changes)

    (gathers relevant sources, deep_runner applies changes)
"""
from __future__ import annotations

import json
import queue as _queue
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

from .adapters.retry_utils import format_provider_error

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

# Emit deprecation warning when this module is imported
warnings.warn(
    "The ANSI interactive.py module is deprecated. Use the Textual UI instead "
    "(textual_ui.py) by installing the [tui] extra: pip install quest-ai-runner[tui]. "
    "This module is kept for ANSI-only fallback compatibility only.",
    DeprecationWarning,
    stacklevel=2
)

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import Orchestrator, OrchestratorResult, ProgressEvent


# ── Optional [tui] dependencies ───────────────────────────────────────────────

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

try:
    from rich.console import Console as _RichConsole
    from rich.markdown import Markdown as _RichMarkdown
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
_GOLD    = "\033[38;5;220m"  # 256-color gold for opus
_BRIGHT_CYAN = "\033[96m"

def _a(code: str, s: str) -> str:
    return f"{code}{s}{_RESET}"


def _highlight_ansi(text: str) -> str:
    """Minimal ANSI syntax highlight for markdown code blocks and emphasis (non-rich fallback)."""
    import re
    lines = text.split("\n")
    out: List[str] = []
    in_block = False
    for ln in lines:
        if ln.startswith("```"):
            in_block = not in_block
            # Show fence in dim; language label in cyan
            lang = ln[3:].strip()
            if in_block:
                out.append(_a(_DIM, "```") + (_a(_CYAN, lang) if lang else ""))
            else:
                out.append(_a(_DIM, "```"))
            continue
        if in_block:
            # Code block lines: dim yellow
            out.append(_a(_YELLOW, ln))
            continue
        # Inline code: `foo` → cyan
        ln = re.sub(r"`([^`]+)`", lambda m: _a(_CYAN, "`" + m.group(1) + "`"), ln)
        # Bold: **foo** → bold
        ln = re.sub(r"\*\*(.+?)\*\*", lambda m: _a(_BOLD, m.group(1)), ln)
        # Italic: *foo* → dim
        ln = re.sub(r"\*(.+?)\*", lambda m: _a(_DIM, m.group(1)), ln)
        out.append(ln)
    return "\n".join(out)


def _bullet(text: str, indent: int = 0, color: Optional[str] = None) -> str:
    """Format a bullet point with optional color and indentation."""
    prefix = "  " * indent + "● "
    if color:
        return prefix + color + text + _RESET
    return prefix + text

def _sub_bullet(text: str, indent: int = 1) -> str:
    """Format a sub-bullet (indented)."""
    return "  " * indent + "⎿  " + text

def _activity(duration_sec: float, status: str = "Running") -> str:
    """Format an activity indicator like Claude Code's 'Churned for 20s'."""
    mins, secs = divmod(int(duration_sec), 60)
    if mins > 0:
        time_str = f"{mins}m {secs}s"
    else:
        time_str = f"{secs}s"
    return _DIM + f"✻ {status} for {time_str}" + _RESET


def _parse_skill_frontmatter(skill_file: str) -> dict:
    """Parse SKILL.md YAML frontmatter and return metadata dict."""
    if not yaml:
        return {}
    try:
        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.startswith("---"):
            return {}
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


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

    def bullet(self, text: str, indent: int = 0, color: Optional[str] = None) -> None:
        """Print a bullet point with optional color."""
        if self._rich and color:
            self._rich.print(f"{'  ' * indent}[{color}]●[/] {text}", highlight=False)
        else:
            self.line(_bullet(text, indent, color))

    def sub_bullet(self, text: str, indent: int = 1) -> None:
        """Print a sub-bullet (tree-style indented)."""
        self.dim("  " * indent + "⎿  " + text)

    def markdown(self, text: str) -> None:
        """Render text as markdown with syntax-highlighted code blocks."""
        if self._rich:
            self._rich.print(_RichMarkdown(text, code_theme="monokai"), highlight=False)
        elif self._color:
            self.line(_highlight_ansi(text))
        else:
            self.line(text)

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
    background thread calls ``_spin`` at ~12 fps. Uses a content buffer
    to blend updates smoothly without jarring stop/start cycles.
    """

    def __init__(self, console: _Console) -> None:
        self._c = console
        self._tty = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frame = 0
        self._phase = "thinking…"
        self._sources: List[str] = []    # visible (most recent, deduplicated)
        self._seen_sources: set = set()  # all source paths ever added (for dedup)
        self._overflow = 0               # folded off the top
        self._total_sources = 0          # total sources seen (for footer)
        self._replans = 0
        self._last_line_count = 0
        self._cards: List[dict] = []     # full card objects with files instead of just names
        self._card_files_map: dict = {}  # card_id -> [files] mapping

    def start(self) -> None:
        if not self._tty:
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def set_phase(self, text: str) -> None:
        with self._lock:
            self._phase = text

    def set_cards(self, cards: List[dict]) -> None:
        """Set the context cards that were selected."""
        with self._lock:
            self._cards = cards
            self._card_files_map = {}
            for card in cards:
                card_id = card.get("id", "?")
                files = card.get("files", [])[:3]  # Show top 3 files per card
                if files:
                    self._card_files_map[card_id] = files

    def inc_replans(self) -> None:
        with self._lock:
            self._replans += 1
            # Clear displayed sources on replan (keep counts for footer)
            # This prevents old sources from cluttering the display
            self._sources = []
            self._overflow = 0
            self._cards = []
            self._card_files_map = {}

    def add_sources(self, paths: List[str], count: int) -> List[str]:
        """Called when a READ event arrives with its source paths. Returns only NEW paths."""
        new_paths: List[str] = []
        with self._lock:
            self._total_sources += count
            for p in paths:
                if p not in self._seen_sources:
                    self._seen_sources.add(p)
                    self._sources.append(p)
                    new_paths.append(p)
            # Keep only the most recent _MAX_SOURCES_SHOWN visible.
            if len(self._sources) > _MAX_SOURCES_SHOWN:
                dropped = len(self._sources) - _MAX_SOURCES_SHOWN
                self._overflow += dropped
                self._sources = self._sources[dropped:]
        if not self._tty:
            for p in new_paths:
                self._c.line(f"  ↗  {p}")
        return new_paths

    def stop(self) -> None:
        """Stop the spinner and erase the panel (final state)."""
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
            cards = list(self._cards)
            card_files_map = dict(self._card_files_map)

        lines: List[str] = [f"  {frame} {phase}"]

        # Show context cards with their files grouped underneath
        if cards:
            lines.append("")
            lines.append(f"  {_a(_BRIGHT_CYAN, '📇 Context Cards')}")
            for card in cards:
                card_id = card.get("id", "?")
                title = card.get("title", "(no title)")[:50]
                adapter = card.get("adapter", "")
                # Show card with adapter label and color differentiation
                adapter_label = f"[{adapter}]" if adapter else ""
                lines.append(f"  {_a(_BRIGHT_CYAN, '●')} {adapter_label} {_a(_DIM, card_id)}: {title}")

                # Show relevant files nested under this card
                files = card_files_map.get(card_id, card.get("files", [])[:3])
                if files:
                    for file_path in files[:3]:
                        file_label = file_path if len(file_path) <= 50 else "…" + file_path[-47:]
                        lines.append(f"    {_a(_BRIGHT_CYAN, '→')} {file_label}")
                    file_count = len(card.get("files", []))
                    if file_count > 3:
                        lines.append(f"    {_a(_DIM, f'+ {file_count - 3} more files')}")

        if sources:
            # Only show remaining ad-hoc sources (not in any card)
            lines.append("")
            lines.append(f"  {_a(_BRIGHT_CYAN, '⌕ Additional Sources')}")
            file_sources = [s for s in sources if not s.startswith("(")]
            search_sources = [s for s in sources if s.startswith("(searched")]

            if file_sources:
                for src in file_sources:
                    label = src if len(src) <= 50 else "…" + src[-47:]
                    lines.append(f"  {_a(_BRIGHT_CYAN, '↗')} {label}")
            if search_sources:
                for src in search_sources:
                    label = src if len(src) <= 50 else "…" + src[-47:]
                    lines.append(f"  {_a(_BRIGHT_CYAN, '◆')} {label}")

            if overflow:
                lines.append(f"     {_a(_DIM, f'and {overflow} more…')}")

        # Each render ends with \n after every line, so the cursor sits one line
        # BELOW the last spinner line. On the next render we move up by n lines
        # to land exactly on the first spinner line and overwrite from there.
        n = self._last_line_count
        if n:
            sys.stdout.write(f"\033[{n}A")
        for ln in lines:
            sys.stdout.write(f"\r\033[2K{ln}\n")
        sys.stdout.flush()
        self._last_line_count = len(lines)

    def _erase(self) -> None:
        n = self._last_line_count
        if not n:
            return
        # Cursor is one line below the last spinner line; move up n to reach the first.
        sys.stdout.write(f"\033[{n}A")
        for i in range(n):
            sys.stdout.write("\r\033[2K")
            if i < n - 1:
                sys.stdout.write("\n")
        # After the loop cursor is on the last cleared line; move back to the first.
        if n > 1:
            sys.stdout.write(f"\033[{n-1}A")
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
        import os
        import select
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
            except Exception:  # noqa: BLE001
                return
            if not r:
                continue
            # Read directly from the fd (bypassing Python's buffered text I/O) so we
            # get ALL bytes that arrived together in one shot.  Arrow keys and function
            # keys send multi-byte escape sequences (\x1b[C, \x1bOP, \x1b[15~, …).
            # Reading up to 16 bytes captures the full sequence in a single read(),
            # so we can distinguish "bare ESC" (exactly 1 byte) from "escape sequence"
            # (the \x1b plus at least one more byte) without a timing-based peek.
            try:
                data = os.read(self._fd, 16)
            except OSError:
                return
            if not data:
                continue
            if data[0:1] != b"\x1b":
                continue                        # ordinary key (a, enter, …) — ignore
            if len(data) > 1:
                continue                        # multi-byte sequence (arrow / fn key) — ignore
            # Exactly one byte (b"\x1b").  Could still be the first byte of a sequence
            # that arrived in two reads; peek for 50 ms to be sure.
            try:
                r2, _, _ = select.select([sys.stdin], [], [], 0.05)
            except Exception:  # noqa: BLE001
                self._cancelled.set(); return
            if r2:
                try:
                    os.read(self._fd, 16)       # drain the trailing sequence bytes
                except OSError:
                    pass
                continue                        # was an escape sequence — keep watching
            self._cancelled.set(); return       # bare ESC confirmed — cancel

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


class _DeepRunTracker:
    """Track multiple concurrent deep runs and their latest output.

    When multiple deep tasks execute concurrently, shows a dashboard with latest
    output from each, and allows toggling to view one run's full progress.
    """

    def __init__(self) -> None:
        self._runs: dict = {}  # run_id -> {'goal': str, 'status': str, 'output': str, 'started': float}
        self._lock = threading.Lock()
        self._active_run_id: Optional[str] = None  # which run is currently displayed in detail

    def add_run(self, run_id: str, goal: str) -> None:
        """Register a new deep run."""
        with self._lock:
            self._runs[run_id] = {
                'goal': goal,
                'status': 'running',
                'output': '',
                'started': time.time(),
                'exec_lines': [],  # accumulate exec events for this run
                'final_output': '',  # the worker's final result, set on completion
            }
            if self._active_run_id is None:
                self._active_run_id = run_id

    def set_final_output(self, run_id: str, text: str) -> None:
        """Record a deep run's final worker output (its result, not a progress tick)."""
        with self._lock:
            if run_id in self._runs and text:
                self._runs[run_id]['final_output'] = text

    def update_goal(self, run_id: str, goal: str) -> None:
        """Set/refine a run's goal text once the real subgoal is known."""
        with self._lock:
            if run_id in self._runs and goal:
                self._runs[run_id]['goal'] = goal

    def update_run_output(self, run_id: str, text: str) -> None:
        """Add output to a deep run's progress."""
        with self._lock:
            if run_id in self._runs:
                # Keep latest 10 lines of output for dashboard view
                current = self._runs[run_id]['output']
                lines = (current + '\n' + text).strip().split('\n')
                self._runs[run_id]['output'] = '\n'.join(lines[-10:])
                self._runs[run_id]['exec_lines'].append(text)

    def set_run_status(self, run_id: str, status: str) -> None:
        """Update a run's status (running/done/error)."""
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]['status'] = status

    def get_dashboard(self, lines_per_run: Optional[int] = None) -> str:
        """Return a dashboard summary of all runs with latest output.

        ``lines_per_run`` controls how many output lines are shown per agent.
        When omitted it scales automatically: 5 for one run, 3 for two, 2 for three+.
        """
        with self._lock:
            if not self._runs:
                return ""

            if lines_per_run is None:
                # Scale to keep the inline block calm but actually legible: a
                # single run gets a few lines (the common case the user reads),
                # concurrent runs tighten so the block doesn't balloon.
                n = len(self._runs)
                lines_per_run = 3 if n <= 1 else (2 if n == 2 else 1)

            lines = []
            for run_id, info in sorted(self._runs.items()):
                status_icon = "▶" if info['status'] == 'running' else ("✓" if info['status'] == 'done' else "✗")
                elapsed = time.time() - info['started']
                mins, secs = divmod(int(elapsed), 60)
                time_str = f"{mins}m{secs}s" if mins > 0 else f"{secs}s"

                # The SUBGOAL this run is working on: its own prominent (bold cyan) header line so
                # the user always sees WHAT the live actions below are for. Shown fully (generous cap
                # vs the old 60 chars that cut sentences mid-word); the renderer wraps if needed.
                goal = " ".join((info['goal'] or "").split())
                if len(goal) > 160:
                    goal = goal[:160].rstrip() + "…"
                lines.append(f"\x1b[1;36m⎅ {goal}\x1b[0m" if goal else "\x1b[1;36m⎅ deep task\x1b[0m")
                # Status + elapsed sit under the subgoal, then the latest live action lines.
                lines.append(f"\x1b[2m  {status_icon} {time_str}\x1b[0m")

                if info['output']:
                    output_lines = [l.strip() for l in info['output'].split('\n') if l.strip()]
                    for ol in output_lines[-lines_per_run:]:
                        prefix = "  → " if '/' in ol else "    "
                        lines.append(f"{prefix}{ol}")

            return "\n".join(lines)

    def set_active_run(self, run_id: str) -> bool:
        """Switch to viewing a specific run's detailed progress."""
        with self._lock:
            if run_id in self._runs:
                self._active_run_id = run_id
                return True
        return False

    def next_run(self) -> Optional[str]:
        """Cycle to the next run."""
        with self._lock:
            run_ids = sorted(self._runs.keys())
            if not run_ids:
                return None
            if self._active_run_id is None or self._active_run_id not in run_ids:
                self._active_run_id = run_ids[0]
                return self._active_run_id
            idx = run_ids.index(self._active_run_id)
            self._active_run_id = run_ids[(idx + 1) % len(run_ids)]
            return self._active_run_id

    def get_active_run(self) -> Optional[str]:
        """Get the currently active run ID."""
        with self._lock:
            return self._active_run_id

    def get_run_detail(self, run_id: Optional[str]) -> str:
        """Get full execution details for a run."""
        with self._lock:
            if run_id and run_id in self._runs:
                info = self._runs[run_id]
                return f"\n".join(info.get('exec_lines', [])[-50:])  # last 50 exec events
        return ""


class _TurnRenderer:
    """Routes ProgressEvents to the context panel and console for one turn."""

    def __init__(self, console: _Console, panel: _ContextPanel,
                 rep_name: str, deep_tracker: Optional[_DeepRunTracker] = None) -> None:
        self._c = console
        self._panel = panel
        self._rep_name = rep_name
        self._deep_tracker = deep_tracker or _DeepRunTracker()
        self._in_partial = False
        self._partial_started = False
        self._ai_label_printed = False
        self._ev = None
        self._current_deep_run_id: Optional[str] = None  # track which deep run we're in

    def _types(self):
        if self._ev is None:
            from .core.adapters import (
                EVENT_CONTEXT, EVENT_STATUS, EVENT_PLAN, EVENT_READ, EVENT_REPLAN,
                EVENT_PARTIAL, EVENT_EXEC, EVENT_RESULT, EVENT_DECISION,
                EVENT_MILESTONE, EVENT_DONE,
            )
            self._ev = dict(
                context=EVENT_CONTEXT, status=EVENT_STATUS, plan=EVENT_PLAN, read=EVENT_READ,
                replan=EVENT_REPLAN, partial=EVENT_PARTIAL, exec=EVENT_EXEC,
                result=EVENT_RESULT, decision=EVENT_DECISION,
                milestone=EVENT_MILESTONE, done=EVENT_DONE,
            )
        return self._ev

    def begin(self) -> None:
        self._panel.start()

    def _ensure_ai_label(self) -> None:
        if not self._ai_label_printed:
            # Print label on its own line with proper spacing (Claude Code style)
            if self._c._color or self._c._rich:
                self._c.line(f"{_BOLD}{_CYAN}{self._rep_name} (AI):{_RESET}")
            else:
                self._c.line(f"{self._rep_name} (AI):")
            self._ai_label_printed = True

    def _display(self, kind: str, text: str, prefix: str = "") -> None:
        """Unified display method for all user-facing output.

        Types:
          step       - dim action line (e.g. "▸ plan  reasoning")
          success    - green checkmark + plain text (e.g. "✓ Completed: ...")
          milestone  - green checkmark + markdown rendering (completion messages)
          markdown   - render markdown without prefix (AI response text)
          plain      - plain text line
          exec       - execution progress line (e.g. "→ Read: file.ts")
        """
        c = self._c
        if kind == "step":
            if c._rich:
                c._rich.print(f"  [dim]{prefix}  {text}[/]", highlight=False)
            elif c._color:
                c.line(f"  {_DIM}{prefix}  {text}{_RESET}")
            else:
                c.line(f"  {prefix}  {text}")
        elif kind == "success":
            if c._rich:
                c._rich.print(f"  [green]✓[/] {text}", highlight=False)
            elif c._color:
                c.line(f"  {_a(_GREEN, '✓')} {text}")
            else:
                c.line(f"  ✓ {text}")
        elif kind == "milestone":
            # Green checkmark + markdown rendering
            if c._rich:
                c._rich.print(f"  [green]✓[/]", highlight=False, end=" ")
            elif c._color:
                c.write(f"  {_a(_GREEN, '✓')} ")
            else:
                c.write(f"  ✓ ")
            c.markdown(text)
        elif kind == "markdown":
            # Markdown rendering without prefix
            c.markdown(text)
        elif kind == "exec":
            # Execution progress line (cyan arrow)
            if c._rich:
                c._rich.print(f"  [cyan]→[/] {text[:100]}", highlight=False, soft_wrap=True)
            elif c._color:
                c.line(f"  {_CYAN}→{_RESET} {text[:100]}")
            else:
                c.line(f"  → {text[:100]}")
        elif kind == "plain":
            c.line(text)

    def render(self, event) -> None:
        # run_stream() yields dicts (via ProgressEvent.to_dict()); support both.
        if isinstance(event, dict):
            t      = event.get("type", "")
            text   = (event.get("text") or "").rstrip()
            action = event.get("action") or ""
            data   = event.get("data") or {}
        else:
            t      = event.type
            text   = (event.text or "").rstrip()
            action = getattr(event, "action", None) or ""
            data   = event.data or {}
        ev = self._types()

        # All output types go through unified _display() method
        if t == ev["partial"]:
            # Narration beats (the instant ack + the planner's conversational rationale) come as
            # EVENT_PARTIAL tagged data={"narration": True} (legacy: "ack"). Show them as a dim
            # note above the spinner; never let them start the streamed-answer path.
            is_ack = isinstance(data, dict) and (data.get("narration") or data.get("ack"))
            if is_ack:
                # Instant ack: show as a dim note above the spinner, then restart it.
                # Do NOT set _partial_started/_in_partial — the real result still shows normally.
                self._panel.stop()
                if text:
                    self._c.dim(f"  {text}")
                self._panel.start()
                return
            # Regular streaming token path.
            if not self._partial_started:
                self._panel.stop()
                self._ensure_ai_label()
                self._c.line("")  # Blank line after label for breathing room
                self._partial_started = True
                self._in_partial = True
            self._c.write(text)
            return

        if self._in_partial:
            self._c.line(""); self._in_partial = False

        if t == ev["plan"]:
            if text:
                self._panel.stop()
                label = f"▸ {action}" if action else "▸ plan"
                self._display("step", text, label)
                self._panel.start()
            self._panel.set_phase("Planning…")
        elif t == ev["replan"]:
            self._panel.inc_replans()
            if text:
                self._panel.stop()
                self._display("step", text, "↺ replan")
                self._panel.start()
            self._panel.set_phase("Re-planning…")
        elif t == ev["context"]:
            # Display selected context cards + their sources + relevant files
            card_meta = data.get("card_metadata") or []
            sources = data.get("sources") or []
            if card_meta:
                # Update the spinner panel to show card names
                self._panel.set_cards(card_meta)
            if card_meta or sources:
                self._panel.stop()
                self._ensure_ai_label()
                # Show selected cards with their relevant files/sources grouped by card
                if card_meta:
                    c = self._c
                    if c._rich:
                        c._rich.print("[dim]Context cards selected:[/]", highlight=False)
                    else:
                        c.line(f"{_DIM}Context cards selected:{_RESET}")
                    for card in card_meta:
                        card_id = card.get("id", "?")
                        title = card.get("title", "(no title)")[:60]
                        score = card.get("relevance_score", 0)
                        adapter = card.get("adapter", "unknown")
                        file_count = card.get("file_count", 0)
                        card_files = card.get("files", [])
                        # Format: ● [adapter] card_id: title (score: 0.85, 3 files)
                        if c._rich:
                            score_str = f"score: {score:.2f}" if score else "score: unknown"
                            files_str = f"{file_count} file{'s' if file_count != 1 else ''}"
                            c._rich.print(
                                f"  [cyan]●[/] [{adapter}] {_a(_DIM, card_id)}: {title}",
                                highlight=False
                            )
                            c._rich.print(
                                f"    [dim]{score_str}, {files_str}[/]",
                                highlight=False
                            )
                            # Show relevant files from this card
                            if card_files:
                                for file_path in card_files[:5]:  # Show top 5 files per card
                                    c._rich.print(
                                        f"      [dim]→ {file_path}[/]",
                                        highlight=False
                                    )
                                if len(card_files) > 5:
                                    c._rich.print(
                                        f"      [dim]... and {len(card_files) - 5} more files[/]",
                                        highlight=False
                                    )
                        elif c._color:
                            score_str = f"score: {score:.2f}" if score else "score: unknown"
                            files_str = f"{file_count} file{'s' if file_count != 1 else ''}"
                            c.line(f"  {_CYAN}●{_RESET} [{adapter}] {_a(_DIM, card_id)}: {title}")
                            c.line(f"    {_DIM}{score_str}, {files_str}{_RESET}")
                            # Show relevant files from this card
                            if card_files:
                                for file_path in card_files[:5]:  # Show top 5 files per card
                                    c.line(f"      {_DIM}→ {file_path}{_RESET}")
                                if len(card_files) > 5:
                                    c.line(f"      {_DIM}... and {len(card_files) - 5} more files{_RESET}")
                        else:
                            score_str = f"score: {score:.2f}" if score else "score: unknown"
                            files_str = f"{file_count} file{'s' if file_count != 1 else ''}"
                            c.line(f"  • [{adapter}] {card_id}: {title}")
                            c.line(f"    {score_str}, {files_str}")
                            # Show relevant files from this card
                            if card_files:
                                for file_path in card_files[:5]:  # Show top 5 files per card
                                    c.line(f"      → {file_path}")
                                if len(card_files) > 5:
                                    c.line(f"      ... and {len(card_files) - 5} more files")
                # Show source attribution
                if sources:
                    c = self._c
                    if c._rich:
                        c._rich.print("[dim]Sources:[/]", highlight=False)
                    else:
                        c.line(f"{_DIM}Sources:{_RESET}")
                    for src in sources:
                        src_adapter = src.get("adapter", "?")
                        src_label = src.get("label", src_adapter)
                        items = src.get("items") or []
                        if items:
                            items_str = ", ".join(str(x).split("/")[-1] for x in items[:3])
                            extra = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
                            if c._rich:
                                c._rich.print(
                                    f"  [dim]• {src_label}: {items_str}{extra}[/]",
                                    highlight=False
                                )
                            elif c._color:
                                c.line(f"  {_DIM}• {src_label}: {items_str}{extra}{_RESET}")
                            else:
                                c.line(f"  • {src_label}: {items_str}{extra}")
                self._panel.start()
        elif t == ev["status"]:
            # Show user-friendly status messages
            status = text or "Thinking…"
            if status == "Thinking…":
                self._panel.set_phase("Thinking…")
            else:
                self._panel.set_phase(status)
        elif t == ev["read"]:
            paths = data.get("sources") or []
            count = data.get("reads", len(paths))
            new_paths = self._panel.add_sources(paths, count or len(paths))
            if new_paths:
                self._panel.stop()
                for p in new_paths:
                    # "(searched ...)" markers use a search glyph; real paths use ↗
                    prefix = "⌕" if p.startswith("(searched ") else "↗"
                    self._display("step", p, prefix)
                self._panel.start()
            total = self._panel._total_sources
            self._panel.set_phase(
                f"gathering context  "
                f"({total} source{'s' if total != 1 else ''} so far)"
            )
        elif t == ev["exec"]:
            # Track execution progress in deep runs
            run_id = data.get("run_id") or "default"
            event_count = data.get("event_number", 0)

            # If this is a new/first exec event for this run, register it
            if run_id != self._current_deep_run_id:
                raw_goal = (data.get("goal") or "").strip()
                goal = (raw_goal[:1].upper() + raw_goal[1:]) if raw_goal else "Executing work…"
                self._deep_tracker.add_run(run_id, goal)
                self._current_deep_run_id = run_id
                # Print goal header once per run
                self._panel.stop()
                self._c.line("")
                if self._c._color:
                    self._c.line(f"{_BOLD}{_BRIGHT_CYAN}{goal}{_RESET}")
                else:
                    self._c.line(f"{goal}")
                self._panel.start()

            # Update this run's output (accumulate without printing every line)
            if text:
                self._deep_tracker.update_run_output(run_id, text)

            # Show dashboard only occasionally (every 10 events) to avoid flicker
            if event_count % 10 == 0 and event_count > 0:
                self._panel.stop()
                dashboard = self._deep_tracker.get_dashboard()
                if dashboard:
                    for line in dashboard.split('\n'):
                        if line.strip():
                            self._c.dim("  " + line)
                self._panel.start()
        elif t == ev["milestone"]:
            self._panel.stop()
            if text:
                self._display("markup", text)
            self._panel.start()
        elif t == ev["result"]:
            if not self._partial_started and text:
                self._panel.stop()
                self._ensure_ai_label()
                self._c.line("")  # Blank line after label for breathing room
                self._display("markdown", text)
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
            self._c.dim("  Cancelled.")


# ── Input prompt ──────────────────────────────────────────────────────────────

_CTRL_C_WINDOW = 2.0   # seconds: second Ctrl+C within this window exits

_SLASH_COMMANDS = [
    "/help", "/clear", "/reps", "/rep ", "/file ",
    "/quests", "/goal ", "/whoami", "/status", "/tasks",
    "/quit", "/q",
    "/models", "/model", "/model ", "/depth", "/depth ",
    "/system", "/replan",
    "/save ", "/save", "/load ", "/sessions",
]
# /goal with a space triggers search; bare /goal (no arg) also works as a browse
# /models — interactive model tier selection menu
# /model [haiku|sonnet|opus|fable] — set or show current model tier (no arg → menu)
# /depth [light|standard|deep] — alias for /model (light=haiku, standard=sonnet, deep=opus)
# /system [text] — set or show a custom system-prompt prepended to the persona
# /replan — prime the next turn to force a fresh re-planning pass at opus tier
# /save [name] — save session to ~/.quest-ai-runner/sessions/<name>.json
# /load <name> — restore a saved session
# /sessions — list saved sessions


if _HAS_PROMPT_TOOLKIT:
    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.strip()
            if not text.startswith("/"):
                return
            # Show completions for slash commands
            for cmd in _SLASH_COMMANDS:
                if cmd.startswith(text):
                    # Return the remainder to complete, plus the full command for display
                    completion_text = cmd[len(text):]
                    yield Completion(completion_text, display=cmd, start_position=len(text) - len(document.text_before_cursor))
else:
    _SlashCompleter = None  # type: ignore


def _history_path() -> Optional[Path]:
    """Return the path for persistent input history, creating parent dirs as needed."""
    try:
        p = Path.home() / ".quest-ai-runner" / "history"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:  # noqa: BLE001
        return None


def _make_prompt_session(last_ctrl_c: list):
    """Build a PromptSession with Claude-Code-style Ctrl+C behaviour and file-backed history.

    ``last_ctrl_c`` is a one-element list holding the monotonic time of the
    most recent Ctrl+C (or 0.0). It is mutated by the key binding so the REPL
    loop can decide whether to exit on a ``KeyboardInterrupt``.

    First Ctrl+C: clears the input buffer, prints the hint, records the time.
    Second Ctrl+C within ``_CTRL_C_WINDOW`` seconds: raises ``KeyboardInterrupt``
    so prompt_toolkit surfaces it to the caller — the REPL loop then exits.

    Ctrl+R activates incremental fuzzy history search (prompt_toolkit built-in).
    """
    if not _HAS_PROMPT_TOOLKIT:
        return None
    from prompt_toolkit.styles import Style
    kb = KeyBindings()

    # Store deep run tracker for Tab key access
    deep_tracker = _DeepRunTracker()

    @kb.add("c-c")
    def _cc(event):
        now = time.monotonic()
        if now - last_ctrl_c[0] < _CTRL_C_WINDOW:
            # Second Ctrl+C: raise so the REPL loop's except catches it → exit.
            raise KeyboardInterrupt
        last_ctrl_c[0] = now
        event.app.current_buffer.reset()
        # Write the hint directly to the real stdout (patch_stdout corrupts \033 bytes).
        sys.__stdout__.write(f"\n{_DIM}  (press Ctrl+C again to exit){_RESET}\n")
        sys.__stdout__.flush()

    @kb.add("tab")
    def _tab(event):
        """Switch to next deep run when multiple are executing."""
        next_run = deep_tracker.next_run()
        if next_run:
            # Brief visual feedback
            event.app.current_buffer.reset()
            sys.__stdout__.write(f"\n{_DIM}  → Viewing: {next_run}{_RESET}\n")
            sys.__stdout__.flush()

    style = Style.from_dict({
        '': '#ffffff',
        'completion-menu.completion': 'bg:#1a1a2e #aaaaaa',
        'completion-menu.completion.current': 'bg:#4444aa #ffffff bold',
        # History search toolbar
        'bottom-toolbar': 'bg:#222244 #aaaacc',
        'bottom-toolbar.text': 'bg:#222244 #aaaacc',
        'reverse-i-search': 'bg:#222244 #aaaacc',
        'incsearch': 'bg:#222244 #aaaacc',
        'incsearch.current': 'bold underline #ffffff bg:#444488',
    })
    completer = _SlashCompleter()
    hp = _history_path()
    history = FileHistory(str(hp)) if hp is not None else InMemoryHistory()
    return PromptSession(history=history, key_bindings=kb,
                         style=style, completer=completer,
                         complete_while_typing=True,
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

  ● Session & Configuration
    /whoami              Show AI identity and session state
    /status              Show token usage and speed metrics
    /tasks               Show recently completed tasks
    /reps                List available AI representatives (skill files)
    /rep <name>          Set representative name directly
    /file <path>         Load a persona file

  ● Model & Behavior
    /models              Interactive model selection menu
    /model [tier]        Show or set model tier: haiku, sonnet, opus, fable
    /depth [level]       Alias for /model: light=haiku, standard=sonnet, deep=opus
    /system [text]       Show or set a custom system prompt prepended to persona
    /replan              Prime next turn for a fresh re-planning pass (uses opus)

  ● Sessions
    /save [name]         Save this session (transcript + config) to disk
    /load <name>         Restore a saved session
    /sessions            List saved sessions

  ● Conversation
    /clear               Reset the transcript
    /help                Show this help

  ● Goals & Quests
    /quests              Browse and attach to goals
    /goal <search|id>    Search goals or attach by ID

  ● Exit
    /quit, /q            Exit the session

Keys:

  ESC            Cancel current turn (while streaming)
  Ctrl+C         Clear input line (twice within 2s to exit)
  Ctrl+D         Exit
  Ctrl+R         Search input history (fuzzy, incremental)
"""

_BANNER = """\
{B}{C}Quest AI Runner{R}  Grounded AI that acts like a colleague

  What makes this different:
  ● finds just the right context efficiently; no "look at this file" needed
  ● routes to the right model automatically (haiku → sonnet → opus)
  ● optimal token usage; can run forever without context bloat
  ● named AI reps learn how to act like their associated human

  {D}ESC to cancel  ·  Ctrl+D to exit  ·  /help for all commands{R}
"""


class InteractiveSession:
    """Multi-turn interactive session over a RunnerConfig's orchestrator."""

    def __init__(self, cfg: "RunnerConfig", *, rep_name: str = "Assistant",
                 persona: Optional[str] = None, goal_id: Optional[str] = None) -> None:
        # Silence background-scanning INFO logs during interactive use.
        # The context panel already shows what's being gathered turn by turn;
        # raw log lines from background threads corrupt the prompt display because
        # Python's logging writes to stderr, which patch_stdout() does not intercept.
        import logging as _logging
        _bg_log = _logging.getLogger("quest-ai-runner.context")
        if _bg_log.level == _logging.NOTSET or _bg_log.level <= _logging.INFO:
            _bg_log.setLevel(_logging.WARNING)
        from .config import build_orchestrator
        # Collect bootstrap/index notices to emit as system messages after the header.
        self._startup_notices: List[str] = []
        # Create a console reference that will be available in the notify callback
        self._console = _Console()

        def notify_and_log(msg: str) -> None:
            """Show bootstrap/index messages to user and queue for header."""
            self._startup_notices.append(msg)
            # Show immediately to console if we have access (in interactive mode)
            self._console.dim(f"  {msg}")

        self._orch: "Orchestrator" = build_orchestrator(
            cfg, notify=notify_and_log
        )
        self._orch.cfg.instant_ack = True
        self._cfg = cfg
        self._rep_name = rep_name
        self._persona = persona
        self._goal_id = goal_id
        # Single-turn buffer for immediate transcript context.
        self._last_user: str = ""
        self._last_assistant: str = ""
        self._turn_count: int = 0
        # Turn history for /tasks and /status commands
        self._turns: List[dict] = []  # [{user, model, tokens_in, tokens_out, elapsed, timestamp}]
        # TurnContextStore is wired automatically by resolve_context_assembler in config.py,
        # at <corpus_root>/.quest-context/turns/ — same root as file cards.
        self._cancelled = threading.Event()
        # Feature: model selection (/model, /models)
        self._model_hint: Optional[str] = None
        # Feature: system-prompt customization (/system)
        self._system: Optional[str] = None
        # Feature: replan priming (/replan) — one-shot flag consumed on the next turn
        self._replan_next: bool = False
        # Feature: session save/load (/save, /load, /sessions)
        self._sessions_dir: Path = Path.home() / ".quest-ai-runner" / "sessions"
        # Track persona source file path for persistence (/personas, /reps, /file)
        self._persona_file: Optional[str] = None
        # Feature: multi-deep-run tracking (dashboard view)
        self._deep_tracker = _DeepRunTracker()
        # Restore persisted model/persona from qar_state.json (best-effort)
        self._load_session_state()
        # Resolve display name from skill frontmatter (display_name > name > rep_name as given)
        self._refresh_rep_name_from_skill()
        # If no skill file loaded yet, try auto-discovering one by rep name
        if not self._persona_file:
            self._try_load_skill_by_name(rep_name)
        # Build dynamic model tier menu from the registry
        self._build_model_tiers_menu()

    # -- header ----------------------------------------------------------------

    def _print_header(self) -> None:
        c = self._console
        # Banner already ends with \n; use write() to avoid a double blank line.
        c.write(_BANNER.format(B=_BOLD, C=_CYAN, R=_RESET, D=_DIM))
        parts = [f"AI: {self._rep_name}"]
        corpus = getattr(self._cfg, "corpus_root", None)
        if corpus:
            corpus_short = corpus.split("/")[-1] if "/" in corpus else corpus
            parts.append(f"corpus: {corpus_short}")
        if self._goal_id:
            parts.append(f"goal: {self._goal_id}")
        c.dim("  " + "  •  ".join(parts))
        for notice in self._startup_notices:
            c.dim(f"  {notice}")
        c.line("")

    # -- one turn --------------------------------------------------------------

    def _last_transcript(self) -> str:
        """Return a single-turn transcript of the immediately preceding exchange."""
        if not self._last_user:
            return ""
        asst = self._last_assistant
        if len(asst) > 400:
            asst = asst[:400].rstrip() + "…"
        return f"User: {self._last_user}\nAssistant: {asst}"

    def _effective_preamble(self) -> Optional[str]:
        """Combine the system prompt and persona into the rep_preamble passed to the orchestrator."""
        parts = []
        if self._system:
            parts.append(self._system)
        if self._persona:
            parts.append(self._persona)
        return "\n\n".join(parts) if parts else None

    def _run_turn(self, user_text: str) -> None:
        from .core.orchestrator import OrchestratorResult

        self._cancelled.clear()
        # Do NOT echo user_text — prompt_toolkit already shows it at the ❯ prompt.

        # Consume the replan flag: force opus for this turn, then reset.
        model_hint = self._model_hint
        if self._replan_next:
            model_hint = "opus"
            self._replan_next = False
            self._console.dim("  Replan mode: using opus for this turn.")

        panel = _ContextPanel(self._console)
        renderer = _TurnRenderer(self._console, panel, self._rep_name, self._deep_tracker)
        renderer.begin()

        t0 = time.monotonic()
        final: Optional[OrchestratorResult] = None

        # Feed run_stream() into a queue so the main thread can poll with a short
        # timeout and respond to ESC within ~50 ms (instead of blocking on q.get()
        # until the next event arrives, which may be many seconds away during an API call).
        _iq: "_queue.Queue" = _queue.Queue()
        _DONE_ITEM = object()

        def _feed() -> None:
            try:
                # run_stream() streams all orchestrator events: planning, context gathering (READ),
                # execution (EXEC, MILESTONE), and final results. Context retrieval happens here
                # via the RetrievalAdapter (if configured), regardless of whether deep_runner is
                # set. Events are displayed in real-time: spinner shows sources as they're gathered,
                # steps show actions taken. Same pipeline for regular turns and /execute deep runs.
                for it in self._orch.run_stream(
                    user_text,
                    transcript=self._last_transcript(),
                    quest_id=self._goal_id,
                    rep_preamble=self._effective_preamble(),
                    model_hint=model_hint,
                ):
                    _iq.put(it)
            except Exception as e:  # noqa: BLE001
                _iq.put(e)
            finally:
                _iq.put(_DONE_ITEM)

        feed = threading.Thread(target=_feed, daemon=True)
        feed.start()

        try:
            with _EscWatcher(self._cancelled):
                while True:
                    try:
                        item = _iq.get(timeout=0.05)
                    except _queue.Empty:
                        if self._cancelled.is_set():
                            break
                        continue
                    if item is _DONE_ITEM:
                        break
                    if isinstance(item, Exception):
                        friendly = format_provider_error(item)
                        self._console.line(f"\n  Error: {friendly}\n")
                        return
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

        if self._cancelled.is_set() and user_text:
            # Cancelled turn: still remember what the user asked so the next turn
            # ("try again", follow-up, etc.) has the prior question as context.
            self._last_user = user_text
            self._last_assistant = "[cancelled by user]"
            # Also persist to TurnContextStore so it survives across more turns.
            _ctx = getattr(self._orch, "context_assembler", None)
            if _ctx is not None:
                try:
                    _ctx.record(user_text, {"kind": "cancelled",
                                            "response": "[turn was cancelled by user]"})
                except Exception:  # noqa: BLE001
                    pass

        elif not self._cancelled.is_set() and final is not None:
            # Record turn for /tasks and /status commands
            tok_in = getattr(final, "tokens_in", 0) or 0
            tok_out = getattr(final, "tokens_out", 0) or 0
            model = _model_label(getattr(final, "model", None))
            # Truncate user question for history display
            user_summary = user_text[:60] + "…" if len(user_text) > 60 else user_text
            self._turns.append({
                "user": user_summary,
                "model": model,
                "tokens_in": tok_in,
                "tokens_out": tok_out,
                "elapsed": elapsed,
                "timestamp": time.time(),
            })
            # Deep result handling: execution may have run or only been planned.
            if final.kind == "deep":
                goals = final.goals or []
                # A deep turn that actually executed has at least one DeepResult that either met
                # its goal or produced output (its text/milestones were already streamed live).
                # ``OrchestratorResult`` never sets ``.text`` for kind="deep", so checking ``.text``
                # alone always looked unexecuted and wrongly showed the "use /execute" hint (and
                # crashed on a renderer-only helper). Key off the deep results instead.
                executed = any(
                    getattr(d, "met", False) or (getattr(d, "output", "") or "").strip()
                    for d in (final.deep_results or [])
                )
                if not executed and goals:
                    # Nothing ran (no deep_runner wired, or it produced nothing)
                    panel.stop()
                    renderer._ensure_ai_label()
                    self._console.line("")
                    self._console.dim("  Task identified. Planned changes:")
                    for i, g in enumerate(goals, 1):
                        prefix = "▸ " if i == 1 else "  "
                        self._console.dim(f"  {prefix}{g}")
                    self._console.line("")

                    # If deep_runner is configured, execute automatically (don't ask)
                    # Otherwise skip (no executor available)
                    if self._cfg.deep_runner:
                        self._run_turn("Execute it. No more planning, just code it and apply changes now.")
                    else:
                        self._console.dim("  (No deep executor configured; cannot auto-execute)")

            self._last_user = user_text
            # For deep runs, signal completion clearly — goal strings look like unfinished TODOs
            if final.kind == "deep":
                deep_results = final.deep_results or []
                goals = final.goals or []
                all_met = bool(deep_results) and all(d.met for d in deep_results)
                goal_str = ("; ".join(goals))[:300] if goals else ""
                prefix = "Completed" if all_met else "Attempted"
                self._last_assistant = (f"{prefix}: {goal_str}" if goal_str else f"{prefix}.")
            else:
                self._last_assistant = final.text or ""
            self._turn_count += 1
            self._console.line("")
            self._print_turn_footer(final, panel, elapsed)

        self._console.line("")
        self._console.rule()
        self._console.line("")

    def _print_turn_footer(self, result: "OrchestratorResult",
                           panel: _ContextPanel, elapsed: float) -> None:
        """Structured footer: steps · sources · model (color-coded) · tokens · duration."""
        src_count, replan_count = panel.summary()
        c = self._console

        # Collect metrics for display
        metrics: List[str] = []
        steps = getattr(result, "steps", 0)
        if steps:
            metrics.append(f"{steps} step{'s' if steps != 1 else ''}")
        if src_count:
            metrics.append(f"{src_count} source{'s' if src_count != 1 else ''}")
        if replan_count:
            metrics.append(f"{replan_count} replan{'s' if replan_count != 1 else ''}")

        # Model tier with color
        model_lbl = _model_label(getattr(result, "model", None))
        model_colored = ""
        if model_lbl:
            if "haiku" in model_lbl:
                model_colored = _a(_CYAN, model_lbl)
            elif "sonnet" in model_lbl:
                model_colored = _a(_GREEN, model_lbl)
            elif "opus" in model_lbl:
                model_colored = _a(_GOLD, model_lbl)
            elif "fable" in model_lbl:
                model_colored = _a(_MAGENTA, model_lbl)
            else:
                model_colored = model_lbl
            metrics.append(model_colored)

        # Token usage with better formatting
        tok_in = getattr(result, "tokens_in", 0) or 0
        tok_out = getattr(result, "tokens_out", 0) or 0
        if tok_in or tok_out:
            def _k(n):
                return f"{n/1000:.1f}k" if n >= 1000 else str(n)
            metrics.append(f"↥ {_k(tok_in)} in · ↦ {_k(tok_out)} out")

        metrics.append(f"{elapsed:.1f}s")

        # Print as a clean, indented metrics line
        c.dim("  " + "  ·  ".join(metrics))

    # -- REPL ------------------------------------------------------------------

    def run(self) -> None:
        self._print_header()
        last_ctrl_c: list = [0.0]   # shared with the prompt_toolkit key binding
        session = _make_prompt_session(last_ctrl_c)

        while True:
            try:
                if _HAS_PROMPT_TOOLKIT:
                    with patch_stdout():
                        line = _read_line(session, "  ❯ ")
                else:
                    line = _read_line(session, "  ❯ ")
            except KeyboardInterrupt:
                # prompt_toolkit raises this on the second Ctrl+C (via the binding).
                # Plain input() raises it on every Ctrl+C — apply the same two-strike
                # logic here for the fallback path.
                now = time.monotonic()
                if not _HAS_PROMPT_TOOLKIT:
                    if now - last_ctrl_c[0] < _CTRL_C_WINDOW:
                        break   # second strike → exit
                    last_ctrl_c[0] = now
                    self._console.line("")
                    self._console.dim("  (Press Ctrl+C again to exit)")
                    continue
                break   # prompt_toolkit already applied the two-strike logic → exit

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
                self._last_user = ""
                self._last_assistant = ""
                self._console.dim("  Transcript cleared."); continue
            if line.startswith("/rep "):
                self._rep_name = line[5:].strip()
                self._console.dim(f"  Representative: {self._rep_name}")
                self._persist_session_state()
                continue
            if line.startswith("/file "):
                path = line[6:].strip()
                try:
                    self._persona = open(path).read()  # noqa: WPS515
                    self._persona_file = path
                    kb = max(1, len(self._persona.encode()) // 1024)
                    self._console.dim(f"  Loaded: {path} ({kb}KB)")
                    self._persist_session_state()
                except OSError as e:
                    self._console.dim(f"  Could not read {path!r}: {e}")
                continue
            if line.startswith("/goal"):
                self._cmd_goal(line[5:].strip(), session); continue
            if line == "/whoami":
                self._print_whoami(); continue
            if line == "/status":
                self._print_status(); continue
            if line == "/tasks":
                self._print_tasks(); continue
            if line == "/quests":
                self._cmd_quests(session); continue
            if line == "/reps":
                self._cmd_reps(session); continue
            if line in ("/models", "/model"):
                self._cmd_models_menu(session); continue
            if line.startswith("/model"):
                self._cmd_model(line[6:].strip()); continue
            if line.startswith("/depth"):
                self._cmd_depth(line[6:].strip()); continue
            if line.startswith("/system"):
                self._cmd_system(line[7:].strip()); continue
            if line == "/replan":
                self._replan_next = True
                self._console.dim("  Next turn will use opus for a fresh re-planning pass."); continue
            if line.startswith("/save"):
                self._cmd_save(line[5:].strip()); continue
            if line.startswith("/load "):
                self._cmd_load(line[6:].strip()); continue
            if line == "/sessions":
                self._cmd_sessions(); continue
            if line.startswith("/"):
                self._console.dim(f"  Unknown command: {line!r}  (/help for list)"); continue

            self._run_turn(line)

        self._console.dim("Goodbye.")

    # -- New command handlers --------------------------------------------------

    _DEPTH_ALIASES = {
        "light": "fast", "quick": "fast",
        "standard": "balanced", "normal": "balanced", "default": "balanced",
        "deep": "quality", "thorough": "quality", "hard": "quality",
    }
    _VALID_TIERS = {"fast", "balanced", "quality", "best", "auto"}

    def _cmd_model(self, arg: str) -> None:
        """Set the model tier directly (no arg resets to auto)."""
        c = self._console
        if not arg:
            current = self._model_hint or "auto (orchestrator decides)"
            c.dim(f"  Model: {current}")
            c.dim("  Usage: /model fast | balanced | quality | best  — or /models for a menu")
            return
        tier = arg.lower().strip()
        if tier in ("auto", "reset", "clear"):
            self._model_hint = None
            c.dim("  Model set to auto (orchestrator decides).")
            self._persist_session_state()
            return
        if tier not in self._VALID_TIERS:
            c.dim(f"  Unknown tier {tier!r}. Choose: fast, balanced, quality, best")
            return
        self._model_hint = tier
        c.dim(f"  Model set to {tier}.")
        self._persist_session_state()

    def _cmd_depth(self, arg: str) -> None:
        """Alias for /model using friendlier level names."""
        c = self._console
        if not arg:
            current = self._model_hint or "auto"
            c.dim(f"  Depth/model: {current}")
            c.dim("  Usage: /depth light | standard | deep  —  or /models for the full menu")
            return
        level = arg.lower().strip()
        tier = self._DEPTH_ALIASES.get(level, level)
        if tier not in self._VALID_TIERS:
            c.dim(f"  Unknown depth {level!r}. Choose: light (fast), standard (balanced), deep (quality)")
            return
        self._model_hint = tier
        c.dim(f"  Depth set to {level} (model: {tier}).")

    def _cmd_system(self, arg: str) -> None:
        """Show or set a custom system prompt prepended to the persona."""
        c = self._console
        if not arg:
            if self._system:
                preview = self._system[:120] + "…" if len(self._system) > 120 else self._system
                c.dim(f"  System prompt ({len(self._system)} chars): {preview}")
                c.dim("  Use '/system clear' to remove, or '/system <text>' to replace.")
            else:
                c.dim("  No custom system prompt set.")
                c.dim("  Usage: /system <text>  —  sets a custom prompt prepended to the persona.")
            return
        if arg.lower() == "clear":
            self._system = None
            c.dim("  System prompt cleared.")
            return
        self._system = arg
        kb = max(1, len(arg.encode()) // 1024)
        c.dim(f"  System prompt set ({kb}KB).")

    # -- Interactive model/persona menus --------------------------------------

    def _build_model_tiers_menu(self) -> None:
        """Build dynamic model tier menu from provider's actual models."""
        from .core.model_registry import ModelRegistry
        registry = self._orch.registry
        models = registry.top_models()
        # Build menu with semantic tiers and actual model names
        self._model_tiers = [
            ("auto",      "orchestrator decides (recommended)"),
            ("fast",      f"light-weight tasks — {models['fast']}"),
            ("balanced",  f"general chat and coding — {models['balanced']}"),
            ("quality",   f"thorough, deep reasoning — {models['quality']}"),
            ("best",      f"best available (if needed) — {models['best']}"),
        ]

    def _cmd_models_menu(self, session) -> None:
        """Interactive numbered model tier selection menu."""
        c = self._console
        current = self._model_hint or "auto"
        c.line("")
        c.dim("  Available models:")
        c.line("")
        for i, (tier, desc) in enumerate(self._model_tiers, 1):
            marker = "●" if tier == current else " "
            if tier == "fast":
                tier_colored = _a(_CYAN, tier)
            elif tier == "balanced":
                tier_colored = _a(_GREEN, tier)
            elif tier == "quality":
                tier_colored = _a(_GOLD, tier)
            elif tier == "best":
                tier_colored = _a(_MAGENTA, tier)
            else:
                tier_colored = _a(_DIM, tier)
            pad = " " * max(0, 10 - len(tier))
            c.dim(f"  {i}.  {marker} {tier_colored}{pad}  {desc}")
        c.dim(f"  0.  Cancel (keep: {current})")
        c.line("")
        try:
            if session is not None:
                raw = session.prompt(ANSI(f"{_CYAN}  select › {_RESET}"))
            else:
                raw = input("  select › ")
        except (EOFError, KeyboardInterrupt):
            c.dim("  Cancelled."); return
        try:
            n = int((raw or "").strip())
        except ValueError:
            c.dim("  Cancelled."); return
        if n == 0 or n > len(self._model_tiers):
            c.dim("  Cancelled."); return
        tier_name, _ = self._model_tiers[n - 1]
        if tier_name == "auto":
            self._model_hint = None
            c.dim("  Model set to auto (orchestrator decides).")
        else:
            self._model_hint = tier_name
            c.dim(f"  Model set to {tier_name}.")
        self._persist_session_state()

    # -- qar_state.json session state persistence -----------------------------

    def _state_path(self) -> Path:
        """Path to qar_state.json — same file the poller uses, different key."""
        import os
        return Path(os.getenv("QAR_STATE_PATH", "./qar_state.json"))

    def _persist_session_state(self) -> None:
        """Write model_hint/rep_name/persona_file into qar_state.json chat_state (best-effort)."""
        try:
            path = self._state_path()
            state: dict = {}
            if path.exists():
                try:
                    state = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    state = {}
            state["chat_state"] = {
                "model_hint": self._model_hint,
                "rep_name": self._rep_name,
                "persona_file": self._persona_file,
            }
            path.write_text(json.dumps(state, indent=2))
        except Exception:  # noqa: BLE001
            pass  # persistence is best-effort; never block the session

    def _load_session_state(self) -> None:
        """Restore model_hint/rep_name/persona from qar_state.json chat_state (best-effort)."""
        try:
            path = self._state_path()
            if not path.exists():
                return
            state = json.loads(path.read_text())
            cs = state.get("chat_state")
            if not isinstance(cs, dict):
                return
            if cs.get("model_hint"):
                self._model_hint = cs["model_hint"]
            if cs.get("rep_name"):
                self._rep_name = cs["rep_name"]
            pf = cs.get("persona_file")
            if pf:
                try:
                    self._persona = open(pf).read()  # noqa: WPS515
                    self._persona_file = pf
                    self._refresh_rep_name_from_skill()
                except OSError:
                    pass  # file moved or deleted — skip silently
        except Exception:  # noqa: BLE001
            pass  # best-effort; never block startup

    # -- Session save/load ----------------------------------------------------

    def _sessions_path(self) -> Path:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        return self._sessions_dir

    def _cmd_save(self, arg: str) -> None:
        """Save the current session to disk."""
        c = self._console
        name = arg.strip() or "default"
        # Sanitize: only alphanumeric, dash, underscore, dot
        import re as _re
        name = _re.sub(r"[^A-Za-z0-9._-]", "_", name) or "default"
        path = self._sessions_path() / f"{name}.json"
        payload = {
            "rep_name": self._rep_name,
            "persona": self._persona,
            "goal_id": self._goal_id,
            "system": self._system,
            "model_hint": self._model_hint,
            "last_user": self._last_user,
            "last_assistant": self._last_assistant,
            "turn_count": self._turn_count,
            "turns": self._turns,
        }
        try:
            path.write_text(json.dumps(payload, indent=2, default=str))
            c.dim(f"  Session saved: {path}")
        except OSError as e:
            c.dim(f"  Could not save session: {e}")

    def _cmd_load(self, arg: str) -> None:
        """Load a saved session from disk."""
        c = self._console
        name = arg.strip()
        if not name:
            c.dim("  Usage: /load <name>   (see /sessions for saved sessions)")
            return
        import re as _re
        name = _re.sub(r"[^A-Za-z0-9._-]", "_", name)
        path = self._sessions_path() / f"{name}.json"
        if not path.exists():
            c.dim(f"  Session not found: {name!r}  (use /sessions to list)")
            return
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            c.dim(f"  Could not load session: {e}")
            return
        self._rep_name = payload.get("rep_name") or self._rep_name
        self._persona = payload.get("persona")
        self._goal_id = payload.get("goal_id")
        self._system = payload.get("system")
        self._model_hint = payload.get("model_hint")
        self._last_user = payload.get("last_user") or ""
        self._last_assistant = payload.get("last_assistant") or ""
        self._turn_count = payload.get("turn_count") or 0
        self._turns = payload.get("turns") or []
        c.dim(f"  Session loaded: {name!r}  ({self._turn_count} prior turns)")
        c.dim(f"  Rep: {self._rep_name}" + (f"  Model: {self._model_hint}" if self._model_hint else ""))

    def _cmd_sessions(self) -> None:
        """List saved sessions."""
        c = self._console
        try:
            paths = sorted(self._sessions_path().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        if not paths:
            c.dim("  No saved sessions.  Use /save <name> to save one.")
            return
        c.line("")
        for p in paths:
            try:
                sz = p.stat().st_size
                data = json.loads(p.read_text())
                rep = data.get("rep_name") or "?"
                turns = data.get("turn_count") or 0
                goal = data.get("goal_id") or ""
                suffix = f"  goal: {goal}" if goal else ""
                c.dim(f"  {p.stem:20s}  {rep:12s}  {turns} turns  ({sz//1024+1}KB){suffix}")
            except Exception:  # noqa: BLE001
                c.dim(f"  {p.stem}")
        c.dim("")
        c.dim("  /load <name>  to restore a session")

    # -- Quest client (lazy, only when credentials are configured) -------------

    def _quest_client(self):
        """Return a QuestClient if Quest credentials are configured, else None."""
        try:
            from .runner.quest_client import QuestClient
            url = getattr(self._cfg, "quest_base_url", "") or ""
            key = getattr(self._cfg, "quest_api_key", "") or ""
            if not url or not key:
                return None
            return QuestClient(url, key,
                               team_id=getattr(self._cfg, "team_id", None) or None)
        except Exception:  # noqa: BLE001
            return None

    def _pick_from_list(self, items: list, label_fn, session) -> Optional[int]:
        """Print a numbered list and return the user's 0-based choice, or None for cancel."""
        c = self._console
        c.line("")
        for i, item in enumerate(items, 1):
            c.dim(f"  {i}.  {label_fn(item)}")
        c.dim("  0.  Cancel")
        c.line("")
        try:
            if session is not None:
                raw = session.prompt(ANSI(f"{_CYAN}  select › {_RESET}"))
            else:
                raw = input("  select › ")
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            n = int((raw or "").strip())
        except ValueError:
            return None
        if n <= 0 or n > len(items):
            return None
        return n - 1

    def _cmd_goal(self, arg: str, session) -> None:
        """Attach to a goal. With no arg or a name: open the quest→goal picker.
        With a bare id (no spaces, short): set it directly without an API call."""
        c = self._console
        if not arg:
            self._cmd_quests(session); return
        # Bare id — attach directly, no network call needed.
        if " " not in arg and len(arg) < 80:
            self._goal_id = arg
            c.dim(f"  Goal set to {arg!r} — use /quests to browse by name."); return
        # Name search — open picker then filter within the chosen quest.
        self._cmd_quests(session)

    def _cmd_quests(self, session) -> None:
        """Show all goals across all team quests organized by time period, then let the user pick one."""
        c = self._console
        client = self._quest_client()
        if client is None:
            c.dim("  Quest credentials not configured. Set QUEST_BASE_URL, QUEST_API_KEY, QUEST_TEAM_ID.")
            return

        c.dim("  Fetching quests and goals…")
        try:
            quests = client.list_quests()
        except Exception as e:  # noqa: BLE001
            c.dim(f"  Could not fetch quests: {e}"); return
        if not quests:
            c.dim("  No quests attached to this team (QUEST_TEAM_ID=%s)." % getattr(self._cfg, "team_id", "?"))
            c.dim("  Quests must be attached to the team before they appear here."); return

        # Fetch goals for each quest and merge into time-period buckets
        # bucket key: (time_scope, period) → {period_label, time_scope, period, goals: [...]}
        SCOPE_ORDER = ["year", "quarter", "month", "week", "day", "custom", "quest", ""]
        def _scope_rank(s):
            try:
                return SCOPE_ORDER.index(str(s or ""))
            except ValueError:
                return len(SCOPE_ORDER)

        buckets: dict = {}
        goals_errors = []
        for quest in quests:
            quest_id = quest.get("quest_id") or ""
            quest_outcome = quest.get("outcome") or quest_id or "untitled"
            if not quest_id:
                continue
            try:
                data = client.list_quest_goals(quest_id)
            except Exception as e:  # noqa: BLE001
                goals_errors.append(f"    {quest_outcome}: {e}")
                continue
            for group in (data.get("period_groups") or []):
                scope = group.get("time_scope") or "custom"
                period = group.get("period") or ""
                period_label = group.get("period_label") or period or scope
                key = (scope, period)
                if key not in buckets:
                    buckets[key] = {"time_scope": scope, "period": period,
                                    "period_label": period_label, "goals": []}
                for g in (group.get("goals") or []):
                    g["_quest_outcome"] = quest_outcome
                    buckets[key]["goals"].append(g)

        if goals_errors:
            c.dim("  Could not fetch goals for some quests:")
            for err in goals_errors:
                c.dim(err)
        if not buckets:
            c.dim("  No goals found across %d quest(s)." % len(quests)); return

        sorted_groups = sorted(buckets.values(),
                               key=lambda p: (_scope_rank(p["time_scope"]), p.get("period") or ""))

        # Build display: period headers (non-selectable) + numbered goal rows
        flat_goals = []
        display_rows = []  # (num_or_none, label)
        entry_num = 0
        for group in sorted_groups:
            goals_in_group = group.get("goals") or []
            if not goals_in_group:
                continue
            display_rows.append((None, f"── {group['period_label']} ──"))
            for g in goals_in_group:
                entry_num += 1
                name = g.get("name") or g.get("title") or g.get("id") or "untitled"
                quest_ctx = g.get("_quest_outcome") or ""
                done = "  ✓" if g.get("completed") else ""
                suffix = f"  ({quest_ctx}){done}" if quest_ctx else done
                flat_goals.append(g)
                display_rows.append((entry_num, f"{name}{suffix}"))

        if not flat_goals:
            c.dim("  No goals found."); return

        c.line("")
        for num, label in display_rows:
            if num is None:
                c.dim(f"       {label}")
            else:
                c.dim(f"  {num:2d}.  {label}")
        c.dim("   0.  cancel")
        c.line("")

        try:
            if session is not None:
                raw = session.prompt(ANSI(f"{_CYAN}  select › {_RESET}"))
            else:
                raw = input("  select › ")
        except (EOFError, KeyboardInterrupt):
            c.dim("  Cancelled."); return

        try:
            n = int((raw or "").strip())
        except ValueError:
            c.dim("  Cancelled."); return
        if n <= 0 or n > len(flat_goals):
            c.dim("  Cancelled."); return

        g = flat_goals[n - 1]
        self._goal_id = g.get("id") or g.get("goal_id") or ""
        title = g.get("name") or g.get("title") or self._goal_id
        c.dim(f"  Attached to: {title}")

    def _cmd_reps(self, session) -> None:
        """List locally-synced AI reps (SKILL.md files) and let the user select one."""
        import os
        c = self._console
        skills_dir = self._skills_dir()
        if not os.path.isdir(skills_dir):
            c.dim(f"  No SKILL.md files found in {skills_dir}")
            c.dim("  Create .claude/skills/<name>/SKILL.md, or use /rep <name> and /file <path> directly.")
            return
        reps = []
        for entry in sorted(os.scandir(skills_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            skill_file = os.path.join(entry.path, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue
            meta = _parse_skill_frontmatter(skill_file)
            display_name = meta.get("display_name") or entry.name
            reps.append({"name": entry.name, "display_name": display_name, "skill_file": skill_file})
        if not reps:
            c.dim(f"  No SKILL.md files found under {skills_dir}.")
            return
        def _label(r):
            return r["display_name"]
        idx = self._pick_from_list(reps, _label, session)
        if idx is None:
            c.dim("  Cancelled."); return
        r = reps[idx]
        self._rep_name = r["display_name"]
        try:
            self._persona = open(r["skill_file"]).read()  # noqa: WPS515
            self._persona_file = r["skill_file"]
            kb = max(1, len(self._persona.encode()) // 1024)
            c.dim(f"  Representative: {self._rep_name}  (skill file loaded: {r['skill_file']}, {kb}KB)")
            self._persist_session_state()
        except OSError as e:
            c.dim(f"  Could not read {r['skill_file']!r}: {e}")

    def _skills_dir(self) -> Optional[str]:
        """Resolve the local skills directory: QAR_SKILLS_DIR > corpus_root/.claude/skills > cwd/.claude/skills."""
        import os
        # Explicit env var takes priority
        explicit = os.getenv("QAR_SKILLS_DIR")
        if explicit:
            return explicit
        # Next, try corpus_root if configured
        corpus = getattr(self._cfg, "corpus_root", None)
        if corpus:
            return os.path.join(corpus, ".claude", "skills")
        # Finally, default to current working directory
        cwd_skills = os.path.join(os.getcwd(), ".claude", "skills")
        if os.path.isdir(cwd_skills):
            return cwd_skills
        # If none of the above exist, still return cwd path (for /reps to show the hint)
        return cwd_skills

    def _refresh_rep_name_from_skill(self) -> None:
        """Update _rep_name from the loaded skill file's display_name/name field."""
        if not self._persona_file:
            return
        meta = _parse_skill_frontmatter(self._persona_file)
        name = meta.get("display_name") or meta.get("name")
        if name:
            self._rep_name = name

    def _try_load_skill_by_name(self, name: str) -> None:
        """Auto-load SKILL.md for a rep name if discoverable, and update display name."""
        if not name or name in ("AI", "Assistant"):
            return
        skills_dir = self._skills_dir()
        if not skills_dir:
            return
        skill_file = os.path.join(skills_dir, name, "SKILL.md")
        if not os.path.isfile(skill_file):
            return
        try:
            self._persona = open(skill_file).read()  # noqa: WPS515
            self._persona_file = skill_file
            self._refresh_rep_name_from_skill()
        except OSError:
            pass

    def _print_whoami(self) -> None:
        c = self._console
        c.line("")
        c.speaker(self._rep_name, "cyan", "Session info")
        c.line("")
        if self._persona:
            kb = max(1, len(self._persona.encode()) // 1024)
            c.bullet(f"representative: {self._rep_name} ({kb}KB skill file)", indent=1)
        else:
            c.bullet(f"representative: {self._rep_name} (use /reps to load a skill file)", indent=1)
        corpus = getattr(self._cfg, "corpus_root", None)
        if corpus:
            c.bullet(f"corpus: {corpus}", indent=1)
        if self._goal_id:
            c.bullet(f"goal: {self._goal_id}", indent=1)
        c.bullet(f"turns: {self._turn_count} in this session", indent=1)
        c.line("")

    def _print_tasks(self) -> None:
        """Show recently completed tasks with metadata."""
        c = self._console
        if not self._turns:
            c.line("")
            c.dim("  No turns yet.")
            c.line("")
            return

        c.line("")
        c.speaker("Tasks", "cyan", f"{len(self._turns)} completed")
        c.line("")

        # Show up to last 10 turns
        for i, turn in enumerate(self._turns[-10:], 1):
            user_text = turn["user"]
            model = turn["model"] or "?"
            tokens = f"{turn['tokens_in']}↥ {turn['tokens_out']}↦"
            elapsed = f"{turn['elapsed']:.1f}s"

            # Color the model tier
            model_colored = model
            if "haiku" in model.lower():
                model_colored = _a(_CYAN, model)
            elif "sonnet" in model.lower():
                model_colored = _a(_GREEN, model)
            elif "opus" in model.lower():
                model_colored = _a(_GOLD, model)

            c.dim(f"  {i:2d}.  {user_text[:45]}...")
            c.dim(f"       {model_colored}  {tokens}  {elapsed}")

        c.line("")

    def _print_status(self) -> None:
        """Show session statistics: token usage, model distribution, speed metrics."""
        c = self._console
        c.line("")
        c.speaker("Session stats", "cyan", "")
        c.line("")

        if not self._turns:
            c.bullet("turns: 0", indent=1)
            c.bullet("total tokens: 0", indent=1)
            c.line("")
            return

        # Aggregate stats
        total_in = sum(t["tokens_in"] for t in self._turns)
        total_out = sum(t["tokens_out"] for t in self._turns)
        avg_in = total_in // len(self._turns) if self._turns else 0
        avg_out = total_out // len(self._turns) if self._turns else 0
        total_time = sum(t["elapsed"] for t in self._turns)
        avg_time = total_time / len(self._turns) if self._turns else 0

        # Model distribution
        model_counts: dict = {}
        for t in self._turns:
            model = t["model"] or "unknown"
            model_counts[model] = model_counts.get(model, 0) + 1

        # Speed metrics
        times = [t["elapsed"] for t in self._turns]
        fastest = min(times) if times else 0
        slowest = max(times) if times else 0

        c.bullet(f"turns: {len(self._turns)}", indent=1)
        c.bullet(f"total tokens: {total_in + total_out:,} ({total_in:,} in, {total_out:,} out)",
                indent=1)
        c.bullet(f"avg tokens per turn: {avg_in} in, {avg_out} out", indent=1)
        c.line("")

        c.dim("  Models used:")
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            pct = (count / len(self._turns) * 100) if self._turns else 0
            c.dim(f"    {model:8s}  {count:2d} turn{'s' if count != 1 else ''}  ({pct:5.1f}%)")

        c.line("")
        c.dim(f"  Speed:  fastest {fastest:.1f}s  ·  slowest {slowest:.1f}s  ·  avg {avg_time:.1f}s")
        c.line("")


# ── Public entry point ────────────────────────────────────────────────────────

def start_interactive(cfg: "RunnerConfig", *, rep_name: str = "AI",
                      persona: Optional[str] = None,
                      goal_id: Optional[str] = None) -> None:
    """Build an InteractiveSession from cfg and run it until the user quits."""
    InteractiveSession(cfg, rep_name=rep_name, persona=persona, goal_id=goal_id).run()
