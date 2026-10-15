"""Running a deep goal: attempts, continuation, verification and result.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from .auxiliary_prompts import (
    DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION,
    DEEP_FUTURE_CONTEXT_INSTRUCTION,
)
from .adapters import (
    DeepResult,
    EVENT_EXEC,
    EVENT_MILESTONE,
    EVENT_STATUS,
    FUTURE_CONTEXT_VIA_FIELD,
    PlanDecision,
    ProgressEvent,
    ScopedThreadPoolExecutor as ThreadPoolExecutor,
    runner_starts_background_work,
    runner_uses_deep_model,
)
from .guard import ExecutionFact, ExecutionRecord, classify_exec_phase
from .planner_context import _is_discovery_obs, _render_gathered
from .orchestrator_decisions import _truncate_goal, apply_quest_data_ladder_guard
from .orchestrator_decide_tool import DEFERRED_RUNNER_KEY
from .orchestrator_constants import (
    CONTINUE_AFTER_DECISION_NOTE,
    DEEP_CONTINUATION_TURN_MULTIPLIER_CAP,
    NOT_STARTED_AFTER_DECISION_NOTE,
    NO_DEEP_EXECUTOR_TEXT,
    log,
    result_reports_something,
)
from .orchestrator_capabilities import (
    _run_goal_accepts_context_preamble,
    _run_goal_accepts_emit,
    _run_goal_accepts_resume_session_id,
    _run_goal_accepts_run_id,
    _run_goal_accepts_working_dir,
    run_goal_accepts_gathered_observations,
    run_goal_accepts_is_subgoal,
)
from .orchestrator_text import (
    _future_context_channel,
    _normalize_future_context,
    _strip_future_context,
)
from .orchestrator_turn import _Emitter
from .orchestrator_types import OrchestratorResult


class DeepRunMixin:
    """Running a deep goal: attempts, continuation, verification and result."""

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
                  working_dir_override: Optional[str] = None,
                  resume_session_id: Optional[str] = None,
                  self_initiated: bool = False,
                  user_ordered: bool = False) -> OrchestratorResult:
        # ``self_initiated``: this deep run is the orchestrator's OWN escalation (the answer
        # verifier asked for more context, or the last-resort run before giving up), not work the
        # user or the planner asked for. A goal that resolves to a runner whose work outlives the
        # turn (``starts_background_work``, e.g. a consumer's task-queue runner) is then NOT
        # started: its DeepResult comes back ``declined_background`` and the escalation site keeps
        # the answer it has. Found 2026-10-07 in Quest's chat: "Mia loves the yellow paint, I'm
        # buying it tomorrow" got a background task queued to "purchase the paint".
        # ``user_ordered``: the planner's structured verdict said the user's message ordered work
        # (``user_ordered_work(turn_user_intent)``), so a self-initiated run may still start
        # background work. A missing verdict is False here: the conservative side is not starting
        # background work the user did not clearly ask for.
        # ``resume_session_id``: a session left behind by an EARLIER RUN of this thread (see
        # ``Orchestrator.run``). The first attempt of a SINGLE-goal deep run picks it up through
        # the same ``resume_session`` variable the within-run turn-budget continuation uses, so
        # there is exactly one resume path. It is deliberately NOT applied to a fanned-out
        # multi-subgoal run: those subgoals run concurrently, and pointing several workers at one
        # session would interleave them in a single transcript. None = a cold start, as before.
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
        # The single quest this run is about, if any -- used below only for the orphan-decision
        # recovery probe (see ``quest_open_decision_ids``/``find_new_decision_id``). A multi-quest
        # conversation's ``quest_ids`` is not consulted here: the probe is a best-effort narrowing
        # to ONE quest's decisions, and guessing among several would be worse than skipping it.
        if ctx_meta is not None:
            # Turn-level record that a deep run was attempted (see ``deep_before_giving_up``).
            ctx_meta["deep_attempted"] = True
            # DOCS-FALLBACK GATING (see ``gate_docs_fallback``): by the time a deep run starts,
            # the planner has already BLOCKED on this same turn's reach verdict to build its own
            # prompt, so a non-blocking peek here is free. Every per-goal/widening context
            # assembly for the rest of THIS deep run reads ``ctx_meta``, so stamping it once here
            # covers all of them.
            self.gate_docs_fallback(ctx_meta, user_message)
        quest_id = (ctx_meta or {}).get("quest_id")
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
            # CARRY THE EXPLANATION ON THE RESULT ITSELF. This used to return with ``text=None``,
            # which left every caller to invent its own account of what happened — and both chat
            # UIs, having nothing to show, fell back to the interim "Executing: <goal>" line and
            # presented it as the answer. The result now states plainly that nothing ran and what
            # to do about it, so the honest wording travels with the result to every consumer
            # (chat UIs, the poll lane's task report, a backend relaying it) instead of being
            # re-derived, or lost, at each one.
            return OrchestratorResult(kind="deep", goals=goals, rationale=plan.rationale,
                                      deep_results=[], text=NO_DEEP_EXECUTOR_TEXT)

        # HIERARCHICAL GOAL: the overall user-level goal this turn pursues. When the work fans out
        # into parallel subgoals, each subgoal process must be told the HIGHER goal it serves, so it
        # stays aligned with the whole instead of optimizing its piece in isolation.
        overall_goal = (plan.goal or "").strip() or user_message
        multi = len(subtasks) > 1
        # Whether the async post-deep card updater is active (toggle on + provider + card-update
        # store). Computed once: it gates appending the FUTURE-CONTEXT instruction to each deep brief.
        card_update_active = self._card_updater_active()

        # The model ladder for THIS deep run, resolved ONCE (every subgoal of a fan-out shares it):
        # an explicit per-task / guidance model pins it (no escalation); otherwise the planner's
        # difficulty rating picks the starting rung and the goal loop escalates from there. The
        # automatic choice is announced once as a status event carrying a structured
        # ``deep_model_selection`` payload, which the task lane records on the task's progress feed.
        planned_deep_models, deep_selection = self.deep_model_plan(
            model_hint, quality_standards, model,
            difficulty=getattr(plan, "deep_difficulty", None),
            difficulty_reason=getattr(plan, "deep_difficulty_reason", None))
        # ANNOUNCED ONLY FOR A RUNNER THAT RUNS THE MODEL, and so only once a goal's runner is
        # resolved (inside ``run_one`` below). A consumer's named runner may answer in-process with
        # its own fixed model, or queue the work for another lane that picks its own; announcing
        # "Starting on sonnet, with a stronger model ready" for those told a person a deep run was
        # starting when none was. Once per deep run, whichever subgoal reaches it first.
        selection_announced = threading.Event()
        selection_lock = threading.Lock()
        # SIBLINGS DO NOT PILE UP MORE ASKS: set the moment any subgoal of this fan-out parks on a
        # human decision (``DeepResult.decision_id``). Checked by every OTHER subgoal's attempt
        # loop before it starts a new attempt, so a sibling not yet underway (or not yet on its
        # next retry/rung) stops rather than producing a second, competing ask. Scoped to a single
        # ``_run_deep`` call; a single-goal turn never sets or checks it (``multi`` guards both).
        fanout_parked = threading.Event()
        # Which subgoal each result came from (keyed by the result object's id), so a sibling the
        # aggregation step cannot report on (it stopped before it ever ran) is still NAMED in the
        # reply as not done yet instead of vanishing.
        result_goals: Dict[int, str] = {}

        def announce_model_selection(runner: Any) -> None:
            if deep_selection is None or emit is None or not runner_uses_deep_model(runner):
                return
            with selection_lock:
                if selection_announced.is_set():
                    return
                selection_announced.set()
            level = deep_selection.get("difficulty")
            start_text = f"Starting on {deep_selection['start_model']}"
            if level:
                start_text += f" ({level} task)"
            if len(deep_selection["ladder"]) > 1:
                start_text += ", with a stronger model ready if the result falls short"
            emit.emit(ProgressEvent(type=EVENT_STATUS, text=start_text + ".",
                                    data={"deep_model_selection": dict(deep_selection)}))

        def run_one(task: Dict[str, Any], task_index: int = 0) -> DeepResult:
            goal = (task.get("goal") or "").strip() or f"Fully address: {user_message}"
            brief = (task.get("brief") or goal).strip()
            # EVERY deep process is told BOTH the top input-level goal (the user's actual request)
            # and, when it is a subgoal of a larger fan-out, the overall goal it serves — so it never
            # loses sight of what the user wants while pursuing its specific piece. (Its OWN process
            # goal/done-standard is added separately by compose_goal_prompt.) This header is baked
            # into the base brief, so every retry keeps it alongside the prior output + feedback.
            # The request is shown ONCE. When no plan wrote a separate brief, the brief IS the
            # request (a queued task's whole text), and repeating it under TASK sent the same
            # 110K-character brief twice in one prompt (2026-10-06).
            if brief == (user_message or "").strip():
                brief = "Work the request above, in full: it is this task's whole brief."
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
            # DIRECT TOOLS for the deep worker: the tools relevant to this goal, each with the
            # exact shell command that calls it, so a run that needs (say) to send the quest's
            # mail calls the tool instead of rediscovering how. Absent when no tools are wired.
            if self.tools is not None:
                deep_tools_block = self.tools.render_deep_block(
                    f"{user_message}\n{goal}", pins=(ctx_meta or {}).get("tool_pins") or ())
                if deep_tools_block:
                    if quest_id:
                        deep_tools_block += (f"\nThis run's quest is {quest_id}: add "
                                         f"--quest {quest_id} to a call so tools that take the "
                                         f"quest from context get it.")
                    brief_task_id = (ctx_meta or {}).get("task_id")
                    if brief_task_id:
                        deep_tools_block += (f"\nThis run's task is {brief_task_id}: add "
                                             f"--task {brief_task_id} to a call so tools that "
                                             f"need the task (who asked, what they replied) get it.")
                    brief = brief + "\n\n" + deep_tools_block
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

            # Resolve which runner(s) handle THIS goal ONCE per task (not per retry — the
            # classifier's inputs (user_message/goal/brief) don't change across retries of the
            # same task). The result is the LADDER for this goal, cheapest rung first, indexed by
            # attempt in the loop below.
            #
            # Only the DEFAULT path can be multi-rung. A pinned ``runner_override`` and a
            # classifier-selected named runner are both deliberate routing decisions about where
            # this goal belongs, so each collapses to a one-rung ladder: silently prepending a
            # cheaper rung would override a choice the caller/consumer already made.
            runner_ladder: List[Any] = ([runner_override] if runner_override is not None
                                        else list(self.deep_runner_ladder))
            # True once the ladder has been narrowed to ONE rung by a DELIBERATE routing choice
            # (a pinned ``runner_override``, or the classifier matching a named runner) rather
            # than the default multi-rung ladder. The quest-data guard below must not re-route
            # either of those -- they are the caller's/consumer's own choice of where this goal
            # belongs -- but it still appends its brief instruction to them when needed.
            ladder_is_deliberate_choice = runner_override is not None
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
                        runner_ladder = [self.deep_runners[key]]
                        ladder_is_deliberate_choice = True
                        log.debug(f"deep_runner_classifier selected runner {key!r}")
                    else:
                        log.warning(
                            f"deep_runner_classifier returned unknown key {key!r}; "
                            f"using default runner"
                        )
                except Exception as e:  # noqa: BLE001 — classifier failure must never block a run
                    log.warning(f"deep_runner_classifier failed ({e}); using default runner")

            if not runner_ladder:
                runner_ladder = [None]  # nothing wired: _do_run reports that honestly, as before

            # QUEST-DATA LADDER GUARD (Joshua, 2026-10-01): a quest-data change must never land on
            # a rung that can only edit files, and must prefer a governed-operations rung over the
            # full agent when one is available. Reached only when the planner judged this goal's
            # target is the person's own Quest data; byte-identical otherwise.
            if getattr(plan, "deep_target", None) == "quest_data":
                runner_ladder, brief = apply_quest_data_ladder_guard(
                    runner_ladder, brief, deliberate_choice=ladder_is_deliberate_choice
                )

            # The TERMINAL rung — the runner that has the last word on this goal, and the one whose
            # channel the brief's FUTURE-CONTEXT ask is written for. With the default one-rung
            # ladder this IS the resolved runner, so nothing changes for any existing consumer. On
            # a multi-rung ladder it is the right choice of the two: the cheap first rung fills
            # ``future_context`` from what it knows structurally (see FastEditRunner) rather than
            # from an instruction in its brief, while the terminal rung is a prose worker that does
            # need to be asked.
            terminal_runner = runner_ladder[-1]
            # Decline BEFORE anything is announced, so no status line promises a run that never
            # starts. Only when the user's own words did not ask for work: a request the planner
            # merely answered ("research venues for me") may still go to the queue this way.
            if (self_initiated and not user_ordered
                    and any(runner_starts_background_work(r) for r in runner_ladder)):
                log.info("Self-initiated escalation resolved to a background-work runner the "
                         "user did not ask for; not starting it (the current answer stands)")
                return DeepResult(met=False, output="", declined_background=True,
                                  error="escalation not started: the runner it resolved to "
                                        "starts background work the user did not ask for")
            if per_goal_context and emit is not None:
                # Progress the person can read, not internal state. "Selected context for goal: ..."
                # named an orchestrator step and read as leaked machinery in the live status pill.
                emit.status(f"Working on: {goal[:60]}")
            announce_model_selection(terminal_runner)

            # FUTURE-CONTEXT ask, routed by the RESOLVED runner's channel. When the async card updater
            # is active, EVERY runner is asked for future context (a code generator knows the most
            # reusable facts of all: the entities, ids, and schema it touched), but a strict-format
            # runner is asked for it OUT OF BAND so its payload stays a valid payload. This is the ask
            # side; ``_normalize_future_context`` below is the guarantee side.
            if card_update_active:
                brief = brief + (
                    DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION
                    if _future_context_channel(terminal_runner) == FUTURE_CONTEXT_VIA_FIELD
                    else DEEP_FUTURE_CONTEXT_INSTRUCTION
                )

            # WHICH OPTIONAL KWARGS a rung accepts. Every one of these is passed to a runner ONLY
            # if its ``run_goal`` accepts it (directly or via **kwargs), so older signatures keep
            # working. Decided by signature inspection — never by a try/except TypeError, which
            # could re-invoke a runner that already ran a side effect (e.g. a data mutation).
            #
            # Computed PER RUNNER (cached by identity), because the ladder's rungs are different
            # objects with different signatures: a probe of one rung says nothing about another.
            # It must also never be computed against ``self.deep_runner``, which is None when the
            # named-runner registry is the wiring style (that once silently dropped exec streaming
            # for every named-registry consumer).
            #   emit        — the live EXECUTION-lifecycle stream. We TEE it so EVENT_EXEC phase
            #                 ticks are recorded into ``exec_record`` (per-subtask) for the
            #                 broken-promise guard, while still streaming to the live sink.
            #   run_id      — the stable id for this subgoal's whole retry sequence.
            #   preamble    — a per-task ``rep_preamble`` (e.g. an AI rep's pulled persona) plus the
            #                 brain's own ``gathered`` reads, combined. Probed regardless of whether
            #                 either is set: either alone is enough to build a useful preamble.
            #   working_dir — a per-task working-directory override (e.g. a quest's synced folder).
            #                 See quest_autopilot_design.md's execution-environment section: "one
            #                 quest, one folder, one env" -- the worker starts where that quest's
            #                 real work lives.
            #   gathered    — the SAME brain content as ``preamble``, as structured dicts instead of
            #                 flattened text (see ``run_goal_accepts_gathered_observations``).
            runner_caps: Dict[int, Dict[str, bool]] = {}

            def caps_for(runner: Any) -> Dict[str, bool]:
                if runner is None:
                    return {"emit": False, "run_id": False, "preamble": False, "gathered": False,
                            "working_dir": False, "resume": False, "is_subgoal": False}
                cached = runner_caps.get(id(runner))
                if cached is None:
                    cached = {
                        "emit": emit is not None and _run_goal_accepts_emit(runner),
                        "run_id": _run_goal_accepts_run_id(runner),
                        "preamble": _run_goal_accepts_context_preamble(runner),
                        "gathered": run_goal_accepts_gathered_observations(runner),
                        "working_dir": _run_goal_accepts_working_dir(runner),
                        "resume": _run_goal_accepts_resume_session_id(runner),
                        "is_subgoal": run_goal_accepts_is_subgoal(runner),
                    }
                    runner_caps[id(runner)] = cached
                return cached

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
                # No ``wants_emit`` guard needed here: this callback is handed to a runner ONLY
                # when that rung's caps say it accepts ``emit``, which already requires a sink.
                if emit is not None:
                    log.debug(f"emitting exec event: {getattr(ev, 'type', '?')}: "
                             f"{(getattr(ev, 'text', '') or '')[:80]}")
                    emit.emit(ev)

            def _do_run(current_brief: str, run_model: Optional[str],
                        active_runner: Any, *,
                        resume_session_id: Optional[str] = None,
                        max_turns: Optional[int] = None) -> DeepResult:
                caps = caps_for(active_runner)
                try:
                    if active_runner is None:
                        return DeepResult(met=False, error="no deep runner configured")
                    kwargs = dict(goal=goal, brief=current_brief, model=run_model,
                                  max_turns=max_turns or self.cfg.deep_max_turns)
                    # CONTINUATION of an attempt that ran out of turns: same session, so the worker
                    # resumes with everything it already read and decided. Only for a runner that
                    # accepts it; everyone else re-runs from scratch exactly as before.
                    if resume_session_id and caps["resume"]:
                        kwargs["resume_session_id"] = resume_session_id
                    if caps["emit"]:
                        kwargs["emit"] = _emit_one
                    if caps["run_id"]:
                        # task_uuid is generated ONCE per subgoal (before the retry loop below), so
                        # every attempt -- even one that spawns a brand-new subprocess/session --
                        # reports under the same id. Without this, a consumer's dashboard would show
                        # each retry as a new, duplicate deep-run entry for one ongoing subgoal.
                        kwargs["run_id"] = task_uuid
                    if caps["preamble"] or caps["gathered"]:
                        # MAIN-FLOW ACCUMULATION vs SUBGOAL FOCUS. A single main-flow deep run
                        # ACCUMULATES: it carries the brain's gathered content forward. A fanned-out
                        # subgoal (multi) does NOT inherit that whole pile -- it is handed ONLY its
                        # own focused context so it concentrates on its piece (it can still search for
                        # more if it falls short, via the widening below).
                        # Exclude discovery/capability listings (the operations/sources MENU): they
                        # are planner-only routing aids, not content the worker should ground on (the
                        # worker has its own tools). Pass forward only the real content the brain read.
                        _brain_content = [o for o in (gathered or []) if not _is_discovery_obs(o)]
                        # Same list, as structured dicts (see ``run_goal_accepts_gathered_observations``)
                        # for a runner that wants to filter it itself instead of re-parsing text.
                        if caps["gathered"] and _brain_content and not multi:
                            kwargs["gathered_observations"] = _brain_content
                    if caps["preamble"]:
                        preamble_parts = []
                        if rep_preamble:
                            preamble_parts.append(rep_preamble)
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
                    if caps["working_dir"] and working_dir_override:
                        kwargs["working_dir"] = working_dir_override
                    # Tell an opted-in runner this is ONE of several concurrent subgoals of the
                    # same turn (see ``run_goal_accepts_is_subgoal`` above), so it can scope
                    # itself to just this subgoal rather than re-deriving a sibling's work from
                    # the fuller USER'S REQUEST header this call's brief also carries.
                    if caps["is_subgoal"] and multi:
                        kwargs["is_subgoal"] = True
                    # The runner LADDER was resolved once per task, above (not re-resolved per
                    # retry — the classifier's inputs don't change across retries of the same
                    # task); which rung of it runs THIS attempt is decided by the loop below and
                    # passed in.
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
            # At least one attempt PER RUNG, so a ladder can always reach its terminal runner even
            # if a consumer configured a very small iteration cap. With the default one-rung ladder
            # this is exactly ``max(1, deep_goal_max_iterations)``, unchanged.
            max_iters = max(max(1, self.cfg.deep_goal_max_iterations), len(runner_ladder))
            budget = self.cfg.deep_goal_token_budget
            # The model ladder for THIS turn: an explicit per-task / guidance model pins it (no
            # escalation); otherwise fast -> strong, starting at the fast tier by default.
            deep_models = list(planned_deep_models)   # a copy: a verifier tier override edits it
            tier_idx = 0
            tokens_used = 0
            res = DeepResult(met=False)
            # TURN-BUDGET CONTINUATION state. ``resume_session`` is set when the previous attempt
            # ran out of turns and its worker session can be picked up again; ``attempt_turns`` is
            # the budget for the next attempt, grown once per continuation so a task that simply
            # needed more room gets it instead of hitting the same wall every attempt.
            # ...and it is ALSO where an ACROSS-RUN resume enters: a session a previous run of this
            # thread left behind (``resume_session_id``) is simply the value this starts at, so the
            # first attempt continues it exactly as a continuation attempt would. Single-goal runs
            # only: see the note on the parameter for why a fan-out never resumes.
            resume_session: Optional[str] = None if multi else (resume_session_id or None)
            attempt_turns = self.cfg.deep_max_turns
            continued = 0

            def can_continue(prev: DeepResult, runner: Any) -> bool:
                """Whether ``prev`` is a run that can be PICKED UP rather than redone.

                Three things have to hold: the worker itself said it ran out of turns
                (``limit_hit``, never our inference, see ``SubprocessGoalRunner``), it left a
                session id to resume, and this runner can actually resume one.
                """
                return bool(getattr(prev, "limit_hit", False)
                            and getattr(prev, "session_id", None)
                            and caps_for(runner)["resume"])

            def begin_continuation(prev: DeepResult,
                                   verdict: Optional[Dict[str, Any]]) -> bool:
                """Set the next attempt up to CONTINUE ``prev``'s session. False = cannot.

                Grows the turn budget once per continuation (capped), points the next attempt at
                the session to resume, and swaps in the short continuation brief. The only reason
                it refuses is the deep token budget: a continuation is a cheaper way to finish, it
                is never a way around the budget.
                """
                nonlocal continued, resume_session, attempt_turns, current_brief
                if budget is not None and tokens_used >= budget:
                    if emit is not None:
                        emit.status(f"Deep token budget reached ({tokens_used}/{budget}); "
                                    "stopping without continuing the run.")
                    return False
                continued += 1
                resume_session = prev.session_id
                attempt_turns = self.cfg.deep_max_turns * min(
                    continued + 1, DEEP_CONTINUATION_TURN_MULTIPLIER_CAP)
                current_brief = self._continuation_brief(base_brief, verdict)
                if emit is not None:
                    emit.status("That run used all its turns before finishing; continuing the "
                                f"same session with a larger budget ({attempt_turns} turns)…")
                return True

            # True only right after the VERIFIER judged a completed attempt not met. The "Goal not
            # met yet, retrying" line is said for that case alone: a launch failure, an empty run
            # falling through to the next rung, or a continuation each announce themselves, and
            # none of them is a goal that was checked and fell short.
            verified_not_met = False
            for attempt in range(1, max_iters + 1):
                # Cooperative cancellation, checked before starting each new attempt (a retry can be
                # a full agentic subprocess run, so this is the natural point to stop rather than
                # mid-subprocess). ``res`` keeps whatever the prior attempt produced.
                if cancel_check is not None and cancel_check():
                    break
                # A SIBLING subgoal of this same fan-out already parked on a human decision: stop
                # before starting another attempt rather than piling a second ask onto the same
                # turn (see ``fanout_parked`` above). Single-goal runs never set this (``multi``).
                if multi and fanout_parked.is_set():
                    break
                run_model = deep_models[min(tier_idx, len(deep_models) - 1)]
                # WHICH RUNNER runs this attempt: the ladder indexed by attempt, cheapest rung
                # first, with the terminal rung repeating for every further attempt — the same
                # ``min(idx, len - 1)`` shape the model ladder uses one line above. With the
                # default one-rung ladder this is the single resolved runner on every attempt.
                rung_idx = min(attempt - 1, len(runner_ladder) - 1)
                active_runner = runner_ladder[rung_idx]
                has_more_rungs = rung_idx < len(runner_ladder) - 1
                # Whether THIS rung runs the ladder's model at all. Every model claim below (which
                # model a retry uses, "Deep run used X", a tier switch) is made only when it does.
                model_applies = runner_uses_deep_model(active_runner)
                if emit is not None and attempt > 1 and verified_not_met:
                    emit.status("Goal not met yet, retrying"
                                + (f" with {run_model}" if run_model and model_applies else "")
                                + "…")
                verified_not_met = False
                # Fold in any NEW user messages that arrived since the run started, so this process
                # (the first attempt or a retry) acts on the latest input, not a stale request.
                _new = self._drain_pending(pending_inputs)
                run_brief = current_brief if not _new else (current_brief + "\n\n" + _new)
                # Snapshot BEFORE the run so a decision the worker raises directly against the
                # consumer's API (out of band from this sink) can be recovered below even if the
                # worker never prints (or garbles) the QAR-ESCALATED marker.
                before_decision_ids = self.quest_open_decision_ids(quest_id)
                res = _do_run(run_brief, run_model, active_runner,
                              resume_session_id=resume_session, max_turns=attempt_turns)
                if model_applies:
                    res.model = res.model or run_model
                if emit is not None and res.model and not res.launch_failed:
                    # WHICH model this attempt ran on: the tier it was asked for and the full id the
                    # CLI resolved it to, as structured data the task feed can show per attempt.
                    ran_on = (f"{res.model} ({res.resolved_model})"
                              if res.resolved_model and res.resolved_model != res.model else res.model)
                    emit.emit(ProgressEvent(
                        type=EVENT_STATUS, text=f"Deep run used {ran_on}.",
                        data={"deep_run_model": {"attempt": attempt, "tier": res.model,
                                                 "model": res.resolved_model or res.model,
                                                 "run_id": captured_run_id["id"]}}))
                resume_session = None   # consumed: a continuation is offered per attempt, not sticky
                if res.decision_id is None and not getattr(res, "deferred", False):
                    recovered = self.find_new_decision_id(quest_id, before_decision_ids)
                    if recovered:
                        log.info("deep run for quest %s created decision %s without printing a "
                                "QAR-ESCALATED marker; recovered it via the escalation sink "
                                "instead of closing the task done with it orphaned",
                                quest_id, recovered)
                        res.decision_id = recovered
                        res.met = False
                tokens_used += max(0, getattr(res, "tokens", 0) or 0)
                # ASYNC HAND-OFF: the runner queued the real run to finish out-of-band (its
                # ``output`` is a "task #N launched"-style sentinel, not work product). Re-verifying
                # that sentinel against the goal would ALWAYS fail and relaunch a fresh task every
                # iteration (a runaway loop). Trust the hand-off's own ``met`` and stop; the real
                # outcome is verified when it reflects back.
                if getattr(res, "deferred", False):
                    break
                # CLAUDE CODE IS AT ITS USAGE LIMIT (``DeepResult.usage_limited``): terminal for this
                # turn, whatever rungs remain, since every rung is a Claude model and would be
                # refused the same way. Nothing to verify; the task executor puts the work back in
                # the queue until the limit resets (``core.usage_limit``).
                if getattr(res, "usage_limited", False):
                    if emit is not None:
                        emit.status("Claude Code reached its usage limit; pausing this work "
                                    "until it resets.")
                    break
                # THE WORKER NEVER STARTED (``DeepResult.launch_failed``): nothing ran, so there is
                # nothing to verify, and a stronger model was never the problem. Verifying the CLI's
                # error text produced "Goal not met" and a retry on the next model within seconds,
                # which read as a deep run that ran and fell short. Say what happened instead, and
                # stop. A further RUNG is a different runner, so it still gets its turn.
                if getattr(res, "launch_failed", False):
                    if emit is not None:
                        emit.status(res.error or "The deep run could not start.")
                    if has_more_rungs:
                        continue
                    break
                # TURN-BUDGET CONTINUATION, decided BEFORE the terminal guard below, because a
                # worker stopped by ``--max-turns`` produces EXACTLY the shape that guard calls a
                # hard failure, and it is not one.
                #
                # Claude Code's ``error_max_turns`` envelope carries no result text: the run is cut
                # off before the worker ever writes its final message, so an out-of-turns result is
                # (error set, output EMPTY) by construction, every time. Deciding continuation
                # after the guard therefore meant the continuation never ran for the one case it
                # was written for. It reported the work as a failure instead, twice on a real lane
                # (2026-09-09, and again 2026-09-21 with the email already sent and the goal note
                # already posted), and the continuation brief had never once been sent to a worker.
                #
                # There is nothing to verify here (no output means no claim to check), so this
                # goes STRAIGHT back into the same worker session rather than spending a verifier
                # call on an empty string and then feeding that verdict's "it did nothing" reason
                # to a worker that had in fact done nearly everything.
                if (not res.decision_id and not (res.output or "").strip()
                        and can_continue(res, active_runner)):
                    if begin_continuation(res, None):
                        continue
                    break
                # A human-decision escalation, or a hard failure with NO output (binary missing,
                # timeout, silent no-op), is terminal: do not verify or iterate. A run that merely
                # ran out of turns is none of those and was handled just above.
                #
                # …unless there is a further RUNG to fall through to. That rule was written when a
                # goal only ever had one runner, so "this runner produced nothing" and "nothing
                # more can be tried" were the same statement. On a ladder they are not: a cheap
                # first rung that declines the goal or fails outright is precisely the case the
                # next rung exists for, and stopping here would strand the work. There is nothing
                # to verify (no output), so we go straight to the next attempt, which the indexing
                # above hands to the next runner.
                if res.decision_id or (res.error and not (res.output or "").strip()):
                    if res.decision_id or not has_more_rungs:
                        break
                    if emit is not None:
                        emit.status("That did not produce anything; escalating to the full "
                                    "deep runner…")
                    continue
                # THE RUNNER IS OUT OF MOVES (``DeepResult.exhausted``): it already spent its own
                # attempt budget this turn, or its retry reproduced a result it had returned before.
                # Re-launching it with an augmented brief is exactly the endless "correcting its
                # code" loop a user watched: same runner, same data, same result. A not-met
                # exhausted run is terminal (its output already says what it tried); a further
                # rung, when there is one, still gets its turn.
                if getattr(res, "exhausted", False) and not res.met:
                    if has_more_rungs:
                        continue
                    if emit is not None:
                        emit.status("Nothing new left to try on this; stopping instead of "
                                    "repeating the same attempt.")
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
                #
                # ``observations``/``observations_reported`` from THIS attempt's own ``res``: the
                # runner's own write receipts and read confirmations, the structured evidence the
                # verifier used to never see (it judged ``res.output`` and the brief alone). See
                # ``_verify_goal``'s docstring for the live finding this closes.
                verdict, verify_error = self._verify_goal(goal, base_brief, res.output or "",
                                            rep_preamble=rep_preamble,
                                            quality_standards=quality_standards,
                                            context_layer=per_goal_context,
                                            observations=getattr(res, "observations", None),
                                            observations_reported=bool(
                                                getattr(res, "observations_reported", False)))
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
                reason = verdict.get("reason") or "the done-standard was not satisfied"
                res.verdict_reason = reason
                res.verdict_next_action = (verdict.get("next_action") or "").strip()
                # The verifier's own account of WHY another attempt would or would not help. A gap
                # that is only proof beyond the ask is not re-run (re-running the worker to re-prove
                # finished work is pure cost); a gap only a person can close is not re-run either,
                # because the next attempt would hit the same wall. Neither claims a claimed change
                # the execution record contradicts, which is a different flag.
                _blocker = verdict.get("blocker") or "more_work"
                _q = (verdict.get("question") or "").strip()
                if _blocker == "evidence_only" and _q and not verdict.get("claims_unexecuted"):
                    res.met = True
                    res.error = None
                    res.unconfirmed_note = _q
                    if emit is not None:
                        emit.status("Goal accepted; one thing was not independently confirmed: "
                                    + _q)
                    break
                if _blocker == "needs_person" and _q:
                    res.needs_person = _q
                    if emit is not None:
                        emit.status("Stopping here: this needs a person. " + _q)
                    break
                # Reads as a sentence, because a person reads it: this lands in the activity feed
                # and can reach a human. A lowercase "goal not yet met:" followed by a verifier's
                # raw clause looked like debug output leaking into a report.
                res.error = res.error or ("Goal not yet met: " + reason[:1].upper() + reason[1:])
                # A runner that reported it is out of moves gets no retry even when its own ``met``
                # was optimistic: the verifier said no, and the runner said another run would only
                # repeat this one.
                stop_after_this = bool(getattr(res, "exhausted", False)) and not has_more_rungs
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
                if stop_after_this:
                    break
                # TURN-BUDGET CONTINUATION: the worker did not fail here, it ran out of room. Its
                # session still holds everything it read and changed, so continuing THAT session
                # with a bigger budget is both cheaper and far likelier to finish than re-running
                # the goal cold, which pays for the same discovery again and tends to stop in the
                # same place. (Without this, a long task burned attempt after attempt on the same
                # wall and then reported a bare failure, with its real work left uncommitted.)
                # …and the same continuation for a run that DID produce output and was then cut
                # off: it reached the verifier, so the continuation brief can carry the verifier's
                # specific next action instead of only "you were cut off".
                if can_continue(res, active_runner):
                    if begin_continuation(res, verdict):
                        continue
                    break

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
                # A rung that does not run the ladder's model has no tier to switch: the next
                # attempt is the same runner with the verifier's feedback, and is described as such.
                _vt = verdict.get("next_tier") if model_applies else None
                _resolved_tier = self._resolved_deep_tier(_vt, deep_models) if _vt else None
                if _resolved_tier is not None:
                    run_model = _resolved_tier
                    deep_models[min(tier_idx, len(deep_models) - 1)] = _resolved_tier
                    if emit is not None:
                        emit.status(f"Switching model tier to {_vt} for the next attempt…")
                elif model_applies and tier_idx < len(deep_models) - 1:
                    tier_idx += 1  # a capability gap is a common cause; try a stronger model next
                if budget is not None and tokens_used >= budget:
                    if emit is not None:
                        emit.status(f"Deep token budget reached ({tokens_used}/{budget}); stopping.")
                    break
                current_brief = self._augment_brief(base_brief, res.output or "", verdict)
                verified_not_met = True

            # This subgoal parked on a human decision: tell siblings so none of them starts a new
            # attempt after this (see ``fanout_parked`` above; checked at the aggregation step too,
            # which is what actually keeps a second ask out of the reply regardless of timing).
            if multi and res.decision_id:
                fanout_parked.set()
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
                                              "deep_output": _strip_future_context(res.output).strip() or None,
                                              "model": res.model,
                                              "resolved_model": res.resolved_model}))
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
            result_goals[id(res)] = goal
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

        deep_results = [r for r in all_results if r is not None]
        # ONE ASK, BUT NEVER AT THE COST OF WHAT ALREADY HAPPENED (structural, on ``decision_id``,
        # never on any result's own text -- hard rule #3).
        #
        # Once ANY subgoal of this fan-out parked on a human decision, exactly ONE ask reaches the
        # reply: the FIRST parked result by subtask order. Every FURTHER parked result is dropped,
        # which is the bug this guards (two independently-reviewed subtasks parked on the same
        # conflict, so the reply carried the conflict question twice plus an extra, unrequested
        # goal proposal).
        #
        # What is NOT dropped is a sibling that already DID its work. The first version of this
        # filter kept the parked result alone, so in a live run where all three requested goals
        # had actually landed the reply was a bare "please approve these changes to proceed": the
        # landed subgoals' own receipts and text never reached the reader, and the ask was the
        # only thing left to read. The order is therefore what happened, then the one ask, then
        # one short code-written continuation sentence. ``fanout_parked`` above still stops a
        # sibling that had not yet started its next attempt; this step guarantees the shape
        # regardless of how the concurrent runs happened to race.
        #
        # A single-goal turn (``multi`` False) and a fan-out where nothing parked are unaffected.
        if multi:
            parked = next((r for r in deep_results if getattr(r, "decision_id", None)), None)
            if parked is not None:
                # A sibling with nothing of its own to report (it was stopped before it ever ran,
                # because this ask came first) is NAMED as not done yet, never silently dropped:
                # otherwise the turn reads as though that part were handled (review, 2026-10-07).
                not_started = [result_goals.get(id(r)) for r in deep_results
                               if r is not parked and not getattr(r, "decision_id", None)
                               and not result_reports_something(r)]
                not_started = [g.strip().rstrip(".") for g in not_started if g and g.strip()]
                tail = CONTINUE_AFTER_DECISION_NOTE
                if not_started:
                    tail = (NOT_STARTED_AFTER_DECISION_NOTE.format(goals="; ".join(not_started))
                            + "\n\n" + CONTINUE_AFTER_DECISION_NOTE)
                if (parked.output or "").strip():
                    if CONTINUE_AFTER_DECISION_NOTE not in parked.output:
                        parked.output = f"{parked.output}\n\n{tail}"
                else:
                    parked.output = tail
                deep_results = [r for r in deep_results
                                if r is parked
                                or (not getattr(r, "decision_id", None)
                                    and result_reports_something(r))]
                deep_results.sort(key=lambda r: 1 if r is parked else 0)

        return OrchestratorResult(
            kind="deep",
            deep_results=deep_results,
            goals=all_goals,
            rationale=plan.rationale,
        )
