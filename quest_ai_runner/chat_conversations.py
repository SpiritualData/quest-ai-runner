"""Find and reopen QAR's own saved chat conversations (``quest-ai-runner chat --resume``).

Every attended chat session already writes its turns to one JSON file,
``<QAR_CHAT_HISTORY_DIR>/qar_chat_<hex>.json`` (default ``~/.quest-ai-runner/conversations``).
Resuming means reopening one of those files: its turns become the new session's in-memory
history (so anaphora resolution and "what we just said" work exactly as if the session never
ended), and later turns are appended to the SAME file instead of starting a new one, so the
conversation stays one record.

Only QAR's own directory is read here. ``~/.claude/sessions`` belongs to Claude Code and is
never a resume target.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FILE_PREFIX = "qar_chat_"


def chat_history_dir() -> Path:
    """The directory QAR writes chat conversations to (``QAR_CHAT_HISTORY_DIR`` wins)."""
    return Path(os.getenv("QAR_CHAT_HISTORY_DIR") or (Path.home() / ".quest-ai-runner" / "conversations"))


@dataclass
class SavedConversation:
    """One saved chat conversation file, with just enough read to list and resume it."""

    path: Path
    history: List[Tuple[str, str]] = field(default_factory=list)
    meta: Dict[str, object] = field(default_factory=dict)

    @property
    def conv_id(self) -> str:
        return self.path.stem

    @property
    def short_id(self) -> str:
        return self.conv_id[len(FILE_PREFIX):] if self.conv_id.startswith(FILE_PREFIX) else self.conv_id

    @property
    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def first_message(self, limit: int = 70) -> str:
        text = " ".join((self.history[0][0] if self.history else "").split())
        return text if len(text) <= limit else text[: limit - 3] + "..."


def read_conversation(path: Path) -> Optional[SavedConversation]:
    """Parse one conversation file into (user, assistant) pairs. None if unreadable or empty.

    The file holds ``{"messages": [{"role", "content"}, ...], ...metadata}``. Messages are paired
    in order; a trailing user message with no reply (a session killed mid-turn) is kept with an
    empty reply so the question is not silently lost.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
    if not isinstance(messages, list):
        return None
    history: List[Tuple[str, str]] = []
    pending_user: Optional[str] = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        if role == "user":
            if pending_user is not None:
                history.append((pending_user, ""))
            pending_user = content
        elif role == "assistant" and pending_user is not None:
            history.append((pending_user, content))
            pending_user = None
    if pending_user is not None:
        history.append((pending_user, ""))
    if not history:
        return None
    meta = {k: v for k, v in data.items() if k != "messages"}
    return SavedConversation(path=Path(path), history=history, meta=meta)


def list_conversations(conv_dir: Optional[Path] = None,
                       corpus_root: Optional[str] = None) -> List[SavedConversation]:
    """Saved conversations, newest first.

    With ``corpus_root``, a conversation that recorded a DIFFERENT corpus is left out (one
    history directory can serve several corpora); one that recorded none (written before the
    metadata existed) is kept.
    """
    conv_dir = Path(conv_dir) if conv_dir is not None else chat_history_dir()
    if not conv_dir.is_dir():
        return []
    found: List[SavedConversation] = []
    for path in conv_dir.glob(f"{FILE_PREFIX}*.json"):
        conv = read_conversation(path)
        if conv is None:
            continue
        recorded = conv.meta.get("corpus_root")
        if corpus_root and recorded and os.path.abspath(str(recorded)) != os.path.abspath(corpus_root):
            continue
        found.append(conv)
    found.sort(key=lambda c: c.mtime, reverse=True)
    return found


def resolve_conversation(ref: Optional[str], conv_dir: Optional[Path] = None,
                         corpus_root: Optional[str] = None) -> SavedConversation:
    """The conversation ``--resume [REF]`` names. Raises LookupError with a user-facing reason.

    An empty REF (or ``last``) is the most recent conversation for this corpus. Otherwise REF is
    a conversation id, with or without the ``qar_chat_`` prefix, or any unambiguous prefix of it.
    An explicit id is looked up across every corpus, since the user named it on purpose.
    """
    ref = (ref or "").strip()
    if ref.endswith(".json"):
        ref = ref[: -len(".json")]
    if not ref or ref == "last":
        convs = list_conversations(conv_dir, corpus_root=corpus_root)
        if not convs:
            raise LookupError("no saved chat conversations to resume")
        return convs[0]
    needle = ref[len(FILE_PREFIX):] if ref.startswith(FILE_PREFIX) else ref
    matches = [c for c in list_conversations(conv_dir) if c.short_id.startswith(needle)]
    exact = [c for c in matches if c.short_id == needle]
    if exact:
        return exact[0]
    if not matches:
        raise LookupError(f"no saved chat conversation matches {ref!r}")
    if len(matches) > 1:
        ids = ", ".join(c.short_id[:12] for c in matches[:5])
        raise LookupError(f"{ref!r} matches {len(matches)} conversations ({ids}); give more of the id")
    return matches[0]


def format_conversation_list(convs: List[SavedConversation], limit: int = 20) -> str:
    """Plain-text table for ``chat --list-conversations``: id, age, turns, first message."""
    import time
    if not convs:
        return "No saved chat conversations."
    lines = []
    now = time.time()
    for conv in convs[:limit]:
        age = max(0.0, now - conv.mtime)
        if age < 3600:
            when = f"{int(age // 60)}m ago"
        elif age < 86400:
            when = f"{int(age // 3600)}h ago"
        else:
            when = f"{int(age // 86400)}d ago"
        turns = len(conv.history)
        lines.append(f"{conv.short_id[:12]}  {when:>8}  {turns:>3} turn{'s' if turns != 1 else ' '}  "
                     f"{conv.first_message()}")
    if len(convs) > limit:
        lines.append(f"... and {len(convs) - limit} older")
    lines.append("")
    lines.append("Resume one with: quest-ai-runner chat --resume <id>")
    return "\n".join(lines)
