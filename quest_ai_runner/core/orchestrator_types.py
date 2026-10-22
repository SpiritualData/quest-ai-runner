"""``OrchestratorConfig`` (every knob of the loop) and ``OrchestratorResult`` (what a run returns).

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from .deep_model_selection import DEFAULT_DIFFICULTY_MODELS
from .planner_prompt import DEFAULT_PLANNER_PROMPT_PROFILE
from .adapters import DeepResult
from .guard import ExecutionRecord
from .orchestrator_constants import (
    AUTO_FOLLOWUP_MAX_RUNS,
    AUTO_FOLLOWUP_TOKEN_BUDGET,
    DEFAULT_CARD_MERGE_SIMILARITY,
    DEFAULT_DEEP_MAX_TURNS,
    DEFAULT_MAX_CONSECUTIVE_READS,
    DEFAULT_MAX_DEEP_SUBTASKS,
    DEFAULT_MAX_ELAPSED_SECONDS,
    DEFAULT_MAX_GATHERED_CHARS,
    DEFAULT_MAX_GATHER_CHARS,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_MAX_READS_PER_STEP,
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_SUBQUESTIONS,
    DEFAULT_PLANNER_ABBREVIATE_REPEAT_CONTEXT,
    DEFAULT_PLANNER_COMPRESS_OVER,
    DEFAULT_PLANNER_CONTEXT_MAX_CHARS,
    DEFAULT_PLANNER_GATHERED_MAX_CHARS,
    DEFAULT_PLANNER_TRANSCRIPT_MAX_CHARS,
    DEFAULT_PLANNER_RECENT_FULL,
)


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
    # Attempts a deep goal gets once the verifier reports ``tool_failure`` (a tool or service the
    # work needs is failing in a way a retry cannot fix). One retry covers a transient blip; more
    # only re-run a model, a stronger one each time, against the same broken tool.
    deep_tool_failure_attempts: int = 2
    # Overall TOKEN BUDGET for one turn's deep goal loop (sum of the worker's reported tokens across
    # attempts). The loop keeps iterating + escalating the model WHILE under budget; None disables it
    # (then only the attempt cap applies). The consumer sets this from an env var so it is operator-
    # tunable. Default allows a few full deep attempts.
    deep_goal_token_budget: Optional[int] = 300_000
    # AUTOMATIC FOLLOW-UP RUNS for a queued task (``runner/auto_followup.py``): when a task's deep
    # run ends with the original ask clearly unfinished and nothing is waiting on a person, the task
    # executor starts up to this many further deep runs for the remaining items. 0 turns it off.
    auto_followup_max_runs: int = AUTO_FOLLOWUP_MAX_RUNS
    # Token budget over the whole chain of runs (the first run plus its follow-ups); None disables
    # the guard (then only the run cap applies).
    auto_followup_token_budget: Optional[int] = AUTO_FOLLOWUP_TOKEN_BUDGET
    # The deep-worker MODEL LADDER: models tried in order, escalating to a STRONGER one when a goal
    # is not met (the failure may be a model-capability gap). The deep worker is Claude Code, so
    # these are Claude models/aliases (fast -> strong, e.g. ["haiku","sonnet","opus"]). None = use the
    # single model the orchestrator was given (back-compat). The consumer sets it (e.g. from
    # QAR_DEEP_MODELS); an explicit per-task model request or a guidance "model preference" pins the
    # model and disables auto-escalation.
    deep_model_ladder: Optional[List[str]] = None
    # AUTOMATIC STARTING MODEL for a deep run nobody pinned (core/deep_model_selection.py). The
    # planner rates the work "simple" | "normal" | "hard" on the call it already makes, and the run
    # STARTS on that difficulty's model, then escalates up the ladder (``deep_model_ladder``, or
    # ``DEFAULT_AUTO_DEEP_LADDER`` = haiku, sonnet when none is configured) on a not-met goal
    # exactly as before. Defaults: simple -> haiku, normal and hard -> sonnet (the strongest model
    # is the escalation rung, not a starting point). Consumers set these from QAR_DEEP_AUTO_MODEL
    # and QAR_DEEP_MODEL_SIMPLE / _NORMAL / _HARD. False = the ladder is used exactly as before.
    deep_auto_model: bool = True
    deep_difficulty_models: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_DIFFICULTY_MODELS))
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
    # Character caps on what the planner prompt carries (0 = uncapped). Transcript and gathered keep
    # the NEWEST text, context keeps its head; each cut leaves a "[truncated N chars]" marker.
    planner_transcript_max_chars: int = DEFAULT_PLANNER_TRANSCRIPT_MAX_CHARS
    planner_context_max_chars: int = DEFAULT_PLANNER_CONTEXT_MAX_CHARS
    planner_gathered_max_chars: int = DEFAULT_PLANNER_GATHERED_MAX_CHARS
    # On re-plan steps (step > 1), replace the unchanged transcript + static context_view with a
    # short reference note (they were sent in full on step 1). Default off → unchanged behavior.
    # The final ANSWER path is never affected — it always grounds on the full transcript/context.
    planner_abbreviate_repeat_context: bool = DEFAULT_PLANNER_ABBREVIATE_REPEAT_CONTEXT
    # WHICH PLANNER PROMPT this run sends: "full" (the default, unchanged) or "compact", the same
    # doctrine with the parts a planner does not use removed. See PLANNER_PROFILES. The reason to
    # pick "compact" is cost and small-model reliability: a planner call is almost all input
    # tokens, and the compact profile cuts them by about 40 percent, which matters when the point
    # of the deployment is to run routing on a cheap model.
    planner_prompt_profile: str = DEFAULT_PLANNER_PROMPT_PROFILE
    # THE ROUTING DECISION'S OWN MODEL. Empty (the default) resolves ``planner_tier`` as before.
    # A model id here is used verbatim for the decide call ONLY, so a deployment can put the
    # routing decision on a different model from everything else that shares ``planner_tier``
    # (card updates, request understanding, summaries, fallbacks). Routing is mostly input
    # tokens, so it is the call most worth moving to a cheap model, and it is the only one that
    # was measured there (docs/cheap-model-routing.md).
    planner_model: str = ""
    # THE OVERSEER CASCADE for routing decisions. Off by default. When on, the planner also
    # reports how sure it is of each decision (one extra enum field on the call it already makes,
    # zero extra calls), and a decision it is NOT sure of is re-decided by a STRONGER model that
    # sees only a short digest: the request, the cheap model's choice and reasoning, and the
    # grounding it needs, rather than the whole planner prompt again. The point is to buy the
    # strong model's judgment on the few percent of decisions that need it without paying its
    # input cost on all of them. See core/planner_cascade.py.
    planner_cascade: bool = False
    planner_cascade_tier: str = "balanced"
    # WHICH DECISIONS get reviewed. A comma list of confidence levels ("low", "low,medium") and
    # of actions written "action:read". The default is the ACTION signal, not confidence, because
    # self-reported confidence was measured and did not work: a cheap planner answered "high" on
    # 84 of 86 decisions including 36 wrong ones (see core/planner_cascade.py). A decision with no
    # confidence reported is NEVER escalated on the confidence signal, so a parse failure cannot
    # escalate everything; the action signal has no such hole.
    planner_cascade_escalate_on: str = "action:read"
    # Hard ceiling on the digest the stronger model sees, in characters. The whole value of the
    # cascade is that the second call is small, so this is a real cap, not a hint.
    planner_cascade_digest_chars: int = 9000
    # THE REACH JUDGE (see core/reach_judge.py). One small question answered by a STRONGER tier
    # before planning: does what this request needs live inside the readable sources, outside them,
    # or in the world right now. Its verdict is stamped into the planner prompt as settled fact.
    # Off by default, and inert without ``read_reach_summary``, which only a consumer can write.
    # RECIPE FAST PATH (core/recipes.py). With a ``RecipeStore`` wired on the Orchestrator, a request
    # that matches an operation already worked out once (a tool call that succeeded) runs that
    # operation immediately, before understanding and context search, and the tool's receipt is the
    # answer. Inert without a store. ``recipe_allow_mutating`` False limits the fast path to
    # read-only tools. A new operation is learned from every successful planner ``tool`` action.
    recipe_fast_path: bool = True
    recipe_allow_mutating: bool = True
    # DEEP RECIPES: the same idea for a consumer whose operations are generated code run by a deep
    # runner rather than registered tools (Quest chat). A quest-data deep run that succeeded is
    # remembered; the next matching request skips the planner and the turn-start context search and
    # goes straight to that deep run, which still applies all of its own gates (approval cards,
    # write review). Needs a deep runner and a store; inert otherwise.
    recipe_deep: bool = True
    planner_reach_judge: bool = False
    planner_reach_judge_tier: str = "balanced"
    # The judge starts at the top of run(), concurrently with request understanding and context
    # assembly, and the first plan only COLLECTS it. This is how long that collect may wait before
    # planning proceeds without a verdict (exactly as with the judge off). It bounds a stuck judge;
    # it is not a latency target, since the judge normally finishes before context assembly does.
    planner_reach_judge_timeout_seconds: float = 20.0
    # CONSUMER-SUPPLIED: a short statement of what this deployment's reads can reach, and what its
    # attached execution environments handle. The library cannot know either, and a judge given a
    # generic guess would be worse than no judge, so an empty value disables the judge outright.
    read_reach_summary: str = ""
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
    # SUPPORT CHECK, folded into the SAME verdict as ``verify_claims`` (no extra call): the verdict
    # also flags factual claims or advice the reply states as certain that nothing this turn
    # supports (``claims_unsupported``). A flagged reply is rewritten ONCE to state its basis,
    # hedge, or drop the claim. Only consulted when ``verify_claims`` is on.
    verify_support: bool = True
    # LAST-RESORT DEEP RUN: when the turn's goal verdict is definitively NOT met and no deep run
    # was attempted this turn, run ONE deep pass before handing the user a non-answer (for example
    # "I don't have material on that, which file should I look in?"). Only applies when a deep
    # execution path is wired. The in-loop ``need_more_context`` escalation still comes first.
    deep_before_giving_up: bool = True
    # STRUCTURAL SUFFICIENCY GATE (see core/sufficiency.py). The prose SUFFICIENCY gate in the
    # planner prompt is an instruction the model may simply not follow: a real turn answered from a
    # card item that held only a short synthesized SUMMARY of a note, and spent its reply telling
    # the user it had "only the header" and asking whether to go and fetch the rest. When on, a plan
    # that terminates in "answer" while a declared-ABRIDGED context item's ``full_ref`` read spec
    # has NOT been executed this turn is turned into ONE "read" step that executes it, and the loop
    # re-plans with the full text in GATHERED. Keyed entirely off structured data (the item's own
    # declared fetch spec vs the read specs that actually ran), never off words in the model's
    # output. INERT unless a content item declares ``full_ref``: with no declaration there is
    # nothing to fetch, no notice is rendered, and the turn is byte-for-byte what it was.
    full_read_before_answer: bool = True
    # GOAL-VERIFICATION JUDGE TIER. The tier ``_verify_goal`` (the met/not-met + claims-honesty
    # verdict) resolves its model from. This verdict is the run's risk gate: it decides done vs
    # needs_you/failed and whether a reply's completion claims are honest, so a wrong verdict either
    # ships a false "done" or triggers a full regeneration / deep re-run — both far costlier than
    # the tier delta on ONE small call whose inputs are already hard-capped (~12.5k chars). Spend
    # the strong model on judgment, keep the cheap tiers for gathering. Empty string falls back to
    # ``planner_tier`` (the previous behavior).
    verify_tier: str = "balanced"
    # INTENT-DIRECTIVE JUDGE: the FALLBACK for a turn whose planner gave no usable ``user_intent``
    # verdict (the planner's structured verdict decides every other turn for free, on the call it
    # already makes). ONE structured LLM call judges the message instead of guessing. This is a
    # ROUTING decision, not the run's outcome gate (``verify_tier`` is that), so it defaults to the
    # cheaper "balanced" tier. The call is hard-timeout-guarded (``intent_judge_timeout_seconds()``)
    # and falls back to "not a directive" on any failure/timeout/parse miss, so it can never block
    # the turn.
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
    # routine AI-doable work) or escalate_human (a genuine human-only fork). ON by default: a run
    # nobody is watching is the failure mode this exists to prevent, and every consult is pre-filtered
    # by a free non-LLM gate, capped per run, and non-blocking, so the cost of leaving it on is small
    # and bounded. Set ``overseer=False`` to opt out; when off the run is byte-for-byte identical
    # (zero overseer calls, no events, no threads). The overseer NEVER raises: any failure degrades
    # to "proceed" (do nothing).
    overseer: bool = True
    overseer_tier: str = "quality"       # the tier the overseer model resolves to
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
    # Hook B (final answer checkpoint) is the LAST look before the draft reaches the user, so unlike
    # hook A there is no later step to apply a correction one step late. Fix 11 made it fully
    # non-blocking by reusing ``overseer_poll_timeout_seconds`` (0.0), which removed its latency cost
    # but also removed its AUTHORITY: no provider call resolves in zero seconds, so in practice the
    # draft always shipped first and the verdict landed in a background finisher that, by design,
    # never touches the already-returned answer. A checkpoint that can never stop anything is not a
    # checkpoint. Hook B therefore has its OWN small bounded wait: long enough for a quick judge to
    # correct the same answer it is judging, short enough that a slow one still degrades to shipping
    # the draft and finishing in the background. Hook A stays at 0.0 (never stall the walk).
    # Set 0.0 to restore the fully non-blocking Fix-11 behavior.
    overseer_answer_checkpoint_timeout_seconds: float = 2.5
    # This bounds how long the BACKGROUND finisher will wait before giving up entirely (a late
    # resolution past this point is simply dropped; nothing is waiting on it).
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
    # QUEST AI ANTICIPATION ENGINE (see core/anticipation.py), opt-in (default OFF -- with the
    # flag off, or no Anticipator wired, the run is BYTE-FOR-BYTE identical: zero anticipation
    # calls, zero threads, zero events, no store files touched; the same guarantee the overseer
    # gives). When ON and an Anticipator is wired (``Orchestrator.anticipator``), each turn:
    #   * at TURN START, observes the actual message against the live predictions planned after
    #     the previous turn (every prediction gets an outcome score -- the logged objective
    #     function -- and its source pattern gets the EMA hit/miss update), and when the best
    #     match clears MATCH_SERVE, seeds the turn with that prediction's PRECOMPUTED context as
    #     a cheap discardable hint (CACHED_HINT_GATE doctrine; the normal path is untouched);
    #   * at TURN END, learns the ask pattern and plans the next predictions (with precomputed
    #     bundles) in a background daemon thread that never blocks the returned answer.
    # Pattern-based only in this lane: NO LLM calls anywhere in the engine. Env: QAR_ANTICIPATION
    # ("1"/"true" enables; read in cli.py's _config_from_env).
    anticipation_enabled: bool = False
    # OPTIONAL one-LLM-call-per-turn refresh of the anticipation predictions (see
    # core/anticipation.py ``Anticipator.refresh``): refines chip display text, drops
    # conversation-obsoleted predictions, adds a few follow-ups. Default OFF so the lane stays
    # zero-LLM by default; enabling it only takes effect when an Anticipator with a wired ``refiner``
    # is present (``resolve_anticipator`` builds one from ``cfg.model_provider`` when this is on).
    # Env: QAR_ANTICIPATION_LLM ("1"/"true" enables; read in cli.py's _config_from_env).
    anticipation_llm_enabled: bool = False
    # "EXPLAIN HOW I GOT THIS" (see core/answer_explanation.py), opt-in (default OFF -- with the
    # flag off the run is byte-for-byte what it was: zero extra calls, no extra event).
    # When ON, a turn that PASSES the model-free eligibility predicate (it read, executed, ran
    # deep, answered on assembled context, took more than one step, searched the web, or the runner
    # kept a metric result for it) emits ONE extra EVENT_EXPLANATION carrying a user-facing account
    # of how the answer was reached, with the metric before and after in its ``metrics`` list.
    # Placement is deliberate: the call runs AFTER EVENT_RESULT and BEFORE EVENT_DONE, so the
    # answer is already on the reader's screen while it is being written. The answer is never
    # delayed by it. Ineligible turns (plain small talk) emit nothing, so a consumer's toggle
    # simply does not appear. Env: QAR_EXPLAIN_ANSWER ("1"/"true" enables).
    explain_answer: bool = False
    # The tier the explanation call resolves to. This is a summary of a turn that already happened,
    # not a gate on its outcome (that is verify_tier), so it gets the cheap tier.
    explain_tier: str = "fast"
    # LINK GUARD (see core/link_guard.py): every terminal reply is scanned and no link the guard
    # could not verify reaches the reader. A model writes plausible, wrong URLs; a reader who taps
    # one and lands nowhere stops trusting the real links too. ON by default because sending a
    # fabricated address is worse than sending none. Env: QAR_LINK_GUARD ("0"/"false" disables).
    link_guard: bool = True
    # JSON file describing what "verified" means for THIS host app: its route table (or a
    # ``routes_file`` pointing at a generated one), its own origins, trusted hosts, in-app schemes
    # and rewrite rules. Unset means external URLs are still checked over the network while in-app
    # paths have nothing to be checked against, so they come back unverified and are stripped.
    # Env: QAR_LINK_POLICY_FILE.
    link_policy_file: str = ""
    # The same thing built in code: a ``core.link_guard.LinkPolicy``. Wins over ``link_policy_file``
    # when both are set, so a consumer that derives its routes and origins at runtime does not have
    # to write a temporary file.
    link_policy: Optional[Any] = None


@dataclass
class OrchestratorResult:
    """What the loop produced. Exactly one terminal kind."""
    kind: str                          # "answer" | "deep" | "confirm" | "cancelled"
    text: Optional[str] = None         # for answer
    deep_results: List[DeepResult] = field(default_factory=list)   # for deep (1..N)
    goals: List[str] = field(default_factory=list)                 # the goal(s) run
    decision_id: Optional[str] = None  # for confirm, or an "answer" turn whose deferred work
                                        # parked on an approval card (exit_reason == "parked")
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
    # "parked" (deferred work resolved to a parked approval decision with nothing landed; see
    # ``decision_id``, never re-verified against the goal -- the goal is intentionally not met
    # yet) |
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
    # The USER-FACING "how I got this" payload for this turn, when one was produced (see
    # core/answer_explanation.py and OrchestratorConfig.explain_answer). Also emitted live as
    # EVENT_EXPLANATION; carried here as well so a NON-streaming consumer can persist it from the
    # returned result without having to observe the event stream. None whenever the feature is off
    # or the turn was judged not worth explaining.
    explanation: Optional[Dict[str, Any]] = None
    # One entry per link the LINK GUARD judged in this turn's reply (see core/link_guard.py):
    # {"url", "verdict": "ok"|"dead"|"unknown", "reason", "replaced_with"}. Empty when the guard is
    # off or the reply carried no links. This is the record of WHY a link was kept or stripped, so
    # a stripped link is auditable rather than a silent edit.
    link_checks: List[Dict[str, Any]] = field(default_factory=list)
    # The metric record of the turn's deep goals, for a consumer that reads the result and not the events: the
    # ``metrics`` payload entries of every deep result in order (``DeepResult.metric_results``; shape in
    # docs/metric-results.md), and whether any goal changed something and ended with no metric result recorded.
    # Written by the loop when it finishes, from the deep results alone. Empty/False for a turn with no deep goal.
    metric_results: List[Dict[str, Any]] = field(default_factory=list)
    metric_missing: bool = False
