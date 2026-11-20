"""``CONTEXT_UPDATES.md``: everything new on a quest, written where the agent works, indexed in the prompt.

An autopilot pass, an interactive session and a chat run each need to see what is new on a quest since
THEY last looked. Inlining all of it makes the brief long and burns the budget on material the run may
not need; summarising it loses the person's own words. So the bundle is written in full to ONE rolling
file in the quest's working folder (beside ``QUEST_SYNC.md``), and the prompt carries a short index: the
path, the count per source, the first line of up to ``INDEX_ITEMS`` items, and the instruction to read
the file as needed.

The file is newest section first. Each section is headed with the consumer, the window (since -> now) and
the counts per source, then the items grouped by source with their full text (the person's words are
verbatim). Sections older than ``KEEP_DAYS`` are trimmed on every write. A pass with nothing new writes
nothing, so the file never fills with empty sections.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from quest_ai_runner.body.intake_sources.context_update_bundle import ContextUpdates
from quest_ai_runner.body.intake_sources.context_update_receipts import _OFFER_PREAMBLE, usage_receipt_gate
from quest_ai_runner.body.intake_sources.context_update_types import BLOCK_END, BLOCK_START, _as_utc, _utcnow, log

FILE_NAME = "CONTEXT_UPDATES.md"
KEEP_DAYS = 30
INDEX_ITEMS = 15
HEADER = ("# Context updates\n\n"
          "What is new on this quest, written by `quest-ai-runner` (newest section first). Each section is "
          "what ONE consumer (an autopilot, an interactive session, a chat) had not yet seen; the person's "
          "own words are verbatim. Sections older than 30 days are trimmed.\n")
MARKER = "<!-- context-updates-section {stamp} -->"
MARKER_RE = re.compile(r"<!-- context-updates-section (\S+) -->")


def source_counts(bundle: ContextUpdates) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for u in bundle.updates:
        counts[u.source] = counts.get(u.source, 0) + 1
    return counts


def counts_line(bundle: ContextUpdates) -> str:
    counts = source_counts(bundle)
    return ", ".join(f"{name} {n}" for name, n in sorted(counts.items())) or "nothing new"


def window_of(bundle: ContextUpdates) -> str:
    sinces = [r.since for r in bundle.reports if r.since]
    start = min(sinces).strftime("%Y-%m-%d %H:%M") if sinces else "the first look"
    return f"{start} -> {bundle.collected_at.strftime('%Y-%m-%d %H:%M')} UTC"


def render_section(bundle: ContextUpdates, consumer: str) -> str:
    """One section: heading (consumer, window, counts), then the items grouped by source, in full."""
    stamp = bundle.collected_at.isoformat()
    lines = [MARKER.format(stamp=stamp),
             f"## {consumer or 'unnamed consumer'}: {window_of(bundle)}",
             f"{len(bundle.updates)} item(s): {counts_line(bundle)}", ""]
    grouped: Dict[str, list] = {}
    for u in bundle.updates:
        grouped.setdefault(u.source, []).append(u)
    for source, rows in grouped.items():
        lines.append(f"### {source} ({len(rows)})")
        lines.append("")
        for u in rows:
            when = u.occurred_at.strftime("%Y-%m-%d %H:%M") if u.occurred_at else "undated"
            who = f", {u.author}" if u.author else ""
            lines.append(f"#### {u.title or u.kind or source} ({when}{who})")
            if u.url:
                lines.append(u.url)
            if u.body:
                lines.append("")
                lines.append(str(u.body).rstrip())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _split_sections(text: str) -> List[str]:
    parts = re.split(r"(?=<!-- context-updates-section )", text)
    return [p for p in parts if MARKER_RE.match(p)]


def write_context_file(bundle: ContextUpdates, folder: str, *, consumer: str = "",
                       now: Optional[datetime] = None) -> Optional[Path]:
    """Prepend this bundle's section to ``<folder>/CONTEXT_UPDATES.md``, trimming old sections.

    Returns the path, or None when there is nothing new, no folder, or the write failed (best-effort: a
    file that cannot be written must never fail a run, and the caller then keeps the inline rendering).
    """
    if not folder or not bundle.updates:
        return None
    now = now or _utcnow()
    path = Path(folder).expanduser() / FILE_NAME
    try:
        existing = path.read_text() if path.exists() else ""
        cutoff = now - timedelta(days=KEEP_DAYS)
        kept = []
        for section in _split_sections(existing):
            stamp = _as_utc(MARKER_RE.match(section).group(1))
            if stamp is None or stamp >= cutoff:
                kept.append(section.rstrip() + "\n")
        body = HEADER + "\n" + "\n".join([render_section(bundle, consumer)] + kept)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body)
        os.replace(tmp, path)
        return path
    except OSError as e:
        log.warning("could not write %s (%s)", path, e)
        return None


def index_block(bundle: ContextUpdates, path: Path, *, max_items: int = INDEX_ITEMS) -> str:
    """The short prompt block that replaces the inline rendering once the file is written.

    Keeps the block delimiters and ref-tagged manifest lines, so the usage receipt still works for what is
    listed. Slotted updates (reflection, captures) render in their own sections of the brief, so they are
    listed here too (the run is asked to account for them) but cost no room in the item budget.
    """
    slotted = [u for u in bundle.updates if u.slot]
    plain = [u for u in bundle.updates if not u.slot]
    shown = plain[:max_items]
    lines = [BLOCK_START, _OFFER_PREAMBLE, "",
             f"Everything new since you last looked is written in full in `{path}` "
             f"({len(bundle.updates)} item(s): {counts_line(bundle)}). Read the newest section of that file "
             f"for what matters to this work; do not treat every item as an assignment.", ""]
    for u in shown:
        lines.append(u.manifest_line())
    for u in slotted:
        lines.append(u.manifest_line() + "  (rendered in its own section of this brief)")
    if len(plain) > len(shown):
        lines.append(f"... and {len(plain) - len(shown)} more in the file.")
    lines.append(BLOCK_END)
    refs = [u.ref for u in shown + slotted if u.ref]
    lines.append("")
    lines.append(usage_receipt_gate(refs))
    return "\n".join(lines)
