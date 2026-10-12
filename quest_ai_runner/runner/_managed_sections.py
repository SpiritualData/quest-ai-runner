"""Managed-section helpers — the marker-delimited round-trip primitive shared by the runner's
local-file <-> Quest sync modules (``rep_sync.py``, ``quest_folder_sync.py``).

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


MANAGED_MARKER_RE = re.compile(r"<!--\s*QAR:MANAGED:[A-Za-z0-9_.-]+\s+(?:START|END)\s*-->")
MANAGED_BLOCK_RE = re.compile(
    r"<!--\s*QAR:MANAGED:([A-Za-z0-9_.-]+)\s+START\s*-->.*?<!--\s*QAR:MANAGED:\1\s+END\s*-->",
    re.DOTALL)


class ManagedSectionError(ValueError):
    """A managed block body would contain a managed marker, which would nest the block in itself."""


def strip_managed_blocks(text: str) -> str:
    """Remove every managed block (and any stray managed marker) from ``text``.

    Use on any text that is about to become the body of a managed block, so a block can never
    contain a marker and a re-render can never nest the previous file inside itself. Handles
    already-nested corrupt input by repeating until no marker remains.
    """
    out = text or ""
    for _ in range(10000):
        new = MANAGED_BLOCK_RE.sub("", out)
        if new == out:
            break
        out = new
    out = MANAGED_MARKER_RE.sub("", out)
    return out.strip()


def replace_between(text: str, start: str, end: str, body: str) -> str:
    """Replace the content between (and including) a marker pair, or append a fresh block.

    Idempotent: re-rendering with the same body yields the same file. If the markers are absent
    (first render into a human-authored or empty file) the block is appended, leaving existing
    content intact. Raises :class:`ManagedSectionError` if ``body`` contains a managed marker.
    """
    if MANAGED_MARKER_RE.search(body or "") or start in (body or "") or end in (body or ""):
        raise ManagedSectionError(
            f"refusing to render {start!r} block: body contains a managed marker (would nest)")
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
