"""Autopilot — the recurring "autopilot pass" task that scans opted-in quests and makes progress.

Design of record: ``quest_autopilot_design.md`` (Part B). In one sentence: Autopilot is itself a
recurring assistant task (``task_kind == "autopilot"``, routed by ``runner.executor`` before the
normal deep-run path). Each pass:

  1. Lists the team's quests; keeps the ones opted in (``autopilot.mode`` in
     ``suggest``/``act``/``reactive``).
  2. Gates each, cheapest first: a team-wide daily budget, per-quest cadence, REACTIVE mode's own
     "is there anything new to react to" check (see ``has_new_ask_since``), the roster's day
     rule (a quest whose roster names nobody for today does nothing at all today), and -- only
     where the deployment opts into backpressure -- an open autopilot-created task or an
     unresolved HOLD decision already sitting on the quest. By default neither of the last two
     stops a pass: work continues and the unfinished thing is visible in context to be worked
     around.
  3. Reads the quest's goals ONCE and turns them into the GOAL LADDER (``current_goal_ladder``):
     the person's current goals at EVERY horizon, day up to year, which every batch carries.
     Goals are CONTEXT here and nothing else. No goal is selected, assigned, or turned into work:
     what a run produces is what its persona's standing instructions say, and the ladder is what
     that output has to add up to. The one thing still derived from the goals payload is the
     quest's current SCOPE LABEL (``current_scope_label``), which decides which period review to
     read, which previous period to summarize, and what period a proposed goal is filed under.
  4. Decides WHO works the quest today, always INSIDE the day rule: a character works a quest only
     on the days their own roster entry names, and nothing overrides that. EVERY character the
     roster puts on duty today gets one batch (one budget unit). A quest with NO roster gets a
     single batch for the plain assistant, or for whoever a consumer-injected fallback resolver
     names. A roster entry flagged ``instructions_only`` is on duty and gets its batch like any
     other; the flag only keeps that character out of the routing that hands an UNASSIGNED
     recurring task to somebody, which is what lets a specialist share a roster with the character
     who carries the quest's ordinary work.
  5. Decides WHAT each batch is for, at TWO levels that default independently: the quest-wide
     ``autopilot.instructions`` (how to work here and how to deliver, for everyone on the quest)
     and the character's own roster ``instructions`` (what this character works on). Each slot
     takes the person's text where they wrote it and the backend's read-only default otherwise
     (``autopilot.default_quest_instructions`` / ``autopilot.default_persona_instructions``), so
     both slots are always filled and both always ride into the prompt, layered quest-wide first
     and character second. A character on duty therefore always has a specification instead of an
     empty run, and writing one level never silences the other. Recurring tasks the quest opted
     into adopting are folded into the batch of whoever they name.
  6. Creates each batch as a real task (``status="suggested"`` in suggest mode, ``"queued"`` in
     act mode), or -- when planning allows and no batch was produced at all -- proposes the quest's
     next goal instead of a work task, UNLESS the previous pass's proposal is still sitting there
     unanswered (a proposal is one question, and re-asking it every pass is how the same
     suggestion ends up in the person's list every morning). The batch text carries the person's
     OWN latest reflection
     (``runner.reflections``: the daily plan's review of yesterday, plus the newest submitted
     period review) and the insights assigned to THIS quest that they have not yet acted on since its
     last pass (``runner.insights``, with the person's own category tags shown beside each one).
     The reflection is read once per pass (user-scoped); the insights are read per quest, from the
     quest's own route, and never shared between quests. Everything
     else in that text is derived from rows the system recorded; those two are the parts the
     person wrote, so they are what break ties about what actually matters today.
  7. Updates the quest's ``autopilot.last_pass_at`` (and ``miss_streak`` when nothing was
     produced) via the quest update route.
  8. For a quest with a mapped local folder (``quest_folder_map``), REFRESHES that quest's
     canonical next-steps artifact (``quest_folder_sync.publish_next_steps``) with the conclusion
     the pass just reached, and READS the existing artifact into the batch text first. That closes
     a real gap: the pass and an attended session both have to answer "what is next for this
     quest", and before this they answered it separately, from different material, with no way to
     notice they had drifted apart.

WHAT A PASS REPORTS is what it set in motion, in plain words: the work by name, the quest by its
outcome, who it went to, and what is waiting on the person. Not "Created 1 task(s):
atask_d2014273cff6" -- that names an internal id where the work's name belongs, and presents the
scanner's own accounting as the outcome. The pass also stamps its OWN task id as ``parent_task_id``
on everything it creates, which is the link a consumer uses to replace this summary with the work's
actual output once that work finishes (quest-backend does this in
``app/business/quests/autopilot_rollup.py``). So the pass row ends up holding the work itself,
whether or not the quest mails anything, and the mail carries the same text.

A DRY RUN (the pass task's own text contains "dry-run") reports what WOULD be created and creates
nothing: no tasks, no goals, no bookkeeping writes.

Per-quest failures are isolated: one quest's error never aborts the rest of the pass (recorded in
the result instead), and every skipped quest is logged with its gate reason -- silent skips are
exactly the failure mode the qar playbook bans.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .context_updates import ContextUpdates, UpdateEngine
from .insights import InsightsContext
# The client's OWN period formats, borrowed rather than restated: a goal this module proposes is
# created through ``QuestClient.create_goal``, so the shape it must satisfy is that client's, and a
# second copy here would be a second thing to keep in step with the backend's period_utils.
from .quest_client import _PERIOD_RE as _GOAL_PERIOD_RE
from .goal_handoff import preferences_text, run_goal_handoff
from .reflections import ReflectionContext

from .autopilot_defaults import (
    AUTOPILOT_PASS_KIND,
    AUTOPILOT_WORK_KIND,
    BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS,
    BUNDLED_DEFAULT_QUEST_INSTRUCTIONS,
    DEFAULT_ASSISTANT_REP_ID,
    DEFAULT_LADDER_PER_SCOPE,
    DEFAULT_TEAM_DAILY_BUDGET,
    LAST_RUN_OUTPUT_MAX_CHARS,
    MAX_INSTRUCTIONS_CHARS,
    OPEN_TASK_STATUSES,
    PROPOSAL_TEXT_PREFIX,
    log,
)
from .autopilot_periods import (
    _current_period_key,
    _parse_dt,
    cadence_due,
    current_goal_ladder,
    current_scope_label,
    has_new_ask_since,
    previous_period_bounds,
    previous_period_key,
    run_requested,
    select_period_goals,
)
from .autopilot_personas import (
    PERSONA_HELD,
    default_persona_instructions_for,
    default_quest_instructions_for,
    persona_entries_on_duty,
    persona_instructions_for,
    personas_on_duty,
    resolve_persona,
    resolve_task_persona,
    split_held_for_another_day,
)
from .autopilot_briefs import (
    _batch_title,
    compose_batch_text,
    next_steps_from_pass,
    render_last_run_output,
    select_last_run_output,
)
from .autopilot_result import AutopilotResult, _quest_label, _quest_pass_lock, _task_label
from .autopilot_gates import GatesMixin
from .autopilot_work import WorkMixin
from .quest_asks_brief import asks_brief_section, fetch_open_decisions
from .quest_experiments import (
    experiments_brief_section, fetch_experiments, has_experiment, proposal_is_open,
)
from .autopilot_context import PassContextMixin




class AutopilotPass(GatesMixin, WorkMixin, PassContextMixin):
    """Runs ONE autopilot pass against a Quest client. See the module docstring for the algorithm.

    ``client`` is a ``QuestClient`` (or any object with the same methods this class calls:
    ``list_quests``, ``get_quest_autopilot``, ``list_quest_goals``, ``list_tasks``,
    ``list_open_decisions_for_quest``, ``create_task``, ``update_task``,
    ``update_quest_autopilot``, and optionally ``create_goal``, ``get_daily_reflection``,
    ``get_period_reflection`` and ``list_quest_insights`` -- a
    client missing the optional ones simply composes a batch without that material, exactly as
    before they existed).

    ``persona_resolver`` is the consumer-injected fallback (step 4 of ``resolve_persona``) -- e.g.
    the personal lane's card-vote resolver. Given the item being routed (an adopted task, or ``{}``
    when the batch is not about one), returns a rep_id or ``None``. It is reached only on a quest
    whose roster names no work-routing character.

    ``quest_folder_map`` (``{quest_id: folder}``, from ``RunnerConfig``) opts a quest into the
    canonical next-steps artifact: the pass reads the folder's standing answer into the batch it
    creates, and writes its own conclusion back over it (locally and on Quest). Without a map the
    pass behaves exactly as before.

    ``update_engine`` (a ``runner.context_updates.UpdateEngine``, from ``RunnerConfig``) is what
    checks the channels this class does NOT already hand-wire -- notes the person added to the
    quest, comments on a document this work owns, a folder that changed. It is asked once per
    quest, per pass, for exactly what has changed since it last looked (see
    ``runner/context_updates.py``); the result rides into every batch this quest produces this
    pass, as its own ref-tagged block, and the engine's watermark for this card only advances once
    a batch carrying it was actually created (never on a dry run, and never merely because the
    engine was asked). Left ``None``, a pass behaves exactly as it did before this existed: this
    class still reads its own reflections and insights exactly as before, unaffected.
    """

    def __init__(self, client: Any, *, team_id: str = "",
                 team_resolver: Optional[Callable[[str], str]] = None,
                 team_ids: Optional[List[str]] = None,
                 persona_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                 daily_budget: int = DEFAULT_TEAM_DAILY_BUDGET,
                 backpressure: bool = False,
                 adopt_recurring_default: Optional[bool] = None,
                 quest_folder_map: Optional[Dict[str, str]] = None,
                 pending_work: Optional[Dict[str, Any]] = None,
                 update_engine: Optional[UpdateEngine] = None,
                 lane_user_id: Optional[str] = None,
                 judge: Optional[Callable[[str], str]] = None,
                 now: Optional[Callable[[], datetime]] = None):
        self._client = client
        # The account this lane authenticates as (RunnerConfig.lane_user_id), stamped as the
        # assignee of every task this pass creates. WHY (incident 2026-09-20): a created task with
        # no assignee gets the QUEST'S OWNER as its executor, and task discovery is owner-scoped,
        # so on a human-owned quest the pass filed its work batch and no lane could ever see it.
        # The funding quest's passes ran, created their batch, and the batch sat `queued` for days
        # -- so no result existed, and the quest mailer had nothing to send. The poller already
        # stamps this on the PASS row for exactly this reason (see _create_quest_pass); the work
        # the pass creates needs it just as much. None keeps the old default-executor behaviour.
        self._lane_user_id = (lane_user_id or "").strip() or None
        self._update_engine = update_engine
        # prompt -> raw text; used to pick a new goal's assignee and to judge AI handoff. None keeps
        # the deterministic behaviour (and skips the handoff pass).
        self._judge = judge
        self._team_id = team_id or ""
        # Which team a given quest's work belongs on, for a lane that serves several teams
        # (RunnerConfig.team_ids). ``None`` -- the default, and what every existing consumer gets
        # -- means every quest resolves to ``team_id``, exactly as before this existed.
        #
        # It is a RESOLVER rather than per-run state on purpose: one AutopilotPass instance is
        # shared across the poller's thread pool, so concurrent passes for different quests run
        # through this same object. A mutable "current team" attribute would be a data race that
        # files one quest's work on another quest's team, which is precisely the bug this field
        # exists to fix.
        self._team_resolver = team_resolver
        # The teams a TEAM-WIDE pass (one with no single target quest) should list quests from.
        # Empty = just ``team_id``, unchanged.
        self._team_ids: List[str] = [str(t) for t in (team_ids or []) if t]
        self._persona_resolver = persona_resolver
        self._daily_budget = daily_budget if daily_budget and daily_budget > 0 else DEFAULT_TEAM_DAILY_BUDGET
        self._backpressure = bool(backpressure)
        self._adopt_recurring_default = adopt_recurring_default
        # ``{quest_id: folder}`` (RunnerConfig.quest_folder_map). A quest with a folder gets the
        # next-steps artifact read and refreshed; one without is unaffected.
        self._quest_folder_map: Dict[str, str] = {
            str(k): str(v) for k, v in (quest_folder_map or {}).items() if v}
        # ``{quest_id: {"file", "label"}}`` (RunnerConfig.autopilot_pending_work): work waiting on a
        # quest that makes it due even when its cadence is not (see ``runner/autopilot_pending.py``).
        self._pending_work_specs: Dict[str, Any] = dict(pending_work or {})
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._persona_names: Dict[str, str] = {}   # rep_id -> display name, resolved once per pass
        # Reflections are USER-scoped, not per quest, so every quest in a pass would otherwise
        # re-fetch the identical text. Cached per requested period order, for the pass's lifetime
        # only -- a long-lived cache would be a stale one the moment the person writes a review.
        self._reflection_cache: Dict[Tuple[str, ...], ReflectionContext] = {}
        # Insights are USER-scoped too, so the pass reads them ONCE, over the widest window it
        # could need, and each quest narrows that one result to its own ``last_pass_at`` in memory.
        # ``None`` means "not read yet" (an empty context is a legitimate result and must not be
        # mistaken for a cache miss).
        # Keyed by (quest id, since): insights are assigned to a quest, so one quest's set is never
        # served to another.
        self._insights_cache: Dict[Tuple[str, Optional[datetime]], InsightsContext] = {}
        # The id of the pass task currently being run, set in ``run`` and stamped onto everything
        # this pass creates (see _create_autopilot_task). None until a pass starts.
        self._pass_task_id: Optional[str] = None

    # --- the pass --------------------------------------------------------------------------

    def run(self, task: Dict[str, Any]) -> AutopilotResult:
        """Run one pass, never concurrently with another pass on the same quest (see
        ``_QUEST_PASS_LOCKS``)."""
        with _quest_pass_lock(str(task.get("goal_id") or "")):
            return self._run_pass(task)

    def _run_pass(self, task: Dict[str, Any]) -> AutopilotResult:
        text = str(task.get("text") or task.get("title") or "")
        dry_run = "dry-run" in text.lower()
        result = AutopilotResult(ran_at=self._now(), dry_run=dry_run)
        # The pass task's OWN id, stamped onto everything this pass creates as ``parent_task_id``.
        # That link is what lets a consumer answer "what did autopilot actually do" with the work
        # itself rather than with a list of ids: the created task's result can be rolled back onto
        # the pass row that made it. Without it, the two rows are unrelated as far as the data is
        # concerned, and the pass can only ever report its own bookkeeping.
        self._pass_task_id = str(task.get("id") or task.get("task_id") or "") or None
        self.asks_cache = {}  # quest_id -> ask rows, read once per pass (see ``quest_asks``)
        # A per-quest pass (the hybrid schedule, quest_autopilot_design.md's autopilot spec
        # section A) carries its quest's id in ``goal_id`` -- the team pass carries none. Scope
        # this run to exactly that one quest instead of scanning the whole team.
        only_quest_id = str(task.get("goal_id") or "") or None

        quests = self._eligible_quests(only_quest_id)
        # THE DAILY BUDGET IS PER TEAM, not per lane, and that is a deliberate decision rather
        # than a detail of the implementation. A lane serving three teams is an efficiency of
        # deployment: it must not silently divide each team's daily allowance by three, which is
        # what one shared counter would do -- every team would go quiet earlier than its own
        # setting says, with nothing anywhere to explain why. So the counter is keyed by the
        # quest's own team, and each team's denominator is read from its own task list, once,
        # the first time a quest on it is reached. On a single-team lane there is exactly one key
        # and exactly one count call, as before.
        budget_by_team: Dict[str, int] = {}

        for quest in quests:
            quest_id = str(quest.get("quest_id") or quest.get("id") or "")
            if not quest_id:
                continue
            label = _quest_label(quest, quest_id)
            team = self._team_for(quest_id)
            if team not in budget_by_team:
                budget_by_team[team] = (
                    0 if dry_run else self._count_autopilot_tasks_today(team_id=team))
            budget_used = budget_by_team[team]
            try:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id, label,
                              f"today's budget of {self._daily_budget} autopilot task(s) is used up")
                    continue
                pending = self._pending_work(quest_id)
                gate_reason = self._gate_quest(quest, quest_id, pending)
                if gate_reason:
                    self._skip(result, quest_id, label, gate_reason)
                    continue
                if pending and pending.count > 0:
                    result.pending_work.append({
                        "quest_id": quest_id, "quest_label": label,
                        "count": pending.count, "label": pending.label})
                budget_used = self._run_one_quest(quest, quest_id, dry_run, budget_used, result)
                budget_by_team[team] = budget_used
            except Exception as e:  # noqa: BLE001 -- one quest's failure never aborts the pass
                log.error("autopilot: quest %s pass failed: %s", quest_id, e, exc_info=True)
                result.errors.append({"quest_id": quest_id, "quest_label": label,
                                      "error": f"{type(e).__name__}: {e}"})
        return result

    def _run_one_quest(self, quest: Dict[str, Any], quest_id: str, dry_run: bool,
                       budget_used: int, result: AutopilotResult) -> int:
        autopilot_cfg = quest.get("autopilot") or {}
        mode = str(autopilot_cfg.get("mode") or "off")
        planning = str(autopilot_cfg.get("planning") or "work_only")

        # Standing instructions: read once, near the top, defensively truncated at the same cap
        # the backend enforces on write (values written before the cap existed are the only way
        # this ever fires in practice). Never logged verbatim -- it is the person's private
        # content and can run to 8 KB.
        instructions = str(autopilot_cfg.get("instructions") or "").strip()
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            log.warning("autopilot: quest %s instructions truncated from %d to %d characters",
                       quest_id, len(instructions), MAX_INSTRUCTIONS_CHARS)
            instructions = instructions[:MAX_INSTRUCTIONS_CHARS]
        if instructions:
            log.info("autopilot: quest %s has standing instructions (%d chars)",
                     quest_id, len(instructions))
        # The floor under each instruction level, read once and independently: the quest-wide one
        # fills the quest slot when this quest wrote nothing, the persona one fills each
        # character's slot when that character has nothing of their own. The backend serves the
        # authoritative texts on the quest payload; these degrade to the bundled copies against an
        # older one.
        default_quest_instructions = default_quest_instructions_for(autopilot_cfg)
        default_persona_instructions = default_persona_instructions_for(autopilot_cfg)

        goals_payload = self._client.list_quest_goals(
            quest_id, team_id=self._team_for(quest_id) or None) or {}
        # Which period this quest is planning in. A LABEL only: it decides which period review to
        # read, what "the previous period" means here, and what period a proposed goal is filed
        # under. It selects no work, because goals are not work.
        scope_label = current_scope_label(goals_payload, self._now())
        # THE ONLY WAY GOALS REACH A RUN: the person's current goals at every horizon, built from
        # the payload already fetched (no extra call) and carried into EVERY batch below. Nothing
        # here is this run's assignment; it is the picture the run's output has to add up to, and
        # a run working standing instructions is exactly the run that would otherwise know nothing
        # about what the person is aiming at.
        goal_ladder = current_goal_ladder(goals_payload, self._now())
        # The finest current horizon's goals, for the quest's next-steps artifact below. That
        # artifact answers "what is next for this quest", and the person's own goals at the scope
        # the quest is working in are that answer, restated rather than re-derived.
        current_goals = list(goal_ladder[0]["goals"]) if goal_ladder else []

        # Human-assigned goals the assignee's AI rep could handle (best-effort, act mode only).
        if self._judge is not None and mode == "act" and not dry_run:
            try:
                run_goal_handoff(self._client, self._judge, goals_payload,
                                 team_id=self._team_for(quest_id) or None,
                                 outcome=str(quest.get("outcome") or ""),
                                 current_state=str(quest.get("current_state") or ""),
                                 preferences=preferences_text(quest.get("preferences")))
            except Exception:  # noqa: BLE001
                log.info("autopilot: goal handoff failed for quest %s", quest_id, exc_info=True)

        # Recurring tasks the user set up on this quest. Adopted ONLY when the quest opts in:
        # taking over a task someone scheduled themselves is a real change in who executes it.
        adopted = (self._due_recurring_tasks(quest_id)
                   if self._adopts_recurring(autopilot_cfg) else [])
        # An adopted task becomes a batch for whoever it names, so it is under the day rule too.
        # Held means simply NOT adopted: the occurrence stays queued and runs as the person
        # scheduled it, which is the same direction ``_close_adopted`` fails in (duplicated work
        # is recoverable, lost work is not).
        adopted, held_adopted = split_held_for_another_day(adopted, autopilot_cfg, self._now())
        if held_adopted:
            log.info("autopilot: quest %s -- leaving %d recurring task(s) %s unadopted: their "
                     "character is not rostered for %s",
                     quest_id, len(held_adopted),
                     [str(t.get("id") or t.get("task_id") or "?") for t in held_adopted],
                     self._now().strftime("%A"))
        # Fetched ONCE and shared by both consumers below. Before this, ``_previous_period_summary``
        # called ``list_tasks(goal_id=quest_id)`` (a task's ``goal_id`` holds the QUEST id, a
        # historical misnomer this is not the place to fix) entirely on its own; adding a second
        # consumer that needs the identical list is not a reason to add a second identical call.
        # Approach (a) of the two considered: fetch here and hand the list to both
        # ``_previous_period_summary`` (bounded to the previous calendar period) and
        # ``_last_run_output`` (no period window, most-recent-only), rather than (b) one method
        # doing both jobs and returning a pair. (a) keeps each method answering the ONE question
        # its name says it answers, testable and readable on its own, and the two questions
        # genuinely have different selection rules (a window vs. a pure recency-with-rollup-
        # preference rule) that read worse interleaved in one body than side by side as two
        # top-level calls. Best-effort exactly as before: a failed read degrades BOTH consumers
        # the same way ``_previous_period_summary`` alone used to degrade on its own failed read
        # (goals-only previous, and now also no last-run block) rather than losing anything or
        # aborting the pass.
        try:
            quest_tasks: Optional[List[Dict[str, Any]]] = self._client.list_tasks(
                team_id=self._team_for(quest_id) or None, goal_id=quest_id) or []
        except Exception:  # noqa: BLE001 -- goals-only summary (no last-run block) beats no pass
            log.info("autopilot: could not read prior tasks for quest %s", quest_id, exc_info=True)
            quest_tasks = None
        previous = self._previous_period_summary(quest_id, goals_payload, scope_label, quest_tasks)
        # The previous run's own output, independent of any period boundary -- see
        # ``select_last_run_output`` for why a period-scoped view alone cannot see it (a
        # week/month/quarter/year-scoped quest's own last run sits inside its CURRENT period, and
        # an unscoped quest gets no previous-period view at all). May mutate ``previous`` in place
        # to drop a duplicate stub; see ``_last_run_output``'s own docstring.
        last_run_text = self._last_run_output(quest_tasks or [], previous)
        # The standing artifact, read BEFORE this pass overwrites it: whatever the last refresh
        # concluded (possibly an attended session's, more recent than any pass) rides into the
        # batch as the plan of record.
        standing_next_steps = self._read_next_steps(quest_id)
        # The quest's experiments, read ONCE per quest (None = could not be read, which renders as
        # no section at all, never as "no experiments"). Whether a proposal is already waiting is
        # asked only when it can matter: the quest has no experiment and may plan.
        quest_experiments = fetch_experiments(self._client, quest_id)
        experiment_proposal_open = (
            quest_experiments is not None and not has_experiment(quest_experiments)
            and planning == "plan_and_work" and proposal_is_open(self._client, quest_id))
        # Why the quest exists, read ONCE per quest from the server (no model call there; empty for an
        # unsorted quest or a person with no intents, so those cost nothing). A client that cannot ask
        # or a failed read gives no section, never a guess.
        intent_frame_text = self.read_intent_frame(quest_id)
        # One batch per pass may be the one that proposes; a second character's batch is told a
        # proposal is already being handled instead of raising a duplicate.
        experiment_proposal_offered = False
        # What people asked of the AI on this quest (and what already waits on a person), read ONCE
        # per quest and rendered per batch. An unreadable ledger renders as no section.
        asked_text = asks_brief_section(self.quest_asks(quest_id),
                                        fetch_open_decisions(self._client, quest_id)) or None
        quest_label = _quest_label(quest, quest_id)
        context_bundle: Optional[ContextUpdates] = None
        context_updates_text: Optional[str] = None
        if self._update_engine is not None:
            # ONE read of everything the person said, through the engine. The reflection and the
            # captures come back as SLOTTED updates: they render through the composer's own two
            # slots below (ref-tagged, so the receipt accounts for them) and are excluded from the
            # general block, which carries everything else -- notes, document comments, folder
            # edits. Before this, the pass read the two slots itself AND the engine delivered them
            # again in the block, so every brief carried the reflection twice.
            context_bundle = self._update_engine.collect(
                quest, card_id=quest_id, card_kind="quest", card_label=quest_label,
                options={"reflections": {"periods": self._reflection_periods(scope_label)}})
            reflection_text = context_bundle.slot_text("reflection") or None
            insights_text = context_bundle.slot_text("insights") or None
            reflection_note = context_bundle.slot_summary("reflection")
            insights_note = context_bundle.slot_summary("insights")
            context_updates_text = context_bundle.as_prompt_block(
                exclude_slots=("reflection", "insights")) or None
        else:
            # No engine: the two slots are read directly, exactly as before the engine existed.
            # What the person themselves last wrote about their work (user-scoped, cached for the
            # pass), and what they captured since this quest's own last pass.
            reflections = self._reflections(scope_label)
            reflection_text = reflections.as_text() or None
            insights = self._insights(autopilot_cfg, quest_id)
            insights_text = insights.as_text() or None
            reflection_note, insights_note = reflections.one_line(), insights.one_line()

        produced = False
        # Whether a batch CARRYING this pass's context updates was actually created. Separate from
        # ``produced`` on purpose: a dry run, a goal proposal and a budget-starved pass all leave
        # ``produced`` in a state that says nothing about whether the person's comment reached a
        # run, and the watermark may only move when it did (see ``ContextUpdates.mark_seen``).
        context_delivered = False
        # THE ALWAYS-WORK RULE, and it is now unconditional for a quest that reaches this point.
        # Every character on duty has an effective brief at BOTH levels -- what the person wrote
        # for that level, or the built-in default for it -- so every character on duty gets ONE
        # batch per due pass. A quest with three unrestricted personas therefore produces three
        # batches a pass, and the only thing that bounds that is the team's daily budget, which is
        # checked per batch inside the loop.
        #
        # A quest with NO roster at all gets a single plain-assistant batch on the two defaults, so
        # a quest somebody switched autopilot on for and then configured no further still does
        # something useful instead of going quiet.
        #
        # The consequence for the goal-proposal ``elif`` below is that it no longer fires: with a
        # default brief there is no such thing as a due pass with nothing to work. See the comment
        # there for why the path is kept rather than deleted.
        batches = self._batches_for_pass(autopilot_cfg, adopted)
        if batches:
            for persona, tasks in batches:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id, quest_label,
                               f"today's budget of {self._daily_budget} autopilot task(s) ran out "
                               f"part-way through this quest")
                    break
                # Resolved per BATCH, from the whole roster rather than today's on-duty entries.
                # Every character routed a batch is on duty today (the day rule guarantees it),
                # but their instructions can sit on a DIFFERENT entry than the one that put them
                # on duty -- a day-restricted entry carrying the brief plus a catch-all, say -- so
                # reading the whole roster is what makes which entry irrelevant.
                persona_instructions = persona_instructions_for(autopilot_cfg, persona)
                # The roster key is not always a character. A quest rostered to the built-in
                # default assistant carries DEFAULT_ASSISTANT_REP_ID, which is read for its days
                # and its instructions above (both of which are the person's own words) and then
                # resolved HERE to whoever the consumer's fallback names, or to nobody. Everything
                # downstream -- the task's persona, its label, a dry run's report -- must see that
                # resolved value, never the marker, or the pass would file work against a rep that
                # does not exist.
                persona = self._batch_persona(persona)
                # Titled from what the PERSON wrote, composed from what actually governs the run.
                # A default is identical on every unconfigured quest, so titling from one would
                # name every task and every mail subject after the same built-in first line.
                title = _batch_title(tasks, instructions=instructions,
                                     persona_instructions=persona_instructions)
                if dry_run:
                    result.proposals.append({
                        "quest_id": quest_id, "quest_label": quest_label, "kind": "work_batch",
                        "persona": persona, "persona_label": self._persona_label(persona),
                        "scope": scope_label,
                        "adopted_task_ids": [t.get("id") or t.get("task_id") for t in tasks],
                        # Which brief is driving this batch. A dry run is read by someone checking
                        # that the right character was picked for the right job, and "standing
                        # instructions" alone does not say whose -- which is the only question a
                        # two-character roster raises.
                        "instructions_from": self.instructions_source(
                            persona, persona_instructions, instructions),
                    })
                    produced = True
                    # A dry-run still SIMULATES budget consumption (one unit per batch that
                    # WOULD be created), so the report honestly shows a later quest going quiet
                    # once the budget is exhausted -- exactly what a real pass would do.
                    budget_used += 1
                    continue
                experiments_text = experiments_brief_section(
                    quest_experiments, quest_id=quest_id, planning=planning,
                    proposal_open=experiment_proposal_open,
                    may_propose=not experiment_proposal_offered) or None
                task_id = self._create_batch_task(quest, quest_id, persona, mode,
                                                  title=title,
                                                  scope_label=scope_label, adopted_tasks=tasks,
                                                  next_steps=standing_next_steps,
                                                  previous=previous,
                                                  last_run=last_run_text,
                                                  reflection=reflection_text,
                                                  insights=insights_text,
                                                  context_updates=context_updates_text,
                                                  instructions=instructions,
                                                  persona_instructions=persona_instructions,
                                                  default_quest_instructions=(
                                                      default_quest_instructions),
                                                  default_persona_instructions=(
                                                      default_persona_instructions),
                                                  goal_ladder=goal_ladder,
                                                  experiments=experiments_text,
                                                  asked=asked_text,
                                                  intent_frame=intent_frame_text)
                if task_id:
                    experiment_proposal_offered = True
                    result.created.append({
                        "task_id": task_id, "kind": "work_batch", "quest_id": quest_id,
                        "quest_label": quest_label, "title": title,
                        "persona_label": self._persona_label(persona),
                        "awaiting_approval": mode != "act",
                        "adopted_titles": [_task_label(t) for t in tasks],
                    })
                    budget_used += 1
                    produced = True
                    if context_bundle is not None:
                        # So the run's receipt can close what it accounts for (see
                        # ``ContextUpdates.remember_offer``).
                        context_bundle.remember_offer(task_id)
                    if context_updates_text:
                        context_delivered = True
                    self._close_adopted(tasks, task_id, quest_id, result)
            # The receipt for the person: what they wrote has now been handed to a real run, so
            # the next pass will not offer it again. Only here -- never at collection time, never
            # on a dry run, and never merely because the engine was asked.
            if context_delivered and context_bundle is not None and not dry_run:
                context_bundle.mark_seen()
            if produced and not dry_run:
                self._refresh_next_steps(quest_id, current_goals, adopted, scope_label, previous,
                                         result, quest_label=quest_label,
                                         reflection_note=reflection_note,
                                         insights_note=insights_note)
        # The goal-proposal path, kept and unchanged, and NOT currently reachable from here: a due
        # pass always has at least one character with an effective brief, so ``batches`` is never
        # empty by the time this runs. It stays because it is a real product surface rather than
        # dead scaffolding -- quest-backend renders these proposals and re-declares
        # ``PROPOSAL_TEXT_PREFIX`` against this module's constant -- and because the condition that
        # makes it unreachable is the DEFAULT BRIEF, which is exactly the kind of thing a
        # deployment may switch off later. Its own helpers are still covered directly by tests.
        elif planning == "plan_and_work":
            if budget_used < self._daily_budget:
                skipped_because = self._handle_proposal(quest, quest_id, quest_label, mode,
                                                        dry_run, result,
                                                        scope_label=scope_label)
                if skipped_because:
                    # Nothing was created, so nothing is spent and nothing is claimed. The person
                    # still hears why the quest was quiet, in one line, instead of getting the
                    # same proposal again.
                    self._skip(result, quest_id, quest_label, skipped_because)
                else:
                    produced = True
                    budget_used += 1
            else:
                self._skip(result, quest_id, quest_label,
                           f"today's budget of {self._daily_budget} autopilot task(s) ran out "
                           f"part-way through this quest")

        if not dry_run:
            self._update_pass_bookkeeping(quest_id, quest_label, autopilot_cfg, produced, result)
        return budget_used
