"""Autopilot: one run of a quest's NAMED autopilot, executed inside the one ``autopilot_run`` task the backend queued for it.

A quest holds any number of named autopilots (``autopilot.autopilots``, each storing every setting it uses; read with ``list_autopilots``). The backend's
scheduler decides WHEN each runs and queues one task per run, naming the autopilot in the task's ``autopilot_id`` field; the runner keeps no series, no
schedule and no bookkeeping, because the run rows are the record. A run:

  1. Reads the autopilot by the task's field and skips, with the reason, when it does not run (paused, past the plan's cap, removed) or when a
     SCHEDULED run waited past its grace ("missed: the lane was down").
  2. Gates the run: the allowance for automatic extra runs (3 per autopilot a day and the team's budget; never a scheduled run or Run now) and,
     only where the deployment opts into backpressure, an unfinished earlier run of THIS autopilot or an unresolved HOLD decision on the quest.
  3. Asks its work source what is due (``source``, today ``learning``), or reads the quest once (goals as the GOAL FRAME, prior tasks, the plan of
     record, experiments, asks, the person's reflection, insights and context updates) and takes the goals the day's routing gave it
     (``autopilot_routing``: one recorded judgment per quest per day, a goal to one on-duty autopilot or to none). Nothing to work (no items, no
     goals, an empty prompt) closes the run as skipped, "no goals relevant to this scope".
  4. Composes ONE brief: the quest's outcome and "About this quest", then the autopilot's own words, the routed goals, the frame and, for a
     source, its bounds and what is due.
  5. Returns the plan the executor applies: ``skipped``, ``suggested`` (parked with the brief until a person approves) or ``act`` (the deep agent
     runs on the brief). For a quest with a mapped folder it also refreshes the canonical next-steps artifact.

A DRY RUN (the task's text contains "dry-run") reports the brief it WOULD work on and changes nothing.

Every skip is logged with its reason, because silent skips are exactly the failure mode the qar playbook bans.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from quest_ai_runner.body.intake_sources.context_updates import ContextUpdates, UpdateEngine
from quest_ai_runner.body.intake_sources.context_updates_file import index_block, write_context_file
from quest_ai_runner.intelligence.higher_mind.guidance.insights import InsightsContext
from quest_ai_runner.intelligence.higher_mind.objectives.goal_handoff import preferences_text, run_goal_handoff
from quest_ai_runner.intelligence.mind.context_intake.reflections import ReflectionContext

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import (  # noqa: F401 -- the names below are re-exported for the poller and the tests
    AUTOPILOT_PASS_KIND, DEFAULT_TEAM_DAILY_BUDGET, EXTRA_RUN_KEY_PREFIXES, MAX_EXTRA_RUNS_PER_AUTOPILOT, MAX_INSTRUCTIONS_CHARS,
    OPEN_TASK_STATUSES, log)
from quest_ai_runner.intelligence.proactivity.autopilot_periods import (  # noqa: F401
    task_lookback_start,
    _parse_dt, current_goal_ladder, current_scope_label)
from quest_ai_runner.intelligence.higher_mind.objectives.quest_folder_bind import read_map
from quest_ai_runner.intelligence.proactivity.autopilot_briefs import _batch_title, compose_batch_text, folder_first_step, next_steps_from_pass  # noqa: F401
from quest_ai_runner.intelligence.proactivity.autopilot_personas import (
    capped_instructions, default_persona_instructions_for, default_quest_instructions_for)
from quest_ai_runner.consciousness.request_intake.local_time import now_in_zone
from quest_ai_runner.intelligence.proactivity.autopilot_routing import route_goals
from quest_ai_runner.intelligence.proactivity.autopilot_units import AUTOPILOT_RUN_KIND, label_of, on_duty, unit_named, units_of
from quest_ai_runner.intelligence.proactivity.autopilot_sources import SOURCES, source_section
from quest_ai_runner.intelligence.critical_thinking.esoi_application.autopilot_esoi import read_readiness, readiness_and_pick
from quest_ai_runner.intelligence.proactivity.autopilot_result import RUN_BRIEF_HEADER, AutopilotResult, RunPlan, _quest_label, _quest_pass_lock, _task_label
from quest_ai_runner.intelligence.proactivity.autopilot_budget_ask import budget_stop_escalation
from quest_ai_runner.intelligence.proactivity.autopilot_gates import GatesMixin
from quest_ai_runner.intelligence.proactivity.autopilot_work import WorkMixin
from quest_ai_runner.intelligence.proactivity.quest_asks_brief import asks_brief_section, fetch_open_decisions
from quest_ai_runner.intelligence.higher_mind.objectives.quest_experiments import (
    experiments_brief_section, fetch_experiments, has_experiment, proposal_is_open)
from quest_ai_runner.intelligence.proactivity.autopilot_context import PassContextMixin




log = logging.getLogger("quest-ai-runner.autopilot")

DRY_RUN_TEXT_MAX = 400   # an autopilot's purpose is at most 400 characters (the backend's AUTOPILOT_PURPOSE_MAX_CHARS); longer text is standing instructions, never a request


def is_dry_run_task(task: Dict[str, Any]) -> bool:
    """A dry run is asked for by a SHORT task text (or title) containing "dry-run". A run whose text is the autopilot's whole instructions (its
    purpose was blank, so the backend used them) may mention "dry-run" in passing and must still run for real."""
    text = str(task.get("text") or "").strip()
    if text and len(text) <= DRY_RUN_TEXT_MAX:
        return "dry-run" in text.lower()
    return not text and "dry-run" in str(task.get("title") or "").lower()


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
                 quest_folder_binding: Optional[Dict[str, str]] = None,
                 update_engine: Optional[UpdateEngine] = None,
                 lane_user_id: Optional[str] = None,
                 judge: Optional[Callable[[str], str]] = None,
                 now: Optional[Callable[[], datetime]] = None,
                 structure_watch: Optional[Callable[[str, bool], Any]] = None,
                 escalation: Optional[Any] = None):
        self._client = client
        # The runner's escalation sink (``RunnerConfig.escalation``). Used for ONE thing here: the
        # visible ask raised when the daily budget stops a pass part-way (autopilot_budget_ask.py).
        # None keeps the old behaviour of a log line and a note only.
        self._escalation = escalation
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
        # ``watch(quest_id, dry_run)``: the hub's structure watch over the quest, once per run (see ``autopilot_structure_watch.watch_quest``); None leaves it off.
        self._structure_watch = structure_watch
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
        # {"map_file", "config_path", "root"} (RunnerConfig.quest_folder_map_file, config_file_path, new_quest_folder_root): when set, a run for a quest with
        # no folder starts by binding one (``quest_folder_bind``). None = off, exactly as before.
        self._quest_folder_binding = dict(quest_folder_binding) if quest_folder_binding and quest_folder_binding.get("map_file") else None
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
        # ``{(quest id, local date): routing rows}``: the day's goal routing, judged once per quest per day (see ``autopilot_routing``).
        self._routing_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    # --- the run ---------------------------------------------------------------------------

    def run(self, task: Dict[str, Any]) -> RunPlan:
        """Plan ONE ``autopilot_run`` task (never concurrently with another run on the same quest, see ``_QUEST_PASS_LOCKS``).

        The backend scheduled the run and queued this task, which carries its autopilot in the ``autopilot_id`` FIELD. The plan says what the executor
        does with it: ``skipped`` (close it, with the reason), ``suggested`` (park it with the composed brief until a person approves) or ``act``
        (run the deep agent on the composed brief, in the quest's folder). Nothing here creates a task: the run IS the task.
        """
        with _quest_pass_lock(str(task.get("goal_id") or "")):
            return self._plan_run(task)

    def _plan_run(self, task: Dict[str, Any]) -> RunPlan:
        dry_run = is_dry_run_task(task)
        quest_id = str(task.get("goal_id") or task.get("quest_id") or "")
        autopilot_id = str(task.get("autopilot_id") or "")
        self._pass_task_id = str(task.get("id") or task.get("task_id") or "") or None
        self.asks_cache = {}  # quest_id -> ask rows, read once per run (see ``quest_asks``)
        plan = RunPlan(quest_id=quest_id, autopilot_id=autopilot_id, dry_run=dry_run)
        if not quest_id or not autopilot_id:
            return plan.skip("the run names no quest or no autopilot")
        state = self._quest_state(quest_id)
        autopilot_cfg = dict((state.get("autopilot") or {}) if state else {})
        if state and state.get("archived"):
            return plan.skip("the quest is archived")
        # The list of autopilots has its own read (each autopilot stores every setting it uses, with a read-only ``status``), and with it the
        # quest-level fields they share: the "About this quest" brief, the zone, the environment and whether the quest is paused.
        reader = getattr(self._client, "list_autopilots", None)
        listed = (reader(quest_id) or {}) if callable(reader) else {}
        if listed.get("autopilots") is not None:
            autopilot_cfg = {**autopilot_cfg, **dict(listed.get("shared") or {}), "autopilots": listed["autopilots"]}
        unit = unit_named(autopilot_cfg, autopilot_id)
        if unit is None or unit["id"] not in {u["id"] for u in units_of(autopilot_cfg)}:
            return plan.skip(f"autopilot {autopilot_id} does not run (paused, past the plan's cap, removed, or its quest is paused)")
        plan.title = label_of(unit)
        quest = {"quest_id": quest_id, "outcome": state.get("outcome") or "", "autopilot": autopilot_cfg}
        reason = self._gate_run(task, unit, quest_id)
        if reason:
            return plan.skip(reason)
        if self._structure_watch is not None:
            self._structure_watch(quest_id, dry_run)      # proposals only, through decision requests; never an edit
        source_text = None
        source_name = str(unit.get("source") or "").strip()
        if source_name:
            source = SOURCES.get(source_name)
            if source is None:
                return plan.skip(f"this runner has no work source named {source_name!r}")
            items = list(source.due_items(self._now()))
            if not items:
                return plan.skip(f"nothing is due for {source.name}")
            source_text = source_section(source, items)
        ctx = None if source_name else self._read_quest_context(quest, quest_id, unit, dry_run)
        assigned = self._assigned_goals(quest, quest_id, unit, ctx) if ctx else []
        own = capped_instructions(unit.get("instructions"), f"autopilot {unit['id']}")
        if not own and not assigned and not source_text:
            return plan.skip("no goals relevant to this scope")
        text = self._compose_run(quest, quest_id, unit, ctx, assigned, own, source_text)
        plan.text = text
        plan.status = "suggested" if str(unit.get("mode") or "suggest") != "act" else "act"
        if ctx is not None and not dry_run:
            if ctx["bundle"] is not None:
                ctx["bundle"].remember_offer(self._pass_task_id or "")
                if ctx["context_updates"]:
                    # A run that will really execute only STAGES the advance: its watermarks move when the executor
                    # reports the task done (``settle_run_context``), so a failed run loses none of the news. A
                    # parked suggestion has nothing to fail, and its brief already points at the written file.
                    ctx["bundle"].mark_seen(task_id=self._pass_task_id if plan.status == "act" else None)
            if plan.status == "act":
                self._refresh_next_steps(quest_id, ctx["current_goals"], ctx["adopted"], ctx["scope_label"], ctx["previous"], plan.notes_sink,
                                         quest_label=_quest_label(quest, quest_id), reflection_note=ctx["reflection_note"], insights_note=ctx["insights_note"])
            if ctx["adopted"] and self._pass_task_id:
                self._close_adopted(ctx["adopted"], self._pass_task_id, quest_id, plan.notes_sink)
        return plan

    def settle_run_context(self, task_id: str, status: str) -> bool:
        """The executor's word on how a run ended: ``done`` commits the context watermarks this run staged, anything else drops them.

        Best-effort and safe to call for any task (a run that staged nothing is a no-op).
        """
        engine = self._update_engine
        if engine is None or not task_id:
            return False
        try:
            return bool(engine.settle(task_id, succeeded=str(status) == "done"))
        except Exception:  # noqa: BLE001 -- bookkeeping never fails a finished run
            log.info("autopilot: could not settle the context marks of task %s", task_id, exc_info=True)
            return False

    def _compose_run(self, quest: Dict[str, Any], quest_id: str, unit: Dict[str, Any], ctx: Optional[Dict[str, Any]],
                     assigned: List[str], own: str, source_text: Optional[str]) -> str:
        """The run's brief: ONE composition path for every autopilot. The quest's outcome and "About this quest", the autopilot's own words (named as
        the person's), the goals routed to it, the goal frame, and for a source run the source's bounds and what is due."""
        autopilot_cfg = quest.get("autopilot") or {}
        quest_brief = capped_instructions(autopilot_cfg.get("instructions"), f"quest {quest_id}")
        persona = self._unit_persona(unit)
        ctx = ctx or {}
        text = compose_batch_text(
            str(quest.get("outcome") or ""), self._persona_label(persona), scope_label=ctx.get("scope_label"),
            adopted_tasks=ctx.get("adopted"), next_steps=ctx.get("next_steps"), previous=ctx.get("previous"), last_run=ctx.get("last_run_text"),
            reflection=ctx.get("reflection"), insights=ctx.get("insights"),
            context_updates=ctx.get("context_updates"), instructions=quest_brief, persona_instructions=own,
            default_quest_instructions=None if source_text else default_quest_instructions_for(autopilot_cfg),
            default_persona_instructions=None, goal_ladder=ctx.get("goal_ladder"), routed_goals=assigned or None,
            experiments=(experiments_brief_section(ctx.get("experiments"), quest_id=quest_id, planning=str(unit.get("planning") or "work_only"),
                                                   proposal_open=bool(ctx.get("proposal_open")), may_propose=True) or None) if ctx else None,
            asked=ctx.get("asked"), intent_frame=ctx.get("intent_frame"), esoi_readiness=(readiness_and_pick(ctx.get("esoi_read"), own, unit.get("rep_id") or None)[0] if ctx else None),
            source_text=source_text)
        first_step = self._folder_first_step(quest, quest_id)
        return f"{RUN_BRIEF_HEADER}\n\n{first_step + chr(10) * 2 if first_step else ''}{text}"

    def set_quest_folder_map(self, quest_folder_map: Optional[Dict[str, str]]) -> None:
        """Replace the quest -> folder map (the poller re-reads the map file, so a folder bound by one run is known to the next without a restart)."""
        self._quest_folder_map = {str(k): str(v) for k, v in (quest_folder_map or {}).items() if v}

    def _folder_first_step(self, quest: Dict[str, Any], quest_id: str) -> str:
        """The "give this quest a folder" section, or "" when binding is off or the quest already has a folder (this lane's map, or the map file as it is now)."""
        binding = self._quest_folder_binding
        if not binding or self._quest_folder_map.get(str(quest_id)):
            return ""
        if read_map(binding["map_file"]).get(str(quest_id)):
            return ""
        return folder_first_step(quest_id, str(quest.get("name") or _quest_label(quest, quest_id)),
                                 config_path=binding.get("config_path") or "<lane config>", root=binding.get("root") or "")

    def _assigned_goals(self, quest: Dict[str, Any], quest_id: str, unit: Dict[str, Any], ctx: Dict[str, Any]) -> List[str]:
        """The goal lines routed to THIS autopilot today: the day's one judgment across the autopilots on duty (``autopilot_routing``), made once per
        quest per day and recorded on the quest (``PUT .../autopilots/routing/{day}``), so every run of that day, in this process or the next one after
        a restart, reads the same record instead of judging again."""
        autopilot_cfg = quest.get("autopilot") or {}
        zone = autopilot_cfg.get("run_timezone") or "UTC"      # the backend keys the day in the quest's zone, UTC when it names none
        today = now_in_zone(zone, self._now()).date().isoformat()
        key = (quest_id, today)
        if key not in self._routing_cache:
            rows = self._recorded_routing(quest_id, today)
            if rows is None:
                duty = [u for u in units_of(autopilot_cfg) if on_duty(u, self._now(), zone)]
                rows = route_goals(ctx["goals_payload"], duty, self._judge, self._now())
                self._record_routing(quest_id, today, rows)
            self._routing_cache[key] = rows
        return [row["line"] for row in self._routing_cache[key] if row.get("autopilot_id") == unit["id"]]

    def _recorded_routing(self, quest_id: str, day: str) -> Optional[List[Dict[str, Any]]]:
        """Today's routing as already recorded on the quest (rows with their goal line), or None when none was written (or it cannot be read)."""
        reader = getattr(self._client, "get_autopilot_routing", None)
        if not callable(reader):
            return None
        try:
            routes = (reader(quest_id, day) or {}).get("routes") or []
        except Exception:  # noqa: BLE001 -- an unreadable record means the day is judged again
            return None
        return [{**r, "line": str(r.get("goal_title") or r.get("goal_id") or "")} for r in routes] or None

    def _record_routing(self, quest_id: str, day: str, rows: List[Dict[str, Any]]) -> None:
        """Write the day's routing (each goal once, with its one-line reason) where the person can read it. Best effort: a failed write never fails a run."""
        writer = getattr(self._client, "put_autopilot_routing", None)
        if not callable(writer) or not rows:
            return
        try:
            writer(quest_id, day, [{k: r[k] for k in ("goal_id", "goal_title", "autopilot_id", "reason")} for r in rows])
        except Exception:  # noqa: BLE001
            log.warning("autopilot: could not record the goal routing for quest %s", quest_id, exc_info=True)

    def _gate_run(self, task: Dict[str, Any], unit: Dict[str, Any], quest_id: str) -> Optional[str]:
        """The run's gates, cheapest first. Returns a skip reason, or None when the run goes ahead.

        WHEN is the backend's (it only queues a run that is due, and only when ``only_when_new`` finds something new); what is left here is what the
        runner alone can see: the daily allowance for AUTOMATIC EXTRA runs (a run a reply reopened, a catch-up), at most 3 per autopilot a day and the
        team's budget, never a scheduled run and never "Run now"; and, only where the deployment opts into backpressure, an unfinished earlier run of
        THIS autopilot or an unresolved HOLD decision on the quest.
        """
        key = str(task.get("occurrence_key") or "")
        if not key.startswith(("run:",) + EXTRA_RUN_KEY_PREFIXES):
            # A SCHEDULED run that waited longer than its autopilot's grace was missed (the lane was down): the backend records the miss, so the late
            # run is not worked now. Run now and automatic extra runs are a person's or a reply's own moment and never expire.
            queued = _parse_dt(task.get("created_at"))
            grace = int(unit.get("grace_minutes") or 360)
            if queued is not None and queued + timedelta(minutes=grace) < self._now():
                return "missed: the lane was down"
        if key.startswith(EXTRA_RUN_KEY_PREFIXES):
            used = self._extra_runs_today(quest_id, unit["id"], str(task.get("id") or task.get("task_id") or ""))
            if used["autopilot"] >= MAX_EXTRA_RUNS_PER_AUTOPILOT:
                return f"{MAX_EXTRA_RUNS_PER_AUTOPILOT} extra runs of this autopilot already happened today"
            if used["team"] >= self._daily_budget:
                return f"today's budget of {self._daily_budget} extra autopilot run(s) is used up"
        if self._backpressure and self._has_backpressure(quest_id, unit["id"], str(task.get("id") or task.get("task_id") or "")):
            return "backpressure: an earlier run of this autopilot is still open"
        if self._backpressure and self._has_open_hold_decision(quest_id):
            return "an open HOLD decision is pending on this quest"
        return None

    def _read_quest_context(self, quest: Dict[str, Any], quest_id: str, unit: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
        """Everything the quest-reading autopilots share, read ONCE per pass: goals, prior tasks, the plan of record, experiments, the ledger of asks."""
        autopilot_cfg = quest.get("autopilot") or {}
        modes = {str(unit.get("mode") or "suggest")}
        planning_any = str(unit.get("planning") or "work_only") == "plan_and_work"
        goals_payload = self._client.list_quest_goals(
            quest_id, team_id=self._team_for(quest_id) or None) or {}
        # Which period this quest is planning in. A LABEL only: it decides which period review to read, what "the previous period" means here, and
        # what period a proposed goal is filed under. It selects no work, because goals are not work.
        scope_label = current_scope_label(goals_payload, self._now())
        # The person's current goals at every horizon, built from the payload already fetched. Orientation for the run, never its assignment: the
        # goals routed to an autopilot's rep are the separate ``routed_goal_lines``.
        goal_ladder = current_goal_ladder(goals_payload, self._now())
        current_goals = list(goal_ladder[0]["goals"]) if goal_ladder else []

        # Human-assigned goals the assignee's AI rep could handle (best-effort, act mode only).
        if self._judge is not None and "act" in modes and not dry_run:
            try:
                run_goal_handoff(self._client, self._judge, goals_payload,
                                 team_id=self._team_for(quest_id) or None,
                                 outcome=str(quest.get("outcome") or ""),
                                 current_state=str(quest.get("current_state") or ""),
                                 preferences=preferences_text(quest.get("preferences")))
            except Exception:  # noqa: BLE001
                log.info("autopilot: goal handoff failed for quest %s", quest_id, exc_info=True)

        # Recurring tasks the user set up on this quest. Adopted ONLY when the quest opts in: taking over a task someone scheduled themselves is a
        # real change in who executes it. Which autopilot takes each is ``_adopt_into_units``; one nobody running now can take stays queued.
        adopted = self._due_recurring_tasks(quest_id) if self._adopts_recurring(autopilot_cfg) else []
        adopted = [t for t in adopted if not t.get("assignee_rep_id") or str(t.get("assignee_rep_id")) == str(unit.get("rep_id") or "")]
        # Fetched ONCE and shared by both consumers below (the previous period and the last run's output). Best-effort: a failed read degrades both.
        try:
            quest_tasks: Optional[List[Dict[str, Any]]] = self._client.list_tasks(
                team_id=self._team_for(quest_id) or None, goal_id=quest_id, without_progress=True,
                created_after=task_lookback_start(scope_label, self._now())) or []   # bounded: a full-history read was ~7 MB a call
        except Exception:  # noqa: BLE001 -- goals-only summary (no last-run block) beats no pass
            log.info("autopilot: could not read prior tasks for quest %s", quest_id, exc_info=True)
            quest_tasks = None
        previous = self._previous_period_summary(quest_id, goals_payload, scope_label, quest_tasks)
        # The previous run's own output, independent of any period boundary (see ``select_last_run_output``).
        last_run_text = self._last_run_output(quest_tasks or [], previous, str(unit['id']))
        # The standing artifact, read BEFORE this pass overwrites it.
        standing_next_steps = self._read_next_steps(quest_id)
        # The quest's experiments, read ONCE (None = could not be read, which renders as no section at all, never as "no experiments").
        quest_experiments = fetch_experiments(self._client, quest_id)
        experiment_proposal_open = (
            quest_experiments is not None and not has_experiment(quest_experiments)
            and planning_any and proposal_is_open(self._client, quest_id))
        # Why the quest exists, read ONCE per quest from the server (no model call there).
        intent_frame_text = self.read_intent_frame(quest_id)
        # Read once per quest per pass, and only for a real run: a dry run must not refresh a specification.
        esoi_read = None if dry_run else read_readiness(self._client, quest_id)
        # What people asked of the AI on this quest (and what already waits on a person), read ONCE per quest and rendered per batch.
        asked_text = asks_brief_section(self.quest_asks(quest_id),
                                        fetch_open_decisions(self._client, quest_id)) or None
        quest_label = _quest_label(quest, quest_id)
        bundle: Optional[ContextUpdates] = None
        context_updates_text: Optional[str] = None
        reflection_text = insights_text = None
        reflection_note = insights_note = ""
        if self._update_engine is not None:
            # ONE read of everything the person said, through the engine. The reflection and the captures come back as SLOTTED updates that render
            # through the composer's own two slots (ref-tagged, so the receipt accounts for them); the general block carries everything else.
            # The notes read knows which autopilot this run is: a note aimed at another autopilot stays out, one aimed at this one leads the block.
            folder = self._quest_folder_map.get(str(quest_id)) or ""
            bundle = self._update_engine.collect(
                quest, card_id=quest_id, card_kind="quest", card_label=quest_label,
                consumer=str(unit.get("id") or ""), folder=folder,
                options={"reflections": {"periods": self._reflection_periods(scope_label)},
                         "quest_notes": {"autopilot_id": str(unit.get("id") or "")}})
            reflection_text = bundle.slot_text("reflection") or None
            insights_text = bundle.slot_text("insights") or None
            reflection_note = bundle.slot_summary("reflection")
            insights_note = bundle.slot_summary("insights")
            context_updates_text = bundle.as_prompt_block(exclude_slots=("reflection", "insights")) or None
            if folder and bundle.updates and not dry_run:
                # With a working folder the news is written there IN FULL and the brief carries a short index; without one (or on a dry run, which writes
                # nothing) the old inline block stands.
                written = write_context_file(bundle, folder, consumer=str(unit.get("id") or ""))
                if written is not None:
                    context_updates_text = index_block(bundle, written)
        else:
            # No engine: the reflection and the captures are read directly. The captures are the ones taken since THIS autopilot's own previous run
            # (the newest earlier ``autopilot_run`` row of it), so each autopilot has its own window.
            reflections = self._reflections(scope_label)
            reflection_text = reflections.as_text() or None
            reflection_note = reflections.one_line()
            earlier = [_parse_dt(t.get("created_at")) for t in (quest_tasks or [])
                       if t.get("task_kind") == AUTOPILOT_RUN_KIND and str(t.get("autopilot_id") or "") == str(unit["id"])
                       and str(t.get("id") or t.get("task_id") or "") != (self._pass_task_id or "")]
            latest = max((d for d in earlier if d is not None), default=None)
            captured = self._insights(latest.isoformat() if latest else None, quest_id)
            insights_text = captured.as_text() or None
            insights_note = captured.one_line()
        return {"goals_payload": goals_payload, "scope_label": scope_label, "goal_ladder": goal_ladder, "current_goals": current_goals,
                "adopted": adopted, "previous": previous, "last_run_text": last_run_text, "next_steps": standing_next_steps,
                "experiments": quest_experiments, "proposal_open": experiment_proposal_open, "intent_frame": intent_frame_text,
                "esoi_read": esoi_read, "asked": asked_text, "bundle": bundle, "context_updates": context_updates_text,
                "reflection": reflection_text, "insights": insights_text, "reflection_note": reflection_note, "insights_note": insights_note,
                "proposal_offered": False}
