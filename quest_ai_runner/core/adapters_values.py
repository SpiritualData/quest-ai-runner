"""Value objects the brain passes around: events, observations, plan decisions, deep results, escalations, deadlines.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Value objects passed across the boundary (plain data, no behavior).
# ---------------------------------------------------------------------------

@dataclass
class ProgressEvent:
    """One thing that happened during a run, emitted by the orchestrator to a ProgressSink.

    The orchestrator emits these as it works; it does NOT decide what is shown — the sink
    (chosen by Mode) does. ``type`` is one of the ``EVENT_*`` constants above.
    """
    type: str
    text: Optional[str] = None             # human-facing message (status tick, reply, milestone)
    step: Optional[int] = None             # loop step index, when relevant
    action: Optional[str] = None           # the planner action, for plan/replan events
    decision_id: Optional[str] = None      # for decision events
    result_kind: Optional[str] = None      # for result/done events: "answer" | "deep" | "confirm"
    data: Dict[str, Any] = field(default_factory=dict)  # any extra structured payload

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type}
        for k in ("text", "step", "action", "decision_id", "result_kind"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.data:
            d["data"] = self.data
        return d


@dataclass
class Observation:
    """One result of a gather step — a read, a grep, or a structured query.

    ``kind`` is "read" | "grep" | "query" | "error". The brain renders these into the
    planner prompt, so they stay small and JSON-serializable.
    """
    kind: str
    rel_path: Optional[str] = None
    locator: Optional[str] = None         # e.g. "lines 10-40", "heading='Metrics'"
    text: Optional[str] = None            # for read/query
    pattern: Optional[str] = None         # for grep
    scope: Optional[str] = None
    hits: List[Dict[str, Any]] = field(default_factory=list)  # for grep
    error: Optional[str] = None
    # A remark for the PLANNER only, never answer content (e.g. a composite's "not answered by"
    # refusals beside another adapter's results). The run loop turns it into a planner_only note.
    planner_note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"kind": self.kind}
        for k in ("rel_path", "locator", "text", "pattern", "scope", "error", "planner_note"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.hits:
            d["hits"] = self.hits
        return d


@dataclass
class PlanDecision:
    """The planner's structured next-step decision (one iteration of the loop)."""
    action: str                                   # "read" | "answer" | "deep" | "confirm" | "clarify" | "tool"
    reads: List[Dict[str, Any]] = field(default_factory=list)
    goal: Optional[str] = None
    deep_brief: Optional[str] = None
    confirm_question: Optional[str] = None
    model_tier: Optional[str] = None              # "haiku" | "sonnet" | "opus" | None
    # The planner's rating of DEEP work, judged on the same planning call (zero extra LLM calls):
    # "simple" | "normal" | "hard" | None. Picks the deep run's STARTING model when nothing pinned
    # one (see core/deep_model_selection.py). None = not assessed; the ladder is then unchanged.
    deep_difficulty: Optional[str] = None
    deep_difficulty_reason: Optional[str] = None  # one short clause, logged with the choice
    # WHAT a "deep" (or deferred_deep) change lands on, judged on the same planning call (zero
    # extra LLM calls): "quest_data" | "code_or_files" | "other" | None. "quest_data" gates the
    # deep-runner ladder (see ladder_for_quest_data in core/orchestrator.py) so a quest-data
    # change never lands on a file-editing rung. None = not assessed; the ladder is unchanged.
    deep_target: Optional[str] = None
    subquestions: List[str] = field(default_factory=list)
    deep_subtasks: List[Dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    # Optional deferred deep work: when action="answer", can also queue a deep task to run after
    deferred_deep: Optional[Dict[str, Any]] = None  # {"goal": "...", "brief": "...", "rationale": "..."}
    # Explicit signal: this answer contains work that needs execution (instead of regex guessing)
    answer_contains_work_to_execute: bool = False  # Set True if answer describes work the AI should do
    # User clarification/input needed: when action="clarify"
    clarification: Optional[Dict[str, Any]] = None  # {"question": "...", "options": [...], "allow_free_input": bool}
    # EXPLICIT execution-mode signal the planner detected in the user's message (LLM judgment on
    # the planning call it already makes; never phrase matching). "enter_brainstorm" |
    # "exit_brainstorm" | None. Anything else the model returns is normalized to None (fail-safe:
    # a detection failure never changes the mode).
    mode_signal: Optional[str] = None
    # PER-IDEA THREADING (opt-in, OrchestratorConfig.card_thread_enabled): which TOPIC CARD this
    # message belongs to, judged on the SAME planning call (zero extra LLM calls; see
    # core/card_thread.py). Raw field, exactly one of "continue" | "switch_to:<card_id>" |
    # "new:<label>"; anything else is normalized to None and the turn CONTINUES the active card
    # (the standing fail-safe: an ambiguous topic assignment must never cost a turn). A topic
    # switch is NOT a mode signal and never touches the brainstorm latch.
    card_thread: Optional[str] = None
    # The escalation this confirm/clarify raises, all optional and all defaulting to the same
    # behavior as before they existed: a decision KIND (Quest's ``^[a-z0-9_:-]{1,64}$``, e.g.
    # "approve", "explicit:spend", "notice:prod-ops" -- validated/defaulted again at the Quest
    # boundary in ``QuestClient.create_decision``, so a hallucinated value here degrades rather
    # than losing the escalation); a deadline the decision should be resolved by, as the planner
    # wrote it (an ISO datetime, or a short relative form like "in 48h" -- parsed by
    # ``core.adapters.parse_deadline``, unparseable or absent means no deadline); and
    # "hold" | "proceed" for what happens if nobody answers by the deadline ("proceed" auto-
    # resolves the decision server-side; anything else, including no deadline at all, "hold"s).
    confirm_kind: Optional[str] = None
    confirm_deadline: Optional[str] = None
    # OPTIONAL http(s) link to the thing the human should look at to answer (see Escalation.review_url).
    confirm_review_url: Optional[str] = None
    confirm_default_on_silence: Optional[str] = None
    # action="tool": the direct tool calls to run in-process, in order, each
    # ``{"name": "<tool>", "args": {...}}`` (see core/tools.py). Only ever populated when the
    # orchestrator was given a tool registry; otherwise "tool" is not an action the planner has.
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    # HOW SURE the planner is of THIS decision, judged on the same planning call (zero extra LLM
    # calls): "high" | "medium" | "low" | None. Present only when the consumer opted into the
    # cascade (OrchestratorConfig.planner_cascade), because a field nobody reads is input tokens
    # on every call for nothing. It is a STRUCTURED self-report the planner fills in, which is why
    # it is an acceptable escalation signal where scanning the planner's own prose would not be
    # (see CLAUDE.md hard rule #3). None = not assessed, and the cascade then leaves the decision
    # alone rather than escalating everything.
    confidence: Optional[str] = None
    # WHAT THE USER'S CURRENT MESSAGE ASKS OF THE ASSISTANT, judged on the same planning call (zero
    # extra LLM calls): "act" (do, produce or change something now) | "ask" (a question, or asking
    # to be told something) | "inform" (news, context, or the user's own plan) | "hold_off" (do
    # not open work, answer here, stop or cancel runs). The orchestrator's escalation nets honor
    # this structured verdict instead of reading the message with a keyword list. None = not given
    # (the nets then fall back to the one-shot intent judge, never to a regex).
    user_intent: Optional[str] = None
    # THE READS THIS DECISION STILL OWES, declared by the planner on the call it already makes (zero
    # extra LLM calls): read specs (same shape as ``reads``) for sources it has located but not
    # opened, which the answer it is giving depends on. Empty = the answer is complete. When an
    # ``answer`` decision carries any, the loop runs them first instead of answering (the planner's
    # own structured declaration, never a reading of its prose; see the pending-read gate in
    # ``orchestrator_run_loop``).
    pending_read: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DeepResult:
    """The outcome of a goal-driven deep run."""
    met: bool                 # True = the run met the written goal cleanly
    output: str = ""
    error: Optional[str] = None
    decision_id: Optional[str] = None   # set if the run needed a human decision instead
    # ASYNC HAND-OFF marker. True = this runner did NOT execute the goal inline; it queued the real
    # run to finish out-of-band and report its outcome back later (e.g. a chat deep runner that
    # creates a tracked task and returns a "task #N launched" sentinel). When set, the goal loop
    # trusts ``met`` and STOPS — it must not re-verify the sentinel ``output`` against the goal
    # (that always fails) nor relaunch, which would spawn a fresh task every iteration. The real
    # result is verified when it reflects back, not here. Inline runners leave this False.
    deferred: bool = False
    # Resource usage the worker reported for THIS run, when available (0 / 0.0 if the runner does
    # not report it). The goal loop accumulates these to enforce an overall token budget instead of
    # a fixed attempt count.
    tokens: int = 0           # input + output tokens consumed by the worker for this run
    cost_usd: float = 0.0     # worker-reported cost for this run, when available
    # OUT-OF-BAND future context (bullets the async card updater learns from). This is the channel
    # that keeps future context away from the PRIMARY PAYLOAD: a runner whose output is strictly
    # formatted (generated code, JSON, a patch) declares
    # ``future_context_channel = FUTURE_CONTEXT_VIA_FIELD`` and returns its bullets HERE, so nothing
    # is ever appended to ``output``. A prose runner may leave this empty and end its ``output``
    # with the delimited section instead; the orchestrator parses that section into this field and
    # strips it from ``output`` at the single seam where a runner returns, so by the time any
    # consumer sees ``output`` it is the clean payload and this field is the only carrier. Either
    # way the card updater reads exactly one place: ``future_context``.
    future_context: str = ""
    # HOW this run ended, when the runner can tell. ``limit_hit`` means the worker ran out of its
    # TURN BUDGET mid-flight (Claude Code's ``error_max_turns``) rather than failing: its work up to
    # that point is real and its session still holds everything it learned. The goal loop reads this
    # to CONTINUE the same session with a bigger budget instead of re-running the goal from a cold
    # start, which is what made a long task burn its whole budget again on rediscovery and still
    # report a bare failure. A runner that cannot tell leaves it False, and behaves exactly as before.
    limit_hit: bool = False
    # The worker session this run used, when the runner has one (Claude Code's ``--session-id``).
    # It is what a continuation resumes, so the follow-up attempt picks up the run's own context.
    session_id: Optional[str] = None
    # The runner is OUT OF MOVES for this goal: it already tried what it can this turn (its own
    # attempt budget is spent, or a retry reproduced a result it had already returned), so running
    # it again would only repeat the same work. The goal loop stops on this instead of re-launching
    # the runner with an augmented brief; the runner's ``output`` should already say, in plain words,
    # what it tried. Structural, set by the runner from its own bookkeeping, never inferred from the
    # text of its output. A runner that cannot tell leaves it False, and behaves exactly as before.
    exhausted: bool = False
    # Claude Code refused this run because the subscription USAGE LIMIT is reached (see
    # ``core.usage_limit``). Not a failed goal: nothing ran, or the run stopped mid-flight, and the
    # work belongs back in the queue until the limit resets. The goal loop stops on it at once (every
    # rung is a Claude model, so another rung would be refused too) and the task executor requeues
    # the task instead of reporting it. ``session_id`` still names the session to continue.
    usage_limited: bool = False
    # The run was STOPPED ON REQUEST (the caller's ``cancel_check`` reported a cancel while the
    # worker was running). The worker process was interrupted and, if it ignored that, terminated;
    # ``session_id`` still names the session so a later reply can resume the work already done.
    # Structural, set by the runner from its own stop, never inferred from any text. The goal loop
    # stops on it at once: no verifier call, no retry.
    cancelled: bool = False
    # The run was PAUSED on request (the caller's control channel asked for a pause). The process
    # was interrupted and released; ``session_id`` is what a resume continues. Not a failure and
    # not a cancel: the work belongs back on the thread until a person resumes it.
    paused: bool = False
    # Messages the run CLAIMED from its control channel and wrote to the worker, but whose arrival
    # at the model was never confirmed before the run ended (a stop, a pause or a crash). They are
    # returned here, as text, so nothing a person sent is silently lost: the caller shows them or
    # sends them with the resume. Empty for a run that saw everything it was given.
    unseen_inputs: List[str] = field(default_factory=list)
    # The worker NEVER STARTED the goal: the binary is missing or not executable, or the CLI refused
    # before its first model turn (an unrecognized model id, bad credentials, a rejected flag), so
    # it consumed nothing and did nothing. Its ``output`` (if any) is the CLI's own error text, not
    # work product. The goal loop treats this as a hard error: it never sends that text to the
    # verifier (which would dress the launch failure up as "goal not met") and never escalates the
    # model on it (a stronger model was never the problem). Structural, set by the runner from the
    # process outcome and its reported usage, never inferred from the wording of the output.
    launch_failed: bool = False
    # The words in ``output`` were WRITTEN BY A MODEL answering in-process (a runner that declares
    # ``uses_deep_model = False``), not produced by a Claude Code worker that went and read things.
    # Set by the orchestrator from the runner it dispatched to, never by a runner. The link guard's
    # provenance rule reads it: such output is never evidence, and its links are judged like the
    # reply's own.
    model_written: bool = False
    # What a model-written run itself READ to produce ``output``, as plain text: the results of the
    # queries it executed, not anything it wrote. The only part of such a run that may vouch for an
    # address. A runner that cannot carry it leaves it empty.
    read_evidence: str = ""
    # The model this run actually executed with, when known. Set by the goal loop from the ladder
    # rung it dispatched to (``core/orchestrator.py``'s ``run_one``), not by the runner itself, so
    # every runner gets this for free; a runner that resolves its own model (or a future rung that
    # differs from what was requested) may overwrite it with the truer value.
    model: Optional[str] = None
    # The full model id the worker's CLI resolved ``model`` to (``sonnet`` -> ``claude-sonnet-...``),
    # read from the run's own session record. None when the runner cannot tell.
    resolved_model: Optional[str] = None
    # The orchestrator did NOT start this goal: the run was its OWN escalation (not something the
    # user or the planner asked for) and the runner the goal resolved to starts work that outlives
    # the turn (``DeepRunnerBase.starts_background_work``). Set by the orchestrator, never by a
    # runner; the escalation site then keeps the answer it already has.
    declined_background: bool = False
    # WHY a not-met run stopped, from the verifier's own verdict (one call, no extra LLM call):
    # ``verdict_reason`` / ``verdict_next_action`` are its last not-met reason and next step, so the
    # report can say what is left instead of only that the goal was not met. ``needs_person`` is the
    # specific question only a human can answer (the run stopped retrying because another attempt
    # could not help). ``unconfirmed_note`` is set on a run accepted as met whose only gap was proof
    # beyond what the request asked for: it names what was not independently confirmed.
    verdict_reason: str = ""
    verdict_next_action: str = ""
    needs_person: str = ""
    unconfirmed_note: str = ""
    # A tool, API or service the work needs failed in a way another attempt cannot change (the
    # verifier's ``tool_failure`` blocker), named in one plain sentence. The goal loop stops after
    # ``deep_tool_failure_attempts`` such verdicts instead of retrying and escalating the model.
    tool_failure: str = ""
    # The goal loop stopped because two attempts in a row fell short for the same reason, said the
    # same thing and changed nothing the runner can see (the verifier's reason, plain). Retrying,
    # or moving to a stronger model, would only repeat the attempt.
    stalled: str = ""
    # NOTHING CHANGED: the runner's own receipts of what it wrote show that this attempt changed
    # nothing in the system it acts on (no write completed, or every write matched nothing). It is
    # a structural fact the runner reports from its own bookkeeping, never inferred from the wording
    # of its output. Together with a not-met (or unverified) verdict it means the turn has no change
    # to report, so the goal loop must not let the result read as completed work; see
    # ``core/orchestrator.py``'s ``finish``. A runner that cannot tell leaves it False.
    changed_nothing: bool = False
    # THE OUTPUT ALREADY SAYS NOTHING CHANGED: the runner's own text carries a code-written sentence
    # stating that no change was made (set by the runner from its own bookkeeping, never inferred
    # from the wording). ``unconfirmed_no_change_text`` then adds no second lead line of its own, so
    # the person reads the no-change fact once. A runner that cannot tell leaves it False.
    states_no_change: bool = False
    # WHAT THIS RUN OBSERVED, one plain line each, built by the runner from its OWN records and
    # never from the wording of its output: a receipt for each change its write log proves landed,
    # and each source it really read back. This is the material the async card updater may draw a
    # new card fact from (see ``core/card_learning.py``); the run's reply text is not, because a
    # card is durable and a claim recorded as a fact grounds every later turn.
    observations: List[str] = field(default_factory=list)
    # THIS RUNNER RECORDS ITS OBSERVATIONS, so an EMPTY ``observations`` list means the run observed
    # nothing, rather than that the runner has no way to tell. Only with this set may the card
    # updater drop an edit for lack of support. A runner that cannot tell leaves it False and its
    # turns behave exactly as they did before.
    observations_reported: bool = False
    # AT LEAST ONE of ``observations`` above is a WRITE RECEIPT: a real change the runner's own
    # write log proves landed, not a read, a declined-write note, or an approval-parked marker.
    # Structural, set by the runner from its own write log, never inferred from the wording of its
    # output or of ``observations`` itself. This is what lets ``core/orchestrator.py``'s
    # ``result_landed_work`` tell a genuine PARTIAL write (the run is ``exhausted`` and not ``met``
    # because something else failed afterward, but part of the change already landed) apart from an
    # honest terminal ask that landed nothing (a clarifying question, a spent budget): both are
    # ``exhausted`` and not ``met``, and only the receipt flag tells them apart. A runner that
    # cannot tell leaves it False, which keeps today's "exhausted and not met -> not landed" rule
    # exactly as it was for every runner that does not set it.
    has_write_receipt: bool = False
    # THE METRIC RECORD OF THIS GOAL, written by the goal loop from its metric ledger (``core/goal_metrics.py``), never by a
    # runner: one dict per metric, ``MetricResult.to_dict()`` (solution_id, metric_id, name, milestone, before, after, delta,
    # source, reason, disagrees; see docs/metric-results.md). Empty means nothing to show. ``metric_missing`` is set when
    # the run changed something (by the runner's own records) and no metric result was recorded for it, not even a
    # not-applicable, after the one request made for it.
    metric_results: List[Dict[str, Any]] = field(default_factory=list)
    metric_missing: bool = False


# The two ways a deep runner can hand its future-context bullets back. Declared per runner as
# ``DeepRunner.future_context_channel``; the orchestrator picks the matching brief instruction and
# reads the result from the matching place. Future context is asked for on BOTH channels: a
# code-generating runner has the most valuable future context of all (what it changed, which ids and
# schema it touched), so it is never opted out, only routed away from its payload.
FUTURE_CONTEXT_VIA_OUTPUT = "output"   # DEFAULT: worker ends its prose output with the delimiter


FUTURE_CONTEXT_VIA_FIELD = "field"     # strict-format runner: worker fills DeepResult.future_context


# What a deep runner's WRITE SURFACE is: what kind of change it is capable of making. Declared per
# runner as ``DeepRunner.write_surface`` (default ``WRITE_SURFACE_AGENT`` on ``DeepRunnerBase``, so
# an existing third-party runner behaves exactly as today). This is what lets the orchestrator tell
# a rung that can only edit files apart from one that changes data through a governed operation --
# a quest-data change must never be satisfied by a file-editing rung standing in for one.
WRITE_SURFACE_FILES = "files"            # the rung can only change files on disk


WRITE_SURFACE_OPERATIONS = "operations"  # the rung changes data through named, governed operations


WRITE_SURFACE_AGENT = "agent"            # a full agent: can do either, including writing code


@dataclass
class Escalation:
    """A human-only step the brain refuses to take autonomously."""
    summary: str
    kind: str = "approve"
    quest_id: Optional[str] = None
    assignee: Optional[str] = None              # consumer-defined routing key
    default_on_silence: str = "hold"
    # WHEN this decision should be resolved by, an aware datetime or None for no deadline. A
    # sink that supports deadlines (``QuestDecisionSink``) applies its own configured default
    # (``QAR_DECISION_DEFAULT_DEADLINE_HOURS``) when this is left None -- callers that never
    # think about deadlines still get one if the deployment wants that; an explicit value here
    # always wins.
    deadline: Optional[datetime] = None
    # OPTIONAL http(s) page the human should open to decide: the draft, preview, doc or PR the ask
    # is about. Shown as a prominent link on the ask in Quest's daily reflection and Asks for you.
    review_url: Optional[str] = None


# A short relative form the planner can emit instead of computing a real timestamp itself:
# "in 48h", "in 2 days", "in 30m". Kept intentionally small (hours/days/minutes) -- a deadline
# on a human decision is never usefully more precise than that.
RELATIVE_DEADLINE_RE = re.compile(
    r"^in\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|days?|d|minutes?|mins?|m)$",
    re.IGNORECASE,
)


def parse_deadline(raw: Optional[str], *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a planner-supplied deadline string into an aware UTC datetime, or None.

    Accepts either a full ISO datetime (``2026-09-25T18:00:00Z`` or with a numeric UTC offset) or
    the short relative form above. Anything else -- missing, empty, unparseable -- returns None,
    exactly as if no deadline had been given: a malformed deadline must degrade to "no deadline",
    never raise into the escalation path.
    """
    text = (raw or "").strip()
    if not text:
        return None
    m = RELATIVE_DEADLINE_RE.match(text)
    if m:
        amount = float(m.group(1))
        unit = m.group(2).lower()
        base = now if now is not None else datetime.now(timezone.utc)
        if unit.startswith("h"):
            return base + timedelta(hours=amount)
        if unit.startswith("d"):
            return base + timedelta(days=amount)
        return base + timedelta(minutes=amount)
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
