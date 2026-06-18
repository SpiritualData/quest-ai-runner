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

import inspect
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .adapters import (
    EVENT_CONTEXT,
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_EXEC,
    EVENT_MILESTONE,
    EVENT_PARTIAL,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    EVENT_TOKENS,
    ContextAssembler,
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
from .context_doctrine import CACHED_HINT_GATE, MODEL_TIER_GATE, SUFFICIENCY_GATE
from .guard import (
    ExecutionFact,
    ExecutionRecord,
    classify_exec_phase,
    honest_rewrite,
    text_claims_action,
    verify_supported,
)
from .model_registry import TIERS, ModelRegistry

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

CORE PRINCIPLE -- READ REAL CONTENT BEFORE ANSWERING:
  The CONTEXT below only LOCATES what exists (a one-line summary per item). It is NOT a
  substitute for reading the actual content. For ANY question about substance -- what a doc says,
  status, numbers, decisions, how something works -- READ the real content first (action "read"),
  THEN answer grounded in it. Only pure chit-chat/meta ("you there?", "thanks") may be answered
  WITHOUT reading.

  EXCEPTION -- CODE / FILE CHANGE TASKS GO STRAIGHT TO "deep": if fulfilling the request means
  changing code or files (fix a bug, implement / build / refactor a feature, edit / apply a change
  to a file), choose "deep" on the FIRST step. Do NOT "read" first and do NOT try to work out the
  fix yourself. The deep runner is a full coding agent with its own file tools: it explores the
  codebase and applies the edit itself, so any planner pre-reading is wasted work. "answer" is
  NEVER correct for a code/file change -- describing the fix or printing a patch instead of letting
  the deep runner apply it is a FAILURE.

  CRITICAL: Do NOT answer with "I need to X" or "I should X" or "To fix this, I need to...".
  These are NOT answers -- they are unexecuted tasks. If you realize work needs doing, choose
  "deep" immediately and let the runner do it. NEVER describe work in an answer; ALWAYS execute it.

  Recognize code-change tasks by keywords: "fix", "bug", "break", "implement", "build", "refactor",
  "edit", "update", "change", "add", "remove", "delete", "rewrite", "apply", "make". If the user
  asks you to fix/implement/change something, escalate to "deep" immediately -- do NOT answer
  about what you think the fix should be.

  If you have already read and gathered context, and now realize execution is needed: choose
  action="answer" WITH deferred_deep. The answer can acknowledge what was found, but deferred_deep
  must specify the work to execute. This executes both: user gets immediate feedback PLUS the work
  gets done in the deferred task.
"""

_PLANNER_ACTIONS = """\
The four actions:
  - "read": TARGETED, PARTIAL reads to gather what you need. In `reads`, list one or more of:
      * a section: {{"rel_path": "...", "heading": "Metrics"}} OR
                   {{"rel_path": "...", "start_line": 40, "end_line": 80}}, and/or
      * a grep:    {{"grep": "regex", "scope": "optional/subpath"}} to LOCATE content, and/or
      * a query:   {{"query": {{...}}}} for a structured source lookup (if supported), and/or
      * DISCOVERY, when you do not yet know what the source of truth contains:
          {{"list_sources": true}}                       -> the collections/tables/doc-sets that exist
          {{"describe_source": "<name>", "describe_path": "<optional nested path>"}}
                                                          -> the fields/types of ONE source (drill down)
          {{"list_operations": true}}                    -> the operations you can call (reads AND changes)
          {{"describe_operation": "<name>"}}             -> the full signature/usage of ONE operation
          {{"list_guidance": true}}                      -> the catalog of use-case-specific guidance
          {{"read_guidance": "<id>"}}                    -> the full instructions of ONE guidance card
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
    mutation must be PROPOSED/EXECUTED, never merely talked about. Provide BOTH `goal` (a CONCRETE,
    CHECKABLE done-standard, as a human would write it) and `deep_brief` (a clear self-contained
    brief that PRESERVES the user's action verb -- say "add/update ...", not "look up/review ..."). BE A
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

Always fill `rationale` (one sentence) and set `model_tier`.

--- THE USER'S MESSAGE ---
{user_message}

--- RECENT TRANSCRIPT (most recent last) ---
{transcript}

--- CONTEXT (compact; LOCATES content, does NOT replace reading it) ---
{context_view}

--- GATHERED SO FAR (targeted reads/greps done this turn; [] = nothing yet) ---
{gathered}
"""

# Assemble the final format()-able prompt. The gate constants from context_doctrine have NO
# literal {/} characters, so they pass through .format() untouched when the final assembled
# string is .format()-ed in _plan(). Only the real {slot_name} placeholders in _PLANNER_ACTIONS
# and _PLANNER_TAIL are substituted; JSON-example braces use the standard {{...}} double-brace form.
PLANNER_PROMPT = (
    _PLANNER_HEAD
    + "\n--- SUFFICIENCY (read enough before acting) ---\n"
    + SUFFICIENCY_GATE + "\n\n"
    + _PLANNER_ACTIONS
    + "\n--- " + MODEL_TIER_GATE.split("\n")[0] + "\n"
    + "\n".join(MODEL_TIER_GATE.split("\n")[1:]) + "\n\n"
    + "\n--- " + CACHED_HINT_GATE.split("\n")[0] + "\n"
    + "\n".join(CACHED_HINT_GATE.split("\n")[1:]) + "\n\n"
    + _PLANNER_TAIL
)

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
                        "list_sources": {"type": "boolean"},
                        "describe_source": {"type": "string"},
                        "describe_path": {"type": "string"},
                        "list_operations": {"type": "boolean"},
                        "describe_operation": {"type": "string"},
                        "list_guidance": {"type": "boolean"},
                        "read_guidance": {"type": "string"},
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
                "description": "When action='answer', optionally queue a deep task to run after answering.",
                "properties": {
                    "goal": {"type": "string", "description": "The goal for the deferred deep work"},
                    "brief": {"type": "string", "description": "Brief for the deferred deep work (optional, defaults to user message)"},
                    "rationale": {"type": "string", "description": "Why this deep work is queued"},
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
    planner_tier: str = "haiku"  # the cheap model that runs the planner step
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
    # GUIDANCE PRE-SELECTION: how many use-case-specific guidance cards the orchestrator asks a
    # wired GuidanceProvider to pre-select (via select()) for the "APPLICABLE GUIDANCE" block
    # before planning. Only consulted when a GuidanceProvider is wired; otherwise inert.
    guidance_topk: int = 3
    # BROKEN-PROMISE GUARD (workstream 5). At turn finalization, verify that a reply CLAIMING a
    # completed/imminent action is actually backed by what executed this turn; auto-remediate then
    # re-verify, else rewrite the reply to be honest and flag the result partial (so a background
    # task maps to needs_you/failed, not done). ON by default; the structural gate keeps it free on
    # turns with no action claim. ``max_remediations`` caps SAFE re-runs (only when no action ran).
    verify_claims: bool = True
    max_remediations: int = 1


@dataclass
class OrchestratorResult:
    """What the loop produced. Exactly one terminal kind."""
    kind: str                          # "answer" | "deep" | "confirm"
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


# ---------------------------------------------------------------------------
# Decision normalization (coerce a raw planner dict into a safe PlanDecision).
# ---------------------------------------------------------------------------

def normalize_decision(raw: Dict[str, Any], cfg: OrchestratorConfig) -> PlanDecision:
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
            ):
                clean_reads.append(r)

    tier = raw.get("model_tier")
    if isinstance(tier, str):
        tier = tier.strip().lower()
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
    )


# ---------------------------------------------------------------------------
# Rendering helpers (gathered observations -> prompt text / grounding block).
# ---------------------------------------------------------------------------

def _truncate_goal(goal: str, max_chars: int = 3900) -> str:
    """Truncate goal text to stay under Quest's 4000-char limit."""
    if len(goal) > max_chars:
        return goal[:max_chars] + " [truncated]"
    return goal


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


# Imperative change/build verbs and bug/wrongness signals that mark a USER MESSAGE as a request to
# CHANGE something (code, files, or data), not just to be informed. Kept app-agnostic.
_CHANGE_VERBS = (
    r"fix|implement|build|refactor|add|remove|delete|change|update|edit|rewrite|apply|create|"
    r"make|migrate|rename|move|replace|configure|enable|disable|integrate|wire\s+up|hook\s+up|"
    r"set\s+up|ensure|adjust|correct|patch|resolve|handle|support|improve|optimize"
)
_CHANGE_VERB_RE = re.compile(r"\b(?:" + _CHANGE_VERBS + r")\b", re.IGNORECASE)
# Bug/wrongness descriptions ("it incorrectly X", "doesn't work", "should X but Y", "is broken").
_WRONGNESS_RE = re.compile(
    r"\b(?:bug|broken|incorrect(?:ly)?|wrong(?:ly)?|fail(?:s|ing|ed)?|error|"
    r"does\s*n['’]?t\s+work|do\s+not\s+work|not\s+working|is\s*n['’]?t\s+working|"
    r"should\b.{0,60}\bbut\b|instead\s+of)\b",
    re.IGNORECASE,
)
# A purely interrogative opener: when the message is just a how/what/why question with no imperative
# change verb, it wants an explanation, not an edit. Used to avoid auto-editing on "how do I…?".
_INFO_QUESTION_RE = re.compile(
    r"^\s*(?:how|what|what['’]?s|why|which|who|when|where|explain|describe|tell\s+me|"
    r"can\s+you\s+explain|is\s+there|are\s+there|do\s+you|does\s+it|should\s+i)\b",
    re.IGNORECASE,
)


def _message_requests_change(message: Optional[str]) -> bool:
    """True iff the USER MESSAGE asks for a CHANGE to be made (code/files/data), not just info.

    Keyed off the STABLE user message rather than the (highly variable) answer text, because the
    cheap planner often misroutes an actionable request to "answer" and then only DESCRIBES the
    change. This is the reliable signal that the turn should have executed work. Conservative on
    pure questions: a how/what/why explanation request with no imperative change verb returns False
    so an informational ask is never auto-escalated into a file-editing run. Never raises.
    """
    if not message or not message.strip():
        return False
    try:
        m = message.strip()
        has_verb = bool(_CHANGE_VERB_RE.search(m))
        has_wrongness = bool(_WRONGNESS_RE.search(m))
        if not (has_verb or has_wrongness):
            return False
        # A leading interrogative with NO imperative change verb is an explanation request: don't
        # escalate (e.g. "how does the date logic work?"). A bug statement ("it incorrectly X") or
        # an imperative ("fix the date bug") is a change request even if it also reads as a report.
        if _INFO_QUESTION_RE.search(m) and not has_verb:
            return False
        return True
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


def _render_gathered(gathered: List[Dict[str, Any]]) -> str:
    if not gathered:
        return "[]"
    parts: List[str] = []
    for obs in gathered:
        kind = obs.get("kind")
        if kind == "grep":
            hits = obs.get("hits") or []
            head = [f"  {h.get('rel_path')}:{h.get('line_no')}: {h.get('line')}" for h in hits[:20]]
            more = "" if len(hits) <= 20 else f"\n  … (+{len(hits) - 20} more hits)"
            parts.append(
                f"GREP {obs.get('pattern')!r}"
                + (f" in {obs.get('scope')}" if obs.get("scope") else "")
                + f" → {len(hits)} hit(s):\n" + ("\n".join(head) if head else "  (none)") + more
            )
        elif kind in ("read", "query"):
            loc = obs.get("locator", "")
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

    kind = obs.get("kind")
    if kind == "grep":
        hits = obs.get("hits") or []
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


def _grounding_block(context_view: str, gathered: List[Dict[str, Any]], partial: bool) -> str:
    parts = ["--- GROUNDING CONTEXT (use this; do not fabricate beyond it) ---", context_view or "(none)"]
    if gathered:
        parts.append("\n--- ACTUAL CONTENT READ FOR THIS ANSWER ---")
        parts.append(_render_gathered(gathered))
    if partial:
        parts.append(
            "NOTE: this is a BEST-EFFORT answer assembled before fully exploring; if the content "
            "above doesn't cover the question, say plainly that you'd need to dig further."
        )
    parts.append(
        "Answer the user's latest message grounded in the context above. If it doesn't cover "
        "something, say so plainly rather than inventing details."
    )
    return "\n\n".join(parts)


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


class Orchestrator:
    """The domain-free brain. Construct with adapters; call ``run`` per request."""

    def __init__(
        self,
        *,
        retrieval: RetrievalAdapter,
        provider: ModelProvider,
        registry: ModelRegistry,
        deep_runner: Optional[DeepRunner] = None,
        escalation: Optional[EscalationSink] = None,
        config: Optional[OrchestratorConfig] = None,
        status: Optional[Callable[[str], None]] = None,
        vision_provider: Optional[ModelProvider] = None,
        vision_model: Optional[str] = None,
        context_assembler: Optional[ContextAssembler] = None,
        guidance: Optional[GuidanceProvider] = None,
    ):
        self.retrieval = retrieval
        self.provider = provider
        self.registry = registry
        self.deep_runner = deep_runner
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
                       guidance_selected_ids: Optional[set] = None) -> Optional[Observation]:
        if not isinstance(spec, dict):
            return None
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
                return self.retrieval.query(spec)
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

    def _do_reads(self, reads: List[Dict[str, Any]],
                  guidance_selected_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        specs = [s for s in (reads or [])[: self.cfg.max_reads_per_step] if isinstance(s, dict)]
        if not specs:
            return []
        if len(specs) == 1:
            obs = self._exec_one_read(specs[0], guidance_selected_ids)
            return [obs.to_dict()] if obs is not None else []
        workers = min(self.cfg.max_parallel, len(specs))
        results: List[Optional[Observation]] = [None] * len(specs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._exec_one_read, s, guidance_selected_ids): i
                       for i, s in enumerate(specs)}
            for fut in futures:
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Read operation failed: {type(e).__name__}: {e}", exc_info=True)
                    results[i] = Observation(kind="error", error=type(e).__name__)
        return [r.to_dict() for r in results if r is not None]

    # --- planner call --------------------------------------------------------

    def _plan(self, user_message: str, transcript: str, context_view: str,
              gathered: List[Dict[str, Any]], *, step: int = 0) -> PlanDecision:
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
        prompt = PLANNER_PROMPT.format(
            user_message=user_message,
            transcript=plan_transcript or "(no prior messages)",
            context_view=plan_context or "(no context)",
            gathered=_render_gathered_for_planner(
                gathered, self.cfg.planner_recent_full, self.cfg.planner_compress_over),
            max_reads=self.cfg.max_reads_per_step,
            max_subq=self.cfg.max_subquestions,
            max_deep=self.cfg.max_deep_subtasks,
        )
        model = self.registry.resolve_tier(self.cfg.planner_tier)
        provider = self.get_provider_for_model(model)
        raw = provider.plan(prompt, model=model, tool_schema=DECIDE_TOOL)
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
                         native_blocks: Optional[List[Dict[str, Any]]] = None) -> str:
        messages = []
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
        return provider.answer(messages, model=model)

    def _answer_subquestions(self, user_message: str, transcript: str, context_view: str,
                             gathered: List[Dict[str, Any]], model: str,
                             subquestions: List[str],
                             native_blocks: Optional[List[Dict[str, Any]]] = None) -> str:
        subs = [s for s in subquestions if s][: self.cfg.max_subquestions]
        if len(subs) < 2:
            return self._grounded_answer(user_message, transcript, context_view, gathered, model,
                                         False, native_blocks=native_blocks)
        ground = _grounding_block(context_view, gathered, False)

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
                return {"q": sub, "a": provider.answer(msgs, model=model)}
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
                                         False, native_blocks=native_blocks)
        if len(ok) == 1:
            return ok[0]["a"]
        merged = "\n\n".join(f"SUB-QUESTION: {a['q']}\nANSWER: {a['a']}" for a in ok)
        try:
            return self.provider.answer(
                [{"role": "user", "content": (
                    f"The user asked: {user_message}\n\nYou answered its independent parts below. "
                    "Merge them into ONE coherent, non-repetitive reply (don't mention the split):"
                    f"\n\n{merged}")}],
                model=model,
            )
        except Exception:  # noqa: BLE001
            return "\n\n".join(a["a"] for a in ok)

    # --- deep fan-out --------------------------------------------------------

    def _run_deep(self, plan: PlanDecision, user_message: str, model: str,
                  emit: Optional[_Emitter] = None,
                  rep_preamble: Optional[str] = None,
                  exec_record: Optional[ExecutionRecord] = None,
                  gathered: Optional[List[Dict[str, Any]]] = None) -> OrchestratorResult:
        subtasks = (plan.deep_subtasks or [])[: self.cfg.max_deep_subtasks]
        if not subtasks:
            subtasks = [{"goal": _truncate_goal(plan.goal or f"Fully address the request: {user_message}"),
                         "brief": plan.deep_brief or user_message}]
        if self.deep_runner is None:
            # No runner configured: surface the goal(s) without executing (caller may spawn).
            goals = [(st.get("goal") or "").strip() or user_message for st in subtasks]
            # Record each goal as REQUESTED-but-not-executed (neither success nor failure) so the
            # guard knows a re-run is the SAFE remediation here (nothing actually mutated).
            if exec_record is not None:
                for g in goals:
                    exec_record.facts.append(ExecutionFact(goal=g))
            return OrchestratorResult(kind="deep", goals=goals, rationale=plan.rationale,
                                      deep_results=[])

        # Pass the live emitter to the runner ONLY if its run_goal accepts an ``emit`` kwarg
        # (or **kwargs). Decided by signature inspection — never by a try/except TypeError, which
        # could re-invoke a runner that already ran a side effect (e.g. a data mutation).
        # We also TEE the emitter so EVENT_EXEC phase ticks are recorded into ``exec_record``
        # (per-subtask) for the broken-promise guard, while still streaming to the live sink.
        wants_emit = emit is not None and _run_goal_accepts_emit(self.deep_runner)
        # A per-task ``rep_preamble`` (e.g. an AI rep's pulled persona) and the brain's specific
        # ``gathered`` reads are both forwarded to the deep run as a combined ``context_preamble``,
        # ONLY to a runner whose ``run_goal`` accepts that kwarg (older signatures are untouched).
        # We check capability regardless of whether rep_preamble or gathered are set — either alone
        # is enough to build a useful context_preamble for the deep runner.
        wants_preamble = _run_goal_accepts_context_preamble(self.deep_runner)

        def run_one(st: Dict[str, Any]) -> DeepResult:
            goal = (st.get("goal") or "").strip() or f"Fully address: {user_message}"
            brief = (st.get("brief") or goal).strip()
            # Per-subtask execution fact — populated from EVENT_EXEC phase ticks (live) and finalized
            # from the DeepResult.met below. Recording per-subtask keeps facts correct even when
            # multiple subtasks run concurrently (each closure owns its own ``fact``).
            fact = ExecutionFact(goal=goal) if exec_record is not None else None

            def _emit_one(ev: ProgressEvent) -> None:
                # TEE: classify any EVENT_EXEC phase into the fact, then forward to the live sink.
                try:
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
                    emit.emit(ev)

            try:
                kwargs = dict(goal=goal, brief=brief, model=model,
                              max_turns=self.cfg.deep_max_turns)
                if wants_emit:
                    kwargs["emit"] = _emit_one
                if wants_preamble:
                    preamble_parts = []
                    if rep_preamble:
                        preamble_parts.append(rep_preamble)
                    if gathered:
                        preamble_parts.append(
                            "--- RELEVANT CONTENT FOUND BY THE BRAIN ---\n"
                            + _render_gathered(gathered)
                        )
                    if preamble_parts:
                        kwargs["context_preamble"] = "\n\n".join(preamble_parts)
                res = self.deep_runner.run_goal(**kwargs)
            except Exception as e:  # noqa: BLE001
                log.error(f"Deep runner failed: {type(e).__name__}: {e}", exc_info=True)
                res = DeepResult(met=False, error=type(e).__name__)
            # DeepResult.met is the AUTHORITATIVE outcome — a met run is a confirmed success; a
            # not-met run that actually executed is a confirmed failure. This is what makes a re-run
            # UNSAFE (a real attempt happened), so the guard will prefer honest correction.
            if fact is not None:
                if res.met:
                    fact.succeeded = True
                else:
                    fact.failed = True
                    fact.error = fact.error or res.error
                exec_record.facts.append(fact)
            if emit is not None and res.met:
                # A completed subtask is a real milestone — surfaces even in BACKGROUND.
                emit.emit(ProgressEvent(type=EVENT_MILESTONE, text=f"Completed: {goal}",
                                        data={"goal": goal}))
            return res

        if len(subtasks) == 1:
            res = run_one(subtasks[0])
            return OrchestratorResult(kind="deep", deep_results=[res],
                                      goals=[(subtasks[0].get("goal") or "").strip() or user_message],
                                      rationale=plan.rationale)
        workers = min(self.cfg.max_parallel, len(subtasks))
        results: List[Optional[DeepResult]] = [None] * len(subtasks)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(run_one, st): i for i, st in enumerate(subtasks)}
            for f in futs:
                results[futs[f]] = f.result()
        return OrchestratorResult(
            kind="deep",
            deep_results=[r for r in results if r is not None],
            goals=[(st.get("goal") or "").strip() or user_message for st in subtasks],
            rationale=plan.rationale,
        )

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

    # --- broken-promise guard (post-turn honesty check) ----------------------

    def _guard_turn(self, res: OrchestratorResult, exec_record: ExecutionRecord, *,
                    user_message: str, plan: Optional[PlanDecision],
                    model_hint: Optional[str], emit: Optional[_Emitter],
                    rep_preamble: Optional[str],
                    gathered: Optional[List[Dict[str, Any]]] = None) -> None:
        """AUTO-REMEDIATE THEN VERIFY (workstream 5). Mutates ``res`` in place when it corrects an
        overstated claim. Never raises (the caller also wraps it, belt-and-suspenders).

        Only ``answer`` results are guarded: a ``deep`` result already maps its truth via
        ``DeepResult.met`` (a not-met deep run reports failed/needs_you, never done), and ``confirm``
        makes no completion claim. The risk this addresses is an ANSWER that asserts it did/ will do
        something the turn did not actually execute.
        """
        if not self.cfg.verify_claims:
            return
        if res.kind != "answer":
            return
        reply = res.text or ""
        # CHEAP STRUCTURAL GATE: only engage when the reply asserts a completed/imminent action.
        # No claim signal -> pass through unchanged, ZERO model cost (the verification call is not made).
        if not text_claims_action(reply):
            return

        verify_model = self.registry.resolve_tier(self.cfg.planner_tier)
        if verify_supported(self.provider, verify_model, reply, exec_record):
            return  # claim MATCHES reality -> leave the reply and status exactly as-is.

        # --- MISMATCH: the reply overstates what happened. -------------------------------------
        # SAFE REMEDIATION (re-run) is allowed ONLY when NOTHING actually executed this turn:
        #   - any_success  -> the action already ran successfully; re-running risks a DOUBLE mutation.
        #   - any_failure  -> a real attempt was made; host actions aren't guaranteed idempotent, so a
        #                     blind re-run could still double-apply a partially-applied change.
        # So we re-run ONLY when an action was REQUESTED but produced NO recorded outcome (e.g. the
        # planner answered without ever executing, or no deep runner was wired). Otherwise we go
        # straight to honest correction. This is the core double-mutation safeguard.
        remediations = 0
        can_remediate = (
            self.deep_runner is not None
            and plan is not None
            and not exec_record.any_success
            and not exec_record.any_failure
        )
        while (can_remediate and remediations < max(0, self.cfg.max_remediations)
               and not exec_record.any_success):
            remediations += 1
            if emit is not None:
                emit.status("That did not go through. Retrying it now...")
            # Re-run the intended action. Reuse the planner's deep goal/brief; if the planner did not
            # author one (it had chosen "answer"), synthesize a concrete one from the request.
            remediate_plan = plan
            if not (plan.goal or plan.deep_brief or plan.deep_subtasks):
                remediate_plan = PlanDecision(
                    action="deep",
                    goal=f"Fully carry out the user's request: {user_message}",
                    deep_brief=user_message,
                    model_tier=plan.model_tier,
                    rationale="remediation: the prior turn claimed this action without executing it",
                )
            deep_model = self._answer_model(remediate_plan, "opus", hint=model_hint)
            redo = self._run_deep(remediate_plan, user_message, deep_model,
                                  emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                                  gathered=gathered)
            # Keep the original reply ONLY if the re-run met its goal AND the original claim is now
            # actually supported by what executed. Re-verifying (not just trusting ``met`` on a
            # possibly-vague synthesized goal) protects the honesty guarantee for specific claims.
            if (redo.deep_results and all(d.met for d in redo.deep_results)
                    and verify_supported(self.provider, verify_model, reply, exec_record)):
                if emit is not None:
                    emit.emit(ProgressEvent(type=EVENT_MILESTONE,
                                            text="Completed on retry.", data={"remediated": True}))
                return
            break  # re-run did not succeed -> fall through to honest correction.

        # STILL UNMET (or remediation was unsafe/not attempted): rewrite the reply to be HONEST and
        # flag the result so a background task maps to needs_you/failed instead of done.
        rewrite_model = self.registry.resolve_tier(model_hint or "sonnet")
        honest = honest_rewrite(self.provider, rewrite_model, reply, exec_record)
        res.text = honest
        res.partial = True
        res.claim_corrected = True
        if emit is not None:
            emit.emit(ProgressEvent(type=EVENT_MILESTONE,
                                    text="Corrected the reply to reflect what actually happened.",
                                    data={"claim_corrected": True}))

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

    def _run_clarify(self, plan: PlanDecision, *, quest_id: Optional[str] = None,
                     emit: Optional[_Emitter] = None) -> OrchestratorResult:
        """Surface user clarification/selection need as a decision request.

        Creates a decision-request with the question and options (if any) so the user can
        respond on the frontend or terminal. Returns with kind="confirm" to trigger UI.
        """
        clarif = plan.clarification or {}
        question = clarif.get("question", "Need your input to proceed").strip()
        options = clarif.get("options") or []
        allow_free = clarif.get("allow_free_input", False)

        # Format options for display
        if options:
            question_with_opts = f"{question}\n\nOptions:\n" + "\n".join(f"- {opt}" for opt in options)
            if allow_free:
                question_with_opts += "\n\n(You can also provide custom input)"
        else:
            question_with_opts = question

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
            rep_preamble: Optional[str] = None) -> OrchestratorResult:
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

        ``run`` still works with NO event args (back-compat: same signature callers used before
        plus keyword-only extras), returning the terminal ``OrchestratorResult``.
        """
        user_message = (user_message or "").strip()
        gathered: List[Dict[str, Any]] = []
        # Run-local durable EXECUTION FACTS (the broken-promise guard's evidence): each deep
        # subtask that actually executes records its outcome (success/failure) here, threaded like
        # ``gathered``. Attached to the OrchestratorResult in finish().
        exec_record = ExecutionRecord()
        started = time.monotonic()
        cfg = self.cfg

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
        _ack_future = None
        if cfg.instant_ack:
            try:
                emit.status("Looking into this...")
            except Exception:  # noqa: BLE001
                pass
            if self.provider is not None:
                try:
                    _ack_msg = user_message  # capture for closure
                    _ack_provider = self.provider
                    _ack_model = self.registry.resolve_tier(cfg.planner_tier)

                    def _do_ack() -> Optional[str]:
                        try:
                            _prompt = (
                                "Write ONE sentence (max 20 words) that restates the following "
                                "request in your own words and says you are looking into it. "
                                "Do NOT use em dashes (--). Be natural and brief.\n\n"
                                f"Request: {_ack_msg[:300]}"
                            )
                            return _ack_provider.answer(
                                [{"role": "user", "content": _prompt}],
                                model=_ack_model,
                            )
                        except Exception:  # noqa: BLE001
                            return None

                    _ack_executor = ThreadPoolExecutor(max_workers=1)
                    _ack_future = _ack_executor.submit(_do_ack)
                except Exception:  # noqa: BLE001
                    _ack_future = None

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
        _assembled = None
        _ctx_future = None
        _ctx_executor = None
        if self.context_assembler is not None:
            try:
                _ctx_assembler = self.context_assembler
                _ctx_msg = user_message

                def _do_assemble() -> Any:
                    return _ctx_assembler.assemble(_ctx_msg, meta=_ctx_meta or None)

                _ctx_executor = ThreadPoolExecutor(max_workers=1)
                _ctx_future = _ctx_executor.submit(_do_assemble)
                try:
                    emit.status("searching corpus…")
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
                context_view = (_guidance_view + "\n\n" + context_view if context_view
                                else _guidance_view)
                # Transparency: a STATUS tick naming the guidance applied this turn.
                try:
                    emit.status("Applied guidance: "
                                + ", ".join(c.title for c in _cards) + ".")
                except Exception:  # noqa: BLE001
                    pass

        # --- Instant-ack: emit the background ack text when it is ready -----------------------
        # At this point context assembly + the ack call have been running concurrently.  We
        # collect the ack result NOW (it should be ~done by the time we reach here) and emit it
        # as an EVENT_PARTIAL so the consumer streams it.  If not yet done we wait briefly; if
        # it failed the future result is None and we skip.  Joining here (before the planner
        # loop) ensures the background thread is cleaned up before run() returns.
        if _ack_future is not None:
            try:
                _ack_text = _ack_future.result(timeout=5.0)
                if _ack_text and _ack_text.strip():
                    emit.emit(ProgressEvent(type=EVENT_PARTIAL, text=_ack_text.strip(), data={"ack": True}))
            except Exception:  # noqa: BLE001 — timeout, cancelled, or provider error: skip
                pass
            finally:
                try:
                    _ack_executor.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass

        # --- ContextAssembler: collect the background assemble() result -----------------------
        # The assemble() call has been running concurrently with the ack + guidance.  Collect it
        # NOW with a short timeout so corpus search never blocks the interactive turn.  On timeout
        # or error we proceed with no assembled context (the reactive gather still runs in-loop).
        # The assembled cards go FIRST, then the caller's context, so cards apply even when the
        # caller provided its own grounding (panel docs / chat uploads append below).
        if _ctx_future is not None:
            try:
                _assembled = _ctx_future.result(timeout=3.0)
            except Exception as e:  # noqa: BLE001 — timeout, cancelled, or assembler error: skip
                log.warning(f"Context assembly failed or timed out: {e}")
                _assembled = None
            finally:
                try:
                    _ctx_executor.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
            if _assembled is not None:
                try:
                    if _assembled.context_view:
                        context_view = (_assembled.context_view + "\n\n" + context_view if context_view
                                        else _assembled.context_view)
                    if model_hint is None and _assembled.model_tier_hint:
                        model_hint = _assembled.model_tier_hint
                except Exception:  # noqa: BLE001
                    pass

        # --- CONTEXT EVENT: emit EVENT_CONTEXT showing which cards were selected -----------
        # Dedicated event for context assembly: card selection + sources. Surfaces in all modes.
        # Never raises.
        if _assembled is not None:
            try:
                _card_meta = getattr(_assembled, "card_metadata", None) or []
                _sources = getattr(_assembled, "sources", None) or []
                log.debug(f"Assembled context: {len(_card_meta)} cards, {len(_sources)} sources")
                if _card_meta or _sources:
                    # Build human-readable card summary for text field.
                    _card_titles = [c.get("title", c.get("id", "?"))[:50] for c in _card_meta]
                    _text = "Selected cards: " + ", ".join(_card_titles) + "." if _card_titles else ""
                    log.debug(f"Emitting EVENT_CONTEXT: {_text}")
                    emit.emit(ProgressEvent(
                        type=EVENT_CONTEXT,
                        text=_text,
                        data={
                            "card_metadata": _card_meta,
                            "sources": _sources,
                            "card_count": len(_card_meta),
                            "source_count": len(_sources),
                        }
                    ))
                else:
                    log.debug("No card metadata or sources to emit")
            except Exception as e:  # noqa: BLE001
                log.error(f"Failed to emit EVENT_CONTEXT: {e}", exc_info=True)

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
            # Collect token counts from the provider if it tracks them.
            try:
                if hasattr(self.provider, "tokens_in"):
                    res.tokens_in = self.provider.tokens_in
                    res.tokens_out = self.provider.tokens_out
            except Exception:  # noqa: BLE001
                pass
            # --- BROKEN-PROMISE GUARD (workstream 5): post-turn honesty check. ----------------
            # Verify a reply that CLAIMS a completed/imminent action against what actually executed;
            # auto-remediate (one safe re-run) then re-verify; else rewrite the reply to be honest
            # and flag the result partial. Never raises (degrades to leaving the turn unchanged).
            try:
                self._guard_turn(res, exec_record, user_message=user_message, plan=plan,
                                 model_hint=model_hint, emit=emit, rep_preamble=rep_preamble,
                                 gathered=gathered)
            except Exception:  # noqa: BLE001 — the guard must never break a turn
                pass
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
            if res.kind == "answer":
                emit.emit(ProgressEvent(type=EVENT_RESULT, text=res.text, result_kind="answer"))
            elif res.kind == "confirm":
                emit.emit(ProgressEvent(type=EVENT_DECISION, text=res.question,
                                        decision_id=res.decision_id, result_kind="confirm"))
            elif res.kind == "deep":
                out = "\n\n".join(d.output for d in res.deep_results if d.output) or None
                emit.emit(ProgressEvent(type=EVENT_RESULT, text=out, result_kind="deep"))
            emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=steps))
            return res

        plan: Optional[PlanDecision] = None
        steps = 0
        consecutive_reads = 0  # Track how many steps in a row chose "read"
        for step in range(cfg.max_steps):
            steps = step + 1
            emit.status("planning…" if step == 0 else "re-planning…")
            try:
                plan = self._plan(user_message, transcript, context_view, gathered, step=step)
            except Exception as e:  # noqa: BLE001 — planner failure -> grounded fallback answer
                log.exception(
                    f"Planner failed on step {steps}: {e}. Falling back to grounded answer."
                )
                plan = PlanDecision(action="answer", rationale="planner error → grounded answer")

            # Safety gate: if planner chose "read" for many consecutive steps, force a terminal action
            if plan and plan.action == "read":
                consecutive_reads += 1
                # Escalate to deep if: 5+ consecutive reads OR approaching max_steps while still reading
                if (consecutive_reads >= 5) or (steps > cfg.max_steps - 2 and consecutive_reads >= 2):
                    log.warning(
                        f"Planner stuck in read loop after {steps} steps / {consecutive_reads} reads. "
                        f"Force-escalating to deep with gathered context."
                    )
                    plan.action = "deep"
                    plan.goal = plan.goal or f"Complete the request: {user_message}"
                    plan.deep_brief = plan.deep_brief or user_message
            else:
                consecutive_reads = 0  # Reset when planner chooses something else
            emit.emit(ProgressEvent(type=(EVENT_PLAN if step == 0 else EVENT_REPLAN),
                                    action=plan.action, step=steps, text=plan.rationale or None))
            # Emit cumulative token counts so live consumers see usage grow in real time.
            _ti = getattr(self.provider, 'tokens_in', 0)
            _to = getattr(self.provider, 'tokens_out', 0)
            if _ti or _to:
                emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                        data={"tokens_in": _ti, "tokens_out": _to,
                                              "total": _ti + _to}))

            if plan.action == "read":
                if not plan.reads:
                    plan.action = "answer"
                else:
                    if any(r.get("list_sources") or r.get("describe_source")
                           or r.get("list_operations") or r.get("describe_operation")
                           or r.get("list_guidance") or r.get("read_guidance")
                           for r in plan.reads):
                        emit.status("exploring…")
                    else:
                        emit.status("searching…" if any(r.get("grep") for r in plan.reads) else "reading…")
                    new_obs = self._do_reads(plan.reads, guidance_selected_ids)
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

        # Cap/budget fallback: still in read mode -> best-effort answer or escalate to deep.
        if final not in ("answer", "deep", "confirm", "clarify"):
            if gathered:
                emit.status("wrapping up with a best-effort answer…")
                model = self._answer_model(plan, "balanced", hint=model_hint)
                text = self._grounded_answer(user_message, transcript, context_view, gathered, model,
                                             True, native_blocks=native_blocks)
                return finish(OrchestratorResult(kind="answer", text=text, rationale=plan.rationale,
                                                 partial=True, model=model))
            plan.action = final = "deep"
            plan.goal = _truncate_goal(plan.goal or f"Fully address the request: {user_message}")
            plan.deep_brief = plan.deep_brief or user_message

        if final == "clarify":
            # User clarification/selection needed: surface as decision-request
            res = self._run_clarify(plan, quest_id=quest_id, emit=emit)
            return finish(res)

        if final == "deep":
            emit.status("working on this now…")
            res = self._run_deep(plan, user_message, self._answer_model(plan, "opus", hint=model_hint),
                                 emit=emit, rep_preamble=rep_preamble, exec_record=exec_record,
                                 gathered=gathered)
            # Background: categorize edited files into context cards (deep runner returns edited_files in metadata)
            if res.deep_results and any(dr.met for dr in res.deep_results):
                self._update_context_cards_after_deep(res, context_meta)
            return finish(res)

        if final == "confirm":
            res = self._run_confirm(plan, quest_id=quest_id)
            return finish(res)

        # answer
        model = self._answer_model(plan, "sonnet", hint=model_hint)
        if len(plan.subquestions) >= 2:
            emit.status(f"answering {len(plan.subquestions)} parts in parallel…")
            text = self._answer_subquestions(user_message, transcript, context_view, gathered,
                                             model, plan.subquestions, native_blocks=native_blocks)
        else:
            emit.status("answering")
            text = self._grounded_answer(user_message, transcript, context_view, gathered, model,
                                         False, native_blocks=native_blocks)
        _ti = getattr(self.provider, 'tokens_in', 0)
        _to = getattr(self.provider, 'tokens_out', 0)
        if _ti or _to:
            emit.emit(ProgressEvent(type=EVENT_TOKENS,
                                    data={"tokens_in": _ti, "tokens_out": _to, "total": _ti + _to}))

        # If deferred_deep is set, also run the deep task after returning the answer
        # OR if planner explicitly flagged answer_contains_work_to_execute
        # OR auto-detect false claims (fallback for broken prompts)
        should_defer_deep = plan.deferred_deep
        if not should_defer_deep and self.deep_runner is not None:
            # Primary: trust planner's explicit flag
            if plan.answer_contains_work_to_execute:
                should_defer_deep = {"goal": f"Execute what the answer describes: {user_message}",
                                      "rationale": "planner indicated answer contains work to execute"}
                if emit is not None:
                    emit.status("executing described work now…")
            # Fallback: regex pattern matching for false claims (safety net for bad planner output)
            elif text_claims_action(text):
                should_defer_deep = {"goal": f"Execute what was claimed: {user_message}",
                                      "rationale": "auto-detected false claim in answer (fallback)"}
                if emit is not None:
                    emit.status("executing claimed work now…")
            # Fallback: the answer DESCRIBES executable work it never did ("I need to update X",
            # "to fix this I need to..."). The cheap planner frequently forgets to set
            # answer_contains_work_to_execute on code/file-change tasks, so without this net the
            # turn ends having only TALKED about the fix instead of doing it (the "it just finishes
            # the request" regression). Re-wired here so a described-but-unexecuted fix still
            # escalates to a deep run that actually applies it.
            elif _answer_describes_unexecuted_work(text):
                should_defer_deep = {"goal": f"Execute the work the answer describes: {user_message}",
                                      "rationale": "auto-detected unexecuted work in answer (fallback)"}
                if emit is not None:
                    emit.status("executing described work now…")
            # Decisive fallback, keyed off the STABLE USER MESSAGE (not the variable answer text):
            # the user asked for a CHANGE (fix/implement/"it incorrectly X"…), a deep runner is
            # available, yet the planner routed to "answer" and nothing executed this turn. The
            # earlier regex nets only match specific ANSWER phrasings, which a model like gemini
            # rarely produces verbatim, so an actionable request would silently end as a proposal.
            # Detecting intent from the message instead reliably catches that case. The brief carries
            # the assistant's proposed approach so the deep run APPLIES it rather than re-deriving.
            elif (_message_requests_change(user_message)
                  and (exec_record is None or not exec_record.any_mutation_attempted)):
                log.info("Escalating answer->deep: user message requests a change but the turn only "
                         "produced a proposal; running it now.")
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
                    emit.status("you asked for a change, making it now…")

        if should_defer_deep:
            try:
                if not plan.deferred_deep:
                    emit.status("executing follow-up work…")
                else:
                    emit.status("queuing follow-up work…")
                deferred_plan = PlanDecision(
                    action="deep",
                    goal=_truncate_goal(should_defer_deep.get("goal") or f"Execute: {user_message}"),
                    deep_brief=(should_defer_deep.get("brief") or user_message)[:2000],
                    rationale=should_defer_deep.get("rationale") or "follow-up work from answer phase",
                )
                deep_model = self._answer_model(deferred_plan, "opus", hint=model_hint)
                deep_res = self._run_deep(deferred_plan, user_message, deep_model,
                                         emit=emit, rep_preamble=rep_preamble,
                                         exec_record=exec_record, gathered=gathered)
                # Emit execution results as a separate milestone/message (not appended to answer)
                if deep_res and deep_res.deep_results:
                    deep_output = "\n\n".join(d.output for d in deep_res.deep_results if d.output)
                    if deep_output and emit is not None:
                        emit.emit(ProgressEvent(type=EVENT_MILESTONE,
                                                text=deep_output,
                                                data={"execution_results": True}))
            except Exception as e:  # noqa: BLE001 — deferred work must never break the answer
                log.warning(f"Deferred deep work failed: {type(e).__name__}: {e}", exc_info=True)

        return finish(OrchestratorResult(kind="answer", text=text, rationale=plan.rationale,
                                         model=model))

    # --- LIVE streaming convenience: a generator yielding events as they happen --------

    def run_stream(self, user_message: str, *, transcript: str = "", context_view: str = "",
                   quest_id: Optional[str] = None,
                   mode: Mode = Mode.LIVE,
                   model_hint: Optional[str] = None,
                   attachments: Optional[List[Dict[str, Any]]] = None,
                   rep_preamble: Optional[str] = None):
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
                               attachments=attachments, rep_preamble=rep_preamble)
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
