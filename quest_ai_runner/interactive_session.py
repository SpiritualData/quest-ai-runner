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

# Imports below stay importable from here for existing callers, although the code now lives in
# the interactive_session_<role> modules re-exported at the bottom of this block.
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

from .interactive_session_render import (  # noqa: F401
    _BLUE,
    _BOLD,
    _BRIGHT_CYAN,
    _CYAN,
    _Console,
    _DIM,
    _GOLD,
    _GREEN,
    _MAGENTA,
    _RESET,
    _YELLOW,
    _a,
    _bullet,
    _highlight_ansi,
    _model_display,
    _model_label,
    _parse_skill_frontmatter,
    _HAS_RICH,
    _RichConsole,
    _RichMarkdown,
    yaml,
)
from .interactive_session_tracker import (  # noqa: F401
    _DeepRunTracker,
)
from .interactive_session_text import (  # noqa: F401
    _BANNER,
    _HELP,
    _SLASH_COMMANDS,
)
from .interactive_session_store import (  # noqa: F401
    ChatSessionStore,
)
from .interactive_session_persona import (  # noqa: F401
    _BACKGROUND_BOOTSTRAP_LOGGER_NAMES,
    _PERSONA_RESOLUTION_MAX_CHARS,
    _PERSONA_RESOLUTION_PROMPT,
    _PERSONA_RESOLUTION_TIMEOUT_SECONDS,
    _make_startup_notifier,
    _read_persona_file_in_corpus,
    _resolve_persona_from_corpus,
    _suppress_background_bootstrap_logs,
)
from .interactive_session_quests import QuestsMixin
from .interactive_session_commands import CommandsMixin
from .interactive_session_persist import PersistenceMixin
from .interactive_session_status import StatusMixin


if TYPE_CHECKING:
    from .chat_conversations import SavedConversation
    from .config import RunnerConfig
    from .core.orchestrator import Orchestrator, OrchestratorResult, ProgressEvent


class InteractiveSession(QuestsMixin, CommandsMixin, PersistenceMixin, StatusMixin):
    """Multi-turn interactive session over a RunnerConfig's orchestrator."""

    def __init__(self, cfg: "RunnerConfig", *, rep_name: str = "Assistant",
                 persona: Optional[str] = None, goal_id: Optional[str] = None,
                 _startup_notify=None, verbose: bool = False,
                 rep_specified: bool = True, persona_specified: bool = True,
                 resume: Optional["SavedConversation"] = None) -> None:
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
        # `chat --resume`: the conversation this session continues, or None for a fresh one.
        self.resumed = resume
        if resume is not None:
            # Continue in the SAME file (one conversation stays one record) with its turns as
            # this session's history. Mutate in place: ChatSessionStore below holds this list.
            self._session_file = resume.path
            self._conv_id = resume.conv_id
            self._session_history.extend(resume.history)
            self._turn_count = len(resume.history)
            self._last_user, self._last_assistant = resume.history[-1]
            if goal_id is None and isinstance(resume.meta.get("goal_id"), str):
                self._goal_id = resume.meta["goal_id"]
        else:
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
        # Every locally synced quest folder, so each turn can be grounded in the quest it is about
        # without the brain having to go looking (runner/quest_folder_index.py). A plain directory
        # walk, tens of milliseconds. "auto" matches every message; "none" never adds a quest (for
        # people whose chats are mostly not about a quest); a pinned quest is added to every turn.
        self.quest_folders = []
        self.quest_search_root = ""
        try:
            from .runner.quest_folder_index import discover_quest_folders
            self.quest_search_root = getattr(cfg, "corpus_root", None) or os.getcwd()
            self.quest_folders = discover_quest_folders(
                self.quest_search_root, getattr(cfg, "quest_folder_map", None))
        except Exception:  # noqa: BLE001 — must never break session startup
            self.quest_folders = []
        # Each synced quest is a context card, like every quest is in Quest itself, so selection can
        # find it on its own and a matched turn can put it first. Rewritten only when it changed.
        try:
            from .runner.quest_folder_index import card_writer, sync_quest_cards
            sync_quest_cards(card_writer(getattr(self._orch, "context_assembler", None)),
                             self.quest_folders, getattr(cfg, "corpus_root", None))
        except Exception:  # noqa: BLE001 — must never break session startup
            pass
        self.quest_match_mode = ("none" if os.getenv("QAR_QUEST_AUTO_MATCH", "1").strip().lower()
                                 in ("0", "false", "no", "off", "none") else "auto")
        self.pinned_quest = None
        # The quest whose synced folder this session was started in: the default for every turn.
        try:
            from .runner.quest_folder_index import quest_for_directory
            self.home_quest = quest_for_directory(os.getcwd(), self.quest_folders)
        except Exception:  # noqa: BLE001 — grounding is a bonus, never a reason to fail startup
            self.home_quest = None
        self.turn_quest = None  # the quest the latest turn was grounded in, for the UI
        # Quests the Quest account can reach but that have no local folder, for the "/quest" menu.
        # Fetched off-thread: a slow or absent Quest API must never delay the session.
        self.remote_quests = []
        # `chat --goal-id <quest_id>` starts with that quest selected, same as `/quest <id>`.
        if self._goal_id:
            from .runner.quest_folder_index import quest_without_folder
            self.pinned_quest = next((q for q in self.quest_folders if q.quest_id == self._goal_id),
                                     None) or quest_without_folder(self._goal_id)
        self.quest_list_listeners = []  # called (no args) when the remote list arrives
        threading.Thread(target=self.fetch_remote_quests, name="qar-quest-list", daemon=True).start()
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
        if getattr(self, "saved_quest_match_mode", None):
            self.quest_match_mode = self.saved_quest_match_mode
        # Resolve display name from skill frontmatter (display_name > name > rep_name as given)
        self._refresh_rep_name_from_skill()
        # If no skill file loaded yet, try auto-discovering one by rep name
        if not self._persona_file:
            self._try_load_skill_by_name(rep_name)
        # Build dynamic model tier menu from the registry
        self._build_model_tiers_menu()
