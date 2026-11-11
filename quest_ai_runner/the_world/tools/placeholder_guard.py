"""Content that is a placeholder is never saved to a person's records.

A note or a goal written by an AI step must say something. Text that announces its own emptiness ("pending", "to be added", "findings to follow"), carries a template marker
(``{{x}}``, ``<insert ...>``, ``[TBD]``, ``lorem ipsum``), points the reader elsewhere for the content it should hold ("review that investigation for the figure"), or is a heading
with nothing under it is refused with a plain reason, so the write never happens and the reply cannot say it did. Pure; the markers are template syntax and fixed phrases of
placeholder text, never judgements about meaning.
"""
from __future__ import annotations

import re
from typing import Optional

#: Template syntax and fixed placeholder phrases.
MARKERS = (
    re.compile(r"\{\{[^}]*\}\}|\{%[^%]*%\}|\$\{[^}]*\}"),                                  # {{x}}, {% x %}, ${x}
    re.compile(r"<\s*(?:insert|fill|todo|tbd|placeholder)[^>]*>", re.IGNORECASE),            # <insert here>
    re.compile(r"\[\s*(?:tbd|todo|insert[^\]]*|fill[^\]]*|placeholder|\.\.\.|to be (?:added|filled|written)[^\]]*)\s*\]", re.IGNORECASE),
    re.compile(r"\blorem ipsum\b", re.IGNORECASE),
    re.compile(r"\((?:pending|tbd|todo|placeholder|draft|to follow)\)", re.IGNORECASE),
    re.compile(r"\b(?:to be (?:added|filled in|filled|written|completed|determined|confirmed)|will be (?:added|filled in|written) (?:later|once|when)|findings? (?:to follow|to come|pending)|content (?:to follow|pending))\b", re.IGNORECASE),
    re.compile(r"\b(?:attached to|listed in|recorded in|see) (?:the )?(?:quest'?s? )?investigations?(?: list)?\b.{0,80}\b(?:review|open|see|check)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:review|open|see|check) (?:that|the|this) investigation for\b", re.IGNORECASE),
)
HEADING = re.compile(r"^\s*(?:#+\s*)?[A-Za-z][^\n]{0,60}:\s*$")
BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*(.*)$")


def placeholder_reason(text: str) -> Optional[str]:
    """Why ``text`` may not be saved as a note or goal text, or None when it carries content. Plain words for the reply."""
    body = (text or "").strip()
    if not body:
        return "it is empty"
    for marker in MARKERS:
        found = marker.search(body)
        if found:
            return f"it holds placeholder text ({found.group(0).strip()[:60]!r}), not content"
    lines = [ln for ln in body.splitlines() if ln.strip()]
    for index, line in enumerate(lines):
        if HEADING.match(line):
            nxt = lines[index + 1] if index + 1 < len(lines) else ""
            if not nxt or HEADING.match(nxt):
                return f"the section {line.strip()[:40]!r} has nothing under it"
        bullet = BULLET.match(line)
        if bullet and not re.search(r"[A-Za-z0-9]", bullet.group(1)):
            return "a list item is empty"
    return None
