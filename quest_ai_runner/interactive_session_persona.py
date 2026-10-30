"""Startup helpers: quiet background bootstrap logs, corpus persona resolution and the startup notifier.

Split out of ``interactive_session.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable, List, Optional, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from .interactive_session_render import _Console


# Loggers that carry internal per-stage bootstrap/scan diagnostics, not user-facing
# output. The user-facing summary is the separate notify()/_tell() callback path in
# config.py ("Context index: building for the first time in background.", "Context
# index ready: N card(s) indexed."), which is unaffected by logger level and keeps
# working exactly the same either way.
_BACKGROUND_BOOTSTRAP_LOGGER_NAMES = (
    "quest-ai-runner.context",                       # config.py, adapters/file_context_store.py
    "hubofhubs.recall",                              # the card store's folder review, ceilings, mounts and bootstrap, moved to the hub
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
            notify("Starting up…")
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
