"""Orchestrator loop constants: log, defaults, standing notes and the work-landed checks.

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here.
"""
from __future__ import annotations

import logging
from typing import Any


RECIPE_SKIP_GOAL_CONTEXT = "recipe_skip_goal_context"   # ctx_meta key set on a deep-recipe turn


# Log labels for where a deep-worker ladder came from (see Orchestrator.log_deep_ladder).
CONFIGURED_LADDER_SOURCE = "configured deep_model_ladder (e.g. QAR_DEEP_MODELS)"


FALLBACK_LADDER_SOURCE = "fallback model + Claude-runnable tier resolution"


log = logging.getLogger("quest-ai-runner.orchestrator")


# What a deep turn says when there is NO execution capability wired at all. This is the turn's real
# answer in that case: the request was understood and broken into goals, and then nothing ran. It
# is a module constant so every renderer and every test names the same sentence rather than each
# inventing (or omitting) its own, and so the honest wording lives on the result instead of in one
# UI's side note. Kept short and free of em dashes, per the copy conventions the UIs follow.
NO_DEEP_EXECUTOR_TEXT = (
    "I did not execute this. No deep executor is configured for this session, so the work was "
    "planned but nothing actually ran: no files were changed and no commands were executed. "
    "Install Claude Code (or set QAR_CLAUDE_PATH to its binary) so the default executor can be "
    "wired, or pass RunnerConfig.deep_runner explicitly, then ask again."
)


# Said once a fanned-out turn has a subgoal PARKED on a human decision (``DeepResult.decision_id``
# set): the rest of that turn's subgoals do not also surface, so the person sees exactly one ask,
# not a pile of them. Code-written, never derived from any result's own text (hard rule #3: a
# control-flow/reply decision is never gated on a model's words), and kept free of em dashes per
# the copy conventions every QAR-rendered sentence follows.
CONTINUE_AFTER_DECISION_NOTE = (
    "I will continue with the rest of this once you answer that."
)


def result_reports_something(result: Any) -> bool:
    """Whether a deep result has anything of its own to tell the person: text it produced, a
    verified outcome, or a usage-limit wait.

    Used where a fan-out's results are trimmed around a single ask (below): a sibling subgoal that
    was stopped before it ever ran leaves an EMPTY placeholder result, which must not become a
    blank paragraph in the reply, while a sibling that genuinely finished its work must survive.
    Judged on structure and emptiness only, never on what any wording says. Never raises."""
    try:
        if (getattr(result, "output", "") or "").strip():
            return True
        return bool(getattr(result, "met", False) or getattr(result, "usage_limited", False))
    except Exception:  # noqa: BLE001
        return False


def result_landed_work(result: Any) -> bool:
    """Whether a deep result is work that really LANDED, judged only on its structured fields.

    The run's OWN RECORDS decide: not parked on a decision (``decision_id``); its write receipts do
    not say it changed nothing (``changed_nothing``); and, when the runner records what it observed
    (``observations_reported``), a run that was NOT verified met either observed something, or is
    the runner's own TERMINAL ASK for this turn (``exhausted``, see below). "Not parked" alone is
    NOT "landed": a sibling that failed, or could not be verified and has no receipt of any effect,
    folded into a "you already did the work" synthesis is how a failure was reported as done. A
    verified text answer with nothing to record still counts (its answer is the work), and a runner
    that keeps no receipts at all (``observations_reported`` False) is judged as before -- this is
    deliberately narrower than "exhausted and not met" alone would be: the orchestrator's OWN goal
    loop also sets ``exhausted`` on a result whose verification merely could not run (no receipts
    ever reported), and that is genuinely finished work the test ``observations_reported`` False
    case covers. Never reads wording (hard rule #3).

    ``observations_reported`` and ``exhausted`` TOGETHER (round-2 regression, MS-046 L10c): a
    receipt-tracking runner can end a turn with its own honest terminal message -- "which quest did
    you mean?", "I could not reliably turn that into a change", a spent code-generation budget --
    WITHOUT a ``decision_id`` (that field is only for an approval-card park; a clarifying question
    is a different kind of ask). Such a result may still carry real, non-empty ``observations`` (a
    reconnaissance read genuinely found data before the runner gave up on WHICH quest to write it
    to), so the plain "observed nothing" check below does not catch it. Live trace: a tempo-run pace
    was computed correctly, the write's quest was ambiguous, the runner asked "which quest should
    this go on?" (met=False, exhausted=True, real observations), and folding that into the "you
    already DID the work" synthesis produced "I have queued the creation of a tempo-run goal for
    that day" -- nothing was queued, parked, or written anywhere.

    ``has_write_receipt`` is the one exception to "exhausted and not met means not landed"
    (BLOCKER fix 2026-10-07): a runner can end a turn ``exhausted`` and not ``met`` while a
    GENUINE PARTIAL WRITE already landed (e.g. three of five updates completed, then the fourth
    raised and the runner stopped rather than risk a duplicate retry). Dropping that receipt
    reported real work as not done. The flag is read, never the wording of ``observations`` or
    ``output`` -- a runner that cannot tell leaves it False, so this stays exactly the blanket
    "exhausted and not met -> not landed" rule for every runner that does not set it (including
    the ambiguous-quest clarify question above, which never sets it).

    Structural throughout, like every other branch here: driven by
    ``observations_reported``/``exhausted``/``met``/``has_write_receipt``, never by scanning the
    output text for "queued"/"asking"/"wrote"/etc. Never raises."""
    try:
        if getattr(result, "decision_id", None) or getattr(result, "changed_nothing", False):
            return False
        if getattr(result, "observations_reported", False) and not getattr(result, "met", False):
            if not (getattr(result, "observations", None) or []):
                return False
            if getattr(result, "exhausted", False) and not getattr(result, "has_write_receipt", False):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


# Named in the reply for every fan-out sibling that never got to run because another subgoal of the
# same turn parked on a human decision first: silently dropping it made the turn read as if that
# part were handled. Code-written (the goal text is the plan's own), no em dash.
NOT_STARTED_AFTER_DECISION_NOTE = "Not done yet, waiting on your answer above: {goals}."


# Defaults (all overridable via OrchestratorConfig). The elapsed/chars budget bounds the WHOLE
# read cascade for a turn (every grep + read this turn shares it), not a single read: a 60s/60000
# char cap left a simple "check these 3 named files" request no room left after one broad grep,
# so the turn gave up mid-cascade and reported a false "couldn't check X" instead of ever reaching
# the planner's next read step. Widened so a normal few-file lookup completes within budget.
DEFAULT_MAX_STEPS = 15


DEFAULT_MAX_ELAPSED_SECONDS = 90.0


DEFAULT_MAX_GATHERED_CHARS = 150000


DEFAULT_MAX_READS_PER_STEP = 8


DEFAULT_MAX_PARALLEL = 8


DEFAULT_MAX_SUBQUESTIONS = 4


DEFAULT_MAX_DEEP_SUBTASKS = 4


DEFAULT_DEEP_MAX_TURNS = 30


# How far a CONTINUATION may grow one attempt's turn budget (see the goal loop's limit_hit branch).
# Each continuation gets ``deep_max_turns * (n + 1)`` turns, capped here: a task that needed a bit
# more room gets it quickly, while a worker that is genuinely going in circles cannot talk its way
# into an unbounded run one continuation at a time.
DEEP_CONTINUATION_TURN_MULTIPLIER_CAP = 4


DEFAULT_MAX_GATHER_CHARS = 6000


DEFAULT_MAX_CONSECUTIVE_READS = 20


# Lean re-plan view: the planner is re-fed the WHOLE cumulative ``gathered`` each step, which
# bloats fast on multi-read runs. Instead, keep the most-recent observations in full and COMPRESS
# older ones to one-line summaries (path/source + key finding) — but only once ``gathered`` has
# grown past a threshold, so short runs are byte-for-byte unchanged. The full ``gathered`` is still
# used verbatim for the final ANSWER synthesis; only the per-step PLANNER view is leaned out.
DEFAULT_PLANNER_RECENT_FULL = 4      # newest N observations rendered in full to the planner


# Hard character caps on the three volatile sections of the planner prompt (0 disables a cap). The
# planner only routes, so it needs the latest turns and the head of the context, not all of it; the
# final ANSWER path keeps the full transcript and context. See ``planner_context.cap_section``.
DEFAULT_PLANNER_TRANSCRIPT_MAX_CHARS = 8_000
DEFAULT_PLANNER_CONTEXT_MAX_CHARS = 8_000
DEFAULT_PLANNER_GATHERED_MAX_CHARS = 10_000
DEFAULT_PLANNER_COMPRESS_OVER = 6    # leave gathered untouched until it exceeds this many obs


# Cross-step repeat-context leaning: within a SINGLE run the transcript and the static
# context_view never change between steps, yet the prior wave re-sent BOTH in full to the planner
# on every re-plan step. On re-plan steps the planner's job is to REACT to the new ``gathered``
# observations, not to re-read context it already saw on step 1. When enabled, steps after the
# first replace the unchanged transcript + context_view with a short "already provided on step 1"
# reference. This NEVER affects step 1 (the planner still sees both in full) and NEVER affects the
# final ANSWER (which always gets the full transcript + context_view).
#
# Default ON (2026-10-06, token-usage pass): the static context_view alone (vector hits + cards +
# earlier conversations) measured about 5,200 tokens on a real planner call, and with ~2.4 planner
# calls per turn, re-sending that unchanged block on every re-plan step was most of the cost for no
# benefit the planner could use -- it does not change within a turn. Flip back to False only if a
# consumer finds a planner that genuinely needs the raw context on a re-plan step; the final answer
# is unaffected either way (see ``test_repeat_context_on_answer_still_gets_full_context``).
DEFAULT_PLANNER_ABBREVIATE_REPEAT_CONTEXT = True


# CARD MERGE (semantic dedup). When the post-deep card updater would CREATE a NEW card, it first asks
# the vector-backed card store whether a sufficiently-similar card already exists for THIS user (by
# embedding COSINE similarity) and, if so, UPDATES that card instead of creating a near-duplicate
# twin. This default is the COSINE floor a candidate must clear to count as the "same" card: HIGH on
# purpose, so only a CLEAR twin merges and an unrelated card is never collapsed. A value of 1.0
# effectively DISABLES the behavior (cosine ~never reaches a clean 1.0 except an identical card), as
# does a card store with no embeddings (the keyword-only FileContextStore exposes no
# ``find_similar_card``, so the updater silently degrades to create-as-before). Override via
# ``OrchestratorConfig.card_merge_similarity``.
DEFAULT_CARD_MERGE_SIMILARITY = 0.85
