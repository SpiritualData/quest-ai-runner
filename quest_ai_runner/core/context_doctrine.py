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
# CARD-THREAD GATE -- per-idea threading, where the IDEA IS THE CARD (see core/card_thread.py).
# Injected into the planner prompt ONLY when the consumer opted in (cfg.card_thread_enabled), and
# it teaches the ONE field the planner emits. The candidate list under it is a PRIOR: it narrows
# and surfaces, it never decides. Judgment decides.
#
# The continue-vs-new call is graded by an INDEPENDENT-RECALL test (would this still be worth
# looking up if the current card had never existed), not a same-subject/different-subject vibe:
# a plan's sub-decisions (pricing, timeline, a vendor pick) routinely SOUND like their own subject
# without BEING independently recallable, and that mismatch is what used to misfile them.
#
# NOT handled here, intentionally: "graduation" (a sub-topic that keeps recurring across several
# cards eventually earning its own card) is a cross-turn mechanism and out of scope for this
# per-turn gate.
# ---------------------------------------------------------------------------
CARD_THREAD_GATE: str = """\
TOPIC (`card_thread`, REQUIRED on every plan): which topic is THIS message about?
  Every message belongs to some TOPIC, and a topic is a CARD. Cards outlive conversations, so "the
  same idea" means "the same card", wherever it was discussed. Name the subject this message is
  actually about, then emit exactly one of:
    * "continue"            -- it is about the CURRENT TOPIC: a follow-up, a refinement, a question
                               about what you just said, a correction.
    * "switch_to:<card_id>" -- it is about one of the KNOWN TOPICS listed below. Use the id EXACTLY
                               as given: this is how "back to the launch plan" resolves, and how a
                               question about a specific piece of work reaches that work's own topic.
    * "new:<short label>"   -- it is a genuinely different subject that is NOT on the list. Label it
                               the way a person would name the idea, in 2 to 5 words.

  THE TEST (use this, not a guess about shared or different words): if the CURRENT card were deleted
  tomorrow, would anyone still want to look THIS up on its own, unconnected to what the current card
  was for?
    * NO  -- it is a sub-decision that only exists in service of the current effort (a price, a
      timeline, a vendor, a channel). It stays on the current card ("continue"), no matter how
      different or its own-subject the surface words sound.
    * YES -- it is a distinct, standalone thing with its own future: worth recalling even if the
      current card had never mattered. Give it its own card: "switch_to:<card_id>" if it is one of
      the KNOWN TOPICS below, "new:<short label>" otherwise.

  Examples:
    * "what should we charge for the launch?" (mid launch planning) -> continue: pricing here only
      ever gets looked up in service of "how did we launch this".
    * "actually let's delay the launch a week" -> continue: a correction to the same effort.
    * "how's Sarah's onboarding going" (asked mid launch planning) -> new: a different person with
      her own independent trajectory, one that would be asked about even if the launch never
      happened.
    * "separately, can you check on the Q3 budget" -> new: an explicit signpost, and the budget's
      standing is unconnected to whether the launch succeeds.
    * "thanks, that's helpful" -> stays on the general/current topic: chit-chat never opens one.

  DO NOT STAY ON A TOPIC JUST BECAUSE IT IS THE CURRENT ONE. Filing an exchange under the wrong idea
  buries it in another idea's history, and it stays buried. When the user signposts a change
  ("separately", "different question", "unrelated", "on another note", "back to ..."), believe them:
  the subject has changed, even when the words resemble what came before.
  CHIT-CHAT: greetings, thanks, "how are you", small talk with no subject of its own belong on the
  general topic when one is listed (switch to it). Never open a new topic for those.
  A topic assignment is NOT a mode change and NOT an action: it never suppresses, triggers, or
  approves any work, and it says nothing about how the conversation should run.
  Only when the test above is genuinely a toss-up, prefer "continue".\
"""

# ---------------------------------------------------------------------------
# CARD LIFECYCLE GATE -- a card outlives the work it describes.
# Injected with the card-thread gate. The failure this exists to stop: a completed piece of work
# resurfaces as context and the assistant starts proposing how to do it, as if it were still open.
# ---------------------------------------------------------------------------
CARD_LIFECYCLE_GATE: str = """\
FINISHED WORK IS KNOWLEDGE, NOT A TO DO:
  A topic card outlives the work it describes. When a card or its context says the work is
  completed or archived, treat it as KNOWLEDGE: you may discuss it, cite what happened, draw
  lessons from it, and build new work on top of it. Do NOT plan it, propose next steps for it,
  offer to pick it up, or speak about it as if it were in progress. The one exception is the user
  explicitly reopening it; until they do, past work is history, not a plan.\
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
