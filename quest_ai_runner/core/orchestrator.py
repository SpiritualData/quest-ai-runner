"""Orchestrator brain — the BOUNDED ITERATIVE LOOP (plan -> gather -> re-plan -> answer/deep/confirm).

A generic orchestration loop, with no org/Quest/DB specifics baked in. The brain runs a
bounded loop per request: a fast PLANNER (one cheap structured
model call) AUTO-DECIDES the next step, given the message + everything gathered so far. The loop
lets the brain narrow in like Claude Code does: grep -> read the matching section -> re-plan ->
answer. The planner returns one of four actions each step:

  * read    — TARGETED partial reads/greps via the RetrievalAdapter (batched, run CONCURRENTLY).
              Appended to ``gathered``; the loop RE-PLANS with what it just saw.
  * answer  — reply now, grounded in ``gathered``. May fan out 2-N independent sub-questions,
              answered CONCURRENTLY then synthesized.
  * deep    — needs real work: author a concrete ``goal`` (checkable done-standard) + brief and
              hand to the DeepRunner. May fan out 2-N independent deep subtasks CONCURRENTLY.
  * confirm — a human-only / risky step: raise it via the EscalationSink and stop.

BOUNDING: capped at ``max_steps`` plus a wall-clock + gathered-size budget. If it hits the cap
without answering, it makes a best-effort grounded answer from ``gathered``, or escalates to a
deep run if nothing useful was gathered.

It is **domain-free**: it takes ADAPTERS (a RetrievalAdapter, a ModelProvider, a ModelRegistry,
and optional DeepRunner + EscalationSink), never hardcoded paths. The result is a structured
``OrchestratorResult`` the caller (in-process chat, or the runner's executor) turns into a chat
message, a deep run, or a Quest decision-request.
"""
from __future__ import annotations

import copy
import inspect
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters import (
    EVENT_CARD_THREAD,
    EVENT_CONTEXT,
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_EXEC,
    EVENT_MILESTONE,
    EVENT_MODE_SIGNAL,
    EVENT_OVERSEER,
    EVENT_PARTIAL,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    EVENT_TOKENS,
    EVENT_UNDERSTANDING,
    FUTURE_CONTEXT_VIA_FIELD,
    FUTURE_CONTEXT_VIA_OUTPUT,
    ContextAssembler,
    ConversationStore,
    DeepResult,
    DeepRunner,
    Escalation,
    EscalationSink,
    GuidanceProvider,
    Mode,
    ModelProvider,
    Observation,
    PlanDecision,
    ProgressEvent,
    ProgressSink,
    RetrievalAdapter,
)
from .card_filter import _extract_json
from .card_thread import (
    CardCandidate,
    CardThreadContext,
    CardThreadDecision,
    merge_candidates,
    parse_card_thread,
    render_thread_hint,
)
from .context_doctrine import (
    CACHED_HINT_GATE,
    CARD_LIFECYCLE_GATE,
    CARD_THREAD_GATE,
    MODEL_TIER_GATE,
    SPECIFICITY_GATE,
    SUFFICIENCY_GATE,
)
from .inbox import InputInbox
from .guard import (
    ExecutionFact,
    ExecutionRecord,
    classify_exec_phase,
)
from .model_registry import TIERS, ModelRegistry
from .overseer import OverseerSignal, build_digest, oversee
from .prompt_layers import PromptLayers, compose_layers, language_instruction, turn_prompt_head
from .recent_context import (
    GLOBAL_SCOPE_KEY,
    RecentContextStore,
    build_item_usage_hint,
    conv_scope_key,
    filter_relevant,
    quest_scope_key,
    render_recent_cards,
)

log = logging.getLogger("quest-ai-runner.orchestrator")

# Defaults (all overridable via OrchestratorConfig).
DEFAULT_MAX_STEPS = 15
DEFAULT_MAX_ELAPSED_SECONDS = 60.0
DEFAULT_MAX_GATHERED_CHARS = 60000
DEFAULT_MAX_READS_PER_STEP = 8
DEFAULT_MAX_PARALLEL = 8
DEFAULT_MAX_SUBQUESTIONS = 4
DEFAULT_MAX_DEEP_SUBTASKS = 4
DEFAULT_DEEP_MAX_TURNS = 30
DEFAULT_MAX_GATHER_CHARS = 6000
DEFAULT_MAX_CONSECUTIVE_READS = 20
# Lean re-plan view: the planner is re-fed the WHOLE cumulative ``gathered`` each step, which
# bloats fast on multi-read runs. Instead, keep the most-recent observations in full and COMPRESS
# older ones to one-line summaries (path/source + key finding) — but only once ``gathered`` has
# grown past a threshold, so short runs are byte-for-byte unchanged. The full ``gathered`` is still
# used verbatim for the final ANSWER synthesis; only the per-step PLANNER view is leaned out.
DEFAULT_PLANNER_RECENT_FULL = 4      # newest N observations rendered in full to the planner
DEFAULT_PLANNER_COMPRESS_OVER = 6    # leave gathered untouched until it exceeds this many obs
# Cross-step repeat-context leaning: within a SINGLE run the transcript and the static
# context_view never change between steps, yet the prior wave re-sent BOTH in full to the planner
# on every re-plan step. On re-plan steps the planner's job is to REACT to the new ``gathered``
# observations, not to re-read context it already saw on step 1. When enabled, steps after the
# first replace the unchanged transcript + context_view with a short "already provided on step 1"
# reference. This NEVER affects step 1 (the planner still sees both in full) and NEVER affects the
# final ANSWER (which always gets the full transcript + context_view). Default off → byte-for-byte
# current behavior unless a consumer opts in.
DEFAULT_PLANNER_ABBREVIATE_REPEAT_CONTEXT = False
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


# ===========================================================================
# THE PLANNER PROMPT — the single tunable that governs how the brain decides.
# Rendered with .format(...). Generic: no org names, no app names.
#
# Built at module load from three named parts so the doctrine gates from context_doctrine
# (SUFFICIENCY_GATE, MODEL_TIER_GATE) can be woven in WITHOUT brace-escaping issues.
# Those constants contain NO literal {/} characters, so they pass through .format() untouched
# when the final assembled string is .format()-ed in _plan(). Only the real format slots
# ({user_message}, {transcript}, {context_view}, {gathered}, {max_reads}, {max_subq},
# {max_deep}) are substituted; JSON-example braces use the standard {{...}} double-brace form.
# ===========================================================================

_PLANNER_HEAD = """\
You are the PLANNER for an AI assistant answering a request.

Your job: decide, FAST, the NEXT step to respond WELL. You do NOT write the reply yourself.
Choose exactly one action via the `decide` tool. You run in a LOOP: after a "read" you'll be
called again with what was read, so you can narrow in -- grep to locate, read the matching
section, then answer -- exactly like a careful human reading the real source.

QUESTION vs COMMAND -- DECIDE THIS FIRST, BEFORE ANYTHING ELSE:
  Read the user's message and judge what they actually want from you:
    * A QUESTION / REQUEST FOR INFORMATION -- they want to be TOLD, SHOWN, or ADVISED
      something: an explanation, a status, a summary, a comparison, your opinion, or a
      "what would it take / how would I / should we ..." Answer it ("answer", after a
      "read" when it's about substance). DO NOT execute work or open a task for a question,
      EVEN WHEN it mentions action words like "fix", "add", "change", "build", "update",
      "show". "How does the back button work?", "What would it take to add SSO?", "Should
      we refactor this?", "Why is the build failing?", "Can you explain how X works?" are
      all QUESTIONS -- answer them, do not do the work.
    * A COMMAND / DIRECTIVE -- they are telling you to GO DO the work NOW: "fix the back
      button", "add a field to the form", "update my goal", "build the endpoint", and the
      polite-imperative forms aimed at you, "can you fix ...", "please add ...". THIS is the
      kind of request that becomes "deep".
  The test is INTENT, not keywords -- the SAME verb appears in both a question and a command.
  An interrogative opener ("how / what / why / which / should we / would it / is it / do you
  ...") or a message that asks ABOUT something and ends in "?" is a QUESTION -> answer. A
  plain imperative, or a polite imperative directed at you ("can you ...", "please ..."), is
  a COMMAND -> deep. When you are genuinely torn, ANSWER (and you may offer to do the work) --
  never silently turn a question into a task.

CODE / FILE CHANGE COMMANDS (once you've judged it's a COMMAND, this is highest priority):
  If the user is DIRECTING a change to code or files (fix bug, implement feature, refactor,
  edit/apply a file, expand/collapse/toggle/show/hide a UI element, etc.) -- NOT merely asking
  about one -- choose action="deep" IMMEDIATELY.
  Do NOT read first. The deep runner is a full coding agent -- it explores and edits itself.
  Describing a fix instead of executing is a FAILURE. (But explaining a fix when the user only
  ASKED how it works is correct -- that was a question, not a command.)

  WHEN SEARCHES RETURN NOTHING (code/file tasks only): if you searched/grepped for a component,
  file, or symbol for a COMMAND/change request and got no results, that is NOT a reason to
  answer with generic advice. Choose "deep" -- the deep runner can grep and browse the codebase.
  Never give a 'here is how you would implement this' guide when the user asked you to do it.
  EXCEPTION: for QUESTION / INFORMATION requests (recall, status, history, "what did we find
  about X", "tell me recent findings"), if searches return nothing that IS a valid answer --
  respond with "searched but found nothing relevant" rather than spawning a deep task. Do NOT
  escalate informational questions to deep just because the search returned empty.

CORE PRINCIPLE -- READ REAL CONTENT BEFORE ANSWERING:
  The CONTEXT below only LOCATES what exists (a one-line summary per item). It is NOT a
  substitute for reading the actual content. For ANY question about substance -- what a doc says,
  status, numbers, decisions, how something works -- READ the real content first (action "read"),
  THEN answer grounded in it. Only pure chit-chat/meta ("you there?", "thanks") may be answered
  WITHOUT reading.

  BUT FIRST -- ANSWER FROM THE CONVERSATION WHEN IT'S ALREADY THERE: before you choose "read",
  check the RECENT TRANSCRIPT and GATHERED. If the answer is ALREADY present there, answer straight
  from it -- do NOT re-search the corpus for something this conversation just established. This is
  the common case for a DIRECT FOLLOW-UP about what you JUST said: you described a plan, a file, a
  number, a name, or a decision in the prior turn and the user now asks about that same thing
  ("what's the filepath?", "where is it?", "which one?", "what was that number?", "say that again").
  The prior turn holds the answer -- give it. Only "read" when the current message genuinely needs
  substance the transcript/GATHERED does NOT already contain (a new topic, deeper detail you never
  pulled, or verification the prior turn explicitly left open). When in doubt between re-reading and
  answering from a clear prior statement, ANSWER -- re-grepping for a fact you just stated wastes the
  user's time.

  CRITICAL: Do NOT answer with "I need to X" or "I should X" or "To fix this, I need to...".
  These are NOT answers -- they are unexecuted tasks. If you realize work needs doing, choose
  "deep" immediately and let the runner do it. NEVER describe work in an answer; ALWAYS execute it.
  NEVER say "if you provide the file name I can help" -- find the file yourself via "deep".

  These verbs often signal a code-change task: "fix", "bug", "break", "implement", "build",
  "refactor", "edit", "update", "change", "add", "remove", "delete", "rewrite", "apply", "make",
  "expand", "collapse", "toggle", "show", "hide", "open", "close", "display", "render". When the
  user DIRECTS such a change (a command, per the QUESTION vs COMMAND gate above), escalate to
  "deep" immediately -- do NOT answer about what you think the fix should be. But when the user
  is only ASKING about it ("how would I ...", "what would it take to ...", "why does ... break?"),
  that is a question -- ANSWER it; the same verb in a question is not a command to act.

  If you have already read and gathered context, and now realize execution is needed: choose
  action="answer" WITH deferred_deep. The answer can acknowledge what was found, but deferred_deep
  must specify the work to execute. {deferred_deep_semantics}
"""

# The deferred_deep semantics sentence injected into the planner doctrine above. Which one a turn
# gets is decided by ``OrchestratorConfig.deferred_deep_queued`` so the words always match the
# ACTUAL behavior of the wired deep runner:
#   * INLINE (default): the deferred work runs synchronously right after the answer, in the same
#     turn (today's behavior with an inline deep runner).
#   * QUEUED: the consumer wired a deferred deep runner that hands the work to a background task
#     queue (returning ``DeepResult(deferred=True)`` after the enqueue is confirmed); the work
#     runs out-of-band and the user is told in the conversation when it finishes.
# Both must stay true for their configuration; neither may describe the other's mechanism.
DEFERRED_DEEP_INLINE_SEMANTICS = (
    "The answer ships first, then the deferred_deep work runs immediately after it in the SAME "
    "turn (it is not saved for later): the user gets immediate feedback PLUS the work gets done "
    "right after."
)
DEFERRED_DEEP_QUEUED_SEMANTICS = (
    "The answer ships first, then the deferred_deep work is handed to the background task queue "
    "and runs as its own task: the user gets immediate feedback now and is told in this "
    "conversation when the background work finishes."
)

# The DETERMINISTIC floor for the honest-enqueue path (queued deployments): appended to the reply
# when the hand-off failed AND the LLM rewrite that would have said so also failed. Honesty about a
# failed enqueue must never depend on a second model call succeeding, because the un-rewritten
# draft was written under queued doctrine and may already claim the work is queued.
NOT_QUEUED_NOTE = (
    "Correction: I was not able to hand this work to the background queue, so it has NOT been "
    "queued, started, or done. Ask me to try again when you are ready."
)

_PLANNER_ACTIONS = """\
The four actions:
  - "read": TARGETED, PARTIAL reads to gather what you need. In `reads`, list one or more of:
      * a section: {{"rel_path": "...", "heading": "Metrics"}} OR
                   {{"rel_path": "...", "start_line": 40, "end_line": 80}}, and/or
      * a grep:    {{"grep": "regex", "scope": "optional/subpath"}} to LOCATE content, and/or
      * a query:   {{"query": {{...}}}} for a structured source lookup (if supported), and/or
      * FILTERS alongside a query, when the request names a specific TIME PERIOD, TOPIC, WHO
        (you/a rep/the team), or KIND OF CONTENT (tasks done, decisions, conversations, files):
          {{"query": {{...}}, "time_range": {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}},
            "topic_terms": ["..."], "actor": "me"|"rep"|"team",
            "content_kind": "tasks_done"|"decisions"|"conversations"|"files"}}
        A source applies filters as a HARD filter first, then ranks by relevance within the
        matches -- use this to search past conversations/history by time or kind instead of a
        plain keyword query when the request is genuinely about a period or kind, not just a topic.
        A source that does not support a given filter ignores it. Omit filters you don't need.
      * DISCOVERY, when you do not yet know what the source of truth contains:
          {{"list_sources": true}}                       -> the collections/tables/doc-sets that exist
          {{"describe_source": "<name>", "describe_path": "<optional nested path>"}}
                                                          -> the fields/types of ONE source (drill down)
          {{"list_operations": true}}                    -> the operations you can call (reads AND changes)
          {{"describe_operation": "<name>"}}             -> the full signature/usage of ONE operation
          {{"list_guidance": true}}                      -> the catalog of use-case-specific guidance
          {{"read_guidance": "<id>"}}                    -> the full instructions of ONE guidance card
      * KNOWN-TOPIC CONTEXT (memory cards this assistant has built about topics/people/work),
        distinct from grepping files:
          {{"cards": "<query text>"}}                    -> fetch remembered context for a topic/query
          {{"card": "<card_id>"}}                        -> fetch ONE known card's content by id
        Use "cards" when the answer likely lives in what you ALREADY KNOW about a subject (prior
        conversations, a person, an ongoing piece of work), NOT in a specific file: it reaches the
        SAME topic memory that loads at the start of a turn, so if the turn started without enough
        context you can pull more at ANY step. Prefer a file grep/read for source-of-truth content
        in the corpus, and "cards" for accumulated knowledge about a topic.
    APPLICABLE GUIDANCE: the most relevant use-case-specific instructions may ALREADY be injected in
    the CONTEXT under "APPLICABLE GUIDANCE"; when the request matches a kind of work not covered
    there, list_guidance then read_guidance the matching card BEFORE you answer or act.
    DISCOVER BEFORE YOU GUESS: if you don't already know the exact source, field, or operation a
    request needs, list_sources / list_operations first (then describe_* the few you'll use), rather
    than inventing a shape. Discovery is the cheapest, most reliable way to honor what the user
    literally asked for. It does NOT favor any particular source or operation -- it just shows what
    exists. BATCH AGGRESSIVELY: reads in ONE step run IN PARALLEL -- list ALL you'd plausibly want now
    (up to {max_reads}), including several describe_* calls at once. After the read you'll be
    re-invoked with the results in GATHERED.
  - "answer": you have ENOUGH real content in GATHERED -- or it's chit-chat needing no reading.
    A DISCOVERY/CAPABILITY listing (the "AVAILABLE CAPABILITIES" menu from list_operations /
    list_sources / describe_*) is NOT real content: it only tells you what you COULD call or read.
    For a substantive request you MUST first read/grep the actual sources (or run a real query) to
    gather facts before you "answer"; answering from just the capability menu, or replying with
    which discovery operations you would run, is a FAILURE -- gather first, then answer.
    Use "answer" ONLY to INFORM (explain, summarize, advise). If the user asked you to CHANGE
    something (create/add/update/edit/delete/mark/set/rename their data or artifacts, OR fix/
    implement/build/refactor CODE OR FILES), that is an ACTION -- do NOT just describe the change in
    an answer; choose "deep" so the change is actually proposed/made. Describing a mutation in prose,
    or printing a diff/patch instead of applying it, is a FAILURE.
  - "deep": this needs REAL WORK. The test is simple: if fulfilling the request means PRODUCING or
    CHANGING an artifact (not just explaining one), it is "deep". That covers BOTH the user's data
    or artifacts -- CREATE / ADD / UPDATE / EDIT / DELETE / MARK / SET (e.g. "add a goal", "add a
    measurable outcome", "make this goal more ambitious", "update my X", "create a strategy") -- AND
    code or files: FIX a bug, IMPLEMENT / BUILD / REFACTOR a feature, EDIT or APPLY a change to a
    file (e.g. "fix the back button", "implement the new endpoint", "add a field to the form"). A
    coding/file task is ALWAYS "deep": the deep runner edits the real files, so never "answer" a
    change request by describing the fix or emitting a patch. This holds even
    mutation must be PROPOSED/EXECUTED, never merely talked about. Provide BOTH `goal` and
    `deep_brief`, and KEEP THEM DISTINCT:
      * `goal` = the SHORT, CHECKABLE DONE-STANDARD only -- the single condition that means the work
        is COMPLETE, the stop-condition an executor is held to and verifies ("the back button
        returns to the previous screen", "a backdated habit entry no longer counts toward today").
        ONE sentence, ideally under 200 characters. It is NOT a place for the task details, the
        analysis, the plan, code, or a restatement of the whole request -- a long or dumped `goal`
        is WRONG and will be rejected by the executor.
      * `deep_brief` = the clear self-contained brief with the details, which PRESERVES the user's
        action verb (say "add/update ...", not "look up/review ..."). All the context goes HERE,
        never in `goal`.
    BE A
    GROUNDED FIRST RESPONDER: if the request is actionable but UNDER-SPECIFIED (e.g. "add a goal"
    with no details), do NOT bounce it back as a question -- GROUND in the CONTEXT/GATHERED above and
    author a concrete, specific `goal` + `deep_brief` yourself (a reasonable proposal the human can
    review and edit). A mutating proposal is surfaced for review BEFORE it takes effect, so
    proposing is safe and is strongly preferred over asking for more information OR describing it.
    GROUND THE CHANGE IN A REAL OPERATION: if you are not already certain which source/operation
    the change targets, do a "read" discovery step FIRST (list_operations / list_sources, then
    describe the relevant one) so your proposal uses the actual operation the user named, not a
    guessed shape. Match what the user literally asked for; do not substitute a different artifact
    because it's easier to write.
  - "confirm": reserved for a genuine FORK you cannot ground past -- the request is truly ambiguous
    (you cannot form a reasonable proposal even after reading), OR risky/irreversible enough that a
    human must approve the DIRECTION first. Prefer "deep" with a concrete proposal whenever the
    context lets you make a sensible one; choose "confirm" only when it genuinely doesn't. Put the
    question in `confirm_question`. Do NOT also act.

MODEL TIER (`model_tier`): always set one of "haiku" | "sonnet" | "opus" -- governs the model
  that GENERATES the answer / deep run (the planner itself always runs cheap). haiku=triage/
  trivial, sonnet=most answers (default), opus=hard reasoning / deep work.
"""

_PLANNER_TAIL = """\
PARALLEL SUB-QUESTIONS (optional): if the message has INDEPENDENT parts, set `subquestions` to
  2-{max_subq} short self-contained sub-questions (answered CONCURRENTLY, then synthesized).

DEEP FAN-OUT (optional, for "deep"): if the work splits into INDEPENDENT subtasks, set
  `deep_subtasks` to 2-{max_deep} of {{"goal": "...", "brief": "..."}} -- each a concurrent run.

{mode_signal_block}{card_thread_block}{rationale_instruction}

--- THE USER'S MESSAGE ---
{user_message}

--- RECENT TRANSCRIPT (prior completed exchanges, most recent last) ---
NOTE: The transcript shows COMPLETED PRIOR WORK. The USER'S MESSAGE above is the NEW, CURRENT
REQUEST. Focus entirely on that message. Do NOT redo or continue prior tasks unless the user
explicitly asks you to. EXCEPTION: when the message is a DIRECT FOLLOW-UP about what you JUST said
(e.g. "what's the filepath?", "which one?", "say that again"), the transcript is exactly where the
answer lives -- use it and answer from it instead of re-searching the corpus.
{transcript}

--- CONTEXT (compact; LOCATES content, does NOT replace reading it) ---
{context_view}

--- GATHERED SO FAR (targeted reads/greps done this turn; [] = nothing yet) ---
{gathered}
"""

# Injected into the {mode_signal_block} slot of _PLANNER_TAIL ONLY when the consumer opted into
# planner-detected mode signals (OrchestratorConfig.mode_signals_enabled). With the flag off
# (the default) the slot renders empty, the DECIDE_TOOL schema carries no `mode_signal` field,
# and any stray `mode_signal` in a response is ignored -- the planner never hears about working
# modes, so a misread musing can never suppress a turn's actions.
_MODE_SIGNAL_PLANNER_BLOCK = """\
MODE SIGNAL (`mode_signal`, optional -- almost always omit it): detect an EXPLICIT request from
  the user to change the WORKING MODE of this conversation. Judge their INTENT in their own
  words; there are no trigger phrases.
    * "enter_brainstorm": ONLY when the user explicitly says they want to think out loud /
      explore ideas together WITHOUT anything being executed, changed, or turned into a task.
      They must be asking for the no-action mode itself, in whatever words.
    * "exit_brainstorm": ONLY when the user speaks about the MODE itself and lifts the hold --
      they tell you to stop holding back and start acting ("okay go ahead and do it now", "we are
      done brainstorming, act on this", "stop holding off, make it happen").
      An instruction about the SUBJECT MATTER is NOT a mode release, even in the imperative:
      "create a goal called X and add it to my plan", "send her an email about it", "book it" are
      how a person thinks out loud about a thing. The user talking ABOUT the work is not the user
      asking to LEAVE the mode. Those stay in brainstorm; do NOT signal an exit for them.
    * omit (null): EVERYTHING else. A topic shift, musing, a hypothetical, an open-ended
      question, or discussing ideas is NOT a mode signal. When in ANY doubt, omit it: holding is
      recoverable in one sentence (the user says go ahead), acting is not.

"""

# Injected into the {card_thread_block} slot of _PLANNER_TAIL ONLY when the consumer opted into
# per-idea threading (OrchestratorConfig.card_thread_enabled). With the flag off (the default) the
# slot renders empty, the DECIDE_TOOL schema carries no `card_thread` field, and a stray
# `card_thread` in a response is ignored, so a consumer that never asked for threads sees a
# byte-identical planner prompt and a byte-identical schema. ``{thread_hint}`` is the per-turn
# candidate list (see core.card_thread.render_thread_hint): the cheap PRIOR, which narrows and
# surfaces; the model's judgment decides. Contains no literal {/} beyond that one slot.
_CARD_THREAD_PLANNER_BLOCK_TEMPLATE = (
    CARD_THREAD_GATE
    + "\n\n"
    + "{thread_hint}\n\n"
)

# The `rationale` field is dual-purpose: by default it is the planner's terse internal reasoning,
# but when the consumer turns on narration (cfg.narrate) it becomes the user-facing, spoken
# "train of thought" beat for this step (Approach B: no extra LLM call — the planner writes its
# rationale conversationally, in the selected rep's voice, in the call it already makes). The
# orchestrator picks which instruction to inject per run via the {rationale_instruction} slot.
_RATIONALE_INSTRUCTION_PLAIN = "Always fill `rationale` (one sentence) and set `model_tier`."
# Step 0: no data yet. Say what you're about to look at, specifically. Also set `model_tier`.
_RATIONALE_INSTRUCTION_NARRATE = (
    "`rationale` = ONE spoken line, thinking out loud, naming the specific thing you're about to "
    "look at (not 'details'). You have NOT read anything yet, so name what you're going to check, "
    "never what you expect to find or conclude. No em dashes, greeting, or markdown. Empty if "
    "nothing worth saying. Also set `model_tier`."
) + "\n\n" + language_instruction()
# Re-plan steps (step > 0): data is in GATHERED. React to it like a coach, then say what it makes
# you do next. Bridge insight to intent, never narrate a read in isolation. Also set `model_tier`.
_RATIONALE_INSTRUCTION_NARRATE_REPLAN = (
    "`rationale` = ONE spoken line reacting to what you ACTUALLY found in GATHERED and what that "
    "makes you check next (e.g. 'your pace holds early but drops at mile 18, so I'll look at your "
    "training load'). Be specific and opinionated about what to check NEXT, but stay honest about "
    "how much you've actually seen: speak only to what GATHERED shows. If you have NOT found "
    "something yet, say so ('I haven't found a spec for that yet'), never claim it doesn't exist or "
    "that some other thing must be true instead. Do not state a gap, cause, or conclusion as settled "
    "before you've read enough to back it; while it's still a hunch, voice it as a hunch or a "
    "question, not a fact. Never repeat anything in 'Already said'. No em dashes, greeting, or "
    "markdown. Empty if nothing genuinely new. Also set `model_tier`."
) + "\n\n" + language_instruction()

# Injected at the top of the planner prompt (both the flattened and the layered shape) when the
# consumer runs the turn with execution_mode="brainstorm". It narrows the ACTION space only; the
# reading/answering doctrine above it applies unchanged, so brainstorm turns keep full context
# and full intelligence. Contains no literal {/} so it is .format()-safe.
_BRAINSTORM_PLANNER_NOTE = """\
--- BRAINSTORM MODE (active for this turn) ---
The user has asked to think out loud in this conversation: nothing gets executed, changed, turned
into a task, or parked as a pending question while this mode is on. The actions "deep", "confirm"
and "clarify" are ALL UNAVAILABLE this turn -- use "read" or "answer" only, and do not set
`deferred_deep` or `answer_contains_work_to_execute`. Read as much as you need and answer with
your full judgment: explore, compare, weigh options, sketch plans, advise. Describing possible
work is CORRECT here, not a failure -- the ordinary rule that a change request must escalate to
"deep" is suspended. If something is ambiguous, do NOT stop to ask for a decision: answer with
your best reading and put the open question to the user inside the reply itself.
"""

# Appended to _BRAINSTORM_PLANNER_NOTE only when mode signals are enabled (the mode vocabulary the
# note leans on only exists in the schema when the consumer opted in). The planner does NOT own the
# exit while the latch is held: a dedicated structured judgment (Orchestrator.judge_brainstorm_release,
# MODE_RELEASE_PROMPT) already decided, before this plan, whether the user released the hold, and its
# verdict is the one that counts. Telling the planner that here keeps the prompt honest and stops it
# from reading a subject-matter imperative as permission to act.
_BRAINSTORM_EXIT_SIGNAL_NOTE = """\
Whether the user has RELEASED this mode was already judged, separately, before this plan, so do not
try to leave it yourself: leave `mode_signal` empty. An instruction about the SUBJECT MATTER ("create
a goal called X and add it to my plan", "send her an email about it", "book it") is part of thinking
out loud, not a release, so answer it without acting on it.
"""

# Folded into the grounding of EVERY reply a latched turn produces, whatever terminal path it took
# (zero extra LLM calls). It carries two things:
#
#   1. The HONESTY FLOOR (a hard rule): a held turn ran nothing, so no reply of it may say or imply
#      that it did, or that execution is imminent. A real held reply once said "The system will now
#      execute this action to add it directly to your plan" while nothing ran: the worst possible
#      failure, and the reason this rides on EVERY latched reply.
#   2. The NO-ACTION ACKNOWLEDGMENT (guidance, with explicit permission to skip): if the message
#      asked for something, name the hold out loud so the user knows why nothing happened and how to
#      lift it. This is deliberately NOT gated on an intent detector. A real run proved why: the
#      cheap regex prefilter does not see "Set that up.", "Book it." or "Do the thing we discussed"
#      as directives at all, so gating on it left exactly the turns that most needed the
#      acknowledgment without it. The ANSWERING model reads the message anyway; it is the better
#      judge of whether the message asked for something, and the note tells it to skip the
#      acknowledgment when the user was plainly just thinking out loud.
#
# No em dashes: this steers user-facing text.
BRAINSTORM_NO_ACTION_ACK_NOTE = """\
--- BRAINSTORM MODE: NOTHING WAS EXECUTED THIS TURN ---
This conversation is on hold: nothing was executed, changed, sent, scheduled, or queued this turn,
and nothing will be until the user lifts the hold. Never say or imply that you have acted, that
something is underway, or that anything is about to run. Talk about the work as something you would
do once they say go ahead.
If this message asked you to do something, say plainly that you have not done it because brainstorm
mode is on, and that the user can tell you to go ahead when they are ready. That part is guidance,
not a script: use your judgment, and skip or soften the acknowledgment when the user was clearly
just thinking out loud, or when your reply already makes the no-action state obvious.
"""

# Appended to the note above only when the PLANNER itself was ready to act this turn (it chose
# deep/confirm, or set deferred_deep / answer_contains_work_to_execute) and the latch held it back.
BRAINSTORM_HELD_WORK_ACK_NOTE = """\
You were ready to start this work and held off only because of brainstorm mode. Make clear that
you will begin as soon as the user says to go ahead.
"""

# Appended to the no-action note when the turn WANTED to stop and ask the user something (the
# planner chose "clarify", or the input-understanding stage could not resolve the message). While
# the latch is held, asking is not allowed to park a decision-request: a latched turn escalates
# NOTHING. The question is carried into the reply instead, so the user still gets asked, in the
# conversation, with no pending ask created anywhere. ``question`` is appended by the caller.
BRAINSTORM_CLARIFY_ACK_PREFIX = """\
Before you could act on this you would need one thing settled. Put that question to the user
directly in your reply, in your own words, and give your best thinking on it meanwhile. Do not
assume an answer and do not act on one. The open question is:
"""

# Assemble the final format()-able prompt. The gate constants from context_doctrine have NO
# literal {/} characters, so they pass through .format() untouched when the final assembled
# string is .format()-ed in _plan(). Only the real {slot_name} placeholders in _PLANNER_ACTIONS
# and _PLANNER_TAIL are substituted; JSON-example braces use the standard {{...}} double-brace form.
PLANNER_PROMPT = (
    _PLANNER_HEAD
    + "\n--- SUFFICIENCY (read enough before acting) ---\n"
    + SUFFICIENCY_GATE + "\n\n"
    + "\n--- SPECIFICITY (match the exact subject, not its category) ---\n"
    + SPECIFICITY_GATE + "\n\n"
    + _PLANNER_ACTIONS
    + "\n--- " + MODEL_TIER_GATE.split("\n")[0] + "\n"
    + "\n".join(MODEL_TIER_GATE.split("\n")[1:]) + "\n\n"
    + "\n--- " + CACHED_HINT_GATE.split("\n")[0] + "\n"
    + "\n".join(CACHED_HINT_GATE.split("\n")[1:]) + "\n\n"
    + _PLANNER_TAIL
)


def planner_prompt_defaults() -> Dict[str, Any]:
    """PUBLIC: a safe default for EVERY ``PLANNER_PROMPT`` format slot.

    ``PLANNER_PROMPT`` is a public export, and every slot it carries is mandatory for a raw
    ``.format()`` call: adding one (``deferred_deep_semantics``, for the queued-deferred wording)
    breaks any external consumer that renders the prompt itself. This is the non-breaking path:
    pair it with ``render_planner_prompt``, which fills whatever the caller does not pass, so a
    future slot can be added without breaking anyone again.
    """
    cfg = OrchestratorConfig()
    return {
        "user_message": "",
        "transcript": "(no prior messages)",
        "context_view": "(no context)",
        "gathered": "(nothing gathered yet)",
        "max_reads": cfg.max_reads_per_step,
        "max_subq": cfg.max_subquestions,
        "max_deep": cfg.max_deep_subtasks,
        "mode_signal_block": "",
        "card_thread_block": "",
        # Inline is the DEFAULT deployment (OrchestratorConfig.deferred_deep_queued = False), so
        # the default wording must be the inline one: it is the only one true by default.
        "deferred_deep_semantics": DEFERRED_DEEP_INLINE_SEMANTICS,
        "rationale_instruction": _RATIONALE_INSTRUCTION_PLAIN,
    }


def render_planner_prompt(**slots: Any) -> str:
    """PUBLIC: render ``PLANNER_PROMPT`` with defaults for every slot the caller omits.

    ``render_planner_prompt(user_message="...")`` is the stable way for an external consumer to
    render the planner prompt. Pass any subset of the slots (see ``planner_prompt_defaults``);
    the rest are filled with their defaults instead of raising ``KeyError``.
    """
    values = planner_prompt_defaults()
    values.update(slots)
    return PLANNER_PROMPT.format(**values)


# The structured decision schema the planner MUST return (forced tool use).
DECIDE_TOOL: Dict[str, Any] = {
    "name": "decide",
    "description": "Record the chosen NEXT step and its parameters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "answer", "deep", "confirm", "clarify"]},
            "reads": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rel_path": {"type": "string"},
                        "heading": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                        "grep": {"type": "string"},
                        "scope": {"type": "string"},
                        "query": {"type": "object"},
                        "time_range": {
                            "type": ["object", "null"],
                            "description": "Optional HARD filter alongside a query/grep: only "
                                           "consider content from this period. Resolve relative "
                                           "dates ('Wednesday', 'last week') to concrete dates.",
                            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
                        },
                        "topic_terms": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Optional topic/keyword terms to narrow a query/grep.",
                        },
                        "actor": {"type": ["string", "null"], "enum": ["me", "rep", "team", None]},
                        "content_kind": {
                            "type": ["string", "null"],
                            "enum": ["tasks_done", "decisions", "conversations", "files", None],
                        },
                        "list_sources": {"type": "boolean"},
                        "describe_source": {"type": "string"},
                        "describe_path": {"type": "string"},
                        "list_operations": {"type": "boolean"},
                        "describe_operation": {"type": "string"},
                        "list_guidance": {"type": "boolean"},
                        "read_guidance": {"type": "string"},
                        "cards": {"type": "string",
                                  "description": "Fetch remembered topic/person/work context for "
                                                 "this query from the assistant's memory cards "
                                                 "(the SAME context loaded at turn start; callable "
                                                 "at any step). Use for accumulated knowledge, not "
                                                 "source-of-truth files (grep/read those)."},
                        "card": {"type": "string",
                                 "description": "Fetch ONE known context card's content by its id."},
                    },
                },
            },
            "goal": {"type": ["string", "null"]},
            "deep_brief": {"type": ["string", "null"]},
            "confirm_question": {"type": ["string", "null"]},
            "model_tier": {"type": ["string", "null"], "enum": ["haiku", "sonnet", "opus", None]},
            "subquestions": {"type": "array", "items": {"type": "string"}},
            "deep_subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"goal": {"type": "string"}, "brief": {"type": "string"}},
                },
            },
            "deferred_deep": {
                "type": ["object", "null"],
                "description": "When action='answer', optionally specify deep work that runs "
                               "immediately after the answer, in the same turn (the answer ships "
                               "first, then this work executes right away).",
                "properties": {
                    "goal": {"type": "string", "description": "The goal for the follow-up deep work"},
                    "brief": {"type": "string", "description": "Brief for the follow-up deep work (optional, defaults to user message)"},
                    "rationale": {"type": "string", "description": "Why this deep work runs after the answer"},
                },
                "required": ["goal"],
            },
            "answer_contains_work_to_execute": {
                "type": "boolean",
                "description": "Set to true if this answer describes work the AI should execute (instead of just reporting). Triggers auto-escalation to deep.",
            },
            "clarification": {
                "type": ["object", "null"],
                "description": "When action='clarify', specify what user input/approval is needed.",
                "properties": {
                    "question": {"type": "string", "description": "What to ask the user"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "List of possible responses (if multiple-choice)"},
                    "allow_free_input": {"type": "boolean", "description": "Whether user can provide free-text input beyond options"},
                },
                "required": ["question"],
            },
            "rationale": {"type": "string"},
        },
        "required": ["action", "rationale"],
    },
}

# The opt-in DECIDE_TOOL variant used when OrchestratorConfig.mode_signals_enabled is on: the
# base schema plus the `mode_signal` field. Kept as a separate schema (rather than a field the
# planner is told to ignore) so a consumer that never opted in exposes no mode vocabulary at
# all -- the planner cannot misfire a signal it cannot express.
_MODE_SIGNAL_TOOL_FIELD: Dict[str, Any] = {
    "type": ["string", "null"],
    "enum": ["enter_brainstorm", "exit_brainstorm", None],
    "description": "Set ONLY when the user EXPLICITLY asks to change the working "
                   "mode: 'enter_brainstorm' when they explicitly want to think out "
                   "loud with nothing acted on, 'exit_brainstorm' when they "
                   "explicitly ask to proceed/act on what was discussed. Otherwise "
                   "null. Judge intent; a topic shift or musing is never a signal.",
}
DECIDE_TOOL_WITH_MODE_SIGNAL: Dict[str, Any] = copy.deepcopy(DECIDE_TOOL)
DECIDE_TOOL_WITH_MODE_SIGNAL["input_schema"]["properties"]["mode_signal"] = _MODE_SIGNAL_TOOL_FIELD

# The opt-in `card_thread` field (OrchestratorConfig.card_thread_enabled): the ONE field per-idea
# threading costs. It rides the planning call the orchestrator already makes every turn, so topic
# assignment adds ZERO extra LLM calls. Same discipline as the mode signal: a consumer that never
# opted in exposes no thread vocabulary at all, so the planner cannot misfire a field it cannot
# express.
_CARD_THREAD_TOOL_FIELD: Dict[str, Any] = {
    "type": ["string", "null"],
    "description": "Which TOPIC (context card) this message belongs to. Exactly one of: "
                   "'continue' (the current topic, the usual answer), "
                   "'switch_to:<card_id>' (one of the KNOWN TOPICS listed in the prompt, using its "
                   "id verbatim), or 'new:<short label>' (a genuinely new idea; 2 to 5 words). "
                   "Never a mode change and never an action. When in doubt, 'continue'.",
}

# The deferred_deep field description for a QUEUED deployment (OrchestratorConfig.
# deferred_deep_queued): the consumer wired a deep runner that enqueues the work as a background
# task, so the schema must describe that mechanism, not the inline same-turn one. The inline
# description lives on DECIDE_TOOL itself (the default configuration).
DEFERRED_DEEP_FIELD_DESC_QUEUED = (
    "When action='answer', optionally specify deep work to hand to the background task queue "
    "(the answer ships first; the queued work then runs as its own background task and the user "
    "is told in this conversation when it finishes)."
)

# Reserved named-runner registry key for QUEUED deployments: when OrchestratorConfig.
# deferred_deep_queued is on and the consumer registered a runner under this key in
# ``deep_runners``, every planner ``deferred_deep`` is PINNED to that runner (bypassing the
# classifier) so deferred work always reaches the queue and is never re-routed to an inline
# runner. Without this key, deferred work resolves through the normal runner wiring.
#
# RESERVED, NOT CLASSIFIER-SELECTABLE: this key is reachable ONLY through the deferred hand-off's
# explicit runner_override. A ``deep_runner_classifier`` that returns it for an ordinary deep turn
# is REJECTED (the default runner handles that turn instead), because routing normal deep work to
# the queue runner would hand the goal loop a queue receipt and let it be reported as finished work.
DEFERRED_RUNNER_KEY = "deferred"


def decide_tool_for(mode_signals: bool, deferred_queued: bool,
                    card_thread: bool = False) -> Dict[str, Any]:
    """Return the decide-tool schema variant for this run's configuration.

    ``mode_signals`` adds the opt-in ``mode_signal`` field; ``card_thread`` adds the opt-in
    ``card_thread`` field (per-idea threading); ``deferred_queued`` swaps the ``deferred_deep``
    field description for the queued-background wording so the schema always tells the planner what
    the wired deep runner ACTUALLY does with deferred work.
    """
    base = DECIDE_TOOL_WITH_MODE_SIGNAL if mode_signals else DECIDE_TOOL
    if not deferred_queued and not card_thread:
        return base
    tool = copy.deepcopy(base)
    if deferred_queued:
        tool["input_schema"]["properties"]["deferred_deep"]["description"] = (
            DEFERRED_DEEP_FIELD_DESC_QUEUED)
    if card_thread:
        tool["input_schema"]["properties"]["card_thread"] = copy.deepcopy(_CARD_THREAD_TOOL_FIELD)
        # REQUIRED, not optional. An optional field is one a model quietly omits, and every omission
        # lands on the fail-safe ("continue the current card"), which reads as a topic that never
        # moves: a real run left three different ideas all sitting on the first card the user had
        # opened. Requiring it forces the judgment to actually happen every turn. The fail-safe is
        # unchanged and still catches a malformed or missing value; it just stops being the norm.
        required = tool["input_schema"].setdefault("required", [])
        if "card_thread" not in required:
            required.append("card_thread")
    return tool


@dataclass
class OrchestratorConfig:
    max_steps: int = DEFAULT_MAX_STEPS
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS
    max_gathered_chars: int = DEFAULT_MAX_GATHERED_CHARS
    max_reads_per_step: int = DEFAULT_MAX_READS_PER_STEP
    max_parallel: int = DEFAULT_MAX_PARALLEL
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS
    max_deep_subtasks: int = DEFAULT_MAX_DEEP_SUBTASKS
    deep_max_turns: int = DEFAULT_DEEP_MAX_TURNS
    max_gather_chars: int = DEFAULT_MAX_GATHER_CHARS
    max_consecutive_reads: int = DEFAULT_MAX_CONSECUTIVE_READS
    # OUR OWN GOAL LOOP (replaces Claude Code's /goal). After the deep worker runs, the brain
    # verifies the done-standard with one cheap LLM call; if it is not yet met, it feeds back what
    # went wrong / what to do next and re-runs, up to this many attempts. 1 = no verify-retry (single
    # shot). This is more token-efficient than /goal (which re-verifies inside the worker every turn)
    # and lets the brain steer the next attempt. NOTE: now a HARD SAFETY CAP on attempts; the PRIMARY
    # stop is the token budget below.
    deep_goal_max_iterations: int = 8
    # Overall TOKEN BUDGET for one turn's deep goal loop (sum of the worker's reported tokens across
    # attempts). The loop keeps iterating + escalating the model WHILE under budget; None disables it
    # (then only the attempt cap applies). The consumer sets this from an env var so it is operator-
    # tunable. Default allows a few full deep attempts.
    deep_goal_token_budget: Optional[int] = 300_000
    # The deep-worker MODEL LADDER: models tried in order, escalating to a STRONGER one when a goal
    # is not met (the failure may be a model-capability gap). The deep worker is Claude Code, so
    # these are Claude models/aliases (fast -> strong, e.g. ["haiku","sonnet","opus"]). None = use the
    # single model the orchestrator was given (back-compat). The consumer sets it (e.g. from
    # QAR_DEEP_MODELS); an explicit per-task model request or a guidance "model preference" pins the
    # model and disables auto-escalation.
    deep_model_ladder: Optional[List[str]] = None
    # The SAME goal loop applied to plain ANSWERS (not just deep execution): after an answer is
    # written, the brain verifies it meets the goal at the quality bar (the guidance cards selected
    # for the input) and, if unmet, regenerates with steering — up to this many attempts. Only
    # engages when a GuidanceProvider is wired (there is a quality bar to check). 1 = no verify-retry.
    answer_goal_max_iterations: int = 2
    planner_tier: str = "balanced"  # routing tier: balanced catches misclassifications haiku misses
    # Per-step planner-view leaning (see DEFAULT_PLANNER_* above). The full ``gathered`` is always
    # kept for the final answer; these only trim what the cheap PLANNER re-reads each re-plan step.
    planner_recent_full: int = DEFAULT_PLANNER_RECENT_FULL
    planner_compress_over: int = DEFAULT_PLANNER_COMPRESS_OVER
    # On re-plan steps (step > 1), replace the unchanged transcript + static context_view with a
    # short reference note (they were sent in full on step 1). Default off → unchanged behavior.
    # The final ANSWER path is never affected — it always grounds on the full transcript/context.
    planner_abbreviate_repeat_context: bool = DEFAULT_PLANNER_ABBREVIATE_REPEAT_CONTEXT
    # INSTANT ACK: when True, emit an immediate "Looking into this..." status at the top of run()
    # and launch a cheap one-sentence acknowledgment LLM call IN A BACKGROUND THREAD so it runs
    # CONCURRENTLY with context assembly + the first planner step.  The ack is emitted as an
    # EVENT_PARTIAL when it returns (~1 s); a failure is swallowed silently.  Default False so
    # existing callers see no behavior change.
    instant_ack: bool = False
    # CONVERSATIONAL NARRATION: when True, the orchestrator narrates the turn as ONE continuous,
    # human "thinking out loud" train of thought (the instant ack is just its first beat, not a
    # separate path): a short conversational line at each meaningful stage — the new message,
    # gathering context, running the deeper work — each CONTINUING the same thought and reacting to
    # what just came in (e.g. the planner's reasoning), the way a person says new things as they
    # occur to them. Lines are emitted as EVENT_PARTIAL (shown live in the chat bubble, not
    # persisted; spoken on voice). Generated at the cheap planner tier; the first beat runs
    # concurrently so quick turns add no latency, and slow stages (gather/deep) absorb the cheap
    # line since there is real wait to fill. Failures are swallowed (the turn never depends on
    # narration). Speaks in the selected rep's persona (rep_preamble) when available. Supersedes
    # instant_ack when set.
    narrate: bool = False
    # Consumer-supplied system prompt defining HOW to narrate (voice, style, rules). When None, a
    # sensible generic default is used. The selected rep persona is always applied on top of it.
    narration_system_prompt: Optional[str] = None
    # GUIDANCE PRE-SELECTION: how many use-case-specific guidance cards the orchestrator asks a
    # wired GuidanceProvider to pre-select (via select()) for the "APPLICABLE GUIDANCE" block
    # before planning. Only consulted when a GuidanceProvider is wired; otherwise inert.
    guidance_topk: int = 3
    # BROKEN-PROMISE CHECK, folded into the goal verification. When on, every answer turn's goal
    # verdict also judges whether actions the reply CLAIMS it completed are backed by the turn's
    # EXECUTION RECORD (the brain itself can never change files/data; only deep runs can). An
    # unsupported claim remediates inside the answer goal loop: execute the work for real via a
    # deep run when NOTHING ran this turn (safe), otherwise regenerate the reply to be honest and
    # flag the result partial (so a background task maps to needs_you/failed, not done). ON by
    # default. ``max_remediations`` caps the execute-for-real re-runs (only when no action ran).
    verify_claims: bool = True
    max_remediations: int = 1
    # GOAL-VERIFICATION JUDGE TIER. The tier ``_verify_goal`` (the met/not-met + claims-honesty
    # verdict) resolves its model from. This verdict is the run's risk gate: it decides done vs
    # needs_you/failed and whether a reply's completion claims are honest, so a wrong verdict either
    # ships a false "done" or triggers a full regeneration / deep re-run — both far costlier than
    # the tier delta on ONE small call whose inputs are already hard-capped (~12.5k chars). Spend
    # the strong model on judgment, keep the cheap tiers for gathering. Empty string falls back to
    # ``planner_tier`` (the previous behavior).
    verify_tier: str = "best"
    # INTENT-DIRECTIVE JUDGE (WS3: structured judgment replacing a regex-only call). The cheap
    # regex prefilter (``_message_requests_change``) decides most turns for free; when it CANNOT
    # (a change-verb/wrongness signal fired but an interrogative opener or a bare "?" overrode it --
    # see ``message_change_signal_ambiguous``), ONE structured LLM call judges the ambiguous
    # message instead of guessing. This is a ROUTING decision, not the run's outcome gate
    # (``verify_tier`` is that), so it defaults to the cheaper "balanced" tier. The call is
    # hard-timeout-guarded (``intent_judge_timeout_seconds()``) and ALWAYS falls back to the regex
    # verdict on any failure/timeout/parse miss -- it can only ever ADD an escalation the regex
    # missed, never block the turn or override a "yes" the regex already gave.
    intent_judge_tier: str = "balanced"
    # BRAINSTORM-RELEASE JUDGE TIER. The tier ``judge_brainstorm_release`` (does THIS message lift
    # the no-action hold?) resolves its model from. Deliberately NOT the planner tier: the planner
    # is cheap by design, and a cheap model reads any imperative -- including one purely about the
    # subject matter ("create a goal called X and add it to my plan") -- as "the user is asking to
    # proceed", which silently releases the latch and executes work in a conversation the user put
    # on hold. "balanced" buys the judgment that distinguishes an instruction ABOUT THE WORK (held)
    # from a release OF THE MODE (exit), on ONE small call that only ever runs while the latch is
    # held. Fail-safe: any failure/timeout/parse miss HOLDS (see judge_brainstorm_release).
    mode_release_tier: str = "balanced"
    # EXECUTION MODE for this run, supplied by the CONSUMER per run (the orchestrator is stateless
    # about it; the consumer owns the latch and persists it wherever its conversation state lives).
    #   * "normal" (default): today's behavior, byte-for-byte.
    #   * "brainstorm": the user is thinking out loud and nothing may be acted on this turn.
    #     Reads, context assembly, and answers are UNTOUCHED (full context, full intelligence);
    #     what is disabled is ACTING: a planner "deep" or "confirm" degrades to "answer", and the
    #     nets that can only ADD execution (deferred deep, the message-intent escalation fallback,
    #     overseer escalations, claim-remediation and insufficient-context deep re-runs) are
    #     skipped. Any unrecognized value of this field behaves as "normal" (fail-safe). A
    #     consumer may drive this mode purely from its own state (a settings toggle, a slash
    #     command) without ever enabling planner-detected signals below.
    execution_mode: str = "normal"
    # PLANNER-DETECTED MODE SIGNALS, opt-in (default OFF -- with the flag off, behavior is
    # byte-identical to a build without the feature: the planner prompt carries no MODE SIGNAL
    # block, the decide tool schema has no ``mode_signal`` field, and a stray ``mode_signal`` in
    # a response is ignored, so a planner misfire can never silently suppress a turn's actions
    # for a consumer that never asked for modes). When ON, mode CHANGES are detected by the
    # planner itself (LLM judgment on the planning call that already runs every turn, never
    # phrase matching) and reported via ``PlanDecision.mode_signal`` -> EVENT_MODE_SIGNAL +
    # ``OrchestratorResult.mode_signal``; an "enter_brainstorm" signal engages the gating for the
    # SAME turn. The orchestrator stays stateless; the consumer owns persisting the latch.
    #
    # EXIT is different, and deliberately does NOT ride the planner call. While the latch is held
    # (``execution_mode="brainstorm"``), a planner "exit_brainstorm" is IGNORED; the release is
    # decided once per turn by ``judge_brainstorm_release`` (a dedicated structured call at
    # ``mode_release_tier``), whose verdict is what sets ``mode_signal="exit_brainstorm"`` and
    # releases the gating for the same turn. See ``mode_release_tier`` for why: a cheap planner
    # judges a subject-matter imperative ("create a goal called X and add it to my plan") as a
    # request to proceed, which broke the latch exactly when it mattered most.
    mode_signals_enabled: bool = False
    # PER-IDEA THREADING, opt-in (default OFF -- with the flag off the planner prompt carries no
    # TOPIC block, the decide-tool schema has no ``card_thread`` field, a stray ``card_thread`` in a
    # response is ignored, no EVENT_CARD_THREAD is emitted, and no thread meta reaches the
    # assembler: byte-identical to a build without the feature, so other consumers are unaffected).
    #
    # When ON, THE IDEA IS THE CARD (see core/card_thread.py). Every turn is assigned to a context
    # card by the SAME planning call the orchestrator already makes (ZERO extra LLM calls): the
    # planner emits ONE field, "continue" | "switch_to:<card_id>" | "new:<label>", after being shown
    # a cheap PRIOR (the cards this turn's hybrid retrieval already scored, plus whatever the
    # consumer always wants offered). The prior only NARROWS and SURFACES; the model's judgment
    # decides, and any parse failure or ambiguity CONTINUES the current card. The resolved
    # assignment is reported via ``OrchestratorResult.card_thread`` + EVENT_CARD_THREAD; the
    # orchestrator stays stateless, exactly as it is about modes: the consumer owns the card store
    # and stamps its own messages.
    #
    # A topic switch is NOT a mode signal: it never touches the brainstorm latch (a new idea in a
    # held conversation is still held; a returning idea does not release it).
    card_thread_enabled: bool = False
    # DEFERRED DEEP IS QUEUED. Set True by a consumer whose wired deep runner QUEUES a planner
    # ``deferred_deep`` as a background task (confirming the enqueue and returning
    # ``DeepResult(met=True, deferred=True)`` with a hand-off sentinel as its output) instead of
    # executing it inline. Three things change, all so words and behavior stay truthful:
    #   1. The planner doctrine + decide-tool schema describe deferred_deep as queued background
    #      work the user is told about when it finishes (with the flag off they keep the inline
    #      same-turn wording; each wording is only ever shown when it is true).
    #   2. A CONFIRMED hand-off (a deferred deep result with met=True) rewrites the reply via the
    #      queued synthesis prompt (report the work as queued, never as done) and skips the
    #      answer goal-verification loop (the deferred contract: trust met, no re-verify of a
    #      sentinel, no relaunch); the result's exit_reason becomes "deferred".
    #   3. A deferred attempt that produced NO confirmed hand-off, AND no real inline output to
    #      report, regenerates the reply with an honesty steer so it never claims the work was
    #      queued or done (the honest-enqueue rule: "queued" may only be claimed after the enqueue
    #      is confirmed). A hand-off is only CONFIRMED when the result is deferred, met, error-free
    #      and carries a non-empty receipt; anything else is treated as a failed enqueue. If the
    #      deep work actually RAN inline this turn (e.g. the deferred runner key is missing from
    #      ``deep_runners``, so the normal wiring executed it for real), its output is folded back
    #      and reported as done: a turn that produced real work is never described as "not queued".
    # False (default): deferred_deep runs inline right after the answer, exactly as before.
    #
    # WIRING PRECONDITION: register the queue runner under ``deep_runners[DEFERRED_RUNNER_KEY]``.
    # That alone is enough for a planner-chosen ``deferred_deep`` to reach the queue: the hand-off
    # pins the runner explicitly, so it needs neither a ``deep_runner`` nor a
    # ``deep_runner_classifier``. The escalation nets that INFER deferred work from an answer
    # (answer_contains_work_to_execute, the described-work net, the message-intent fallback) also
    # fire for a queue-only wiring. Every OTHER deep path (a planner "deep" action, an overseer
    # escalate_deep, claim remediation) still needs real inline capability: a ``deep_runner`` or
    # ``deep_runners`` + a ``deep_runner_classifier``.
    deferred_deep_queued: bool = False
    # MINIMAL-INTERVENTION OVERSEER. A high-quality model reads a tiny digest of the run and writes a
    # tiny signal, watching the loop the way a person's awareness watches their own body walk: almost
    # always silent, occasionally sending one small course correction. It is consulted at two points
    # (inside the plan loop, and once at the answer checkpoint) and can redirect (nudge the next plan
    # with one hint), answer_now (stop reading and answer), escalate_deep (hand off to deep execution,
    # routine AI-doable work) or escalate_human (a genuine human-only fork). OFF by default; when off
    # the run is byte-for-byte identical (zero overseer calls, no events, no threads). The overseer
    # NEVER raises: any failure degrades to "proceed" (do nothing).
    overseer: bool = False
    overseer_tier: str = "best"          # the (high-quality) tier the overseer model resolves to
    overseer_every_steps: int = 1        # consult once every N plan steps (>= overseer_min_step)
    overseer_max_signals: int = 3        # hard cap on overseer consultations per run (both hooks)
    overseer_min_step: int = 1           # earliest plan step (1-based) the overseer may run
    overseer_digest_char_budget: int = 1600  # hard cap on the digest string fed to the overseer
    # NON-BLOCKING overseer (hooks A and B; see docs/overseer.md). The overseer's provider call runs
    # in a BACKGROUND thread and its result is polled with this timeout, so consulting it never
    # stalls the loop or the answer. 0.0 = a pure, non-blocking ``future.done()`` check (the design
    # default: never block). A small positive value lets the poll briefly WAIT for a still-running
    # consult; it always degrades to proceed (hook A) / ships the draft as-is (hook B) on timeout, so
    # it can never hang the run. Used mainly by tests to make the async apply deterministic.
    overseer_poll_timeout_seconds: float = 0.0
    # Hook B (final answer checkpoint) used to WAIT synchronously up to a short bound before shipping
    # the answer. It no longer does (Fix 11): it now does the SAME non-blocking check as hook A
    # (``overseer_poll_timeout_seconds``) and, if the consult has not resolved yet, ships the draft
    # immediately and hands the pending future to a BACKGROUND finisher instead of blocking every
    # single answer. This bounds how long that background finisher will wait before giving up
    # entirely (a late resolution past this point is simply dropped; nothing is waiting on it).
    overseer_background_finish_timeout_seconds: float = 30.0
    # CHEAP, NON-LLM PRE-FILTER GATE for hook A (Fix 12): submitting a consult to the expensive
    # overseer model on a blind fixed cadence wastes calls on runs that are obviously fine. Hook A
    # only submits when at least one free signal suggests the step is actually worth a look:
    #   - consecutive_reads has crossed ``overseer_gate_min_consecutive_reads`` (a stuck read loop),
    #   - the plan repeats the previous step's action+goal (``overseer_gate_repeat_plan``, a sign of
    #     looping on the same idea), or
    #   - elapsed time OR gathered-read volume has crossed ``overseer_gate_spend_fraction`` of budget.
    # Hook B (the final answer checkpoint) is NOT gated by this -- it always consults (subject only
    # to overseer_max_signals), since it is a one-time final check, not a cadence.
    overseer_gate_min_consecutive_reads: int = 2
    overseer_gate_repeat_plan: bool = True
    overseer_gate_spend_fraction: float = 0.6
    # ASYNC POST-DEEP CONTEXT-CARD UPDATER. After a deep task finishes (answer already delivered),
    # an ASYNC, best-effort LLM process updates THIS user's context cards to prepare for future
    # similar requests: it appends a "future context" instruction to each deep brief, parses that
    # section back out of the result, makes ONE cheap LLM call to plan card edits (fields + content,
    # corrections, removals), and applies them via the card-update API. ON by default; fully inert
    # when no card-update-capable store, no provider, or this toggle is off (then deep is byte-for-
    # byte unchanged, including the brief: the future-context block is appended ONLY when active). The
    # consumer can disable it from env/config. The updater never blocks the answer and never raises.
    async_card_update: bool = True
    # Hard cap on how many CARDS the updater will touch in one run (keeps a best-effort background
    # write bounded regardless of what the LLM returns).
    async_card_update_max_cards: int = 6
    # Hard cap on edit OPERATIONS (add/replace/remove items + field edits) applied per card.
    async_card_update_max_edits_per_card: int = 12
    # SEMANTIC CARD-MERGE threshold (cosine). When the updater would CREATE a new card, it first asks
    # the card store's optional ``find_similar_card`` capability whether a card this similar already
    # exists for THIS user and, if so, redirects the edit to UPDATE that card (no near-duplicate twin).
    # HIGH by default (clear-twin only); 1.0 effectively disables the behavior, and a store without
    # embeddings (no ``find_similar_card``) skips it entirely. See DEFAULT_CARD_MERGE_SIMILARITY.
    card_merge_similarity: float = DEFAULT_CARD_MERGE_SIMILARITY
    # WARM RECENT-CONTEXT FALLBACK (see core/recent_context.py). ON by default. When a
    # RecentContextStore is wired (``Orchestrator.recent_context``), each turn synchronously loads
    # the small set of cards the CONVERSATION's own recent turns selected (a fast local file read,
    # no LLM call) and gates them through ``filter_relevant`` (pure lexical overlap, no LLM) before
    # merging any survivors into context_view. This is what keeps a follow-up turn warm even when
    # the fresh background assembly times out or nothing is wired at all. False disables the
    # fallback even when a store is wired. Env: QAR_RECENT_CONTEXT ("0"/"false" disables; read in
    # cli.py's _config_from_env).
    recent_context_enabled: bool = True
    # Hard cap on how many recent-turn cards ``filter_relevant`` may let through in one turn (these
    # are ADDITIONAL to whatever the fresh assembly finds, so kept small). Env:
    # QAR_RECENT_CONTEXT_MAX_CARDS (read in cli.py's _config_from_env).
    recent_context_max_cards: int = 6
    # Whether the WARM recent-context store's "global" scope (everything recently selected anywhere,
    # not just this conversation/quest) is consulted at all. True by default. Setting this False
    # turns off ONLY cross-conversation/cross-quest memory -- conv- and quest-scoped warm context
    # keep working unchanged. Env: QAR_RECENT_CONTEXT_GLOBAL ("0"/"false" disables; read in
    # cli.py's _config_from_env).
    recent_context_global_enabled: bool = True


@dataclass
class OrchestratorResult:
    """What the loop produced. Exactly one terminal kind."""
    kind: str                          # "answer" | "deep" | "confirm" | "cancelled"
    text: Optional[str] = None         # for answer
    deep_results: List[DeepResult] = field(default_factory=list)   # for deep (1..N)
    goals: List[str] = field(default_factory=list)                 # the goal(s) run
    decision_id: Optional[str] = None  # for confirm (if an EscalationSink was provided)
    question: Optional[str] = None     # for confirm
    rationale: str = ""
    steps: int = 0
    gathered: List[Dict[str, Any]] = field(default_factory=list)
    partial: bool = False              # answer assembled before fully exploring
    # Durable per-turn EXECUTION FACTS (what mutating work ran + whether it succeeded/failed),
    # populated by the loop from deep results + EVENT_EXEC ticks. Used by the broken-promise guard
    # and available to consumers for their own auditing. None until the loop attaches it.
    execution_record: Optional["ExecutionRecord"] = None
    # Set True by the broken-promise guard when it rewrote the reply to be honest about an
    # overstated/unfulfilled claim. Distinct from ``partial`` (which it also sets), purely for tracing.
    claim_corrected: bool = False
    # The model id used for the final answer/deep step (set by the loop; None for confirm turns).
    model: Optional[str] = None
    # Total token counts across all LLM calls this turn (plan + answer). Populated by finish()
    # from provider.tokens_in / tokens_out when the provider supports tracking.
    tokens_in: int = 0
    tokens_out: int = 0
    # Why the loop exited. One of: "verified" | "max_turns" | "escalated_deep" | "read_budget" |
    # "unverified" | "deferred" (a queued deployment confirmed a deferred hand-off this turn; the
    # external runner verifies the real outcome out-of-band) |
    # "deep_met" | "deep_not_met" | "clarify" | "confirm" |
    # "overseer_answer_now" | "overseer_escalated_deep" | "overseer_escalated_human" (set when an
    # overseer signal decided the path) | "cancelled" (a caller-supplied ``cancel_check`` reported
    # the run was cancelled mid-execution; see ``kind == "cancelled"``).
    exit_reason: str = ""
    # The last goal-verification verdict: {"met": bool, "reason": str, "next_action": str, ...}.
    # Populated whenever _verify_goal ran. None when goal verification did not run this turn.
    goal_verdict: Optional[Dict[str, Any]] = None
    # Every minimal-intervention OVERSEER consultation this run, oldest first, each a dict
    # {"signal", "hint", "reason", "step"}. None when the overseer did not run (off / no consult).
    overseer_signals: Optional[List[Dict[str, Any]]] = None
    # Structured retrieval constraints parsed alongside the goal condition (spec v3, work package
    # C): {time_range, topic_terms, actor, content_kind}, any subset. None when nothing in the
    # message called for filtering (today's behavior) or Step 1 took the conversation-context path
    # (goal-condition derivation, which parses constraints, only runs for self-contained input).
    retrieval_constraints: Optional[Dict[str, Any]] = None
    # EXPLICIT execution-mode change the planner detected in the user's message this turn:
    # "enter_brainstorm" | "exit_brainstorm" | None (no signal / detection failed safe). Also
    # emitted live as EVENT_MODE_SIGNAL. The orchestrator does NOT persist a mode; the consumer
    # owns the latch (see OrchestratorConfig.execution_mode).
    mode_signal: Optional[str] = None
    # PER-IDEA THREADING (opt-in; see OrchestratorConfig.card_thread_enabled): the TOPIC CARD this
    # turn was assigned to, as ``CardThreadDecision.as_dict()``:
    # {"action": "continue"|"switch"|"new", "card_id": <id or None>, "label": <label or None>,
    # "raw": <what the planner emitted>, "fell_back": <True when the fail-safe fired>}. For a "new"
    # decision ``card_id`` is None: the CONSUMER creates (or dedupes onto) the card, since it owns
    # the store. Also emitted live as EVENT_CARD_THREAD. None when threading is off.
    card_thread: Optional[Dict[str, Any]] = None
    # Every narration line actually spoken THIS turn (ack + relayed rationale beats, oldest first),
    # when narration is on. Empty when narration is off/disabled or nothing was said. A caller that
    # wants the narrator's ack to stop reopening every turn with its own recent generic shape (see
    # ``Narrator`` / ``run(prior_narration=...)``) reads this back and passes the last few lines
    # forward as the next turn's ``prior_narration``. The orchestrator persists nothing itself.
    narration_said: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decision normalization (coerce a raw planner dict into a safe PlanDecision).
# ---------------------------------------------------------------------------

def normalize_decision(raw: Dict[str, Any], cfg: OrchestratorConfig) -> PlanDecision:
    # A provider's structured output is not guaranteed to be a dict: some models/SDKs return a LIST
    # (e.g. multiple tool calls, or a JSON array). Coerce to a dict so a stray shape degrades to a
    # safe "answer" instead of raising 'list' object has no attribute 'get' from the planner.
    if not isinstance(raw, dict):
        if isinstance(raw, list):
            raw = next((x for x in raw if isinstance(x, dict)), {})
        else:
            raw = {}
    action = (raw.get("action") or "answer").strip().lower()
    if action not in ("read", "answer", "deep", "confirm", "clarify"):
        action = "answer"

    reads_in = raw.get("reads") or []
    clean_reads: List[Dict[str, Any]] = []
    if isinstance(reads_in, list):
        for r in reads_in[: cfg.max_reads_per_step]:
            if isinstance(r, dict) and (
                r.get("grep") or r.get("rel_path") or r.get("query")
                or r.get("list_sources") or r.get("describe_source")
                or r.get("list_operations") or r.get("describe_operation")
                or r.get("list_guidance") or r.get("read_guidance")
                or r.get("cards") or r.get("card")
            ):
                clean_reads.append(r)

    tier = raw.get("model_tier")
    if isinstance(tier, str):
        tier = tier.strip().lower()
        _tier_alias = {"haiku": "fast", "sonnet": "balanced", "opus": "quality"}
        if tier in _tier_alias:
            tier = _tier_alias[tier]
        if tier not in TIERS:
            tier = None
    else:
        tier = None

    subs: List[str] = []
    for s in (raw.get("subquestions") or []):
        if isinstance(s, str) and s.strip():
            subs.append(s.strip())
    subs = subs[: cfg.max_subquestions]

    deep_subs: List[Dict[str, Any]] = []
    for d in (raw.get("deep_subtasks") or []):
        if isinstance(d, dict) and (d.get("goal") or d.get("brief")):
            deep_subs.append({"goal": d.get("goal") or None, "brief": d.get("brief") or None})
    deep_subs = deep_subs[: cfg.max_deep_subtasks]

    deferred_deep: Optional[Dict[str, Any]] = None
    deferred_raw = raw.get("deferred_deep")
    if deferred_raw and isinstance(deferred_raw, dict) and deferred_raw.get("goal"):
        deferred_deep = {
            "goal": deferred_raw.get("goal"),
            "brief": deferred_raw.get("brief"),
            "rationale": deferred_raw.get("rationale"),
        }

    clarification: Optional[Dict[str, Any]] = None
    clarif_raw = raw.get("clarification")
    if clarif_raw and isinstance(clarif_raw, dict) and clarif_raw.get("question"):
        clarification = {
            "question": clarif_raw.get("question"),
            "options": clarif_raw.get("options") or [],
            "allow_free_input": bool(clarif_raw.get("allow_free_input", False)),
        }

    # Mode signal: only parsed when the consumer opted into planner-detected mode signals
    # (``cfg.mode_signals_enabled``); with the flag off a stray ``mode_signal`` in the response
    # is ignored outright. When on, strictly validated -- anything but the two known values
    # (wrong type, empty, garbage, a hallucinated mode name) normalizes to None, so a detection
    # failure can never change the consumer's mode (fail-safe).
    mode_signal = raw.get("mode_signal") if cfg.mode_signals_enabled else None
    if not (isinstance(mode_signal, str)
            and mode_signal.strip().lower() in ("enter_brainstorm", "exit_brainstorm")):
        mode_signal = None
    else:
        mode_signal = mode_signal.strip().lower()

    # Card thread (per-idea threading): only read when the consumer opted in. Kept RAW here (the
    # string the planner emitted); ``core.card_thread.parse_card_thread`` resolves and fail-safes it
    # in run(), where the candidate ids that make an id "known" are in hand. A non-string is dropped
    # to None, which resolves to "continue the current card".
    card_thread_raw = raw.get("card_thread") if cfg.card_thread_enabled else None
    if not (isinstance(card_thread_raw, str) and card_thread_raw.strip()):
        card_thread_raw = None
    else:
        card_thread_raw = card_thread_raw.strip()

    return PlanDecision(
        action=action,
        reads=clean_reads,
        goal=raw.get("goal") or None,
        deep_brief=raw.get("deep_brief") or None,
        confirm_question=raw.get("confirm_question") or None,
        model_tier=tier,
        subquestions=subs,
        deep_subtasks=deep_subs,
        rationale=(raw.get("rationale") or "").strip(),
        deferred_deep=deferred_deep,
        answer_contains_work_to_execute=bool(raw.get("answer_contains_work_to_execute", False)),
        clarification=clarification,
        mode_signal=mode_signal,
        card_thread=card_thread_raw,
    )


# ---------------------------------------------------------------------------
# Rendering helpers (gathered observations -> prompt text / grounding block).
# ---------------------------------------------------------------------------

def _truncate_goal(goal: str, max_chars: int = 3900) -> str:
    """Truncate goal text to stay under Quest's 4000-char limit."""
    if len(goal) > max_chars:
        return goal[:max_chars] + " [truncated]"
    return goal


def _clarify_question_text(plan: "PlanDecision") -> str:
    """Render a planner ``clarify`` decision as ONE user-facing question (options folded in).

    Shared by the escalating path (``_run_clarify``, which parks it as a decision-request) and the
    brainstorm path (which is forbidden to escalate, so it puts the same question in the reply).
    """
    clarif = (plan.clarification or {}) if plan is not None else {}
    question = (clarif.get("question") or "").strip() or "Need your input to proceed"
    options = clarif.get("options") or []
    if options:
        question += "\n\nOptions:\n" + "\n".join(f"- {opt}" for opt in options)
        if clarif.get("allow_free_input", False):
            question += "\n\n(You can also provide custom input)"
    return question


# A decision's summary is stored by Quest as a goal CONDITION — a short, human-readable done-standard.
# Raw text (a verbose planner question, a deep brief, dumped gathered context) must NEVER be written
# there: it overflows Quest's 4000-char limit and reads as a wall of text instead of a decision ask.
# When a summary is long it is condensed by an LLM into a one or two sentence ask before escalation.
_CONCISE_DECISION_LIMIT = 600  # summaries longer than this are condensed before they reach Quest
_CONDENSE_DECISION_PROMPT = (
    "Rewrite the following into a SINGLE concise decision request for a human to approve or answer. "
    "At most 2 sentences, under 400 characters. State plainly what decision or input is needed; omit "
    "analysis, code, and background. Do NOT use em dashes (--); use a comma, a colon, or parentheses "
    "instead.\n\nTEXT:\n{text}"
)


# ---------------------------------------------------------------------------
# OUR OWN GOAL VERIFICATION (replaces Claude Code's /goal self-check).
# After a deep worker runs, the brain decides whether the done-standard is met — judged through the
# AI rep's lens (rep_preamble) and AGAINST the applicable GUIDANCE CARDS, which encode the quality
# standards the result must satisfy — and, when it is not met, what the next attempt should do. This
# is the primitive the goal loop iterates on.
# ---------------------------------------------------------------------------

VERIFY_GOAL_TOOL: Dict[str, Any] = {
    "name": "goal_verdict",
    "description": "Judge whether the worker's run met the done-standard at the quality bar; if not, say what to do next, whether more context is needed, and which model tier to use.",
    "input_schema": {
        "type": "object",
        "properties": {
            "met": {"type": "boolean",
                    "description": "True ONLY if the output gives concrete evidence the goal is fully "
                                   "satisfied AND meets the quality standards."},
            "reason": {"type": "string", "description": "One sentence: why it is or is not met."},
            "next_action": {"type": "string",
                            "description": "If not met: a SHORT, specific instruction for the next attempt "
                                           "(what to fix, what context/file to look at, or why it failed)."},
            "need_more_context": {"type": "boolean",
                                  "description": "True if the worker fell short because it did NOT have "
                                                 "enough context (it lacked a file, a prior message, or a "
                                                 "fact it needed). False if it had what it needed but did "
                                                 "the work wrong or incompletely."},
            "context_query": {"type": "string",
                              "description": "If need_more_context is true: a SHORT search query naming "
                                             "the missing context to pull (e.g. a file, topic, or term)."},
            "next_tier": {"type": "string",
                          "description": "Optional. The model tier the next attempt should use, one of: "
                                         "fast, balanced, quality, best (or haiku, sonnet, opus). Omit to "
                                         "keep the current tier. Raise it when the failure looks like a "
                                         "reasoning/capability gap."},
            "claims_unexecuted": {"type": "boolean",
                                  "description": "True if the output CLAIMS it completed a change "
                                                 "(edited a file, saved data, sent something, changed "
                                                 "configuration) that the EXECUTION RECORD does not "
                                                 "show succeeding. False when no such claim is made, "
                                                 "or every claimed change is backed by the record."},
        },
        "required": ["met"],
    },
}

VERIFY_GOAL_PROMPT = """\
You are verifying whether an autonomous worker MET a goal (a checkable done-standard) AT THE REQUIRED
QUALITY BAR. Judge strictly from the EVIDENCE in the worker's reported output.

Decide:
  - met=true ONLY if the output gives concrete evidence the done-standard is fully satisfied AND it
    meets the QUALITY STANDARDS below (it names the specific change/result it produced and that
    clearly matches the goal and the standards).
  - met=false if the output is vague, only describes a plan, is partial, hit a limit or error, falls
    short of the quality standards, or does not clearly satisfy the goal.

CRITICAL: Future intent is NOT a result. If the output says things like "Let me check", "I'm pulling
up", "I'll look at", "I'm going to", "I will now", "I'm searching", or any other phrase that describes
what the worker is ABOUT to do rather than reporting what it DID, set met=false. The worker must have
actually executed the task and reported the outcome -- not described its plan to do so.

CRITICAL: Absence of context can be the correct answer. If the goal asks about prior conversation
history, prior messages, or what was previously said, and the CONVERSATION HISTORY below confirms
there is no prior history (it is empty or shows only the current message), then an answer of
"there is no prior history" or "this is the start of the session" IS the complete and correct
answer -- set met=true. Do NOT set need_more_context=true when the history IS shown and it is
simply empty. Only set need_more_context=true when the history is ABSENT from the context
entirely (not shown at all) and the answer could not have known whether history exists.

{claims_rules}When met=false:
  - set next_action to a SHORT, specific instruction for the next attempt: what to fix, what context
    or file to look at next, or the likely reason it failed.
  - set need_more_context=true ONLY when the worker clearly lacked context it needed (a missing file,
    a prior message, an unknown fact that is NOT shown in the context below); then set context_query
    to a short search query naming that missing context. If the worker had what it needed but did the
    work poorly, leave need_more_context=false.
  - optionally set next_tier to a stronger model tier (fast, balanced, quality, best) when the
    failure looks like a reasoning or capability gap rather than missing context.
Do NOT use em dashes.

{persona}{standards}--- GOAL (done-standard) ---
{goal}

--- TASK BRIEF ---
{brief}

--- CONVERSATION HISTORY (what the worker had access to; empty means no prior turns exist) ---
{transcript}

{context}--- WORKER OUTPUT (what it reports it did) ---
{output}
"""

# ---------------------------------------------------------------------------
# INTENT-DIRECTIVE JUDGE (WS3): the ONE structured LLM call that decides the AMBIGUOUS band the
# cheap regex prefilter (_message_requests_change / message_change_signal_ambiguous) leaves
# undecided. See Orchestrator.judge_execution_directive. Kept tiny and app-agnostic: no org names,
# no examples baked from any one deployment's data.
# ---------------------------------------------------------------------------

INTENT_DIRECTIVE_TOOL: Dict[str, Any] = {
    "name": "execution_directive_verdict",
    "description": "Judge whether the user's message is a DIRECTIVE to actually make a change "
                   "(code, files, or data) right now, as opposed to a question, an exploration, an "
                   "opinion request, or a hypothetical.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_execution_directive": {
                "type": "boolean",
                "description": "True ONLY if the user is directing the assistant to actually make "
                               "the change now (a command, even a polite one: 'can you fix X', "
                               "'go ahead and add Y'). False if the message asks ABOUT something, "
                               "weighs options, or does not actually direct action.",
            },
            "reason": {"type": "string", "description": "One short sentence: why."},
        },
        "required": ["is_execution_directive"],
    },
}

INTENT_DIRECTIVE_PROMPT = """\
A cheap keyword prefilter could not confidently classify this user message. Judge whether it is a
DIRECTIVE to actually execute a change (code, files, or data) right now, as opposed to a question,
an exploration ("how would I..."), an opinion request, or a hypothetical.

Set is_execution_directive=true ONLY when the user is telling the assistant to make the change now
(a command, even a polite one: "can you fix X", "go ahead and add Y"). Set it false when the
message is asking ABOUT something, weighing options, or does not actually direct action.

Do NOT use em dashes.

--- USER MESSAGE ---
{message}

--- THE ASSISTANT'S DRAFT ANSWER THIS TURN (context only; judge the MESSAGE, not the answer) ---
{answer}
"""

# ---------------------------------------------------------------------------
# BRAINSTORM-RELEASE JUDGE: the ONE structured judgment that decides, on a LATCHED brainstorm turn,
# whether the user released the hold. It exists because that decision is too consequential to ride
# the planner call at the cheap planner tier: a cheap planner reads a bare subject-matter imperative
# ("create a goal called X and add it to my plan") as "the user is asking to proceed", releases the
# latch mid-turn, and the work executes in a conversation the user explicitly put on hold. Acting is
# not recoverable; holding is (one sentence from the user). So the exit gets its own call, at its own
# tier, whose fail-safe direction is HOLD. It runs at most ONCE per turn and ONLY while the latch is
# held, so normal turns cost exactly what they cost today. Generic and app-agnostic.
# ---------------------------------------------------------------------------

MODE_RELEASE_TOOL: Dict[str, Any] = {
    "name": "brainstorm_release_verdict",
    "description": "Judge whether the user's newest message RELEASES the brainstorm hold (tells the "
                   "assistant to stop holding back and start acting) rather than simply talking "
                   "about the work.",
    "input_schema": {
        "type": "object",
        "properties": {
            "release_brainstorm": {
                "type": "boolean",
                "description": "True ONLY when the user speaks about the HOLD itself and lifts it "
                               "('okay go ahead', 'we are done brainstorming', 'stop holding off'). "
                               "False for anything about the SUBJECT MATTER, including bare and "
                               "anaphoric imperatives ('create a goal called X', 'add that to my "
                               "plan', 'email her about it', 'do the thing we discussed', 'set that "
                               "up', 'just do it'), which is how people think out loud.",
            },
            "reason": {"type": "string", "description": "One short sentence: why."},
        },
        "required": ["release_brainstorm"],
    },
}

MODE_RELEASE_PROMPT = """\
This conversation is LATCHED in brainstorm mode: the user asked to think out loud, and nothing is
executed, changed, or scheduled until they release that hold. Judge ONE thing about their newest
message: does it RELEASE the hold?

The distinction that decides it:
  * Talking about the SUBJECT MATTER is NOT a release, not even in the imperative, and not even
    when the imperative points BACK at something already discussed. If the message says WHAT should
    happen (create it, add it, email her, book it, set it up, do the thing we talked about), that is
    a person thinking out loud about the thing, and it stays held: release_brainstorm = false. A
    bare, blunt or impatient imperative ("just do it", "handle it", "set that up") is still the
    user saying WHAT they want, not that the hold is over.
  * Agreeing with an IDEA is not a release either. Settling on a plan in the first person ("let us
    do it", "I love that, that is the plan") says the user likes the idea, not that the hold is
    lifted. False.
  * Releasing the hold means the user speaks to YOU about the HOLD itself: the brainstorming, the
    waiting, the holding back, or an explicit go-ahead ("go ahead", "go for it", "you can act now",
    "stop holding off", "we are done brainstorming"). Only that lifts it:
    release_brainstorm = true.

Contrasting examples:
  "Create a goal called Morning Pages and add it to my plan."  -> false (subject matter, phrased as
      an instruction; the user is still shaping the idea out loud)
  "Send her an email about it and book the room."              -> false (subject matter)
  "Do the thing we discussed."                                 -> false (an imperative pointing back
      at the work; it says WHAT they want, it does not lift the hold)
  "Set that up." / "Book it." / "Just do it."                  -> false (blunt subject-matter
      imperatives; still thinking out loud about the thing)
  "Let us do it."                                              -> false (first-person agreement with
      the idea; it does not tell you to stop holding back)
  "Okay, go ahead and do it now."                              -> true (an explicit go-ahead, spoken
      to you about your holding back; the hold itself is lifted)
  "We are done brainstorming. Act on what we discussed."       -> true (explicit release)
  "Stop holding off, make it happen."                          -> true (explicit release)

Unless the message unmistakably lifts the hold, answer false. Holding is recoverable in one
sentence (the user says go ahead and everything runs); acting is not.

Do NOT use em dashes.

--- THE USER'S NEWEST MESSAGE (judge THIS) ---
{message}

--- RECENT TRANSCRIPT (context only; earlier turns, most recent last) ---
{transcript}
"""

# Inserted into VERIFY_GOAL_PROMPT (the {claims_rules} slot) ONLY when an execution record is
# supplied, i.e. on answer turns with verify_claims on. The record is the authoritative list of
# mutating actions that actually ran this turn; the answering step itself can never change files,
# code, data, or configuration, so any completed-change claim not backed by the record is false.
VERIFY_CLAIMS_RULES = """\
CRITICAL, honesty of completion claims: the EXECUTION RECORD below is the authoritative list of
mutating actions that actually ran this turn (and whether each succeeded). The author of the output
CANNOT change files, code, data, or configuration itself; such changes only happen through the
recorded actions. If the output claims or implies it COMPLETED a change (edited or wrote a file,
saved data, sent something, applied configuration) that the record does not show as SUCCEEDED, set
met=false AND claims_unexecuted=true, and say in reason which claim is unbacked. An output that
makes no completed-change claim, or that honestly says the change has NOT been made yet, is fine on
this dimension (claims_unexecuted=false). Statements about history from before this turn are not
completion claims.

--- EXECUTION RECORD (mutating actions that actually ran this turn) ---
{record}

"""

# ---------------------------------------------------------------------------
# ASYNC POST-DEEP CONTEXT-CARD UPDATER (prepare for the FUTURE after a deep run).
# Two centralized prompts: (1) the instruction appended to a deep process's brief so its output ends
# with a machine-parseable "future context" section, and (2) the updater prompt that turns the run
# into a STRUCTURED set of card edits. Both forbid em dashes (hard brand rule). Generic: no org names.
# ---------------------------------------------------------------------------

# The exact delimiter line the deep worker must emit before its future-context bullets, and the
# prefix the parser slices on. Kept as one constant so the brief instruction and the parser agree.
FUTURE_CONTEXT_DELIMITER = "=== FUTURE CONTEXT (for similar requests by this user) ==="

# WHAT a worker should name, shared by both instructions below so the ask never drifts between the
# two channels. The expensive knowledge a deep run buys is WHERE THINGS LIVE in the environment it
# explored: rediscovering that is what the next run pays for twice. So the ask leads with the
# environment references (exact locations, entry points, what was ruled out) and treats prose as the
# last resort. Generic: an "environment" is whatever the worker explored (a repository, a corpus, a
# filesystem, a data store), and a "location" is whatever addresses a thing in it (a path, an id).
_FUTURE_CONTEXT_WHAT_TO_NAME = (
    "Name REFERENCES first, prose last. The costly thing to rediscover is WHERE THINGS LIVE in the "
    "environment you just explored, so lead with:\n"
    "- the exact LOCATION of each thing you relied on, written the way it is addressed in that "
    "environment (a file path exactly as it appears relative to the working directory, a collection "
    "or data source with its name AND id), one bullet each, plus what it holds;\n"
    "- the ENTRY POINTS: where this area starts, what calls what, which location to open first for "
    "work like this;\n"
    "- what you RULED OUT: places you searched that did not hold it, so nobody pays for that search "
    "twice;\n"
    "- stable facts and conventions that will still be true next time.\n"
    "Never write a bullet that only describes a thing without saying where it is. Do not invent a "
    "location you did not actually open."
)

# Appended to a deep process's brief ONLY when the async card updater is active (a card-update store
# + a provider are available and the toggle is on). A non-updating deployment never sees this block.
DEEP_FUTURE_CONTEXT_INSTRUCTION = (
    "\n\n--- PREPARE FUTURE CONTEXT ---\n"
    "After you finish the work above and write your normal result, END your output with a clearly "
    "delimited section that names context which would help a FUTURE similar request by THIS user. "
    "Start the section with this exact line on its own:\n"
    f"{FUTURE_CONTEXT_DELIMITER}\n"
    "Then list one bullet per line (start each line with '- ').\n"
    + _FUTURE_CONTEXT_WHAT_TO_NAME
    + "\nKeep each bullet short. If nothing is worth remembering, write the delimiter line followed "
    "by '- (none)'. Do not use em dashes; use a comma, a colon, or parentheses instead."
)

# The SAME ask, for a runner whose primary output is a STRICT FORMAT (generated code, JSON, a patch):
# appending a prose section there would corrupt the payload, so the bullets travel out-of-band in the
# runner's structured future-context field instead. Note it never names the delimiter: the worker must
# not emit it into a strict payload at all. Appended when the runner declares
# FUTURE_CONTEXT_VIA_FIELD, so a code generator is still ASKED for future context (it has the most
# reusable facts of any worker) and the card updater keeps learning from those turns.
DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION = (
    "\n\n--- PREPARE FUTURE CONTEXT (OUT OF BAND) ---\n"
    "Alongside the work above, name context which would help a FUTURE similar request by THIS user. "
    "Return it ONLY through the separate future-context field of your result, never inside your "
    "primary output: your primary output must stay a valid, strictly formatted payload with nothing "
    "appended to it. Give one short bullet per line (start each line with '- ').\n"
    + _FUTURE_CONTEXT_WHAT_TO_NAME
    + "\nAlso name the entities and schema you touched. If nothing is worth remembering, return "
    "'- (none)'. Do not use em dashes; use a comma, a colon, or parentheses instead."
)

# The updater's ONE LLM call. It is given the request, what executed, the parsed future-context
# section, and the user's CURRENT relevant cards, and must return a STRUCTURED edit plan as JSON.
CARD_UPDATE_TOOL: Dict[str, Any] = {
    "name": "card_edits",
    "description": "Return the context-card edits that best prepare for future similar requests by "
                   "this user, correcting stale prior items where needed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "description": "One entry per card to update or create (bounded; keep it minimal).",
                "items": {
                    "type": "object",
                    "properties": {
                        "card_id": {"type": "string",
                                    "description": "The id of an existing card to update, or a new "
                                                   "short slug to create one."},
                        "name": {"type": ["string", "null"],
                                 "description": "Optional new card name (re-embeds the card)."},
                        "description": {"type": ["string", "null"],
                                        "description": "Optional new card description (re-embeds)."},
                        "add": {
                            "type": "array",
                            "description": "Content items to ADD. PREFER a resolvable reference "
                                           "(a collection with name+id) over a copied snapshot; use "
                                           "a note ONLY when there is nothing external to point at.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string",
                                             "enum": ["collection", "file", "conversation",
                                                      "query", "note"]},
                                    "locator": {"type": "object",
                                                "description": "For collection: {name,id}. For "
                                                               "note: {text}. For file: {path}."},
                                    "why": {"type": "string"},
                                },
                                "required": ["type"],
                            },
                        },
                        "replace": {
                            "type": "array",
                            "description": "Corrections of existing items: each {item_id, item} "
                                           "where item is the corrected content item.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "item_id": {"type": "string"},
                                    "item": {"type": "object"},
                                },
                                "required": ["item_id", "item"],
                            },
                        },
                        "remove": {
                            "type": "array",
                            "description": "Ids of stale items to drop from the card.",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["card_id"],
                },
            },
        },
        "required": ["edits"],
    },
}

CARD_UPDATE_PROMPT = """\
You maintain a user's reusable CONTEXT CARDS so future similar requests start better-grounded.
A deep task just finished for this user. Decide a small, high-signal set of card edits that best help
a FUTURE similar request by THIS user, and correct any stale prior items you can see.

The VALUE of a card is its RESOLVABLE REFERENCES, not its prose. A card that has only a name and a
description but no reference items cannot pull fresh data later, so it is nearly useless. Capturing
the references is the MAIN job.

Rules:
  - Extract EVERY external source named in the FUTURE-CONTEXT or the executed work as a resolvable
    reference and put it in the card's "add" list:
      * a collection named with an id  -> {{"type": "collection", "locator": {{"name": "...", "id": "..."}}, "why": "..."}}
      * a file path                    -> {{"type": "file", "locator": {{"path": "..."}}, "why": "..."}}
    Add one item for EACH source named. Copy each locator EXACTLY as the work named it (a file path
    stays relative to the working directory if that is how it was given, so it still resolves next
    time), and put what the source holds, and why it mattered, in "why". Do NOT return a card whose
    "add" is empty when the future-context names collections or files.
  - PREFER references over copied text. Use a "note" ({{"locator": {{"text": "..."}}}}) ONLY for a
    durable fact with nothing external to point at.
  - Group related references onto ONE topical card. Set its "name"/"description" so it is easy to
    find later.
  - To UPDATE an existing card, reuse its exact card_id from CURRENT CARDS below; for something new,
    use a short new slug as card_id.
  - Correct a wrong or outdated existing item via "replace" (its item_id plus the corrected item),
    or drop it via "remove".
  - Keep the NUMBER of cards small, but when the FUTURE-CONTEXT names any collection, file, or
    durable fact, you MUST return at least one edit that captures it. An empty edits list is correct
    ONLY when the future-context is genuinely empty or purely transient (nothing reusable).
  - Do not use em dashes. Use a comma, a colon, or parentheses instead.

EXAMPLE: if the future-context says the dream journal is the collection "Dream Journal" (id col_123)
and stress is tracked in "Daily Mood" (id col_456), return:
  {{"edits": [
    {{"card_id": "dreams", "name": "Dreams and stress",
      "add": [
        {{"type": "collection", "locator": {{"name": "Dream Journal", "id": "col_123"}}, "why": "the user's dream entries"}},
        {{"type": "collection", "locator": {{"name": "Daily Mood", "id": "col_456"}}, "why": "daily stress levels to correlate"}}
      ]}}
  ]}}

--- THE USER'S REQUEST / GOAL ---
{request}

--- WHAT WAS EXECUTED (brief + result) ---
{executed}

--- FUTURE-CONTEXT THE WORKER FLAGGED ---
{future_context}

--- THIS USER'S CURRENT RELEVANT CARDS (id, name, and current items) ---
{current_cards}
"""


def _normalize_card_edits(raw: Any) -> List[Dict[str, Any]]:
    """Coerce a card-updater LLM result into a list of edit dicts. Accepts every shape models
    actually return: a ``{"edits": [...]}`` object, a BARE list of edit objects, a single
    ``{"card_id": ...}`` edit, or a list that contains a wrapper object. Returns [] on anything else.
    Never raises."""
    try:
        if isinstance(raw, dict):
            if isinstance(raw.get("edits"), list):
                return [e for e in raw["edits"] if isinstance(e, dict)]
            return [raw] if raw.get("card_id") else []
        if isinstance(raw, list):
            out: List[Dict[str, Any]] = []
            for el in raw:
                if isinstance(el, dict):
                    if isinstance(el.get("edits"), list):
                        out.extend(e for e in el["edits"] if isinstance(e, dict))
                    elif el.get("card_id"):
                        out.append(el)
            return out
    except Exception:  # noqa: BLE001
        pass
    return []


def _proposed_card_text(edit: Dict[str, Any]) -> str:
    """Build the text that REPRESENTS a would-be-created card from a card-updater edit, for the
    semantic-merge similarity check. Mirrors ``card_repository.card_embed_text`` (the canonical card
    embed text) WITHOUT importing it, so the brain keeps no dependency on a concrete adapter module:
    the topic NAME and DESCRIPTION, then each ``add`` item's ``why`` and any ``note`` text. Returns
    "" when there is nothing to match on (then the caller skips the merge and creates as before).
    Never raises."""
    try:
        parts: List[str] = []
        for _k in ("name", "description"):
            v = edit.get(_k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        for it in (edit.get("add") or []):
            if not isinstance(it, dict):
                continue
            why = it.get("why")
            if isinstance(why, str) and why.strip():
                parts.append(why.strip())
            if it.get("type") == "note":
                note_text = (it.get("locator") or {}).get("text")
                if isinstance(note_text, str) and note_text.strip():
                    parts.append(note_text.strip())
        return " ".join(parts).strip()
    except Exception:  # noqa: BLE001
        return ""


# STEP 1 (User Input Understanding): rewrite a short/anaphoric latest message into ONE
# self-contained instruction (a goal condition) using ONLY the provided conversation context.
# NO em dashes anywhere (hard brand rule). Reserved replies: MORE_CONTEXT_NEEDED / CLARIFY: <q>.
RESOLVE_REQUEST_PROMPT = """\
You resolve what a user's latest message means so downstream work targets the right thing.
You are given the CURRENT conversation (and sometimes OTHER past conversations that may be
unrelated), plus the user's latest message. Rewrite the latest message as ONE self-contained
instruction: a goal condition, a concrete CHECKABLE done-standard stating what would satisfy the
request (the single condition that means the request is fully addressed), not just a
pronouns-filled-in restatement.

Resolve references ("it", "that", "the first one", "the second one", "do it", "yes") against the
CURRENT conversation. When the CURRENT conversation contains the referent, resolve it: it lists
items and the message picks one by position or description ("the second one" = the second item it
listed), or it proposed an action and the message accepts it ("do it" = carry out that proposal).
Use an OTHER past conversation ONLY when the latest message clearly continues that specific thread
("like we discussed" / "from yesterday"). Never borrow a referent from an unrelated conversation
just because it happens to contain a list, an option, or a proposal.

Rules (check in order):
- If the referent is genuinely missing from the CURRENT conversation (for example "the third one"
  when fewer than three were offered, or "do it" / "go ahead" when nothing actionable was proposed),
  reply: CLARIFY: <one short question>. Do NOT invent or borrow a referent to avoid asking.
- If the latest message explicitly refers to another conversation or an earlier time and that thing
  is not in the provided context, reply exactly: MORE_CONTEXT_NEEDED
- Otherwise reply with ONLY the rewritten self-contained instruction, nothing else.
Do not use em dashes. Be concise and concrete.

{conv_context}

Latest user message: {user_message}
"""

# Fix 13: for a SELF-CONTAINED input (the cheap keyword check said it does NOT lean on conversation
# context), derive a concise, CHECKABLE done-standard from the message ALONE, no conversation history
# needed. This runs on EVERY turn that skips the context-fetch path above, so it must stay CHEAP (a
# fast/balanced tier, never "best") -- see ``_derive_goal_condition``.
#
# Query-aware retrieval routing (spec v3, work package C): this SAME call ALSO emits an OPTIONAL
# second line naming structured retrieval constraints (time_range/topic_terms/actor/content_kind),
# parsed by ``parse_goal_condition_reply``. No new LLM call -- one extra thing parsed from the same
# reply. Absent/unparseable constraints degrade to exactly today's behavior (goal_condition only).
DERIVE_GOAL_CONDITION_PROMPT = """\
Restate the user's message below as ONE short, concrete, CHECKABLE done-standard: the single
condition that would mean this request has been fully addressed. Do not add requirements the user
did not ask for, do not change what was asked, and do not invent extra steps. If the message is
already a clear, checkable done-standard as written (e.g. a plain question, an already-concrete
instruction), reply with it UNCHANGED.

Reply with ONE OR TWO LINES, nothing else:
  Line 1: ONLY the restated instruction. No preamble, no explanation. One sentence, plain and
    concrete. Do not use em dashes.
  Line 2 (OPTIONAL, only when it genuinely applies): if the message names a specific TIME PERIOD
    ("Wednesday", "last week", "yesterday", "this month"), a specific TOPIC, WHO it is about (you,
    a rep, the team), or a KIND OF CONTENT (tasks done, decisions, conversations, files), emit a
    single-line JSON object with any of these OPTIONAL keys (omit keys that do not apply; omit this
    whole line when NONE apply):
      "time_range": {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}} -- resolve relative expressions
        against CURRENT DATE below into concrete calendar dates.
      "topic_terms": ["..."] -- a few short topic/keyword terms named in the message.
      "actor": "me" | "rep" | "team" -- who the message is about, only when it names one.
      "content_kind": "tasks_done" | "decisions" | "conversations" | "files" -- the kind of content
        asked for, only when the message names one.
    Never invent a filter the message does not support. When unsure, omit line 2 entirely.

{now_block}

User message: {user_message}
"""

# The keys/values ``parse_goal_condition_reply`` accepts from the OPTIONAL constraints line above.
# Anything else (an unrecognized key, or a value outside these enums) is DROPPED, not passed
# through opaquely -- a hallucinated field must never reach a store as a real filter.
_CONSTRAINT_ACTOR_VALUES = frozenset({"me", "rep", "team"})
_CONSTRAINT_CONTENT_KIND_VALUES = frozenset({"tasks_done", "decisions", "conversations", "files"})


def _format_now_block(now: Optional[str]) -> str:
    """Render the CURRENT DATE line fed to ``DERIVE_GOAL_CONDITION_PROMPT`` so relative date
    expressions ("Wednesday", "last week") resolve against a real "today". ``now`` is an optional
    ISO date/datetime string; absent or unparseable falls back to the system clock (UTC), so
    relative-date resolution is always possible and this never blocks the turn."""
    dt = None
    if now:
        try:
            dt = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    return f"CURRENT DATE: {dt.strftime('%Y-%m-%d')} ({dt.strftime('%A')})"


def parse_goal_condition_reply(raw: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Split a ``DERIVE_GOAL_CONDITION_PROMPT`` reply into ``(goal_condition, constraints)``.

    The reply is normally just the restated instruction (line 1) -- ``constraints`` is None and
    behavior is byte-for-byte what it was before constraints existed. The reply MAY carry a second
    line with a single JSON object naming OPTIONAL structured retrieval constraints (see the
    prompt): ``time_range`` ({{"start", "end"}}), ``topic_terms`` (list of strings), ``actor``
    ("me"|"rep"|"team"), ``content_kind`` ("tasks_done"|"decisions"|"conversations"|"files").

    Best-effort and NEVER raises: malformed/missing JSON, or a JSON object with no recognized
    keys, yields ``constraints=None``. Only whitelisted keys/values survive parsing; anything else
    is dropped so a hallucinated field can never reach a store as a real filter."""
    text = (raw or "").strip()
    if not text:
        return "", None
    lines = text.splitlines()
    goal_condition = lines[0].strip()
    constraints: Optional[Dict[str, Any]] = None
    rest = "\n".join(lines[1:]).strip()
    if rest:
        try:
            match = re.search(r"\{.*\}", rest, re.DOTALL)
            obj = json.loads(match.group(0)) if match else None
        except Exception:  # noqa: BLE001 -- malformed constraints must never break the turn
            obj = None
        if isinstance(obj, dict):
            cleaned: Dict[str, Any] = {}
            time_range = obj.get("time_range")
            if isinstance(time_range, dict):
                start = time_range.get("start")
                end = time_range.get("end")
                if isinstance(start, str) or isinstance(end, str):
                    cleaned["time_range"] = {
                        "start": start if isinstance(start, str) else None,
                        "end": end if isinstance(end, str) else None,
                    }
            topic_terms = obj.get("topic_terms")
            if isinstance(topic_terms, list):
                cleaned_terms = [str(t).strip() for t in topic_terms if str(t).strip()]
                if cleaned_terms:
                    cleaned["topic_terms"] = cleaned_terms
            actor = obj.get("actor")
            if isinstance(actor, str) and actor.strip().lower() in _CONSTRAINT_ACTOR_VALUES:
                cleaned["actor"] = actor.strip().lower()
            content_kind = obj.get("content_kind")
            if (isinstance(content_kind, str)
                    and content_kind.strip().lower() in _CONSTRAINT_CONTENT_KIND_VALUES):
                cleaned["content_kind"] = content_kind.strip().lower()
            if cleaned:
                constraints = cleaned
    return goal_condition, constraints

# A message that carries no request to resolve: a greeting, a thanks, an acknowledgement.
#
# These exist because of what a cheap model does with "Hello" when asked to restate it as a
# done-standard: it stops restating and starts HELPING ("Hello. How can I help you with your Quests
# today?"). That answer-shaped string then became the turn's goal condition, which put it on the
# understanding channel as "Understood as: Hello. How can I help you..." AND injected it into the
# answer's context as the UNDERSTOOD REQUEST. A greeting has nothing to resolve, so the fix is not to
# ask: skip the LLM hop entirely and let the message stand as its own goal condition. One less round
# trip on the most common turn there is, and the echo cannot be generated in the first place.
_SMALL_TALK_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|hiya|howdy|sup|greetings|good\s+(morning|afternoon|evening|day)|"
    r"thanks|thank\s+you|thx|ty|ok|okay|k|cool|nice|great|awesome|got\s+it|understood|"
    r"bye|goodbye|see\s+you|good\s?night)"
    r"[\s!.,?~]*(there|everyone|team|again|so\s+much|a\s+lot)?[\s!.,?~]*$",
    re.IGNORECASE,
)


def is_small_talk(user_message: str) -> bool:
    """True for a bare greeting / thanks / acknowledgement: a message with no request in it.

    Deliberately conservative. It matches only a SHORT message that is nothing but the pleasantry,
    so "hi, can you create a habit for me" (a real request that opens with a greeting) does NOT
    match and still gets a proper goal condition.
    """
    text = (user_message or "").strip()
    if not text or len(text) > 40:
        return False
    return bool(_SMALL_TALK_RE.match(text))


# The contract for the UNDERSTANDING channel: the goal-condition calls (``_derive_goal_condition``
# and ``_understand_input``).
#
# These are the calls the first fix missed. They are NOT reply-producing, so REPLY_VOICE_SYSTEM is
# the wrong contract for them, but like the answer stage they were going out with NO system prompt at
# all, and a cheap model handed a bare user message with no role to play defaults to the one role it
# knows: assistant. It answers. That answer is then labelled "Understood as: ..." and shown to the
# person, which is the meta-echo they saw. This tells the model it is not in a conversation at all.
GOAL_CONDITION_SYSTEM = """\
You are a request analyser inside a system. You are NOT in a conversation, and nobody reads what you
write here. Your output is a machine-readable field, not a message.

Output ONE line: a short, concrete, checkable done-standard restating what the message asks for.

- Never answer the message, never greet, never offer help, never ask a question.
- Never write "Understood as", "The user wants", or any other preamble. Output the standard alone.
- If the message is already clear and concrete, output it unchanged.
- Never invent requirements the message does not contain.
- Never use an em dash.
"""


def restates_meaningfully(goal_condition: str, user_message: str) -> bool:
    """True when the resolved goal condition says something the raw message did not.

    The understanding event ("Understood as: ...") is internal, but a consumer may surface it in a
    details/debug view, so it should only fire when the resolution actually ADDS information. The
    old guard was a byte-for-byte ``!=``, so a cheap-tier restatement that merely re-punctuated or
    re-cased the message ("Hello" -> "Hello.") counted as a fresh understanding, and the person got
    their own word echoed back at them above the reply. Compare normalized text instead: same words
    in the same order means nothing was learned, so stay quiet.
    """
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()
    return norm(goal_condition) != norm(user_message)


# The voice contract for EVERY call that produces text the person actually reads.
#
# This is the ONE system prompt for the reply channel, and it exists because of a real class of bug:
# the answer stage used to be called with no system prompt at all, so the only instructions the model
# saw were the grounding/planning blocks, which are written ABOUT the person in the third person
# ("Answer the user's latest message...", "The user asked: ..."). A model with no voice contract
# mirrors the voice of its instructions. The reply then came back as internal machinery read aloud:
# an echo of the request ("Understood as: ..."), a third-person narration of the model's own plan
# ("The user expressed interest in ... I will create a habit titled ..."), and a recital of which
# files and cards were retrieved.
#
# The separation this enforces: the run emits internal material on its OWN typed channels
# (EVENT_UNDERSTANDING for the resolved goal condition, EVENT_CONTEXT for retrieval metadata and
# sources, EVENT_STATUS / narration EVENT_PARTIAL for progress, EVENT_PLAN for reasoning). Those
# stay machine-readable and are what a consumer shows in a debug/details surface. NONE of them may
# be restated inside EVENT_RESULT, which is the reply and nothing else.
#
# Pass it as ``system=`` on every reply-producing provider.answer() call. Do NOT append it as a
# user-role message: that is what let it blend into the third-person instruction voice before.
REPLY_VOICE_SYSTEM = """\
You are the assistant, talking directly to the person you are helping. What you write in this step is
delivered to them verbatim as a chat message, and it is the only thing they see. Everything else in
this turn (your reasoning, what you retrieved, what you are about to do) travels on separate internal
channels and is not yours to repeat.

Write ONLY the final reply.

Voice:
- First person for yourself ("I"), second person for them ("you"). Never call them "the user", and
  never describe yourself in the third person.
- Speak TO them, never ABOUT them, and never about the turn you are in.

Never include any of the following. Each one is internal machinery, and it reads as noise:
- Any echo or restatement of their message ("Understood as:", "You asked:", "The user wants:",
  "Request:"). Just reply.
- Narration of your reasoning, your plan, or your next action ("I will now create...", "First I will
  check...", "Let me search..."). State the outcome instead. If you cannot act in this step, say
  plainly what still needs to happen and who does it.
- Any account of what you read: file names, document or source titles, card names, source counts,
  "Sources reviewed", "Reviewing N sources", or retrieval metadata of any kind. Use what you read.
  Do not report that you read it. If one specific source genuinely matters to them, mention it in an
  ordinary sentence, never as a list of retrieval hits.
- Raw internal state: ids, tool names, goal conditions, or the headings of the material you were
  given (GROUNDING CONTEXT, UNDERSTOOD REQUEST, ACTUAL CONTENT READ, CONVERSATION CONTEXT,
  SUB-QUESTION, and the like). Those headings are addressed to you, not to them.
- Progress or status commentary ("Selected context for...", "Reviewing...", "Working on it").
- Preambles and sign-offs about the reply itself ("Here is my response:", "Hope that helps!").

Style:
- Lead with the substance. Your first sentence answers them.
- Concrete and concise. No filler.
- Never use an em dash. Use a comma, a colon, parentheses, or two sentences.
""" + "\n\n" + language_instruction()

# Synthesize the FINAL user-facing reply after a deferred deep run, grounding it in what the deep
# run ACTUALLY did/produced. Without this, a deferred-deep turn returns the pre-deep proposal (which
# typically reads as "shall I proceed?"), discarding the real deliverable the deep run created.
SYNTHESIZE_AFTER_DEEP_PROMPT = """\
You already DID the work the user asked for. Below is the user's request, the proposal you made
before doing it, and the ACTUAL OUTPUT/RESULT of the work you just carried out. Write the final
reply to the user.

Rules:
- Report and PRESENT what was actually done or produced, grounded in the RESULT below. This is the
  deliverable, not a plan to do it later. Do NOT say "I will", "I would recommend", "shall I", "let
  me know if you want me to", or ask permission to start work that is already finished.
- If the request asked you to PLAN/REVIEW/ANALYZE/DESIGN something, the RESULT contains that
  plan/review/analysis. Present it clearly and usefully to the user (organize it, keep the substance).
- If the request asked you to CHANGE/FIX/BUILD something, state concretely what was changed, then
  briefly note anything left open.
- Drop tool-runner noise (session logs, "Running…", internal markers). Keep only what helps the user.
- If the RESULT genuinely shows the work is incomplete, say plainly what is done and what remains
  with a concrete next step. Do not pretend completeness.
- Write in the assistant's own voice. Be concise and concrete. Never use em dashes.

=== USER REQUEST ===
{request}

=== YOUR PRE-WORK PROPOSAL (context only; may be obsolete now that the work is done) ===
{proposal}

=== ACTUAL RESULT OF THE WORK YOU JUST DID ===
{result}
"""

# Synthesize the reply after a CONFIRMED deferred hand-off (deferred_deep_queued deployments): the
# work was NOT executed this turn, it was queued as a background task, so the reply must report a
# hand-off, never a result. Only used after the enqueue is confirmed (a deferred deep result with
# met=True), so saying "queued" here is always honest.
SYNTHESIZE_AFTER_QUEUED_PROMPT = """\
You just handed the user's request off to run in the BACKGROUND: the work has been queued as its
own task (the hand-off record below is CONFIRMED), and the user will be told in this conversation
when it finishes. Write the reply to the user.

Rules:
- Say clearly that the work is now queued and will run in the background, and that you will
  report back in this conversation when it is done. Do not promise an exact finish time.
- Do NOT claim any of the queued work is already done, and do NOT present planned work as a
  result. The ONLY thing that has happened is the hand-off recorded below.
- If your draft reply below contains findings or answers part of the request from context you
  already gathered, keep that useful substance.
- Write in the assistant's own voice. Be concise and concrete. Never use an em dash.

=== USER REQUEST ===
{request}

=== YOUR DRAFT REPLY BEFORE THE HAND-OFF (context; keep any real findings) ===
{proposal}

=== CONFIRMED HAND-OFF RECORD (what was queued) ===
{result}
"""

# Pure acknowledgements / deferrals that carry NO standalone instruction — they only make sense
# against prior conversation. A whole message equal to one of these needs context to be understood.
_ACK_PHRASES = frozenset({
    "ok", "okay", "k", "yes", "yep", "yeah", "yup", "sure", "go ahead", "go for it",
    "do it", "please do", "do that", "do so", "continue", "proceed", "carry on",
    "same as before", "as before", "like before", "as we discussed", "as discussed",
    "sounds good", "go", "agreed", "approved", "confirm", "confirmed",
})
# Anaphora / reference words that point at something earlier in the conversation. A short message
# leaning on one of these WITHOUT a concrete noun cannot be understood on its own.
_ANAPHORA_RE = re.compile(
    r"\b(it|its|that|this|those|these|them|they|the first one|the second one|the last one|"
    r"the other one|the previous one|as we discussed|as discussed|like before|as before|"
    r"same as before)\b",
    re.IGNORECASE,
)

# A guidance card may state a model preference for its kind of work (e.g. a "model selection" card
# whose body contains "model: sonnet" or "preferred model: claude-opus-4-8"). This scans the
# applicable-guidance text for such a directive so the deep worker honors it. Returns None if absent.
_GUIDANCE_MODEL_RE = re.compile(
    r"(?:preferred[ _]+)?model\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]{1,60})", re.IGNORECASE)


def _guidance_model_pref(quality_standards: Optional[str]) -> Optional[str]:
    """Extract a model preference declared in the applicable guidance cards, or None. Never raises."""
    if not quality_standards:
        return None
    try:
        m = _GUIDANCE_MODEL_RE.search(quality_standards)
        if m:
            return m.group(1).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


# Imperative change/build verbs and bug/wrongness signals that mark a USER MESSAGE as a request to
# CHANGE something (code, files, or data), not just to be informed. Kept app-agnostic.
_CHANGE_VERBS = (
    r"fix|implement|build|refactor|add|remove|delete|change|update|edit|rewrite|apply|create|"
    r"make|migrate|rename|move|replace|configure|enable|disable|integrate|wire\s+up|hook\s+up|"
    r"set\s+up|ensure|adjust|correct|patch|resolve|handle|support|improve|optimize|"
    r"expand|collapse|toggle|show|hide|open|close|display|render|scroll|load|initialize|reset|"
    r"convert|transform|format|parse|extract|inject|wrap|unwrap|expose|attach|detach"
)
_CHANGE_VERB_RE = re.compile(r"\b(?:" + _CHANGE_VERBS + r")\b", re.IGNORECASE)
# Bug/wrongness descriptions ("it incorrectly X", "doesn't work", "should X but Y", "is broken").
_WRONGNESS_RE = re.compile(
    r"\b(?:bug|broken|incorrect(?:ly)?|wrong(?:ly)?|fail(?:s|ing|ed)?|error|"
    r"does\s*n['’]?t\s+work|do\s+not\s+work|not\s+working|is\s*n['’]?t\s+working|"
    r"should\b.{0,60}\bbut\b|instead\s+of)\b",
    re.IGNORECASE,
)
# A purely interrogative opener: when the message ASKS ABOUT something (information, explanation,
# or opinion), it wants an answer, not an edit -- even if it also mentions an action verb ("how
# would I add X?", "should we refactor Y?"). Used to avoid auto-executing a question as a task.
_INFO_QUESTION_RE = re.compile(
    r"^\s*(?:how|what|what['’]?s|why|which|who|whom|whose|when|where|explain|describe|summari[sz]e|"
    r"tell\s+me|walk\s+me\s+through|is\s+it|are\s+there|is\s+there|do\s+you|does\s+it|did\s+you|"
    r"would\s+it|could\s+we|should\s+(?:i|we|it)|do\s+we|is\s+it\s+possible)\b",
    re.IGNORECASE,
)
# A POLITE IMPERATIVE aimed at the assistant ("can you fix…", "could you add…", "please update…").
# This reads like a question but is really a COMMAND to perform the action -- keep treating it as a
# change request. Distinguishes "can you add a field" (do it) from "should we add a field?" (advise).
_POLITE_COMMAND_RE = re.compile(
    r"^\s*(?:please\b|(?:can|could|would|will)\s+you\b|i'?d\s+like\s+you\s+to\b|"
    r"i\s+(?:want|need)\s+you\s+to\b|let'?s\b|go\s+ahead\b)",
    re.IGNORECASE,
)


def _message_requests_change(message: Optional[str]) -> bool:
    """True iff the USER MESSAGE asks for a CHANGE to be made (code/files/data), not just info.

    Keyed off the STABLE user message rather than the (highly variable) answer text, because the
    cheap planner often misroutes an actionable request to "answer" and then only DESCRIBES the
    change. This is the reliable signal that the turn should have executed work. Conservative on
    QUESTIONS: an interrogative message that ASKS ABOUT something (information, explanation, or
    opinion) returns False even when it mentions an action verb ("how would I add X?", "should we
    refactor Y?"), so a question is never auto-escalated into a file-editing task. A polite
    imperative aimed at the assistant ("can you add…", "please fix…") is still a command and
    returns True. Never raises.
    """
    if not message or not message.strip():
        return False
    try:
        m = message.strip()
        has_verb = bool(_CHANGE_VERB_RE.search(m))
        has_wrongness = bool(_WRONGNESS_RE.search(m))
        if not (has_verb or has_wrongness):
            return False
        # A polite imperative directed at the assistant ("can you fix…", "please add…") IS a
        # command even though it is phrased as a question -- keep escalating it.
        if _POLITE_COMMAND_RE.search(m):
            return True
        # An interrogative message ASKS ABOUT something (explanation, status, opinion) -- answer
        # it, do not execute, even if it mentions a change verb ("how would I add X?", "should we
        # refactor Y?", "what would it take to fix Z?"). This is the fix for questions being
        # mishandled as tasks.
        if _INFO_QUESTION_RE.search(m):
            return False
        # A message ending in "?" reads as a question by default, not a command, unless it was
        # already caught above as a polite command directed at the assistant ("can you fix...?").
        # This also covers conversational/opinion questions that carry a change verb but no
        # "you"-directed phrasing or interrogative opener ("can we improve conversion here?",
        # "should we optimize this query?") -- previously these fell through to True here even
        # though they read as questions. Returning False does not silently drop them: a verb/
        # wrongness signal still makes ``message_change_signal_ambiguous`` true, so they land in
        # the one-shot LLM judgment band (``judge_execution_directive``) instead of being forced
        # into a task by regex alone. A bug statement or plain imperative with no "?" still
        # escalates via the plain ``return True`` below.
        if m.endswith("?"):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def message_change_signal_ambiguous(message: Optional[str]) -> bool:
    """True when ``message`` carries a cheap signal of an executable directive (a change verb or a
    wrongness description) but ``_message_requests_change`` still returned False for it -- because
    an interrogative opener or a bare "?" ending overrode the signal. This is the AMBIGUOUS band
    worth spending ONE structured LLM judgment on (see ``Orchestrator.judge_execution_directive``):
    a message with NO signal at all (e.g. "thanks!") never reaches here, so the judgment call is not
    spent on every regex miss, only on messages a human would find genuinely borderline ("how would
    I add X, go ahead and do it if you can"). Never raises."""
    if not message or not message.strip():
        return False
    try:
        m = message.strip()
        return bool(_CHANGE_VERB_RE.search(m) or _WRONGNESS_RE.search(m))
    except Exception:  # noqa: BLE001
        return False


def _answer_describes_unexecuted_work(text: Optional[str]) -> bool:
    """Check if answer contains EXECUTABLE work that should've been done.

    Returns True ONLY if the unexecuted work is something the AI CAN do.
    Returns False if work is user-dependent (needs input, confirmation, etc).

    Executable: "I need to update code", "I should modify X", etc.
    User-dependent: "I need your input", "Please provide", etc. (OK to describe).
    """
    if not text or not text.strip():
        return False
    try:
        import re
        # Patterns for work AI CAN execute (executable patterns)
        # Match "I/we/you need to", "the system needs to", "the code needs to", etc.
        executable = [
            # Explicit inability confession: "I cannot execute this task", "I can't run commands
            # or edit files here" — the answer itself admits the work was not done.
            r"(?:i|we)\s+(?:cannot|can'?t|am\s+not\s+able\s+to|was\s+unable\s+to)\s+"
            r"(?:execute|run|perform|complete|do|apply|write|edit|modify|create)",
            # "The system will need to execute/run/write ..." — deferring the work to
            # an unnamed executor instead of doing it.
            r"the\s+system\s+(?:will\s+)?(?:need|have)s?\s+to\s+"
            r"(?:execute|run|perform|write|update|modify|change|create|apply)",
            r"(?:i|we|the\s+(?:code|system|logic|field|implementation))\s+(?:need|should|must)\s+(?:to\s+)?(?:update|modify|change|fix|add|remove|delete|create|implement|edit)",
            r"(?:i|we)\s+(?:need|should|must)\s+(?:to\s+)?(?:update|edit|modify).{0,30}(?:code|file|logic|field|database|api|endpoint)",
            r"to\s+(?:fix|address|resolve)\s+this,?\s+(?:i|we)\s+(?:need|should|must)",
            r"to\s+resolve\s+this.*(?:i|we)\s+need\s+to\s+ensure",
            # "the fix/solution is to update X" — describing the change instead of applying it.
            r"the\s+(?:fix|solution)\s+is\s+to\s+(?:update|modify|change|fix|add|remove|delete|create|implement|edit)",
            # "this/it needs to be updated", "the code needs updating" — passive describe-not-do.
            r"(?:this|it|the\s+(?:code|logic|field|implementation))\s+(?:needs?|requires?)\s+(?:to\s+be\s+)?(?:updat|modif|chang|fix|add|remov|delet|creat|implement|edit)",
        ]

        # Patterns for work that NEEDS USER INPUT (don't escalate these)
        user_dependent = [
            r"(?:you|your|please)\s+(?:need|should|must|will need|could|would)",
            r"(?:i|we)\s+need\s+(?:your|the user'?s?)\s+(?:input|decision|feedback|confirmation|approval)",
            r"please\s+(?:provide|specify|confirm|decide|clarify|choose)",
            r"requires?\s+(?:your|user|human)\s+(?:input|decision|confirmation|approval)",
        ]

        # Check user-dependent patterns FIRST (higher priority)
        for pattern in user_dependent:
            if re.search(pattern, text, re.IGNORECASE):
                return False  # OK — work needs user input, don't force execution

        # Check executable patterns
        for pattern in executable:
            if re.search(pattern, text, re.IGNORECASE):
                return True  # Should escalate — AI can execute this
    except Exception:  # noqa: BLE001
        pass
    return False


def _parse_future_context(output: Optional[str]) -> str:
    """Extract the worker's FUTURE-CONTEXT section from a DeepResult.output. Returns "" when absent.

    The deep brief instructs the worker to END its output with the
    ``FUTURE_CONTEXT_DELIMITER`` line followed by bullet lines. This slices from the LAST occurrence
    of the delimiter to the end (the last one wins so a worker that echoes the instruction earlier
    does not confuse the parser), strips a trailing code fence, and returns the bullets verbatim.
    Robust to a missing/garbled section: any miss yields "". Never raises.
    """
    if not output or not isinstance(output, str):
        return ""
    try:
        idx = output.rfind(FUTURE_CONTEXT_DELIMITER)
        if idx < 0:
            return ""
        section = output[idx + len(FUTURE_CONTEXT_DELIMITER):]
        # Drop a trailing markdown fence if the worker wrapped the section.
        section = section.replace("```", "")
        return section.strip()
    except Exception:  # noqa: BLE001
        return ""


def _future_context_channel(runner: Any) -> str:
    """Which FUTURE-CONTEXT channel this deep runner declares (see adapters.FUTURE_CONTEXT_VIA_*).

    Read with ``getattr`` so DUCK-TYPED runners (the common case; most consumers never subclass
    ``DeepRunnerBase``) keep working: anything that does not declare a channel is a prose runner and
    gets exactly today's behaviour. Never raises.
    """
    try:
        ch = getattr(runner, "future_context_channel", FUTURE_CONTEXT_VIA_OUTPUT)
        return ch if ch in (FUTURE_CONTEXT_VIA_OUTPUT, FUTURE_CONTEXT_VIA_FIELD) \
            else FUTURE_CONTEXT_VIA_OUTPUT
    except Exception:  # noqa: BLE001 — a weird runner attribute must never break a deep run
        return FUTURE_CONTEXT_VIA_OUTPUT


def _normalize_future_context(res: DeepResult) -> DeepResult:
    """ONE seam, applied to EVERY DeepResult the moment a runner returns it: move the worker's
    FUTURE-CONTEXT bullets OUT of ``output`` and into ``future_context``.

    This is what makes payload corruption impossible by construction rather than by each consumer
    remembering to strip:

      * a FIELD-channel runner (code / JSON / patch generator) already put its bullets in
        ``future_context`` and left ``output`` a clean payload, so this is a no-op for it -- except
        that a worker which ignored the instruction and appended the delimiter anyway ALSO gets
        cleaned here, which is the whole point of doing it centrally;
      * an OUTPUT-channel (prose) runner ended its output with the delimited section: it is parsed
        into ``future_context`` and cut from ``output``.

    After this, ``output`` is the deliverable and ``future_context`` is the ONLY carrier of the
    bullets, so every downstream reader (the async card updater, the UI panel, the consumer's
    payload) reads exactly one place. Mutates and returns the same result object. Never raises.
    """
    try:
        if not isinstance(res, DeepResult):
            return res
        parsed = _parse_future_context(res.output)
        if not (getattr(res, "future_context", "") or "").strip() and parsed:
            res.future_context = parsed
        if parsed:
            # The delimiter is present in the payload: cut it, whatever the declared channel.
            res.output = _strip_future_context(res.output)
    except Exception:  # noqa: BLE001 — normalization must never break a deep run
        pass
    return res


def _deep_future_context(result: Any) -> str:
    """The FUTURE-CONTEXT bullets of one deep result, from the structured field with a fallback to
    parsing the output.

    The field is authoritative (``_normalize_future_context`` fills it at the runner seam). The
    parse fallback covers a DeepResult built OUTSIDE that seam (e.g. a consumer that constructs one
    directly, or an older runner reflected back through a queue), so the card updater keeps learning
    in those paths too. Never raises.
    """
    try:
        fc = (getattr(result, "future_context", "") or "").strip()
        return fc or _parse_future_context(getattr(result, "output", None))
    except Exception:  # noqa: BLE001
        return ""


def _strip_future_context(output: Optional[str]) -> str:
    """Remove the worker's FUTURE-CONTEXT section from output destined for the USER.

    An OUTPUT-channel deep brief asks the worker to END its output with the
    ``FUTURE_CONTEXT_DELIMITER`` line plus bullets, which the async card updater learns from. That
    section is internal plumbing for learning, NOT part of the deliverable, so it must never reach
    the user. This cuts from the LAST delimiter occurrence to the end (symmetric with
    ``_parse_future_context``, which reads from that same point) and trims trailing whitespace.
    Returns the output unchanged when the delimiter is absent.

    It is applied ONCE, at the runner seam, by ``_normalize_future_context`` -- so ``DeepResult.output``
    is already clean everywhere downstream. The remaining calls on the emit paths are a deliberate
    belt-and-braces net for a DeepResult that never passed through that seam. Never raises.
    """
    if not output or not isinstance(output, str):
        return output or ""
    try:
        idx = output.rfind(FUTURE_CONTEXT_DELIMITER)
        if idx < 0:
            return output
        return output[:idx].rstrip()
    except Exception:  # noqa: BLE001
        return output


def _future_context_for_display(results: Any) -> str:
    """Collect the FUTURE-CONTEXT bullets across deep results into ONE display string for the user.

    This is the same section the async card updater learns from (see ``_parse_future_context``),
    surfaced so a consumer can show it as an expandable "what I'll remember for next time" panel on
    a deep-output message. Drops the worker's "(none)" placeholder so an empty section yields "" (the
    UI then shows nothing). Returns "" when there is nothing worth showing. Never raises.
    """
    try:
        parts: List[str] = []
        for d in (results or []):
            section = _deep_future_context(d)
            if not section:
                continue
            # Drop pure "(none)" placeholders; keep real bullets verbatim.
            kept = [ln for ln in section.splitlines()
                    if ln.strip().lower().lstrip("- ").strip() not in ("(none)", "none", "")]
            if kept:
                parts.append("\n".join(kept))
        return "\n".join(parts).strip()
    except Exception:  # noqa: BLE001
        return ""


def _card_update_store(assembler: Any) -> Optional[Any]:
    """Return the underlying card store that exposes the card-update API, or None. Never raises.

    GENERIC capability detection (no hardcoded ``FileContextStore`` type check): an object is
    card-update-capable when it exposes callable ``update_card`` AND ``add_content``. The wired
    ``context_assembler`` may BE such a store, or may be a composite/hybrid that wraps one, so this
    unwraps the known wrapper shapes (a ``CompositeContextAssembler``'s ``_assemblers`` list, a
    ``HybridContextAssembler``'s ``_keyword``/``_vector`` arms) and returns the first capable inner
    store it finds. Returns None when nothing card-update-capable is reachable.
    """
    def _is_capable(obj: Any) -> bool:
        return bool(obj is not None
                    and callable(getattr(obj, "update_card", None))
                    and callable(getattr(obj, "add_content", None)))

    try:
        seen: set = set()
        stack: List[Any] = [assembler]
        while stack:
            obj = stack.pop()
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            if _is_capable(obj):
                return obj
            # Unwrap known wrapper shapes (duck-typed, so a custom composite with the same
            # attribute is handled too without importing concrete classes).
            inner = getattr(obj, "_assemblers", None)
            if isinstance(inner, (list, tuple)):
                stack.extend(inner)
            for attr in ("_keyword", "_vector", "_store", "_inner", "_delegate"):
                child = getattr(obj, attr, None)
                if child is not None:
                    stack.append(child)
    except Exception:  # noqa: BLE001
        return None
    return None


# Discovery specs return a CAPABILITY/SOURCE MENU (what the assistant CAN call or look in), not
# content to answer from. They inform the PLANNER's choice of action; they must NEVER be handed to
# the answer LLM as "content read for this answer" (that makes it answer FROM the menu instead of
# gathering real material — the "I have these operations, shall I run discovery?" failure mode).
# ``read_guidance`` is deliberately excluded: it returns actual instructions, which ARE grounding.
_DISCOVERY_SPEC_KEYS = ("list_operations", "list_sources", "list_guidance",
                        "describe_operation", "describe_source")


def _is_discovery_spec(spec: Any) -> bool:
    """True if a read spec only DISCOVERS capabilities/sources (a menu), not real content."""
    return isinstance(spec, dict) and any(spec.get(k) for k in _DISCOVERY_SPEC_KEYS)


def _is_discovery_obs(obs: Any) -> bool:
    """True if a gathered observation is a tagged discovery/capability listing (menu, not content)."""
    return isinstance(obs, dict) and bool(obs.get("discovery"))


def _render_gathered(gathered: List[Dict[str, Any]]) -> str:
    if not gathered:
        return "[]"
    parts: List[str] = []
    for obs in gathered:
        if not isinstance(obs, dict):  # defensive: a malformed observation must not crash a replan
            continue
        kind = obs.get("kind")
        if kind == "grep":
            hits = [h for h in (obs.get("hits") or []) if isinstance(h, dict)]
            head = [f"  {h.get('rel_path')}:{h.get('line_no')}: {h.get('line')}" for h in hits[:20]]
            more = "" if len(hits) <= 20 else f"\n  … (+{len(hits) - 20} more hits)"
            parts.append(
                f"GREP {obs.get('pattern')!r}"
                + (f" in {obs.get('scope')}" if obs.get("scope") else "")
                + f" → {len(hits)} hit(s):\n" + ("\n".join(head) if head else "  (none)") + more
            )
        elif kind in ("read", "query"):
            loc = obs.get("locator", "")
            if _is_discovery_obs(obs):
                # A capability/source MENU, labeled so the reader treats it as "what I could call",
                # not as facts gathered. (The answer path drops these entirely; see _grounding_block.)
                parts.append(
                    f"AVAILABLE CAPABILITIES [{loc}] — a MENU of what you can call or look in, NOT "
                    f"content and NOT an answer; you still need to read/grep/query the actual sources "
                    f"to gather facts before answering:\n{obs.get('text', '')}")
            else:
                parts.append(f"{kind.upper()} {obs.get('rel_path') or ''} [{loc}]:\n{obs.get('text', '')}")
        elif kind == "error":
            parts.append(f"(gather error: {obs.get('error')})")
    return "\n\n".join(parts)


def _summarize_observation(obs: Dict[str, Any]) -> str:
    """One-line summary of a single gathered observation (source/path + key finding), for the
    COMPRESSED older-context view fed to the planner on re-plan steps. Loses the verbatim body but
    keeps WHAT was looked at and WHAT it turned up, so the planner still knows the ground it covered
    and won't re-issue the same read. Never raises."""

    def _oneline(s: Any, limit: int = 160) -> str:
        text = " ".join(str(s or "").split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    if not isinstance(obs, dict):
        return _oneline(obs)
    kind = obs.get("kind")
    if kind == "grep":
        hits = [h for h in (obs.get("hits") or []) if isinstance(h, dict)]
        where = f" in {obs.get('scope')}" if obs.get("scope") else ""
        paths = sorted({h.get("rel_path") for h in hits if h.get("rel_path")})
        files = "" if not paths else " across " + ", ".join(list(paths)[:5]) + (
            f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
        return f"GREP {obs.get('pattern')!r}{where} → {len(hits)} hit(s){files}"
    if kind in ("read", "query"):
        loc = obs.get("locator", "")
        head = f"{kind.upper()} {obs.get('rel_path') or ''} [{loc}]".strip()
        return f"{head}: {_oneline(obs.get('text', ''))}"
    if kind == "error":
        return f"(gather error: {obs.get('error')})"
    return _oneline(kind)


def _oversee_worth_a_look(*, consecutive_reads: int, plan_repeats_prev: bool,
                          elapsed_seconds: float, max_elapsed_seconds: float,
                          gathered_chars: int, max_gathered_chars: int,
                          min_consecutive_reads: int, gate_repeat_plan: bool,
                          spend_fraction: float) -> bool:
    """FREE, non-LLM heuristic gate (Fix 12): decides whether hook A's current step looks risky
    enough to be worth waking the expensive overseer model at all, instead of consulting on a blind
    fixed cadence regardless of how the run is actually going. True when ANY cheap signal suggests
    drift, looping, or budget pressure:
      - ``consecutive_reads`` has crossed ``min_consecutive_reads`` (a stuck read loop), or
      - the plan repeats the previous step's action+goal verbatim (``plan_repeats_prev``, when
        ``gate_repeat_plan`` is enabled -- looping on the same idea), or
      - elapsed time OR gathered-read volume has crossed ``spend_fraction`` of its budget.
    Pure, never raises (a bad input just fails every check -> False -> skip the consult, safe)."""
    try:
        if consecutive_reads >= max(1, int(min_consecutive_reads)):
            return True
        if gate_repeat_plan and plan_repeats_prev:
            return True
        if max_elapsed_seconds and (elapsed_seconds / max_elapsed_seconds) >= spend_fraction:
            return True
        if max_gathered_chars and (gathered_chars / max_gathered_chars) >= spend_fraction:
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _recent_conversation_turns(conv_ctx_text: str, *, exclude: Optional[List[str]] = None,
                               max_turns: int = 3) -> List[str]:
    """Extract the last few PRIOR user turns from THIS conversation's rendered context block (the
    ``conv_ctx_text`` ``_understand_input`` builds), for the overseer digest's RECENT CONVERSATION
    section (Fix 5a). Restricted to the "CURRENT CONVERSATION" block only -- never "OTHER PAST
    CONVERSATIONS" (a genuinely different conversation is a different concept, out of scope here).
    Excludes any turn matching one of ``exclude`` (the current turn's own request, raw and/or
    resolved) so it is never duplicated against CURRENT USER REQUEST / RESOLVED AS. Never raises."""
    try:
        if not conv_ctx_text:
            return []
        marker = "=== CURRENT CONVERSATION ==="
        idx = conv_ctx_text.find(marker)
        if idx == -1:
            return []
        block = conv_ctx_text[idx + len(marker):]
        end_idx = block.find("\n=== ")
        if end_idx != -1:
            block = block[:end_idx]
        excl_norm = {" ".join(str(e or "").split()).strip().lower() for e in (exclude or []) if e}
        turns: List[str] = []
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line.upper().startswith("USER:"):
                continue
            turn = line[len("USER:"):].strip()
            if not turn:
                continue
            if " ".join(turn.split()).lower() in excl_norm:
                continue
            turns.append(turn)
        n = max(0, int(max_turns))
        return turns[-n:] if n else []
    except Exception:  # noqa: BLE001
        return []


def _prior_escalation_lines(prior_escalations: Optional[List[Dict[str, Any]]]) -> List[str]:
    """Format the caller-supplied ``prior_escalations`` history (Fix 7) into one-line strings for
    the digest's PRIOR ESCALATIONS THIS CONVERSATION section, e.g. "1: escalated to deep, outcome:
    deep_met". Tolerant of missing/odd keys. Never raises."""
    try:
        lines: List[str] = []
        for i, e in enumerate(prior_escalations or [], start=1):
            if not isinstance(e, dict):
                continue
            kind = str(e.get("kind") or "unknown")
            outcome = str(e.get("outcome") or e.get("exit_reason") or "unknown")
            lines.append(f"{i}: escalated to {kind}, outcome: {outcome}")
        return lines
    except Exception:  # noqa: BLE001
        return []


def _render_gathered_for_planner(gathered: List[Dict[str, Any]],
                                 recent_full: int, compress_over: int) -> str:
    """Leaner view of ``gathered`` for the PER-STEP PLANNER.

    The newest ``recent_full`` observations are rendered in FULL (same as ``_render_gathered``);
    everything older is collapsed to one-line summaries. To stay byte-for-byte identical for short
    runs, compression only kicks in once ``len(gathered) > compress_over`` (and only ever when there
    are genuinely older observations to compress, i.e. more than ``recent_full``). The full
    ``gathered`` is unaffected and is what the final ANSWER is still synthesized from."""
    if not gathered:
        return "[]"
    n = len(gathered)
    recent_full = max(0, recent_full)
    if n <= compress_over or n <= recent_full:
        return _render_gathered(gathered)
    older, recent = gathered[: n - recent_full], gathered[n - recent_full:]
    parts: List[str] = [
        f"--- EARLIER READS ({len(older)}), compressed to one line each "
        "(already covered — re-read only if you need the full body) ---"
    ]
    parts.extend(f"  • {_summarize_observation(o)}" for o in older)
    if recent:
        parts.append(f"--- MOST RECENT READS ({len(recent)}), in full ---")
        parts.append(_render_gathered(recent))
    return "\n\n".join(parts)


# A re-plan step re-sends the SAME transcript + context_view it already sent on step 1 (neither
# changes within one run). These reference notes stand in for them on later steps when
# ``planner_abbreviate_repeat_context`` is on, so the planner focuses on the NEW gathered
# observations without re-reading unchanged context. The wording tells the planner the content is
# unchanged and was already provided, so it doesn't treat the absence as "no context".
_REPLAN_TRANSCRIPT_REF = (
    "(unchanged since step 1 — the full recent transcript was provided then; "
    "nothing new has been added this turn)"
)
_REPLAN_CONTEXT_REF = (
    "(unchanged since step 1 — the full CONTEXT was provided then and has not changed; "
    "focus on the NEW gathered observations below)"
)


def _is_orchestrator_command(text: str) -> bool:
    """True if text is a JSON command structure (not user-facing text).

    Orchestrator commands like {"list_operations": true} or {"grep": "..."}
    should NEVER appear as result text. This detects them so they can be rejected.
    """
    if not text or not text.strip().startswith("{"):
        return False
    try:
        obj = json.loads(text.strip())
        if not isinstance(obj, dict):
            return False
        # Check for known orchestrator command keys
        orchestrator_keys = {
            "list_operations", "describe_operation", "list_sources", "describe_source",
            "grep", "rel_path", "query", "list_guidance", "read_guidance", "cards", "card"
        }
        return any(k in obj for k in orchestrator_keys)
    except (json.JSONDecodeError, ValueError, TypeError):
        return False


def _strip_discovery_section(context_view: str) -> str:
    """Remove the discovery/command documentation from context_view for the answer LLM.

    The discovery block (which documents commands like {"list_operations": true}) is for the
    PLANNER to understand what operations exist. The answer LLM should not see these examples,
    as it should produce text answers, not command structures. Preserve only the quest/data
    context pointers.
    """
    if not context_view:
        return ""
    # Find where "## Discovery" starts and remove everything from there onwards
    discovery_idx = context_view.find("## Discovery")
    if discovery_idx >= 0:
        # Keep everything before "## Discovery"
        return context_view[:discovery_idx].rstrip()
    return context_view


def _safe_event_title(cm: Dict[str, Any]) -> str:
    """A card title fit to leave the process, with the person's own words taken out of it.

    A conversation-history card is titled with the RAW USER TURN it came from (see
    ``core/turn_context_store.py``: ``{"title": cards[i]["user"], "adapter": "turn"}``). That title
    rode out on EVENT_CONTEXT and a consumer rendered it, so the retrieval panel replayed the chat
    back at the person: "Hi", "User: Hi...", "Hello". A retrieval listing says WHAT the run drew on;
    it must never quote it. Describe history cards instead of quoting them, and flatten every other
    title to one short line.
    """
    raw = re.sub(r"\s+", " ", str(cm.get("title") or "")).strip()
    adapter = str(cm.get("adapter") or "").strip().lower()
    if adapter == "turn" or re.match(r"^(user|assistant|ai|human)\s*:", raw, re.IGNORECASE):
        return "Conversation turn"
    if len(raw) > 80:
        return raw[:77].rstrip() + "..."
    return raw


def _project_sources_for_event(sources: List[Any]) -> List[Any]:
    """Project retrieval ``sources`` into a shape safe to stream. Never raises.

    An assembler's source entry is ``{"adapter", "label", "items"}``, and ``items`` can hold real
    content (a conversation turn, a matched passage), not just file paths. Only path-like items
    survive here; anything free-text collapses to a count. A consumer that wants to show "what the
    AI read" gets the labels and the counts, never the material itself.
    """
    out: List[Any] = []
    try:
        for src in (sources or []):
            if isinstance(src, str):
                out.append(src)
                continue
            if not isinstance(src, dict):
                continue
            items = src.get("items") or []
            kept = [
                it for it in items
                if isinstance(it, str) and "\n" not in it and len(it) <= 120 and " " not in it.strip()
            ]
            proj: Dict[str, Any] = {
                "adapter": src.get("adapter", ""),
                "label": src.get("label", ""),
                "item_count": len(items),
            }
            if kept:
                proj["items"] = kept
            out.append(proj)
    except Exception:  # noqa: BLE001 — projection must never break event emission
        return []
    return out


def _project_card_metadata_for_event(card_meta: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project assembled ``card_metadata`` into a LIGHTWEIGHT shape for EVENT_CONTEXT. Never raises.

    Each card's structured ``items`` now carry the full resolved ``text`` (and preview/locator),
    which is too heavy to stream in an event. This keeps the existing display fields (id, title,
    relevance_score, file_count, files, adapter) and summarizes the items as a count, per-type
    counts, lightweight per-item descriptors (id/type/why only), and any file paths, WITHOUT dumping
    each item's full text. Cards with no items keep exactly their prior shape.
    """
    out: List[Dict[str, Any]] = []
    try:
        for cm in (card_meta or []):
            if not isinstance(cm, dict):
                continue
            proj: Dict[str, Any] = {
                "id": cm.get("id", ""),
                "title": _safe_event_title(cm),
                "relevance_score": cm.get("relevance_score"),
                "file_count": cm.get("file_count"),
                "files": cm.get("files"),
                "adapter": cm.get("adapter", ""),
            }
            items = cm.get("items") or []
            if items:
                type_counts: Dict[str, int] = {}
                file_paths: List[str] = []
                light_items: List[Dict[str, Any]] = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    itype = it.get("type", "note")
                    type_counts[itype] = type_counts.get(itype, 0) + 1
                    light_items.append({
                        "id": it.get("id", ""),
                        "type": itype,
                        "why": it.get("why", ""),
                        "deliver": it.get("deliver", "paste"),
                    })
                    locator = it.get("locator") if isinstance(it.get("locator"), dict) else {}
                    path = locator.get("path")
                    if path and path not in file_paths:
                        file_paths.append(path)
                proj["item_count"] = len(light_items)
                proj["item_type_counts"] = type_counts
                proj["items"] = light_items  # id/type/why/deliver only, NOT the full item text
                if file_paths:
                    proj["item_file_paths"] = file_paths
            out.append(proj)
    except Exception:  # noqa: BLE001 — projection must never break event emission
        return card_meta or []
    return out


def grounding_context_layer(context_view: str) -> str:
    """The L2 CONTEXT layer for an answer: the grounding header + the discovery-stripped context.

    Split out of ``_grounding_block`` so the answer path can place this stable block in the cached L2
    layer (identical while the cards are unchanged) while the volatile gathered/instruction tail
    stays in L3. The answer LLM strips the planner-only discovery block (it should produce text, not
    JSON command structures).
    """
    answer_context_view = _strip_discovery_section(context_view)
    return "\n\n".join([
        "--- GROUNDING CONTEXT (INTERNAL: answer FROM this; never quote it, never name its sources, "
        "never mention that you read anything) ---",
        answer_context_view or "(none)",
    ])


def grounding_answer_tail(gathered: List[Dict[str, Any]], partial: bool) -> str:
    """The L3 volatile tail for an answer: the gathered content read this turn + the closing rules.

    Everything after the context layer: the actual-content-read section (this turn's mid-loop reads,
    which change every call), the best-effort caveat, and the ability/closing instructions.
    """
    parts: List[str] = []
    # Discovery/capability listings (list_operations/list_sources/describe_*) are a menu of what the
    # assistant COULD do, not material to answer from — drop them here so the answer grounds only on
    # real content. When nothing real was gathered, the section is omitted and the answer LLM will
    # honestly say it lacks context instead of answering from the operations menu.
    content_gathered = [o for o in (gathered or []) if not _is_discovery_obs(o)]
    if content_gathered:
        parts.append("\n--- ACTUAL CONTENT READ FOR THIS ANSWER (INTERNAL: same rule, use it "
                     "silently; do not list or count these items back to them) ---")
        parts.append(_render_gathered(content_gathered))
    if partial:
        parts.append(
            "NOTE: this is a BEST-EFFORT answer assembled before fully exploring; if the content "
            "above doesn't cover the question, say plainly that you'd need to dig further."
        )
    parts.append(
        "ABOUT YOUR OWN ABILITIES in this reply: you are in a read-and-answer step. You cannot "
        "edit files, run commands, or change any code, data, or configuration here; changes "
        "happen only through a separate execution run that the system launches after your reply. "
        "NEVER state that you made, wrote, applied, updated, or staged a change (in any tense), "
        "even if the conversation suggests a change was attempted before. If the request needs a "
        "change, say plainly that it has not been made yet and describe exactly what should be "
        "done; the system will execute it."
    )
    parts.append(
        "Now write the reply itself, and nothing but the reply: no restatement of what they asked, "
        "no account of your reasoning or of what you are about to do, no mention of the material "
        "above or where it came from. Answer their latest message grounded in the context above, "
        "and ONLY in the material "
        "about the SPECIFIC subject they named. Material about a sibling topic that merely shares a "
        "category word (a DIFFERENT evaluation, pipeline, project, or metric than the one asked "
        "about) is NOT on point: do not answer from it and do not silently switch to it. If the "
        "context covers only a different specific subject, say plainly you don't have material "
        "specifically about what was asked, name what you DID find, and offer to dig in or ask which "
        "they meant. If it doesn't cover something, say so plainly rather than inventing details. "
        "For a current-status or 'what's next' answer, rely on the most on-subject material and note "
        "the date or age of any dated item you use rather than presenting an old document as the "
        "present state."
    )
    return "\n\n".join(parts)


def _grounding_block(context_view: str, gathered: List[Dict[str, Any]], partial: bool) -> str:
    """The full grounding block (L2 context layer + L3 answer tail), for the non-layered path.

    Composed from ``grounding_context_layer`` + ``grounding_answer_tail`` so the flattened output is
    byte-for-byte what it was before the split, while the layered answer path can use the two pieces
    independently (context in the cached L2 layer, the rest in the volatile tail).
    """
    return grounding_context_layer(context_view) + "\n\n" + grounding_answer_tail(gathered, partial)


# ---------------------------------------------------------------------------
# The Orchestrator.
# ---------------------------------------------------------------------------

def _run_goal_accepts_emit(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts an ``emit`` keyword (directly or via **kwargs).

    Lets the orchestrator stream EXECUTION-lifecycle events to runners that opt in, while leaving
    older ``run_goal(*, goal, brief, model, max_turns)`` signatures untouched. Decided by signature
    inspection rather than a try/except so a runner with a side effect is never invoked twice.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "emit" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_context_preamble(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``context_preamble`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``: a per-task context preamble (e.g. an AI
    rep's pulled persona) is forwarded ONLY to runners that accept the kwarg, so older
    ``run_goal`` signatures keep working unchanged. Signature inspection, never try/except, so a
    runner with a side effect is never invoked twice.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "context_preamble" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_run_id(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``run_id`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``. When accepted, the orchestrator passes
    the subgoal's own stable ``task_uuid`` as ``run_id`` so every retry (each of which may spawn a
    brand-new underlying process/session) tags its EVENT_EXEC events with the SAME id — otherwise
    a consumer's dashboard renders each retry as a new, duplicate deep-run entry for what is really
    one ongoing subgoal.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "run_id" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_working_dir(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``working_dir`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``: a per-task working-directory override
    (e.g. a quest's synced folder, so the deep agent starts where that quest's real work lives —
    see ``quest_autopilot_design.md``'s execution-environment section) is forwarded ONLY to a
    runner that accepts it, leaving older ``run_goal`` signatures untouched.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "working_dir" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def provider_call_accepts_layers(fn: Any) -> bool:
    """Whether a provider's ``plan``/``answer`` accepts the ``layers`` cache-hint kwarg (or **kwargs).

    Same opt-in discipline the deep-runner helpers use: the orchestrator passes ``layers`` ONLY to a
    provider whose method accepts it, so an older ModelProvider (or a test stub) written before the
    layered cache surface existed keeps working unchanged via its plain ``prompt``/``messages`` path.
    Decided by signature inspection rather than try/except so an LLM call with side effects is never
    issued twice.
    """
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    for p in sig.parameters.values():
        if p.name == "layers" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


# --- WS1 reliability floor: configurable, loud timeouts (see docs/HANDS_FREE_QUEST_AI_DESIGN.md) --

def read_op_timeout_seconds() -> float:
    """Per-operation wall-clock budget for one mid-loop read/grep/query dispatched by
    ``Orchestrator._do_reads``. Env ``QAR_READ_OP_TIMEOUT_SECONDS`` (default 60, accepts a float);
    read fresh on every call, not cached, so a deployment (or a test) can change it without a
    restart. Before this existed, one slow retrieval adapter could wedge the whole turn with no
    signal at all; a timeout now reports plainly which operation stalled and for how long instead
    of silently looking like an empty "nothing found" result."""
    raw = os.getenv("QAR_READ_OP_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return value if value > 0 else 60.0


def intent_judge_timeout_seconds() -> float:
    """Wall-clock budget for the ONE structured intent-directive judgment call (see
    ``Orchestrator.judge_execution_directive``). Env ``QAR_INTENT_JUDGE_TIMEOUT_SECONDS`` (default
    8, accepts a float); read fresh on every call, not cached. A short cap is deliberate: this call
    only runs in the ambiguous band the regex prefilter left undecided, and must never be the reason
    a turn feels slow -- a timeout falls back to the regex verdict instead of blocking."""
    raw = os.getenv("QAR_INTENT_JUDGE_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 8.0
    try:
        value = float(raw)
    except ValueError:
        return 8.0
    return value if value > 0 else 8.0


def mode_release_timeout_seconds() -> float:
    """Wall-clock budget for the ONE structured brainstorm-release judgment (see
    ``Orchestrator.judge_brainstorm_release``). Env ``QAR_MODE_RELEASE_TIMEOUT_SECONDS`` (default 8,
    accepts a float); read fresh on every call, not cached. A timeout HOLDS the latch (the fail-safe
    direction), so a slow provider can delay a brainstorm turn by at most this budget and can never
    cause work to run in a conversation the user put on hold."""
    raw = os.getenv("QAR_MODE_RELEASE_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 8.0
    try:
        value = float(raw)
    except ValueError:
        return 8.0
    return value if value > 0 else 8.0


def describe_read_spec(spec: Dict[str, Any]) -> str:
    """Human-readable operation name for a read spec, mirroring the ``locator`` naming
    ``Orchestrator._exec_one_read`` gives its own Observations. Used only to name WHICH
    operation timed out, so a stall is reported as a named, diagnosable failure rather than an
    empty result."""
    if not isinstance(spec, dict):
        return "read"
    if spec.get("list_guidance"):
        return "list_guidance"
    if spec.get("read_guidance"):
        return f"read_guidance({spec['read_guidance']})"
    if spec.get("list_sources"):
        return "list_sources"
    if spec.get("describe_source"):
        return f"describe_source({spec['describe_source']})"
    if spec.get("list_operations"):
        return "list_operations"
    if spec.get("describe_operation"):
        return f"describe_operation({spec['describe_operation']})"
    if spec.get("cards") is not None:
        return f"cards({spec['cards']!r})"
    if spec.get("card") is not None:
        return f"card({spec['card']})"
    if spec.get("grep"):
        return f"grep({spec['grep']!r})"
    if spec.get("query") is not None:
        query = spec.get("query")
        text = query.get("text") if isinstance(query, dict) else query
        return f"query({text!r})" if text else "query"
    if spec.get("rel_path"):
        return f"read_section({spec['rel_path']})"
    return "read"


def context_assembly_timeout_seconds() -> float:
    """Wall-clock budget for the turn-start context-assembly background fetch (cards + recent +
    corpus consolidation, run concurrently with the instant ack). Env
    ``QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS`` (default 5.0, accepts a float); read fresh on every
    call so it can be tuned without a restart. A soft deadline slightly under this budget is
    threaded to the assembler via ``meta["assembly_deadline"]`` so a deadline-aware assembler
    (e.g. ``HybridContextAssembler``) returns whatever completed in time as a PARTIAL result
    (``AssembledContext.partial``) instead of blowing the whole budget. Only when not even a
    partial result lands in time does the collect below drop ALL fresh context for the turn --
    see the WARNING logged at the call site plus ``record_context_assembly_timeout`` for the
    counter this triggers."""
    raw = os.getenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        return 5.0
    return value if value > 0 else 5.0


def verify_context_max_chars() -> int:
    """Character cap on the context layer handed to goal verification (``Orchestrator._verify_goal``).

    See HANDS_FREE_QUEST_AI_DESIGN.md sections 4 and 6: the overseer/verifier should affordably see
    the SAME context layer the worker/answer call saw (cache-read pricing makes that cheap once the
    layer is already cached), so ``_verify_goal`` is handed the turn's rendered L2 context block
    straight through, unchanged. This cap is a safety valve for the rare case where that block is
    itself huge (a card layer with no upstream cap), not a normal-path limiter. Env
    ``QAR_VERIFY_CONTEXT_MAX_CHARS`` (default 24000, accepts an int); read fresh on every call so it
    can be tuned without a restart. A non-positive value disables the cap entirely."""
    raw = os.getenv("QAR_VERIFY_CONTEXT_MAX_CHARS")
    if raw is None or not raw.strip():
        return 24000
    try:
        value = int(raw)
    except ValueError:
        return 24000
    return value if value > 0 else 24000


def truncate_verify_context(text: str, max_chars: Optional[int] = None) -> str:
    """Cap a context block for goal verification, dropping only its TAIL when it is too large.

    Truncating the tail (never the head, never by summarizing) keeps the returned block a true
    byte-PREFIX of the untruncated one -- the same stable-prefix property the whole layering scheme
    depends on (``prompt_layers.PromptLayers.prefix``). ``max_chars`` defaults to
    ``verify_context_max_chars()``; a non-positive cap means "no limit". Returns ``text`` unchanged
    when it is empty or already within the cap. When truncated, appends a short note naming how much
    was cut, so the judge knows the context it saw was partial rather than assuming completeness."""
    if not text:
        return ""
    cap = verify_context_max_chars() if max_chars is None else max_chars
    if cap <= 0 or len(text) <= cap:
        return text
    dropped = len(text) - cap
    head = text[:cap].rstrip()
    return (head + f"\n\n[... context truncated for verification: {dropped} more "
            "characters omitted, earliest content kept ...]")


context_assembly_timeout_lock = threading.Lock()
context_assembly_timeout_count = 0


def record_context_assembly_timeout() -> int:
    """Thread-safe increment of the process-wide context-assembly-timeout counter (module-level
    because assembly runs on a background executor thread per turn, and turns run concurrently).
    No metrics backend is wired in this repo, so this is the observable marker a consumer can
    poll/export; it is paired with a WARNING log at the call site, never silent. Returns the new
    total count."""
    global context_assembly_timeout_count
    with context_assembly_timeout_lock:
        context_assembly_timeout_count += 1
        return context_assembly_timeout_count


class TurnCardCache:
    """One in-run, query-keyed cache of context assembly, shared by BOTH context paths this turn.

    The unified context primitive (docs/HANDS_FREE_QUEST_AI_DESIGN.md sec. 3): turn-start assembly
    and mid-loop ``{"cards": <query>}`` reads reach the SAME cards through ONE object, so a 5s
    turn-start timeout is no longer unrecoverable for the whole turn. Two mechanisms:

    * The turn-start eager pre-fetch registers its running ``Future`` here (``register_prefetch``).
      When that fetch TIMES OUT at turn start, the future is kept referenced (not cancelled), so if
      it lands late a later mid-loop read for the SAME query serves it from here WITHOUT re-running
      assembly.
    * A mid-loop read for a query with no pre-fetch runs a fresh, bounded ``assemble`` and caches it
      under the query, so repeats within the turn are free.

    Everything is best-effort and thread-safe (mid-loop reads run in ``_do_reads``' parallel pool):
    a lock guards the two dicts. No cross-turn persistence -- the cache dies with the run.
    """

    def __init__(self, assembler: Optional[Any], meta: Optional[Dict[str, Any]]):
        self.assembler = assembler
        self.meta = meta or None
        self.lock = threading.Lock()
        self.results: Dict[str, Any] = {}          # query -> AssembledContext (completed)
        self.futures: Dict[str, Future] = {}       # query -> in-flight assemble (may land late)
        self.executor: Optional[ThreadPoolExecutor] = None
        self.emit: Optional[Any] = None            # per-run event sink (set by run()); may stay None

    def register_prefetch(self, query: str, future: Future) -> None:
        """Register the turn-start eager pre-fetch future under its query. Never raises."""
        if not query or future is None:
            return
        try:
            with self.lock:
                self.futures[query] = future
        except Exception:  # noqa: BLE001
            pass

    def register_result(self, query: str, assembled: Any) -> None:
        """Record a completed AssembledContext so a same-query read is a pure cache hit. Never raises.

        Only a FULL result belongs here: a PARTIAL one (``assembled.partial``) would displace the
        fresh, deadline-free assemble a later mid-loop read could run to recover the full fuse --
        the caller uses ``discard_prefetch`` for that case instead of registering."""
        if not query or assembled is None:
            return
        try:
            with self.lock:
                self.results[query] = assembled
                self.futures.pop(query, None)
        except Exception:  # noqa: BLE001
            pass

    def discard_prefetch(self, query: str) -> None:
        """Drop a registered pre-fetch future without caching anything. Never raises.

        Used when the turn-start prefetch resolved to a PARTIAL result: that future is already
        done holding the partial, so leaving it registered would hand the same partial to every
        mid-loop read. Discarding it makes the next same-query read fall through to the fresh
        assemble path, which runs with the deadline-free ``self.meta`` and recovers the FULL
        result."""
        if not query:
            return
        try:
            with self.lock:
                self.futures.pop(query, None)
        except Exception:  # noqa: BLE001
            pass

    def assemble_for_query(self, query: str, timeout: float):
        """Assembled context for ``query`` plus a one-word ORIGIN ("cache" | "prefetch" | "fresh").

        Serves a completed result, else waits on a registered pre-fetch future (kept referenced even
        after a turn-start timeout), else runs a fresh bounded assemble. Raises ``TimeoutError`` when
        the wait exceeds ``timeout`` (the caller turns that into a NAMED timeout observation, never
        empty); the still-running future stays referenced so a later read can reuse it.

        A PARTIAL result (``assembled.partial``, e.g. a deadline-bounded turn-start prefetch that
        landed late) is served to THIS read -- better than nothing -- but never cached as the
        query's completed result: its future is dropped instead, so the NEXT same-query read falls
        through to a fresh assemble with the deadline-free ``self.meta`` and recovers the full fuse.
        """
        if self.assembler is None:
            return None, "none"
        with self.lock:
            if query in self.results:
                return self.results[query], "cache"
            future = self.futures.get(query)
            origin = "prefetch" if future is not None else "fresh"
            if future is None:
                if self.executor is None:
                    self.executor = ThreadPoolExecutor(max_workers=1)
                assembler = self.assembler
                meta = self.meta
                future = self.executor.submit(lambda: assembler.assemble(query, meta=meta))
                self.futures[query] = future
        assembled = future.result(timeout=timeout)  # may raise TimeoutError -> named error obs
        with self.lock:
            if not bool(getattr(assembled, "partial", False)):
                self.results[query] = assembled
            self.futures.pop(query, None)
        return assembled, origin

    def render_card(self, card_id: str, timeout: float):
        """One card's rendered content plus an ORIGIN ("card" | "unsupported" | "none"). Never the
        loop's job to know the store type: dispatched via getattr, so an assembler without the
        optional ``render_card`` returns ("unsupported"). Raises ``TimeoutError`` on a slow store."""
        if self.assembler is None:
            return None, "none"
        fn = getattr(self.assembler, "render_card", None)
        if not callable(fn):
            return None, "unsupported"
        meta = self.meta

        def call_render() -> Optional[str]:
            try:
                return fn(card_id, meta=meta)
            except TypeError:
                # An assembler whose render_card does not accept meta (older/stub): call positionally.
                return fn(card_id)

        with self.lock:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=1)
            future = self.executor.submit(call_render)
        return future.result(timeout=timeout), "card"

    def close(self) -> None:
        """Shut the fresh-assemble executor down non-blocking (a still-running late fetch finishes on
        its own; Python threads cannot be force-killed). Never raises."""
        try:
            if self.executor is not None:
                self.executor.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


class _Emitter:
    """Per-run event router. The orchestrator calls ``emit(...)`` / ``status(...)``; this
    forwards a ProgressEvent to the run's ProgressSink (chosen by Mode) and to the legacy
    ``status`` callback. It also exposes ``maybe_detach()`` so a LIVE run can drop to the
    background sink mid-flight (the live↔background handoff). The orchestrator NEVER decides
    what surfaces — it only emits; the sink decides.
    """

    def __init__(self, sink: Optional[ProgressSink], mode: Mode,
                 status_cb: Callable[[str], None],
                 detach_check: Optional[Callable[[], bool]] = None,
                 on_detach: Optional[Callable[[], None]] = None):
        self._sink = sink
        self.mode = mode
        self._status_cb = status_cb
        self._detach_check = detach_check
        self._on_detach = on_detach
        self._detached = False

    def emit(self, event: ProgressEvent) -> None:
        self.maybe_detach()
        if self._sink is not None:
            try:
                self._sink.update(event, self.mode)
            except Exception:  # noqa: BLE001 — a sink must never break the run
                pass

    def status(self, text: str) -> None:
        # Legacy status callback (kept for back-compat) + a STATUS progress event.
        try:
            self._status_cb(text)
        except Exception:  # noqa: BLE001
            pass
        self.emit(ProgressEvent(type=EVENT_STATUS, text=text))

    def maybe_detach(self) -> None:
        """If a detach was requested (consumer disconnected), switch to the background sink
        for the rest of the run, ONCE."""
        if self._detached or self._detach_check is None:
            return
        try:
            if self._detach_check():
                self._detached = True
                if self._on_detach is not None:
                    self._on_detach()
                self.mode = Mode.BACKGROUND
        except Exception:  # noqa: BLE001
            pass


# Cap on how many prior-turn narration lines a consumer's ``prior_narration`` can carry into a
# Narrator: bounds prompt growth from a long voice conversation to the last handful of beats, which
# is enough to stop the ack repeating its own recent pattern without re-reading a whole session.
_MAX_PRIOR_NARRATION = 8


class Narrator:
    """Conversational, single-train-of-thought progress narration for one turn.

    Unified, latency-free flow with TWO sources of beats, both emitted as EVENT_PARTIAL (shown live
    in the chat bubble, not persisted, and spoken on voice):

    * The FIRST beat (the instant ack) is one cheap call started with ``begin()`` and collected with
      ``flush_first()``, so it runs CONCURRENTLY with context assembly and adds no wall-clock latency
      to quick turns. It is grounded in the user's new message + recent conversation + persona, and
      claims no findings (nothing has been read yet), so it cannot fabricate.
    * Every later beat is the planner's OWN ``rationale``, written conversationally in the rep's
      voice in the planning call the orchestrator already makes (Approach B), and handed here via
      ``relay(line)``. ``relay`` does NO LLM call — it just emits the line — so per-step narration is
      free and never blocks the turn.

    Generation (for the ack only) uses the cheap planner tier and SPEAKS IN THE SELECTED REP'S
    PERSONA when one is given. HOW the ack narrates is overridable by the consumer via
    ``system_prompt`` (the persona is always layered on top). Every failure is swallowed: the turn
    never depends on narration.

    Cross-turn awareness (``prior_narration``): a fresh ``Narrator`` is built for every turn, so
    without help the ack has no memory of what it already said in EARLIER turns of the same
    conversation, only of ``transcript_tail`` (the actual message content). On a voice consumer that
    speaks every beat aloud, that gap is what makes consecutive turns each open with their own
    differently-worded but equally generic "let me look into that" / "searching for that now" line:
    each ack sounds fresh to the model, so it never learns to stop. The caller (a consumer that
    persists a short rolling history of what actually reached this conversation's audio) can pass
    those lines in via ``prior_narration``; they seed both the repeat-detector (``_is_repeat``) and
    the ack/relay prompts with an explicit instruction to go quiet on that pattern rather than vary
    the wording. ``_said`` (this turn's own beats) stays separate so a consumer reading it back after
    the turn (e.g. via ``OrchestratorResult.narration_said``) gets exactly what to persist forward,
    with no double-counting of the seed.
    """

    DEFAULT_SYSTEM = (
        "You are working on the user's request right now and you think out loud to them while you "
        "work, like a person reasoning through something as they do it. Speak as ONE continuous "
        "train of thought across the whole turn: each line CONTINUES the same thought and reacts to "
        "whatever just came in, the way new things occur to someone as they look closer. Keep each "
        "line to one short, natural, spoken sentence. No lists, no markdown, no greeting, no "
        "restating the request back, no sign-off. Stay light and human, never robotic status labels. "
        "Connect to what was last said or done with the user when it helps the moment feel "
        "continuous. Do NOT use em dashes; use a comma or a period. Only speak when you have a "
        "concrete, specific thing to name (an actual finding, a specific next step, a real detail) "
        "worth saying out loud; a generic line that just restates that you are working on it, "
        "searching, or checking something, without naming anything specific, is not worth saying "
        "twice in a row and is not worth saying again if you already said one like it earlier in "
        "this same conversation. If nothing fresh and specific is worth saying at this moment, reply "
        "with an empty string rather than repeat the shape of an earlier line."
    )

    def __init__(self, *, provider: Any, model: str, emit: "_Emitter",
                 persona: str = "", transcript_tail: str = "",
                 system_prompt: Optional[str] = None, enabled: bool = True,
                 prior_narration: Optional[List[str]] = None):
        self._provider = provider
        self._model = model
        self._emit = emit
        self._persona = (persona or "").strip()
        # Lines already spoken aloud in EARLIER turns of this conversation (consumer-supplied, most
        # recent last). Read-only here: used for repeat detection and shown to the model, but never
        # mutated or re-emitted, so this turn always answers "what have I ALREADY told this user, in
        # total" without re-narrating any of it.
        self._prior: List[str] = list(prior_narration or [])[-_MAX_PRIOR_NARRATION:]
        self._recent = (transcript_tail or "").strip()[-800:]
        self._system = (system_prompt or self.DEFAULT_SYSTEM).strip()
        self.enabled = bool(enabled and provider is not None)
        self._said: List[str] = []
        self._first_future: Optional[Any] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        # Guards _said + the emit call so the background first beat (emitted from its own thread the
        # instant it is ready) and a main-thread relay() beat never interleave their append+emit.
        self._lock = threading.Lock()

    def _gen(self, moment: str) -> Optional[str]:
        msgs: List[Dict[str, str]] = [{"role": "system", "content": self._system}]
        if self._persona:
            msgs.append({"role": "system", "content": "Speak as this persona:\n" + self._persona})
        user = ""
        if self._recent:
            user += f"The conversation so far (recent):\n{self._recent}\n\n"
        if self._prior:
            user += (
                "Lines you already said out loud in EARLIER turns of this same conversation "
                "(do not say another line in this same generic shape; only speak now if you have "
                "something concretely new to name):\n"
                + "\n".join(f"- {s}" for s in self._prior) + "\n\n"
            )
        if self._said:
            user += "What you've already said out loud this turn:\n" + "\n".join(self._said) + "\n\n"
        user += f"What just happened: {moment}\n\nSay the next line of your thinking (or empty)."
        msgs.append({"role": "user", "content": user})
        return self._provider.answer(msgs, model=self._model)

    def _say(self, line: Optional[str]) -> None:
        if not line:
            return
        # Defensive brand cleanup: never speak em dashes even if the model slips one in.
        line = line.strip().replace("—", ", ").replace(" -- ", ", ")
        if not line:
            return
        # Emit with a trailing space so consecutive beats (ack, then each per-step beat) read as
        # separate sentences when a consumer streams them into one bubble, instead of running
        # together ("...this.I'm pulling up..."). The clean line (no trailing space) is what we
        # store for dedup. A consumer that replaces the bubble on `done` discards the spacing anyway.
        # Locked: the ack beat emits from the background thread while relay() beats emit from the
        # main thread; the lock keeps append+emit atomic so the two never interleave.
        with self._lock:
            self._said.append(line)
            try:
                self._emit.emit(ProgressEvent(type=EVENT_PARTIAL, text=line + " ",
                                              data={"narration": True}))
            except Exception:  # noqa: BLE001 — narration must never break the run
                pass

    def begin(self, user_message: str) -> None:
        """Start the first beat concurrently AND emit it the instant it is ready.

        The background task generates the one-sentence ack and emits it ITSELF (``_gen_and_say``),
        so the instant response goes out the moment the model returns, never sequenced behind the
        main pipeline's context search or guidance selection. Ordering vs later beats is preserved
        by ``flush_first()`` (a join barrier the orchestrator runs before the planner loop emits any
        relay beat), so the ack always precedes the planner's rationale.
        """
        if not self.enabled:
            return
        moment = f"the user just sent a new message: {(user_message or '')[:300]}"
        try:
            self._executor = ThreadPoolExecutor(max_workers=1)
            self._first_future = self._executor.submit(self._gen_and_say, moment)
        except Exception:  # noqa: BLE001
            self._first_future = None

    def _gen_and_say(self, moment: str) -> None:
        """Generate the first beat and emit it immediately from this background thread.

        Backstopped by ``_is_repeat`` against ``_prior`` (this is the one beat with no earlier
        beats THIS turn to compare against, so without ``prior_narration`` it would never be
        caught): the ack is exactly the beat that repeats a generic "let me look into that" shape
        turn after turn when the consumer has no cross-turn memory to give it.
        """
        try:
            line = self._gen(moment)
            clean = (line or "").strip()
            if clean and self._is_repeat(clean):
                return
            self._say(clean)
        except Exception:  # noqa: BLE001 — narration must never break the run
            pass

    def flush_first(self) -> None:
        """Join barrier: wait for the background first beat to finish emitting before the planner
        loop emits any later (relay) beat, so the ack always comes first. The beat emits ITSELF the
        instant it is ready (see ``begin`` / ``_gen_and_say``), so this no longer emits — it only
        joins and cleans up. A timeout just proceeds (a very slow beat still emits when ready)."""
        if self._first_future is None:
            return
        try:
            self._first_future.result(timeout=5.0)
        except Exception:  # noqa: BLE001 — timeout / cancelled / provider error: skip
            pass
        finally:
            try:
                if self._executor is not None:
                    self._executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._first_future = None

    def relay(self, line: Optional[str]) -> None:
        """Emit a pre-composed beat (the planner's conversational rationale). No LLM call.

        Used for every beat after the instant ack: the planner already wrote this line in the
        rep's voice, so we just speak it. De-duplicated against what was already said this turn AND
        against ``_prior`` (earlier turns) so a planner that repeats itself across re-plan steps, or
        across separate turns of a longer voice conversation, doesn't echo.
        """
        if not self.enabled or not line:
            return
        clean = line.strip()
        if clean and self._is_repeat(clean):
            return
        self._say(clean)

    def _is_repeat(self, line: str) -> bool:
        """True if ``line`` repeats or near-repeats something already said, THIS turn or earlier.

        Catches not just exact echoes but paraphrases (the bug was six lines that all meant
        "looking up your marathon quest"): compare on a normalized word set and treat a high
        overlap as a repeat. Checked against ``_prior`` (earlier turns, consumer-supplied) as well
        as ``_said`` (this turn), since the same generic-filler repeat can happen either within one
        long turn or across the separate turns of one voice conversation. This is the backstop; the
        system/planner prompts (which are shown both lists) are the primary defense.

        A line that normalizes to NO content words at all (e.g. "Let me look into that for you",
        every word of which is stopworded away) is exactly the content-free "still searching/
        checking" filler users hear as "the same thing over and over": there is nothing left to
        word-overlap-compare, so without a special case it would never match anything and could
        repeat indefinitely. Treat it instead as a repeat the moment ANY earlier line (this turn or
        prior) was ALSO content-free, capping content-free filler to at most one per conversation.
        """
        all_prev = (*self._prior, *self._said)
        norm = self._norm(line)
        if not norm:
            return any(not self._norm(prev) for prev in all_prev)
        for prev in all_prev:
            p = self._norm(prev)
            if not p:
                continue
            if norm == p:
                return True
            shared = len(norm & p)
            # >=70% of the shorter line's words shared → same beat reworded.
            if shared and shared / min(len(norm), len(p)) >= 0.7:
                return True
        return False

    @staticmethod
    def _norm(line: str) -> frozenset:
        words = re.findall(r"[a-z0-9']+", line.lower())
        stop = {"the", "a", "an", "to", "of", "your", "you", "i", "i'm", "im", "and", "for",
                "on", "at", "is", "it", "this", "that", "let", "me", "now", "so", "what",
                "into", "look", "looking", "check", "checking", "see", "want"}
        return frozenset(w for w in words if w not in stop)


class Orchestrator:
    """The domain-free brain. Construct with adapters; call ``run`` per request."""

    def __init__(
        self,
        *,
        retrieval: RetrievalAdapter,
        provider: ModelProvider,
        registry: ModelRegistry,
        deep_runner: Optional[DeepRunner] = None,
        deep_runners: Optional[Dict[str, Any]] = None,
        deep_runner_classifier: Optional[Any] = None,
        escalation: Optional[EscalationSink] = None,
        config: Optional[OrchestratorConfig] = None,
        status: Optional[Callable[[str], None]] = None,
        vision_provider: Optional[ModelProvider] = None,
        vision_model: Optional[str] = None,
        context_assembler: Optional[ContextAssembler] = None,
        guidance: Optional[GuidanceProvider] = None,
        input_inbox: Optional["InputInbox"] = None,
        conversation_store: Optional[ConversationStore] = None,
        recent_context: Optional[RecentContextStore] = None,
    ):
        self.retrieval = retrieval
        self.provider = provider
        self.registry = registry
        self.deep_runner = deep_runner
        # Named deep-runner registry + classifier (both optional). When both are set, the
        # classifier selects a runner key for each goal. Falls back to deep_runner on any
        # failure or missing key. All existing behaviour is unchanged when either is absent.
        self.deep_runners: Dict[str, Any] = deep_runners or {}
        self.deep_runner_classifier: Optional[Any] = deep_runner_classifier
        # Generic conversation inbox for mid-run user messages. When wired, ``run`` auto-drains the
        # current conversation's pending messages between goal-loop steps, so ANY interface that
        # pushes to it (chat, Quest frontend, ...) gets mid-run message folding with no extra wiring.
        self.input_inbox = input_inbox
        self.escalation = escalation
        self.cfg = config or OrchestratorConfig()
        self._status = status or (lambda _msg: None)
        # The describer used for the image describe-fallback path (a non-vision answering model, or
        # a text-only provider like the keyless CLI). When None, ``prepare_attachments`` reuses the
        # answering provider/model if that is itself vision-capable; otherwise images degrade to
        # honest notes. A consumer wiring a keyless answering provider should pass a vision-capable
        # ``vision_provider`` here so chat images are transcribed rather than dropped.
        self.vision_provider = vision_provider
        # The model id the ``vision_provider`` should use to DESCRIBE images. Required whenever the
        # vision_provider is a DIFFERENT provider from the answering one: the answering model id is
        # foreign to the describer (e.g. a Gemini/tier-alias answer id handed to an Anthropic
        # describer would 404), so a consumer must name the describer's own model here. When None,
        # ``prepare_attachments`` falls back to the answering model id (correct only when the
        # describer IS the answering provider).
        self.vision_model = vision_model
        # Optional PRE-FLIGHT CONTEXT adapter (the fifth adapter role). When wired, assemble() is
        # called once before the loop to inject task-specific context, and record() is called
        # after the run completes as a best-effort write-back. Never raises in either direction.
        self.context_assembler = context_assembler
        # Optional USE-CASE-SPECIFIC INSTRUCTIONS adapter (the GuidanceProvider role). When wired,
        # the orchestrator pre-selects the cards most relevant to the user's message into an
        # "APPLICABLE GUIDANCE" block before planning, and the planner may list_guidance /
        # read_guidance on demand. Cards are opaque text. Never raises. None = today's behavior.
        self.guidance = guidance
        # Optional storage-agnostic CONVERSATION STORE (the User Input Understanding step). When
        # wired AND the caller passes a conv_id, the brain may pull a relevant slice of the current
        # (and related) conversation to rewrite a short/anaphoric message into a self-contained goal
        # condition before selecting context. Never raises. None = Step 1 is a no-op (zero latency).
        self.conversation_store = conversation_store
        # Optional WARM RECENT-CONTEXT store (see core/recent_context.py). When wired, ``run`` (and
        # each deep goal via ``_assemble_for_goal``) loads the cards recently selected across THREE
        # scopes -- this conversation, this quest, and always "global" -- (fast, no LLM), gates them
        # through a pure lexical relevance filter weighted by scope, and merges any survivors into
        # context_view so a follow-up turn (or a background task) is warm even before/without a
        # fresh assembly. It also remembers, PER CARD, which content items past turns found useful
        # for a similar input, ranking them to the front both when rendering a carried-over card and
        # as a hint to the consolidating LLM pass when a card is re-found by fresh assembly. Never
        # raises. None = exactly today's behavior (no recent-turn fallback).
        self.recent_context = recent_context

    def get_provider_for_model(self, model: str) -> ModelProvider:
        """Auto-detect and return the provider for a given model based on name prefix.

        Intelligently routes to the right provider for multi-provider setups:
        - claude-* → Anthropic provider
        - gemini-* or models/* → Gemini provider (models/* is Gemini's convention)
        - gpt-* → OpenAI provider
        Falls back to primary provider if no multi-provider setup or no match.
        """
        if not model:
            return self.provider

        model_lower = model.lower()
        providers = getattr(self.registry, "_providers", {}) or {}

        # Auto-detect based on model prefix (all model names start with provider family)
        if model_lower.startswith("claude"):
            # Anthropic Claude models: claude-opus-*, claude-sonnet-*, claude-haiku-*, etc.
            if "anthropic" in providers:
                log.debug(f"Routing claude model '{model}' to Anthropic provider")
                return providers["anthropic"]
        elif model_lower.startswith("gemini") or model.startswith("models/"):
            # Gemini models: gemini-3.5-*, gemini-1.5-*, or models/* (Gemini API convention)
            if "gemini" in providers:
                log.debug(f"Routing Gemini model '{model}' to Gemini provider")
                return providers["gemini"]
            else:
                log.warning(f"Gemini model '{model}' requested but Gemini provider not registered. Available: {list(providers.keys())}")
        elif model_lower.startswith("gpt"):
            # OpenAI models: gpt-4o, gpt-4-turbo, etc.
            if "openai" in providers:
                log.debug(f"Routing GPT model '{model}' to OpenAI provider")
                return providers["openai"]

        # Fallback to primary provider
        log.debug(f"Model '{model}' falling back to primary provider ({type(self.provider).__name__})")
        return self.provider

    # --- gather (parallel reads/greps/queries via the RetrievalAdapter) ------

    def _exec_one_read(self, spec: Dict[str, Any],
                       guidance_selected_ids: Optional[set] = None,
                       card_context: Optional["TurnCardCache"] = None) -> Optional[Observation]:
        if not isinstance(spec, dict):
            return None
        # CARD CONTEXT (the unified primitive, mid-loop): reach the SAME cards + assembly the
        # turn-start path uses, at any loop step. Handled BEFORE the retrieval-None guard because
        # these route through the ContextAssembler, not the RetrievalAdapter (see the two helpers).
        if spec.get("cards") is not None:
            return self.read_cards_context(str(spec["cards"]), card_context)
        if spec.get("card") is not None:
            return self.read_one_card(str(spec["card"]), card_context)
        # No retrieval adapter: gracefully report unsupported rather than crashing. The brain
        # can still answer from transcript/context_view; it just cannot ground on a corpus.
        if self.retrieval is None and not (
            spec.get("list_guidance") or spec.get("read_guidance")
        ):
            return Observation(kind="query", locator="corpus",
                               text="No retrieval adapter configured — corpus grounding unavailable.")
        try:
            # GUIDANCE discovery (the GuidanceProvider role) — dispatched via getattr/None-guard so
            # an orchestrator with guidance=None returns a benign Observation, never raises. Both
            # flow into ``gathered`` as Observation(kind="query"), the SAME path as any read.
            if spec.get("list_guidance"):
                if self.guidance is None:
                    return Observation(kind="query", locator="list_guidance",
                                       text="No guidance is available for this assistant.")
                try:
                    cards = self.guidance.list() or []
                except Exception:  # noqa: BLE001 — a provider must never break the loop
                    cards = []
                if not cards:
                    return Observation(kind="query", locator="list_guidance",
                                       text="No guidance cards are available.")
                lines = [f"- {c.id}: {c.title} — applies when: {c.relevance}" for c in cards]
                return Observation(kind="query", locator="list_guidance",
                                   text="AVAILABLE GUIDANCE (read_guidance by id for the full "
                                        "instructions):\n" + "\n".join(lines))
            if spec.get("read_guidance"):
                card_id = str(spec["read_guidance"])
                if self.guidance is None:
                    return Observation(kind="query", locator=f"read_guidance({card_id})",
                                       text="No guidance is available for this assistant.")
                # De-dupe: a card already pre-selected into APPLICABLE GUIDANCE this turn is
                # already in front of the model — point back to it instead of re-injecting it.
                if guidance_selected_ids and card_id in guidance_selected_ids:
                    return Observation(
                        kind="query", locator=f"read_guidance({card_id})",
                        text=f"Guidance {card_id!r} was already provided above under "
                             "APPLICABLE GUIDANCE; refer to it there.")
                try:
                    card = self.guidance.read(card_id)
                except Exception:  # noqa: BLE001
                    card = None
                if card is None:
                    return Observation(kind="query", locator=f"read_guidance({card_id})",
                                       text=f"No guidance card with id {card_id!r}.")
                return Observation(
                    kind="query", locator=f"read_guidance({card_id})",
                    text=f"GUIDANCE: {card.title}\n(applies when: {card.relevance})\n\n{card.body}")
            # Discovery specs first — dispatched via getattr so a structural adapter that
            # predates the discovery methods degrades to a benign "unsupported" Observation
            # instead of raising (back-compat: the four methods are optional on the Protocol).
            if spec.get("list_sources"):
                fn = getattr(self.retrieval, "list_sources", None)
                return fn() if fn else Observation(
                    kind="query", locator="list_sources",
                    text="discovery not supported by this adapter")
            if spec.get("describe_source"):
                name = str(spec["describe_source"])
                fn = getattr(self.retrieval, "describe_source", None)
                return fn(name, path=spec.get("describe_path") or None) if fn else Observation(
                    kind="query", locator=f"describe_source({name})",
                    text="discovery not supported by this adapter")
            if spec.get("list_operations"):
                fn = getattr(self.retrieval, "list_operations", None)
                return fn() if fn else Observation(
                    kind="query", locator="list_operations",
                    text="discovery not supported by this adapter")
            if spec.get("describe_operation"):
                name = str(spec["describe_operation"])
                fn = getattr(self.retrieval, "describe_operation", None)
                return fn(name) if fn else Observation(
                    kind="query", locator=f"describe_operation({name})",
                    text="discovery not supported by this adapter")
            if spec.get("grep"):
                return self.retrieval.grep(
                    str(spec["grep"]), scope=spec.get("scope") or None
                )
            if spec.get("query") is not None:
                # Planner specs nest the query params under "query" ({"query": {"text": ...},
                # "time_range": ...}), but adapters read params at the TOP level of the dict
                # they receive (text/collection/...). Flatten the nested params over the spec
                # (nested keys win) so both shapes reach every adapter; sibling constraint keys
                # (time_range, topic_terms, actor, content_kind) stay intact. Without this, an
                # adapter expecting top-level "text" saw only the nested dict and the planner's
                # semantic-search step silently returned an error observation every time.
                payload = dict(spec)
                if isinstance(spec["query"], dict):
                    payload.update(spec["query"])
                return self.retrieval.query(payload)
            if spec.get("rel_path"):
                return self.retrieval.read_section(
                    str(spec["rel_path"]),
                    start_line=spec.get("start_line"),
                    end_line=spec.get("end_line"),
                    heading=spec.get("heading"),
                    max_bytes=self.cfg.max_gather_chars,
                )
        except Exception as e:  # noqa: BLE001 — a bad spec must never break the loop
            log.warning(f"Read spec execution failed: {type(e).__name__}: {e}", exc_info=True)
            return Observation(kind="error", error=type(e).__name__)
        return None

    # --- CARD CONTEXT reads: the unified primitive reachable at any loop step ------------------

    def read_cards_context(self, query: str,
                           card_context: Optional["TurnCardCache"]) -> Observation:
        """Run card/topic context assembly for ``query`` mid-loop, through the SAME assembler the
        turn-start path uses. Serves from the shared in-run cache (incl. a late-landing turn-start
        pre-fetch) when possible, else runs a fresh bounded assemble. A timeout or failure returns a
        NAMED error observation (never empty), matching the WS1 discipline. Never raises."""
        query = (query or "").strip()
        if not query:
            return Observation(kind="query", locator="cards",
                               text="No query text was given for the cards read.")
        if card_context is None or card_context.assembler is None:
            return Observation(
                kind="query", locator=f"cards({query!r})",
                text="No context assembler is configured, so topic/card context is unavailable.")
        timeout = read_op_timeout_seconds()
        try:
            assembled, origin = card_context.assemble_for_query(query, timeout)
        except FuturesTimeoutError:
            return Observation(
                kind="error", locator=f"cards({query!r})",
                error=f"Card context assembly for {query!r} timed out after {timeout:.0f}s")
        except Exception as e:  # noqa: BLE001 — a mid-loop read must never break the loop
            log.warning(f"Mid-loop cards read failed: {type(e).__name__}: {e}", exc_info=True)
            return Observation(kind="error", locator=f"cards({query!r})",
                               error=f"{type(e).__name__}: {e}")
        text = (getattr(assembled, "context_view", "") or "").strip() if assembled is not None else ""
        card_meta = list(getattr(assembled, "card_metadata", None) or []) if assembled is not None else []
        sources = list(getattr(assembled, "sources", None) or []) if assembled is not None else []
        self.emit_context_event_midloop(card_context, query, card_meta, sources, origin)
        if not text:
            return Observation(
                kind="query", locator=f"cards({query!r})",
                text=f"No topic/card context found for {query!r} "
                     f"(assembly ran via {origin}, found nothing).")
        return Observation(kind="query", locator=f"cards({query!r})", text=text)

    def read_one_card(self, card_id: str,
                      card_context: Optional["TurnCardCache"]) -> Observation:
        """Fetch ONE card's rendered content by id mid-loop, via the assembler's optional
        ``render_card``. Unsupported store / absent card / timeout each return a NAMED observation
        (never empty). Never raises."""
        card_id = (card_id or "").strip()
        if not card_id:
            return Observation(kind="query", locator="card",
                               text="No card id was given for the card read.")
        if card_context is None or card_context.assembler is None:
            return Observation(kind="query", locator=f"card({card_id})",
                               text="No context assembler is configured, so card fetch is unavailable.")
        timeout = read_op_timeout_seconds()
        try:
            text, origin = card_context.render_card(card_id, timeout)
        except FuturesTimeoutError:
            return Observation(kind="error", locator=f"card({card_id})",
                               error=f"Fetching card {card_id!r} timed out after {timeout:.0f}s")
        except Exception as e:  # noqa: BLE001 — a mid-loop read must never break the loop
            log.warning(f"Mid-loop card read failed: {type(e).__name__}: {e}", exc_info=True)
            return Observation(kind="error", locator=f"card({card_id})",
                               error=f"{type(e).__name__}: {e}")
        if origin == "unsupported":
            return Observation(
                kind="query", locator=f"card({card_id})",
                text="This assistant's context store does not support fetching a single card by "
                     "id; use a cards read (a query string) to retrieve topic context instead.")
        text = (text or "").strip()
        if not text:
            return Observation(kind="query", locator=f"card({card_id})",
                               text=f"No context card with id {card_id!r}.")
        self.emit_context_event_midloop(card_context, card_id, [{"id": card_id}], [], "card")
        return Observation(kind="query", locator=f"card({card_id})", text=text)

    def emit_context_event_midloop(self, card_context: Optional["TurnCardCache"], query: str,
                                   card_meta: List[Dict[str, Any]],
                                   sources: List[Dict[str, Any]], origin: str) -> None:
        """Emit EVENT_CONTEXT for a mid-loop card read, marked ``midloop`` so a consumer can tell it
        apart from turn-start assembly (docs sec. 3, point 4). Best-effort; never raises."""
        emit = getattr(card_context, "emit", None) if card_context is not None else None
        if emit is None:
            return
        try:
            card_meta_light = _project_card_metadata_for_event(card_meta or [])
            sources_light = _project_sources_for_event(sources or [])
            count = len(card_meta or [])
            text = (f"Fetched {count} context card{'s' if count != 1 else ''} mid-turn."
                    if count else "")
            emit.emit(ProgressEvent(
                type=EVENT_CONTEXT,
                text=text,
                data={
                    "card_metadata": card_meta_light,
                    "sources": sources_light,
                    "card_count": count,
                    "source_count": len(sources or []),
                    # The marker distinguishing a mid-loop card read from turn-start assembly.
                    "midloop": True,
                    "origin": origin,
                    "internal": True,
                }))
        except Exception as e:  # noqa: BLE001
            log.debug(f"Failed to emit mid-loop EVENT_CONTEXT: {e}", exc_info=True)

    def _do_reads(self, reads: List[Dict[str, Any]],
                  guidance_selected_ids: Optional[set] = None,
                  card_context: Optional["TurnCardCache"] = None) -> List[Dict[str, Any]]:
        specs = [s for s in (reads or [])[: self.cfg.max_reads_per_step] if isinstance(s, dict)]
        if not specs:
            return []
        def _tagged(obs: Observation, spec: Dict[str, Any]) -> Dict[str, Any]:
            # Tag discovery/capability listings so the answer path can exclude them (menu, not content).
            d = obs.to_dict()
            if _is_discovery_spec(spec):
                d["discovery"] = True
            return d

        if len(specs) == 1:
            obs = self._exec_one_read(specs[0], guidance_selected_ids, card_context)
            return [_tagged(obs, specs[0])] if obs is not None else []
        workers = min(self.cfg.max_parallel, len(specs))
        op_timeout = read_op_timeout_seconds()
        results: List[Optional[Observation]] = [None] * len(specs)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(self._exec_one_read, s, guidance_selected_ids, card_context): i
                       for i, s in enumerate(specs)}
            for fut in futures:
                i = futures[fut]
                try:
                    # Per-op timeout (opposite discipline from the untimed version): one slow
                    # adapter member must never wedge every other read in this step. Named
                    # explicitly so a timeout reports "operation X stalled", never an empty
                    # result that looks like "nothing found".
                    results[i] = fut.result(timeout=op_timeout)
                except FuturesTimeoutError:
                    op_name = describe_read_spec(specs[i])
                    log.warning(
                        "Read operation timed out: %s exceeded %.0fs "
                        "(QAR_READ_OP_TIMEOUT_SECONDS to adjust)", op_name, op_timeout)
                    results[i] = Observation(
                        kind="error",
                        error=f"Operation '{op_name}' timed out after {op_timeout:.0f}s")
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Read operation failed: {type(e).__name__}: {e}", exc_info=True)
                    results[i] = Observation(kind="error", error=f"{type(e).__name__}: {e}")
        finally:
            # Non-blocking shutdown: every spec's outcome was already reported above (via a
            # normal result or a named timeout), so the turn must not then sit here waiting for
            # a still-running worker thread to actually finish (Python threads cannot be
            # force-killed) -- mirrors the context-assembly executor's own wait=False shutdown,
            # for the same reason.
            pool.shutdown(wait=False)
        return [_tagged(r, specs[i]) for i, r in enumerate(results) if r is not None]

    # --- execution directive detection (LLM-based, FORCE deep when user explicitly rejects planning) ----

    def _detect_execution_directive(self, user_message: str) -> bool:
        """True iff the user has EXPLICITLY demanded execution over planning.

        Uses a fast LLM call (haiku) to classify whether the message contains
        a directive to execute/implement/build rather than plan/analyze.

        This is flexible and maintains naturally — no brittle keyword lists.
        """
        if not user_message or len(user_message.strip()) < 10:
            return False

        # Fast classification call (haiku tier)
        classify_prompt = (
            "Classify this user message as EXECUTION or ANALYSIS.\n\n"
            "EXECUTION: user explicitly demands you DO work (code it, implement, build, execute, "
            "fix it, apply changes, make it happen, just do it, no more planning, etc.)\n\n"
            "ANALYSIS: user asks you to think/plan/review/explain/understand (not to execute)\n\n"
            f"Message: {user_message}\n\n"
            "Answer ONLY 'EXECUTION' or 'ANALYSIS':"
        )

        try:
            model = self.registry.resolve_tier("haiku")
            provider = self.get_provider_for_model(model)
            result = provider.answer(
                [{"role": "user", "content": classify_prompt}],
                model=model
            )
            return result and "EXECUTION" in result.upper()
        except Exception:  # noqa: BLE001 — classification failure → assume not an execution directive
            return False

    # --- planner call --------------------------------------------------------

    def _plan(self, user_message: str, transcript: str, context_view: str,
              gathered: List[Dict[str, Any]], *, step: int = 0,
              narrate: bool = False, persona: str = "",
              already_said: Optional[List[str]] = None,
              brainstorm: bool = False,
              card_thread_block: str = "") -> PlanDecision:
        # Step 1 (step == 0) always sees the FULL transcript + context_view. On later re-plan
        # steps, if the consumer opted in, swap the (unchanged) transcript + context_view for a
        # short reference note — the planner's job there is to react to the NEW gathered
        # observations, not to re-read context it already saw. The ANSWER path is untouched.
        plan_transcript = transcript
        plan_context = context_view
        if step > 0 and self.cfg.planner_abbreviate_repeat_context:
            if transcript:
                plan_transcript = _REPLAN_TRANSCRIPT_REF
            if context_view:
                plan_context = _REPLAN_CONTEXT_REF
        # When narration is on, the planner writes its `rationale` as the user-facing spoken beat
        # for this step (Approach B), in the selected rep's voice — so we layer the persona on top
        # and swap in the conversational rationale instruction. No extra LLM call: the beat rides on
        # the planning call the orchestrator already makes.
        # Mode signals are opt-in: with the flag off the planner prompt carries no MODE SIGNAL
        # block, the decide schema has no `mode_signal` field, and the brainstorm note (when a
        # consumer drives the mode from its own state) omits the exit-signal exception.
        mode_signal_block = _MODE_SIGNAL_PLANNER_BLOCK if self.cfg.mode_signals_enabled else ""
        brainstorm_note = _BRAINSTORM_PLANNER_NOTE
        if self.cfg.mode_signals_enabled:
            brainstorm_note += _BRAINSTORM_EXIT_SIGNAL_NOTE
        decide_tool = decide_tool_for(self.cfg.mode_signals_enabled,
                                      self.cfg.deferred_deep_queued,
                                      self.cfg.card_thread_enabled)
        deferred_semantics = (DEFERRED_DEEP_QUEUED_SEMANTICS if self.cfg.deferred_deep_queued
                              else DEFERRED_DEEP_INLINE_SEMANTICS)
        # Per-idea threading: the TOPIC block (doctrine + this turn's candidate prior) is rendered
        # ONCE per turn by run() and passed in. Empty string when the consumer did not opt in, so
        # the prompt is byte-identical to a build without the feature.
        thread_block = card_thread_block if self.cfg.card_thread_enabled else ""
        prompt = PLANNER_PROMPT.format(
            user_message=user_message,
            transcript=plan_transcript or "(no prior messages)",
            context_view=plan_context or "(no context)",
            gathered=_render_gathered_for_planner(
                gathered, self.cfg.planner_recent_full, self.cfg.planner_compress_over),
            max_reads=self.cfg.max_reads_per_step,
            max_subq=self.cfg.max_subquestions,
            max_deep=self.cfg.max_deep_subtasks,
            mode_signal_block=mode_signal_block,
            card_thread_block=thread_block,
            deferred_deep_semantics=deferred_semantics,
            rationale_instruction=(
                (_RATIONALE_INSTRUCTION_NARRATE_REPLAN if step > 0 else _RATIONALE_INSTRUCTION_NARRATE)
                if narrate else _RATIONALE_INSTRUCTION_PLAIN),
        )
        preamble_parts: List[str] = []
        if brainstorm:
            preamble_parts.append(brainstorm_note)
        if narrate and persona.strip():
            preamble_parts.append(
                "--- SPEAK AS THIS PERSONA (for your `rationale` line only) ---\n"
                + persona.strip()[:1500]
            )
        if narrate and already_said:
            preamble_parts.append(
                "--- ALREADY SAID OUT LOUD THIS TURN (do NOT repeat, echo, or paraphrase) ---\n"
                + "\n".join(f"• {s}" for s in already_said)
            )
        if preamble_parts:
            prompt = "\n\n".join(preamble_parts) + "\n\n" + prompt
        model = self.registry.resolve_tier(self.cfg.planner_tier)
        provider = self.get_provider_for_model(model)
        # Cache-friendly layered shape (in addition to the flattened ``prompt`` fallback above): the
        # persona rides in the stable L1 head, the context view is the stable L2 layer, and the
        # planner instructions + message + gathered are the volatile L3 tail (with the context slot
        # relocated to a pointer, since the real context now sits above in L2). Only passed to a
        # provider that accepts ``layers``; the flattened ``prompt`` remains the faithful fallback so
        # a provider without the layered surface is byte-for-byte unchanged.
        plan_kwargs: Dict[str, Any] = {"model": model, "tool_schema": decide_tool}
        if provider_call_accepts_layers(provider.plan):
            plan_body = PLANNER_PROMPT.format(
                user_message=user_message,
                transcript=plan_transcript or "(no prior messages)",
                context_view="(provided in the CONTEXT section above)",
                gathered=_render_gathered_for_planner(
                    gathered, self.cfg.planner_recent_full, self.cfg.planner_compress_over),
                max_reads=self.cfg.max_reads_per_step,
                max_subq=self.cfg.max_subquestions,
                max_deep=self.cfg.max_deep_subtasks,
                mode_signal_block=mode_signal_block,
                card_thread_block=thread_block,
                deferred_deep_semantics=deferred_semantics,
                rationale_instruction=(
                    (_RATIONALE_INSTRUCTION_NARRATE_REPLAN if step > 0 else _RATIONALE_INSTRUCTION_NARRATE)
                    if narrate else _RATIONALE_INSTRUCTION_PLAIN),
            )
            tail_parts: List[str] = []
            if brainstorm:
                tail_parts.append(brainstorm_note)
            if narrate and already_said:
                tail_parts.append(
                    "--- ALREADY SAID OUT LOUD THIS TURN (do NOT repeat, echo, or paraphrase) ---\n"
                    + "\n".join(f"• {s}" for s in already_said)
                )
            tail_parts.append(plan_body)
            plan_kwargs["layers"] = compose_layers(
                persona=(persona if (narrate and persona.strip()) else ""),
                context=plan_context or "",
                tail="\n\n".join(tail_parts),
            ).blocks()
        raw = provider.plan(prompt, **plan_kwargs)
        return normalize_decision(raw or {}, self.cfg)

    # --- answer generation (grounded; optional parallel sub-questions) -------

    def _answer_model(self, plan: PlanDecision, default_tier: str,
                      hint: Optional[str] = None) -> str:
        """Resolve the model id for an answer or deep step.

        Precedence (highest to lowest):
          1. ``hint`` — a per-run model string passed by the caller (e.g. from a task's stored
             ``model`` field). Opaque: the consumer's ModelProvider and ModelRegistry interpret it.
             Unknown tiers degrade gracefully to the registry's default (never raises).
          2. ``plan.model_tier`` — the planner's own choice for this step.
          3. ``default_tier`` — the caller's compile-time default (``"sonnet"`` for answers,
             ``"opus"`` for deep runs).
        """
        tier = hint or plan.model_tier or default_tier
        return self.registry.resolve_tier(tier)

    def _grounded_answer(self, user_message: str, transcript: str, context_view: str,
                         gathered: List[Dict[str, Any]], model: str, partial: bool,
                         native_blocks: Optional[List[Dict[str, Any]]] = None,
                         rep_preamble: Optional[str] = None,
                         reply_directive: Optional[str] = None) -> str:
        messages = []
        if rep_preamble:
            messages.append({"role": "user", "content": rep_preamble})
        if transcript:
            messages.append({"role": "user", "content": transcript})
        messages.append({"role": "user", "content": _grounding_block(context_view, gathered, partial)})
        # The final user message carries the question; when native image blocks are present (an
        # image attachment going to a vision-capable model/provider) they ride along in the SAME
        # message as a content-block list, so the model sees the image alongside the question.
        if native_blocks:
            content: List[Dict[str, Any]] = list(native_blocks)
            content.append({"type": "text", "text": user_message})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_message})
        provider = self.get_provider_for_model(model)
        # REPLY_VOICE_SYSTEM rides as the system prompt, not as another user turn: it has to sit
        # ABOVE the grounding/transcript blocks (which are phrased as instructions ABOUT the person)
        # so the model answers in its own voice instead of mirroring theirs.
        # Cache-friendly layered shape (in addition to the ``messages`` fallback): the persona is the
        # stable L1 head, the grounding CONTEXT is the stable L2 layer, and transcript + this turn's
        # gathered content + the new message are the volatile L3 tail. Skipped for a multimodal turn
        # (native image blocks can't be flattened into the tail) and for any provider without the
        # layered surface, both of which keep the untouched ``messages`` path.
        # ``reply_directive`` is a HOW-TO-REPLY instruction for this turn (today: the brainstorm
        # no-action acknowledgment). It rides on the SYSTEM prompt, with the rest of the reply
        # contract, NOT inside the grounding block: that block is introduced to the model as
        # material to answer FROM and to never mention, which is exactly the wrong frame for an
        # instruction the reply must obey and speak to.
        answer_kwargs: Dict[str, Any] = {
            "model": model,
            "system": (f"{REPLY_VOICE_SYSTEM}\n\n{reply_directive}" if reply_directive
                       else REPLY_VOICE_SYSTEM),
        }
        if not native_blocks and provider_call_accepts_layers(provider.answer):
            tail_parts: List[str] = []
            if transcript:
                tail_parts.append(transcript)
            tail_parts.append(grounding_answer_tail(gathered, partial))
            tail_parts.append(user_message)
            answer_kwargs["layers"] = compose_layers(
                persona=rep_preamble or "",
                context=grounding_context_layer(context_view),
                tail="\n\n".join(tail_parts),
            ).blocks()
        return provider.answer(messages, **answer_kwargs)

    def _synthesize_after_deep(self, user_message: str, *, prior_answer: str, deep_output: str,
                               transcript: str, model: str,
                               rep_preamble: Optional[str] = None) -> str:
        """Rewrite the final reply to REPORT what a (deferred) deep run actually did/produced.

        After a deferred deep run, the pre-deep answer is a proposal that usually reads as "shall I
        proceed?", while the real deliverable lives in the deep run's output. This folds that output
        back into one user-facing reply grounded in what was actually done, in the rep's voice. One
        LLM call. Never raises: on any failure it falls back to the deep output itself (the real
        work), and only then to the prior answer, so the user always sees the substance.
        """
        out = (deep_output or "").strip()
        try:
            prompt = SYNTHESIZE_AFTER_DEEP_PROMPT.format(
                request=user_message,
                proposal=(prior_answer or "(none)").strip()[:4000],
                result=out[:24000],
            )
            messages: List[Dict[str, Any]] = []
            if rep_preamble:
                messages.append({"role": "user", "content": rep_preamble})
            if transcript:
                messages.append({"role": "user", "content": transcript})
            messages.append({"role": "user", "content": prompt})
            provider = self.get_provider_for_model(model)
            synthesized = provider.answer(messages, model=model, system=REPLY_VOICE_SYSTEM)
            if isinstance(synthesized, str) and synthesized.strip():
                return synthesized.strip()
        except Exception:  # noqa: BLE001 — synthesis must never break the turn
            log.warning("post-deep synthesis failed; falling back to deep output", exc_info=True)
        return out or (prior_answer or "")

    def _synthesize_after_queued(self, user_message: str, *, prior_answer: str,
                                 handoff_output: str, transcript: str, model: str,
                                 rep_preamble: Optional[str] = None) -> str:
        """Rewrite the reply after a CONFIRMED deferred hand-off (queued deployments).

        The counterpart of ``_synthesize_after_deep`` for a deep runner that QUEUED the work as a
        background task instead of executing it: the reply must report the hand-off (queued, will
        report back when finished), never present the work as done. Only called after the enqueue
        is confirmed, so the fallback line may honestly say the work is queued. One LLM call;
        never raises.
        """
        try:
            prompt = SYNTHESIZE_AFTER_QUEUED_PROMPT.format(
                request=user_message,
                proposal=(prior_answer or "(none)").strip()[:4000],
                result=(handoff_output or "(hand-off confirmed)").strip()[:4000],
            )
            messages: List[Dict[str, Any]] = []
            if rep_preamble:
                messages.append({"role": "user", "content": rep_preamble})
            if transcript:
                messages.append({"role": "user", "content": transcript})
            messages.append({"role": "user", "content": prompt})
            provider = self.get_provider_for_model(model)
            synthesized = provider.answer(messages, model=model, system=REPLY_VOICE_SYSTEM)
            if isinstance(synthesized, str) and synthesized.strip():
                return synthesized.strip()
        except Exception:  # noqa: BLE001 — synthesis must never break the turn
            log.warning("post-queue synthesis failed; falling back to a plain hand-off note",
                        exc_info=True)
        note = ("I have queued this work to run in the background and will report back in this "
                "conversation when it finishes.")
        return ((prior_answer or "").strip() + "\n\n" + note).strip()

    def synthesize_task_report(self, request: str, deep_output: str, *,
                               transcript: str = "",
                               rep_preamble: Optional[str] = None,
                               tier: str = "balanced") -> str:
        """PUBLIC: write the finished-work report for a completed background task.

        Used by the runner's executor to fold a deep run's raw output into a message that reads
        as the AI reporting its own finished work (same prompt shape as the interactive
        after-deep synthesis), before posting the task's done message into the originating chat.
        ``tier`` resolves through the registry ("balanced" by default; the registry's tiers are
        "fast", "balanced" and "best"). Never raises: any failure returns the raw ``deep_output``
        unchanged.
        """
        try:
            model = self.registry.resolve_tier(tier)
            return self._synthesize_after_deep(
                request, prior_answer="", deep_output=deep_output,
                transcript=transcript, model=model, rep_preamble=rep_preamble)
        except Exception:  # noqa: BLE001 — a report rewrite must never break task reporting
            log.warning("task report synthesis failed; using raw output", exc_info=True)
            return deep_output

    def report_claims_unbacked(self, request: str, report_text: str,
                               exec_record: Optional["ExecutionRecord"]
                               ) -> Optional[bool]:
        """PUBLIC: claim-check a synthesized report against the run's execution record.

        Returns True when the report claims completed work the execution record does NOT back
        (``claims_unexecuted``), False when the claims are clean, and None when the check could
        not run (no record, LLM outage, parse failure). Callers must treat None as "do not trust
        the rewrite" and fall back to the verified raw output. Never raises.
        """
        if exec_record is None:
            return None
        try:
            verdict, _err = self._verify_goal(
                f"Honestly report the outcome of: {request}", request, report_text,
                exec_record=exec_record)
            if verdict is None:
                return None
            return bool(verdict.get("claims_unexecuted"))
        except Exception:  # noqa: BLE001 — a claim check must never break task reporting
            log.warning("task report claim check failed", exc_info=True)
            return None

    def _answer_subquestions(self, user_message: str, transcript: str, context_view: str,
                             gathered: List[Dict[str, Any]], model: str,
                             subquestions: List[str],
                             native_blocks: Optional[List[Dict[str, Any]]] = None,
                             reply_directive: Optional[str] = None) -> str:
        subs = [s for s in subquestions if s][: self.cfg.max_subquestions]
        if len(subs) < 2:
            return self._grounded_answer(user_message, transcript, context_view, gathered, model,
                                         False, native_blocks=native_blocks,
                                         reply_directive=reply_directive)
        ground = _grounding_block(context_view, gathered, False)
        # Same contract as _grounded_answer: a per-turn reply directive rides on the system prompt.
        sub_system = (f"{REPLY_VOICE_SYSTEM}\n\n{reply_directive}" if reply_directive
                      else REPLY_VOICE_SYSTEM)

        def answer_one(sub: str) -> Optional[Dict[str, str]]:
            try:
                # Each sub-question gets the native image blocks too, so any visual sub-question
                # can see the image (the answering model is vision-capable on this path).
                focus = f"Focus ONLY on this sub-question, grounded in the context above:\n\n{sub}"
                if native_blocks:
                    focus_content: List[Dict[str, Any]] = list(native_blocks)
                    focus_content.append({"type": "text", "text": focus})
                    sub_msg = {"role": "user", "content": focus_content}
                else:
                    sub_msg = {"role": "user", "content": focus}
                msgs = [{"role": "user", "content": ground}, sub_msg]
                provider = self.get_provider_for_model(model)
                # A single surviving sub-answer is returned to them verbatim (see below), so each one
                # is written under the same voice contract as a whole reply.
                return {"q": sub, "a": provider.answer(msgs, model=model, system=sub_system)}
            except Exception as e:  # noqa: BLE001
                log.warning(f"Sub-question answer generation failed: {type(e).__name__}: {e}", exc_info=True)
                return None

        workers = min(self.cfg.max_parallel, len(subs))
        out: List[Optional[Dict[str, str]]] = [None] * len(subs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(answer_one, s): i for i, s in enumerate(subs)}
            for f in futs:
                try:
                    out[futs[f]] = f.result()
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Sub-question result collection failed: {type(e).__name__}: {e}", exc_info=True)
                    out[futs[f]] = None
        ok = [a for a in out if a and (a.get("a") or "").strip()]
        if not ok:
            return self._grounded_answer(user_message, transcript, context_view, gathered, model,
                                         False, native_blocks=native_blocks,
                                         reply_directive=reply_directive)
        if len(ok) == 1:
            return ok[0]["a"]
        merged = "\n\n".join(f"SUB-QUESTION: {a['q']}\nANSWER: {a['a']}" for a in ok)
        try:
            # The old wording here opened with "The user asked: ...", which handed the model a
            # third-person frame and invited it to narrate the split back ("The user asked about X,
            # here is the merged answer"). Address it as their message, and say the split is internal.
            return self.provider.answer(
                [{"role": "user", "content": (
                    f"Their message was:\n\n{user_message}\n\nYou answered its independent parts "
                    "below. The split is INTERNAL scaffolding: merge the parts into ONE coherent, "
                    "non-repetitive reply written straight to them, and never mention the split, "
                    "the sub-questions, or the headings below.\n\n" + merged)}],
                model=model,
                system=sub_system,     # the merge writes the reply too, so it obeys the directive
            )
        except Exception:  # noqa: BLE001
            return "\n\n".join(a["a"] for a in ok)

    # --- our own goal verification (replaces Claude Code's /goal) -------------

    def _verify_goal(self, goal: str, brief: str, output: str, *,
                     rep_preamble: Optional[str] = None,
                     quality_standards: Optional[str] = None,
                     transcript: Optional[str] = None,
                     exec_record: Optional[ExecutionRecord] = None,
                     context_layer: Optional[str] = None,
                     ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Decide whether the worker's run met the done-standard AT THE QUALITY BAR.

        Judged through the AI rep's lens (``rep_preamble``) and against the applicable GUIDANCE CARDS
        (``quality_standards`` — the quality bar the result must clear). When ``exec_record`` is
        supplied (answer turns with ``verify_claims`` on), the SAME verdict also judges honesty of
        completion claims: any change the output claims it completed must be backed by a SUCCEEDED
        entry in the record, else ``met=false`` with ``claims_unexecuted=true``.

        ``context_layer`` is the turn's ALREADY-RENDERED L2 context block -- the exact same string the
        caller's plan/answer/deep call put in ITS layers (``plan_context``, ``grounding_context_layer
        (context_view)``, or a deep goal's own ``per_goal_context``; see the call sites in
        ``_run_deep``'s goal loop and the answer-verification loop). Passed straight through, NEVER
        re-rendered here, so the verifier sees the SAME context the call it is judging saw (see
        HANDS_FREE_QUEST_AI_DESIGN.md sections 4 and 6: the overseer should affordably see the FULL
        context the worker/answer saw, and because the block is byte-identical to what that call
        already sent, the marginal cost to a cached lineage is a cache read, not a fresh write). Only
        the TAIL is truncated when the block exceeds ``verify_context_max_chars()`` (env
        ``QAR_VERIFY_CONTEXT_MAX_CHARS``), preserving the stable head/prefix; see
        ``truncate_verify_context``. ``None`` or empty means no context was available at the call
        site (e.g. no assembler/store wired) -- the verdict logic is unaffected either way.

        Returns a ``(verdict, error)`` pair:
        - ``verdict`` is ``{"met": bool, "reason": str, "next_action": str, "need_more_context":
          bool, "context_query": str, "next_tier": Optional[str], "claims_unexecuted": bool}``
          when verification ran; the extra fields let the goal loop decide whether to pull MORE
          context for the next iteration and at which model tier, defaulting to
          ``False``/``""``/``None`` on any parse miss.
        - ``error`` is ``None`` whenever ``verdict`` is populated. When verification could NOT run
          (every tier failed, an unresolvable tier, a parse miss, or no output to judge),
          ``verdict`` is ``None`` and ``error`` carries the real reason (an exception message, not
          just a class name) — the caller MUST treat this as "unverified", never as a silent trust
          of the worker's own reported outcome (context presence never changes this contract). Never
          raises.
        """
        if not (output or "").strip():
            # Nothing to judge — the worker reported no result. Treat as not verifiable here; the
            # loop already handles an empty-output run as a terminal failure before calling this.
            return None, "the worker produced no output to verify"
        persona = ""
        if rep_preamble and rep_preamble.strip():
            persona = ("--- ACT AS THIS PERSONA WHEN JUDGING ---\n"
                       + rep_preamble.strip()[:1500] + "\n\n")
        standards = ""
        if quality_standards and quality_standards.strip():
            standards = ("--- QUALITY STANDARDS (the bar the result must meet) ---\n"
                         + quality_standards.strip()[:3000] + "\n\n")
        claims_rules = ""
        if exec_record is not None:
            claims_rules = VERIFY_CLAIMS_RULES.format(record=exec_record.summary()[:2000])
        # The context block for the FLATTENED prompt (the non-layered fallback): rendered clearly as
        # its own section, placed before the worker output it is meant to ground the judgment of,
        # same prefix-block convention as ``persona``/``standards`` above (present-with-trailing-blank-
        # line, or empty -- so an empty context leaves the prompt byte-for-byte what it was before this
        # parameter existed). Never stripped/re-sliced when present -- that would break the
        # byte-identity with the caller's own L2 that is the whole point of threading it through.
        raw_context_layer = context_layer or ""
        context_text = truncate_verify_context(raw_context_layer) if raw_context_layer.strip() else ""
        context_block = ""
        if context_text:
            context_block = (
                "--- CONTEXT AVAILABLE TO THE WORKER (INTERNAL: the same assembled context/cards the "
                "worker had access to when producing this output; use it to judge whether the output "
                "is actually grounded and complete) ---\n" + context_text + "\n\n")
        prompt = VERIFY_GOAL_PROMPT.format(
            persona=persona, standards=standards, claims_rules=claims_rules, context=context_block,
            goal=(goal or "")[:1000], brief=(brief or "")[:2000],
            transcript=(transcript or "").strip()[:2000] or "(no prior turns)",
            output=(output or "")[:6000])
        # Cache-friendly layered shape (in addition to the flattened ``prompt`` fallback): the rep
        # persona and quality standards ride in the stable L1 head; the SAME rendered context layer the
        # turn's plan/answer/deep call carried rides in the stable L2 (byte-identical to theirs, so a
        # cached lineage reads it instead of re-sending it); the verify instructions + goal + brief +
        # transcript + worker output are the volatile L3 tail (built with empty persona/standards/
        # context slots, since those now sit in the head/L2). Only used for a provider that accepts
        # ``layers``; the flattened ``prompt`` (context inline, see above) is the faithful fallback for
        # every other provider.
        verify_layers = compose_layers(
            persona=(rep_preamble or ""),
            standards=(quality_standards or ""),
            context=context_text,
            tail=VERIFY_GOAL_PROMPT.format(
                persona="", standards="", claims_rules=claims_rules, context="",
                goal=(goal or "")[:1000], brief=(brief or "")[:2000],
                transcript=(transcript or "").strip()[:2000] or "(no prior turns)",
                output=(output or "")[:6000]),
        ).blocks()
        # The judge runs at ``verify_tier`` (default "best"): this ONE small, hard-capped call
        # gates the whole turn's outcome (done vs needs_you/failed, claim honesty), so it gets the
        # strong model. Routed through get_provider_for_model (same as the overseer) since the best
        # tier may resolve to a different provider's model than the planner's. FALLBACK: if the
        # strong-tier call fails or returns an unusable verdict, retry ONCE at ``planner_tier`` (the
        # previous judge) rather than degrading straight to "no verification" — a deployment whose
        # best tier resolves to a model the wired provider cannot serve must not silently lose the
        # goal/claims gate it had before.
        models: List[str] = []
        for tier in (self.cfg.verify_tier or self.cfg.planner_tier, self.cfg.planner_tier):
            try:
                resolved = self.registry.resolve_tier(tier)
            except Exception:  # noqa: BLE001 — an unresolvable tier just drops out of the ladder
                continue
            if resolved and resolved not in models:
                models.append(resolved)
        if not models:
            return None, "no verify tier resolved to a usable model"
        last_error = "verifier returned no usable verdict"
        for model in models:
            try:
                provider = self.get_provider_for_model(model)
                verify_kwargs: Dict[str, Any] = {"model": model, "tool_schema": VERIFY_GOAL_TOOL}
                if provider_call_accepts_layers(provider.plan):
                    verify_kwargs["layers"] = verify_layers
                raw = provider.plan(prompt, **verify_kwargs)
                # A tool-schema provider returns the structured dict directly; a provider that can
                # only return text (no forced tool_choice) returns a string. Reuse the repo's
                # JSON-from-LLM helper to recover the object in that case.
                if isinstance(raw, str):
                    try:
                        raw = json.loads(_extract_json(raw) or "{}")
                    except Exception as e:  # noqa: BLE001 — a parse miss is just "could not verify"
                        last_error = f"could not parse verifier response (model={model}): {type(e).__name__}: {e}"
                        raw = {}
                if isinstance(raw, dict) and "met" in raw:
                    _tier = raw.get("next_tier")
                    return {"met": bool(raw.get("met")),
                            "reason": str(raw.get("reason") or "").strip(),
                            "next_action": str(raw.get("next_action") or "").strip(),
                            "need_more_context": bool(raw.get("need_more_context")),
                            "context_query": str(raw.get("context_query") or "").strip(),
                            "next_tier": (str(_tier).strip() or None) if _tier else None,
                            "claims_unexecuted": bool(raw.get("claims_unexecuted"))}, None
                last_error = f"verifier response missing 'met' (model={model})"
            except Exception as e:  # noqa: BLE001 — verification must never break the run
                last_error = f"{type(e).__name__}: {e}"
                log.warning("Goal verification call failed (model=%s): %s",
                           model, last_error, exc_info=True)
        return None, last_error

    def judge_execution_directive(self, user_message: str, answer_text: str) -> Tuple[bool, str]:
        """ONE structured LLM judgment for the AMBIGUOUS band ``_message_requests_change`` (the
        cheap regex prefilter) leaves undecided -- see ``message_change_signal_ambiguous`` for
        exactly which messages reach here. Design: HANDS_FREE_QUEST_AI_DESIGN.md section 4 --
        intent-ambiguity calls belong to a structured judgment, not regex.

        Runs at ``cfg.intent_judge_tier`` (default "balanced"): this is a routing decision, not the
        run's outcome gate (``verify_tier``/``_verify_goal`` is that), so it does not need the
        strong tier. Hard-capped by ``intent_judge_timeout_seconds()`` so a slow/hung provider can
        NEVER block the turn. On ANY failure, timeout, or unusable response, falls back to the
        regex verdict -- False, since the caller only reaches here after the regex already said no
        -- and returns that with a clear reason so the caller can log why. Never raises.

        Returns ``(is_execution_directive, reason)``.
        """
        fallback_reason = "regex prefilter verdict (LLM judgment unavailable)"
        try:
            model = self.registry.resolve_tier(self.cfg.intent_judge_tier)
        except Exception as e:  # noqa: BLE001 — an unresolvable tier just means no judgment
            log.warning("Intent-directive judge: could not resolve tier %r (%s); using the regex verdict.",
                       self.cfg.intent_judge_tier, e)
            return False, fallback_reason
        if not model:
            return False, fallback_reason
        prompt = INTENT_DIRECTIVE_PROMPT.format(
            message=(user_message or "")[:1000],
            answer=(answer_text or "").strip()[:1500] or "(no answer produced yet)",
        )

        def call_judge() -> Dict[str, Any]:
            provider = self.get_provider_for_model(model)
            raw = provider.plan(prompt, model=model, tool_schema=INTENT_DIRECTIVE_TOOL)
            if isinstance(raw, str):
                raw = json.loads(_extract_json(raw) or "{}")
            return raw if isinstance(raw, dict) else {}

        timeout = intent_judge_timeout_seconds()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(call_judge)
            result = future.result(timeout=timeout)
        except FuturesTimeoutError:
            log.warning("Intent-directive judge timed out after %.0fs "
                       "(QAR_INTENT_JUDGE_TIMEOUT_SECONDS to adjust); falling back to the regex verdict.",
                       timeout)
            return False, fallback_reason
        except Exception as e:  # noqa: BLE001 — the judgment call must never break the turn
            log.warning("Intent-directive judge failed (%s: %s); falling back to the regex verdict.",
                       type(e).__name__, e)
            return False, fallback_reason
        finally:
            pool.shutdown(wait=False)
        if not isinstance(result, dict) or "is_execution_directive" not in result:
            log.warning("Intent-directive judge returned no usable verdict; falling back to the regex verdict.")
            return False, fallback_reason
        is_directive = bool(result.get("is_execution_directive"))
        reason = str(result.get("reason") or "").strip() or "LLM intent judgment"
        return is_directive, reason

    def judge_brainstorm_release(self, user_message: str, transcript: str = "") -> Tuple[bool, str]:
        """ONE structured LLM judgment, on a LATCHED brainstorm turn only: did the user RELEASE the
        no-action hold, or are they still talking about the work?

        This is the exit authority while ``execution_mode="brainstorm"``. It replaces the planner's
        own ``mode_signal="exit_brainstorm"`` for that case (which is ignored while latched) because
        the planner runs at the cheap ``planner_tier`` and a cheap model treats ANY imperative as a
        request to proceed: "create a goal called X and add it to my plan" is an instruction about
        the SUBJECT MATTER, exactly what a person says while thinking out loud, and it released the
        latch mid-turn. Here the question is asked on its own, at ``cfg.mode_release_tier``, with the
        subject-matter vs mode-release distinction spelled out (``MODE_RELEASE_PROMPT``).

        Still LLM judgment, never phrase matching: no keyword list, no regex, no trigger phrases.

        Cost: at most ONE extra structured call, and ONLY while the latch is held. Normal turns are
        untouched. Hard-capped by ``mode_release_timeout_seconds()``.

        FAIL-SAFE DIRECTION IS HOLD. An unresolvable tier, a provider failure, a timeout, a
        malformed response, an empty message: every one of them returns ``(False, reason)``, i.e.
        the latch stays on and the turn produces the no-action acknowledgment. Holding costs the
        user one sentence ("go ahead"); acting on a conversation they put on hold cannot be undone.
        Never raises.

        Returns ``(release, reason)``.
        """
        message = (user_message or "").strip()
        if not message:
            return False, "empty message; holding the brainstorm latch"
        try:
            model = self.registry.resolve_tier(self.cfg.mode_release_tier)
        except Exception as e:  # noqa: BLE001 — an unresolvable tier must not release the latch
            log.warning("Brainstorm-release judge: could not resolve tier %r (%s); HOLDING the latch.",
                       self.cfg.mode_release_tier, e)
            return False, "release judge unavailable (tier unresolvable); holding the latch"
        if not model:
            return False, "release judge unavailable (no model); holding the latch"
        prompt = MODE_RELEASE_PROMPT.format(
            message=message[:1000],
            transcript=(transcript or "").strip()[:1500] or "(no prior turns)",
        )

        def call_judge() -> Dict[str, Any]:
            provider = self.get_provider_for_model(model)
            raw = provider.plan(prompt, model=model, tool_schema=MODE_RELEASE_TOOL)
            if isinstance(raw, str):
                raw = json.loads(_extract_json(raw) or "{}")
            return raw if isinstance(raw, dict) else {}

        timeout = mode_release_timeout_seconds()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(call_judge)
            result = future.result(timeout=timeout)
        except FuturesTimeoutError:
            log.warning("Brainstorm-release judge timed out after %.0fs "
                       "(QAR_MODE_RELEASE_TIMEOUT_SECONDS to adjust); HOLDING the latch.", timeout)
            return False, "release judge timed out; holding the latch"
        except Exception as e:  # noqa: BLE001 — the judgment must never break the turn
            log.warning("Brainstorm-release judge failed (%s: %s); HOLDING the latch.",
                       type(e).__name__, e)
            return False, "release judge failed; holding the latch"
        finally:
            pool.shutdown(wait=False)
        if not isinstance(result, dict) or "release_brainstorm" not in result:
            log.warning("Brainstorm-release judge returned no usable verdict; HOLDING the latch.")
            return False, "release judge gave no verdict; holding the latch"
        release = bool(result.get("release_brainstorm"))
        reason = str(result.get("reason") or "").strip() or "LLM mode-release judgment"
        return release, reason

    def _deep_models(self, model_hint: Optional[str], quality_standards: Optional[str],
                     fallback: Optional[str]) -> List[Optional[str]]:
        """Resolve the deep-worker model LADDER (tried in order, escalating on a not-met goal).

        Priority: (1) an explicit per-task model request via ``model_hint`` when it names a model the
        worker can run; (2) a guidance card model preference; either PINS a single model (no
        escalation). Otherwise (3) the configured ``deep_model_ladder`` (the consumer's explicit
        ladder, e.g. from ``QAR_DEEP_MODELS`` -- REAL Claude ids/aliases, weak -> strong), else
        (4) a ladder built from the single ``fallback`` model the orchestrator was given, EXTENDED
        with any Claude-runnable id found by resolving the "quality"/"best" tiers (so a Gemini/
        OpenAI deployment whose tier config still names a Claude id for its strong tier -- e.g.
        ``QAR_MODEL_BEST=claude-opus-4-8`` -- gets a real escalation step instead of a silently
        inert length-1 ladder; see ``fallback_deep_ladder``). Always returns a non-empty list.
        Logs (INFO) the resolved ladder once per deep run, and WARNS when a NON-pinned resolution
        still comes out length <= 1 (escalation unavailable), so a deployment can see and fix it."""
        from .goal_runner import _is_claude_model  # worker-runnable check (deep worker is Claude Code)
        # Explicit per-task model request: ``fallback`` already factored ``model_hint`` through the
        # registry. When a hint was given and it resolved to a model the worker can run (Claude), pin
        # it (no auto-escalation). In a non-Claude deployment the resolved hint is not worker-runnable,
        # so we fall through to the ladder instead of handing Claude Code a Gemini/OpenAI id.
        if model_hint and fallback and _is_claude_model(fallback):
            log.info("Deep-worker model ladder: pinned to %r (explicit per-task model request); "
                     "escalation intentionally disabled.", fallback)
            return [fallback]
        pref = _guidance_model_pref(quality_standards)
        if pref:
            log.info("Deep-worker model ladder: pinned to %r (guidance model preference); "
                     "escalation intentionally disabled.", pref)
            return [pref]
        if self.cfg.deep_model_ladder:
            ladder = [m for m in self.cfg.deep_model_ladder if m]
            if ladder:
                self.log_deep_ladder(ladder, source="configured deep_model_ladder (e.g. QAR_DEEP_MODELS)")
                return list(ladder)
        ladder = self.fallback_deep_ladder(fallback)
        self.log_deep_ladder(ladder, source="fallback model + Claude-runnable tier resolution")
        return ladder

    def fallback_deep_ladder(self, fallback: Optional[str]) -> List[Optional[str]]:
        """Build the deep-worker ladder when no explicit ``deep_model_ladder`` is configured.

        Starts from the single ``fallback`` model the orchestrator was already given. If that model
        is NOT Claude-runnable (a Gemini/OpenAI deployment, so the deep worker -- Claude Code --
        could never actually use it as ``--model``), try to EXTEND the ladder with a real escalation
        step by resolving the "quality" then "best" tiers through the registry: on many deployments
        these still name a genuine Claude id (either the library's own last-known default, or an
        explicit operator override like ``QAR_MODEL_BEST=claude-opus-4-8`` -- see
        HANDS_FREE_QUEST_AI_DESIGN.md section 2, point 4 for why this matters), so this is what makes
        "goal not met -> stronger model" do something even when the deployment's PRIMARY model is not
        Claude. Never raises; always returns a non-empty list (falls back to ``[fallback]`` alone,
        even if that is not Claude-runnable, so a caller always has something to try)."""
        from .goal_runner import _is_claude_model  # worker-runnable check
        ladder: List[str] = [fallback] if fallback else []
        if not fallback or not _is_claude_model(fallback):
            for tier in ("quality", "best"):
                try:
                    resolved = self.registry.resolve_tier(tier)
                except Exception:  # noqa: BLE001 — an unresolvable tier is just skipped
                    continue
                if resolved and _is_claude_model(resolved) and resolved not in ladder:
                    ladder.append(resolved)
        return ladder or [fallback]

    @staticmethod
    def log_deep_ladder(ladder: List[Optional[str]], *, source: str) -> None:
        """Log the resolved deep-worker ladder once per deep run (called from ``_deep_models``).

        A ladder this reports on is NEVER an intentional pin (those log their own INFO line and
        return before reaching here) -- so length <= 1 here genuinely means "goal not met -> a
        stronger model" has nothing to escalate to, worth a WARNING naming the fix. Never raises."""
        try:
            log.info("Deep-worker model ladder resolved (%s): %s", source, ladder)
            if len(ladder) <= 1:
                log.warning(
                    "Escalation unavailable: no additional Claude-runnable model configured for the "
                    "deep-worker ladder (resolved: %s). The deep worker (Claude Code) can only "
                    "escalate to another Claude model -- set QAR_DEEP_MODELS to a comma-separated "
                    "list of Claude model ids/aliases (e.g. \"sonnet,opus\"), or point a tier "
                    "override (e.g. QAR_MODEL_BEST) at a real Claude id.", ladder)
        except Exception:  # noqa: BLE001 — logging must never break a deep run
            pass

    def _resolved_deep_tier(self, tier: Optional[str],
                            deep_models: List[Optional[str]]) -> Optional[str]:
        """Resolve a verifier-requested ``next_tier`` to a worker-runnable model id via the registry.

        The deep worker is Claude Code, so the model must be a Claude id. When the tier resolves to
        a Claude model we use it; otherwise (e.g. a Gemini/OpenAI deployment whose tiers map to
        non-Claude ids) we fall back to the STRONGEST model already on the deep ladder so we never
        hand the Claude worker a foreign id. Returns ``None`` if ``tier`` is empty or unresolvable,
        so the caller keeps stepping the ladder instead. Never raises."""
        if not (tier or "").strip():
            return None
        from .goal_runner import _is_claude_model  # worker-runnable check
        try:
            resolved = self.registry.resolve_tier(tier)
        except Exception:  # noqa: BLE001 — an unknown tier just means "no override"
            return None
        if resolved and _is_claude_model(resolved):
            return resolved
        # Non-Claude deployment: pick the strongest Claude model already on the ladder, if any.
        for m in reversed(deep_models):
            if m and _is_claude_model(m):
                return m
        return None

    @staticmethod
    def _drain_pending(pending_inputs: Optional[Callable[[], List[str]]]) -> str:
        """Pull any NEW user messages that arrived mid-run (the consumer supplies the callable) and
        render them as a block to fold into the next attempt, so a long-running goal loop picks up
        what the user said after it started. Returns "" when none / not wired. Never raises."""
        if pending_inputs is None:
            return ""
        try:
            msgs = [str(m).strip() for m in (pending_inputs() or []) if str(m).strip()]
        except Exception:  # noqa: BLE001 — draining must never break the run
            return ""
        if not msgs:
            return ""
        return ("--- NEW MESSAGES FROM THE USER SINCE YOU STARTED (incorporate these) ---\n"
                + "\n".join(f"- {m}" for m in msgs))

    @staticmethod
    def _augment_brief(base_brief: str, prev_output: str, verdict: Dict[str, Any]) -> str:
        """Build the next attempt's brief from the verdict: the original brief PLUS what the last
        attempt produced, why it fell short, and the specific next action to take."""
        reason = (verdict.get("reason") or "the done-standard was not yet satisfied").strip()
        nxt = (verdict.get("next_action")
               or "complete the remaining work so the goal is fully met.").strip()
        return (
            f"{base_brief}\n\n"
            "--- PREVIOUS ATTEMPT DID NOT YET MEET THE GOAL ---\n"
            f"What it reported:\n{(prev_output or '').strip()[:1500]}\n\n"
            f"Why it fell short: {reason}\n"
            f"Do this now: {nxt}"
        )

    # --- warm recent-context scoping (shared by the main turn and per-goal deep context) -----

    def _recent_scope_keys(self, ctx_meta: Optional[Dict[str, Any]]) -> List[str]:
        """The WARM recent-context scope keys for this run/goal (see core/recent_context.py):
        ``conv:<conv_id>`` when a conversation id is in scope, ``quest:<quest_id>`` when a quest id
        is in scope, PLUS always ``"global"`` (everything recently selected anywhere), unless the
        consumer turned cross-conversation memory off via ``cfg.recent_context_global_enabled``. A
        turn/goal with neither a conv_id nor a quest_id in scope still reads/records global (as
        long as global is enabled). Never raises; [] when recent-context is off entirely."""
        keys: List[str] = []
        try:
            meta = ctx_meta or {}
            conv_id = meta.get("conv_id")
            quest_id = meta.get("quest_id")
            if conv_id:
                keys.append(conv_scope_key(conv_id))
            if quest_id:
                keys.append(quest_scope_key(quest_id))
            if self.cfg.recent_context_global_enabled:
                keys.append(GLOBAL_SCOPE_KEY)
        except Exception:  # noqa: BLE001
            return []
        return keys

    # --- per-goal context selection (each deep goal gets its OWN context) -----

    def _assemble_for_goal(self, goal: str, *,
                           ctx_meta: Optional[Dict[str, Any]]) -> str:
        """Select PER-GOAL context for a single deep goal condition (text only). Thin wrapper over
        ``_assemble_for_goal_with_cards`` for any caller that only needs the rendered block."""
        text, _cards = self._assemble_for_goal_with_cards(goal, ctx_meta=ctx_meta)
        return text

    def _assemble_for_goal_with_cards(
        self, goal: str, *, ctx_meta: Optional[Dict[str, Any]]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Select PER-GOAL context for a single deep goal condition. Renders a block from the
        wired ``context_assembler`` (targeting THIS goal, not the shared run-level message), the
        WARM recent-context store's own scoped warm set (see core/recent_context.py -- the SAME
        completeness guarantee a chat turn gets, so a deep/background task benefits from warm
        context too, not just the interactive turn), plus a relevant CURRENT-conversation slice
        from the wired ``conversation_store``. Returns ``(rendered_text, card_metadata)`` --
        ``card_metadata`` is the merged fresh + surviving-recent card list this goal's context
        actually included (fresh cards first, dedupe-fresh-wins), so the caller can ``record()``
        it back to the warm store after the goal completes (see ``run_one`` in ``_run_deep``), the
        same way the main turn does. Returns ``("", [])`` when nothing is wired or found. Never
        raises — a degraded source is simply skipped."""
        parts: List[str] = []
        fresh_card_ids: set = set()
        fresh_card_meta: List[Dict[str, Any]] = []
        goal_text = (goal or "").strip()

        # Load the scoped warm set ONCE (used both for the recent_item_usage hint below and for
        # the recent-turn merge further down), gated against the GOAL text (not the run-level
        # message -- each subgoal gets memory relevant to ITS own focus).
        recent_records: List[Dict[str, Any]] = []
        recent_hint: Dict[str, List[str]] = {}
        if self.recent_context is not None and self.cfg.recent_context_enabled and goal_text:
            try:
                scope_keys = self._recent_scope_keys(ctx_meta)
                if scope_keys:
                    recent_records = self.recent_context.load(scope_keys)
                    recent_hint = build_item_usage_hint(recent_records, goal_text)
            except Exception:  # noqa: BLE001
                log.debug("per-goal recent-context load failed", exc_info=True)
                recent_records, recent_hint = [], {}

        if self.context_assembler is not None and goal_text:
            try:
                # A fresh dict for THIS goal's hint: never inherit the run-level ``ctx_meta``'s own
                # ``recent_item_usage`` (built for the user's whole message, not this subgoal) when
                # this goal's own scoped load produced no hint.
                goal_meta = dict(ctx_meta or {})
                if recent_hint:
                    goal_meta["recent_item_usage"] = recent_hint
                else:
                    goal_meta.pop("recent_item_usage", None)
                assembled = self.context_assembler.assemble(goal_text, meta=goal_meta or None)
                cv = self._materialize_deep_context(assembled)
                if cv:
                    parts.append("--- CONTEXT SELECTED FOR THIS GOAL ---\n" + cv)
                fresh_card_meta = [
                    cm for cm in (getattr(assembled, "card_metadata", None) or [])
                    if isinstance(cm, dict) and cm.get("id")
                ]
                fresh_card_ids = {cm.get("id") for cm in fresh_card_meta}
            except Exception:  # noqa: BLE001 — assembly must never break the run
                log.debug("per-goal context assembly failed", exc_info=True)

        recent_entries: List[Dict[str, Any]] = []
        if recent_records:
            try:
                # The turn's time_range (when ``_derive_goal_condition`` parsed one; see run())
                # rides in ctx_meta, so a deep goal's recent-context set honors the same hard
                # time filter as the main turn. Absent key: byte-for-byte today's behavior.
                goal_time_range = (ctx_meta or {}).get("time_range")
                filtered = filter_relevant(
                    recent_records, goal_text, is_followup=False,
                    max_cards=self.cfg.recent_context_max_cards,
                    time_range=goal_time_range)
                survivors = [r for r in filtered if r.get("id") not in fresh_card_ids]
                recent_text, recent_entries = render_recent_cards(
                    survivors, goal_text, time_range=goal_time_range)
                if recent_text:
                    parts.append(recent_text)
            except Exception:  # noqa: BLE001 — recent-context merge must never break the run
                log.debug("per-goal recent-context merge failed", exc_info=True)
                recent_entries = []

        conv_id = (ctx_meta or {}).get("conv_id")
        if self.conversation_store is not None and conv_id and goal_text:
            try:
                slc = self.conversation_store.current_slice(conv_id, goal)
                txt = (getattr(slc, "text", "") or "").strip()
                if txt:
                    parts.append("--- RELEVANT CONVERSATION FOR THIS GOAL ---\n" + txt)
            except Exception:  # noqa: BLE001 — store must never break the run
                log.debug("per-goal conversation slice failed", exc_info=True)
        return "\n\n".join(parts), fresh_card_meta + recent_entries

    @staticmethod
    def _materialize_deep_context(assembled: Any) -> str:
        """Render the per-goal DEEP context from the assembled ``card_metadata``, choosing PASTE vs
        POINTER per item. Never raises.

        Each card carries its VERBATIM ``rendered_section`` (its whole rendered block: summary + file
        listings + content + conventions) plus its structured items (each ``{type, why, locator,
        text, deliver, pointer_eligible}``). When a card has a ``rendered_section`` we paste it
        VERBATIM, except that each item the consolidator tagged ``deliver=="pointer"`` (only ever a
        file, since the worker can re-read it) has its pasted text fragment SWAPPED for a short
        pointer line, so the file's full body is not duplicated. A card WITHOUT a ``rendered_section``
        (e.g. a stub assembler or a deployment that emits only items) falls back to the prior
        item-only rebuild under a ``### <title>`` header. When the assembled context carries neither
        sections nor items (e.g. a file-only deployment), this falls back to the plain
        ``context_view`` (today's behavior, byte-for-byte), so the change is never worse.
        """
        def _label(it: dict) -> str:
            # WHAT the reference points at (a file's path, a collection's name/id). MUST mirror
            # adapters.card_content_render.locator_label; kept inline so the core brain never imports
            # an adapter. Keep the two in sync.
            itype = it.get("type", "note")
            loc = it.get("locator") if isinstance(it.get("locator"), dict) else {}
            if itype == "file":
                return str(loc.get("path") or "").strip()
            if itype == "collection":
                name = str(loc.get("name") or loc.get("collection") or "").strip()
                cid = str(loc.get("id") or "").strip()
                if name and cid:
                    return f"{name} ({cid})"
                return name or cid
            if itype == "conversation":
                return str(loc.get("conv_id") or loc.get("id") or "").strip()
            if itype == "query":
                return str(loc.get("query") or loc.get("text") or "").strip()
            if itype == "note":
                return ""
            return str(loc.get("name") or loc.get("id") or loc.get("path") or "").strip()

        def _frag(it: dict) -> str:
            # The exact fragment a content item contributed to a rendered section (header + indented
            # body). MUST mirror adapters.card_content_render.render_block_lines; kept inline so the
            # core brain never imports an adapter. Keep the two in sync.
            itype = it.get("type", "note")
            why = it.get("why", "")
            text = it.get("text", "") or ""
            label = _label(it)
            head = f"  - ({itype})"
            if label:
                head += f" {label}"
                if why:
                    head += f" -- {why}"
            elif why:
                head += f" {why}"
            lines = [head]
            for rl in text.splitlines() or [text]:
                lines.append(f"      {rl}")
            return "\n".join(lines)

        try:
            cms = [cm for cm in (getattr(assembled, "card_metadata", None) or [])
                   if isinstance(cm, dict)]
            has_sections = any(cm.get("rendered_section") for cm in cms)
            has_items = any(cm.get("items") for cm in cms)
            if not has_sections and not has_items:
                return (getattr(assembled, "context_view", "") or "").strip()
            blocks: List[str] = []
            for cm in cms:
                items = cm.get("items") or []
                rendered = cm.get("rendered_section") or ""
                if rendered:
                    # Paste the VERBATIM section; swap each pointer-delivered file item's fragment for
                    # a pointer line (guard: only swap if the fragment is found in the section).
                    section = rendered
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        locator = it.get("locator") if isinstance(it.get("locator"), dict) else {}
                        path = locator.get("path")
                        if it.get("deliver") == "pointer" and it.get("pointer_eligible") and path:
                            why = (it.get("why") or "").strip()
                            pointer_line = (
                                f"  - (file) {why} -> read this file fresh if needed: {path}"
                            )
                            frag = _frag(it)
                            if frag and frag in section:
                                section = section.replace(frag, pointer_line, 1)
                            else:
                                raw = it.get("text", "")
                                if raw and raw in section:
                                    section = section.replace(raw, pointer_line, 1)
                    if section.strip():
                        blocks.append(section)
                    continue
                # No rendered_section: prior item-only rebuild.
                if not items:
                    continue
                title = cm.get("title") or cm.get("id") or "context"
                lines: List[str] = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    locator = it.get("locator") if isinstance(it.get("locator"), dict) else {}
                    path = locator.get("path")
                    if it.get("deliver") == "pointer" and it.get("pointer_eligible") and path:
                        why = (it.get("why") or "").strip()
                        # NB: a pointer is ALWAYS a file (the worker can open it itself).
                        lines.append(
                            f"- (file) {why} -> read this file fresh if needed: {path}"
                        )
                    else:
                        text = it.get("text") or ""
                        if text:
                            lines.append(text)
                if lines:
                    blocks.append(f"### {title}\n" + "\n".join(lines))
            rendered_all = "\n\n---\n\n".join(blocks).strip()
            return rendered_all or (getattr(assembled, "context_view", "") or "").strip()
        except Exception:  # noqa: BLE001 — materialization must never break the run
            return (getattr(assembled, "context_view", "") or "").strip()

    def _widen_for_goal(self, goal: str, query: str, round_idx: int, *,
                        ctx_meta: Optional[Dict[str, Any]]) -> str:
        """Pull MORE context for a retry that reported it lacked context. Widens by round:
        a fresh assembler read targeting the verifier's ``context_query``, WIDER conversation
        retrieval (the current slice plus related OTHER conversations in scope), and a targeted
        retrieval grep/read of the missing term. ``round_idx`` (1-based) grows the budget so each
        retry can see more than the last. Returns "" when nothing extra is found. Never raises."""
        q = (query or goal or "").strip()
        if not q:
            return ""
        parts: List[str] = []
        # 1) Re-run the assembler against the SPECIFIC missing context the verifier named.
        if self.context_assembler is not None:
            try:
                assembled = self.context_assembler.assemble(q, meta=ctx_meta or None)
                cv = (getattr(assembled, "context_view", "") or "").strip()
                if cv:
                    parts.append("--- ADDITIONAL CONTEXT (" + q[:80] + ") ---\n" + cv)
            except Exception:  # noqa: BLE001
                log.debug("widen assembler read failed", exc_info=True)
        # 2) Widen the conversation: the current slice PLUS related other conversations, allowing
        #    more characters/conversations on each successive round.
        conv_id = (ctx_meta or {}).get("conv_id")
        conv_scope = (ctx_meta or {}).get("conv_scope")
        if self.conversation_store is not None and conv_scope:
            try:
                rel = self.conversation_store.related_slices(
                    q, conv_scope, exclude_conv_id=conv_id or None,
                    max_convs=min(3 + round_idx, 8),
                    max_chars=6000 + 2000 * round_idx)
                txt = (getattr(rel, "text", "") or "").strip()
                if txt:
                    parts.append("--- RELATED CONVERSATIONS ---\n" + txt)
            except Exception:  # noqa: BLE001
                log.debug("widen related slices failed", exc_info=True)
        # 3) Targeted retrieval read: grep the named term across the corpus for the next attempt.
        if self.retrieval is not None:
            try:
                obs = self.retrieval.grep(q, max_hits=min(10 + 5 * round_idx, 40))
                hits = getattr(obs, "hits", None) or []
                if hits:
                    lines = []
                    for h in hits[:min(10 + 5 * round_idx, 40)]:
                        if isinstance(h, dict):
                            lines.append(f"{h.get('rel_path','?')}:{h.get('line_no','?')}: "
                                         f"{str(h.get('line','')).strip()[:200]}")
                    if lines:
                        parts.append("--- RETRIEVAL HITS (" + q[:80] + ") ---\n"
                                     + "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.debug("widen retrieval grep failed", exc_info=True)
        return "\n\n".join(parts)

    # --- deep fan-out --------------------------------------------------------

    def _has_deep_execution_capability(self) -> bool:
        """Whether ANY execution path is wired: a single default ``deep_runner``, OR the named
        registry (``deep_runners`` + a ``deep_runner_classifier`` to pick among them).

        Several call sites used to test ``self.deep_runner is not None`` as "can we actually
        execute deep work". That was correct back when a single runner was the only wiring style,
        but it went STALE once the named-runner registry was added: a consumer that wires
        ``deep_runner=None`` plus ``deep_runners``/``deep_runner_classifier`` (the multi-runner
        dispatch style, e.g. one runner for in-app data ops, one for open-ended tasks) has real
        execution capability, but every ``self.deep_runner is not None`` gate treated it as "no
        runner configured" and silently skipped execution/remediation. This is the one place that
        answers "can we run deep work at all", used by every such gate.
        """
        return self.deep_runner is not None or bool(
            self.deep_runners and self.deep_runner_classifier is not None)

    def _has_deferred_queue_capability(self) -> bool:
        """Whether DEFERRED work can be handed off, even with no inline execution capability.

        A queued deployment (``deferred_deep_queued``) that registered its queue runner under
        ``deep_runners[DEFERRED_RUNNER_KEY]`` can hand deferred work off with nothing else wired:
        the hand-off PINS that runner via ``_run_deep(runner_override=...)``, so it needs neither a
        default ``deep_runner`` nor a ``deep_runner_classifier``. Without this, such a consumer hit
        the ``_has_deep_execution_capability`` gate on the deferred nets and the queue was simply
        unreachable. Deliberately NOT merged into ``_has_deep_execution_capability``: the other
        gates it guards (planner "deep", overseer escalate_deep, claim remediation) run work INLINE
        through the normal wiring, which a queue-only consumer genuinely does not have.
        """
        return bool(self.cfg.deferred_deep_queued and self.deep_runners
                    and self.deep_runners.get(DEFERRED_RUNNER_KEY) is not None)

    def _run_deep(self, plan: PlanDecision, user_message: str, model: str,
                  emit: Optional[_Emitter] = None,
                  rep_preamble: Optional[str] = None,
                  exec_record: Optional[ExecutionRecord] = None,
                  gathered: Optional[List[Dict[str, Any]]] = None,
                  quality_standards: Optional[str] = None,
                  pending_inputs: Optional[Callable[[], List[str]]] = None,
                  model_hint: Optional[str] = None,
                  ctx_meta: Optional[Dict[str, Any]] = None,
                  cancel_check: Optional[Callable[[], bool]] = None,
                  runner_override: Optional[Any] = None,
                  working_dir_override: Optional[str] = None) -> OrchestratorResult:
        # ``runner_override``: PIN a specific runner for every goal of this call, bypassing the
        # named-runner classifier. Used by the deferred_deep hand-off in a queued deployment
        # (OrchestratorConfig.deferred_deep_queued) so deferred work always reaches the consumer's
        # queue runner (registry key DEFERRED_RUNNER_KEY) and is never re-routed to an inline
        # runner by the classifier. None = today's resolution, byte-for-byte.
        # Cooperative mid-run cancellation (see ``Orchestrator.run``'s ``cancel_check`` docstring):
        # bail out before spawning any subtask work at all when the caller already reports the run
        # was cancelled. ``goals`` mirrors the "no deep-runner configured" early return above so a
        # caller inspecting a cancelled result still sees which goal(s) were requested.
        if cancel_check is not None and cancel_check():
            _cancelled_goals = [
                (st.get("goal") or "").strip() or user_message
                for st in (plan.deep_subtasks or [{"goal": plan.goal}])
            ]
            return OrchestratorResult(kind="cancelled", goals=_cancelled_goals,
                                      rationale=plan.rationale, exit_reason="cancelled")
        subtasks = (plan.deep_subtasks or [])[: self.cfg.max_deep_subtasks]
        if not subtasks:
            subtasks = [{"goal": _truncate_goal(plan.goal or f"Fully address the request: {user_message}"),
                         "brief": plan.deep_brief or user_message}]
        if runner_override is None and not self._has_deep_execution_capability():
            # No runner configured at all (neither a default runner nor the named registry):
            # surface the goal(s) without executing (caller may spawn). An explicit
            # ``runner_override`` IS the capability (the caller pinned a real runner, e.g. the
            # queued deferred hand-off pinning DEFERRED_RUNNER_KEY), so it bypasses this gate:
            # otherwise a consumer that wires ONLY the queue runner would find the deferred path
            # unreachable and the work would silently never be handed off.
            goals = [(st.get("goal") or "").strip() or user_message for st in subtasks]
            # Record each goal as REQUESTED-but-not-executed (neither success nor failure) so the
            # guard knows a re-run is the SAFE remediation here (nothing actually mutated).
            if exec_record is not None:
                for g in goals:
                    exec_record.facts.append(ExecutionFact(goal=g))
            return OrchestratorResult(kind="deep", goals=goals, rationale=plan.rationale,
                                      deep_results=[])

        # HIERARCHICAL GOAL: the overall user-level goal this turn pursues. When the work fans out
        # into parallel subgoals, each subgoal process must be told the HIGHER goal it serves, so it
        # stays aligned with the whole instead of optimizing its piece in isolation.
        overall_goal = (plan.goal or "").strip() or user_message
        multi = len(subtasks) > 1
        # Whether the async post-deep card updater is active (toggle on + provider + card-update
        # store). Computed once: it gates appending the FUTURE-CONTEXT instruction to each deep brief.
        card_update_active = self._card_updater_active()

        def run_one(task: Dict[str, Any], task_index: int = 0) -> DeepResult:
            goal = (task.get("goal") or "").strip() or f"Fully address: {user_message}"
            brief = (task.get("brief") or goal).strip()
            # EVERY deep process is told BOTH the top input-level goal (the user's actual request)
            # and, when it is a subgoal of a larger fan-out, the overall goal it serves — so it never
            # loses sight of what the user wants while pursuing its specific piece. (Its OWN process
            # goal/done-standard is added separately by compose_goal_prompt.) This header is baked
            # into the base brief, so every retry keeps it alongside the prior output + feedback.
            _hdr = [f"USER'S REQUEST (the top-level goal):\n{user_message}"]
            if multi and overall_goal and overall_goal != goal:
                _hdr.append(f"OVERALL GOAL (this process is ONE subgoal serving it, stay aligned):\n"
                            f"{overall_goal}")
                _hdr.append("FOCUS: concentrate on THIS subgoal using the context selected for it "
                            "below. Search for more only if you genuinely need it.")
            # Generate task UUID for matching with JSONL session file
            import uuid
            task_uuid = str(uuid.uuid4())[:8]
            # Show task identifier in brief so user sees which task is running
            task_label = f"TASK {task_index}" if multi else "TASK"
            brief = "\n\n".join(_hdr) + f"\n\n{task_label} [{task_uuid}]: {goal}\n\n" + brief
            # NOTE: the FUTURE-CONTEXT instruction is appended further down, once the runner that will
            # handle THIS goal is resolved: which of the two instructions applies depends on that
            # runner's ``future_context_channel``. Both are appended only when the updater is active,
            # so a non-updating deployment's deep brief is byte-for-byte unchanged.
            # Per-subtask execution fact — populated from EVENT_EXEC phase ticks (live) and finalized
            # from the DeepResult.met below. Recording per-subtask keeps facts correct even when
            # multiple subtasks run concurrently (each closure owns its own ``fact``).
            fact = ExecutionFact(goal=goal) if exec_record is not None else None

            # PER-GOAL CONTEXT: each deep goal selects its OWN context (its own assembler read +
            # conversation slice for THIS goal), distinct from the shared run-level context_view.
            # Built once here; ``extra_context`` accumulates additional context pulled on retries
            # that report they did not have enough (the "look at more if it did not learn enough"
            # widening principle). Empty when no assembler/store is wired (single-goal/no-store path
            # is byte-for-byte unchanged). The closure mutates ``extra_context`` across iterations.
            per_goal_context, per_goal_cards = self._assemble_for_goal_with_cards(goal, ctx_meta=ctx_meta)
            extra_context: List[str] = []
            # The runner picks the run_id (from its session file); we learn it from the first exec
            # event and reuse it to label the completion milestone, so the consumer can attach this
            # task's final output to the same run it streamed.
            captured_run_id: Dict[str, Optional[str]] = {"id": None}
            if per_goal_context and emit is not None:
                # Progress the person can read, not internal state. "Selected context for goal: ..."
                # named an orchestrator step and read as leaked machinery in the live status pill.
                emit.status(f"Working on: {goal[:60]}")

            # Resolve which runner handles THIS goal ONCE per task (not per retry — the
            # classifier's inputs (user_message/goal/brief) don't change across retries of the
            # same task): if named runners + a classifier are registered, the classifier picks a
            # key; otherwise the single default ``deep_runner``. Falls back to ``deep_runner`` on
            # any classifier failure or an unknown key.
            active_runner = runner_override if runner_override is not None else self.deep_runner
            if (runner_override is None and self.deep_runners
                    and self.deep_runner_classifier is not None):
                try:
                    key = self.deep_runner_classifier(user_message, goal, brief)
                    if key == DEFERRED_RUNNER_KEY:
                        # RESERVED KEY, never classifier-selectable. The queue runner returns a
                        # hand-off receipt, not finished work; routing an ordinary deep turn to it
                        # would give the goal loop a receipt to verify and let the caller report a
                        # queue acknowledgement as completed work. It is reachable ONLY through the
                        # deferred hand-off's explicit runner_override (handled above).
                        log.warning(
                            f"deep_runner_classifier returned the reserved key "
                            f"{DEFERRED_RUNNER_KEY!r}; it is not selectable for ordinary deep work, "
                            f"using default runner"
                        )
                    elif key in self.deep_runners:
                        active_runner = self.deep_runners[key]
                        log.debug(f"deep_runner_classifier selected runner {key!r}")
                    else:
                        log.warning(
                            f"deep_runner_classifier returned unknown key {key!r}; "
                            f"using default runner"
                        )
                except Exception as e:  # noqa: BLE001 — classifier failure must never block a run
                    log.warning(f"deep_runner_classifier failed ({e}); using default runner")

            # FUTURE-CONTEXT ask, routed by the RESOLVED runner's channel. When the async card updater
            # is active, EVERY runner is asked for future context (a code generator knows the most
            # reusable facts of all: the entities, ids, and schema it touched), but a strict-format
            # runner is asked for it OUT OF BAND so its payload stays a valid payload. This is the ask
            # side; ``_normalize_future_context`` below is the guarantee side.
            if card_update_active:
                brief = brief + (
                    DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION
                    if _future_context_channel(active_runner) == FUTURE_CONTEXT_VIA_FIELD
                    else DEEP_FUTURE_CONTEXT_INSTRUCTION
                )

            # Pass the live emitter to the runner ONLY if its run_goal accepts an ``emit`` kwarg
            # (or **kwargs). Decided by signature inspection — never by a try/except TypeError,
            # which could re-invoke a runner that already ran a side effect (e.g. a data
            # mutation). Checked against the ACTUAL runner resolved above — not ``self.deep_runner``,
            # which is None when the named-runner registry is the wiring style, so this must NOT be
            # computed against it (that silently dropped exec streaming for every named-registry
            # consumer). We also TEE the emitter so EVENT_EXEC phase ticks are recorded into
            # ``exec_record`` (per-subtask) for the broken-promise guard, while still streaming to
            # the live sink.
            wants_emit = (emit is not None and active_runner is not None
                         and _run_goal_accepts_emit(active_runner))
            wants_run_id = active_runner is not None and _run_goal_accepts_run_id(active_runner)
            # A per-task ``rep_preamble`` (e.g. an AI rep's pulled persona) and the brain's specific
            # ``gathered`` reads are both forwarded to the deep run as a combined ``context_preamble``,
            # ONLY to a runner whose ``run_goal`` accepts that kwarg (older signatures are untouched).
            # We check capability regardless of whether rep_preamble or gathered are set — either alone
            # is enough to build a useful context_preamble for the deep runner.
            wants_preamble = (active_runner is not None
                              and _run_goal_accepts_context_preamble(active_runner))
            # A per-task working-directory override (e.g. a quest's synced folder), forwarded ONLY
            # to a runner whose run_goal accepts that kwarg (older signatures are untouched). See
            # quest_autopilot_design.md's execution-environment section: "one quest, one folder,
            # one env" -- the deep agent starts where that quest's real work lives.
            wants_working_dir = (active_runner is not None
                                 and _run_goal_accepts_working_dir(active_runner))

            def _emit_one(ev: ProgressEvent) -> None:
                # TEE: classify any EVENT_EXEC phase into the fact, then forward to the live sink.
                try:
                    if getattr(ev, "type", None) == EVENT_EXEC and isinstance(ev.data, dict):
                        # The runner-level exec event knows its run_id but not WHICH subgoal it
                        # serves (the brain owns that). Stamp this subgoal's text onto the event so
                        # the consumer can show "what this deep task was assigned to do", and capture
                        # the run_id so the completion milestone below can be tied back to this run.
                        if not ev.data.get("goal"):
                            ev.data["goal"] = goal
                        rid = ev.data.get("run_id")
                        if rid:
                            captured_run_id["id"] = rid
                    if fact is not None and getattr(ev, "type", None) == EVENT_EXEC:
                        phase = (ev.data or {}).get("phase") if isinstance(ev.data, dict) else None
                        cls = classify_exec_phase(phase)
                        if phase:
                            fact.phases.append(str(phase))
                        if cls == "success":
                            fact.succeeded = True
                        elif cls == "failure":
                            fact.failed = True
                except Exception:  # noqa: BLE001 — recording must never break the run
                    pass
                if wants_emit and emit is not None:
                    log.debug(f"emitting exec event: {getattr(ev, 'type', '?')}: "
                             f"{(getattr(ev, 'text', '') or '')[:80]}")
                    emit.emit(ev)

            def _do_run(current_brief: str, run_model: Optional[str]) -> DeepResult:
                try:
                    if active_runner is None:
                        return DeepResult(met=False, error="no deep runner configured")
                    kwargs = dict(goal=goal, brief=current_brief, model=run_model,
                                  max_turns=self.cfg.deep_max_turns)
                    if wants_emit:
                        kwargs["emit"] = _emit_one
                    if wants_run_id:
                        # task_uuid is generated ONCE per subgoal (before the retry loop below), so
                        # every attempt -- even one that spawns a brand-new subprocess/session --
                        # reports under the same id. Without this, a consumer's dashboard would show
                        # each retry as a new, duplicate deep-run entry for one ongoing subgoal.
                        kwargs["run_id"] = task_uuid
                    if wants_preamble:
                        preamble_parts = []
                        if rep_preamble:
                            preamble_parts.append(rep_preamble)
                        # MAIN-FLOW ACCUMULATION vs SUBGOAL FOCUS. A single main-flow deep run
                        # ACCUMULATES: it carries the brain's gathered content forward. A fanned-out
                        # subgoal (multi) does NOT inherit that whole pile -- it is handed ONLY its
                        # own focused context so it concentrates on its piece (it can still search for
                        # more if it falls short, via the widening below).
                        # Exclude discovery/capability listings (the operations/sources MENU): they
                        # are planner-only routing aids, not content the worker should ground on (the
                        # worker has its own tools). Pass forward only the real content the brain read.
                        _brain_content = [o for o in (gathered or []) if not _is_discovery_obs(o)]
                        if _brain_content and not multi:
                            preamble_parts.append(
                                "--- RELEVANT CONTENT FOUND BY THE BRAIN ---\n"
                                + _render_gathered(_brain_content)
                            )
                        # This goal's OWN selected context, plus anything pulled by widening on a
                        # prior retry that reported it lacked context. Each goal therefore runs with
                        # context targeted at IT, not just the shared run-level view.
                        if per_goal_context:
                            preamble_parts.append(per_goal_context)
                        if extra_context:
                            preamble_parts.extend(extra_context)
                        if preamble_parts:
                            kwargs["context_preamble"] = "\n\n".join(preamble_parts)
                    if wants_working_dir and working_dir_override:
                        kwargs["working_dir"] = working_dir_override
                    # active_runner was already resolved once per task, above (not re-resolved per
                    # retry — the classifier's inputs don't change across retries of the same task).
                    # THE SEAM: every DeepResult, from every runner, is normalized here — the
                    # FUTURE-CONTEXT bullets are moved out of ``output`` into ``future_context``, so
                    # the payload handed to the goal verifier, the emit paths, and the consumer can
                    # never carry the section, and the card updater has exactly one place to read.
                    return _normalize_future_context(active_runner.run_goal(**kwargs))
                except Exception as e:  # noqa: BLE001
                    log.error(f"Deep runner failed: {type(e).__name__}: {e}", exc_info=True)
                    return DeepResult(met=False, error=type(e).__name__)

            # OUR OWN GOAL LOOP (replaces Claude Code's /goal): run the worker, then VERIFY the
            # done-standard at the quality bar with ONE small ``verify_tier`` LLM call (judged
            # through the rep persona and the applicable guidance cards). If not met, feed back what fell short + what to do
            # next AND ESCALATE to a stronger model, then re-run — WHILE under the overall token
            # budget (bounded by a hard attempt cap). More token-efficient than /goal (no per-turn
            # self-check inside the worker), the brain steers each retry, and the model auto-escalates.
            base_brief = brief
            current_brief = brief
            max_iters = max(1, self.cfg.deep_goal_max_iterations)
            budget = self.cfg.deep_goal_token_budget
            # The model ladder for THIS turn: an explicit per-task / guidance model pins it (no
            # escalation); otherwise fast -> strong, starting at the fast tier by default.
            deep_models = self._deep_models(model_hint, quality_standards, model)
            tier_idx = 0
            tokens_used = 0
            res = DeepResult(met=False)
            for attempt in range(1, max_iters + 1):
                # Cooperative cancellation, checked before starting each new attempt (a retry can be
                # a full agentic subprocess run, so this is the natural point to stop rather than
                # mid-subprocess). ``res`` keeps whatever the prior attempt produced.
                if cancel_check is not None and cancel_check():
                    break
                run_model = deep_models[min(tier_idx, len(deep_models) - 1)]
                if emit is not None and attempt > 1:
                    emit.status("Goal not met yet, retrying"
                                + (f" with {run_model}" if run_model else "") + "…")
                # Fold in any NEW user messages that arrived since the run started, so this process
                # (the first attempt or a retry) acts on the latest input, not a stale request.
                _new = self._drain_pending(pending_inputs)
                run_brief = current_brief if not _new else (current_brief + "\n\n" + _new)
                res = _do_run(run_brief, run_model)
                tokens_used += max(0, getattr(res, "tokens", 0) or 0)
                # ASYNC HAND-OFF: the runner queued the real run to finish out-of-band (its
                # ``output`` is a "task #N launched"-style sentinel, not work product). Re-verifying
                # that sentinel against the goal would ALWAYS fail and relaunch a fresh task every
                # iteration (a runaway loop). Trust the hand-off's own ``met`` and stop; the real
                # outcome is verified when it reflects back.
                if getattr(res, "deferred", False):
                    break
                # A human-decision escalation, or a hard failure with NO output (binary missing,
                # timeout, silent no-op), is terminal — do not verify or iterate.
                if res.decision_id or (res.error and not (res.output or "").strip()):
                    break
                # Verify the done-standard ourselves, applying the quality standards (guidance) and
                # the rep persona. verdict is None => verification could not run (LLM outage, no
                # verify tier, parse failure) => this run is UNVERIFIED, and must NEVER be reported
                # as done just because the worker's own exit code said success (that was the exact
                # "verifier outage silently re-opens 'said Completed but did nothing'" bug — see
                # HANDS_FREE_QUEST_AI_DESIGN.md section 2). The real reason travels with the result.
                # ``context_layer=per_goal_context``: this goal's OWN assembled context (built once
                # above, unchanged across attempts) -- the SAME block this goal's worker actually
                # received in its ``context_preamble`` (see ``_do_run`` above). The turn-level
                # plan/answer context is not in scope in this per-goal closure and would not describe
                # what THIS worker saw anyway; per_goal_context is the truer "what the worker saw"
                # for a deep goal, and staying stable across attempts keeps this call's L2 cached
                # across retries of the same goal too.
                verdict, verify_error = self._verify_goal(goal, base_brief, res.output or "",
                                            rep_preamble=rep_preamble,
                                            quality_standards=quality_standards,
                                            context_layer=per_goal_context)
                if verdict is None:
                    reason = verify_error or "verification did not run for an unknown reason"
                    res.met = False
                    res.error = ("Unverified: goal verification did not run (" + reason
                                 + "). Not confirmed complete.")
                    if emit is not None:
                        emit.status("Could not verify the result (" + reason
                                    + "); marking unverified.")
                    break
                if verdict.get("met"):
                    res.met = True
                    if emit is not None:
                        emit.status("Goal verified met.")
                    break
                # Not met: record why; escalate the model; stop if the token budget is spent.
                res.met = False
                reason = verdict.get("reason") or "done-standard not satisfied"
                res.error = res.error or ("goal not yet met: " + reason)
                if emit is not None:
                    # Always show what was attempted (the output) first
                    output_text = _strip_future_context(res.output).strip()
                    if output_text:
                        emit.emit(ProgressEvent(type=EVENT_MILESTONE,
                                               text=f"Worker output:\n{output_text[:600]}",
                                               data={"attempt": attempt}))
                    else:
                        emit.emit(ProgressEvent(type=EVENT_MILESTONE,
                                               text="Worker returned no output.",
                                               data={"attempt": attempt}))
                    # Show the specific reason why the goal wasn't met
                    if verdict and verdict.get("reason"):
                        emit.status("Goal not met: " + verdict.get("reason"))
                    else:
                        emit.status("Goal not met: " + reason)
                # WIDEN: if the verifier says the worker lacked context, pull MORE for the next
                # attempt (a fresh assembler read for the named missing context, wider conversation
                # retrieval, and a targeted retrieval grep). The widening grows with each round so a
                # retry always sees more than the last, never the same context again. Added to
                # ``extra_context`` so it rides the next ``_do_run`` preamble for THIS goal.
                if verdict.get("need_more_context"):
                    widened = self._widen_for_goal(
                        goal, verdict.get("context_query") or reason, attempt,
                        ctx_meta=ctx_meta)
                    if widened:
                        extra_context.append(widened)
                        if emit is not None:
                            emit.status("Fetching more context for the next attempt…")
                # TIER: prefer the verifier's explicit ``next_tier`` (resolved through the registry)
                # when it names a real tier; otherwise step one rung up the deep-model ladder, since a
                # capability gap is a common cause of a not-met goal. Either way the next attempt runs
                # at the chosen tier.
                _vt = verdict.get("next_tier")
                _resolved_tier = self._resolved_deep_tier(_vt, deep_models) if _vt else None
                if _resolved_tier is not None:
                    run_model = _resolved_tier
                    deep_models[min(tier_idx, len(deep_models) - 1)] = _resolved_tier
                    if emit is not None:
                        emit.status(f"Switching model tier to {_vt} for the next attempt…")
                elif tier_idx < len(deep_models) - 1:
                    tier_idx += 1  # a capability gap is a common cause; try a stronger model next
                if budget is not None and tokens_used >= budget:
                    if emit is not None:
                        emit.status(f"Deep token budget reached ({tokens_used}/{budget}); stopping.")
                    break
                current_brief = self._augment_brief(base_brief, res.output or "", verdict)

            # res.met is now the brain-verified outcome (not just the worker's exit code). The fact
            # records it for the broken-promise guard; a verified-not-met run is a confirmed failure.
            if fact is not None:
                if res.met:
                    fact.succeeded = True
                else:
                    fact.failed = True
                    fact.error = fact.error or res.error
                exec_record.facts.append(fact)
            if emit is not None and res.met:
                # A completed subtask is a real milestone — surfaces even in BACKGROUND. Carry the
                # FULL task output so consumers can show exactly what THIS deep task produced (not
                # just a "Completed" line); the terminal renders it in full under the goal header.
                emit.emit(ProgressEvent(type=EVENT_MILESTONE, text=f"Completed: {goal}",
                                        data={"goal": goal,
                                              "run_id": captured_run_id["id"],
                                              "deep_output": _strip_future_context(res.output).strip() or None}))
            # WARM recent-context write-back (see core/recent_context.py): record the cards+items
            # THIS goal's context actually included, under every applicable scope key, so a task
            # follow-up (another deep goal on the same quest/conversation, or the next chat turn)
            # warm-starts on what this run already found -- the completeness half of the fallback:
            # a background/deep run benefits from warm memory the same way an interactive turn
            # does. Keyed by the GOAL text (what the context was selected FOR). Best-effort, never
            # raises, and runs regardless of whether the goal was ultimately verified met (the
            # context was genuinely used either way).
            if (self.recent_context is not None and self.cfg.recent_context_enabled
                    and per_goal_cards):
                try:
                    goal_scope_keys = self._recent_scope_keys(ctx_meta)
                    if goal_scope_keys:
                        self.recent_context.record(goal_scope_keys, per_goal_cards, goal)
                except Exception:  # noqa: BLE001
                    log.debug("per-goal recent-context record failed", exc_info=True)
            return res

        # Handle nested task groups for sequential dependencies:
        # - flat task dict = run in parallel with others
        # - [task, task, ...] = run sequentially within group, in parallel with other groups
        def is_sequential_group(item):
            return isinstance(item, list)

        # Separate sequential groups from flat tasks
        flat_tasks = []
        seq_groups = []
        for item in subtasks:
            if is_sequential_group(item):
                seq_groups.append([t for t in item if isinstance(t, dict)])
            elif isinstance(item, dict):
                flat_tasks.append(item)

        # Execute: run all groups & flat tasks in parallel, but within each group tasks run sequentially
        all_results: List[Optional[DeepResult]] = []
        all_goals: List[str] = []

        def run_sequential_group(group: List[Dict[str, Any]], group_index: int = 0) -> List[Optional[DeepResult]]:
            """Run tasks in a group sequentially, returning results in order."""
            results = []
            for task_num, task in enumerate(group, 1):
                res = run_one(task, task_index=f"{group_index}.{task_num}")
                results.append(res)
                all_goals.append((task.get("goal") or "").strip() or user_message)
            return results

        # If only flat tasks (no groups), run all in parallel
        if not seq_groups and flat_tasks:
            if len(flat_tasks) == 1:
                res = run_one(flat_tasks[0], task_index=1)
                all_results.append(res)
                all_goals.append((flat_tasks[0].get("goal") or "").strip() or user_message)
            else:
                workers = min(self.cfg.max_parallel, len(flat_tasks))
                results: List[Optional[DeepResult]] = [None] * len(flat_tasks)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = {pool.submit(run_one, st, i+1): i for i, st in enumerate(flat_tasks)}
                    for f in futs:
                        results[futs[f]] = f.result()
                all_results.extend([r for r in results if r is not None])
                for st in flat_tasks:
                    all_goals.append((st.get("goal") or "").strip() or user_message)

        # If there are sequential groups, run them with flat tasks in parallel
        # Each group's tasks run sequentially, but groups run parallel with each other
        elif seq_groups or flat_tasks:
            # Combine: each group + each flat task becomes a unit in the thread pool
            all_units = seq_groups + [[t] for t in flat_tasks]  # Wrap flat tasks in lists
            workers = min(self.cfg.max_parallel, len(all_units))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(run_sequential_group, unit, i+1): i for i, unit in enumerate(all_units)}
                for f in futs:
                    group_results = f.result()
                    all_results.extend([r for r in group_results if r is not None])

        # Aggregate cancellation check: the subtasks above may have stopped early (each attempt
        # loop breaks cooperatively), so re-check once more here before reporting the outcome as a
        # normal "deep" result. This is the point that decides whether the CALLER (Orchestrator.run)
        # sees this as a genuine (possibly partial) deep outcome or a cancelled one.
        if cancel_check is not None and cancel_check():
            return OrchestratorResult(kind="cancelled", goals=all_goals, rationale=plan.rationale,
                                      exit_reason="cancelled")

        return OrchestratorResult(
            kind="deep",
            deep_results=[r for r in all_results if r is not None],
            goals=all_goals,
            rationale=plan.rationale,
        )

    # --- async post-deep context-card updater (prepare for the FUTURE) -------

    def _card_updater_active(self) -> bool:
        """True when the async post-deep card updater can run: the toggle is on, a provider is
        wired, and the wired context_assembler exposes (or wraps) the card-update API. Never raises.

        When False, the deep loop is byte-for-byte unchanged: no future-context instruction is
        appended to deep briefs, and no updater LLM call is made.
        """
        try:
            return bool(self.cfg.async_card_update
                        and self.provider is not None
                        and _card_update_store(self.context_assembler) is not None)
        except Exception:  # noqa: BLE001
            return False

    def _select_current_cards(self, query: str,
                              ctx_meta: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Select the user's CURRENT relevant cards for ``query`` via the wired assembler, so the
        updater can CORRECT existing ones rather than only add. Returns the assembler's
        ``card_metadata`` list (``[{id, title, files, ...}]``) or []. Never raises."""
        if self.context_assembler is None or not (query or "").strip():
            return []
        try:
            assembled = self.context_assembler.assemble(query, meta=ctx_meta or None)
            return list(getattr(assembled, "card_metadata", None) or [])
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _scoped_card_id(card_id: str, user_id: Optional[str]) -> str:
        """Attribute a card id to a user so cards never leak across users. Existing ids that already
        carry the user prefix (an UPDATE of a current card) are left as-is; a fresh slug is prefixed
        with ``u:<user_id>:`` so a created card is user-scoped. No user_id means no change (single-
        tenant / unscoped deployments behave as before). Never raises."""
        cid = (card_id or "").strip()
        if not user_id:
            return cid
        prefix = f"u:{user_id}:"
        if not cid:
            return prefix
        return cid if cid.startswith(prefix) else prefix + cid

    def _update_cards_after_deep(
        self,
        *,
        request: str,
        executed: str,
        future_context: str,
        ctx_meta: Optional[Dict[str, Any]],
    ) -> int:
        """SYNC post-deep card updater (the loop also runs this in a background thread).

        Makes ONE cheap LLM call that turns the finished run into a STRUCTURED set of card edits
        (fields name/description + content add/replace/remove), then applies them via the card-update
        API, user-scoped by ``ctx_meta['user_id']`` and bounded by the config caps. Returns the number
        of cards it successfully wrote (0 when inactive, nothing to do, or a parse miss). Best-effort:
        NEVER raises and NEVER affects the OrchestratorResult. Exposed as a sync method so a test can
        call it directly; the loop invokes it off the result path in a thread.
        """
        try:
            store = _card_update_store(self.context_assembler)
            if store is None or self.provider is None or not self.cfg.async_card_update:
                return 0
            user_id = (ctx_meta or {}).get("user_id")
            current_cards = self._select_current_cards(request, ctx_meta)
            current_view = self._render_cards_for_updater(current_cards)
            # The user-scoped ids of the cards we SHOWED the updater (its CURRENT CARDS). These are
            # exactly the ids it can reference for an UPDATE; any other id is a would-be CREATE and is
            # eligible for the semantic-merge redirect below.
            known_ids = {
                self._scoped_card_id(str(c.get("id")), user_id)
                for c in current_cards
                if isinstance(c, dict) and c.get("id")
            }

            model = self.registry.resolve_tier(self.cfg.planner_tier)
            prompt = CARD_UPDATE_PROMPT.format(
                request=(request or "")[:2000],
                executed=(executed or "")[:6000],
                future_context=(future_context or "(none)")[:3000],
                current_cards=current_view or "(no current cards)",
            )
            # Prefer a forced-tool structured return; degrade to text + _extract_json on a provider
            # that only does plain answers. The model can return an empty set even when the
            # future-context names reusable sources, so retry ONCE on empty (still at most two cheap
            # calls). Any miss after that -> do nothing.
            edits = self._call_card_updater(prompt, model)
            if not edits:
                edits = self._call_card_updater(prompt, model)
            if not edits:
                return 0
            return self._apply_card_edits(store, edits, user_id, known_card_ids=known_ids)
        except Exception:  # noqa: BLE001 — the updater must never affect the run
            log.debug("post-deep card update failed", exc_info=True)
            return 0

    def _call_card_updater(self, prompt: str, model: str) -> List[Dict[str, Any]]:
        """Make the single updater LLM call and return the parsed ``edits`` list (or []). Never
        raises. Uses forced tool use when the provider supports ``plan``; else parses ``answer``
        text with the repo's ``_extract_json`` helper. A parse miss yields []."""
        raw: Any = None
        try:
            if hasattr(self.provider, "plan"):
                raw = self.provider.plan(prompt, model=model, tool_schema=CARD_UPDATE_TOOL)
        except Exception:  # noqa: BLE001 — fall through to the text path
            raw = None
        # plan() may return a dict, a bare list (tool args as an array), or None. Only fall back to
        # the text path when it gave us nothing usable.
        if not isinstance(raw, (dict, list)) or (isinstance(raw, list) and not raw):
            try:
                txt = self.provider.answer([{"role": "user", "content": prompt}], model=model)
            except Exception:  # noqa: BLE001
                return []
            try:
                raw = json.loads(_extract_json(txt or "") or "null")
            except Exception:  # noqa: BLE001 — parse miss: do nothing
                return []
        return _normalize_card_edits(raw)

    def _apply_card_edits(self, store: Any, edits: List[Dict[str, Any]],
                          user_id: Optional[str],
                          known_card_ids: Optional[set] = None) -> int:
        """Apply parsed card edits via the card-update API, user-scoped + bounded. Returns the count
        of cards written. Never raises (each card edit is independently guarded).

        SEMANTIC CARD-MERGE (item 3): for an edit that would CREATE a new card (its user-scoped id is
        NOT one of ``known_card_ids`` -- the cards the updater was actually shown, the only ids it can
        target for an update), first ask the store's OPTIONAL ``find_similar_card`` capability whether
        a sufficiently-similar card already exists in THIS user's scope; if so, REDIRECT the edit to
        UPDATE that card (merging the proposed fields + content) instead of creating a near-duplicate
        twin. The capability is detected by duck-typing (``callable(getattr(store, ...))``), so a card
        store without embeddings (the keyword-only ``FileContextStore``) is skipped entirely and the
        edit creates as before. Disabled when the threshold is >= 1.0. Any miss/error -> create as
        before; an edit that already targets a known existing card id is left untouched.
        """
        written = 0
        max_cards = max(0, self.cfg.async_card_update_max_cards)
        max_edits = max(0, self.cfg.async_card_update_max_edits_per_card)
        known = known_card_ids or set()
        finder = getattr(store, "find_similar_card", None)
        merge_threshold = float(getattr(self.cfg, "card_merge_similarity",
                                        DEFAULT_CARD_MERGE_SIMILARITY))
        merge_enabled = callable(finder) and merge_threshold < 1.0
        for edit in edits[:max_cards]:
            try:
                cid = self._scoped_card_id(str(edit.get("card_id") or "").strip(), user_id)
                if not cid:
                    continue
                # If this edit would CREATE a new card, try to redirect it onto a clear existing twin
                # for the SAME user (embedding similarity) rather than minting a near-duplicate.
                if merge_enabled and cid not in known:
                    try:
                        text = _proposed_card_text(edit)
                        if text:
                            match = finder(text, user_id=user_id, min_score=merge_threshold)
                            if isinstance(match, str) and match.strip():
                                cid = match.strip()
                    except Exception:  # noqa: BLE001 — merge is best-effort: fall back to create
                        log.debug("card-merge similarity check failed", exc_info=True)
                fields: Dict[str, Any] = {}
                for _k in ("name", "description"):
                    v = edit.get(_k)
                    if isinstance(v, str) and v.strip():
                        fields[_k] = v.strip()
                add_items = [it for it in (edit.get("add") or []) if isinstance(it, dict)]
                # STAMP each new item with NOW. The card content model ranks and trims by ``ts``, and
                # an item that arrives without one is treated as maximally old: unstamped references
                # rank below anything dated and are the first trimmed, so a run's freshly learned
                # references would decay out of the card they were written to. The updater LLM has no
                # clock, so the brain stamps them.
                _now = time.time()
                for _it in add_items:
                    try:
                        if not float(_it.get("ts") or 0.0) > 0.0:
                            _it["ts"] = _now
                    except (TypeError, ValueError):
                        _it["ts"] = _now
                replace_in = [r for r in (edit.get("replace") or []) if isinstance(r, dict)]
                remove_ids = [str(r) for r in (edit.get("remove") or [])
                              if isinstance(r, (str, int))]
                # Bound total operations per card so one card can't run away.
                budget = max_edits
                add_items = add_items[:budget]
                budget -= len(add_items)
                replace_pairs: List[Tuple[str, Dict[str, Any]]] = []
                for r in replace_in[:max(0, budget)]:
                    item_id = str(r.get("item_id") or "").strip()
                    item = r.get("item")
                    if item_id and isinstance(item, dict):
                        replace_pairs.append((item_id, item))
                budget -= len(replace_pairs)
                remove_ids = remove_ids[:max(0, budget)]
                if not (fields or add_items or replace_pairs or remove_ids):
                    continue
                ok = store.update_card(
                    cid,
                    add=add_items or None,
                    replace=replace_pairs or None,
                    remove=remove_ids or None,
                    fields=fields or None,
                )
                if ok:
                    written += 1
            except Exception:  # noqa: BLE001 — one bad edit must not stop the rest
                log.debug("applying a card edit failed", exc_info=True)
        if written:
            log.debug("post-deep card update wrote %d card(s)", written)
        return written

    @staticmethod
    def _render_cards_for_updater(cards: List[Dict[str, Any]]) -> str:
        """Render the user's current relevant cards (id + title + a few file pointers) for the
        updater prompt, so it can target existing card_ids and correct stale items. Never raises."""
        if not cards:
            return ""
        lines: List[str] = []
        for c in cards[:8]:
            if not isinstance(c, dict):
                continue
            cid = c.get("id", "?")
            title = c.get("title") or c.get("summary") or ""
            files = [str(f) for f in (c.get("files") or [])][:5]
            line = f"- [{cid}] {title}".rstrip()
            if files:
                line += "  (files: " + ", ".join(files) + ")"
            lines.append(line)
        return "\n".join(lines)

    def _update_cards_after_deep_async(
        self,
        *,
        request: str,
        executed: str,
        future_context: str,
        ctx_meta: Optional[Dict[str, Any]],
        emit: Optional[_Emitter] = None,
    ) -> None:
        """Spawn the post-deep card updater in a BACKGROUND daemon thread so it never blocks the
        returned answer. Inert when the updater is not active. Best-effort: a failure to even start
        the thread is swallowed. A quiet STATUS tick is emitted; it never surfaces as a user message.
        """
        if not self._card_updater_active():
            return

        def _bg() -> None:
            try:
                n = self._update_cards_after_deep(
                    request=request, executed=executed,
                    future_context=future_context, ctx_meta=ctx_meta)
                if n and emit is not None:
                    try:
                        emit.status(f"Updated {n} context card(s) for next time.")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 — the background updater must never raise out
                log.debug("background card updater failed", exc_info=True)

        try:
            threading.Thread(target=_bg, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass

    def _update_context_cards_after_deep(
        self,
        deep_result: OrchestratorResult,
        context_meta: Optional[Dict[str, Any]],
        project_path: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Update context cards with files modified during deep execution.

        Runs in background (non-blocking). Discovers edited files via multiple strategies:
        1. Claude Code: parses ~/.claude/projects/<project>/<session_id>/messages.jsonl
        2. Other runners: extracts from result metadata or parses prompted output format
        Then categorizes them into the context cards used for this task and updates
        card JSON files idempotently.

        Args:
            deep_result: The OrchestratorResult from deep execution
            context_meta: Metadata about context cards used for this task
            project_path: Optional path to project (for Claude Code session parsing)
            session_id: Optional session ID (for Claude Code session parsing)
        """
        if not context_meta or not context_meta.get("cards"):
            return

        def background_update():
            try:
                from ..adapters.context_card_updater import (
                    categorize_files_with_llm,
                    extract_edited_files,
                    update_context_cards,
                )

                # Discover edited files using multi-strategy extraction
                edited_files = extract_edited_files(
                    deep_result,
                    project_path=project_path,
                    session_id=session_id,
                )

                if not edited_files:
                    return

                # Categorize edited files into the context cards that were used
                categorization = categorize_files_with_llm(
                    edited_files,
                    context_meta["cards"],
                    model_provider=self.provider,
                    registry=self.registry,
                )

                if not categorization:
                    return

                # Update the cards
                card_store_dir = context_meta.get("card_store_dir", ".quest-context")
                update_context_cards(
                    context_meta["cards"],
                    categorization,
                    card_store_dir,
                )
                log.debug(f"Updated context cards with {len(edited_files)} edited files")
            except Exception as e:
                log.debug(f"Background context card update failed (non-blocking): {e}")

        # Launch in background (fire and forget, doesn't block task result)
        thread = threading.Thread(target=background_update, daemon=True)
        thread.start()

    def _kickoff_card_update(self, res: OrchestratorResult, plan: Optional[PlanDecision],
                             user_message: str, ctx_meta: Optional[Dict[str, Any]],
                             emit: Optional[_Emitter]) -> None:
        """Build the updater's input bundle from a finished deep result and kick off the async card
        updater. The request is the user's goal/condition; ``executed`` is the brief + each deep
        result's output; the FUTURE-CONTEXT bullets come from each result's ``future_context`` field
        (filled at the runner seam by ``_normalize_future_context``, from EITHER channel). Inert when
        the updater is not active or nothing executed. Never raises."""
        try:
            if not self._card_updater_active():
                return
            results = list(getattr(res, "deep_results", None) or [])
            if not results:
                return
            request = (getattr(plan, "goal", None) or "").strip() or user_message
            brief = (getattr(plan, "deep_brief", None) or "").strip()
            outputs = [(d.output or "").strip() for d in results if (d.output or "").strip()]
            executed_parts: List[str] = []
            if brief:
                executed_parts.append("BRIEF:\n" + brief)
            if outputs:
                executed_parts.append("RESULT:\n" + "\n\n".join(outputs))
            future = "\n".join(
                fc for fc in (_deep_future_context(d) for d in results) if fc
            )
            self._update_cards_after_deep_async(
                request=request,
                executed="\n\n".join(executed_parts),
                future_context=future,
                ctx_meta=ctx_meta,
                emit=emit,
            )
        except Exception:  # noqa: BLE001 — kicking off the updater must never break the turn
            log.debug("card-update kickoff failed", exc_info=True)

    # --- minimal-intervention overseer ---------------------------------------

    def _run_spend_metrics(self, gathered: List[Dict[str, Any]],
                           started: float) -> "Tuple[int, int, float]":
        """Cheap, pure metrics shared by the digest builder AND the Fix-12 pre-filter gate, computed
        once so they are never duplicated: ``(gathered_chars, consecutive_reads, elapsed_seconds)``.
        Never raises."""
        gathered_chars = 0
        consecutive_reads = 0
        try:
            gathered_chars = sum(len(o.get("text", "")) + len(str(o.get("hits", "")))
                                 for o in gathered if isinstance(o, dict))
            consecutive_reads = sum(1 for o in gathered
                                    if isinstance(o, dict) and o.get("kind") in ("read", "grep"))
        except Exception:  # noqa: BLE001
            pass
        elapsed_seconds = 0.0
        try:
            elapsed_seconds = time.monotonic() - started
        except Exception:  # noqa: BLE001
            pass
        return gathered_chars, consecutive_reads, elapsed_seconds

    def _oversee_operation_lines(self, gathered: List[Dict[str, Any]],
                                 window: int = 8) -> "Tuple[List[str], int]":
        """Build the ``operations``/``operations_total`` pair for the digest's OPERATIONS THIS TURN
        section (Fix 5b): each entry tagged with its observation ``kind`` (read/grep/query/...),
        e.g. ``"[read] cli.py [head]: found argparse subcommands..."``, reusing the SAME one-line
        summarizer the planner view uses so the full observation bodies never reach the overseer.
        ``operations_total`` is the TRUE count of operations so far (``operations`` may be a trailing
        window when the run is long). Never raises."""
        try:
            ops_all = [o for o in gathered if isinstance(o, dict)]
            recent = ops_all[-window:]
            lines = [f"[{o.get('kind') or 'op'}] {_summarize_observation(o)}" for o in recent]
            return lines, len(ops_all)
        except Exception:  # noqa: BLE001
            return [], 0

    def _build_oversee_digest(self, *, user_message: str, goal_condition: Optional[str] = None,
                              step: int, plan: Optional[PlanDecision],
                              gathered: List[Dict[str, Any]], started: float,
                              draft_answer: Optional[str] = None,
                              quality_standards: Optional[str] = None,
                              recent_conversation: Optional[List[str]] = None,
                              prior_escalations: Optional[List[str]] = None) -> str:
        """Build the cheap, capped overseer digest for the current run state. Pure + synchronous
        (no network): it only summarizes what is already in hand, so it is safe to run inline right
        before submitting the ONE provider call to a background thread. Never raises."""
        cfg = self.cfg
        gathered_chars, consecutive_reads, elapsed_seconds = self._run_spend_metrics(gathered, started)
        operations, operations_total = self._oversee_operation_lines(gathered)
        tokens_in = getattr(self.provider, "tokens_in", 0) or 0
        tokens_out = getattr(self.provider, "tokens_out", 0) or 0
        return build_digest(
            user_message=user_message,
            goal_condition=goal_condition,
            step=step,
            max_steps=cfg.max_steps,
            plan_action=(plan.action if plan else ""),
            plan_rationale=(plan.rationale if plan else ""),
            plan_goal=(plan.goal if plan else ""),
            recent_conversation=recent_conversation,
            prior_escalations=prior_escalations,
            operations=operations,
            operations_total=operations_total,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            elapsed_seconds=elapsed_seconds,
            max_elapsed_seconds=cfg.max_elapsed_seconds,
            gathered_chars=gathered_chars,
            max_gathered_chars=cfg.max_gathered_chars,
            consecutive_reads=consecutive_reads,
            draft_answer=draft_answer,
            quality_standards=quality_standards,
            char_budget=cfg.overseer_digest_char_budget,
        )

    def _submit_oversee(self, executor: ThreadPoolExecutor, *, user_message: str, step: int,
                        plan: Optional[PlanDecision], gathered: List[Dict[str, Any]],
                        started: float, goal_condition: Optional[str] = None,
                        draft_answer: Optional[str] = None,
                        quality_standards: Optional[str] = None,
                        recent_conversation: Optional[List[str]] = None,
                        prior_escalations: Optional[List[str]] = None,
                        prev_plan_signature: Optional[Any] = None,
                        gate: bool = True) -> Optional[Dict[str, Any]]:
        """FIRE-AND-FORGET consult (Fix 1): build the cheap digest synchronously, then submit the
        ONE overseer provider call to a BACKGROUND thread and return a pending-consultation record
        WITHOUT waiting. Recording + emitting happen later in ``_collect_oversee`` (when the result
        is applied), so ``overseer_signals`` only ever holds consults that actually completed. This
        mirrors the context-assembly background-thread idiom in ``run()``. Never raises: on any
        setup failure -- OR when ``gate=True`` and the cheap pre-filter (Fix 12) says this step is
        not worth a look -- it returns ``None`` (the caller simply proceeds as if the overseer were
        off / not yet due). ``gate=False`` (used by hook B, the one-time answer checkpoint) always
        submits, subject only to the caller's own ``overseer_max_signals`` bookkeeping.
        """
        try:
            gathered_chars, consecutive_reads, elapsed_seconds = self._run_spend_metrics(gathered, started)
            if gate:
                plan_repeats_prev = bool(
                    prev_plan_signature is not None and plan is not None
                    and (plan.action, plan.goal) == prev_plan_signature
                )
                if not _oversee_worth_a_look(
                    consecutive_reads=consecutive_reads,
                    plan_repeats_prev=plan_repeats_prev,
                    elapsed_seconds=elapsed_seconds,
                    max_elapsed_seconds=self.cfg.max_elapsed_seconds,
                    gathered_chars=gathered_chars,
                    max_gathered_chars=self.cfg.max_gathered_chars,
                    min_consecutive_reads=self.cfg.overseer_gate_min_consecutive_reads,
                    gate_repeat_plan=self.cfg.overseer_gate_repeat_plan,
                    spend_fraction=self.cfg.overseer_gate_spend_fraction,
                ):
                    return None
            digest = self._build_oversee_digest(
                user_message=user_message, goal_condition=goal_condition, step=step, plan=plan,
                gathered=gathered, started=started, draft_answer=draft_answer,
                quality_standards=quality_standards, recent_conversation=recent_conversation,
                prior_escalations=prior_escalations)
            model = self.registry.resolve_tier(self.cfg.overseer_tier)
            provider = self.get_provider_for_model(model)
            # FALLBACK (mirrors _verify_goal's tier ladder): a consult that comes back DEGRADED
            # (provider error / unusable response, not a real verdict) retries ONCE at the planner
            # tier. Without this, a deployment whose overseer tier resolves to a model the wired
            # provider cannot serve has a permanently silent overseer that looks exactly like a
            # healthy one — every consult "proceeds". Both calls run in the SAME background worker,
            # so the loop still never blocks.
            fb_model: Optional[str] = None
            try:
                fb_model = self.registry.resolve_tier(self.cfg.planner_tier)
            except Exception:  # noqa: BLE001 — no fallback tier, primary consult only
                fb_model = None
            fb_provider = self.get_provider_for_model(fb_model) if fb_model else None

            def _consult() -> OverseerSignal:
                sig = oversee(provider, model, digest)
                if sig.degraded and fb_model and fb_model != model and fb_provider is not None:
                    return oversee(fb_provider, fb_model, digest)
                return sig

            # oversee() itself never raises (degrades to proceed), so the worker is safe.
            future: Future = executor.submit(_consult)
            return {"future": future, "step": step, "plan": plan}
        except Exception:  # noqa: BLE001 — submission failure must never break the run
            return None

    def _collect_oversee(self, pending: Optional[Dict[str, Any]], *,
                         signals: List[Dict[str, Any]], emit: Optional[_Emitter],
                         timeout: float = 0.0) -> Optional[OverseerSignal]:
        """Poll a previously-submitted consult. Returns its ``OverseerSignal`` if it has RESOLVED,
        recording it into ``signals`` and emitting an EVENT_OVERSEER exactly as the old synchronous
        path did (so late-resolving consults are still recorded). Returns ``None`` when it has NOT
        resolved yet (the caller proceeds as if ``proceed``).

        ``timeout=0.0`` is a pure, non-blocking ``future.done()`` check that NEVER blocks (the design
        default). A small positive ``timeout`` lets it briefly wait for a still-running consult; on
        timeout it returns ``None`` (proceed), so it can never hang the run. Never raises.
        """
        if not pending:
            return None
        future = pending.get("future")
        step = pending.get("step", 0)
        plan = pending.get("plan")
        if future is None:
            return None
        try:
            if timeout and timeout > 0:
                try:
                    signal = future.result(timeout=timeout)
                except FuturesTimeoutError:
                    return None  # still running -> proceed, re-check next opportunity
            else:
                if not future.done():
                    return None  # not resolved yet -> proceed, never block
                signal = future.result()
        except Exception:  # noqa: BLE001 — a worker failure degrades to proceed
            signal = OverseerSignal("proceed")
        if not isinstance(signal, OverseerSignal):
            signal = OverseerSignal("proceed")
        signals.append({"signal": signal.signal, "hint": signal.hint,
                        "reason": signal.reason, "step": step})
        if emit is not None:
            try:
                emit.emit(ProgressEvent(
                    type=EVENT_OVERSEER, step=step,
                    action=(plan.action if plan else None),
                    text=(signal.reason or None),
                    data={"signal": signal.signal, "hint": signal.hint}))
            except Exception:  # noqa: BLE001
                pass
        return signal

    def _finish_oversee_in_background(self, pending: Optional[Dict[str, Any]], *,
                                      emit: Optional[_Emitter],
                                      quest_id: Optional[str] = None,
                                      brainstorm_active: bool = False) -> None:
        """Fire-and-forget continuation for hook B (Fix 11): when the answer-checkpoint consult has
        NOT resolved by the time the answer ships, the answer is returned immediately and this
        BACKGROUND daemon thread waits (bounded by ``overseer_background_finish_timeout_seconds``)
        for it, then applies a BEST-EFFORT, non-authoritative follow-up -- never mutates the already-
        returned ``OrchestratorResult`` (it may already be gone by the time this runs):

          - ``escalate_human``: raises a REAL decision-request via the wired ``EscalationSink`` (the
            same durable mechanism ``_run_confirm`` uses), so a human is notified even though the
            stream that served this turn's answer may already be closed. This is the one case where
            "best-effort" is still durable, since decision-requests are their own async channel.
          - ``redirect`` / ``escalate_deep``: recorded via a late ``EVENT_OVERSEER`` (data includes
            ``"late": True``) so a STILL-LISTENING consumer can see it, and so a caller that persists
            these events can pass them forward as ``prior_escalations`` on the NEXT turn (see
            ``run()``'s ``prior_escalations`` param and the digest's PRIOR ESCALATIONS THIS
            CONVERSATION section). Deliberately does NOT autonomously launch a new deep execution
            here: nothing is left to receive its result once the original caller has already moved on
            with its answer, so auto-running unattended work with no supervision is out of scope; see
            docs/overseer.md for this tradeoff.
          - ``proceed`` / a timeout: nothing to do.

        ``brainstorm_active`` (the latch's value when the answer shipped) mirrors the synchronous
        hook-B rule: while a brainstorm turn is active, overseer escalations may not ADD actions,
        so a late ``escalate_human`` raises NO decision-request (the late event is still emitted
        as telemetry); ``proceed``/``redirect``/``answer_now`` handling is unchanged.

        Never raises. Runs in a daemon thread so it cannot keep the process alive or crash on
        interpreter shutdown; started best-effort (a failure to even start the thread is swallowed).
        """
        if not pending:
            return
        future = pending.get("future")
        step = pending.get("step", 0)
        plan = pending.get("plan")
        if future is None:
            return

        def _bg() -> None:
            try:
                signal = future.result(timeout=self.cfg.overseer_background_finish_timeout_seconds)
            except Exception:  # noqa: BLE001 — timeout or worker failure: nothing to finish
                return
            if not isinstance(signal, OverseerSignal) or signal.signal == "proceed":
                return
            if emit is not None:
                try:
                    emit.emit(ProgressEvent(
                        type=EVENT_OVERSEER, step=step,
                        action=(plan.action if plan else None),
                        text=(signal.reason or None),
                        data={"signal": signal.signal, "hint": signal.hint, "late": True}))
                except Exception:  # noqa: BLE001
                    pass
            if (signal.signal == "escalate_human" and not brainstorm_active
                    and self.escalation is not None):
                try:
                    q = signal.reason or "A prior response may need your input; please confirm."
                    self.escalation.escalate(Escalation(
                        summary=self._concise_decision_summary(q),
                        kind="approve", quest_id=quest_id, default_on_silence="hold"))
                except Exception:  # noqa: BLE001
                    pass

        try:
            threading.Thread(target=_bg, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass

    # --- clarify (user selection/clarification) --------------------------------

    def _concise_decision_summary(self, text: str) -> str:
        """Return a concise summary safe to store as a Quest goal CONDITION.

        A decision summary becomes a goal condition (a short done-standard), so raw text (a verbose
        planner question, a brief, dumped analysis) must not go there. A short summary passes through
        unchanged; a long one is condensed by a cheap LLM call into a one or two sentence ask, with a
        hard-truncation fallback if the model call fails. Never raises.
        """
        s = (text or "").strip()
        if len(s) <= _CONCISE_DECISION_LIMIT:
            return s
        try:
            model = self.registry.resolve_tier(self.cfg.planner_tier)
            out = self.provider.answer(
                [{"role": "user", "content": _CONDENSE_DECISION_PROMPT.format(text=s[:6000])}],
                model=model,
            )
            if isinstance(out, str) and out.strip():
                return out.strip()[:_CONCISE_DECISION_LIMIT]
        except Exception:  # noqa: BLE001 — condensing must never break an escalation
            pass
        return s[:_CONCISE_DECISION_LIMIT].rstrip() + " [...]"

    # --- STEP 1: User Input Understanding ------------------------------------

    def _needs_context_to_understand(self, msg: str) -> bool:
        """Cheap, NO-LLM check: does this message lean on conversation context to be understood?

        Returns True for short/anaphoric inputs that can't stand alone — a pure acknowledgement
        ("ok", "go ahead"), a very short message (<= ~5 words), or one that leans on a pronoun /
        anaphor ("it", "that", "the first one", "as we discussed") WITHOUT a concrete noun to anchor
        it. CONSERVATIVE by design: when unsure, return False so a self-contained input skips the
        LLM hop entirely and adds zero latency. Never raises."""
        try:
            s = (msg or "").strip().lower()
            if not s:
                return False
            # Pure acknowledgement / deferral (whole message is one of the ack phrases): needs context.
            if s.rstrip(".!?") in _ACK_PHRASES:
                return True
            words = s.split()
            # Very short messages can't carry a self-contained instruction on their own.
            if len(words) <= 5:
                return True
            # Longer messages: only treat as context-dependent when they lean on an anaphor AND have
            # no concrete noun-like anchor. We approximate "concrete noun" as a token > 3 chars that
            # is not itself a stopword/anaphor/ack word — conservative, so we DON'T fire on a request
            # that names its own subject (e.g. "update the pricing docs to mention the new tier").
            if _ANAPHORA_RE.search(s):
                _generic = _ACK_PHRASES | {
                    "the", "a", "an", "and", "or", "but", "to", "of", "for", "with", "please",
                    "can", "you", "could", "would", "will", "should", "it", "its", "that", "this",
                    "those", "these", "them", "they", "one", "first", "second", "last", "other",
                    "previous", "as", "we", "discussed", "like", "before", "same", "now", "then",
                }
                has_concrete_noun = any(
                    len(w.strip(".,!?:;\"'")) > 3 and w.strip(".,!?:;\"'") not in _generic
                    for w in words
                )
                return not has_concrete_noun
            return False
        except Exception:  # noqa: BLE001 — the gate must never break the run
            return False

    def _understand_input(self, user_message: str, conv_id: str, conv_scope: Dict[str, Any],
                          emit: "_Emitter"):
        """Resolve a short/anaphoric ``user_message`` into a self-contained GOAL CONDITION using the
        wired ``conversation_store``. Returns ``(goal_condition, conv_ctx_text, clarify_or_None)``:

          * ``goal_condition``  — the rewritten self-contained instruction (defaults to the raw
            message when resolution does not produce a better one).
          * ``conv_ctx_text``   — the conversation-context text actually pulled (current slice plus
            any related slice), so the caller can inject it into ``context_view``.
          * ``clarify_or_None`` — a short question to ask the user, or None.

        Iterative context expansion — stops as soon as the LLM resolves the message:
          1. Last 1 exchange (cheap — covers the vast majority of follow-up messages).
          2. Last 3 exchanges (if MORE_CONTEXT_NEEDED — catches multi-turn threads).
          3. Related past sessions (if still MORE_CONTEXT_NEEDED — cross-session recall).
          4. Escalate to CLARIFY if still unresolved.

        At most THREE LLM calls. Never raises (the caller also guards)."""
        store = self.conversation_store
        model = self.registry.resolve_tier(self.cfg.planner_tier)

        def _resolve(ctx_text: str) -> str:
            prompt = RESOLVE_REQUEST_PROMPT.format(
                conv_context=ctx_text or "=== CURRENT CONVERSATION ===\n(no prior conversation available)",
                user_message=user_message)
            try:
                # Same contract as _derive_goal_condition: this call resolves a request into a
                # done-standard, it does not talk to anyone. Without it, a model handed a bare
                # message answers it, and that answer gets labelled "Understood as: ...".
                out = self.provider.answer(
                    [{"role": "user", "content": prompt}], model=model,
                    system=GOAL_CONDITION_SYSTEM)
            except Exception:  # noqa: BLE001 — provider error degrades to "no resolution"
                return ""
            return (out or "").strip()

        def _current_block(recent_turns: int) -> str:
            cur = store.current_slice(conv_id, user_message, recent_turns=recent_turns)
            cur_text = (cur.text or "") if cur is not None else ""
            # Label the CURRENT conversation distinctly from any OTHER conversations pulled on
            # widening, so the resolver only borrows a referent when it clearly continues it
            # (a bare "do it" / "the third one" must CLARIFY, never grab an unrelated list).
            return f"=== CURRENT CONVERSATION ===\n{cur_text}" if cur_text else ""

        # Step 1: last 1 exchange — covers most follow-ups ("ok do it", "yes", "repeat that").
        conv_ctx_text = _current_block(recent_turns=1)
        reply = _resolve(conv_ctx_text)

        if reply == "MORE_CONTEXT_NEEDED":
            # Step 2: expand to last 3 exchanges — covers multi-turn threads.
            conv_ctx_text = _current_block(recent_turns=3)
            reply = _resolve(conv_ctx_text)

        if reply == "MORE_CONTEXT_NEEDED":
            # Step 3: pull related past sessions alongside the expanded current slice.
            try:
                rel = store.related_slices(user_message, conv_scope, exclude_conv_id=conv_id)
            except Exception:  # noqa: BLE001
                rel = None
            if rel is not None and rel.text:
                related_block = f"=== OTHER PAST CONVERSATIONS (may be unrelated) ===\n{rel.text}"
                conv_ctx_text = (conv_ctx_text + "\n\n" + related_block if conv_ctx_text
                                 else related_block)
            reply = _resolve(conv_ctx_text)

        if reply == "MORE_CONTEXT_NEEDED" or reply.startswith("CLARIFY:"):
            if reply.startswith("CLARIFY:"):
                q = reply[len("CLARIFY:"):].strip() or "Could you clarify what you'd like me to do?"
            else:
                q = "Could you clarify what you'd like me to do?"
            return user_message, conv_ctx_text, q

        # A successful resolution is the goal condition; fall back to the raw message if empty.
        goal_condition = reply if reply else user_message
        return goal_condition, conv_ctx_text, None

    def _derive_goal_condition(
        self, user_message: str, *, now: Optional[str] = None
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Derive a concise, CHECKABLE done-standard for a SELF-CONTAINED input (Fix 13).

        Unlike ``_understand_input`` (the context-FETCH path, gated by
        ``_needs_context_to_understand`` and only run for anaphoric/short follow-ups), this runs for
        EVERY turn that did NOT need conversation context -- goal-condition ESTABLISHMENT is a
        separate concern from context-fetching, and now always happens. Uses ONE cheap-tier LLM call
        (no conversation history needed, since the message is already self-contained) to restate the
        message as a concrete done-standard, mirroring the deep planner's own ``goal`` field in
        spirit: "the SHORT, CHECKABLE DONE-STANDARD... the single condition that means the work is
        COMPLETE", not just a restatement.

        Query-aware retrieval routing (spec v3, work package C): the SAME reply may ALSO carry
        optional structured retrieval constraints (time_range/topic_terms/actor/content_kind), parsed
        by ``parse_goal_condition_reply`` -- no new LLM call, just more parsed from the one call this
        already makes. ``now`` (optional ISO date/datetime string) resolves relative date expressions
        in the message ("Wednesday", "last week") against a real "today"; see ``_format_now_block``.
        Returns ``(goal_condition, constraints)``; ``constraints`` is None exactly when nothing in
        the message calls for filtering -- today's behavior, byte for byte, for ``goal_condition``.

        Fails safe: any exception, or an empty/unusable reply, degrades to the raw ``user_message``
        unchanged with no constraints (today's fallback), and never breaks the run. Deliberately uses
        a CHEAP tier ("fast", never "best"): this adds one LLM round trip to every turn that reaches
        here, so it must stay fast and inexpensive.
        """
        # A bare greeting/thanks has no request to resolve. Asking a cheap model to restate it is how
        # "Hello" came back as "Hello. How can I help you with your Quests today?" and ended up
        # echoed above the reply. Skip the call: the message IS its own done-standard.
        if is_small_talk(user_message):
            return user_message, None
        try:
            model = self.registry.resolve_tier("fast")
            prompt = DERIVE_GOAL_CONDITION_PROMPT.format(
                user_message=user_message, now_block=_format_now_block(now))
            out = self.provider.answer(
                [{"role": "user", "content": prompt}], model=model,
                system=GOAL_CONDITION_SYSTEM)
            goal_condition, constraints = parse_goal_condition_reply(out or "")
            return (goal_condition or user_message), constraints
        except Exception:  # noqa: BLE001 — must never break the run
            return user_message, None

    def _run_clarify(self, plan: PlanDecision, *, quest_id: Optional[str] = None,
                     emit: Optional[_Emitter] = None) -> OrchestratorResult:
        """Surface user clarification/selection need as a decision request.

        Creates a decision-request with the question and options (if any) so the user can
        respond on the frontend or terminal. Returns with kind="confirm" to trigger UI.
        """
        clarif = plan.clarification or {}
        question = (clarif.get("question") or "").strip() or "Need your input to proceed"
        options = clarif.get("options") or []
        allow_free = clarif.get("allow_free_input", False)
        # Same rendering the brainstorm (non-escalating) path uses, so the user is asked the same
        # question whichever way it reaches them.
        question_with_opts = _clarify_question_text(plan)

        decision_id = None
        if self.escalation is not None:
            try:
                decision_id = self.escalation.escalate(Escalation(
                    # Condense to a concise done-standard: a decision summary is stored as a goal
                    # CONDITION, never a place to dump raw text.
                    summary=self._concise_decision_summary(question_with_opts),
                    kind="clarify" if options or allow_free else "approve",
                    quest_id=quest_id,
                    default_on_silence="hold"))
            except Exception:  # noqa: BLE001
                decision_id = None

        if emit is not None:
            emit.emit(ProgressEvent(type=EVENT_MILESTONE,
                                    text=f"Clarification needed: {question}"))

        return OrchestratorResult(kind="confirm", question=question_with_opts,
                                  decision_id=decision_id, rationale=plan.rationale)

    # --- confirm -------------------------------------------------------------

    def _run_confirm(self, plan: PlanDecision, *, quest_id: Optional[str]) -> OrchestratorResult:
        question = (plan.confirm_question
                    or "Before I act on this — can you confirm you'd like me to go ahead?").strip()
        decision_id = None
        if self.escalation is not None:
            try:
                decision_id = self.escalation.escalate(Escalation(
                    # Condense long questions: the summary is stored as a goal CONDITION.
                    summary=self._concise_decision_summary(question),
                    kind="approve", quest_id=quest_id, default_on_silence="hold"))
            except Exception:  # noqa: BLE001 — escalation failure still returns the question
                decision_id = None
        return OrchestratorResult(kind="confirm", question=question, decision_id=decision_id,
                                  rationale=plan.rationale)

    def _finish_understanding_clarify(
        self, res: OrchestratorResult, *, emit: "_Emitter", exec_record: "ExecutionRecord",
        user_message: str, ctx_meta: Dict[str, Any]) -> OrchestratorResult:
        """Terminate the turn on a STEP 1 (understanding) CLARIFY, mirroring the confirm branch of
        ``run``'s ``finish``: attach the execution record, emit the decision + done events, and run
        the best-effort ContextAssembler write-back. The planner loop is NOT run. Never raises from
        the write-back (it is wrapped). Kept tiny and parallel to ``finish`` so a clarify produced by
        understanding reports IDENTICALLY to a planner-produced confirm (executor → needs_you)."""
        res.steps = 0
        res.gathered = []
        res.execution_record = exec_record
        if self.context_assembler is not None:
            try:
                self.context_assembler.record(
                    user_message,
                    {"kind": res.kind, "steps": 0, "files": [], "response": res.question,
                     **(ctx_meta or {})},
                )
            except Exception:  # noqa: BLE001 — write-back must never break the run
                pass
        emit.emit(ProgressEvent(type=EVENT_DECISION, text=res.question,
                                decision_id=res.decision_id, result_kind="confirm"))
        emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=0))
        return res

    # --- the loop ------------------------------------------------------------

    def run(self, user_message: str, *, transcript: str = "", context_view: str = "",
            quest_id: Optional[str] = None,
            mode: Mode = Mode.LIVE,
            sink: Optional[ProgressSink] = None,
            background_sink: Optional[ProgressSink] = None,
            detach_check: Optional[Callable[[], bool]] = None,
            model_hint: Optional[str] = None,
            attachments: Optional[List[Dict[str, Any]]] = None,
            context_meta: Optional[Dict[str, Any]] = None,
            rep_preamble: Optional[str] = None,
            working_dir_override: Optional[str] = None,
            pending_inputs: Optional[Callable[[], List[str]]] = None,
            conv_id: Optional[str] = None,
            conv_scope: Optional[Dict[str, Any]] = None,
            prior_escalations: Optional[List[Dict[str, Any]]] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
            card_thread: Optional[Any] = None,
            prior_narration: Optional[List[str]] = None,
            now: Optional[str] = None) -> OrchestratorResult:
        """Run the bounded loop for one request and return a terminal OrchestratorResult.

        Streaming/event interface (both lanes use the SAME emissions; the SINK decides policy):

        * ``mode``        — ``Mode.LIVE`` (attended; stream everything) or ``Mode.BACKGROUND``
                            (sent-off; surface only result/decision/milestone). Default LIVE.
        * ``sink``        — a ``ProgressSink`` the orchestrator routes every internal event
                            through. For LIVE pass a ``StreamSink``; for BACKGROUND a
                            ``MilestoneSink``. If ``None``, only the legacy ``status`` callback
                            fires (a plain non-streaming run, unchanged for old callers).
        * ``background_sink`` + ``detach_check`` — the LIVE↔BACKGROUND handoff. If a LIVE run is
                            abandoned (``detach_check()`` returns True, e.g. the consumer
                            disconnected), the run CONTINUES to completion and the REST of its
                            events (incl. the final result) are delivered via ``background_sink``
                            (a ``MilestoneSink``) instead of the dropped live stream.
        * ``model_hint``  — optional opaque model/tier string carried by the task or caller
                            (e.g. from ``task.model``). When set, it overrides the planner's own
                            tier choice and the compile-time default for answer and deep steps.
                            The consumer's ``ModelProvider`` and ``ModelRegistry`` interpret the
                            string (a tier name or a model id). Unknown values degrade gracefully
                            to the registry's default. Absent/None means exactly today's behavior.
        * ``attachments`` — optional list of in-memory attachment items (chat file uploads AND/OR
                            panel context-docs, the SAME generic shape; see
                            ``core.attachments.prepare_attachments``). Each is a dict
                            ``{filename, mime_type, data: bytes, kind: "image"|"file"}``. The
                            runner OWNS multimodal (the text provider does not): images go NATIVELY
                            to the answer when the answering model/provider is vision-capable, else
                            they are described to text; non-image files are text-extracted. Their
                            text descriptions/inventory join the PLANNER context (so planning is
                            grounded in them), and native image blocks ride the final ANSWER
                            message. Absent/None means exactly today's behavior.
        * ``rep_preamble`` — optional PER-RUN context preamble for the deep step only (e.g. an AI
                            rep's persona + learned corrections, so a deep run executes AS that
                            rep). The brain stays ignorant of its content — it forwards the string
                            to a deep runner that accepts ``context_preamble`` and leaves older
                            runners untouched. Absent/None means exactly today's behavior.
        * ``working_dir_override`` — optional PER-RUN working-directory override for the deep step
                            only (e.g. a quest's synced folder, resolved by the caller from its own
                            quest-folder map). Forwarded to a deep runner that accepts a
                            ``working_dir`` kwarg (see ``core.goal_runner.SubprocessGoalRunner``);
                            older runners are untouched. Absent/None means the deep runner's own
                            configured global working directory applies, exactly as before.

        * ``conv_id``     — optional id of the CURRENT conversation. When set AND a
                            ``conversation_store`` is wired, Step 1 (User Input Understanding) may
                            pull a relevant slice of that conversation to resolve a short/anaphoric
                            message into a self-contained goal condition before context selection.
                            Absent/None (or no store) means Step 1 is a no-op (zero latency).
        * ``conv_scope``  — optional scope dict ({user_id, team_ids, since, participant_id, ...})
                            for finding RELATED past conversations when the current slice is not
                            enough to resolve the message. Interpreted by the ConversationStore.
        * ``prior_escalations`` — optional caller-supplied history of EARLIER turns in this SAME
                            conversation that already escalated (to deep execution or to a human),
                            each e.g. ``{"kind": "deep"|"human", "exit_reason": "...", "outcome":
                            "..."}``. The brain has no persistent storage of its own (core stays
                            domain-free; see ``adapters.ConversationStore`` for the same pattern), so
                            a caller that wants the overseer to see this history across turns passes
                            it forward here (e.g. sourced from the ``exit_reason``/``overseer_signals``
                            of previous ``OrchestratorResult``s it already persisted). Feeds the
                            overseer digest's PRIOR ESCALATIONS THIS CONVERSATION section. Absent/None
                            means "none yet" (today's behavior, byte-for-byte, when overseer is off).
        * ``cancel_check`` -- optional zero-arg callable a caller polls for COOPERATIVE mid-run
                            cancellation (e.g. a runner backed by a task store where a human can
                            cancel a background task while it is in progress). Checked at natural
                            loop boundaries: before starting each new plan/gather/replan step, before
                            each deep-goal retry attempt, and once more after deep execution
                            finishes. When it returns True the run stops cleanly and returns an
                            ``OrchestratorResult`` with ``kind="cancelled"`` (no ``text``/
                            ``deep_results``/``decision_id`` to act on) instead of the normal
                            answer/deep/confirm outcome. It is never called more than the loop's own
                            natural cadence, so a cheap/throttled implementation is fine. Absent/None
                            means exactly today's behavior (a run can never be cancelled mid-flight).
        * ``card_thread`` -- optional PER-IDEA THREADING context (a ``CardThreadContext`` or the
                            equivalent dict; see ``core.card_thread``). Only consulted when the
                            consumer opted in with ``cfg.card_thread_enabled``. It names the card
                            (idea) the conversation is CURRENTLY on plus any candidates the consumer
                            always wants offered; the orchestrator adds the cards its own retrieval
                            already scored this turn (the free PRIOR) and asks the planner, on the
                            call it already makes, which card THIS message belongs to. The resolved
                            assignment comes back as ``OrchestratorResult.card_thread`` and
                            EVENT_CARD_THREAD. The orchestrator persists nothing: the consumer owns
                            the card store and stamps its own messages. Absent/None (or the flag off)
                            means exactly today's behavior.
        * ``prior_narration`` -- optional list of narration lines (most recent last) already spoken
                            aloud to this user in EARLIER turns of this same conversation, when
                            narration is on (``cfg.narrate``). The brain has no persistent storage of
                            its own (same reasoning as ``prior_escalations`` above), so a caller that
                            narrates every turn out loud (e.g. a voice consumer) and wants the ack to
                            stop reopening with its own recent generic "let me look into that" shape
                            passes the last few lines it actually delivered to audio back in here; a
                            reasonable source is ``OrchestratorResult.narration_said`` from the
                            PREVIOUS turn(s), capped by the caller to a handful of lines. Feeds the
                            narrator's repeat-detector and its ack/relay prompts (see ``Narrator``).
                            Absent/None means "no cross-turn memory" (today's behavior).
        * ``now``          -- optional ISO date/datetime string, the CALLER's notion of "now". Fed
                            into goal-condition/constraint derivation (see ``_derive_goal_condition``)
                            so a relative date in the message ("Wednesday", "last week") resolves
                            against the caller's real clock instead of the server's. Absent/None
                            falls back to the system clock (UTC) -- relative-date resolution still
                            works, just against this process's clock.

        ``run`` still works with NO event args (back-compat: same signature callers used before
        plus keyword-only extras), returning the terminal ``OrchestratorResult``.
        """
        user_message = (user_message or "").strip()
        gathered: List[Dict[str, Any]] = []
        # Per-run record of every minimal-intervention OVERSEER consultation (both hook points), and
        # the count both hooks share against cfg.overseer_max_signals. Stamped onto the result in
        # finish(). Stays empty (and overseer_signals None) whenever the overseer is off.
        overseer_signals: List[Dict[str, Any]] = []
        # NON-BLOCKING overseer plumbing (Fix 1). The overseer's provider call runs in a BACKGROUND
        # thread (same idiom as context assembly) so consulting it NEVER stalls the user-facing loop.
        #  - ``_oversee_executor``: a single per-run worker, created lazily on first consult and torn
        #    down (wait=False) in finish(); a still-running consult finishes on its own (bounded
        #    network call) and is never joined synchronously.
        #  - ``pending_oversee``: the in-flight hook-A consult submitted on a prior step, polled at
        #    the top of the next step; kept until it resolves (re-checked each step, never dropped).
        #  - ``overseer_submitted``: submissions counted against cfg.overseer_max_signals (a
        #    submitted-but-uncollected consult still counts, so the shared cap holds across both hooks).
        _oversee_executor: Optional[ThreadPoolExecutor] = None
        pending_oversee: Optional[Dict[str, Any]] = None
        overseer_submitted = 0
        # Previous step's plan (Fix 12's repeat-plan gate signal), captured just before each step's
        # planner call overwrites ``plan``. None until the first plan exists.
        _prev_plan_for_gate: Optional[PlanDecision] = None
        # Fix 7: the caller-supplied cross-turn escalation history, pre-formatted once for the
        # digest (see ``_prior_escalation_lines``). Computed once; reused by both hooks this run.
        _prior_escalation_digest_lines = _prior_escalation_lines(prior_escalations)
        # Run-local durable EXECUTION FACTS (the broken-promise guard's evidence): each deep
        # subtask that actually executes records its outcome (success/failure) here, threaded like
        # ``gathered``. Attached to the OrchestratorResult in finish().
        exec_record = ExecutionRecord()
        started = time.monotonic()
        cfg = self.cfg

        # --- EXECUTION MODE (brainstorm latch, consumer-owned) --------------------------------
        # ``brainstorm_active`` gates every path that could ACT this turn (deep, confirm, and the
        # nets that can only ADD execution). It starts from the consumer-supplied config. Two
        # things can move it within the turn: the dedicated release judgment below (the ONLY way a
        # held latch opens; fail-safe HOLD), and a planner "enter_brainstorm" signal, which engages
        # the gating from an unlatched turn. Any other execution_mode value behaves as "normal".
        # ``mode_signal_detected`` is the first (and only) signal captured this turn, surfaced via
        # EVENT_MODE_SIGNAL + OrchestratorResult.mode_signal; the orchestrator persists nothing.
        brainstorm_active = (cfg.execution_mode == "brainstorm")
        mode_signal_detected: Optional[str] = None
        # --- PER-IDEA THREADING (the idea IS the card; opt-in via cfg.card_thread_enabled) -------
        # ``card_thread_ctx`` is the consumer's per-turn thread context (active card + the cards it
        # always wants offered). ``card_thread_decision`` is what the planner resolved this turn,
        # stamped onto the result in finish(). Both stay None when the flag is off, so the run is
        # byte-identical to a build without the feature.
        card_thread_ctx: Optional[CardThreadContext] = (
            CardThreadContext.coerce(card_thread) if cfg.card_thread_enabled else None)
        card_thread_decision: Optional[CardThreadDecision] = None
        card_thread_block: str = ""
        card_thread_candidate_ids: List[str] = []
        # The action ("deep"/"confirm"/"clarify") the turn chose that the brainstorm latch degraded
        # to "answer", if any. Feeds the no-action acknowledgment steer on the answer path so the
        # reply can say the work was held rather than silently dropping it.
        brainstorm_suppressed_action: Optional[str] = None
        # The question a suppressed "clarify" (planner) or a suppressed understanding-clarify
        # (stage 1) wanted to park as a decision-request. While the latch is held nothing may
        # escalate, so the question rides into the REPLY instead (see BRAINSTORM_CLARIFY_ACK_PREFIX).
        brainstorm_clarify_question: Optional[str] = None
        # True when the release judge LIFTED the hold on this very turn. The user just told us to
        # stop holding back and act on what was discussed, so the turn is a directive by definition:
        # it must not end as one more proposal (that is what the whole conversation already was).
        brainstorm_released_this_turn = False

        # Reset per-turn token counters on the provider (if it tracks them).
        try:
            if hasattr(self.provider, "tokens_in"):
                self.provider.tokens_in = 0
                self.provider.tokens_out = 0
        except Exception:  # noqa: BLE001
            pass

        # If a handoff is configured, route events through a FanoutSink that flips live->bg on detach.
        # We need to set up the emitter early so instant_ack can use it.
        on_detach = None
        active_sink = sink
        if background_sink is not None and detach_check is not None:
            from .adapters import FanoutSink
            fan = FanoutSink(live=sink, background=background_sink) if sink is not None else \
                FanoutSink(live=background_sink, background=background_sink)
            active_sink = fan
            on_detach = fan.detach
        emit = _Emitter(active_sink, mode, self._status, detach_check=detach_check, on_detach=on_detach)

        # --- INSTANT ACK (Feature 1): best-effort, no latency impact -------------------------
        # When cfg.instant_ack is True:
        #   1. Synchronously emit "Looking into this..." so the consumer gets an immediate tick.
        #   2. Launch a CHEAP one-sentence ack call IN A BACKGROUND THREAD — it runs concurrently
        #      with context assembly + the first planner step, so it adds ZERO wall-clock latency.
        # The ack prompt explicitly forbids em dashes (brand rule). A failure is swallowed.
        # The narrator unifies the old instant-ack into a single conversational train of thought:
        # its first beat (started here, concurrent with context assembly) acknowledges the new
        # message, and later beats are the planner's own conversational rationale, relayed at the
        # slow stages (read/deep) via narrator.relay() with NO extra LLM call. ``narrate`` supersedes
        # the legacy ``instant_ack`` flag. Speaks in the selected rep's persona; HOW the ack narrates
        # is overridable by the consumer via cfg.narration_system_prompt.
        narrator = Narrator(
            provider=self.provider,
            model=self.registry.resolve_tier(cfg.planner_tier),
            emit=emit,
            persona=rep_preamble or "",
            transcript_tail=transcript,
            system_prompt=cfg.narration_system_prompt,
            enabled=bool(cfg.narrate or cfg.instant_ack),
            prior_narration=prior_narration,
        )
        if narrator.enabled:
            try:
                emit.status("Looking into this...")
            except Exception:  # noqa: BLE001
                pass
            narrator.begin(user_message)

        # --- ContextAssembler: pre-flight context injection (optional fifth adapter) -----------
        # When a ContextAssembler is wired, call assemble() once before the loop so task-specific
        # context is GUARANTEED applied, not left to the reactive gather. It COMPOSES with any
        # context_view the caller already supplied (e.g. a Quest AI chat's bound-quest context):
        # the assembled cards go FIRST, then the caller's context, so cards apply even when the
        # caller provided its own grounding. Panel context-docs / chat uploads (``attachments``)
        # append below, so a single run grounds on cards + caller context + Quest-panel docs.
        # Best-effort: any exception leaves the run unchanged.
        # ``_ctx_meta`` carries the caller's identity/scope (quest_id, plus anything in
        # ``context_meta`` such as user_id/team_id) so a multi-tenant assembler (e.g. a Quest-backed
        # one in quest-backend serving all users) can scope its lookup to the right user/team/quest.
        # The default FileContextStore ignores it. Threaded to both assemble() and record().
        # Context assembly (a corpus search) can be slow, so it runs in a BACKGROUND THREAD
        # concurrently with the ack call and guidance selection — the same pattern as the
        # instant-ack above.  We start it here, do the fast synchronous work below, then
        # collect the result with a short timeout so corpus search never blocks the turn.
        # ``_ctx_meta`` carries the caller's identity/scope (quest_id, plus anything in
        # ``context_meta`` such as user_id/team_id) so a multi-tenant assembler (e.g. a Quest-backed
        # one in quest-backend serving all users) can scope its lookup to the right user/team/quest.
        # The default FileContextStore ignores it. Threaded to both assemble() and record().
        _ctx_meta: Dict[str, Any] = {**(context_meta or {})}
        if quest_id is not None:
            _ctx_meta.setdefault("quest_id", quest_id)
        # Carry the conversation identity into the context meta so PER-GOAL deep context selection
        # (``_run_deep`` -> ``_assemble_for_goal`` / ``_widen_for_goal``) can pull a conversation
        # slice / related conversations for each goal. Inert when no conv_id/scope is in scope.
        if conv_id is not None:
            _ctx_meta.setdefault("conv_id", conv_id)
        if conv_scope is not None:
            _ctx_meta.setdefault("conv_scope", conv_scope)
        # PER-IDEA THREADING: the card the conversation is currently on, so a CARD-AWARE assembler
        # can prefer this idea's material (priority blending, never hard isolation: everything else
        # stays reachable). An assembler that does not know the key ignores it, so this is inert for
        # every existing assembler.
        if card_thread_ctx is not None and card_thread_ctx.active_card_id:
            _ctx_meta.setdefault("thread_card_id", card_thread_ctx.active_card_id)

        # --- Mid-run user messages: auto-drain a wired inbox for THIS conversation ----------------
        # If the caller didn't pass an explicit ``pending_inputs`` but an ``input_inbox`` is wired,
        # build one that drains this conversation's pending messages. This makes mid-run message
        # folding work for ANY interface that pushes to the inbox (chat, Quest frontend, ...) with no
        # per-interface wiring beyond the push. The conversation key is resolved generically.
        if pending_inputs is None and self.input_inbox is not None:
            _conv_key = (quest_id or _ctx_meta.get("conversation_id")
                         or _ctx_meta.get("session_id") or _ctx_meta.get("user_id"))
            if _conv_key:
                _inbox = self.input_inbox
                pending_inputs = lambda: _inbox.drain(_conv_key)  # noqa: E731

        # --- BRAINSTORM RELEASE (latched turns only; the exit authority) -----------------------
        # While the latch is held, ONE dedicated structured judgment decides whether this message
        # lifts the hold (see judge_brainstorm_release). It runs BEFORE ANY stage that could act or
        # escalate (input understanding, the planner loop, the terminal paths), so the whole turn --
        # understanding, planner note, action gating, acknowledgment -- runs in the mode the user is
        # actually in. A HOLD verdict (including every failure mode) leaves the latch on; only a
        # release flips it, and that release is what the consumer sees as mode_signal.
        # Cost is bounded to brainstorm turns: nothing here runs in normal mode.
        if brainstorm_active and cfg.mode_signals_enabled:
            _released, _release_reason = self.judge_brainstorm_release(user_message, transcript)
            log.info("Brainstorm-release judge: %s (%s)",
                     "RELEASE" if _released else "HOLD", _release_reason)
            if _released:
                brainstorm_active = False
                brainstorm_released_this_turn = True
                mode_signal_detected = "exit_brainstorm"
                try:
                    emit.emit(ProgressEvent(type=EVENT_MODE_SIGNAL, step=0,
                                            data={"signal": "exit_brainstorm",
                                                  "execution_mode": cfg.execution_mode,
                                                  "reason": _release_reason}))
                except Exception:  # noqa: BLE001 — reporting the signal must never break the turn
                    pass

        # --- STAGE 1: USER INPUT UNDERSTANDING (resolve the request) --------------------------
        # Three distinct stages follow, each relating to the goal condition but kept separate:
        #   1. UNDERSTAND the input -> a self-contained GOAL CONDITION (uses CONVERSATION context).
        #   2. FIND CONTEXT to understand how to ACHIEVE the goal (STAGE 2 below: context selection
        #      driven by the goal condition, not the raw message).
        #   3. PLAN how to achieve the goal (the planner loop, using stage-2 achievement context and
        #      free to search for more / as a planned gather step).
        # A short or anaphoric input ("ok do it", "the first one") can't be understood alone. Only
        # when a ConversationStore is wired, a conv_id is present, AND a cheap keyword check says the
        # input leans on context, do we pull a relevant slice of the conversation and ask the model
        # ONCE to rewrite the message as a goal condition. A self-contained input skips this entirely
        # (no LLM hop, ZERO added latency). Context ACCUMULATES: the conversation slice gathered here
        # flows forward into stages 2 and 3 (each starts from what is already gathered and searches
        # for more as needed). A subgoal the planner spawns is the exception: it is handed only its
        # own focused context (see _run_deep). The planner still sees the user's LITERAL words too, so
        # word-for-word fidelity is preserved.
        goal_condition = user_message
        conv_ctx_text = ""
        # Query-aware retrieval routing (spec v3, work package C): structured constraints parsed
        # alongside the goal condition below (else-branch only -- see ``_derive_goal_condition``).
        # None whenever nothing in the message calls for filtering (today's behavior).
        retrieval_constraints: Optional[Dict[str, Any]] = None
        if (self.conversation_store is not None and conv_id
                and self._needs_context_to_understand(user_message)):
            try:
                goal_condition, conv_ctx_text, clarify_q = self._understand_input(
                    user_message, conv_id, conv_scope or {}, emit)
            except Exception:  # noqa: BLE001 — understanding must never break the run
                goal_condition, conv_ctx_text, clarify_q = user_message, "", None
            if clarify_q and brainstorm_active:
                # BRAINSTORM: a latched turn escalates NOTHING, so this may not park a decision-
                # request. Carry the question into the reply instead (the answer asks it inline) and
                # let the loop run normally on the best available reading of the message.
                log.info("Brainstorm mode: understanding-clarify degraded to an in-reply question.")
                brainstorm_suppressed_action = brainstorm_suppressed_action or "clarify"
                brainstorm_clarify_question = clarify_q
            elif clarify_q:
                # Short-circuit the turn: ask the user, do NOT run the planner loop. Reuse the
                # clarify/confirm mechanism so the executor maps it to needs_you and chat posts it.
                plan = PlanDecision(action="confirm", confirm_question=clarify_q,
                                    rationale="clarification needed to understand the request")
                res = self._run_confirm(plan, quest_id=quest_id)
                return self._finish_understanding_clarify(
                    res, emit=emit, exec_record=exec_record, user_message=user_message,
                    ctx_meta=_ctx_meta)
            # STAGE 1 produced a goal condition. Context ACCUMULATES across the stages: the
            # conversation context selected here flows FORWARD, so stage 2 (achievement-context
            # selection, below) and stage 3 (the planner) each START from what was already gathered
            # and only search for MORE as needed. So we inject the gathered conversation context AND
            # the resolved request. (Narrowing to a minimal, just-what-it-needs slice happens later
            # per SUBGOAL in _run_deep, to keep each subgoal focused, not on this main flow.)
            if restates_meaningfully(goal_condition, user_message) or conv_ctx_text:
                emit.emit(ProgressEvent(
                    type=EVENT_UNDERSTANDING,
                    text=f"Understood as: {goal_condition}",
                    data={"goal_condition": goal_condition, "internal": True}))
                # The resolver used this context to derive goal_condition, so it IS relevant.
                # Strip the defensive hedge label added during retrieval so the answerer does
                # not treat confirmed-useful context as uncertain background noise.
                clean_conv_ctx = conv_ctx_text.replace(
                    "=== OTHER PAST CONVERSATIONS (may be unrelated) ===",
                    "=== RETRIEVED PAST CONVERSATIONS ==="
                ) if conv_ctx_text else conv_ctx_text
                _understood_block = (
                    "--- CONVERSATION CONTEXT ---\n" + clean_conv_ctx + "\n"
                    if clean_conv_ctx else ""
                ) + ("--- UNDERSTOOD REQUEST (INTERNAL: this is how the run resolved their message; "
                     "answer it, never read it back to them) ---\n" + goal_condition + "\n")
                context_view = (_understood_block + "\n" + context_view
                                if context_view else _understood_block)
        else:
            # Fix 13: goal-condition ESTABLISHMENT is a separate concern from context-FETCHING above,
            # and now always happens -- even for a self-contained input that needed no conversation
            # context. One cheap-tier LLM call derives a concrete, checkable done-standard from the
            # message alone (see ``_derive_goal_condition``; fails safe to the raw ``user_message``,
            # never raises). This adds one LLM round trip to every turn that reaches here (a real
            # cost/latency change from before, when this branch did nothing); see CHANGELOG.md.
            goal_condition, retrieval_constraints = self._derive_goal_condition(user_message, now=now)
            if restates_meaningfully(goal_condition, user_message) or retrieval_constraints:
                _event_data: Dict[str, Any] = {"goal_condition": goal_condition, "internal": True}
                if retrieval_constraints:
                    _event_data["constraints"] = retrieval_constraints
                emit.emit(ProgressEvent(
                    type=EVENT_UNDERSTANDING,
                    text=f"Understood as: {goal_condition}",
                    data=_event_data))
            if retrieval_constraints and isinstance(retrieval_constraints.get("time_range"), dict):
                # C2 card stores (item level): thread the turn's time_range into the assembly meta
                # so a time-filter-capable assembler (e.g. ``FileContextStore``) hard-filters card
                # CONTENT ITEMS by their ``ts``, and the recent-context filter/render calls below
                # apply the same rule. ``_ctx_meta`` also flows into every per-goal assembly this
                # turn spawns (``_run_deep`` -> ``_assemble_for_goal_with_cards``), so deep goals
                # inherit the filter. Assemblers that ignore unknown meta keys are unaffected.
                _ctx_meta["time_range"] = retrieval_constraints["time_range"]
            if retrieval_constraints and self.conversation_store is not None:
                # C3 routing: constraints name a time period / topic / actor / content kind, so do
                # a BOUNDED, hard-filtered cross-conversation search now instead of leaving this to
                # undirected relevance-only recall. The store applies filters FIRST (hard filter),
                # relevance ranks WITHIN the filtered set, and degrades to relevance-only with an
                # explicit labeled note when the filtered set is empty (see ``related_slices``).
                # Best-effort: any failure here must never break the turn.
                try:
                    _filtered_ctx = self.conversation_store.related_slices(
                        goal_condition, conv_scope or {}, exclude_conv_id=conv_id,
                        filters=retrieval_constraints)
                except Exception:  # noqa: BLE001
                    _filtered_ctx = None
                if _filtered_ctx is not None and _filtered_ctx.text:
                    _filtered_block = "=== FILTERED CONVERSATION SEARCH ===\n" + _filtered_ctx.text
                    context_view = (_filtered_block + "\n\n" + context_view
                                    if context_view else _filtered_block)

        # Fix 5a: a handful of PRIOR user turns in this SAME conversation, for the overseer digest's
        # RECENT CONVERSATION section. Computed once (from whatever ``conv_ctx_text`` STAGE 1 pulled;
        # empty when no conversation_store/conv_id was in play), excluding the current turn's own
        # request (raw AND resolved) so it is never duplicated against CURRENT USER REQUEST /
        # RESOLVED AS. Reused by both overseer hooks this run. Never raises.
        _recent_conversation_digest_lines = _recent_conversation_turns(
            conv_ctx_text, exclude=[user_message, goal_condition])

        # --- Recent-turn context: warm NO-LLM fallback (see core/recent_context.py) ------------
        # Independent of the ConversationStore-driven Stage 1 above (that rewrites the message; this
        # carries forward the CARDS recently selected). A fast local file read across THREE scopes
        # -- conv:<conv_id>, quest:<quest_id>, and always "global" (unless the consumer disabled
        # cross-conversation memory) -- gated through a pure lexical filter (no LLM) so an unrelated
        # new question never drags in stale cards. Cards that survive are merged into context_view
        # further below, crucially EVEN WHEN the fresh background assembly started next times out or
        # finds nothing -- that resilience is the point. ``_recent_scope_keys`` is empty only when
        # recent-context is fully off (no conv/quest id AND global disabled is impossible since
        # global defaults on; it is only empty when the consumer wired no recent_context at all,
        # handled by the outer None check below).
        _recent_scope_keys_this_turn = self._recent_scope_keys(_ctx_meta)
        _recent_filtered: List[Dict[str, Any]] = []
        _recent_records: List[Dict[str, Any]] = []
        if (self.recent_context is not None and cfg.recent_context_enabled
                and _recent_scope_keys_this_turn):
            try:
                _recent_records = self.recent_context.load(_recent_scope_keys_this_turn)
            except Exception:  # noqa: BLE001 -- a store failure must never break the run
                _recent_records = []
            try:
                _is_followup = self._needs_context_to_understand(user_message)
            except Exception:  # noqa: BLE001
                _is_followup = False
            try:
                _recent_filtered = filter_relevant(
                    _recent_records, f"{goal_condition} {user_message}",
                    is_followup=_is_followup, max_cards=cfg.recent_context_max_cards,
                    time_range=_ctx_meta.get("time_range"))
            except Exception:  # noqa: BLE001
                _recent_filtered = []
            # ITEM-LEVEL RANKING HINT for fresh assembly (see core/recent_context.py): built from
            # ALL loaded records (not just the ones that survived the CARD-level gate above), since
            # this only influences item ORDER within a card fresh assembly re-selects on its own --
            # even a card that did not itself pass filter_relevant this turn may resurface via fresh
            # assembly, and its item-usage history should still apply. Threaded into the
            # consolidating LLM pass via meta; a pure hint, never a hard override.
            try:
                _recent_item_usage_hint = build_item_usage_hint(
                    _recent_records, f"{goal_condition} {user_message}")
                if _recent_item_usage_hint:
                    _ctx_meta["recent_item_usage"] = _recent_item_usage_hint
            except Exception:  # noqa: BLE001
                pass

        # ONE shared, query-keyed in-run context cache (the unified primitive, docs sec. 3): the
        # turn-start pre-fetch below and any mid-loop {"cards": ...} / {"card": ...} read reach the
        # SAME assembler through it, so a turn-start assembly timeout is recoverable mid-loop (a
        # late-landing pre-fetch future is kept referenced and served from cache) instead of dropping
        # all fresh context for the whole turn. It carries ``emit`` so a mid-loop card read can emit
        # EVENT_CONTEXT. Closed in ``finish()``.
        card_context = TurnCardCache(self.context_assembler, _ctx_meta)
        card_context.emit = emit
        _ctx_prefetch_query: Optional[str] = None

        _assembled = None
        _ctx_future = None
        _ctx_executor = None
        if self.context_assembler is not None:
            try:
                _ctx_assembler = self.context_assembler
                # STEP 2 (context selection) targets the RESOLVED request: when Step 1 rewrote the
                # message into a goal_condition, select context for THAT, not the raw anaphoric text.
                # The planner still receives the literal ``user_message`` (literal-words fidelity);
                # the goal_condition rides in context_view. When Step 1 did not run, goal_condition
                # == user_message, so this is byte-for-byte the prior behavior.
                _ctx_msg = goal_condition
                _ctx_prefetch_query = _ctx_msg

                def _do_assemble() -> Any:
                    # Soft internal deadline slightly under the hard collect timeout below,
                    # threaded via ``meta["assembly_deadline"]`` (a ``time.monotonic()``
                    # timestamp). A deadline-aware assembler (e.g. HybridContextAssembler)
                    # uses it to return whatever its retrieval arms completed in time as a
                    # PARTIAL result (``AssembledContext.partial``) instead of overrunning the
                    # budget and losing everything. Assemblers that ignore the hint behave
                    # exactly as before. Only THIS turn-start prefetch is deadline-bounded:
                    # ``_ctx_meta`` itself is left untouched, so mid-loop reads through
                    # ``TurnCardCache`` stay unbounded and a late full assembly is still
                    # recoverable there.
                    hard_budget = context_assembly_timeout_seconds()
                    soft_budget = max(hard_budget - 0.5, hard_budget * 0.5)
                    assemble_meta = {**(_ctx_meta or {}),
                                     "assembly_deadline": time.monotonic() + soft_budget}
                    return _ctx_assembler.assemble(_ctx_msg, meta=assemble_meta)

                _ctx_executor = ThreadPoolExecutor(max_workers=1)
                _ctx_future = _ctx_executor.submit(_do_assemble)
                # Register the eager pre-fetch so a later mid-loop {"cards": <same query>} read can
                # serve it from the shared cache -- crucially EVEN IF the turn-start collect below
                # times out: the future is not cancelled, so a late result still lands here.
                card_context.register_prefetch(_ctx_msg, _ctx_future)
                try:
                    # This is the ContextAssembler.assemble() call: a hybrid keyword + vector
                    # search over the wired context (cards + turn history), not a single named
                    # source. "searching context" names the stage honestly (STAGE 2: FIND CONTEXT).
                    emit.status("Searching context…")
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 -- assembly setup failure must never break the run
                _ctx_future = None
                _ctx_executor = None

        # --- GuidanceProvider: use-case-specific instruction PRE-SELECTION (optional) ----------
        # When a GuidanceProvider is wired, ask it for the cards most relevant to THIS message and
        # render them into an "APPLICABLE GUIDANCE" block, PREPENDED to context_view (so it leads,
        # ahead of any assembled/caller context — the same compose order the ContextAssembler uses
        # for its own output). The planner is told this block may already cover the request, and to
        # list_guidance/read_guidance for anything it doesn't. ``guidance_selected_ids`` records
        # what was pre-selected so a later read_guidance of the same id returns a de-dupe note.
        # Best-effort: any failure leaves the run exactly as if no guidance were wired.
        guidance_selected_ids: set = set()
        # The guidance cards selected for THIS input are the QUALITY STANDARDS the result must meet.
        # We keep them in ``quality_standards`` so the goal loop can verify the done-standard against
        # the same standards the planner was given (see _run_deep / _verify_goal).
        quality_standards: Optional[str] = None
        if self.guidance is not None:
            try:
                _cards = self.guidance.select(
                    user_message,
                    team_id=_ctx_meta.get("team_id") if _ctx_meta else None,
                    org_id=_ctx_meta.get("org_id") if _ctx_meta else None,
                    limit=cfg.guidance_topk) or []
            except Exception as e:  # noqa: BLE001 -- a provider must never break the run
                log.warning(f"Guidance selection failed: {type(e).__name__}: {e}", exc_info=True)
                _cards = []
            if _cards:
                _blocks = ["--- APPLICABLE GUIDANCE ---"]
                for _c in _cards:
                    guidance_selected_ids.add(_c.id)
                    _blocks.append(f"[{_c.id}] {_c.title}\n(applies when: {_c.relevance})\n{_c.body}")
                _guidance_view = "\n\n".join(_blocks)
                quality_standards = _guidance_view  # rolls into goal verification as the quality bar
                context_view = (_guidance_view + "\n\n" + context_view if context_view
                                else _guidance_view)
                # Transparency: a STATUS tick naming the guidance applied this turn.
                try:
                    emit.status("Applied guidance: "
                                + ", ".join(c.title for c in _cards) + ".")
                except Exception:  # noqa: BLE001
                    pass

        # --- Narrator: ordering barrier for the first beat ------------------------------------
        # The first conversational beat EMITS ITSELF from its background thread the instant the model
        # returns (see Narrator.begin / _gen_and_say), so the instant response already went out as
        # early as possible, concurrently with context assembly + guidance and never sequenced behind
        # them. This is only a JOIN barrier: it waits for that emit to finish before the planner loop
        # emits any relay beat, so the ack always precedes the planner's rationale, then cleans up the
        # background thread before run() returns. A timeout just proceeds.
        narrator.flush_first()

        # --- ContextAssembler: collect the background assemble() result -----------------------
        # The assemble() call has been running concurrently with the ack + guidance.  Collect it
        # NOW with a short timeout so corpus search never blocks the interactive turn.  On timeout
        # or error we proceed with no assembled context (the reactive gather still runs in-loop).
        # The assembled cards go FIRST, then the caller's context, so cards apply even when the
        # caller provided its own grounding (panel docs / chat uploads append below).
        context_assembly_timed_out = False
        context_assembly_partial = False
        if _ctx_future is not None:
            ctx_timeout = context_assembly_timeout_seconds()
            try:
                _assembled = _ctx_future.result(timeout=ctx_timeout)
            except TimeoutError:
                # LOUD by design: this drops ALL fresh context for the turn — not even the
                # assembler's soft-deadline PARTIAL path (see _do_assemble) returned in time —
                # the exact mechanism behind "forgets what I just said". It must never be a
                # debug-only breadcrumb.
                context_assembly_timed_out = True
                timeout_count = record_context_assembly_timeout()
                log.warning(
                    "Context assembly timed out after %.1fs with no partial result; "
                    "turn-start context was dropped and this turn proceeds without it "
                    "(QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust; total timeouts this "
                    "process: %d)",
                    ctx_timeout, timeout_count)
                _assembled = None
            except Exception as e:  # noqa: BLE001 — cancelled or assembler error: skip
                log.warning(f"Context assembly failed: {type(e).__name__}: {e}", exc_info=True)
                _assembled = None
            finally:
                try:
                    _ctx_executor.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
            if _assembled is not None:
                # PARTIAL result: the assembler hit its soft deadline and returned only the
                # retrieval arm(s) that completed. Using it beats the drop-everything path,
                # but stay LOUD about the degradation (same visibility bar as the timeout).
                # ``assembly_partial`` / the WARNING only fire when the partial actually
                # CARRIES content -- an empty partial means nothing was used, and saying
                # "partial context was used" would be untrue.
                _assembly_partial_flag = bool(getattr(_assembled, "partial", False))
                context_assembly_partial = _assembly_partial_flag and bool(
                    getattr(_assembled, "context_view", "") or "")
                if context_assembly_partial:
                    log.warning(
                        "Context assembly hit its soft deadline; PARTIAL context was used "
                        "for this turn (only the retrieval arm(s) that finished within "
                        "budget; QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust)")
                elif _assembly_partial_flag:
                    log.warning(
                        "Context assembly hit its soft deadline and returned an EMPTY "
                        "partial result; no fresh turn-start context was used this turn "
                        "(QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust)")
                # FULL turn-start assembly completed in time: seed the shared cache so a
                # same-query mid-loop {"cards": ...} read is a pure cache hit (no second assembly
                # run). A PARTIAL result is used for THIS turn-start prompt but never cached as
                # the query's completed result -- registering it would displace the full fuse a
                # later mid-loop read can still recover; instead the (already-done) prefetch
                # future is discarded so that read falls through to a fresh, deadline-free
                # assemble. On the timeout branch above we deliberately leave the still-running
                # future registered, so if it lands late a mid-loop read for the same query
                # serves it from the cache.
                if _ctx_prefetch_query:
                    if _assembly_partial_flag:
                        card_context.discard_prefetch(_ctx_prefetch_query)
                    else:
                        card_context.register_result(_ctx_prefetch_query, _assembled)
                try:
                    if _assembled.context_view:
                        context_view = (_assembled.context_view + "\n\n" + context_view if context_view
                                        else _assembled.context_view)
                    if model_hint is None and _assembled.model_tier_hint:
                        model_hint = _assembled.model_tier_hint
                except Exception:  # noqa: BLE001
                    pass

        # --- Recent-turn context: merge cards that survived the relevance gate ----------------
        # Folded in REGARDLESS of whether fresh assembly produced anything -- this is the
        # resilience win from core/recent_context.py: if ``_assembled`` is None (no assembler
        # wired, or the background assemble() above timed out/failed), a follow-up turn still gets
        # the cards its own recent turns already selected, gated by filter_relevant so an unrelated
        # new question never drags them in. A recent card already re-found by fresh assembly this
        # turn is dropped here (the fresh one wins). Never raises.
        _card_meta: List[Dict[str, Any]] = (
            list(getattr(_assembled, "card_metadata", None) or []) if _assembled is not None else []
        )
        _sources: List[Dict[str, Any]] = (
            list(getattr(_assembled, "sources", None) or []) if _assembled is not None else []
        )
        _recent_entries: List[Dict[str, Any]] = []
        if _recent_filtered:
            try:
                _fresh_ids = {cm.get("id") for cm in _card_meta if isinstance(cm, dict)}
                _recent_survivors = [r for r in _recent_filtered if r.get("id") not in _fresh_ids]
                _recent_text, _recent_entries = render_recent_cards(
                    _recent_survivors, f"{goal_condition} {user_message}",
                    time_range=_ctx_meta.get("time_range"))
                if _recent_text:
                    context_view = (_recent_text + "\n\n" + context_view if context_view
                                    else _recent_text)
                if _recent_survivors:
                    _sources.append({
                        "adapter": "recent",
                        "label": "recent turns",
                        "items": [r.get("title") or r.get("id", "") for r in _recent_survivors],
                    })
            except Exception:  # noqa: BLE001 -- recent-context merge must never break the run
                _recent_entries = []
        _merged_card_meta = _card_meta + _recent_entries

        # --- PER-IDEA THREADING: the cheap PRIOR, then the planner decides --------------------
        # The candidate set is the cards this turn's HYBRID RETRIEVAL already scored (keyword/IDF
        # arm + vector arm, plus the warm recent-turn cards), merged with whatever the consumer
        # always wants offered (its general/small-talk card, the ideas recently live in this
        # conversation). So the prior costs ZERO extra model calls and ZERO extra searches: it is
        # the same assembly the turn ran anyway. It NARROWS and SURFACES; it never decides. The
        # planner picks the topic on the call it already makes, and any parse failure or ambiguity
        # continues the current card (see core.card_thread.parse_card_thread).
        if card_thread_ctx is not None:
            try:
                _thread_candidates = merge_candidates(card_thread_ctx, _merged_card_meta)
                card_thread_candidate_ids = [c.id for c in _thread_candidates]
                _hint = render_thread_hint(card_thread_ctx, _thread_candidates)
                card_thread_block = _CARD_THREAD_PLANNER_BLOCK_TEMPLATE.format(thread_hint=_hint)
            except Exception:  # noqa: BLE001 -- threading must never break a turn
                log.debug("card thread hint build failed; the turn continues its current card",
                          exc_info=True)
                card_thread_block = ""
                card_thread_candidate_ids = []

        # --- CONTEXT EVENT: emit EVENT_CONTEXT showing which cards were selected -----------
        # Dedicated event for context assembly: card selection + sources. Surfaces in all modes.
        # Always emitted when assembly ran OR recent-turn cards survived OR assembly timed out
        # (even with 0 cards + 0 sources otherwise) so consumers can show "assembly ran but
        # found nothing" -- or "assembly timed out and was dropped" -- without needing a
        # separate signal. Never raises.
        if _assembled is not None or _recent_entries or context_assembly_timed_out:
            try:
                log.debug(f"Assembled context: {len(_merged_card_meta)} cards, {len(_sources)} sources")
                # LIGHTWEIGHT projection: the card_metadata items carry each item's full resolved
                # ``text`` (heavy), and a conversation-history card's TITLE is the raw user turn, so
                # both the card titles and the source items are sanitized before they leave the
                # process. Nothing on this event quotes the person's own words back at them.
                _card_meta_light = _project_card_metadata_for_event(_merged_card_meta)
                _sources_light = _project_sources_for_event(_sources)
                # The text field used to concatenate the card titles ("Selected cards: Hi, Hello."),
                # which is the same verbatim-turn leak by another route. Counts, not content.
                _text = (f"Selected {len(_merged_card_meta)} context card"
                         f"{'s' if len(_merged_card_meta) != 1 else ''}."
                         if _merged_card_meta else "")
                log.debug(f"Emitting EVENT_CONTEXT: {_text}")
                emit.emit(ProgressEvent(
                    type=EVENT_CONTEXT,
                    text=_text,
                    data={
                        "card_metadata": _card_meta_light,
                        "sources": _sources_light,
                        "card_count": len(_merged_card_meta),
                        "source_count": len(_sources),
                        # Structured marker a consumer can observe/aggregate: this turn's
                        # turn-start context was dropped because assembly ran out of time (see
                        # the WARNING logged at the timeout call site and
                        # ``record_context_assembly_timeout``).
                        "assembly_timed_out": context_assembly_timed_out,
                        # Sibling marker: assembly hit its soft deadline and this turn ran on
                        # a PARTIAL result (the arm(s) that completed in time) instead of
                        # dropping fresh context entirely.
                        "assembly_partial": context_assembly_partial,
                        # Retrieval metadata is the run explaining itself. A consumer routes this to
                        # a debug surface, never into the chat bubble.
                        "internal": True,
                    }
                ))
            except Exception as e:  # noqa: BLE001
                log.error(f"Failed to emit EVENT_CONTEXT: {e}", exc_info=True)

        # --- Recent-turn context: write back the merged selection for the NEXT turn ------------
        # Best-effort, never raises. Includes BOTH freshly assembled and surviving recent cards so
        # the warm set follows the conversation forward; a card that has dropped out of relevance is
        # pruned next turn by filter_relevant, not by record() itself.
        if (self.recent_context is not None and cfg.recent_context_enabled
                and _recent_scope_keys_this_turn and _merged_card_meta):
            try:
                self.recent_context.record(_recent_scope_keys_this_turn, _merged_card_meta, user_message)
            except Exception:  # noqa: BLE001
                pass

        # --- CONTEXT TRANSPARENCY (Feature 2): emit a human-readable summary of sources ------
        # Best-effort: emit a STATUS event naming the adapters + file items so the consumer/UI
        # can show "Context from: ...". Only fires when the assembled context carries source
        # attribution (assemblers opt in by populating AssembledContext.sources). Never raises.
        if _assembled is not None:
            try:
                _sources = getattr(_assembled, "sources", None) or []
                if _sources:
                    _parts: List[str] = []
                    for _src in _sources:
                        _label = _src.get("label") or _src.get("adapter") or "unknown"
                        _items = _src.get("items") or []
                        if _items:
                            _item_names = ", ".join(str(x).split("/")[-1] for x in _items[:5])
                            _extra = f" (+{len(_items) - 5} more)" if len(_items) > 5 else ""
                            _parts.append(f"{_label} ({_item_names}{_extra})")
                        else:
                            _parts.append(_label)
                    if _parts:
                        emit.status("Context from: " + ", ".join(_parts) + ".")
            except Exception:  # noqa: BLE001
                pass

        # --- Attachments (multimodal): ONE path for chat uploads + panel context-docs ----------
        # Prepare against the model that WILL answer (model_hint or the default answer tier), so
        # native-vs-describe is decided by the real answering model's vision capability. The text
        # context grounds the PLANNER; native image blocks are appended to the final answer call.
        native_blocks: List[Dict[str, Any]] = []
        if attachments:
            from .attachments import prepare_attachments
            answer_model = self.registry.resolve_tier(model_hint or "sonnet")
            prepared = prepare_attachments(
                attachments,
                model=answer_model,
                provider=self.provider,
                vision_provider=self.vision_provider,
                vision_model=self.vision_model,
            )
            native_blocks = prepared.native_blocks
            if prepared.text_context:
                context_view = (context_view + "\n\n" + prepared.text_context if context_view
                                else prepared.text_context)

        def budget_exhausted() -> bool:
            size = sum(len(o.get("text", "")) + len(str(o.get("hits", ""))) for o in gathered)
            return (time.monotonic() - started) > cfg.max_elapsed_seconds or size > cfg.max_gathered_chars

        def finish(res: OrchestratorResult) -> OrchestratorResult:
            res.steps = steps
            res.gathered = gathered
            res.execution_record = exec_record
            res.retrieval_constraints = retrieval_constraints
            res.mode_signal = mode_signal_detected
            # PER-IDEA THREADING: the topic this turn was assigned to. Present on EVERY terminal
            # result (answer, deep, confirm, cancelled) once the flag is on, because the consumer
            # stamps the turn's messages from it and a turn that ended early still happened on an
            # idea. None when threading is off, or when the run ended before the first plan.
            if card_thread_decision is not None:
                res.card_thread = card_thread_decision.as_dict()
            if overseer_signals:
                res.overseer_signals = list(overseer_signals)
            # What the narrator actually said aloud this turn (empty when narration is off or it
            # said nothing) — a consumer that persists a rolling window of this across turns and
            # passes it back in as ``run(prior_narration=...)`` is what lets the ack stop reopening
            # every turn with its own recent generic shape; see ``Narrator``.
            if narrator.enabled:
                res.narration_said = list(narrator._said)
            # Tear down the background overseer worker (Fix 1). ``wait=False`` mirrors the
            # context-assembly teardown: any still-running consult (a bounded provider call) finishes
            # on its own without blocking the return, and is never joined synchronously here.
            if _oversee_executor is not None:
                try:
                    _oversee_executor.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
            # Tear down the shared context cache's fresh-assemble executor (``wait=False`` again: a
            # still-running late pre-fetch finishes on its own, exactly like the assembly teardown).
            try:
                card_context.close()
            except Exception:  # noqa: BLE001
                pass
            # Collect token counts from the provider if it tracks them.
            try:
                if hasattr(self.provider, "tokens_in"):
                    res.tokens_in = self.provider.tokens_in
                    res.tokens_out = self.provider.tokens_out
            except Exception:  # noqa: BLE001
                pass
            if res.kind == "cancelled":
                # Cooperative mid-run cancellation: there is no real outcome to write back or verify
                # against the goal, and no answer/deep/confirm event to surface. Just close out the
                # stream so any consumer waiting on EVENT_DONE is not left hanging.
                emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=steps))
                return res
            # Best-effort ContextAssembler write-back (learn from the outcome for next run). Pass the
            # files the brain ACTUALLY read this run (their rel_paths, from the gathered reads/greps)
            # so the card PINS them: that is what makes the loop compound and what staleness later
            # invalidates. Without this the card would pin nothing and never go stale.
            if self.context_assembler is not None:
                try:
                    used_files: List[str] = []
                    _seen = set()
                    for o in gathered:
                        for rp in [o.get("rel_path")] + [h.get("rel_path") for h in (o.get("hits") or [])]:
                            if rp and rp not in _seen:
                                _seen.add(rp)
                                used_files.append(rp)
                    self.context_assembler.record(
                        user_message,
                        {"kind": res.kind, "steps": res.steps, "files": used_files,
                         "response": res.text,
                         **_ctx_meta},
                    )
                except Exception:  # noqa: BLE001 -- write-back must never break the run
                    pass
            # Final token event so consumers get the definitive total alongside the result.
            _fti = res.tokens_in
            _fto = res.tokens_out
            if _fti or _fto:
                emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                        data={"tokens_in": _fti, "tokens_out": _fto,
                                              "total": _fti + _fto, "final": True}))
            # The terminal result + an explicit done event. RESULT/DONE always surface (both lanes).
            # Emit a goal-verdict status line so the user sees WHY the answer was accepted
            # or not — not just a yes/no, but the verifier's reasoning and what was missing.
            if res.goal_verdict is not None:
                _v = res.goal_verdict
                _reason = (_v.get("reason") or "").strip()
                _next = (_v.get("next_action") or "").strip()
                if _v.get("met"):
                    _verdict_line = f"Goal reached. {_reason}" if _reason else "Goal reached."
                elif res.exit_reason == "escalated_deep":
                    _verdict_line = (f"Searching further. {_reason}" if _reason
                                     else "Searching further for a definitive answer.")
                else:
                    # Not met, max turns or unverified — tell the user what was missing
                    _verdict_line = _reason or "Could not fully verify the goal was met."
                    if _next:
                        _verdict_line += f" To complete: {_next}"
                emit.status(_verdict_line)
            if res.kind == "answer":
                # Sanity check: answer text should NEVER be an orchestrator command.
                # If it is, something went wrong in the planner/answer path.
                if res.text and _is_orchestrator_command(res.text):
                    log.error(f"Orchestrator: answer result is an internal command {res.text}; "
                             "replacing with error message")
                    result_text = "I had trouble formulating a proper response to that. Please try again."
                else:
                    result_text = res.text
                emit.emit(ProgressEvent(type=EVENT_RESULT, text=result_text, result_kind="answer",
                                        data={"exit_reason": res.exit_reason,
                                              "goal_verdict": res.goal_verdict} if res.exit_reason else {}))
            elif res.kind == "confirm":
                emit.emit(ProgressEvent(type=EVENT_DECISION, text=res.question,
                                        decision_id=res.decision_id, result_kind="confirm"))
            elif res.kind == "deep":
                out = "\n\n".join(
                    s for s in (_strip_future_context(d.output) for d in res.deep_results) if s
                ) or None
                # Surface the internal FUTURE-CONTEXT bullets as structured data (NOT in the message
                # body) so a consumer can show them as an expandable "what I'll remember" panel.
                _future = _future_context_for_display(res.deep_results)
                emit.emit(ProgressEvent(
                    type=EVENT_RESULT, text=out, result_kind="deep",
                    data={"future_context": _future,
                          "exit_reason": res.exit_reason,
                          "goal_verdict": res.goal_verdict} if (_future or res.exit_reason) else {}))
            emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=steps))
            return res

        plan: Optional[PlanDecision] = None
        steps = 0
        consecutive_reads = 0  # Track how many steps in a row chose "read"
        # Set by an OVERSEER signal that decided the terminal path this run, so finish() can stamp
        # the exit_reason ("overseer_answer_now" | "overseer_escalated_deep" |
        # "overseer_escalated_human"). "" when the overseer did not decide the path.
        overseer_decided = ""
        for step in range(cfg.max_steps):
            steps = step + 1
            # Cooperative cancellation, checked before starting each new plan/gather/replan step.
            if cancel_check is not None and cancel_check():
                return finish(OrchestratorResult(kind="cancelled", rationale="cancelled mid-run",
                                                  exit_reason="cancelled"))
            emit.status("Planning…" if step == 0 else "Re-planning…")

            # --- OVERSEER poll (hook A, applied ONE STEP LATE): pick up a consult SUBMITTED on a
            # prior step whose background call has since resolved (Fix 1). This runs BEFORE this
            # step's planner so a redirect can steer the plan we are about to make. A resolved
            # redirect is injected as a COURSE CORRECTION observation (the planner re-reads it,
            # exactly as the old inline hook did); answer_now / escalate_deep / escalate_human end
            # the loop here. If the consult has NOT resolved yet, we simply proceed and re-check next
            # step (never block: ``overseer_poll_timeout_seconds`` is 0.0 in production). Wrapped so
            # it can NEVER break the loop. The one-step-late application is the intended cost of not
            # stalling the walk.
            if pending_oversee is not None:
                try:
                    _psig = self._collect_oversee(
                        pending_oversee, signals=overseer_signals, emit=emit,
                        timeout=cfg.overseer_poll_timeout_seconds)
                    if _psig is not None:
                        pending_oversee = None  # resolved -> free the slot for a fresh consult
                        if _psig.signal == "redirect" and _psig.hint:
                            gathered.append({
                                "kind": "query", "locator": "overseer",
                                "text": f"COURSE CORRECTION: {_psig.hint}",
                            })
                        elif _psig.signal == "answer_now":
                            plan = plan or PlanDecision(action="answer")
                            plan.action = "answer"
                            overseer_decided = "overseer_answer_now"
                            break
                        elif _psig.signal in ("escalate_deep", "escalate_human") and brainstorm_active:
                            # Brainstorm mode: escalations may not ADD actions; treat as proceed.
                            pass
                        elif _psig.signal == "escalate_deep":
                            plan = plan or PlanDecision(action="answer")
                            plan.action = "deep"
                            plan.goal = plan.goal or f"Complete the request: {user_message}"
                            plan.deep_brief = plan.deep_brief or user_message
                            overseer_decided = "overseer_escalated_deep"
                            break
                        elif _psig.signal == "escalate_human":
                            # Genuine human-only fork (Fix 2): route through the SAME confirm /
                            # decision-request mechanism a planner-originated confirm uses, rather
                            # than guessing or executing. Never auto-run deep work for this signal.
                            plan = plan or PlanDecision(action="answer")
                            plan.action = "confirm"
                            plan.confirm_question = (
                                _psig.reason
                                or f"This needs your input before I continue: {user_message}")
                            overseer_decided = "overseer_escalated_human"
                            break
                    # else: not resolved -> keep ``pending_oversee`` and re-check next step.
                except Exception:  # noqa: BLE001 — the overseer must never break the loop
                    pass

            # AUTO-INJECT FUNCTION DISCOVERY on step 0: pre-load all available operations
            # so the planner sees them from the start, ordered by relevance. This eliminates
            # the need for the planner to first ASK for operations; they're already in hand.
            if step == 0 and self.retrieval is not None:
                if getattr(self.retrieval, "list_operations", None) is not None:
                    try:
                        ops_obs = self._exec_one_read({"list_operations": True})
                        if ops_obs is not None:
                            _ops = ops_obs.to_dict()
                            _ops["discovery"] = True  # a capability menu, not answer content
                            gathered.append(_ops)
                    except Exception as e:  # noqa: BLE001
                        log.debug(f"Auto-injection of list_operations failed: {type(e).__name__}: {e}")

            # Fix 12: remember THIS step's plan (before the planner call below overwrites it) so the
            # hook-A gate can tell whether the NEXT plan repeats it (a cheap looping signal).
            _prev_plan_for_gate = plan

            try:
                plan = self._plan(user_message, transcript, context_view, gathered, step=step,
                                  narrate=narrator.enabled, persona=rep_preamble or "",
                                  already_said=narrator._said if narrator.enabled else None,
                                  brainstorm=brainstorm_active,
                                  card_thread_block=card_thread_block)
            except Exception as e:  # noqa: BLE001 — planner failure -> grounded fallback answer
                log.exception(
                    f"Planner failed on step {steps}: {e}. Falling back to grounded answer."
                )
                plan = PlanDecision(action="answer", rationale="planner error → grounded answer")

            # --- PER-IDEA THREADING: resolve the turn's TOPIC ----------------------------------
            # The topic is a property of the MESSAGE, so the FIRST plan that actually EXPRESSES one
            # owns it, and no later step may re-litigate it (a mid-loop read must not be able to move
            # the turn to a different idea).
            #
            # "Expresses one" is doing real work here. The field is required, but a model can still
            # return nothing for it, and it does so exactly when the turn is busy: a real run had the
            # planner omit the topic on a "back to the launch plan" turn while it was planning reads,
            # which landed the fail-safe (continue) and filed that turn under the idea the user was
            # explicitly leaving. So a FELL-BACK decision is not treated as an answer: it is held as
            # the current best, and a later plan step in the SAME turn may still supply the real one.
            # If no step ever does, the fail-safe stands, which is the standing rule (any parse
            # failure or ambiguity continues the current card).
            #
            # A "new" decision leaves ``card_id`` None: the CONSUMER owns the card store, so it is the
            # one that creates (or dedupes onto) the card and stamps the turn.
            #
            # A TOPIC SWITCH IS NOT A MODE SIGNAL. This block sets no mode, touches no latch, and
            # runs regardless of ``brainstorm_active``: moving to another idea inside a held
            # conversation leaves it held, and coming back to an old idea does not release it.
            if card_thread_ctx is not None and (card_thread_decision is None
                                                or card_thread_decision.fell_back):
                try:
                    _decision = parse_card_thread(
                        plan.card_thread if plan else None,
                        active_card_id=card_thread_ctx.active_card_id,
                        known_ids=card_thread_candidate_ids or None,
                    )
                    # Keep a real judgment over a fail-safe; keep the FIRST real judgment forever.
                    if card_thread_decision is None or not _decision.fell_back:
                        _changed = (card_thread_decision is None
                                    or _decision.as_dict() != card_thread_decision.as_dict())
                        card_thread_decision = _decision
                        if _changed:
                            emit.emit(ProgressEvent(
                                type=EVENT_CARD_THREAD, step=steps,
                                data={**card_thread_decision.as_dict(),
                                      "previous_card_id": card_thread_ctx.active_card_id,
                                      "internal": True}))
                except Exception:  # noqa: BLE001 -- reporting must never break the turn
                    log.debug("card thread resolution failed; continuing the current card",
                              exc_info=True)
                    if card_thread_decision is None:
                        card_thread_decision = CardThreadDecision(
                            action="continue", card_id=card_thread_ctx.active_card_id,
                            fell_back=True)

            # --- EXECUTION-MODE SIGNAL (opt-in via cfg.mode_signals_enabled): capture the first
            # explicit mode change the planner detected this turn (LLM judgment riding the
            # planning call that already ran; zero extra calls). Surfaced to the consumer as an
            # event here and on the result in finish(); the consumer owns persisting the latch.
            # An "enter_brainstorm" engages the gating for the rest of this same turn. Fail-safe by
            # construction: normalize_decision already reduced anything unrecognized -- and, with
            # the flag off, ANY value -- to None, and None changes nothing; the flag check here
            # is belt and braces so a disabled consumer can never see a latch flip or the event.
            #
            # EXIT WHILE LATCHED IS NOT THE PLANNER'S TO GIVE. The planner runs at the cheap
            # planner tier and judges any imperative -- including one purely about the subject
            # matter ("create a goal called X and add it to my plan") -- as the user asking to
            # proceed, which released the latch mid-turn and executed work in a conversation the
            # user had put on hold. The release was already decided ONCE, before the loop, by
            # judge_brainstorm_release (fail-safe HOLD). So while cfg.execution_mode is
            # "brainstorm", a planner exit_brainstorm is dropped here.
            if (cfg.mode_signals_enabled and plan and plan.mode_signal
                    and mode_signal_detected is None):
                if (cfg.execution_mode == "brainstorm"
                        and plan.mode_signal == "exit_brainstorm"):
                    log.info("Brainstorm mode: ignoring the planner's exit_brainstorm; the release "
                             "judge owns the exit while the latch is held.")
                else:
                    mode_signal_detected = plan.mode_signal
                    brainstorm_active = (plan.mode_signal == "enter_brainstorm")
                    try:
                        emit.emit(ProgressEvent(type=EVENT_MODE_SIGNAL, step=steps,
                                                data={"signal": mode_signal_detected,
                                                      "execution_mode": cfg.execution_mode}))
                    except Exception:  # noqa: BLE001 — reporting must never break the turn
                        pass

            # BRAINSTORM GATE: acting AND escalating are both unavailable while the latch is held.
            # If the planner still chose "deep", "confirm" or "clarify" despite the prompt note,
            # degrade to "answer" with everything gathered so far (same fail-safe style as a planner
            # failure above): the turn keeps its full context and produces a grounded reply, it just
            # does not act. "clarify" is included because it surfaces its question through the
            # ESCALATION SINK (a real decision-request in a consumer), which would park a pending ask
            # on a conversation the user explicitly put on hold. Its question is not lost: it rides
            # into the reply text instead (see brainstorm_clarify_question below).
            if plan and brainstorm_active and plan.action in ("deep", "confirm", "clarify"):
                log.info("Brainstorm mode: degrading planner action %r to 'answer'.", plan.action)
                brainstorm_suppressed_action = plan.action
                if plan.action == "clarify":
                    brainstorm_clarify_question = (
                        _clarify_question_text(plan) or brainstorm_clarify_question)
                plan.action = "answer"

            # Safety gate: if planner chose "read" for many consecutive steps, force a terminal action
            if plan and plan.action == "read":
                consecutive_reads += 1
                # Escalate to deep if: max_consecutive_reads+ consecutive reads
                if consecutive_reads >= cfg.max_consecutive_reads:
                    log.warning(
                        f"Planner stuck in read loop after {steps} steps / {consecutive_reads} reads. "
                        f"Force-escalating to {'answer' if brainstorm_active else 'deep'} with gathered context."
                    )
                    if brainstorm_active:
                        # Brainstorm may not act: wrap up with a grounded answer instead.
                        plan.action = "answer"
                    else:
                        plan.action = "deep"
                        plan.goal = plan.goal or f"Complete the request: {user_message}"
                        plan.deep_brief = plan.deep_brief or user_message
            else:
                consecutive_reads = 0  # Reset when planner chooses something else
            # When narrating, the planner's `rationale` is ALREADY the user-facing spoken beat for
            # this step (written conversationally, in the rep's voice). We relay it through the same
            # narration channel as the instant ack (a partial), so the plan/replan event itself
            # carries no duplicate text. When not narrating, the plan event keeps the terse rationale
            # as its expandable detail (legacy behavior).
            emit.emit(ProgressEvent(type=(EVENT_PLAN if step == 0 else EVENT_REPLAN),
                                    action=plan.action, step=steps,
                                    text=(None if narrator.enabled else (plan.rationale or None))))
            # Relay this planner decision as the next beat of the train of thought, but only for
            # actions that involve a real wait (gathering context, deep work) so quick answers add
            # no latency. Zero extra LLM call: the beat is the planner's own conversational rationale.
            if narrator.enabled and plan.action in ("read", "deep"):
                narrator.relay(plan.rationale)
            # Emit cumulative token counts so live consumers see usage grow in real time.
            _ti = getattr(self.provider, 'tokens_in', 0)
            _to = getattr(self.provider, 'tokens_out', 0)
            if _ti or _to:
                emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                        data={"tokens_in": _ti, "tokens_out": _to,
                                              "total": _ti + _to}))

            # --- OVERSEER hook A submit (in-loop, NON-BLOCKING): fire a fresh minimal-intervention
            # consult for THIS plan into a background thread, then keep walking (Fix 1). Its result
            # is polled at the top of the NEXT step (above), one step late. We only submit when the
            # plan is a "read" (the loop will continue, so there IS a next step to apply a signal to)
            # and when no consult is already in flight (one at a time). Gated so it stays cheap:
            # cadence, min-step, a hard cap on total submissions shared with hook B, AND (Fix 12) a
            # free non-LLM pre-filter inside ``_submit_oversee`` (``gate=True``, the default) that
            # skips the consult entirely unless something actually looks worth a look. Wrapped so it
            # can NEVER break the loop. Off by default.
            if (cfg.overseer and plan.action == "read" and pending_oversee is None
                    and steps >= cfg.overseer_min_step
                    and steps % max(1, cfg.overseer_every_steps) == 0
                    and overseer_submitted < cfg.overseer_max_signals):
                try:
                    if _oversee_executor is None:
                        _oversee_executor = ThreadPoolExecutor(max_workers=1)
                    # Fix 3/4: judge the run against the RAW request (user_message) AND the RESOLVED
                    # request (goal_condition, when it differs) plus the completion bar
                    # (quality_standards). Fix 5a/7: also carry this-conversation history.
                    _prev_sig = ((_prev_plan_for_gate.action, _prev_plan_for_gate.goal)
                                if _prev_plan_for_gate is not None else None)
                    pending_oversee = self._submit_oversee(
                        _oversee_executor, user_message=user_message, goal_condition=goal_condition,
                        step=steps, plan=plan, gathered=gathered, started=started,
                        quality_standards=quality_standards,
                        recent_conversation=_recent_conversation_digest_lines,
                        prior_escalations=_prior_escalation_digest_lines,
                        prev_plan_signature=_prev_sig)
                    if pending_oversee is not None:
                        overseer_submitted += 1
                except Exception:  # noqa: BLE001 — the overseer must never break the loop
                    pass

            if plan.action == "read":
                if not plan.reads:
                    plan.action = "answer"
                else:
                    if any(r.get("list_sources") or r.get("describe_source")
                           or r.get("list_operations") or r.get("describe_operation")
                           or r.get("list_guidance") or r.get("read_guidance")
                           for r in plan.reads):
                        emit.status("Exploring…")
                    else:
                        emit.status("Searching…" if any(r.get("grep") for r in plan.reads) else "Reading…")
                    new_obs = self._do_reads(plan.reads, guidance_selected_ids, card_context)
                    gathered.extend(new_obs)
                    _sources: List[str] = []
                    for _o in new_obs:
                        if not isinstance(_o, dict):
                            continue
                        _kind = _o.get("kind", "")
                        if _kind == "grep":
                            # Show matched file paths; on empty show a "(no matches)" marker
                            _hits = _o.get("hits") or []
                            _seen_rp: set = set()
                            for _h in _hits:
                                _rp = _h.get("rel_path")
                                if _rp and _rp not in _seen_rp:
                                    _seen_rp.add(_rp)
                                    _sources.append(_rp)
                            if not _hits:
                                _pat = _o.get("pattern") or ""
                                if _pat:
                                    _sources.append(f"(searched {_pat!r} — nothing found)")
                        else:
                            _rp = _o.get("rel_path") or _o.get("pattern")
                            if _rp:
                                _sources.append(_rp)
                    emit.emit(ProgressEvent(type=EVENT_READ, step=steps,
                                            data={"reads": len(plan.reads),
                                                  "sources": _sources[:8]}))
                    if budget_exhausted():
                        break
                    continue
            if plan.action in ("answer", "deep", "confirm", "clarify"):
                break
        else:
            plan = plan or PlanDecision(action="answer")

        final = (plan or PlanDecision(action="answer")).action

        # --- BRAINSTORM TERMINAL GATE (the single choke point) --------------------------------
        # Whatever set the action -- the planner, the read-loop safety escalation, an overseer
        # signal -- a latched turn may neither ACT nor ESCALATE. Every terminal path below funnels
        # through here, so the invariant ("a latched turn executes nothing and creates no
        # decision-request") holds structurally instead of depending on each path remembering to
        # check the latch. "clarify"/"confirm" carry a question the user still deserves to hear, so
        # it is not dropped: it rides into the reply text via the acknowledgment note below.
        if brainstorm_active and final in ("deep", "confirm", "clarify"):
            log.info("Brainstorm mode: degrading terminal action %r to 'answer'.", final)
            brainstorm_suppressed_action = brainstorm_suppressed_action or final
            if final == "clarify":
                brainstorm_clarify_question = (_clarify_question_text(plan)
                                               or brainstorm_clarify_question)
            elif final == "confirm" and plan.confirm_question:
                brainstorm_clarify_question = (brainstorm_clarify_question
                                               or plan.confirm_question.strip())
            plan.action = final = "answer"

        # BRAINSTORM NO-ACTION ACKNOWLEDGMENT (zero extra LLM calls): while the latch is held, the
        # escalation nets are suppressed wholesale, so a held turn would otherwise end with a reply
        # that never says nothing ran (or, worse, improvises that it is about to run). The note is
        # computed ONCE, HERE, before ANY reply-producing path runs, and passed as the
        # ``reply_directive`` of EVERY reply generator below (the read-budget wrap-up, the main
        # answer, the sub-question answerer, and every regeneration, all of which run through
        # ``_gen_answer``). It lands on the answering call's SYSTEM prompt, next to the rest of the
        # reply contract, which is the layer an instruction the reply must OBEY belongs in.
        #
        # Both halves of that are the fix for a real failure. It used to be folded into the answer
        # GROUNDING at the main answer only, so (a) turns that returned earlier (clarify, the
        # read-budget wrap-up) shipped replies that had never been told the turn was held, and
        # (b) even when it did reach the model it sat inside a block introduced as "answer FROM
        # this, never mention that you read it", the wrong frame for an instruction to speak up.
        # It rides on every latched turn, not only ones an intent detector flags (see the note's own
        # comment: the detector missed exactly the phrasings that needed it most), and the two extra
        # steers below are added when the turn ALSO wanted to act or to ask.
        brainstorm_ack_note: Optional[str] = None
        if brainstorm_active:
            brainstorm_ack_note = BRAINSTORM_NO_ACTION_ACK_NOTE
            planner_tried_to_act = (brainstorm_suppressed_action in ("deep", "confirm")
                                    or bool(plan.deferred_deep)
                                    or bool(plan.answer_contains_work_to_execute))
            if planner_tried_to_act:
                brainstorm_ack_note += BRAINSTORM_HELD_WORK_ACK_NOTE
            if brainstorm_clarify_question:
                brainstorm_ack_note += (BRAINSTORM_CLARIFY_ACK_PREFIX
                                        + brainstorm_clarify_question.strip() + "\n")

        # THE REPLY DIRECTIVE for every reply generator this turn (it lands on the answering call's
        # SYSTEM prompt, next to the reply contract, which is where an instruction the reply must
        # OBEY belongs). Two things can ride it, and they compose:
        #   * the brainstorm no-action acknowledgment (above), when the latch is held; and
        #   * the CARD LIFECYCLE gate, whenever per-idea threading is on. A card outlives the work
        #     it describes, so a COMPLETED quest or a finished project can surface as context on any
        #     turn. Without this the assistant reads it as live work and starts proposing how to do
        #     it. The gate says: treat finished work as knowledge you may cite and build on, never
        #     as open work, unless the user explicitly reopens it.
        reply_directive: Optional[str] = brainstorm_ack_note
        if cfg.card_thread_enabled:
            reply_directive = (CARD_LIFECYCLE_GATE + "\n\n" + brainstorm_ack_note
                               if brainstorm_ack_note else CARD_LIFECYCLE_GATE)

        def _answer_grounding(steering: Optional[str] = None) -> str:
            # The L2 grounding EVERY reply this turn is built on.
            cv = context_view
            if steering:
                cv = ((context_view + "\n\n" if context_view else "")
                      + "--- IMPROVE YOUR ANSWER (it did not yet meet the goal) ---\n" + steering)
            # Forward the planner's reasoning so the answerer benefits from what the planner
            # already worked out. Without this the answerer re-derives from scratch and may
            # reach a different (wrong) conclusion, e.g. "no prior history" despite the planner
            # having already found and summarised the conversation history correctly.
            if plan.rationale and plan.action == "answer":
                planner_block = f"--- PLANNER ANALYSIS ---\n{plan.rationale}"
                cv = (cv + "\n\n" + planner_block if cv else planner_block)
            return cv

        # Cap/budget fallback: still in read mode -> best-effort answer or escalate to deep.
        # In brainstorm mode escalation is unavailable, so a budget-capped turn always wraps up
        # with a best-effort grounded answer (even with nothing gathered) instead of acting -- and
        # it grounds through ``_answer_grounding``, so it carries the no-action acknowledgment too.
        # A CHANGE REQUEST never wraps up with words when escalation is available: a budget-capped
        # "do X" answered with "here is what the system would need to do" used to be reported done
        # with nothing executed (caught live by the 2026-07-19 reliability battery: a write-a-file
        # probe answered "I cannot execute this task in the read-and-answer step" and PATCHed done,
        # bypassing the _answer_describes_unexecuted_work net below, which only guards the normal
        # answer path). Requests for work escalate to deep instead.
        if final not in ("answer", "deep", "confirm", "clarify"):
            must_execute = (not brainstorm_active) and _message_requests_change(user_message)
            if (gathered or brainstorm_active) and not must_execute:
                emit.status("Wrapping up with a best-effort answer…")
                model = self._answer_model(plan, "balanced", hint=model_hint)
                text = self._grounded_answer(user_message, transcript, _answer_grounding(), gathered,
                                             model, True, native_blocks=native_blocks,
                                             reply_directive=reply_directive)
                return finish(OrchestratorResult(kind="answer", text=text, rationale=plan.rationale,
                                                 partial=True, model=model,
                                                 exit_reason="read_budget"))
            plan.action = final = "deep"
            plan.goal = _truncate_goal(plan.goal or f"Fully address the request: {user_message}")
            plan.deep_brief = plan.deep_brief or user_message

        if final == "clarify":
            # User clarification/selection needed: surface as decision-request
            res = self._run_clarify(plan, quest_id=quest_id, emit=emit)
            res.exit_reason = "clarify"
            return finish(res)

        if final == "deep":
            # Show goal condition before executing
            goal_text = plan.goal or f"Complete: {user_message[:100]}"
            emit.emit(ProgressEvent(type=EVENT_RESULT, text=f"Executing: {goal_text}"))
            emit.status("Running now…")
            res = self._run_deep(plan, user_message, self._answer_model(plan, "opus", hint=model_hint),
                                 emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                                 gathered=gathered, quality_standards=quality_standards,
                                 pending_inputs=pending_inputs, model_hint=model_hint,
                                 ctx_meta=_ctx_meta, cancel_check=cancel_check,
                                 working_dir_override=working_dir_override)
            if res.kind == "cancelled":
                return finish(res)
            res.exit_reason = "deep_met" if (res.deep_results and all(d.met for d in res.deep_results)) else "deep_not_met"
            if overseer_decided == "overseer_escalated_deep":
                res.exit_reason = "overseer_escalated_deep"
            # Background: categorize edited files into context cards (deep runner returns edited_files in metadata)
            if res.deep_results and any(dr.met for dr in res.deep_results):
                self._update_context_cards_after_deep(res, context_meta)
            # Background (ASYNC, best-effort): prepare reusable context for this user's NEXT similar
            # request by updating their cards from this run. Off the result path; never blocks finish.
            self._kickoff_card_update(res, plan, user_message, _ctx_meta, emit)
            return finish(res)

        if final == "confirm":
            res = self._run_confirm(plan, quest_id=quest_id)
            # A confirm can be planner-originated (rare) or come from an OVERSEER escalate_human
            # signal (Fix 2); distinguish the exit_reason so a caller/consumer can tell the two apart
            # (e.g. for building next-turn's ``prior_escalations``).
            res.exit_reason = ("overseer_escalated_human" if overseer_decided == "overseer_escalated_human"
                               else "confirm")
            return finish(res)

        # answer
        model = self._answer_model(plan, "sonnet", hint=model_hint)

        def _gen_answer(steering: Optional[str]) -> str:
            # Produce an answer, optionally STEERED by goal-verification feedback (the prior answer
            # plus why it fell short + what to fix) folded into the grounding context. EVERY reply
            # this turn produces (including each regeneration) goes through here or the read-budget
            # wrap-up above, and both pass ``brainstorm_ack_note`` as the reply directive, so a
            # latched turn cannot ship a reply that was never told the turn was held.
            cv = _answer_grounding(steering)
            if len(plan.subquestions) >= 2:
                return self._answer_subquestions(user_message, transcript, cv, gathered,
                                                 model, plan.subquestions, native_blocks=native_blocks,
                                                 reply_directive=reply_directive)
            return self._grounded_answer(user_message, transcript, cv, gathered, model,
                                         False, native_blocks=native_blocks,
                                         rep_preamble=rep_preamble,
                                         reply_directive=reply_directive)

        emit.status(f"Answering {len(plan.subquestions)} parts in parallel…"
                    if len(plan.subquestions) >= 2 else "Answering")
        text = _gen_answer(None)
        _ti = getattr(self.provider, 'tokens_in', 0)
        _to = getattr(self.provider, 'tokens_out', 0)
        if _ti or _to:
            emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                    data={"tokens_in": _ti, "tokens_out": _to, "total": _ti + _to}))

        # --- OVERSEER hook B (answer checkpoint): one last minimal-intervention watch, now with the
        # DRAFT answer in the digest. It can escalate_deep (this is not really an answer, it needs
        # real execution), escalate_human (a genuine human-only fork), or redirect (regenerate the
        # answer once with a one-line steering hint). proceed / answer_now accept the draft. Counted
        # against the same per-run cap as hook A. Wrapped so it can never break the turn (any failure
        # degrades to accepting the draft). Off by default. Not gated by the Fix-12 cadence heuristic
        # (``gate=False``): this is a one-time final check, not a cadence, so it always consults.
        #
        # DESIGN NOTE (Fix 11, hook B non-blocking): hook B used to WAIT synchronously (up to a short
        # bound) before shipping the answer, on EVERY turn -- real added latency even when nothing
        # was wrong. It is now NON-BLOCKING like hook A: submit, do one quick non-blocking check
        # (covers the rare already-resolved case), and if it has not resolved yet, SHIP THE DRAFT NOW
        # and hand the pending consult to ``_finish_oversee_in_background`` instead of blocking. That
        # background finisher raises a REAL decision-request for a late ``escalate_human`` (durable,
        # reaches the human regardless of the stream) and records a late ``EVENT_OVERSEER`` for
        # ``redirect``/``escalate_deep`` (best-effort telemetry for a consumer to fold into next
        # turn's ``prior_escalations``); it deliberately does NOT auto-launch a new deep execution
        # with no one left to receive its result. See ``_finish_oversee_in_background``'s docstring
        # and docs/overseer.md for the full tradeoff. A FAST-resolving consult (the common case for a
        # quick model) still corrects the SAME answer before it ships, exactly as before.
        if cfg.overseer and overseer_submitted < cfg.overseer_max_signals:
            try:
                if _oversee_executor is None:
                    _oversee_executor = ThreadPoolExecutor(max_workers=1)
                # Fix 3/4: the raw request (user_message) AND the resolved request (goal_condition)
                # + completion bar + this-conversation history.
                _bpending = self._submit_oversee(
                    _oversee_executor, user_message=user_message, goal_condition=goal_condition,
                    step=steps, plan=plan, gathered=gathered, started=started, draft_answer=text,
                    quality_standards=quality_standards,
                    recent_conversation=_recent_conversation_digest_lines,
                    prior_escalations=_prior_escalation_digest_lines,
                    gate=False)
                _bsig = OverseerSignal("proceed")
                if _bpending is not None:
                    overseer_submitted += 1
                    _collected = self._collect_oversee(
                        _bpending, signals=overseer_signals, emit=emit,
                        timeout=cfg.overseer_poll_timeout_seconds)
                    if _collected is not None:
                        _bsig = _collected
                    else:
                        # Not resolved yet: ship the draft now; a late resolution is handled async.
                        self._finish_oversee_in_background(
                            _bpending, emit=emit, quest_id=quest_id,
                            brainstorm_active=brainstorm_active)
                if brainstorm_active and _bsig.signal in ("escalate_deep", "escalate_human"):
                    # Brainstorm mode: overseer escalations may not ADD actions; ship the draft.
                    pass
                elif _bsig.signal == "escalate_deep" and self._has_deep_execution_capability():
                    if emit is not None:
                        emit.status("Overseer: this needs real execution, running it now…")
                    _ov_plan = PlanDecision(
                        action="deep",
                        goal=_truncate_goal(plan.goal or f"Carry out the user's request: {user_message}"),
                        deep_brief=(user_message)[:2000],
                        rationale="overseer escalated the answer to deep execution",
                    )
                    _ov_model = self._answer_model(_ov_plan, "opus", hint=model_hint)
                    _ov_res = self._run_deep(
                        _ov_plan, user_message, _ov_model,
                        emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                        gathered=gathered, quality_standards=quality_standards,
                        pending_inputs=pending_inputs, model_hint=model_hint,
                        ctx_meta=_ctx_meta, cancel_check=cancel_check,
                        working_dir_override=working_dir_override)
                    if _ov_res.kind == "cancelled":
                        return finish(_ov_res)
                    _ov_res.exit_reason = "overseer_escalated_deep"
                    self._kickoff_card_update(_ov_res, _ov_plan, user_message, _ctx_meta, emit)
                    return finish(_ov_res)
                elif _bsig.signal == "escalate_human":
                    # Genuine human-only fork (Fix 2): route through the SAME confirm / decision-
                    # request mechanism as a planner-originated confirm, discarding the drafted
                    # answer (mirrors the pre-existing escalate-to-deep precedent just above, which
                    # also discards the draft in favor of the correct terminal path).
                    if emit is not None:
                        emit.status("Overseer: this needs your input before I can finish…")
                    _confirm_plan = PlanDecision(
                        action="confirm",
                        confirm_question=(_bsig.reason
                                          or f"This needs your input before I continue: {user_message}"),
                        rationale="overseer escalated the answer to a human decision",
                    )
                    _confirm_res = self._run_confirm(_confirm_plan, quest_id=quest_id)
                    _confirm_res.exit_reason = "overseer_escalated_human"
                    return finish(_confirm_res)
                if _bsig.signal == "redirect" and _bsig.hint:
                    if emit is not None:
                        emit.status("Overseer: refining the answer…")
                    _steer = (f"A reviewer flagged this for one correction: {_bsig.hint}\n\n"
                              f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                    text = _gen_answer(_steer)
            except Exception:  # noqa: BLE001 — the overseer must never break the turn
                pass

        # If deferred_deep is set, also run the deep task now, synchronously, right after the
        # answer text is produced (same turn; nothing is saved for later)
        # OR if planner explicitly flagged answer_contains_work_to_execute
        # OR auto-detect false claims (fallback for broken prompts)
        # BRAINSTORM MODE: this entire block is an escalation net (it can only ADD execution to
        # an answer turn), so while the latch is held it is skipped wholesale: no deferred deep,
        # no work-to-execute flag, no described-work net, no message-intent fallback (regex OR
        # LLM judgment). Describing possible work IS the product in brainstorm.
        should_defer_deep = None if brainstorm_active else plan.deferred_deep
        # Capability for these nets = inline execution OR a wired deferred queue: everything they
        # can set flows through the deferred block below, which reaches the queue runner by explicit
        # override, so a queue-only consumer (no default runner, no classifier) is capable here.
        if (not should_defer_deep and not brainstorm_active
                and (self._has_deep_execution_capability()
                     or self._has_deferred_queue_capability())):
            # Primary: trust planner's explicit flag
            if plan.answer_contains_work_to_execute:
                should_defer_deep = {"goal": f"Execute what the answer describes: {user_message}",
                                      "rationale": "planner indicated answer contains work to execute"}
                if emit is not None:
                    emit.status("Executing described work now…")
            # Fallback: the answer DESCRIBES executable work it never did ("I need to update X",
            # "to fix this I need to..."). The cheap planner frequently forgets to set
            # answer_contains_work_to_execute on code/file-change tasks, so without this net the
            # turn ends having only TALKED about the fix instead of doing it (the "it just finishes
            # the request" regression). Re-wired here so a described-but-unexecuted fix still
            # escalates to a deep run that actually applies it -- but ONLY when the user's own
            # message was itself a change request (``_message_requests_change``), never for a
            # genuine question ("why is X broken?", "what would it take to fix Y?"). Explaining
            # what a fix would involve IS the correct answer to a question; describing it must
            # never silently open a task. A question whose message still carries an ambiguous
            # action signal gets a fair shot at the message-intent LLM judgment below, which sees
            # this same answer text (``judge_execution_directive``), instead of being escalated by
            # this regex alone.
            elif _answer_describes_unexecuted_work(text) and _message_requests_change(user_message):
                should_defer_deep = {"goal": f"Execute the work the answer describes: {user_message}",
                                      "rationale": "auto-detected unexecuted work in answer (fallback)"}
                if emit is not None:
                    emit.status("Executing described work now…")
            # Decisive fallback, keyed off the STABLE USER MESSAGE (not the variable answer text):
            # the user asked for a CHANGE (fix/implement/"it incorrectly X"…), a deep runner is
            # available, yet the planner routed to "answer" and nothing executed this turn. The
            # earlier regex nets only match specific ANSWER phrasings, which a model like gemini
            # rarely produces verbatim, so an actionable request would silently end as a proposal.
            # Detecting intent from the message instead reliably catches that case. The brief carries
            # the assistant's proposed approach so the deep run APPLIES it rather than re-deriving.
            # (Executing here is fine even when the answer falsely claims completion: the goal
            # verification below re-checks the folded post-deep answer against the execution record.)
            #
            # The regex (_message_requests_change) is a cheap PREFILTER, decisive on its own for the
            # common case (a match is trusted with zero extra cost). Only in the AMBIGUOUS band it
            # leaves undecided -- a change verb/wrongness signal fired but an interrogative opener or
            # a bare "?" ending overrode it, see message_change_signal_ambiguous -- does ONE
            # structured LLM judgment step in (WS3, HANDS_FREE_QUEST_AI_DESIGN.md section 4),
            # hard-timeout-guarded and falling back to the regex verdict (False) on any failure. So
            # this never adds a blocking call to the ordinary "clearly not a directive" case, and
            # never blocks the turn even in the ambiguous case.
            elif exec_record is None or not exec_record.any_mutation_attempted:
                # A turn that just RELEASED the brainstorm hold is a directive by definition: the
                # release judge only says true when the user told us to stop holding back and act on
                # what was discussed. The work itself usually lives in the transcript, not in that
                # short message ("go ahead"), so the regex below cannot see it and the turn would
                # otherwise end with one more proposal (and, worse, a reply claiming it had acted).
                _is_directive = brainstorm_released_this_turn or _message_requests_change(user_message)
                _directive_reason = ("brainstorm release: the user lifted the hold and told us to act"
                                     if brainstorm_released_this_turn
                                     else "message-intent fallback (regex)")
                if not _is_directive and message_change_signal_ambiguous(user_message):
                    _is_directive, _llm_reason = self.judge_execution_directive(user_message, text)
                    _directive_reason = f"message-intent fallback (LLM judgment: {_llm_reason})"
                if _is_directive:
                    log.info("Escalating answer->deep: user message requests a change but the turn "
                             "only produced a proposal; running it now (%s).", _directive_reason)
                    _proposal = (text or "").strip()
                    _brief = user_message if not _proposal else (
                        f"{user_message}\n\nThe assistant proposed this approach. APPLY it (make the "
                        f"actual code/file/data changes, do not just describe them):\n{_proposal}"
                    )
                    should_defer_deep = {
                        "goal": f"Carry out the user's request: {user_message}",
                        "brief": _brief,
                        "rationale": "user message requests a change but turn only proposed it (message-intent fallback)",
                    }
                    if emit is not None:
                        emit.status("You asked for a change, making it now…")

        # True once a deferred deep run has produced substantive output that we folded back into the
        # final answer. Gates the post-deep goal-verification loop below so a deferred-deep turn is
        # still held to the overall goal (grounded in what the deep run actually produced).
        _deferred_deep_grounded = False
        # True once a QUEUED deployment's deferred hand-off is CONFIRMED (the deep runner enqueued
        # the work and returned a deferred met result). Gates the goal-verification loop below off
        # for this turn: the deferred contract trusts met and never re-verifies a hand-off sentinel
        # against the user's goal (that would always fail and could relaunch, double-enqueueing).
        _deferred_handoff_confirmed = False
        _queued_mode = bool(self.cfg.deferred_deep_queued)
        if should_defer_deep:
            try:
                if _queued_mode:
                    emit.status("Handing this work to the background queue…")
                elif not plan.deferred_deep:
                    emit.status("Executing follow-up work…")
                else:
                    emit.status("Continuing with the follow-up work now…")
                deferred_plan = PlanDecision(
                    action="deep",
                    goal=_truncate_goal(should_defer_deep.get("goal") or f"Execute: {user_message}"),
                    deep_brief=(should_defer_deep.get("brief") or user_message)[:2000],
                    rationale=should_defer_deep.get("rationale") or "follow-up work from answer phase",
                )
                # Show goal condition before executing
                if emit is not None:
                    _followup_verb = "Queueing" if _queued_mode else "Executing"
                    emit.emit(ProgressEvent(type=EVENT_RESULT,
                                            text=f"{_followup_verb} follow-up: {deferred_plan.goal}"))
                deep_model = self._answer_model(deferred_plan, "opus", hint=model_hint)
                # Queued deployments pin deferred work to the registered queue runner (reserved
                # key), so the classifier can never re-route it to an inline runner.
                _deferred_runner = (self.deep_runners.get(DEFERRED_RUNNER_KEY)
                                    if _queued_mode else None)
                deep_res = self._run_deep(deferred_plan, user_message, deep_model,
                                         emit=emit, rep_preamble=rep_preamble,
                                         exec_record=exec_record, gathered=gathered,
                                         quality_standards=quality_standards,
                                         pending_inputs=pending_inputs, model_hint=model_hint,
                                         ctx_meta=_ctx_meta, cancel_check=cancel_check,
                                         runner_override=_deferred_runner,
                                         working_dir_override=working_dir_override)
                if deep_res.kind == "cancelled":
                    return finish(deep_res)
                # WHAT ACTUALLY CAME BACK? Three outcomes, and the reply must match the one that
                # really happened this turn:
                #   * CONFIRMED HAND-OFF (queued deployments only): the runner did not execute the
                #     work, it queued it out-of-band and returned a receipt. Report it as queued,
                #     never as done; the real work is reported back into the conversation by the
                #     external runner when it finishes. A receipt is never folded through the
                #     after-deep prompt (that would present a queue acknowledgement as finished work).
                #   * REAL INLINE OUTPUT: the work RAN and produced something. Fold it back and
                #     report it, EVEN IN QUEUED MODE. That is exactly what happens when a queued
                #     consumer's ``deep_runners`` map lacks (or typos) DEFERRED_RUNNER_KEY: the
                #     hand-off pin resolves to nothing, the normal wiring executes the work for
                #     real, and telling that user "this was not queued" would be a lie about a turn
                #     that did the job, with the output dropped on the floor.
                #   * NEITHER: no receipt, no output. Only then may the turn say it is not queued.
                _results = deep_res.deep_results or []
                # A hand-off is TRUSTED only when this deployment actually queues deferred work AND
                # the runner CONFIRMED the enqueue: deferred, met, no error, and a non-empty
                # receipt. A runner that reports met=True with deferred=True on a FAILED enqueue (or
                # returns an empty receipt) is not believed; that turn falls through to the honest
                # paths below rather than promising a queue entry that does not exist.
                _handoffs = ([d for d in _results if getattr(d, "deferred", False)]
                             if _queued_mode else [])
                _confirmed = [d for d in _handoffs
                              if d.met and not (d.error or "").strip() and (d.output or "").strip()]
                # Inline output = output from results that are NOT hand-off receipts. In queued mode
                # every deferred result is a receipt (trusted or not) and never counts as work
                # output; with the flag off there is no hand-off contract at all, so every result is
                # ordinary deep output, exactly as before.
                _inline_results = ([d for d in _results if not getattr(d, "deferred", False)]
                                   if _queued_mode else list(_results))
                deep_output = "\n\n".join(
                    s for s in (_strip_future_context(d.output) for d in _inline_results) if s
                ).strip()
                if _confirmed:
                    _handoff_out = "\n\n".join(
                        (d.output or "").strip() for d in _confirmed).strip()
                    if emit is not None:
                        emit.status("Queued. It will report back here when it finishes.")
                    text = self._synthesize_after_queued(
                        user_message, prior_answer=text, handoff_output=_handoff_out,
                        transcript=transcript, model=model, rep_preamble=rep_preamble)
                    _deferred_handoff_confirmed = True
                elif deep_output:
                    # FOLD THE DEEP OUTPUT BACK INTO THE FINAL ANSWER. The pre-deep `text` is a proposal
                    # ("shall I proceed?"); the real deliverable is what the deep run just produced. If we
                    # only emit it as a side milestone, the user-facing reply stays the stale proposal with
                    # no awareness of the work (and some consumers truncate a milestone to its first line),
                    # so the substance is lost. Re-synthesize the reply grounded in the deep output, ground
                    # the context_view in it too (so any goal-verification regeneration stays aware of it),
                    # and flag the turn so the goal loop below still holds it to the overall goal.
                    if _queued_mode:
                        # Queued mode, yet the work ran inline: the queue runner is not reachable
                        # (missing/typoed DEFERRED_RUNNER_KEY, or a runner that executed instead of
                        # enqueueing). Loud in the log, honest to the user: they get the real result.
                        log.warning(
                            "deferred_deep_queued is on but the deferred work ran INLINE and "
                            "produced output (no confirmed hand-off): check that deep_runners has a "
                            "runner under the %r key. Reporting the real inline result.",
                            DEFERRED_RUNNER_KEY)
                    if emit is not None:
                        emit.status("Writing up what was done…")
                    text = self._synthesize_after_deep(
                        user_message, prior_answer=text, deep_output=deep_output,
                        transcript=transcript, model=model, rep_preamble=rep_preamble)
                    _deep_block = "--- WHAT WAS JUST EXECUTED (deep run output) ---\n" + deep_output
                    context_view = (context_view + "\n\n" + _deep_block) if context_view else _deep_block
                    _deferred_deep_grounded = True
                elif _queued_mode:
                    # HONEST-ENQUEUE: this deployment queues deferred work, NO hand-off was
                    # confirmed this turn (the enqueue failed, or the run errored before it), and
                    # nothing ran inline either, so there is genuinely no completed work to report.
                    # The reply must not claim the work is queued, running, or done. Nothing here
                    # retries the enqueue: a silent second attempt could double-queue.
                    _handoff_err = "; ".join(
                        (d.error or "").strip() for d in _results
                        if (d.error or "").strip()) or "the hand-off did not complete"
                    if emit is not None:
                        emit.status("Could not queue the background work.")
                    try:
                        text = _gen_answer(
                            "The attempt to hand this work to the background queue FAILED: "
                            + _handoff_err + "\n\nRewrite your reply to be honest: the work has NOT "
                            "been queued, started, or done. Keep any real findings you already have, "
                            "say plainly that the background hand-off failed, and suggest the user "
                            "try again or ask you to retry. Do not use em dashes.\n\n"
                            f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                    except Exception:  # noqa: BLE001 — honesty must not depend on an LLM call
                        # DETERMINISTIC FLOOR. The regeneration is the only thing standing between
                        # the user and a pre-deep draft that (under queued doctrine) may already
                        # claim "I have queued this". If that call fails, we still correct the
                        # record without a model: append the plain not-queued sentence.
                        log.warning("honest-enqueue rewrite failed; appending the not-queued note",
                                    exc_info=True)
                        text = ((text or "").strip() + "\n\n" + NOT_QUEUED_NOTE).strip()
                # Async, best-effort: prepare this user's cards for next time from the deferred run.
                # Skipped for a confirmed hand-off: a queue receipt sentinel holds nothing to learn.
                if not _deferred_handoff_confirmed:
                    self._kickoff_card_update(deep_res, deferred_plan, user_message, _ctx_meta, emit)
            except Exception as e:  # noqa: BLE001 — deferred work must never break the answer
                log.warning(f"Deferred deep work failed: {type(e).__name__}: {e}", exc_info=True)

        # TOP-TIER GOAL VERIFICATION — the SAME goal loop, now applied to a plain ANSWER so EVERY
        # input is pursued as a goal. Hold the answer to the user's overall goal at the quality bar
        # and regenerate with steering (the prior answer + why it fell short + what to fix) until it
        # meets the bar or attempts run out. When the verifier says the answer lacks the context needed
        # to be definitive (need_more_context=True), escalate to deep so the deep runner can search
        # further — never accept "I couldn't find it" as a final answer when more searching is possible.
        # With ``verify_claims`` on, the SAME verdict also checks honesty: any change the answer claims
        # it completed must be backed by the turn's EXECUTION RECORD (claims_unexecuted). An unbacked
        # claim remediates here: execute the work for real via a deep run when nothing ran this turn
        # (safe, capped by max_remediations), else regenerate the reply to be honest about what
        # actually happened. Always runs — even when a deferred deep fired but produced no output (in
        # that case the pre-deep proposal still needs to be held to the goal bar). Best-effort: never
        # breaks the turn.
        # DEFERRED HAND-OFF EXCEPTION: when this turn's work was confirmed QUEUED (deferred
        # contract), the goal loop is skipped wholesale. The user's goal is intentionally not met
        # yet (the work runs out-of-band), so verifying the hand-off reply against it would always
        # fail and the remediation net could relaunch the work, double-enqueueing the task. The
        # real outcome is verified by the external runner's own goal loop and reported back.
        _last_verdict: Optional[Dict[str, Any]] = None
        _claim_corrected = False
        if (not _deferred_handoff_confirmed
                and (self.cfg.answer_goal_max_iterations > 1 or self.cfg.verify_claims)):
            try:
                # Verify against the turn's DERIVED GOAL CONDITION (the checkable done-standard
                # established at turn start by _understand_input/_derive_goal_condition) when one
                # was actually derived: that is the user-level bar for the WHOLE turn. Deriving a
                # done-standard and then judging the answer against something else (the plan's own
                # goal restatement) would let the two drift. When no distinct condition was derived
                # (it equals the raw message, e.g. the derivation failed safe), fall back to the
                # plan's goal, then to the generic bar, exactly as before.
                overall_goal = (goal_condition or "").strip()
                if not overall_goal or overall_goal == (user_message or "").strip():
                    overall_goal = (plan.goal or "").strip() or (
                        "Fully and correctly answer the user's request to their satisfaction: "
                        + user_message)
                # The SAME rendered L2 context layer the answer call(s) this turn used (see
                # ``_grounded_answer``'s ``layers`` wiring): ``context_view`` does not change across
                # this loop's regenerate/verify iterations (a regeneration only rewrites ``text``), so
                # computing it once here gives every verify call in this loop a byte-identical L2 to
                # the answer it is judging, and the same L2 across iterations too.
                answer_context_layer = grounding_context_layer(context_view)
                _max = max(1, self.cfg.answer_goal_max_iterations)
                if self.cfg.verify_claims:
                    # The claim check needs at least one verification pass even when the goal loop
                    # itself is configured off (answer_goal_max_iterations <= 1).
                    _max = max(2, _max)
                _remediations = 0
                _attempt = 1
                while _attempt < _max:  # at most _max-1 regenerations after the first answer
                    verdict, verify_error = self._verify_goal(
                        overall_goal, user_message, text,
                        rep_preamble=rep_preamble,
                        quality_standards=quality_standards,
                        transcript=transcript,
                        exec_record=exec_record if self.cfg.verify_claims else None,
                        context_layer=answer_context_layer)
                    if verdict is not None:
                        _last_verdict = verdict
                    if verdict is not None and verdict.get("met"):
                        if emit is not None:
                            emit.status("Answer verified against the goal.")
                        break
                    if verdict is None:
                        # Verifier call failed (LLM error, parse failure, etc.) - do not silently
                        # accept. Retry on the next attempt; if this was the last attempt, the turn
                        # proceeds unverified (``_exit_reason`` stays "unverified" below, never
                        # "verified") rather than being blocked, but the real reason is logged so an
                        # outage is diagnosable, not silent.
                        log.warning("Goal verifier did not run (attempt %d/%d): %s",
                                    _attempt, _max - 1, verify_error or "unknown reason")
                        if _attempt >= _max - 1:
                            break  # out of attempts, proceeds unverified
                        _attempt += 1
                        continue  # retry the verification call
                    # HONESTY: the answer claims a completed change the execution record does not
                    # back. The answering step can never change files/data itself, so either DO the
                    # work for real now (safe only when NOTHING mutated this turn; a prior success or
                    # failure means a re-run risks a double mutation) or rewrite the reply to be
                    # honest. A deep remediation does NOT consume a regeneration attempt: the loop
                    # re-verifies the post-deep answer against the record on the next pass.
                    if verdict.get("claims_unexecuted"):
                        can_execute = (self._has_deep_execution_capability()
                                       and not brainstorm_active
                                       and exec_record is not None
                                       and not exec_record.any_success
                                       and not exec_record.any_failure
                                       and _remediations < max(0, self.cfg.max_remediations))
                        if can_execute:
                            _remediations += 1
                            if emit is not None:
                                emit.status("That change was claimed but never executed, doing it now…")
                            _rem_plan = PlanDecision(
                                action="deep",
                                goal=_truncate_goal(f"Carry out the user's request for real: {user_message}"),
                                deep_brief=(f"{user_message}\n\nThe assistant REPLIED as if this was "
                                            f"already done, but nothing actually executed. APPLY the "
                                            f"change for real now (make the actual code/file/data "
                                            f"changes):\n{(text or '').strip()}")[:2000],
                                rationale="claim verification: answer claimed unexecuted work",
                            )
                            _rem_model = self._answer_model(_rem_plan, "opus", hint=model_hint)
                            _rem_res = self._run_deep(_rem_plan, user_message, _rem_model,
                                                      emit=emit, rep_preamble=rep_preamble,
                                                      exec_record=exec_record, gathered=gathered,
                                                      quality_standards=quality_standards,
                                                      pending_inputs=pending_inputs,
                                                      model_hint=model_hint, ctx_meta=_ctx_meta,
                                                      cancel_check=cancel_check,
                                                      working_dir_override=working_dir_override)
                            if _rem_res.kind == "cancelled":
                                return finish(_rem_res)
                            _rem_out = ""
                            if _rem_res and _rem_res.deep_results:
                                _rem_out = "\n\n".join(
                                    s for s in (_strip_future_context(d.output)
                                                for d in _rem_res.deep_results) if s).strip()
                            if _rem_out:
                                if emit is not None:
                                    emit.status("Writing up what was done…")
                                text = self._synthesize_after_deep(
                                    user_message, prior_answer=text, deep_output=_rem_out,
                                    transcript=transcript, model=model, rep_preamble=rep_preamble)
                                self._kickoff_card_update(_rem_res, _rem_plan, user_message,
                                                          _ctx_meta, emit)
                            continue  # re-verify the remediated answer (attempt not consumed)
                        # Cannot safely re-run: correct the reply instead and flag the result so a
                        # background task maps to needs_you/failed, never a false done.
                        _claim_corrected = True
                        if emit is not None:
                            emit.status("Correcting the reply to reflect what actually happened…")
                        _record_summary = exec_record.summary() if exec_record is not None else ""
                        steer = ("Your previous answer claims a change was completed that did NOT "
                                 "actually execute. What actually ran this turn:\n"
                                 f"{_record_summary}\n\n"
                                 "Rewrite your answer to be honest: never claim a change was made "
                                 "when the record does not show it succeeding. Say plainly what has "
                                 "and has not been done, and what should happen next. Do not use em "
                                 "dashes.\n\n"
                                 f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                        text = _gen_answer(steer)
                        _attempt += 1
                        continue
                    # When the verifier says the answer lacked the context needed to be definitive,
                    # escalate to deep so it can search further. Regenerating with the SAME gathered
                    # context won't help — the deep runner can grep/read on its own.
                    if (verdict.get("need_more_context") and not brainstorm_active
                            and self._has_deep_execution_capability()):
                        if emit is not None:
                            emit.status("Need more context to answer — searching further…")
                        _context_q = verdict.get("context_query") or user_message
                        _esc_plan = PlanDecision(
                            action="deep",
                            goal=_truncate_goal(overall_goal),
                            deep_brief=(
                                f"{user_message}\n\n"
                                "NOTE: An initial search was done but the result was inconclusive. "
                                f"What is missing: {_context_q}. "
                                "Search more thoroughly and give a definitive answer."
                            ),
                            rationale="answer verification escalated to deep (insufficient context)",
                        )
                        _esc_model = self._answer_model(_esc_plan, "opus", hint=model_hint)
                        _esc_res = self._run_deep(
                            _esc_plan, user_message, _esc_model,
                            emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                            gathered=gathered, quality_standards=quality_standards,
                            pending_inputs=pending_inputs, model_hint=model_hint,
                            ctx_meta=_ctx_meta, cancel_check=cancel_check,
                            working_dir_override=working_dir_override)
                        if _esc_res.kind == "cancelled":
                            return finish(_esc_res)
                        _esc_res.exit_reason = "escalated_deep"
                        _esc_res.goal_verdict = verdict
                        # Async, best-effort: prepare this user's cards for next time.
                        self._kickoff_card_update(_esc_res, _esc_plan, user_message, _ctx_meta, emit)
                        return finish(_esc_res)
                    # Goal not met: surface the current answer as a milestone so the user sees
                    # progress while we continue iterating toward the goal.
                    if emit is not None and text:
                        emit.emit(ProgressEvent(type=EVENT_MILESTONE, text=text,
                                                data={"goal_not_met": True,
                                                      "reason": verdict.get("reason") or ""}))
                    if emit is not None:
                        emit.status("Answer not yet at the bar, improving it…")
                    _new = self._drain_pending(pending_inputs)
                    steer = (f"Why your previous answer fell short: "
                             f"{verdict.get('reason') or 'it did not meet the quality bar'}. "
                             f"Do this now: {verdict.get('next_action') or 'address the gap and answer fully.'}\n\n"
                             + (_new + "\n\n" if _new else "")
                             + f"--- YOUR PREVIOUS ANSWER ---\n{text}")
                    text = _gen_answer(steer)
                    _attempt += 1
            except Exception:  # noqa: BLE001 — answer verification must never break the turn
                log.warning("answer goal verification failed", exc_info=True)

        _exit_reason = "unverified"
        if _deferred_handoff_confirmed:
            # The work was confirmed queued out-of-band (deferred contract): neither verified nor
            # unverified applies to this turn; the external runner verifies the real outcome.
            _exit_reason = "deferred"
        elif _last_verdict is not None:
            _exit_reason = "verified" if _last_verdict.get("met") else "max_turns"
        # An overseer answer_now that short-circuited the read loop to this answer wins the reason,
        # so a consumer can see the terminal path was decided by the overseer.
        if (overseer_decided == "overseer_answer_now" and _last_verdict is None
                and not _deferred_handoff_confirmed):
            _exit_reason = "overseer_answer_now"
        _res = OrchestratorResult(kind="answer", text=text, rationale=plan.rationale,
                                  model=model, exit_reason=_exit_reason,
                                  goal_verdict=_last_verdict)
        if _claim_corrected:
            # The reply had to be corrected for honesty and the claimed work never executed: flag
            # the result so a background task maps to needs_you/failed, never a false done.
            _res.claim_corrected = True
            _res.partial = True
        return finish(_res)

    # --- LIVE streaming convenience: a generator yielding events as they happen --------

    def run_stream(self, user_message: str, *, transcript: str = "", context_view: str = "",
                   quest_id: Optional[str] = None,
                   mode: Mode = Mode.LIVE,
                   model_hint: Optional[str] = None,
                   attachments: Optional[List[Dict[str, Any]]] = None,
                   rep_preamble: Optional[str] = None,
                   pending_inputs: Optional[Callable[[], List[str]]] = None,
                   conv_id: Optional[str] = None,
                   conv_scope: Optional[Dict[str, Any]] = None,
                   now: Optional[str] = None):
        """Generator form of ``run`` for a LIVE consumer that wants to iterate events.

        Yields each ``ProgressEvent`` (as emitted, post-sink-policy for the given mode) and,
        as the FINAL item, the terminal ``OrchestratorResult``. Implemented by feeding a
        ``StreamSink`` whose forward appends to a thread-safe queue while ``run`` executes in a
        worker thread, so the caller streams in real time and still gets the structured result.

        ``model_hint`` is forwarded to ``run`` unchanged — see ``run`` for semantics.

        Example::

            for item in orch.run_stream("..."):
                if isinstance(item, OrchestratorResult):
                    final = item            # terminal result
                else:
                    render(item)            # a dict event
        """
        import queue as _queue
        import threading as _threading

        from .adapters import StreamSink

        q: "_queue.Queue" = _queue.Queue()
        _SENTINEL = object()
        sink = StreamSink(lambda ev: q.put(("event", ev)))

        result_box: Dict[str, Any] = {}

        def _worker():
            try:
                res = self.run(user_message, transcript=transcript, context_view=context_view,
                               quest_id=quest_id, mode=mode, sink=sink, model_hint=model_hint,
                               attachments=attachments, rep_preamble=rep_preamble,
                               pending_inputs=pending_inputs, conv_id=conv_id,
                               conv_scope=conv_scope, now=now)
                result_box["result"] = res
            except Exception as e:  # noqa: BLE001
                result_box["error"] = e
            finally:
                q.put(("done", _SENTINEL))

        t = _threading.Thread(target=_worker, daemon=True)
        t.start()
        while True:
            kind, payload = q.get()
            if kind == "event":
                yield payload
            else:
                break
        t.join()
        if "error" in result_box:
            raise result_box["error"]
        yield result_box.get("result")
