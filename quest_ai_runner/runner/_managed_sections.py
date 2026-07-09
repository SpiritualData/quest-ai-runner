"""Managed-section helpers — the marker-delimited round-trip primitive shared by the runner's
local-file <-> Quest sync modules (``rep_sync.py``, ``goal_folder_sync.py``).

A "managed section" is a block of a human-editable local file delimited by an HTML-comment
marker pair, e.g.::

    <!-- QAR:MANAGED:persona START -->
    ...content the sync owns and re-renders...
    <!-- QAR:MANAGED:persona END -->

Everything outside marker pairs belongs to the file's human owner and is never touched. Replacing
a managed section is idempotent: re-rendering the same body yields the same file, byte for byte.
"""
from __future__ import annotations

import re


def replace_between(text: str, start: str, end: str, body: str) -> str:
    """Replace the content between (and including) a marker pair, or append a fresh block.

    Idempotent: re-rendering with the same body yields the same file. If the markers are absent
    (first render into a human-authored or empty file) the block is appended, leaving existing
    content intact.
    """
    block = f"{start}\n{body}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _m: block, text, count=1)
    sep = "" if (not text or text.endswith("\n\n")) else ("\n" if text.endswith("\n") else "\n\n")
    return f"{text}{sep}{block}\n"


def extract_between(text: str, start: str, end: str) -> "str | None":
    """Return the text strictly between a marker pair, or None if the pair isn't present."""
    pattern = re.compile(re.escape(start) + r"\n?(.*?)\n?" + re.escape(end), re.DOTALL)
    m = pattern.search(text or "")
    return m.group(1) if m else None
