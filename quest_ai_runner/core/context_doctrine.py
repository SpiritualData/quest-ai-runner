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
# DEEP_CONTEXT_DOCTRINE -- compact block combining both gates.
# Suitable to prepend to a deep runner's context_preamble so deep agents act the same way.
# ---------------------------------------------------------------------------
DEEP_CONTEXT_DOCTRINE: str = (
    "=== CONTEXT DOCTRINE (applies to this run) ===\n\n"
    + SUFFICIENCY_GATE
    + "\n\n"
    + MODEL_TIER_GATE
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
