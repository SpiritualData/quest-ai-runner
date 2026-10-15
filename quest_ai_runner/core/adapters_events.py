"""``Mode`` and the progress event type constants, with the set that always surfaces.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import enum


# ---------------------------------------------------------------------------
# The TWO PRODUCT MODES (one brain). The orchestrator emits the SAME events in
# both; the Mode only selects which ProgressSink the consumer wires, and the
# sink — never the orchestrator — decides what actually surfaces.
# ---------------------------------------------------------------------------

class Mode(enum.Enum):
    """Which product lane a run executes in.

    * ``LIVE``       — a human is attending. Every event streams to the consumer in real time
                       (status ticks, the reply, confirm requests). Used in-process by chat.
    * ``BACKGROUND`` — sent-off / scheduled; nobody is watching. Surfaces NOTHING intermediate —
                       only a result, a decision-request, or an explicit milestone. The runner's
                       poll-by-due lane uses this.
    """
    LIVE = "live"
    BACKGROUND = "background"


# Event types the orchestrator emits through the ProgressSink. Stable strings so consumers
# (quest-backend, the cockpit) can switch on them. "Chatter" types (status/plan/read/replan)
# are the ones a BACKGROUND sink drops; the rest always surface.
EVENT_STATUS = "status"        # a human-facing progress tick ("reading…", "answering")


EVENT_PLAN = "plan"            # the planner chose a next step (planning/re-planning chatter)


EVENT_READ = "read"            # a gather step ran (reading/grepping chatter)


EVENT_REPLAN = "replan"        # the loop is re-planning with what it just gathered (chatter)


EVENT_PARTIAL = "partial"      # a partial/streaming chunk of the reply (LIVE only)


EVENT_EXEC = "exec"            # a deep-run EXECUTION-lifecycle tick — generated code, an


                               # execution attempt, its raw output, a retry, done/error.
                               # Carries structured ``data`` (phase/code/attempt/output/error).
                               # LIVE-only texture: like the chatter types it is NOT in
                               # SURFACING_EVENTS, so a BACKGROUND run drops it (the runner
                               # still posts its own milestones/result); a LIVE run shows it.
EVENT_UNDERSTANDING = "understanding"  # Step 1 produced a goal condition (the resolved request).


                               # Fired only when User Input Understanding actually ran (a short/
                               # anaphoric input was resolved against conversation context). Carries
                               # ``data`` with the goal_condition. ALWAYS surfaces (in SURFACING_EVENTS).
EVENT_CONTEXT = "context"      # context assembled for this turn: cards selected + sources.


                               # Fired when a ContextAssembler is wired and produces card_metadata.
                               # Carries ``data`` with card_metadata list and sources (ALWAYS surfaces).
EVENT_INTENT = "intent"        # an announcement that work is ABOUT TO START ("Executing: <goal>").


                               # NOT a result and never a stand-in for one: it fires BEFORE the
                               # work runs, so at the moment it is emitted nothing has happened yet.
                               # It exists as its own type precisely because it used to be emitted
                               # as EVENT_RESULT, and both chat UIs (which fall back to the last
                               # result text when a deep turn produces no output of its own) then
                               # showed "Executing: <goal>" as the turn's ANSWER — reading as a
                               # completion report for work that never ran. A consumer must render
                               # this as an in-progress announcement, never as the turn's outcome.
                               # ALWAYS surfaces (in SURFACING_EVENTS).
EVENT_RESULT = "result"        # the final answer / deep output (ALWAYS surfaces)


EVENT_DECISION = "decision"    # a confirm / human decision-request was raised (ALWAYS surfaces)


EVENT_MILESTONE = "milestone"  # an explicit, real milestone worth surfacing (ALWAYS surfaces)


EVENT_DONE = "done"            # the run reached a terminal state (ALWAYS surfaces)


EVENT_TOKENS = "tokens"        # cumulative token counts after a model call (ALWAYS surfaces)


EVENT_OVERSEER = "overseer"    # a minimal-intervention OVERSEER consultation happened this run —


                               # its signal (proceed/redirect/answer_now/escalate) + optional hint.
                               # ALWAYS surfaces (in SURFACING_EVENTS) so a BACKGROUND run can note a
                               # course correction; it fires on every consultation, including proceed.
EVENT_MODE_SIGNAL = "mode_signal"  # the planner detected an EXPLICIT user request to change the


                               # execution mode (see OrchestratorConfig.execution_mode). Carries
                               # ``data`` = {"signal": "enter_brainstorm"|"exit_brainstorm",
                               # "execution_mode": <the mode this run was configured with>}.
                               # The orchestrator is STATELESS about modes: it only reports the
                               # signal; the consumer owns the latch (persisting the mode and
                               # passing it back in on future runs). ALWAYS surfaces.
EVENT_EXPLANATION = "explanation"  # the USER-FACING account of how this turn reached its answer


                               # (opt-in via OrchestratorConfig.explain_answer). Emitted once, AFTER
                               # EVENT_RESULT and BEFORE EVENT_DONE, so the answer is already on the
                               # consumer's screen while this is being written: it costs the reader
                               # no latency. Carries ``data`` = the explanation payload (see
                               # ``orchestrator.build_answer_explanation``): the model-written
                               # sections (understood/approach/assumptions/confidence/limitations/
                               # what_would_change) alongside ``used`` and ``signals``, which are a
                               # RECORD of the run (cards, sources, reads, actions, goal verdict)
                               # and not prose. Emitted ONLY on turns the model-free eligibility
                               # predicate judged worth explaining, so a consumer that shows a
                               # toggle simply shows nothing when no event arrives.
                               # NOTE: this is NOT the internal narration channel. It carries wording
                               # composed FOR THE READER, never the run's own internal wording
                               # relayed. ALWAYS surfaces.
EVENT_CARD_THREAD = "card_thread"  # this turn's TOPIC assignment: which context card (idea) the


                               # message belongs to (see core/card_thread.py; opt-in via
                               # OrchestratorConfig.card_thread_enabled). Carries ``data`` =
                               # {"action": "continue"|"switch"|"new", "card_id": <id or None>,
                               # "label": <label or None>, "previous_card_id": <id or None>}. The
                               # orchestrator is STATELESS about threads exactly as it is about
                               # modes: it reports the assignment, the consumer owns the card store
                               # and stamps its messages. ALWAYS surfaces (a consumer must be able to
                               # stamp the turn even on a BACKGROUND run).

# The event types a BACKGROUND (MilestoneSink) run forwards. Everything else is dropped as
# intermediate chatter. Encoded ONCE here so every consumer inherits the same policy.
SURFACING_EVENTS = frozenset({EVENT_UNDERSTANDING, EVENT_CONTEXT, EVENT_INTENT, EVENT_RESULT, EVENT_DECISION, EVENT_MILESTONE, EVENT_DONE, EVENT_TOKENS, EVENT_OVERSEER, EVENT_MODE_SIGNAL, EVENT_CARD_THREAD, EVENT_EXPLANATION})
