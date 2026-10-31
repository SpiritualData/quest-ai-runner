"""Orchestrator loop constants: the log, the standing notes QAR says in its own words, and the hub's bounds and checks.

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here. The default bounds and the two
structural checks on a deep result (``result_reports_something``, ``result_landed_work``) are the hub's
(``hubofhubs.orchestrate.limits``); the sentences below are QAR's own copy and stay here.
"""
from __future__ import annotations

import logging

from hubofhubs.orchestrate.limits import (  # noqa: F401
    AUTO_FOLLOWUP_ASK_MAX_CHARS,
    AUTO_FOLLOWUP_DONE_MAX_CHARS,
    AUTO_FOLLOWUP_MAX_RUNS,
    AUTO_FOLLOWUP_REMAINING_MAX_CHARS,
    AUTO_FOLLOWUP_TOKEN_BUDGET,
    DEEP_CONTINUATION_TURN_MULTIPLIER_CAP,
    DEFAULT_CARD_MERGE_SIMILARITY,
    DEFAULT_DEEP_MAX_TURNS,
    DEFAULT_MAX_CONSECUTIVE_READS,
    DEFAULT_MAX_DEEP_SUBTASKS,
    DEFAULT_MAX_ELAPSED_SECONDS,
    DEFAULT_MAX_GATHER_CHARS,
    DEFAULT_MAX_GATHERED_CHARS,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_MAX_PLAN_STAGES,
    DEFAULT_MAX_PLAN_TASKS,
    DEFAULT_MAX_READS_PER_STEP,
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_SUBQUESTIONS,
    DEFAULT_PLANNER_ABBREVIATE_REPEAT_CONTEXT,
    DEFAULT_PLANNER_COMPRESS_OVER,
    DEFAULT_PLANNER_CONTEXT_MAX_CHARS,
    DEFAULT_PLANNER_GATHERED_MAX_CHARS,
    DEFAULT_PLANNER_RECENT_FULL,
    DEFAULT_PLANNER_TRANSCRIPT_MAX_CHARS,
    PENDING_READ_MAX_RUNS,
    result_landed_work,
    result_reports_something,
)


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


# Named in the reply for every fan-out sibling that never got to run because another subgoal of the
# same turn parked on a human decision first: silently dropping it made the turn read as if that
# part were handled. Code-written (the goal text is the plan's own), no em dash.
NOT_STARTED_AFTER_DECISION_NOTE = "Not done yet, waiting on your answer above: {goals}."
