"""A deterministic pass that takes out of a reply every sentence that states a figure the turn did not read.

Rule: a figure in a reply (a count, a rate, a day of the month) must come from what this turn read: the results it gathered, the context it was handed, or the person's own message. A figure
from nowhere (an old count recalled from memory, a target that no read shows, a date moved to another record) is how a status answer goes wrong, and a prompt line asking the model not to
leaks. The code reads digits only, never meaning: a sentence or bullet with a figure that no read holds is dropped; the rest of the reply stays. A reply that would be left empty is kept
whole (nothing is better than a blank), and a drafted deliverable (copy the person asked to have written) is not touched, since its numbers are the copy's own.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Mapping, Sequence, Tuple

FIGURE = re.compile(r"\d+(?:[.,]\d+)?")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*\"'(\[])")
LIST_NUMBER = re.compile(r"^\s*(?:[-*•]\s*)?\d+[.)]\s+")
READ_ITEM_CHARS = 2500
CONTEXT_CHARS = 5000


def evidence_of(gathered: Iterable[Any], context_view: str, user_message: str) -> str:
    """What the turn read, as one text: each gathered result (its first ``READ_ITEM_CHARS`` characters), the start of the context it was handed, and the person's message."""
    parts: List[str] = [str(user_message or ""), str(context_view or "")[:CONTEXT_CHARS]]
    for item in gathered or []:
        if isinstance(item, Mapping) and not item.get("planner_only"):
            parts.append(str(item.get("tool_text") or item.get("text") or "")[:READ_ITEM_CHARS])
    return "\n".join(parts)


def figures_of(text: str) -> List[str]:
    return FIGURE.findall(LIST_NUMBER.sub("", text or ""))


def strip_unsupported(text: str, evidence: str) -> Tuple[str, List[str]]:
    """``(the reply without its unsupported sentences, the figures that were unsupported)``. Sentences are cut at line ends and sentence ends; a figure is supported when the same
    number occurs in the evidence as a whole number."""
    held = set(FIGURE.findall(evidence or ""))
    kept: List[str] = []
    removed: List[str] = []
    for raw_line in str(text or "").split("\n"):
        numbered = LIST_NUMBER.match(raw_line)
        prefix, line = (numbered.group(0), raw_line[numbered.end():]) if numbered else ("", raw_line)         # "1. " opens a list item; it is not the end of a sentence
        pieces = SENTENCE_END.split(line) if line.strip() else [line]
        survivors = []
        for piece in pieces:
            bad = [f for f in figures_of(piece) if f not in held]
            if bad:
                removed.extend(bad)
            else:
                survivors.append(piece)
        if survivors or not line.strip():
            kept.append(prefix + " ".join(survivors) if survivors else raw_line)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return (result, removed) if result else (str(text or ""), [])
