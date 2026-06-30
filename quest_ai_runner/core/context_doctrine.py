"""context_doctrine -- centralized prompt gates for the sufficiency and model-tier disciplines.

These constants are used in two places:
  1. ``core/orchestrator.py`` -- woven into ``PLANNER_PROMPT`` at module load (the orchestrator
     always applies them).
  2. Exported via ``compose_deep_preamble`` for a deep-runner consumer that wants to prepend the
     same doctrine to its own ``context_preamble``, so every agent in the chain acts the same way.

All text is generic: no org names, no app names, no em dashes.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# SUFFICIENCY GATE -- proceed vs explore checklist.
# Not a vibe: a checkable list. Exported so the orchestrator and deep runners share the same text.
# ---------------------------------------------------------------------------
SUFFICIENCY_GATE: str = """\
SUFFICIENCY (read enough before acting):
  Before choosing "answer" or "deep", run this checklist:
  1. Can I NAME and have I READ (not just located) each file I will change or reference?
  2. Are the applicable conventions in front of me (not inferred from memory)?
  3. For "does X handle Y?" -- can I TRACE the real code path, or am I guessing?
  4. Is there a verification I can run to confirm the change is correct?
  If any answer is NO, issue another "read" step. Explore in CHEAP PASSES: grep to locate,
  read the matching section, re-plan with what you saw. Stop on the CONTEXT-DRY SIGNAL: a pass
  that adds no new load-bearing file or fact. At that point, escalate the MODEL tier rather
  than looping again on the same context.\
"""

# ---------------------------------------------------------------------------
# SPECIFICITY GATE -- answer about the EXACT subject asked, not its category.
# The primary defense against grounding an answer in a sibling topic that merely shares a
# category word with the request (e.g. a question about one "evaluation" answered from a
# different "evaluation"'s docs). Relevance/specificity is primary; recency is only a backup
# tie-break, never an override. Woven into the planner prompt and the deep doctrine, and
# enforced again at the answer/grounding layer (see orchestrator._grounding_block).
# ---------------------------------------------------------------------------
SPECIFICITY_GATE: str = """\
SPECIFICITY (answer about the SPECIFIC subject asked, not its category):
  Retrieval ranks by similarity, so material about a SIBLING topic in the same category routinely
  surfaces alongside (or instead of) the real subject. Sharing a category word is NOT a match.
  1. Pin the SPECIFIC referent in the request: the named subject, initiative, task, file, or
     metric -- not just its type. "X evaluation" is a DIFFERENT subject from "Y evaluation" even
     though both are evaluations; "the result-prediction pipeline" is not "the atom pipeline".
  2. Before grounding an answer in any located or retrieved item, confirm it is about THAT SAME
     referent, not merely the same kind of thing. A title, path, or snippet that overlaps on a
     category word but is about a different specific subject does NOT qualify.
  3. If the only material you have is about a DIFFERENT specific referent (a sibling in the same
     category), do NOT answer as if it were on point and do NOT silently switch subjects. Either
     issue another "read" to gather material about the ACTUAL referent, or say plainly you have
     nothing specifically about what was asked, name what you DID find, and ask which they meant.
  4. Recency is only a BACKUP tie-break: when two items are equally on-subject, prefer the more
     recent, and when you state a current status or "what's next" note the date of what you used
     instead of presenting an old document as the present. Recency NEVER overrides specificity: a
     recent sibling-topic doc is still the wrong subject; an older on-subject doc is still right.\
"""

# ---------------------------------------------------------------------------
# MODEL TIER GATE -- cheap by default, escalate one tier on failure.
# ---------------------------------------------------------------------------
MODEL_TIER_GATE: str = """\
MODEL TIER DISCIPLINE:
  Haiku  -- find and gather (grep, locate, discovery reads).
  Sonnet -- clear implementation (most answers and straightforward deep runs).
  Opus   -- review, quality checks, ambiguous requirements, irreversible changes.
  Rule: ESCALATE ONE TIER ON A FAILED VERIFICATION rather than re-running identically on the
  same model. Re-running the same model on the same context produces the same failure.\
"""

# ---------------------------------------------------------------------------
# CACHED HINT GATE -- how to use a pre-assembled context hint so it is never a cost.
# A cached hint must be a cheap starting point, not ground truth: use it when it fits,
# discard it cheaply when it does not. This is what keeps the context layer Pareto-better
# (a wrong or stale hint costs a glance then a normal search, never expensive verification),
# so adding context never makes a run worse than running with none.
# ---------------------------------------------------------------------------
CACHED_HINT_GATE: str = """\
USING A CACHED CONTEXT HINT:
  Any cached or pre-loaded file hint is a CHEAP STARTING POINT, not ground truth.
  If it obviously fits the task, use it and confirm with ONE quick read, then proceed.
  If it does NOT obviously fit, DISCARD it immediately and search normally as if it were not
  there. Do NOT spend extra effort verifying, arguing with, or working around a hint that does
  not fit: that is slower than just searching. A hint marked stale or changed must be re-read
  before you rely on it. The hint can only save you work, it must never cost you work.\
"""

# ---------------------------------------------------------------------------
# DEEP_CONTEXT_DOCTRINE -- compact block combining the gates.
# Suitable to prepend to a deep runner's context_preamble so deep agents act the same way.
# ---------------------------------------------------------------------------
DEEP_CONTEXT_DOCTRINE: str = (
    "=== CONTEXT DOCTRINE (applies to this run) ===\n\n"
    + SUFFICIENCY_GATE
    + "\n\n"
    + SPECIFICITY_GATE
    + "\n\n"
    + MODEL_TIER_GATE
    + "\n\n"
    + CACHED_HINT_GATE
    + "\n\n=== END DOCTRINE ===\n"
)


def compose_deep_preamble(base_preamble: str, assembled: "object | None" = None) -> str:
    """Return DEEP_CONTEXT_DOCTRINE + base_preamble + assembled.context_view (if present).

    Intended for a deep-runner consumer that wants to ensure its spawned agent obeys the same
    sufficiency and model-tier disciplines as the orchestrator. Call it to build the
    ``context_preamble`` arg passed to the deep runner.

    ``assembled`` is typed as ``object | None`` (not ``AssembledContext``) to avoid an import
    cycle: this module must not import from ``adapters`` (which in turn imports nothing from here).
    The function only accesses ``assembled.context_view`` via ``getattr``, so any object with
    that attribute works.

    Args:
        base_preamble: the consumer's base preamble text (may be empty).
        assembled:     an ``AssembledContext`` instance, or None.  When present, its
                       ``context_view`` (if non-empty) is appended after the base preamble.

    Returns:
        A single string: doctrine block + base preamble + (optional) assembled context view.
    """
    parts = [DEEP_CONTEXT_DOCTRINE]
    if base_preamble:
        parts.append(base_preamble)
    if assembled is not None:
        cv = getattr(assembled, "context_view", "") or ""
        if cv:
            parts.append("\n--- PRE-ASSEMBLED CONTEXT ---\n" + cv)
    return "\n".join(parts)
