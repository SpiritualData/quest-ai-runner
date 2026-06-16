"""Relevant-turn transcript selection -- stdlib only, no LLM call."""
import re
from dataclasses import dataclass, field
from typing import List, Optional

_STOP = frozenset("""
a an the is are was were be been being have has had do does did will would could should may
might shall can need to of in on at by for with about as into through during before after
above below from and or but not this that these those i you he she it we they what which
who how when where why all both each few more most other some such no nor so yet either
neither s t re ve ll d m
""".split())


def _keywords(text: str) -> frozenset:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return frozenset(w for w in words if w not in _STOP and len(w) > 2)


@dataclass
class _Turn:
    user: str
    assistant: str
    kw: frozenset = field(default_factory=frozenset)


class TurnMemory:
    """Stores conversation turns and retrieves the ones relevant to a new message.

    Replaces raw transcript accumulation. Each new message gets a transcript built
    from: (a) always the most recent ``always_recent`` turns verbatim, and (b) up to
    ``max_older`` older turns scored by keyword overlap with the current message.
    Irrelevant turns are excluded -- their content is not compressed or summarized,
    just not included. No LLM call; stdlib only.

    Usage::

        mem = TurnMemory()
        # after each turn:
        mem.add(user_text, assistant_text)
        # before each turn:
        transcript = mem.relevant_transcript(new_user_message)
        result = orch.run(new_user_message, transcript=transcript, ...)
    """

    def __init__(self, always_recent: int = 2, max_older: int = 4):
        self._turns: List[_Turn] = []
        self._always_recent = max(1, always_recent)
        self._max_older = max(0, max_older)

    # ------------------------------------------------------------------

    def add(self, user: str, assistant: str) -> None:
        """Record a completed turn."""
        kw = _keywords(user + " " + (assistant or ""))
        self._turns.append(_Turn(user=user, assistant=assistant or "", kw=kw))

    def relevant_transcript(self, current_message: str) -> str:
        """Return a transcript string containing only the turns relevant to *current_message*."""
        if not self._turns:
            return ""
        n = len(self._turns)
        recent_n = min(self._always_recent, n)
        recent = self._turns[n - recent_n:]
        recent_ids = set(id(t) for t in recent)

        older = self._turns[: n - recent_n]
        selected_older: List[_Turn] = []
        if older and self._max_older > 0:
            cur_kw = _keywords(current_message)
            scored = sorted(
                ((len(cur_kw & t.kw), i, t) for i, t in enumerate(older) if t.kw & cur_kw),
                key=lambda x: (-x[0], -x[1]),  # highest overlap, most recent first
            )
            selected_older = [t for _, _, t in scored[: self._max_older]]
            # restore chronological order
            idx = {id(t): i for i, t in enumerate(self._turns)}
            selected_older.sort(key=lambda t: idx[id(t)])

        all_selected = selected_older + [t for t in recent if id(t) not in {id(x) for x in selected_older}]
        parts: List[str] = []
        for t in all_selected:
            parts.append(f"User: {t.user}")
            parts.append(f"Assistant: {t.assistant}")
        return "\n".join(parts)

    def clear(self) -> None:
        self._turns.clear()

    @property
    def turn_count(self) -> int:
        return len(self._turns)
