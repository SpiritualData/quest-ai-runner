"""A reply that states a figure the turn never held is rewritten once by its writer, not cut (a step of the figure pass).

Deleting the sentence that holds the figure can leave a later sentence pointing at what was deleted ("that access" with nothing before it). So the writer is told which
figures to drop and to keep everything else and every reference; only when the rewrite still states one is the text cut, and a sentence whose reference lost its antecedent to
the cut goes with it (``reply_standards.drop_orphans``). The figures and the antecedents are found by comparing numbers and words with the turn's evidence and the pre-repair
text, never by reading what the reply means.
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple

from hubofhubs.api import strip_unsupported

from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import drop_orphans, steering


def without_unsupported(rs: Any, text: str, evidence: Any) -> Tuple[str, List[str]]:
    """``(the text, the figures that were found unsupported)``: the text unchanged when none was, else the writer's one rewrite when it is clean, else the cut text without orphaned sentences."""
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
            return (rewritten if not still else drop_orphans(rewritten, again, getattr(rs, "user_message", ""))), dropped
    return drop_orphans(text, cut, getattr(rs, "user_message", "")), dropped
