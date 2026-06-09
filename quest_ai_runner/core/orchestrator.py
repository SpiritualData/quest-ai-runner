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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .adapters import (
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_MILESTONE,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    DeepResult,
    DeepRunner,
    Escalation,
    EscalationSink,
    Mode,
    ModelProvider,
    Observation,
    PlanDecision,
    ProgressEvent,
    ProgressSink,
    RetrievalAdapter,
)
from .model_registry import TIERS, ModelRegistry

# Defaults (all overridable via OrchestratorConfig).
DEFAULT_MAX_STEPS = 5
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
# ===========================================================================
PLANNER_PROMPT = """You are the PLANNER for an AI assistant answering a request.

Your job: decide, FAST, the NEXT step to respond WELL. You do NOT write the reply yourself.
Choose exactly one action via the `decide` tool. You run in a LOOP: after a "read" you'll be
called again with what was read, so you can narrow in — grep to locate, read the matching
section, then answer — exactly like a careful human reading the real source.

CORE PRINCIPLE — READ REAL CONTENT BEFORE ANSWERING:
  The CONTEXT below only LOCATES what exists (a one-line summary per item). It is NOT a
  substitute for reading the actual content. For ANY question about substance — what a doc says,
  status, numbers, decisions, how something works — READ the real content first (action "read"),
  THEN answer grounded in it. Only pure chit-chat/meta ("you there?", "thanks") may be answered
  WITHOUT reading.

The four actions:
  - "read": TARGETED, PARTIAL reads to gather what you need. In `reads`, list one or more of:
      * a section: {{"rel_path": "...", "heading": "Metrics"}} OR
                   {{"rel_path": "...", "start_line": 40, "end_line": 80}}, and/or
      * a grep:    {{"grep": "regex", "scope": "optional/subpath"}} to LOCATE content, and/or
      * a query:   {{"query": {{...}}}} for a structured source lookup (if supported), and/or
      * DISCOVERY, when you do not yet know what the source of truth contains:
          {{"list_sources": true}}                       → the collections/tables/doc-sets that exist
          {{"describe_source": "<name>", "describe_path": "<optional nested path>"}}
                                                          → the fields/types of ONE source (drill down)
          {{"list_operations": true}}                    → the operations you can call (reads AND changes)
          {{"describe_operation": "<name>"}}             → the full signature/usage of ONE operation
    DISCOVER BEFORE YOU GUESS: if you don't already know the exact source, field, or operation a
    request needs, list_sources / list_operations first (then describe_* the few you'll use), rather
    than inventing a shape. Discovery is the cheapest, most reliable way to honor what the user
    literally asked for. It does NOT favor any particular source or operation — it just shows what
    exists. BATCH AGGRESSIVELY: reads in ONE step run IN PARALLEL — list ALL you'd plausibly want now
    (up to {max_reads}), including several describe_* calls at once. After the read you'll be
    re-invoked with the results in GATHERED.
  - "answer": you have ENOUGH real content in GATHERED — or it's chit-chat needing no reading.
    Use "answer" ONLY to INFORM (explain, summarize, advise). If the user asked you to CHANGE
    something (create/add/update/edit/delete/mark/set/rename their data or artifacts), that is an
    ACTION — do NOT just describe the change in an answer; choose "deep" so the change is actually
    proposed/made. Describing a mutation in prose instead of doing it is a FAILURE.
  - "deep": this needs REAL WORK — and crucially, ANY request to CREATE / ADD / UPDATE / EDIT /
    DELETE / MARK / SET the user's data or artifacts (e.g. "add a goal", "add a measurable
    outcome", "make this goal more ambitious", "update my X", "create a strategy") is "deep", even
    when it sounds simple and even when phrased loosely ("add a measurable outcome ABOUT …"): the
    mutation must be PROPOSED/EXECUTED, never merely talked about. Provide BOTH `goal` (a CONCRETE,
    CHECKABLE done-standard, as a human would write it) and `deep_brief` (a clear self-contained
    brief that PRESERVES the user's action verb — say "add/update …", not "look up/review …"). BE A
    GROUNDED FIRST RESPONDER: if the request is actionable but UNDER-SPECIFIED (e.g. "add a goal"
    with no details), do NOT bounce it back as a question — GROUND in the CONTEXT/GATHERED above and
    author a concrete, specific `goal` + `deep_brief` yourself (a reasonable proposal the human can
    review and edit). A mutating proposal is surfaced for review BEFORE it takes effect, so
    proposing is safe and is strongly preferred over asking for more information OR describing it.
    GROUND THE CHANGE IN A REAL OPERATION: if you are not already certain which source/operation
    the change targets, do a "read" discovery step FIRST (list_operations / list_sources, then
    describe the relevant one) so your proposal uses the actual operation the user named, not a
    guessed shape. Match what the user literally asked for; do not substitute a different artifact
    because it's easier to write.
  - "confirm": reserved for a genuine FORK you cannot ground past — the request is truly ambiguous
    (you cannot form a reasonable proposal even after reading), OR risky/irreversible enough that a
    human must approve the DIRECTION first. Prefer "deep" with a concrete proposal whenever the
    context lets you make a sensible one; choose "confirm" only when it genuinely doesn't. Put the
    question in `confirm_question`. Do NOT also act.

MODEL TIER (`model_tier`): always set one of "haiku" | "sonnet" | "opus" — governs the model
  that GENERATES the answer / deep run (the planner itself always runs cheap). haiku=triage/
  trivial, sonnet=most answers (default), opus=hard reasoning / deep work.

PARALLEL SUB-QUESTIONS (optional): if the message has INDEPENDENT parts, set `subquestions` to
  2–{max_subq} short self-contained sub-questions (answered CONCURRENTLY, then synthesized).

DEEP FAN-OUT (optional, for "deep"): if the work splits into INDEPENDENT subtasks, set
  `deep_subtasks` to 2–{max_deep} of {{"goal": "...", "brief": "..."}} — each a concurrent run.

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

# The structured decision schema the planner MUST return (forced tool use).
DECIDE_TOOL: Dict[str, Any] = {
    "name": "decide",
    "description": "Record the chosen NEXT step and its parameters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "answer", "deep", "confirm"]},
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


# ---------------------------------------------------------------------------
# Decision normalization (coerce a raw planner dict into a safe PlanDecision).
# ---------------------------------------------------------------------------

def normalize_decision(raw: Dict[str, Any], cfg: OrchestratorConfig) -> PlanDecision:
    action = (raw.get("action") or "answer").strip().lower()
    if action not in ("read", "answer", "deep", "confirm"):
        action = "answer"

    reads_in = raw.get("reads") or []
    clean_reads: List[Dict[str, Any]] = []
    if isinstance(reads_in, list):
        for r in reads_in[: cfg.max_reads_per_step]:
            if isinstance(r, dict) and (
                r.get("grep") or r.get("rel_path") or r.get("query")
                or r.get("list_sources") or r.get("describe_source")
                or r.get("list_operations") or r.get("describe_operation")
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
    )


# ---------------------------------------------------------------------------
# Rendering helpers (gathered observations -> prompt text / grounding block).
# ---------------------------------------------------------------------------

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
    ):
        self.retrieval = retrieval
        self.provider = provider
        self.registry = registry
        self.deep_runner = deep_runner
        self.escalation = escalation
        self.cfg = config or OrchestratorConfig()
        self._status = status or (lambda _msg: None)

    # --- gather (parallel reads/greps/queries via the RetrievalAdapter) ------

    def _exec_one_read(self, spec: Dict[str, Any]) -> Optional[Observation]:
        if not isinstance(spec, dict):
            return None
        try:
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
                return self.retrieval.query(spec["query"])
            if spec.get("rel_path"):
                return self.retrieval.read_section(
                    str(spec["rel_path"]),
                    start_line=spec.get("start_line"),
                    end_line=spec.get("end_line"),
                    heading=spec.get("heading"),
                    max_bytes=self.cfg.max_gather_chars,
                )
        except Exception as e:  # noqa: BLE001 — a bad spec must never break the loop
            return Observation(kind="error", error=type(e).__name__)
        return None

    def _do_reads(self, reads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        specs = [s for s in (reads or [])[: self.cfg.max_reads_per_step] if isinstance(s, dict)]
        if not specs:
            return []
        if len(specs) == 1:
            obs = self._exec_one_read(specs[0])
            return [obs.to_dict()] if obs is not None else []
        workers = min(self.cfg.max_parallel, len(specs))
        results: List[Optional[Observation]] = [None] * len(specs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._exec_one_read, s): i for i, s in enumerate(specs)}
            for fut in futures:
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # noqa: BLE001
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
        raw = self.provider.plan(prompt, model=model, tool_schema=DECIDE_TOOL)
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
                         gathered: List[Dict[str, Any]], model: str, partial: bool) -> str:
        messages = []
        if transcript:
            messages.append({"role": "user", "content": transcript})
        messages.append({"role": "user", "content": _grounding_block(context_view, gathered, partial)})
        messages.append({"role": "user", "content": user_message})
        return self.provider.answer(messages, model=model)

    def _answer_subquestions(self, user_message: str, transcript: str, context_view: str,
                             gathered: List[Dict[str, Any]], model: str,
                             subquestions: List[str]) -> str:
        subs = [s for s in subquestions if s][: self.cfg.max_subquestions]
        if len(subs) < 2:
            return self._grounded_answer(user_message, transcript, context_view, gathered, model, False)
        ground = _grounding_block(context_view, gathered, False)

        def answer_one(sub: str) -> Optional[Dict[str, str]]:
            try:
                msgs = [
                    {"role": "user", "content": ground},
                    {"role": "user", "content": f"Focus ONLY on this sub-question, grounded in the "
                                                 f"context above:\n\n{sub}"},
                ]
                return {"q": sub, "a": self.provider.answer(msgs, model=model)}
            except Exception:  # noqa: BLE001
                return None

        workers = min(self.cfg.max_parallel, len(subs))
        out: List[Optional[Dict[str, str]]] = [None] * len(subs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(answer_one, s): i for i, s in enumerate(subs)}
            for f in futs:
                try:
                    out[futs[f]] = f.result()
                except Exception:  # noqa: BLE001
                    out[futs[f]] = None
        ok = [a for a in out if a and (a.get("a") or "").strip()]
        if not ok:
            return self._grounded_answer(user_message, transcript, context_view, gathered, model, False)
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
                  emit: Optional[_Emitter] = None) -> OrchestratorResult:
        subtasks = (plan.deep_subtasks or [])[: self.cfg.max_deep_subtasks]
        if not subtasks:
            subtasks = [{"goal": plan.goal or f"Fully address the request: {user_message[:200]}",
                         "brief": plan.deep_brief or user_message}]
        if self.deep_runner is None:
            # No runner configured: surface the goal(s) without executing (caller may spawn).
            goals = [(st.get("goal") or "").strip() or user_message for st in subtasks]
            return OrchestratorResult(kind="deep", goals=goals, rationale=plan.rationale,
                                      deep_results=[])

        # Pass the live emitter to the runner ONLY if its run_goal accepts an ``emit`` kwarg
        # (or **kwargs). Decided by signature inspection — never by a try/except TypeError, which
        # could re-invoke a runner that already ran a side effect (e.g. a data mutation).
        wants_emit = emit is not None and _run_goal_accepts_emit(self.deep_runner)
        emit_fn = (lambda ev: emit.emit(ev)) if wants_emit else None

        def run_one(st: Dict[str, Any]) -> DeepResult:
            goal = (st.get("goal") or "").strip() or f"Fully address: {user_message[:200]}"
            brief = (st.get("brief") or goal).strip()
            try:
                kwargs = dict(goal=goal, brief=brief, model=model,
                              max_turns=self.cfg.deep_max_turns)
                if emit_fn is not None:
                    kwargs["emit"] = emit_fn
                res = self.deep_runner.run_goal(**kwargs)
            except Exception as e:  # noqa: BLE001
                res = DeepResult(met=False, error=type(e).__name__)
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

    # --- confirm -------------------------------------------------------------

    def _run_confirm(self, plan: PlanDecision, *, quest_id: Optional[str]) -> OrchestratorResult:
        question = (plan.confirm_question
                    or "Before I act on this — can you confirm you'd like me to go ahead?").strip()
        decision_id = None
        if self.escalation is not None:
            try:
                decision_id = self.escalation.escalate(Escalation(
                    summary=question, kind="approve", quest_id=quest_id, default_on_silence="hold"))
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
            model_hint: Optional[str] = None) -> OrchestratorResult:
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

        ``run`` still works with NO event args (back-compat: same signature callers used before
        plus keyword-only extras), returning the terminal ``OrchestratorResult``.
        """
        user_message = (user_message or "").strip()
        gathered: List[Dict[str, Any]] = []
        started = time.monotonic()
        cfg = self.cfg

        # If a handoff is configured, route events through a FanoutSink that flips live->bg on detach.
        on_detach = None
        active_sink = sink
        if background_sink is not None and detach_check is not None:
            from .adapters import FanoutSink
            fan = FanoutSink(live=sink, background=background_sink) if sink is not None else \
                FanoutSink(live=background_sink, background=background_sink)
            active_sink = fan
            on_detach = fan.detach
        emit = _Emitter(active_sink, mode, self._status, detach_check=detach_check, on_detach=on_detach)

        def budget_exhausted() -> bool:
            size = sum(len(o.get("text", "")) + len(str(o.get("hits", ""))) for o in gathered)
            return (time.monotonic() - started) > cfg.max_elapsed_seconds or size > cfg.max_gathered_chars

        def finish(res: OrchestratorResult) -> OrchestratorResult:
            res.steps = steps
            res.gathered = gathered
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
        for step in range(cfg.max_steps):
            steps = step + 1
            emit.status("planning…" if step == 0 else "re-planning…")
            try:
                plan = self._plan(user_message, transcript, context_view, gathered, step=step)
            except Exception:  # noqa: BLE001 — planner failure -> grounded fallback answer
                plan = PlanDecision(action="answer", rationale="planner error → grounded answer")
            emit.emit(ProgressEvent(type=(EVENT_PLAN if step == 0 else EVENT_REPLAN),
                                    action=plan.action, step=steps, text=plan.rationale or None))

            if plan.action == "read":
                if not plan.reads:
                    plan.action = "answer"
                else:
                    if any(r.get("list_sources") or r.get("describe_source")
                           or r.get("list_operations") or r.get("describe_operation")
                           for r in plan.reads):
                        emit.status("exploring…")
                    else:
                        emit.status("searching…" if any(r.get("grep") for r in plan.reads) else "reading…")
                    gathered.extend(self._do_reads(plan.reads))
                    emit.emit(ProgressEvent(type=EVENT_READ, step=steps,
                                            data={"reads": len(plan.reads)}))
                    if budget_exhausted():
                        break
                    continue
            if plan.action in ("answer", "deep", "confirm"):
                break
        else:
            plan = plan or PlanDecision(action="answer")

        final = (plan or PlanDecision(action="answer")).action

        # Cap/budget fallback: still in read mode -> best-effort answer or escalate to deep.
        if final not in ("answer", "deep", "confirm"):
            if gathered:
                emit.status("wrapping up with a best-effort answer…")
                model = self._answer_model(plan, "sonnet", hint=model_hint)
                text = self._grounded_answer(user_message, transcript, context_view, gathered, model, True)
                return finish(OrchestratorResult(kind="answer", text=text, rationale=plan.rationale,
                                                 partial=True))
            plan.action = final = "deep"
            plan.goal = plan.goal or f"Fully address the request: {user_message[:200]}"
            plan.deep_brief = plan.deep_brief or user_message

        if final == "deep":
            emit.status("working on this now…")
            res = self._run_deep(plan, user_message, self._answer_model(plan, "opus", hint=model_hint),
                                 emit=emit)
            return finish(res)

        if final == "confirm":
            res = self._run_confirm(plan, quest_id=quest_id)
            return finish(res)

        # answer
        model = self._answer_model(plan, "sonnet", hint=model_hint)
        if len(plan.subquestions) >= 2:
            emit.status(f"answering {len(plan.subquestions)} parts in parallel…")
            text = self._answer_subquestions(user_message, transcript, context_view, gathered,
                                             model, plan.subquestions)
        else:
            emit.status("answering")
            text = self._grounded_answer(user_message, transcript, context_view, gathered, model, False)
        return finish(OrchestratorResult(kind="answer", text=text, rationale=plan.rationale))

    # --- LIVE streaming convenience: a generator yielding events as they happen --------

    def run_stream(self, user_message: str, *, transcript: str = "", context_view: str = "",
                   quest_id: Optional[str] = None,
                   mode: Mode = Mode.LIVE,
                   model_hint: Optional[str] = None):
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
                               quest_id=quest_id, mode=mode, sink=sink, model_hint=model_hint)
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
