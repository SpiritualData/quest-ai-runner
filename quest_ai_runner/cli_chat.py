"""The `chat` subcommand: prerequisite check, conversation resume and the attended-session launcher.

Split out of ``cli.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
import sys

from .cli_config import _config_from_env


# `chat --resume` with no id: open the conversation picker (Claude Code's behavior).
RESUME_PICK = "\0pick"


def resume_command(conv_id: str, rep: Optional[str] = None) -> str:
    """The command that reopens conversation ``conv_id``, keeping the rep the user named."""
    from .chat_conversations import FILE_PREFIX
    short_id = conv_id[len(FILE_PREFIX):] if conv_id.startswith(FILE_PREFIX) else conv_id
    parts = ["quest-ai-runner", "chat"] + ([rep] if rep else []) + ["--resume", short_id]
    return " ".join(parts)


def _check_chat_prerequisites(env=None, which=shutil.which) -> List[str]:
    """Validate what `chat` needs before it can run, without opening the TUI.

    Returns a list of human-readable problems; an empty list means chat is ready to start.
    Checks: a model provider must be reachable (an API key env var, or the claude CLI on PATH
    for the keyless default), and the corpus/context store path, if configured, must exist.
    A pure function (env/which injectable) so it is testable offline.
    """
    env = env if env is not None else os.environ
    problems: List[str] = []

    backend = (env.get("QAR_MODEL_BACKEND") or "").strip().lower()
    has_key = bool(
        env.get("OPENAI_API_KEY") or env.get("GOOGLE_API_KEY") or env.get("ANTHROPIC_API_KEY")
    )
    if not backend or backend == "claude_cli":
        if not has_key and which("claude") is None:
            problems.append(
                "no model provider available: set ANTHROPIC_API_KEY (or OPENAI_API_KEY / "
                "GOOGLE_API_KEY), or install the claude CLI and log in"
            )

    corpus = env.get("QAR_CORPUS_ROOT")
    if corpus and not os.path.isdir(corpus):
        problems.append(f"QAR_CORPUS_ROOT is set but does not exist: {corpus}")

    cards_dir = env.get("QAR_CONTEXT_CARDS_DIR")
    if cards_dir and not os.path.isdir(cards_dir):
        problems.append(f"QAR_CONTEXT_CARDS_DIR is set but does not exist: {cards_dir}")

    return problems


def _cmd_chat(args, log) -> int:
    """Run the `chat` subcommand; returns the process exit code."""
    if getattr(args, "check", False):
        chat_problems = _check_chat_prerequisites()
        if chat_problems:
            for p in chat_problems:
                log.error("chat not ready: %s", p)
            return 1
        log.info("chat prerequisites OK")
        return 0

    cfg = _config_from_env(getattr(args, "config", None))
    # chat only needs a model provider — Quest credentials and a retrieval
    # adapter are optional (no corpus = no grounding, but still works).
    _skip = {"quest", "retrieval adapter", "team_id"}
    problems = [p for p in cfg.validate()
                if not any(kw in p for kw in _skip)]
    if problems:
        for p in problems:
            log.error("config error: %s", p)
        return 1
    # Track whether the caller explicitly named a rep / persona file (env var or flag),
    # as opposed to falling through to the "Assistant" default -- distinct from just
    # checking the final rep_name string, since "--rep Assistant" must count as explicit
    # too. Session-level auto-persona-resolution (interactive_session.py) only runs when
    # NEITHER was explicitly given.
    rep_positional = getattr(args, "rep_positional", None)
    rep_specified = bool(args.rep or rep_positional or os.getenv("QAR_REP_NAME"))
    rep_name = args.rep or rep_positional or os.getenv("QAR_REP_NAME") or "Assistant"
    persona = None
    persona_path = args.persona_file or os.getenv("QAR_REP_PERSONA_FILE")
    if persona_path:
        try:
            with open(persona_path) as fh:
                persona = fh.read()
        except OSError as e:
            log.error("could not read persona file %r: %s", persona_path, e)
            return 1
    elif rep_positional and cfg.corpus_root:
        # `qar <name>` shorthand: look for a <name>/CLAUDE.md directly under the corpus
        # root (the character-folder convention) and load it as the persona, same as
        # passing --persona-file explicitly. Silent no-op if there is no such folder --
        # the session still runs, just with only the display name set.
        from .interactive_session import _read_persona_file_in_corpus
        persona = _read_persona_file_in_corpus(cfg.corpus_root, f"{rep_positional}/CLAUDE.md")
        if persona is None:
            persona = _read_persona_file_in_corpus(
                cfg.corpus_root, f"{rep_positional.lower()}/CLAUDE.md")
    persona_specified = persona is not None

    # --list-conversations / --resume: resolved HERE, before the TUI takes the screen, so a
    # bad id is a plain one-line error in the terminal instead of a crash inside the app.
    from .chat_conversations import (
        format_conversation_list, list_conversations, resolve_conversation,
    )
    if getattr(args, "list_conversations", False):
        print(format_conversation_list(list_conversations(corpus_root=cfg.corpus_root)))
        return 0
    resume = None
    if getattr(args, "resume", None) == RESUME_PICK:
        convs = list_conversations(corpus_root=cfg.corpus_root)
        if not convs:
            log.error("cannot resume: no saved chat conversations to resume")
            return 1
        import sys  # main() imports sys locally further down, so it is local here too
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            # No terminal to pick in: show the choices and how to name one.
            print(format_conversation_list(convs))
            return 1
        from .conversation_picker import pick_conversation
        resume = pick_conversation(convs)
        if resume is None:
            return 0  # cancelled
    elif getattr(args, "resume", None) is not None:
        try:
            resume = resolve_conversation(args.resume, corpus_root=cfg.corpus_root)
        except LookupError as e:
            log.error("cannot resume: %s", e)
            return 1

    # The Textual UI is the only chat UI. `textual` is a CORE dependency (see
    # pyproject.toml), so it should always import from a correctly installed
    # environment; there is no second renderer to degrade to. If it does not
    # import, the install is incomplete (e.g. an editable install never re-synced
    # after `textual` was added to dependencies) — say exactly that and how to fix
    # it, rather than dying on a raw ImportError traceback.
    from .textual_session import is_textual_available, start_textual_interactive
    if not is_textual_available():
        log.error(
            "Textual UI unavailable: the 'textual' package failed to import. It is a "
            "required dependency of quest-ai-runner and there is no fallback chat UI. "
            "Run `pip install --upgrade -e .` (from a source checkout) or `pip install "
            "--upgrade quest-ai-runner` to install it, then try `quest-ai-runner chat` again."
        )
        return 1
    conv_id = start_textual_interactive(
        cfg, rep_name=rep_name, persona=persona, goal_id=args.goal_id,
        verbosity=args.verbose, rep_specified=rep_specified,
        persona_specified=persona_specified, resume=resume)
    if conv_id:
        # Printed after the TUI has left the alternate screen, so it stays in the terminal.
        print(f"\nResume this session with:\n  {resume_command(conv_id, rep_positional)}")
    return 0
