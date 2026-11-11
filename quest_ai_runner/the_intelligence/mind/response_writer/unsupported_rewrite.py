"""A reply that states a figure the turn never held is rewritten once by its writer, not cut (a step of the figure pass).

Deleting the sentence that holds the figure can leave a later sentence pointing at what was deleted ("that access" with nothing before it). So the writer is told which
figures to drop and to keep everything else and every reference; only when the rewrite still states one is the text cut, and a sentence whose reference lost its antecedent to
the cut goes with it (``reply_standards.drop_orphans``). The figures and the antecedents are found by comparing numbers and words with the turn's evidence and the pre-repair
text, never by reading what the reply means.
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Tuple

from hubofhubs.api import strip_unsupported

from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import drop_orphans, steering


def figure_evidence(rs: Any) -> str:
    """What the writer was shown, for the figure pass: the person's message, the transcript, the context the reply was written from and every read of the turn, uncut."""
    from quest_ai_runner.the_intelligence.mind.context_intake.planner_context import answer_evidence
    parts = [str(getattr(rs, "user_message", "") or ""), str(getattr(rs, "transcript", "") or ""), str(getattr(rs, "context_view", "") or "")]
    for item in answer_evidence(list(getattr(rs, "gathered", None) or [])):
        parts.append(str(item.get("tool_text") or item.get("text") or item.get("content") or json.dumps(item, default=str)) if isinstance(item, dict) else str(item))
    return "\n".join(parts)


EMPTY_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*\u2022])\s*(?:\*\*[^*]*\*\*\s*)?$")
ORDERED_ITEM = re.compile(r"^(\s*)\d+([.)])(\s+)")


def tidy_list(text: str) -> str:
    """``text`` with the list items a cut left without words removed ("1." or "- **Proposal, Thursday:**" alone) and each run of numbered items numbered
    from 1 again. Measured 2026-11-11: a week plan's first proposed goal held "15 minutes", the figure cut took its words, and the reply showed "1." empty
    above "2." and "3.". Pure line shape; no wording read."""
    lines, number = [], 0
    for line in text.split("\n"):
        if EMPTY_ITEM.match(line):
            continue
        item = ORDERED_ITEM.match(line)
        if item:
            number += 1
            line = f"{item.group(1)}{number}{item.group(2)}{item.group(3)}" + line[item.end():]
        elif line.strip():
            number = 0
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def without_unsupported(rs: Any, text: str, evidence: Any) -> Tuple[str, List[str]]:
    """``(the text, the figures that were found unsupported)``: the text unchanged when none was, else the writer's one rewrite when it is clean, else the cut text without orphaned sentences.
    A cut text is tidied (``tidy_list``) so no empty or misnumbered list item is left."""
    final, dropped = without_unsupported_untidied(rs, text, evidence)
    return (tidy_list(final) if dropped else final), dropped


def without_unsupported_untidied(rs: Any, text: str, evidence: Any) -> Tuple[str, List[str]]:
    cut, dropped = strip_unsupported(text, evidence)
    if not dropped:
        return text, []
    if all(re.search(rf"(?:^|\s){re.escape(str(d))}[.)]\s", text) for d in dropped):      # every "figure" is a list number ("1. Call"): not a claim, no rewrite is spent
        return cut, dropped
    write = getattr(rs, "_gen_answer", None)
    if callable(write):
        hint = ("remove only these figures, which are in nothing you were given: " + ", ".join(f"'{d}'" for d in dropped[:6])
                + ". Say you do not have the figure where it mattered. Keep every other sentence, its meaning and every reference between sentences, so no sentence points at something no longer said")
        try:
            rewritten = write(steering(hint, text)) or ""
        except Exception:  # noqa: BLE001 -- a rewrite never breaks the turn; the cut stands
            rewritten = ""
        if rewritten.strip():
            again, still = strip_unsupported(rewritten, evidence)
            final = rewritten if not still else drop_orphans(rewritten, again, getattr(rs, "user_message", ""))
            # The writer rewrites from the turn's context, which may only hold the pre-work proposal; the rewrite is the LAST text
            # before the reader, so it is held to the turn's executed-action account like every earlier rewrite.
            keep = getattr(rs, "keep_landed", None)
            if callable(keep):
                final = keep(final)
            return final, dropped
    return drop_orphans(text, cut, getattr(rs, "user_message", "")), dropped
