"""Autopilot — the recurring "autopilot pass" task that scans opted-in quests and makes progress.

Design of record: ``quest_autopilot_design.md`` (Part B). In one sentence: Autopilot is itself a
recurring assistant task (``task_kind == "autopilot"``, routed by ``runner.executor`` before the
normal deep-run path). Each pass:

  1. Lists the team's quests; keeps the ones opted in (``autopilot.mode`` in ``suggest``/``act``).
  2. Gates each, cheapest first: a team-wide daily budget, per-quest cadence, backpressure (an
     open autopilot-created task already sitting on the quest), and an open HOLD decision on the
     quest.
  3. Picks the quest's CURRENT-SCOPE target goals (today's, this period's, or the single next
     incomplete one when the quest is unscoped) among the ones flagged ``ai_help``.
  4. Resolves a persona per goal (goal assignee -> quest persona-for-today -> a consumer-injected
     fallback) and batches goals sharing a persona into ONE task (one budget unit).
  5. Creates each batch as a real task (``status="suggested"`` in suggest mode, ``"queued"`` in
     act mode), or -- when planning allows and nothing is eligible -- proposes the quest's next
     goal instead of a work task. The batch text carries the person's OWN latest reflection
     (``runner.reflections``: the daily plan's review of yesterday, plus the newest submitted
     period review), read once per pass since reflections are user-scoped rather than per quest.
     Everything else in that text is derived from rows the system recorded; the reflection is the
     one part the person wrote, so it is what breaks ties about which eligible goal actually
     matters today.
  6. Updates the quest's ``autopilot.last_pass_at`` (and ``miss_streak`` when nothing was
     produced) via the quest update route.
  7. For a quest with a mapped local folder (``quest_folder_map``), REFRESHES that quest's
     canonical next-steps artifact (``quest_folder_sync.publish_next_steps``) with the conclusion
     the pass just reached, and READS the existing artifact into the batch text first. That closes
     a real gap: the pass and an attended session both have to answer "what is next for this
     quest", and before this they answered it separately, from different material, with no way to
     notice they had drifted apart.

A DRY RUN (the pass task's own text contains "dry-run") reports what WOULD be created and creates
nothing: no tasks, no goals, no bookkeeping writes.

Per-quest failures are isolated: one quest's error never aborts the rest of the pass (recorded in
the result instead), and every skipped quest is logged with its gate reason -- silent skips are
exactly the failure mode the qar playbook bans.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .quest_folder_sync import NextSteps, publish_next_steps, read_next_steps
from .reflections import DEFAULT_PERIODS, ReflectionContext, collect_reflections

log = logging.getLogger("quest-ai-runner.autopilot")

# Team-wide daily cap on autopilot-created tasks (batches + goal proposals both count as one
# unit each). Overridable via ``RunnerConfig.autopilot_daily_budget``.
DEFAULT_TEAM_DAILY_BUDGET = 3

# --- the two PERSISTENT task_kind markers (see QuestClient.create_task) ---------------------
# ``task_kind`` is written once at create and never overwritten by the claim path (unlike
# ``handler``, which every claim stamps with the claiming worker's own label). Two DISTINCT values,
# and the distinction is load-bearing:
#   * PASS_KIND     -- the recurring "autopilot pass" task itself. The executor routes THIS to
#                      AutopilotPass instead of a deep run.
#   * WORK_KIND     -- the work batches / goal proposals the pass CREATES. They must NOT carry
#                      PASS_KIND, or the executor would route each created task back into another
#                      autopilot pass (an infinite self-spawning loop). They are ordinary tasks
#                      that happen to be autopilot-authored, and this marker is what makes them
#                      countable for the daily budget and the per-quest backpressure gate.
AUTOPILOT_PASS_KIND = "autopilot"
AUTOPILOT_WORK_KIND = "autopilot_work"

# The ``source`` stamped on autopilot-created tasks. The Quest API validates source against a
# CLOSED enum and 400s anything else (create_task raises, by design). The enum has since gained
# "autopilot", but "chat" is kept here because it is accepted by BOTH the current and older
# backends, and the authoritative autopilot-authored marker is ``task_kind``
# (AUTOPILOT_WORK_KIND above) rather than source. Deployments pinned to a current backend can
# switch this to AUTOPILOT_SOURCE below for attribution; the gates below count either.
AUTOPILOT_TASK_SOURCE = "chat"

# The dedicated source value on current backends. Rows stamped this way still count toward the
# budget and backpressure gates below, so the two can coexist during a rollout.
AUTOPILOT_LEGACY_SOURCE = "autopilot"

# Cadence name -> minimum days between passes for a given quest. An unrecognized cadence string
# falls back to "weekly" rather than erroring, so a bad/unknown value never wedges a quest either
# always-due or never-due.
_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}

# A previous autopilot task in any of these states is still "open" -- its quest is under
# backpressure and gets no more autonomous work piled on until it resolves.
OPEN_TASK_STATUSES = {"queued", "in_progress", "needs_you", "suggested"}

# Time-scope granularities recognized in a quest's ``list_quest_goals`` period grouping, checked
# FINEST first (day) so "today" wins over a same-quarter month/quarter/year group that also
# happens to be current.
_SCOPE_ORDER = ("day", "week", "month", "quarter", "year")

# Task statuses that count as "this finished" when summarizing the previous period. ``needs_you``
# is included on purpose: a task waiting on the human is one of the most useful things the next
# pass can know, since it usually explains why the period produced nothing else.
_FINISHED_TASK_STATUSES = {"done", "failed", "needs_you"}

# Cap on how many previous-period tasks are described in a batch's text, newest kept. A busy quest
# should not push its actual instructions out of the model's attention with old status lines.
_MAX_PREVIOUS_TASKS = 8


def _truthy(value: Any) -> bool:
    """Whether a config value means yes, tolerating the string forms a JSON round-trip can leave
    behind ("true"/"1"/"yes"). A stray "false" string must never read as enabled."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


# --- small, pure helpers (kept module-level and dependency-free for direct unit testing) --------

def _parse_dt(raw: Any) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string (tolerating a trailing ``Z``). None on any failure."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def cadence_due(autopilot_cfg: Dict[str, Any], now: datetime) -> bool:
    """Whether a quest's cadence gate is DUE (True) or should skip this pass (False).

    Compared as CALENDAR periods, not elapsed time: "daily" means a pass has not run yet TODAY,
    not that 24 hours have elapsed. The elapsed-time reading silently loses days. The pass task
    fires at a fixed wall-clock time, so if one pass runs late (a backed-up queue, a restart, a
    manual run at noon), the next morning's pass is still inside the 24-hour window and skips
    entirely -- and a "daily" quest quietly becomes every-other-day. Real case: a first pass ran at
    16:11 and the following 06:00 pass would have been gated out.

    Never run before (no ``last_pass_at``) is always due. An unparsable timestamp fails OPEN to
    due -- a corrupt/missing stamp must never permanently wedge a quest as "not due yet".
    """
    last_pass = autopilot_cfg.get("last_pass_at")
    if not last_pass:
        return True
    parsed = _parse_dt(last_pass)
    if parsed is None:
        return True
    cadence = str(autopilot_cfg.get("cadence") or "weekly").strip().lower()
    if cadence not in _CADENCE_DAYS:
        cadence = "weekly"
    then = parsed.astimezone(timezone.utc)
    here = now.astimezone(timezone.utc)
    if cadence == "daily":
        return then.date() < here.date()
    if cadence == "weekly":
        return then.isocalendar()[:2] < here.isocalendar()[:2]
    return (then.year, then.month) < (here.year, here.month)


def _current_period_key(scope: str, now: datetime) -> Optional[str]:
    """The canonical period id for ``scope`` at ``now``, in the QUEST BACKEND'S exact format.

    Mirrors quest-backend's ``app/utils/period_utils.get_current_period`` byte-for-byte -- the
    separator is an UNDERSCORE for every scope except day (which is a plain ISO date):

        day     -> "2026-07-12"   (date.isoformat())
        week    -> "2026_W28"     (ISO year + zero-padded ISO week)
        month   -> "2026_07"      (zero-padded month)
        quarter -> "2026_Q3"
        year    -> "2026"

    Getting a separator wrong here is not a cosmetic bug: the key is compared for EQUALITY against
    each period group's ``period`` string, so a hyphen where the backend writes an underscore
    means the current period never matches, every quest silently falls through to the "unscoped"
    fallback, and today's goals are never worked. Keep this in lock-step with period_utils.
    """
    if scope == "day":
        return now.date().isoformat()
    if scope == "week":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}_W{iso_week:02d}"
    if scope == "month":
        return f"{now.year}_{now.month:02d}"
    if scope == "quarter":
        q = (now.month - 1) // 3 + 1
        return f"{now.year}_Q{q}"
    if scope == "year":
        return str(now.year)
    return None


def previous_period_key(scope: str, now: datetime) -> Optional[str]:
    """The period id immediately BEFORE the current one for ``scope``, in the backend's format.

    Used to tell a pass what happened last time it was responsible for this quest, so a daily
    pass opens with "here is what yesterday produced" instead of starting cold every morning.
    Computed by stepping back into the previous period and re-deriving the key, rather than by
    decrementing the key's digits, so week/quarter/year boundaries need no special cases.
    """
    if scope == "day":
        return _current_period_key("day", now - timedelta(days=1))
    if scope == "week":
        return _current_period_key("week", now - timedelta(days=7))
    if scope == "month":
        first_of_month = now.replace(day=1)
        return _current_period_key("month", first_of_month - timedelta(days=1))
    if scope == "quarter":
        first_month_of_quarter = ((now.month - 1) // 3) * 3 + 1
        first_of_quarter = now.replace(month=first_month_of_quarter, day=1)
        return _current_period_key("quarter", first_of_quarter - timedelta(days=1))
    if scope == "year":
        return str(now.year - 1)
    return None


def previous_period_bounds(scope: str, now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """UTC half-open bounds ``[start, end)`` of the period before the current one.

    Derived from ``now`` rather than by parsing a period key back into dates: the key formats are
    the backend's display contract, and re-parsing them here would be a second place to get week
    and quarter boundaries wrong.
    """
    midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if scope == "day":
        return midnight - timedelta(days=1), midnight
    if scope == "week":
        this_week = midnight - timedelta(days=midnight.weekday())  # Monday, per ISO
        return this_week - timedelta(days=7), this_week
    if scope == "month":
        this_month = midnight.replace(day=1)
        return (this_month - timedelta(days=1)).replace(day=1), this_month
    if scope == "quarter":
        this_quarter = midnight.replace(month=((midnight.month - 1) // 3) * 3 + 1, day=1)
        previous = this_quarter - timedelta(days=1)
        return previous.replace(month=((previous.month - 1) // 3) * 3 + 1, day=1), this_quarter
    if scope == "year":
        this_year = midnight.replace(month=1, day=1)
        return this_year.replace(year=this_year.year - 1), this_year
    return None


def select_period_goals(goals_payload: Dict[str, Any], scope: str,
                        period: str) -> List[Dict[str, Any]]:
    """Every goal in one specific (scope, period) group, complete or not. ``[]`` if absent."""
    for group in goals_payload.get("period_groups") or []:
        if (str(group.get("time_scope", "")).strip().lower() == scope
                and str(group.get("period", "")).strip() == period):
            return list(group.get("goals") or [])
    return []


def _goal_ai_help(goal: Dict[str, Any]) -> bool:
    """Missing ``ai_help`` counts as False (human-only, invisible to autopilot) per the design."""
    return bool(goal.get("ai_help"))


def _incomplete_ai_goals(goals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [g for g in goals if not g.get("completed") and _goal_ai_help(g)]


def select_target_goals(goals_payload: Dict[str, Any],
                        now: datetime) -> Tuple[List[Dict[str, Any]], str]:
    """Pick this pass's target goals from a quest's ``list_quest_goals`` period grouping.

    Returns ``(goals, scope_label)``:
      * a CURRENT period group with eligible goals (day beats week beats month beats quarter beats
        year) -> ALL its incomplete + ``ai_help`` goals, ``scope_label`` like ``"day:2026-07-12"``.
        This is deliberately ALL of them (not just one): the resolved 2026-07-12 scope question in
        the design doc is "a pass works ALL incomplete AI-enabled goals in the quest's current
        scope".
      * no scope is current (an unscoped quest, or only "custom"-scoped goals) -> the SINGLE next
        incomplete + ``ai_help`` goal in the payload's own order, ``scope_label="unscoped"``.
      * nothing eligible either way -> ``([], "unscoped")``.

    A current group that exists but yields NOTHING eligible does not stop the search: the next
    COARSER CURRENT scope is tried. This matters more than it sounds. A quest can easily have a
    human-only goal dated today (say "decide whether to email another committee member") sitting
    above a weekly goal that is the actual AI work for that whole week. Stopping at the day group
    would shadow the week, and autopilot would report nothing to do on precisely the days the user
    had also planned something for themselves. A goal scoped to this week is genuinely in scope on
    every day of it.

    But if NO current scope yields anything, the quest goes quiet with the finest matching scope's
    label, rather than falling through to the unscoped next-goal fallback. Having planned today (or
    this week) and left no AI-enabled goal in it is a decision, and pulling in some unrelated future
    goal would override it. The fallback exists for quests with no current scope at all.
    """
    groups = goals_payload.get("period_groups") or []
    matched_label: Optional[str] = None
    for scope in _SCOPE_ORDER:
        key = _current_period_key(scope, now)
        for group in groups:
            if str(group.get("time_scope", "")).strip().lower() != scope:
                continue
            period = str(group.get("period", "")).strip()
            if period == key:
                eligible = _incomplete_ai_goals(group.get("goals") or [])
                if eligible:
                    return eligible, f"{scope}:{key}"
                if matched_label is None:
                    matched_label = f"{scope}:{key}"
    if matched_label is not None:
        return [], matched_label
    flattened: List[Dict[str, Any]] = []
    for group in groups:
        flattened.extend(group.get("goals") or [])
    for goal in flattened:
        if not goal.get("completed") and _goal_ai_help(goal):
            return [goal], "unscoped"
    return [], "unscoped"


def _weekday_abbrev(now: datetime) -> str:
    return now.strftime("%a")  # "Mon", "Tue", ...


def resolve_persona(goal: Dict[str, Any], autopilot_cfg: Dict[str, Any], now: datetime,
                    fallback_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
                    ) -> Optional[str]:
    """Resolver order (explicit always beats inferred), per the design's persona-routing section:

      1. The goal's own ``assignee_rep_id`` (a per-goal override).
      2. A quest ``autopilot.personas`` entry whose ``days`` include today (checked first, so an
         explicit day-restricted assignment wins over an unrestricted one for the same day).
      3. A quest ``autopilot.personas`` entry with NO ``days`` restriction (applies any day).
      4. A consumer-injected fallback (e.g. the existing card-vote resolver), given the goal dict.
      5. ``None`` -- the plain assistant persona (no character voice).
    """
    assignee = goal.get("assignee_rep_id")
    if assignee:
        return str(assignee)
    personas = autopilot_cfg.get("personas") or []
    today = _weekday_abbrev(now)
    for persona in personas:
        days = persona.get("days")
        if days and today in days:
            rep_id = persona.get("rep_id")
            if rep_id:
                return str(rep_id)
    for persona in personas:
        if not persona.get("days"):
            rep_id = persona.get("rep_id")
            if rep_id:
                return str(rep_id)
    if fallback_resolver is not None:
        try:
            resolved = fallback_resolver(goal)
            if resolved:
                return str(resolved)
        except Exception:  # noqa: BLE001 -- a bad fallback must never break a pass
            log.info("autopilot: persona fallback_resolver raised; treating as no match",
                     exc_info=True)
    return None


def personas_on_duty(autopilot_cfg: Dict[str, Any], now: datetime) -> List[str]:
    """Every rep_id in a quest's roster that applies TODAY, most specific first.

    Day-restricted entries that name today come before unrestricted ones, matching
    ``resolve_persona``'s precedence: an explicit "Bailey on Mondays" outranks a catch-all.

    This is what lets an ATTENDED session speak as the same character that would work the quest
    autonomously. Opening a chat inside a quest and getting a generic assistant, while its
    autopilot runs as a named character with that character's accumulated corrections, makes the
    two feel like unrelated systems when they are meant to be one.
    """
    personas = autopilot_cfg.get("personas") or []
    today = _weekday_abbrev(now)
    on_duty: List[str] = []
    for restricted in (True, False):
        for persona in personas:
            days = persona.get("days")
            if bool(days) is not restricted:
                continue
            if days and today not in days:
                continue
            rep_id = persona.get("rep_id")
            if rep_id and str(rep_id) not in on_duty:
                on_duty.append(str(rep_id))
    return on_duty


def batch_by_persona(goals: List[Dict[str, Any]], autopilot_cfg: Dict[str, Any], now: datetime,
                     fallback_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
                     ) -> List[Tuple[Optional[str], List[Dict[str, Any]]]]:
    """Group ``goals`` by resolved persona, preserving first-seen order. Same persona (including
    ``None``, the plain assistant) -> ONE batch; different personas -> separate batches."""
    order: List[Optional[str]] = []
    batches: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for goal in goals:
        persona = resolve_persona(goal, autopilot_cfg, now, fallback_resolver)
        if persona not in batches:
            batches[persona] = []
            order.append(persona)
        batches[persona].append(goal)
    return [(persona, batches[persona]) for persona in order]


def _definition_of_done(goal: Dict[str, Any]) -> str:
    """One short line. The goal's own ``criteria`` when it has any, since the human wrote those
    for this goal specifically and they beat a generic restatement of the brief."""
    criteria = (goal.get("criteria") or "").strip()
    deadline = (goal.get("deadline") or "").strip()
    dod = criteria or "the work matches the brief above and is ready to read as-is."
    if deadline:
        dod += f" Target: {deadline}."
    return dod


def _summarize_previous(previous: Dict[str, Any]) -> str:
    """The "what happened last period" block, or a plain statement that nothing did.

    Saying so explicitly matters: an absent section reads as "no information", while "no recorded
    activity" is itself the signal that the previous period produced nothing and the plan may need
    re-sequencing rather than continuing as written.
    """
    period = previous.get("period") or "the previous period"
    lines = [f"What happened in the previous period ({period}):"]
    done_goals = [g for g in previous.get("goals") or [] if g.get("completed")]
    open_goals = [g for g in previous.get("goals") or [] if not g.get("completed")]
    tasks = previous.get("tasks") or []
    if done_goals:
        lines.append("  Goals completed: "
                     + "; ".join((g.get("name") or "?").strip() for g in done_goals))
    if open_goals:
        lines.append("  Goals left INCOMPLETE (carry them or re-sequence, do not silently drop "
                     "them): " + "; ".join((g.get("name") or "?").strip() for g in open_goals))
    for t in tasks:
        title = (t.get("title") or t.get("text") or "").strip().splitlines()
        label = (title[0] if title else "(untitled task)")[:90]
        outcome = str(t.get("result") or "").strip().replace("\n", " ")
        lines.append(f"  Task [{t.get('status')}] {label}"
                     + (f" -> {outcome[:280]}" if outcome else ""))
    if not (done_goals or open_goals or tasks):
        lines.append("  No recorded activity. Treat the plan's schedule as untouched, and if that "
                     "is because work slipped, say so rather than repeating the same instruction.")
    return "\n".join(lines)


def next_steps_from_pass(goals: List[Dict[str, Any]],
                         adopted_tasks: Optional[List[Dict[str, Any]]] = None, *,
                         scope_label: str = "", updated: str = "",
                         previous: Optional[Dict[str, Any]] = None,
                         reflection_note: str = "") -> NextSteps:
    """The pass's own conclusion about what comes next, as the canonical artifact.

    Deterministic and LLM-free, like ``propose_next_goal``: the pass has already done the selecting
    (current scope, ai_help, incomplete, persona batching), so the artifact is a restatement of that
    decision, not a second opinion about it. Asking a model to re-derive it here would spend a call
    to produce a DIFFERENT answer from the one the pass just acted on, which is the exact drift this
    artifact exists to remove.

    One line per target goal (its deadline included, since "next" and "by when" are the same
    question), then one per adopted recurring task, then the previous period's unfinished goals as
    carry-over.

    ``reflection_note`` is one condensed line from the person's own latest reflection, carried into
    the artifact's ``note`` slot. It is context for the list, never a step: the reflection explains
    WHY these are the next steps, and a reader who disagrees with the list can see what it was
    written against instead of guessing.
    """
    steps: List[str] = []
    for goal in goals:
        name = (goal.get("name") or "(untitled goal)").strip()
        deadline = (goal.get("deadline") or "").strip()
        steps.append(f"{name}{f' (target {deadline})' if deadline else ''}")
    for task in adopted_tasks or []:
        label = str(task.get("title") or task.get("text") or "").strip().splitlines()
        if label:
            steps.append(f"{label[0][:120]} (recurring)")
    carrying = [(g.get("name") or "?").strip()
                for g in ((previous or {}).get("goals") or []) if not g.get("completed")]
    return NextSteps(steps=steps, carrying_over=carrying, source="the autopilot pass",
                     scope=scope_label or "", updated=updated or "",
                     note=(reflection_note or "").strip())


def compose_batch_text(quest_outcome: str, goals: List[Dict[str, Any]],
                       persona: Optional[str] = None, *,
                       scope_label: Optional[str] = None,
                       adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                       next_steps: Optional[str] = None,
                       previous: Optional[Dict[str, Any]] = None,
                       reflection: Optional[str] = None) -> str:
    """The batch task's text: what period this run owns, the goals and AI tasks in it, what the
    person themselves last said about the work, and what the previous period actually produced.

    The last two are what keep a recurring pass from starting cold every time. A daily pass that
    cannot see yesterday's goals and task results has no way to notice that the plan slipped, so it
    reissues the same instruction while the human falls further behind. Feeding the previous
    period's goal completion and task outcomes in makes continuity the default.

    ``reflection`` is the person's own latest daily/period reflection (``runner.reflections``),
    which is the only input here written BY them rather than derived from rows. Everything else
    describes what the system recorded; the reflection says what the person made of it, so it is
    what should break ties about which of several eligible goals actually matters this run.

    ``persona``, when resolved, is named in the text AS WELL AS stamped structurally in
    ``assignee_rep_id`` at creation. The structured field is authoritative; the prose is kept
    because some consumers resolve the persona from the request text.
    """
    parts: List[str] = []
    if persona:
        parts.append(f"Act as {persona}.")
    if quest_outcome:
        parts.append(f"Quest outcome: {quest_outcome}")
    if scope_label:
        # Saying whose target this is matters. A weekly goal handed to a daily run reads as "do all
        # of this today", which is both discouraging and wrong; the run's job is to advance it and
        # report what is left.
        parts.append(f"Scope: this quest's {scope_label}. What follows is that PERIOD's target, "
                     f"not this single run's. Advance it as far as one focused session honestly "
                     f"can, then say plainly what remains.")
    for goal in goals:
        name = (goal.get("name") or "(untitled goal)").strip()
        description = (goal.get("description") or "").strip()
        block = [f"Goal: {name}"]
        if description:
            block.append(f"Brief: {description}")
        block.append(f"Done when: {_definition_of_done(goal)}")
        parts.append("\n".join(block))
    if adopted_tasks:
        block = ["Recurring AI tasks for this period, adopted into this run. Carry out each one "
                 "as part of this run; the original occurrences are closed and will NOT run "
                 "separately, so anything you skip here simply does not happen:"]
        for t in adopted_tasks:
            text = str(t.get("text") or "").strip()
            block.append(f"\n--- adopted task {t.get('id') or t.get('task_id')} ---\n{text}")
        parts.append("\n".join(block))
    if next_steps:
        # The quest folder's canonical next-steps artifact, which an attended session may have
        # refreshed more recently than any pass. Naming it as the standing answer is the point: two
        # sources for "what is next" is how a background run and the person working the quest end up
        # pulling in different directions without either noticing.
        parts.append("The quest folder's standing next-steps artifact (QUEST_SYNC.md), which an "
                     "attended session may have refreshed since the last pass. Treat it as the "
                     "current plan of record, and if the work has moved past it, say so:\n"
                     + next_steps)
    if reflection:
        # Placed after the goals and the plan of record, and before the previous-period rows,
        # because it is the lens to read them through rather than another item on the list. It is
        # quoted as-is: paraphrasing a person's own words back at a model turns the one
        # first-hand input in this brief into a second-hand one.
        parts.append(reflection + "\n\nLet that steer which of the above matters most in this run, "
                     "and what tone to take. If it contradicts the plan above, say so plainly in "
                     "your result rather than quietly following one or the other.")
    if previous:
        parts.append(_summarize_previous(previous))
    return "\n\n".join(parts)


def _batch_title(goals: List[Dict[str, Any]],
                 adopted_tasks: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """A short label for the task list: what this batch is ABOUT.

    Without one the server derives a title from the first line of the text, which is the "Act as
    ..." persona line, so every autopilot task in the list is titled after its persona instead of
    its work. Named goals win; an adoption-only batch is titled after the task it took over.
    """
    names = [(g.get("name") or "").strip() for g in goals]
    names = [n for n in names if n]
    if not names and adopted_tasks:
        first = str((adopted_tasks[0].get("title") or adopted_tasks[0].get("text") or "")).strip()
        names = [first.splitlines()[0]] if first else []
    if not names:
        return None
    title = names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} more)"
    return title[:120]


def propose_next_goal(quest: Dict[str, Any]) -> Tuple[str, str]:
    """A deterministic (LLM-free, so this stays fast/offline-testable) proposed next-goal (title,
    description) in service of the quest's stated outcome, used when ``planning=="plan_and_work"``
    and no AI-enabled goal is eligible this pass."""
    outcome = (quest.get("outcome") or "this quest's outcome").strip()
    title = f"Next step toward: {outcome}"
    description = (
        f'Propose and take the next concrete step toward "{outcome}". No AI-enabled goal was '
        "eligible this pass, and this quest allows autopilot to plan as well as work. Define a "
        "specific, checkable outcome for this goal before starting on it."
    )
    return title, description


@dataclass
class AutopilotResult:
    """What one autopilot pass did -- the pass task's own reported result (see ``summary_text``)."""
    ran_at: datetime
    dry_run: bool = False
    created_task_ids: List[str] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)      # {quest_id, reason}
    proposals: List[Dict[str, Any]] = field(default_factory=list)    # dry-run "would create" items
    errors: List[Dict[str, Any]] = field(default_factory=list)       # {quest_id, error}
    # Bookkeeping writes the backend ACCEPTED (200) but did not actually persist. Kept separate
    # from ``errors`` because the pass itself succeeded; what failed is the cadence/miss_streak
    # memory, which silently degrades the NEXT pass (a last_pass_at that never sticks means the
    # cadence gate can never fire). Surfaced in the reported summary so it can never pass silently.
    bookkeeping_warnings: List[Dict[str, Any]] = field(default_factory=list)  # {quest_id, detail}
    # Quests whose canonical next-steps artifact this pass rewrote: {quest_id, path, quest_target}.
    next_steps_refreshed: List[Dict[str, Any]] = field(default_factory=list)

    def summary_text(self) -> str:
        lines: List[str] = []
        lines.append("Autopilot dry-run: nothing was created. Here is what WOULD happen:"
                     if self.dry_run else "Autopilot pass complete.")
        if self.created_task_ids:
            lines.append(f"Created {len(self.created_task_ids)} task(s): "
                        + ", ".join(self.created_task_ids))
        if self.proposals:
            lines.append(f"Would create {len(self.proposals)} item(s):")
            for p in self.proposals:
                if p.get("kind") == "goal_proposal":
                    lines.append(f"  - Proposed goal on quest {p.get('quest_id')}: {p.get('title')}")
                else:
                    line = (f"  - Work batch on quest {p.get('quest_id')} "
                            f"(persona={p.get('persona') or 'assistant'}, scope={p.get('scope')}): "
                            f"goal(s) {p.get('goal_ids')}")
                    # Adoption CLOSES the user's own recurring tasks, so a report that omitted it
                    # would hide the most consequential thing the pass does.
                    adopted = p.get("adopted_task_ids")
                    if adopted:
                        line += f", adopting and closing recurring task(s) {adopted}"
                    lines.append(line)
        if self.next_steps_refreshed:
            lines.append(f"Refreshed the next-steps artifact on {len(self.next_steps_refreshed)} "
                         f"quest(s):")
            for n in self.next_steps_refreshed:
                lines.append(f"  - {n.get('quest_id')}: {n.get('path')} "
                             f"(on Quest: {n.get('quest_target')})")
        if self.skipped:
            lines.append(f"Skipped {len(self.skipped)} quest(s):")
            for s in self.skipped:
                lines.append(f"  - {s.get('quest_id')}: {s.get('reason')}")
        if self.errors:
            lines.append(f"Errors on {len(self.errors)} quest(s):")
            for e in self.errors:
                lines.append(f"  - {e.get('quest_id')}: {e.get('error')}")
        if self.bookkeeping_warnings:
            lines.append(
                f"WARNING: autopilot bookkeeping did not persist on "
                f"{len(self.bookkeeping_warnings)} quest(s). The cadence gate reads "
                f"last_pass_at, so until this is fixed those quests are considered due on EVERY "
                f"pass (the per-quest cadence cannot hold them back):")
            for w in self.bookkeeping_warnings:
                lines.append(f"  - {w.get('quest_id')}: {w.get('detail')}")
        if not (self.created_task_ids or self.proposals or self.skipped or self.errors):
            lines.append("No opted-in quests found.")
        return "\n".join(lines)


class AutopilotPass:
    """Runs ONE autopilot pass against a Quest client. See the module docstring for the algorithm.

    ``client`` is a ``QuestClient`` (or any object with the same methods this class calls:
    ``list_quests``, ``get_quest_autopilot``, ``list_quest_goals``, ``get_goal``, ``list_tasks``,
    ``list_open_decisions_for_quest``, ``create_task``, ``update_task``,
    ``update_quest_autopilot``, and optionally ``create_goal``, ``get_daily_reflection`` and
    ``get_period_reflection`` -- a client missing the optional ones simply composes a batch without
    that material, exactly as before they existed).

    ``persona_resolver`` is the consumer-injected fallback (step 4 of ``resolve_persona``) -- e.g.
    the personal lane's card-vote resolver. Given a goal dict, returns a rep_id or ``None``.

    ``quest_folder_map`` (``{quest_id: folder}``, from ``RunnerConfig``) opts a quest into the
    canonical next-steps artifact: the pass reads the folder's standing answer into the batch it
    creates, and writes its own conclusion back over it (locally and on Quest). Without a map the
    pass behaves exactly as before.
    """

    def __init__(self, client: Any, *, team_id: str = "",
                 persona_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                 daily_budget: int = DEFAULT_TEAM_DAILY_BUDGET,
                 adopt_recurring_default: Optional[bool] = None,
                 quest_folder_map: Optional[Dict[str, str]] = None,
                 now: Optional[Callable[[], datetime]] = None):
        self._client = client
        self._team_id = team_id or ""
        self._persona_resolver = persona_resolver
        self._daily_budget = daily_budget if daily_budget and daily_budget > 0 else DEFAULT_TEAM_DAILY_BUDGET
        self._adopt_recurring_default = adopt_recurring_default
        # ``{quest_id: folder}`` (RunnerConfig.quest_folder_map). A quest with a folder gets the
        # next-steps artifact read and refreshed; one without is unaffected.
        self._quest_folder_map: Dict[str, str] = {
            str(k): str(v) for k, v in (quest_folder_map or {}).items() if v}
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._persona_names: Dict[str, str] = {}   # rep_id -> display name, resolved once per pass
        # Reflections are USER-scoped, not per quest, so every quest in a pass would otherwise
        # re-fetch the identical text. Cached per requested period order, for the pass's lifetime
        # only -- a long-lived cache would be a stale one the moment the person writes a review.
        self._reflection_cache: Dict[Tuple[str, ...], ReflectionContext] = {}

    def _persona_label(self, rep_id: Optional[str]) -> Optional[str]:
        """A rep's human display name, falling back to the raw id.

        "Act as rep_09d389aeb9ff" is what a task says when nobody looks up the name, and it is
        unreadable to the human reviewing the task and useless to a consumer that resolves personas
        by name. The id remains authoritative (it rides in ``assignee_rep_id``); this is purely for
        the prose. Cached per pass, and any lookup failure degrades to the id rather than failing
        the batch.
        """
        if not rep_id:
            return None
        rep_id = str(rep_id)
        if rep_id in self._persona_names:
            return self._persona_names[rep_id]
        label = rep_id
        getter = getattr(self._client, "get_ai_profile", None)
        if callable(getter):
            try:
                profile = getter(rep_id, team_id=self._team_id or None) or {}
                label = str(profile.get("display_name") or "").strip() or rep_id
            except Exception:  # noqa: BLE001 -- a name is cosmetic; never fail a batch over it
                log.info("autopilot: could not resolve a display name for %s", rep_id,
                         exc_info=True)
        self._persona_names[rep_id] = label
        return label

    def _reflection_periods(self, scope_label: str) -> Tuple[str, ...]:
        """Which period reviews to consult for a quest at ``scope_label``, finest match first.

        A quest working on a monthly target is best informed by the month review; a daily or
        unscoped quest has no matching period, so it falls back to the module default. The quest's
        own scope choosing the period is the point: nothing here hardcodes that a week matters more
        than a quarter, and a deployment that plans in quarters gets its quarter review read.
        """
        scope = (scope_label or "").split(":", 1)[0]
        defaults = tuple(DEFAULT_PERIODS)
        if scope in ("week", "month", "quarter", "year"):
            return (scope,) + tuple(p for p in defaults if p != scope)
        return defaults

    def _reflections(self, scope_label: str) -> ReflectionContext:
        """The person's latest reflection, fetched once per pass per period order.

        Best-effort by construction (``collect_reflections`` never raises and returns an empty
        context for a client that has no reflection methods at all), so a backend or client without
        these endpoints composes exactly the batch text it composed before.
        """
        periods = self._reflection_periods(scope_label)
        cached = self._reflection_cache.get(periods)
        if cached is None:
            cached = collect_reflections(self._client, periods=periods, now=self._now())
            self._reflection_cache[periods] = cached
            if cached.has_any():
                log.info("autopilot: read the person's reflection (daily=%s, period=%s)",
                         cached.daily_date or "none", cached.period or "none")
            else:
                log.info("autopilot: no reflection on record (checked %s)",
                         ", ".join(cached.checked_periods) or "no period")
        return cached

    def _adopts_recurring(self, autopilot_cfg: Dict[str, Any]) -> bool:
        """Whether to adopt this quest's recurring tasks: the QUEST's own setting when it states
        one, otherwise the consumer's default.

        The fallback is not just convenience. ``adopt_recurring`` is a newer field, so a backend
        that predates it stores nothing and every quest would silently read as off, with no way for
        a deployment to turn the behavior on at all until it upgrades.
        """
        stated = autopilot_cfg.get("adopt_recurring")
        if stated is None:
            return bool(self._adopt_recurring_default)
        return _truthy(stated)

    # --- the pass --------------------------------------------------------------------------

    def run(self, task: Dict[str, Any]) -> AutopilotResult:
        text = str(task.get("text") or task.get("title") or "")
        dry_run = "dry-run" in text.lower()
        result = AutopilotResult(ran_at=self._now(), dry_run=dry_run)

        quests = self._eligible_quests()
        budget_used = 0 if dry_run else self._count_autopilot_tasks_today()

        for quest in quests:
            quest_id = str(quest.get("quest_id") or quest.get("id") or "")
            if not quest_id:
                continue
            try:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id,
                              f"team daily budget reached ({budget_used}/{self._daily_budget})")
                    continue
                gate_reason = self._gate_quest(quest, quest_id)
                if gate_reason:
                    self._skip(result, quest_id, gate_reason)
                    continue
                budget_used = self._run_one_quest(quest, quest_id, dry_run, budget_used, result)
            except Exception as e:  # noqa: BLE001 -- one quest's failure never aborts the pass
                log.error("autopilot: quest %s pass failed: %s", quest_id, e, exc_info=True)
                result.errors.append({"quest_id": quest_id, "error": f"{type(e).__name__}: {e}"})
        return result

    def _run_one_quest(self, quest: Dict[str, Any], quest_id: str, dry_run: bool,
                       budget_used: int, result: AutopilotResult) -> int:
        autopilot_cfg = quest.get("autopilot") or {}
        mode = str(autopilot_cfg.get("mode") or "off")
        planning = str(autopilot_cfg.get("planning") or "work_only")

        goals_payload = self._client.list_quest_goals(quest_id, team_id=self._team_id or None) or {}
        target_goals, scope_label = select_target_goals(goals_payload, self._now())
        # The goals-grouping payload does NOT carry each goal's ``description`` (the backend's
        # handler builds a slim per-goal dict: id/name/time_scope/period/deadline/completed/
        # parent_goal_id/ai_help/assignee_rep_id). But the description IS the brief -- it is the
        # entire statement of what the AI is being asked to do. So fetch it per TARGET goal (a
        # handful at most, only for goals that survived the gates and the scope filter).
        target_goals = [self._with_description(quest_id, g) for g in target_goals]

        # Recurring tasks the user set up on this quest. Adopted ONLY when the quest opts in:
        # taking over a task someone scheduled themselves is a real change in who executes it.
        adopted = (self._due_recurring_tasks(quest_id)
                   if self._adopts_recurring(autopilot_cfg) else [])
        previous = self._previous_period_summary(quest_id, goals_payload, scope_label)
        # The standing artifact, read BEFORE this pass overwrites it: whatever the last refresh
        # concluded (possibly an attended session's, more recent than any pass) rides into the
        # batch as the plan of record.
        standing_next_steps = self._read_next_steps(quest_id)
        # What the person themselves last wrote about their work. USER-scoped and cached for the
        # pass, so this is at most one extra pair of reads per pass, not per quest.
        reflections = self._reflections(scope_label)
        reflection_text = reflections.as_text() or None

        produced = False
        if target_goals or adopted:
            batches = self._batches_with_adopted(target_goals, adopted, autopilot_cfg)
            for persona, goals, tasks in batches:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id, "team daily budget reached mid-pass")
                    break
                if dry_run:
                    result.proposals.append({
                        "quest_id": quest_id, "kind": "work_batch", "persona": persona,
                        "goal_ids": [g.get("id") for g in goals], "scope": scope_label,
                        "adopted_task_ids": [t.get("id") or t.get("task_id") for t in tasks],
                    })
                    produced = True
                    # A dry-run still SIMULATES budget consumption (one unit per batch that
                    # WOULD be created), so the report honestly shows a later quest going quiet
                    # once the budget is exhausted -- exactly what a real pass would do.
                    budget_used += 1
                    continue
                task_id = self._create_batch_task(quest, quest_id, persona, goals, mode,
                                                  scope_label=scope_label, adopted_tasks=tasks,
                                                  next_steps=standing_next_steps,
                                                  previous=previous,
                                                  reflection=reflection_text)
                if task_id:
                    result.created_task_ids.append(task_id)
                    budget_used += 1
                    produced = True
                    self._close_adopted(tasks, task_id, quest_id, result)
            if produced and not dry_run:
                self._refresh_next_steps(quest_id, target_goals, adopted, scope_label, previous,
                                         result, reflection_note=reflections.one_line())
        elif planning == "plan_and_work":
            if budget_used < self._daily_budget:
                self._handle_proposal(quest, quest_id, mode, dry_run, result)
                produced = True
                budget_used += 1
            else:
                self._skip(result, quest_id, "team daily budget reached mid-pass")

        if not dry_run:
            self._update_pass_bookkeeping(quest_id, autopilot_cfg, produced, result)
        return budget_used

    # --- gates -------------------------------------------------------------------------------

    def _eligible_quests(self) -> List[Dict[str, Any]]:
        """The team's quests that are opted in (``autopilot.mode`` in suggest/act).

        TWO reads per quest, deliberately. The team quest LISTING
        (``GET /api/teams/{team_id}/quests``) returns only
        ``{quest_id, outcome, completed, owner_user_ids}`` -- it does NOT include the ``autopilot``
        block. Reading the opt-in mode off those rows would find no ``autopilot`` on ANY quest,
        treat every one as mode "off", and make the whole feature a silent no-op forever. The
        ``autopilot`` settings live on the full QuestState, so we fetch it per quest
        (``get_quest_autopilot`` -> ``GET /api/quests/{quest_id}/state``) and merge it onto the
        listing row. Cost is one small read per team quest, once per pass.
        """
        quests = self._client.list_quests(team_id=self._team_id or None) or []
        eligible = []
        for row in quests:
            quest_id = str(row.get("quest_id") or row.get("id") or "")
            if not quest_id:
                continue
            state = self._quest_state(quest_id)
            autopilot_cfg = (state.get("autopilot") or {}) if state else {}
            mode = str(autopilot_cfg.get("mode") or "off")
            if mode not in ("suggest", "act"):
                log.info("autopilot: quest %s mode=%r -- not opted in, skipping", quest_id, mode)
                continue
            eligible.append({
                "quest_id": quest_id,
                # Prefer the full state's outcome; fall back to the listing row's.
                "outcome": state.get("outcome") or row.get("outcome") or "",
                "autopilot": autopilot_cfg,
            })
        return eligible

    def _quest_state(self, quest_id: str) -> Dict[str, Any]:
        """The quest's full state (for ``autopilot`` + ``outcome``). ``{}`` on any failure."""
        reader = getattr(self._client, "get_quest_autopilot", None)
        if not callable(reader):
            return {}
        try:
            return reader(quest_id) or {}
        except Exception:  # noqa: BLE001 -- a bad read must never abort the whole pass
            log.warning("autopilot: could not read quest %s state; treating as not opted in",
                       quest_id, exc_info=True)
            return {}

    def _with_description(self, quest_id: str, goal: Dict[str, Any]) -> Dict[str, Any]:
        """Return ``goal`` enriched with its ``description`` (the AI's brief), fetched per goal.

        The grouping payload omits ``description``; ``get_goal`` returns the full goal document.
        Best-effort: on any failure the goal is used as-is (``compose_batch_text`` simply omits the
        Brief line), which degrades the task's richness but never drops the goal from the batch."""
        if goal.get("description"):
            return goal
        get_goal = getattr(self._client, "get_goal", None)
        goal_id = goal.get("id")
        if not callable(get_goal) or not goal_id:
            return goal
        try:
            full = get_goal(str(goal_id), quest_id=quest_id,
                           team_id=self._team_id or None) or {}
            description = (full.get("description") or "").strip()
            if description:
                enriched = dict(goal)
                enriched["description"] = description
                return enriched
        except Exception:  # noqa: BLE001 -- enrichment is best-effort, never fatal
            log.info("autopilot: could not fetch description for goal %s", goal_id, exc_info=True)
        return goal

    # --- adopting the quest's own recurring tasks (opt-in per quest) --------------------------

    def _due_recurring_tasks(self, quest_id: str) -> List[Dict[str, Any]]:
        """This quest's queued recurring occurrences that are due now and are not autopilot's own.

        The ``task_kind`` exclusion is not a nicety. Adopting the recurring PASS task would fold
        the scanner into the very batch it is creating and then close it, killing the series that
        drives autopilot at all; adopting autopilot's own work batches would let a pass swallow its
        previous output. Neither is recoverable from inside a pass, so both are excluded here.
        """
        tasks = self._client.list_tasks(
            team_id=self._team_id or None, goal_id=quest_id, status="queued") or []
        today = self._now().date()
        due: List[Dict[str, Any]] = []
        for t in tasks:
            if self._is_autopilot_authored(t) or t.get("task_kind") in (AUTOPILOT_PASS_KIND,
                                                                        AUTOPILOT_WORK_KIND):
                continue
            if not (t.get("series_id") or t.get("recurrence")):
                continue  # a one-off task the user queued; not ours to take over
            scheduled = str(t.get("scheduled_date") or "").strip()
            if scheduled:
                parsed = _parse_dt(scheduled)
                if parsed is not None and parsed.date() > today:
                    continue  # scheduled for a later day
            due.append(t)
        return due

    def _batches_with_adopted(self, goals: List[Dict[str, Any]],
                              adopted: List[Dict[str, Any]],
                              autopilot_cfg: Dict[str, Any]
                              ) -> List[Tuple[Optional[str], List[Dict[str, Any]],
                                              List[Dict[str, Any]]]]:
        """Merge goal batches and adopted tasks into ONE batch per persona.

        An adopted task's persona is its own ``assignee_rep_id`` when it has one (the user chose a
        character for it), otherwise the persona the quest's roster resolves for today, so an
        unassigned recurring task rides along with that persona's goal work instead of spawning a
        second run for the same character.
        """
        now = self._now()
        merged: Dict[Optional[str], Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = {}
        order: List[Optional[str]] = []

        def slot(persona: Optional[str]):
            if persona not in merged:
                merged[persona] = ([], [])
                order.append(persona)
            return merged[persona]

        for persona, batch_goals in batch_by_persona(goals, autopilot_cfg, now,
                                                     self._persona_resolver):
            slot(persona)[0].extend(batch_goals)
        for task in adopted:
            persona = task.get("assignee_rep_id") or resolve_persona(
                {}, autopilot_cfg, now, self._persona_resolver)
            slot(str(persona) if persona else None)[1].append(task)
        return [(persona, merged[persona][0], merged[persona][1]) for persona in order]

    def _close_adopted(self, tasks: List[Dict[str, Any]], batch_task_id: str, quest_id: str,
                       result: AutopilotResult) -> None:
        """Close each adopted occurrence, pointing at the batch that took over its work.

        Ordering is deliberate: this runs only AFTER the batch task was created successfully. If a
        close fails, the occurrence stays queued and runs on its own later -- duplicated work, but
        never LOST work, which is the correct direction to fail in. The reverse order would close
        the occurrence and then discover the batch could not be created, and the user's recurring
        task would simply have evaporated for the day.

        Note this does mark work done before the batch has actually run. If the batch later fails,
        it fails visibly as a failed task on the quest, and (for a daily series) the next
        occurrence arrives tomorrow.
        """
        for t in tasks:
            task_id = str(t.get("id") or t.get("task_id") or "")
            if not task_id:
                continue
            try:
                self._client.update_task(task_id, {
                    "status": "done",
                    "result": (f"Adopted by the autopilot pass and folded into task "
                               f"{batch_task_id}, which carries out this task's instructions as "
                               f"part of this period's batch for the quest."),
                })
            except Exception as e:  # noqa: BLE001 -- never fail a pass over bookkeeping
                log.warning("autopilot: could not close adopted task %s (%s); it stays queued "
                            "and will run on its own", task_id, e)
                result.bookkeeping_warnings.append({
                    "quest_id": quest_id,
                    "detail": (f"adopted task {task_id} was folded into {batch_task_id} but could "
                               f"not be closed ({type(e).__name__}), so it will ALSO run "
                               f"separately: expect duplicated work, not missing work"),
                })

    # --- the quest's canonical next-steps artifact --------------------------------------------

    def _read_next_steps(self, quest_id: str) -> Optional[str]:
        """The standing next-steps artifact in this quest's mapped folder, or None.

        Best-effort in both directions: a quest with no mapped folder simply has no artifact, and an
        unreadable file must not stop the pass, because a missing plan-of-record degrades the batch
        text rather than invalidating it.
        """
        folder = self._quest_folder_map.get(str(quest_id))
        if not folder:
            return None
        try:
            return read_next_steps(folder)
        except Exception:  # noqa: BLE001 -- reading the artifact is never worth failing a pass
            log.info("autopilot: could not read next steps for quest %s", quest_id, exc_info=True)
            return None

    def _refresh_next_steps(self, quest_id: str, goals: List[Dict[str, Any]],
                            adopted: List[Dict[str, Any]], scope_label: str,
                            previous: Optional[Dict[str, Any]],
                            result: AutopilotResult, *,
                            reflection_note: str = "") -> None:
        """Write this pass's conclusion as the quest's canonical next steps (folder + Quest).

        Only called when the pass actually PRODUCED work, and never on a dry run. A pass that found
        nothing eligible must leave the artifact alone: overwriting a considered answer with "no
        current target" on the day a quest happens to be gated or quiet would make the artifact less
        trustworthy than the guesswork it replaces.
        """
        folder = self._quest_folder_map.get(str(quest_id))
        if not folder:
            return
        updated = self._now().astimezone(timezone.utc).strftime("%Y-%m-%d")
        next_steps = next_steps_from_pass(goals, adopted, scope_label=scope_label,
                                          updated=updated, previous=previous,
                                          reflection_note=reflection_note)
        try:
            published = publish_next_steps(self._client, quest_id, folder, next_steps)
        except Exception as e:  # noqa: BLE001 -- the artifact must never fail an otherwise-good pass
            log.warning("autopilot: could not refresh next steps for quest %s", quest_id,
                        exc_info=True)
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id,
                 "detail": f"the next-steps artifact was not refreshed ({type(e).__name__}: {e})"})
            return
        result.next_steps_refreshed.append({
            "quest_id": quest_id, "path": published.sync_path,
            "quest_target": published.quest_target,
        })
        if published.detail:
            # The local file is current either way; what did not happen is the Quest-side write, and
            # a silently local-only artifact is how the two views drift apart again.
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id,
                 "detail": f"next-steps artifact written locally, but on Quest: {published.detail}"})

    def _previous_period_summary(self, quest_id: str, goals_payload: Dict[str, Any],
                                 scope_label: str) -> Optional[Dict[str, Any]]:
        """Goals and finished tasks from the period before this one, for run-to-run continuity.

        Returns None for an unscoped quest (there is no previous period to speak of). Best-effort:
        a failure to read tasks degrades to goals-only rather than losing the whole summary."""
        scope = (scope_label or "").split(":", 1)[0]
        if scope not in _SCOPE_ORDER:
            return None
        previous_key = previous_period_key(scope, self._now())
        if not previous_key:
            return None
        summary: Dict[str, Any] = {
            "period": f"{scope}:{previous_key}",
            "goals": select_period_goals(goals_payload, scope, previous_key),
            "tasks": [],
        }
        try:
            tasks = self._client.list_tasks(
                team_id=self._team_id or None, goal_id=quest_id) or []
        except Exception:  # noqa: BLE001 -- goals-only is still a useful summary
            log.info("autopilot: could not read prior tasks for quest %s", quest_id, exc_info=True)
            return summary
        bounds = previous_period_bounds(scope, self._now())
        if bounds is None:
            return summary
        window_start, window_end = bounds
        finished = []
        for t in tasks:
            if str(t.get("status", "")).strip().lower() not in _FINISHED_TASK_STATUSES:
                continue
            when = _parse_dt(t.get("worked_at") or t.get("updated_at") or t.get("created_at"))
            if when is None or not (window_start <= when < window_end):
                continue
            finished.append(t)
        summary["tasks"] = finished[-_MAX_PREVIOUS_TASKS:]
        return summary

    def _gate_quest(self, quest: Dict[str, Any], quest_id: str) -> Optional[str]:
        """Per-quest gates, cheapest first. Returns a skip reason, or None if the quest passes."""
        autopilot_cfg = quest.get("autopilot") or {}
        if not cadence_due(autopilot_cfg, self._now()):
            return "cadence not due yet"
        if self._has_backpressure(quest_id):
            return "backpressure: a previous autopilot task for this quest is still open"
        if self._has_open_hold_decision(quest_id):
            return "an open HOLD decision is pending on this quest"
        return None

    @staticmethod
    def _is_autopilot_authored(task: Dict[str, Any]) -> bool:
        """Whether a task was created by autopilot (so it counts against the budget/backpressure).

        The marker is ``task_kind == "autopilot_work"`` (persistent, never overwritten by a claim).
        ``source == "autopilot"`` is ALSO accepted so that, if the backend's source enum later
        gains that value, rows stamped the new way still count -- and any already-created row is
        never miscounted as human work."""
        return (task.get("task_kind") == AUTOPILOT_WORK_KIND
                or task.get("source") == AUTOPILOT_LEGACY_SOURCE)

    def _count_autopilot_tasks_today(self) -> int:
        """Autopilot-authored tasks created TODAY, team-wide (the daily budget's denominator).

        The Quest list route has no ``source``/``task_kind`` query filter, so this pulls the
        team's tasks and narrows client-side (``list_tasks`` does the same, honestly)."""
        tasks = self._client.list_tasks(team_id=self._team_id or None) or []
        today = self._now().date()
        count = 0
        for t in tasks:
            if not self._is_autopilot_authored(t):
                continue
            created = _parse_dt(t.get("created_at") or t.get("scheduled_at"))
            if created is not None and created.date() == today:
                count += 1
        return count

    def _has_backpressure(self, quest_id: str) -> bool:
        """True when a previous autopilot task for THIS quest is still open (queued/in_progress/
        needs_you/suggested) -- the human has not worked through the last thing we produced.

        A task's link to its quest is its ``goal_id``: the Quest API resolves that field as a
        QUEST id (its handler loads the quest by it), and there is no separate ``quest_id`` field
        on a task at all. So the per-quest listing is ``list_tasks(goal_id=quest_id)``."""
        tasks = self._client.list_tasks(
            team_id=self._team_id or None, goal_id=quest_id) or []
        return any(
            self._is_autopilot_authored(t)
            and str(t.get("status", "")).strip().lower() in OPEN_TASK_STATUSES
            for t in tasks
        )

    def _has_open_hold_decision(self, quest_id: str) -> bool:
        lister = getattr(self._client, "list_open_decisions_for_quest", None)
        if not callable(lister):
            return False
        try:
            return bool(lister(quest_id))
        except Exception:  # noqa: BLE001 -- fail open: never let this check block a pass
            log.info("autopilot: list_open_decisions_for_quest failed for %s", quest_id,
                     exc_info=True)
            return False

    # --- side effects --------------------------------------------------------------------------

    def _create_autopilot_task(self, quest: Dict[str, Any], quest_id: str, text: str, mode: str,
                               persona: Optional[str] = None,
                               title: Optional[str] = None,
                               force_suggested: bool = False) -> Optional[str]:
        """Create ONE autopilot-authored task and land it in the right status.

        THE QUEST LINK IS ALWAYS ``quest_id``. A task's ``goal_id`` field holds a QUEST id -- the
        Quest API resolves it with ``storage.get_quest(goal_id)`` and 404s anything else -- so a
        per-goal id from ``list_quest_goals`` (a separate document with its own id) must never be
        passed here. Doing so failed every work-batch creation outright, and any that had survived
        would have been invisible to ``_has_backpressure``, which looks tasks up by quest id.
        Which GOALS a task covers is carried in its text (see ``compose_batch_text``), not in this
        field.

        ``status`` is asserted AT CREATION rather than PATCHed afterwards. Creating a proposal
        ``queued`` and demoting it in a second call leaves a window in which the runner's poll can
        claim and EXECUTE it before the demotion lands -- exactly the approval that suggest mode
        exists to require. One atomic create closes that window.

        The created task carries ``task_kind="autopilot_work"`` (NOT the pass's own
        ``"autopilot"`` kind, which the executor routes into another pass -- that would spawn an
        infinite loop) and names its persona structurally in ``assignee_rep_id``.
        """
        # suggest mode (and every goal proposal, which is always a proposal for a human) must not
        # be runnable until a human approves it.
        needs_approval = force_suggested or mode != "act"
        kwargs: Dict[str, Any] = dict(
            team_id=self._team_id or None,
            goal_id=quest_id,
            source=AUTOPILOT_TASK_SOURCE,
            task_kind=AUTOPILOT_WORK_KIND,
            status="suggested" if needs_approval else "queued",
        )
        if persona:
            # Structural persona routing. It also rides in the text (some consumers resolve from
            # prose), but a field a resolver can read beats one it has to parse.
            kwargs["assignee_rep_id"] = persona
        if title:
            kwargs["title"] = title
        env_id = (quest.get("autopilot") or {}).get("env_id")
        if env_id:
            kwargs["env_id"] = env_id
        created = self._client.create_task(text, **kwargs) or {}
        task_id = created.get("id") or created.get("task_id")
        if not task_id:
            return None
        return str(task_id)

    def _create_batch_task(self, quest: Dict[str, Any], quest_id: str, persona: Optional[str],
                           goals: List[Dict[str, Any]], mode: str, *,
                           scope_label: Optional[str] = None,
                           adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                           next_steps: Optional[str] = None,
                           previous: Optional[Dict[str, Any]] = None,
                           reflection: Optional[str] = None) -> Optional[str]:
        text = compose_batch_text(str(quest.get("outcome") or ""), goals,
                                  self._persona_label(persona),
                                  scope_label=scope_label, adopted_tasks=adopted_tasks,
                                  next_steps=next_steps, previous=previous,
                                  reflection=reflection)
        try:
            return self._create_autopilot_task(
                quest, quest_id, text, mode, persona=persona,
                title=_batch_title(goals, adopted_tasks))
        except Exception as e:  # noqa: BLE001 -- surfaced to the caller's per-quest try/except
            log.error("autopilot: task creation failed for quest %s: %s", quest_id, e,
                      exc_info=True)
            raise

    def _maybe_create_goal(self, quest_id: str, title: str, description: str,
                          mode: str) -> Optional[str]:
        """Create the REAL goal object only when the client has a create_goal endpoint AND mode is
        "act" (per the design: creating goals stays manual in v1 unless the client already
        supports it). Generic/adapter-agnostic: works today (no-op, since QuestClient has no
        create_goal yet) and picks up automatically once one is added."""
        create_goal = getattr(self._client, "create_goal", None)
        if not callable(create_goal) or mode != "act":
            return None
        try:
            created = create_goal(quest_id, name=title, description=description,
                                  ai_help=True, created_by="ai") or {}
            return created.get("id") if isinstance(created, dict) else None
        except Exception as e:  # noqa: BLE001 -- a missing/misbehaving optional endpoint must not fail the pass
            log.warning("autopilot: create_goal failed for quest %s: %s", quest_id, e)
            return None

    def _handle_proposal(self, quest: Dict[str, Any], quest_id: str, mode: str, dry_run: bool,
                         result: AutopilotResult) -> None:
        title, description = propose_next_goal(quest)
        if dry_run:
            result.proposals.append({
                "quest_id": quest_id, "kind": "goal_proposal",
                "title": title, "description": description,
            })
            return
        created_goal_id = self._maybe_create_goal(quest_id, title, description, mode)
        task_text = f"Proposed goal: {title}\n\n{description}"
        if created_goal_id:
            task_text += f"\n\n(Created as goal {created_goal_id} on this quest.)"
        # A proposed goal is ALWAYS surfaced for a human to accept, even on an `act` quest: the
        # design keeps AI-created goals reviewable ("attributed and editable"), so force suggested.
        task_id = self._create_autopilot_task(
            quest, quest_id, task_text, mode, force_suggested=True)
        if task_id:
            result.created_task_ids.append(task_id)

    def _update_pass_bookkeeping(self, quest_id: str, autopilot_cfg: Dict[str, Any],
                                 produced: bool, result: AutopilotResult) -> None:
        """Stamp ``last_pass_at`` (and the ``miss_streak``) on the quest, then VERIFY it stuck.

        The verify is not paranoia. The quest autopilot PATCH endpoint's request schema currently
        accepts only mode/planning/cadence/personas/env_id; ``last_pass_at`` and ``miss_streak``
        exist on the stored model but are NOT in that schema, and its Pydantic model ignores
        unknown keys -- so this write can return 200 having persisted nothing. If we assumed
        success, ``last_pass_at`` would stay null forever, the cadence gate would consider every
        quest due on every pass, and the per-quest cadence (a core budget control) would be
        silently inert. So: read the echoed settings back, compare, and record a LOUD warning when
        a field did not stick. Never raises -- bookkeeping must not fail an otherwise-good pass --
        but it must never fail SILENTLY either.
        """
        now_iso = self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields: Dict[str, Any] = {"last_pass_at": now_iso}
        fields["miss_streak"] = 0 if produced else int(autopilot_cfg.get("miss_streak") or 0) + 1
        update = getattr(self._client, "update_quest_autopilot", None)
        if not callable(update):
            return
        try:
            resp = update(quest_id, fields) or {}
        except Exception as e:  # noqa: BLE001 -- bookkeeping must never fail an otherwise-good pass
            log.warning("autopilot: last_pass_at/miss_streak update failed for quest %s",
                       quest_id, exc_info=True)
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "detail": f"update raised {type(e).__name__}: {e}"})
            return
        echoed = (resp or {}).get("autopilot")
        if not isinstance(echoed, dict):
            return  # nothing echoed back to check against (an older/mock client); assume nothing
        unpersisted = [k for k, v in fields.items() if echoed.get(k) != v]
        if unpersisted:
            detail = (f"the backend accepted the PATCH but did not persist {unpersisted} "
                      f"(its autopilot update schema does not accept these bookkeeping fields)")
            log.warning("autopilot: quest %s -- %s", quest_id, detail)
            result.bookkeeping_warnings.append({"quest_id": quest_id, "detail": detail})

    def _skip(self, result: AutopilotResult, quest_id: str, reason: str) -> None:
        log.info("autopilot: skipping quest %s (%s)", quest_id, reason)
        result.skipped.append({"quest_id": quest_id, "reason": reason})
