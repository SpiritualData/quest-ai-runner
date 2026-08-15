"""The interactive (attended) session: state and logic behind the chat UI.

This module holds :class:`InteractiveSession` — the "brain wiring" for an attended
multi-turn session: it builds the orchestrator, resolves the rep persona, restores
and persists chat state, runs the non-rendering slash commands (``/model``,
``/system``, ``/save``, ``/status``, …), and owns the conversation history that
future sessions recall.

It renders nothing. The one supported chat UI is Textual (``textual_ui.py``,
launched by ``textual_session.start_textual_interactive``); it drives this object
and does all display work itself. The former ANSI/``prompt_toolkit`` renderer that
also lived here was removed in favour of maintaining a single chat UI.

The small ANSI helpers that remain (``_Console`` and friends) are the shared output
surface the Textual UI subclasses (``_RichLogConsole``) so these command handlers
can print into the Textual transcript unchanged.

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

The terminal shows all of this working: you can watch the activity strip accumulate
real sources from your corpus before the answer, see which model tier ran, and read
the per-turn summary (steps · sources · model · elapsed) — the same format Claude
Code uses for (tool uses · tokens · time).

Usage:
    quest-ai-runner chat
    quest-ai-runner chat --rep "Alex's AI" --persona-file path/to/skill.md
    quest-ai-runner chat --goal-id <quest-goal-id>

Example workflow (task execution):
    ❯ implement markdown rendering for responses
    (AI identifies this as code work, shows planned changes)

    (gathers relevant sources, deep_runner applies changes)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .core.orchestrator import Orchestrator, OrchestratorResult, ProgressEvent


# ── Optional rendering dependency ─────────────────────────────────────────────

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


# ── Model labels and deep-run tracking ────────────────────────────────────────

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

    def get_dashboard(self, lines_per_run: Optional[int] = None,
                      active_run_id: Optional[str] = None) -> str:
        """Return a dashboard summary of all runs with latest output.

        ``lines_per_run`` controls how many output lines are shown per agent.
        When omitted it scales automatically: 5 for one run, 3 for two, 2 for three+.

        ``active_run_id`` (optional), when it names one of the runs, marks that run's header line
        as the one Alt+D/Tab/a click would currently open, so a user with several concurrent runs
        can see which one is selected without having to open the detail panel first.
        """
        text, _ = self._render_dashboard(lines_per_run, active_run_id)
        return text

    def get_dashboard_with_map(self, lines_per_run: Optional[int] = None,
                               active_run_id: Optional[str] = None) -> "Tuple[str, Dict[int, str]]":
        """Same as ``get_dashboard``, plus a ``{row_index: run_id}`` map (0-based, matching the
        returned text's lines) so a UI can hit-test a click's row against the run it fell within
        (click-to-expand)."""
        return self._render_dashboard(lines_per_run, active_run_id)

    def _render_dashboard(self, lines_per_run: Optional[int],
                          active_run_id: Optional[str]) -> "Tuple[str, Dict[int, str]]":
        with self._lock:
            if not self._runs:
                return "", {}

            if lines_per_run is None:
                # Scale to keep the inline block calm but actually legible: a
                # single run gets a few lines (the common case the user reads),
                # concurrent runs tighten so the block doesn't balloon.
                n = len(self._runs)
                lines_per_run = 3 if n <= 1 else (2 if n == 2 else 1)

            lines: List[str] = []
            row_run: Dict[int, str] = {}
            for run_id, info in sorted(self._runs.items()):
                block_start = len(lines)
                status_icon = "●" if info['status'] == 'running' else ("✓" if info['status'] == 'done' else "✗")
                elapsed = time.time() - info['started']
                mins, secs = divmod(int(elapsed), 60)
                time_str = f"{mins}m{secs}s" if mins > 0 else f"{secs}s"

                # The SUBGOAL this run is working on: its own prominent (bold cyan) header line so
                # the user always sees WHAT the live actions below are for. Shown fully (generous cap
                # vs the old 60 chars that cut sentences mid-word); the renderer wraps if needed.
                # One expand/collapse arrow per run, on this header line only (the status/elapsed
                # line below carries no arrow of its own, so there's never a second one to confuse
                # with this one): "▾" when this run is the one currently open in the detail panel
                # (what Alt+D/Tab/a click would open right now), "▸" otherwise, mirroring the
                # familiar expanded/collapsed chevron convention. The open run also gets a brighter
                # (bold yellow) header instead of plain bold cyan, so it's visible at a glance.
                goal = " ".join((info['goal'] or "").split())
                if len(goal) > 160:
                    goal = goal[:160].rstrip() + "…"
                goal_text = goal or "deep task"
                if run_id == active_run_id:
                    lines.append(f"\x1b[1;33m▾ ⎅ {goal_text}\x1b[0m")
                else:
                    lines.append(f"\x1b[1;36m▸ ⎅ {goal_text}\x1b[0m")
                # Status + elapsed sit under the subgoal, then the latest live action lines.
                lines.append(f"\x1b[2m  {status_icon} {time_str}\x1b[0m")

                if info['output']:
                    output_lines = [l.strip() for l in info['output'].split('\n') if l.strip()]
                    for ol in output_lines[-lines_per_run:]:
                        prefix = "  → " if '/' in ol else "    "
                        lines.append(f"{prefix}{ol}")

                for row in range(block_start, len(lines)):
                    row_run[row] = run_id

            return "\n".join(lines), row_run

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


# ── Slash commands ────────────────────────────────────────────────────────────
#
# The canonical command list. The UI owns input handling and completion; this is
# the vocabulary it completes against and dispatches on.

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

Companion CLI commands (run outside this session, e.g. in another terminal):

  quest-ai-runner search-context "<query>"   See which context cards a query would surface
  quest-ai-runner bootstrap --dry-run        Estimate cost/time to index this corpus, before running it
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


class ChatSessionStore:
    """ConversationStore for an active chat session.

    current_slice  — TF-DF-IDF selection over the in-memory _session_history (the current session).
    related_slices — delegates to SessionFileConversationStore over the QAR conversations dir so
                     prior QAR sessions are also reachable for anaphora resolution.

    This is NOT a RetrievalAdapter and does NOT duplicate the ClaudeConversationsAdapter.
    The orchestrator's Step 1 (anaphora resolution) calls this protocol specifically to expand
    short/context-dependent messages into self-contained goal conditions before planning.
    """

    def __init__(self, session_history: "List[Tuple[str, str]]",
                 conv_dir: Optional["Path"] = None) -> None:
        self._history = session_history
        self._file_store: Optional[Any] = None
        self._conv_dir = conv_dir

    def _get_file_store(self) -> Optional[Any]:
        if self._file_store is None and self._conv_dir is not None:
            try:
                from .adapters.session_file_conversation_store import SessionFileConversationStore
                self._file_store = SessionFileConversationStore(sessions_dir=str(self._conv_dir))
            except Exception:
                pass
        return self._file_store

    def current_slice(self, conv_id: str, query: str, **kwargs) -> Any:
        try:
            from .core.adapters import ConversationContext
            from .adapters.conversation_format import select_current_slice
            messages = []
            for u, a in self._history:
                messages.append({"role": "user", "content": u})
                messages.append({"role": "assistant", "content": a})
            if not messages:
                return ConversationContext(scanned=0)
            recent_turns = kwargs.get("recent_turns", 4)
            max_chars = kwargs.get("max_chars", 6000)
            text, turns_meta, truncated = select_current_slice(
                messages, query, recent_turns=recent_turns, max_chars=max_chars
            )
            return ConversationContext(
                text=text, turns=turns_meta,
                sources=[{"conv_id": conv_id, "label": "current session"}],
                scanned=len(messages), truncated=truncated,
            )
        except Exception:
            from .core.adapters import ConversationContext
            return ConversationContext(scanned=0)

    def related_slices(self, query: str, scope: Any, **kwargs) -> Any:
        try:
            store = self._get_file_store()
            if store is None:
                from .core.adapters import ConversationContext
                return ConversationContext(scanned=0)
            return store.related_slices(query, scope or {}, **kwargs)
        except Exception:
            from .core.adapters import ConversationContext
            return ConversationContext(scanned=0)


# Loggers that carry internal per-stage bootstrap/scan diagnostics, not user-facing
# output. The user-facing summary is the separate notify()/_tell() callback path in
# config.py ("Context index: building for the first time in background.", "Context
# index ready: N card(s) indexed."), which is unaffected by logger level and keeps
# working exactly the same either way.
_BACKGROUND_BOOTSTRAP_LOGGER_NAMES = (
    "quest-ai-runner.context",                       # config.py, adapters/file_context_store.py
    "quest_ai_runner.adapters.bm25_content_store",    # adapters/bm25_content_store.py
)


def _suppress_background_bootstrap_logs(verbose: bool) -> None:
    """Raise the internal bootstrap/scan loggers to WARNING so their per-stage INFO
    progress ("stage 2 — analyzing N new files for topics", "BM25 context index:
    building for the first time", etc.) doesn't land in the chat transcript.

    Setting the level on the specific loggers (rather than relying on whatever the
    root logger's level happens to be) makes this hold regardless of which UI/entry
    point constructs the session or in what order — e.g. textual_ui.py's on_mount
    sets the root logger's level from its own verbosity flag, but may not have run
    yet by the time this is called on a worker thread. A no-op when the caller
    explicitly asked for verbose/debug output (-v/-vv): then this noise is exactly
    what was asked for. Only raises the level (never lowers it), so it doesn't
    fight an explicit DEBUG level set some other way.
    """
    if verbose:
        return
    for name in _BACKGROUND_BOOTSTRAP_LOGGER_NAMES:
        bg_log = logging.getLogger(name)
        if bg_log.level == logging.NOTSET or bg_log.level <= logging.INFO:
            bg_log.setLevel(logging.WARNING)


# ── Auto-persona resolution ─────────────────────────────────────────────────
#
# When a corpus's own top-level CLAUDE.md clearly designates a specific named persona as the
# intended owner of the work there, a session with no explicit --rep/--persona-file should pick
# it up automatically instead of starting generic ("AI: Assistant") and only revealing the right
# persona mid-answer, in prose. Domain-free by construction (hard rule #2): this only reads
# whatever CLAUDE.md the consumer's own corpus happens to contain and asks a generic question
# about it — it never hardcodes or references any specific person, org, or persona.

_PERSONA_RESOLUTION_TIMEOUT_SECONDS = 12.0
_PERSONA_RESOLUTION_MAX_CHARS = 20000  # bounded read, matches FilesAdapter's default read cap

_PERSONA_RESOLUTION_PROMPT = """You are given the top-level CLAUDE.md file from a working \
directory (project/organizational context for AI work done there). Determine whether this file \
designates ONE SPECIFIC NAMED individual or persona as the intended owner or representative who \
should be doing the AI work in this corpus — for example, instructions written in that person's \
own voice, or instructions addressed to a named assistant/agent who represents them. This is \
different from merely listing several team members, or a document that discusses a person \
without designating them as the AI's own persona for this work.

Respond with ONLY a JSON object, no other text.

If a persona is designated:
{{"name": "<the designated persona's name>", "persona_file": "<path to a fuller persona/\
instructions file for them, relative to this CLAUDE.md's own directory, if one is referenced, \
else null>"}}

If no such persona is designated:
{{"name": null, "persona_file": null}}

CLAUDE.md content:
{content}
"""


def _read_persona_file_in_corpus(corpus_root: str, rel_path: str) -> Optional[str]:
    """Resolve rel_path against corpus_root and read it, refusing anything that escapes
    corpus_root. Mirrors the containment check in adapters/files_adapter.py's
    ``_resolve_in_tree`` (resolve, then verify containment via ``relative_to``)."""
    root = Path(corpus_root).resolve()
    candidate = Path(rel_path)
    candidate = candidate if candidate.is_absolute() else (root / candidate)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _resolve_persona_from_corpus(
    cfg: "RunnerConfig", *, notify: Optional[Callable[[str], None]] = None,
) -> Optional[Tuple[str, Optional[str]]]:
    """Look for a top-level CLAUDE.md in ``cfg.corpus_root`` that designates a specific named
    persona as this corpus's intended AI representative, and resolve it to ``(name,
    persona_text)`` (``persona_text`` may be None even when a name is found).

    Cheap and bounded by design: reads only the corpus root's OWN top-level CLAUDE.md (no
    subdirectory crawling), makes exactly ONE "fast"-tier LLM call via the required
    MultiProvider/resolve_tier pattern, and never blocks session start for more than a few
    seconds. Returns None (do nothing; caller keeps today's default behavior) whenever: there is
    no corpus root, no CLAUDE.md there, no model provider wired, the LLM call times out / errors
    / returns unparseable output, or no persona is designated. Never raises.
    """
    corpus_root = getattr(cfg, "corpus_root", None)
    if not corpus_root:
        return None
    claude_md_path = os.path.join(corpus_root, "CLAUDE.md")
    if not os.path.isfile(claude_md_path):
        return None
    try:
        with open(claude_md_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(_PERSONA_RESOLUTION_MAX_CHARS)
    except OSError:
        return None
    if not content.strip():
        return None

    provider = getattr(cfg, "model_provider", None)
    if provider is None:
        return None
    try:
        from .core.model_registry import ModelRegistry
        model = ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("fast")
    except Exception:  # noqa: BLE001 — resolution is best-effort, never blocks startup
        return None

    if notify is not None:
        try:
            notify("Resolving AI persona…")
        except Exception:  # noqa: BLE001
            pass

    prompt = _PERSONA_RESOLUTION_PROMPT.format(content=content)

    def _call() -> str:
        return provider.answer([{"role": "user", "content": prompt}], model=model)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            raw = pool.submit(_call).result(timeout=_PERSONA_RESOLUTION_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — timeout, provider error, etc: fall back silently
        return None

    try:
        from .core.card_filter import _extract_json
        parsed = json.loads(_extract_json(raw or "") or "{}")
    except Exception:  # noqa: BLE001 — malformed LLM output: fall back silently
        return None
    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()

    persona_text: Optional[str] = None
    persona_file = parsed.get("persona_file")
    if isinstance(persona_file, str) and persona_file.strip():
        persona_text = _read_persona_file_in_corpus(corpus_root, persona_file.strip())

    return (name, persona_text)


def _make_startup_notifier(console: "_Console", notices: List[str], startup_notify=None):
    """Build the callback ``notify_and_log`` used to surface bootstrap/index notices.

    When a live notice callback is wired (Textual UI), it is the sole display path
    — the TUI shows the message itself. Also writing to ``console`` here would leak
    straight to the real stdout underneath the TUI's alternate screen, showing the
    same message twice. Plain (non-Textual) mode has no such callback, so direct
    console output is the only display path there.
    """

    def notify_and_log(msg: str) -> None:
        notices.append(msg)
        if startup_notify is not None:
            startup_notify(msg)
        else:
            console.dim(f"  {msg}")

    return notify_and_log


class InteractiveSession:
    """Multi-turn interactive session over a RunnerConfig's orchestrator."""

    def __init__(self, cfg: "RunnerConfig", *, rep_name: str = "Assistant",
                 persona: Optional[str] = None, goal_id: Optional[str] = None,
                 _startup_notify=None, verbose: bool = False,
                 rep_specified: bool = True, persona_specified: bool = True) -> None:
        from .config import build_orchestrator

        # Must run before build_orchestrator() spawns the background indexing thread(s).
        _suppress_background_bootstrap_logs(verbose)

        # Collect bootstrap/index notices so the UI can show them under its header.
        self._startup_notices: List[str] = []
        # Default output surface. The UI swaps this for its own console adapter as
        # soon as it has one, so the command handlers below print into its transcript.
        self._console = _Console()

        notify_and_log = _make_startup_notifier(self._console, self._startup_notices, _startup_notify)

        self._cfg = cfg
        self._rep_name = rep_name
        self._persona = persona
        self._goal_id = goal_id
        # Single-turn buffer (kept for backward compat with /save//load).
        self._last_user: str = ""
        self._last_assistant: str = ""
        self._turn_count: int = 0
        # Accumulated conversation history — written to disk so future QAR sessions can recall it.
        self._session_history: List[Tuple[str, str]] = []  # [(user_text, assistant_text), ...]
        # QAR owns this directory. ~/.claude/sessions is Claude Code's territory (read-only for QAR).
        _conv_dir = Path(os.getenv("QAR_CHAT_HISTORY_DIR") or (Path.home() / ".quest-ai-runner" / "conversations"))
        self._session_file: Optional[Path] = None
        self._conv_id: Optional[str] = None
        try:
            _conv_dir.mkdir(parents=True, exist_ok=True)
            self._session_file = _conv_dir / f"qar_chat_{_uuid.uuid4().hex}.json"
            self._conv_id = self._session_file.stem
        except Exception:
            pass

        # Wire Stage 1 anaphora resolution: in-memory current-session context +
        # past QAR session files for cross-session recall.  Must be set before
        # build_orchestrator() so the orchestrator picks it up.
        cfg.conversation_store = ChatSessionStore(self._session_history, conv_dir=_conv_dir)

        self._orch: "Orchestrator" = build_orchestrator(
            cfg, notify=notify_and_log
        )
        self._orch.cfg.instant_ack = True

        # Auto-persona resolution: only when the caller supplied NEITHER an explicit rep name
        # nor an explicit persona file. Runs after build_orchestrator() so cfg.model_provider is
        # the wrapped MultiProvider. Best-effort: falls back to today's exact behavior on any
        # failure, never raises, never blocks startup more than a few seconds (see
        # _resolve_persona_from_corpus's own timeout).
        if not rep_specified and not persona_specified:
            try:
                resolved = _resolve_persona_from_corpus(cfg, notify=notify_and_log)
            except Exception:  # noqa: BLE001 — must never break session startup
                resolved = None
            if resolved is not None:
                resolved_name, resolved_persona = resolved
                if resolved_name:
                    self._rep_name = resolved_name
                if resolved_persona:
                    self._persona = resolved_persona
        # The standing next-steps artifact of the quest folder this session is standing in
        # (QUEST_SYNC.md's QAR:MANAGED:next_steps block). Read ONCE, here in the constructor every
        # UI shares, and threaded into every turn by _effective_preamble(): a session opened in a
        # quest's folder should already hold that folder's answer to "what do I do next here"
        # instead of re-deriving one when asked, which is what happened while retrieval was the
        # only way the artifact could surface. A pure local file read, no Quest call, nothing that
        # can block startup; None (no folder, no file, no block) is exactly today's behavior.
        self._standing_next_steps = None
        try:
            from .runner.session_next_steps import load_standing_next_steps
            self._standing_next_steps = load_standing_next_steps(cfg)
        except Exception:  # noqa: BLE001 — must never break session startup
            self._standing_next_steps = None
        if self._standing_next_steps is not None:
            notify_and_log("Standing next steps for this quest loaded from QUEST_SYNC.md.")
        # Turn history for /tasks and /status commands
        self._turns: List[dict] = []  # [{user, model, tokens_in, tokens_out, elapsed, timestamp}]
        # TurnContextStore is wired automatically by resolve_context_assembler in config.py,
        # at <corpus_root>/.quest-context/turns/ — same root as file cards.
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
        # Restore persisted model/persona from qar_state.json (best-effort)
        self._load_session_state()
        # Resolve display name from skill frontmatter (display_name > name > rep_name as given)
        self._refresh_rep_name_from_skill()
        # If no skill file loaded yet, try auto-discovering one by rep name
        if not self._persona_file:
            self._try_load_skill_by_name(rep_name)
        # Build dynamic model tier menu from the registry
        self._build_model_tiers_menu()

    # -- one turn --------------------------------------------------------------

    def _last_transcript(self) -> str:
        """Return the immediately preceding exchange as a minimal transcript string."""
        if not self._session_history:
            return ""
        user, asst = self._session_history[-1]
        return f"User: {user}\nAssistant: {asst}"

    def _write_session_file(self) -> None:
        """Persist session history to disk so future sessions can recall it via the adapter."""
        if not self._session_file or not self._session_history:
            return
        try:
            messages = []
            for user_text, asst_text in self._session_history:
                messages.append({"role": "user", "content": user_text})
                messages.append({"role": "assistant", "content": asst_text})
            self._session_file.write_text(
                json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _effective_preamble(self) -> Optional[str]:
        """Combine the system prompt, persona and standing next steps into the rep_preamble passed
        to the orchestrator.

        This is where the quest folder's standing next-steps artifact becomes structural rather
        than retrievable. rep_preamble reaches the planner, the answer and the deep preamble on
        every turn, so an answer to "what should I do next here" can no longer depend on the
        artifact happening to out-score the rest of the corpus in a card search. It goes LAST, after
        the persona: the rep's identity is what the judge's truncated lens most needs to keep.
        """
        parts = []
        if self._system:
            parts.append(self._system)
        if self._persona:
            parts.append(self._persona)
        standing = getattr(self, "_standing_next_steps", None)
        if standing is not None:
            try:
                from .runner.session_next_steps import render_standing_next_steps
                block = render_standing_next_steps(standing)
            except Exception:  # noqa: BLE001 — never cost a turn its persona over this
                block = ""
            if block:
                parts.append(block)
        return "\n\n".join(parts) if parts else None

    def _maybe_refresh_next_steps(self, final) -> None:
        """Write this turn's conclusion back as the quest's standing next steps, when it earns it.

        Called once per completed turn from the UI's turn-completion path, and deliberately thin:
        every judgment about WHETHER a turn warrants a refresh, and what the refreshed artifact
        says, lives in ``runner/session_next_steps.next_steps_from_turn`` — the half no UI owns, so
        the trigger cannot drift between them. Conservative by design: only a turn that actually
        executed work and left some of it unfinished replaces the standing answer.

        Never raises, and never reports a write that did not happen: ``refresh_from_turn`` returns
        None for every declined or failed case.
        """
        standing = getattr(self, "_standing_next_steps", None)
        if standing is None or final is None:
            return
        try:
            from .runner.session_next_steps import refresh_from_turn
            result = refresh_from_turn(
                self._quest_client(), standing,
                kind=getattr(final, "kind", "") or "",
                goals=list(getattr(final, "goals", None) or []),
                deep_results=list(getattr(final, "deep_results", None) or []),
            )
        except Exception:  # noqa: BLE001 — the artifact must never fail an otherwise-good turn
            return
        if result is None:
            return
        self._console.dim(f"  Standing next steps refreshed: {result.sync_path}")
        if result.detail:
            # The local file is current either way; what may not have happened is the Quest-side
            # write, and a silently local-only artifact is how the two views drift apart again.
            self._console.dim(f"  On Quest: {result.detail}")

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

    # -- Model tier menu data (the UI renders and picks) -----------------------

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
            "session_history": self._session_history,
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
        raw_history = payload.get("session_history") or []
        self._session_history = [tuple(t) for t in raw_history if isinstance(t, (list, tuple)) and len(t) == 2]
        # Backward compat: if no session_history saved but last_user/assistant exist, seed history.
        if not self._session_history and self._last_user:
            self._session_history = [(self._last_user, self._last_assistant)]
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

        usage_line = self._daily_usage_status()
        if usage_line:
            c.dim(f"  Daily budget:  {usage_line}")
            c.line("")

    def _daily_usage_status(self) -> Optional[str]:
        """Read-only daily-usage status line for /status, or None if no usage file exists.

        Instantiates a DailyUsageTracker from the same env path the poller uses
        (QAR_DAILY_USAGE_PATH, default ./qar_daily_usage.json) purely to read today's tally;
        this session never records against it (only the poller/orchestrator calls do that).
        """
        try:
            path = (os.getenv("QAR_DAILY_USAGE_PATH") or "").strip() or "./qar_daily_usage.json"
            if not os.path.exists(path):
                return None
            from .usage import DailyUsageTracker
            return DailyUsageTracker.from_env().status()
        except Exception:  # noqa: BLE001 — status display must never break /status
            return None
