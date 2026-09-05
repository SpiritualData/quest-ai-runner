"""Autopilot — the recurring "autopilot pass" task that scans opted-in quests and makes progress.

Design of record: ``quest_autopilot_design.md`` (Part B). In one sentence: Autopilot is itself a
recurring assistant task (``task_kind == "autopilot"``, routed by ``runner.executor`` before the
normal deep-run path). Each pass:

  1. Lists the team's quests; keeps the ones opted in (``autopilot.mode`` in ``suggest``/``act``).
  2. Gates each, cheapest first: a team-wide daily budget, per-quest cadence, and -- only where
     the deployment opts into backpressure -- an open autopilot-created task or an unresolved HOLD
     decision already sitting on the quest. By default neither stops a pass: work continues and
     the unfinished thing is visible in context to be worked around.
  3. Picks the quest's CURRENT-SCOPE target goals (today's, this period's, or the single next
     incomplete one when the quest is unscoped) among the ones flagged ``ai_help``.
  4. Resolves a persona per goal (goal assignee -> quest persona-for-today -> a consumer-injected
     fallback) and batches goals sharing a persona into ONE task (one budget unit). A roster entry
     flagged ``instructions_only`` is skipped by that routing: it says its character is on duty to
     work their OWN standing instructions and takes no goals, which is what lets a specialist share
     a roster with the character who actually works the quest's goals that day. Every persona on
     duty carrying instructions of their own also gets a batch, goals or not (see 5).
  5. Creates each batch as a real task (``status="suggested"`` in suggest mode, ``"queued"`` in
     act mode), or -- when planning allows and nothing is eligible -- proposes the quest's next
     goal instead of a work task, UNLESS the previous pass's proposal is still sitting there
     unanswered (a proposal is one question, and re-asking it every pass is how the same
     suggestion ends up in the person's list every morning). The batch text carries the person's
     OWN latest reflection
     (``runner.reflections``: the daily plan's review of yesterday, plus the newest submitted
     period review) and the insights they have CAPTURED but not yet acted on since this quest's
     last pass (``runner.insights``, with the person's own category tags shown beside each one).
     Both are read once per pass, since both are user-scoped rather than per quest. Everything
     else in that text is derived from rows the system recorded; those two are the parts the
     person wrote, so they are what break ties about which eligible goal actually matters today.
  6. Updates the quest's ``autopilot.last_pass_at`` (and ``miss_streak`` when nothing was
     produced) via the quest update route.
  7. For a quest with a mapped local folder (``quest_folder_map``), REFRESHES that quest's
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

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .insights import InsightsContext, collect_unacted_insights
from .local_time import now_in_zone
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

# Every proposed-goal task's text starts with this, which is how a later pass recognizes the
# proposal it already made and does not make it again (see ``_open_proposal``).
#
# It is also a CROSS-REPO wire contract: quest-backend re-declares the identical literal in
# app/business/quests/autopilot_rollup.py to tell a proposal from a piece of work when it renders
# the pass rollup. Rewording it here alone breaks nothing loudly -- the reader just stops
# recognizing proposals and files them under "Waiting for your approval before it can run" again.
# That backend's tests/business/test_autopilot_runner_contract.py asserts equality against THIS
# constant, so change both sides together and it will say so.
PROPOSAL_TEXT_PREFIX = "Proposed goal:"

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

# A quest's standing ``autopilot.instructions``, in characters. Mirrors the backend's own
# ``AutopilotSettings.instructions`` cap (``max_length=8000``) so a value written before either
# cap existed, or written through some other client, still gets truncated defensively here rather
# than riding an oversized block into every prompt this quest ever gets.
MAX_INSTRUCTIONS_CHARS = 8000

# Leading Markdown "furniture" stripped off the first line of a person's instructions when it
# becomes a batch's title (headings, bullets, blockquotes) -- a title should read as a title, not
# carry the markup that made sense inside the instructions block itself.
_MD_TITLE_FURNITURE_RE = re.compile(r"^[#\-*>\s]+")


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


def cadence_due(autopilot_cfg: Dict[str, Any], now: datetime, tz: Optional[str] = None) -> bool:
    """Whether a quest's cadence gate is DUE (True) or should skip this pass (False).

    Compared as CALENDAR periods, not elapsed time: "daily" means a pass has not run yet TODAY,
    not that 24 hours have elapsed. The elapsed-time reading silently loses days. The pass task
    fires at a fixed wall-clock time, so if one pass runs late (a backed-up queue, a restart, a
    manual run at noon), the next morning's pass is still inside the 24-hour window and skips
    entirely -- and a "daily" quest quietly becomes every-other-day. Real case: a first pass ran at
    16:11 and the following 06:00 pass would have been gated out.

    Never run before (no ``last_pass_at``) is always due. An unparsable timestamp fails OPEN to
    due -- a corrupt/missing stamp must never permanently wedge a quest as "not due yet".

    ``tz`` (a quest's own ``run_timezone``) is the zone BOTH ``last_pass_at`` and ``now`` are
    compared in. This is not a nicety: a 22:00 America/Los_Angeles run stamps ``last_pass_at`` at
    about 05:00 UTC the NEXT UTC day. Compared as UTC days, the following day's pass looks like it
    already ran "today" and the brief is skipped.

    A missing or unresolvable ``tz`` degrades to the RUNNER'S OWN clock via
    ``local_time.now_in_zone``, never to UTC -- the one degradation rule this repo has, stated in
    ``local_time``'s module docstring and in the autopilot spec (A4). It used to degrade to UTC
    here instead, and that cost a real brief: this quest set no ``run_timezone``, an evening
    catch-up pass on a US/Pacific runner stamped 03:26Z the next UTC day, and the following
    morning's pass was gated out as "already ran today". A calendar-day comparison against a zone
    nobody lives in is not a safe default, so there is no longer a branch offering one.
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
    then = now_in_zone(tz, parsed)
    here = now_in_zone(tz, now)
    if cadence == "daily":
        return then.date() < here.date()
    if cadence == "weekly":
        return then.isocalendar()[:2] < here.isocalendar()[:2]
    return (then.year, then.month) < (here.year, here.month)


def run_requested(autopilot_cfg: Dict[str, Any]) -> bool:
    """Whether a "Run now" request is PENDING for this quest.

    Pending means ``run_requested_at`` is newer than ``last_pass_at`` -- the request has not yet
    been answered by a pass. That comparison is the whole mechanism, and it is why the request
    needs no separate clear: a finished pass stamps ``last_pass_at``, which makes the request older
    and therefore spent. Nothing has to remember to reset a flag, so there is no state that can be
    left stuck ON (a pass that runs forever) or stuck OFF (a request silently dropped).

    Compared as INSTANTS in UTC, deliberately unlike ``cadence_due``'s calendar-day comparison:
    "is this request newer than that pass" is a question about moments, so no timezone enters into
    it. An unparsable ``run_requested_at`` is not pending (a corrupt stamp must not wedge a quest
    into passing forever), while an unparsable ``last_pass_at`` leaves a real request pending --
    both fail toward the behaviour the user last actually asked for.
    """
    requested = _parse_dt(autopilot_cfg.get("run_requested_at"))
    if requested is None:
        return False
    last_pass = _parse_dt(autopilot_cfg.get("last_pass_at"))
    return last_pass is None or requested > last_pass


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
      2. A quest ``autopilot.personas`` entry whose ``days`` include today, EXCLUDING entries
         flagged ``instructions_only`` (checked first, so an explicit day-restricted assignment
         wins over an unrestricted one for the same day).
      3. A quest ``autopilot.personas`` entry with NO ``days`` restriction, again excluding
         ``instructions_only`` entries (applies any day).
      4. A consumer-injected fallback (e.g. the existing card-vote resolver), given the goal dict.
      5. ``None`` -- the plain assistant persona (no character voice).

    Why ``instructions_only`` is excluded from 2 and 3, and only from those: a roster entry is
    two different statements at once ("this character is on duty today" and "this character is who
    should work the goals"), and rule 2 beating rule 3 makes the second statement greedy. A quest
    with a weekday worker and a Saturday specialist reads, on Saturday, as "the specialist takes
    every goal" -- so the specialist inherits a week of work they were never meant to touch, and
    the weekday worker goes quiet on the one day the specialist is around. ``instructions_only``
    says this entry is only the FIRST statement: the character is on duty and works their own
    standing instructions, and goal routing behaves as though they were not in the roster at all.
    Step 1 still wins over it, because a per-goal ``assignee_rep_id`` is a human naming that
    character for that goal, which is more specific than any roster-wide preference.
    """
    assignee = goal.get("assignee_rep_id")
    if assignee:
        return str(assignee)
    personas = autopilot_cfg.get("personas") or []
    today = _weekday_abbrev(now)
    for persona in personas:
        if _truthy(persona.get("instructions_only")):
            continue
        days = persona.get("days")
        if days and today in days:
            rep_id = persona.get("rep_id")
            if rep_id:
                return str(rep_id)
    for persona in personas:
        if _truthy(persona.get("instructions_only")):
            continue
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


def persona_entries_on_duty(autopilot_cfg: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """The full roster ENTRIES that apply TODAY, most specific first, one per rep_id.

    Day-restricted entries that name today come before unrestricted ones, matching
    ``resolve_persona``'s precedence: an explicit "Bailey on Mondays" outranks a catch-all. A
    rep_id appearing in several entries is kept once, at its earliest (most specific) position.

    The entries, not just the ids, because an entry now carries the character's own standing
    ``instructions`` for this quest and its ``instructions_only`` flag, and a caller deciding
    whether that character has a job to do today needs to read them. ``personas_on_duty`` stays
    the answer to the narrower "who is on duty" question and is written in terms of this.
    """
    personas = autopilot_cfg.get("personas") or []
    today = _weekday_abbrev(now)
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    for restricted in (True, False):
        for persona in personas:
            days = persona.get("days")
            if bool(days) is not restricted:
                continue
            if days and today not in days:
                continue
            rep_id = persona.get("rep_id")
            if rep_id and str(rep_id) not in seen:
                seen.add(str(rep_id))
                entries.append(persona)
    return entries


def personas_on_duty(autopilot_cfg: Dict[str, Any], now: datetime) -> List[str]:
    """Every rep_id in a quest's roster that applies TODAY, most specific first.

    Day-restricted entries that name today come before unrestricted ones, matching
    ``resolve_persona``'s precedence: an explicit "Bailey on Mondays" outranks a catch-all.

    This is what lets an ATTENDED session speak as the same character that would work the quest
    autonomously. Opening a chat inside a quest and getting a generic assistant, while its
    autopilot runs as a named character with that character's accumulated corrections, makes the
    two feel like unrelated systems when they are meant to be one.

    ``instructions_only`` entries are deliberately still listed here. That flag says a character
    does not take the quest's GOALS, not that they are absent: they are on duty, working their own
    standing instructions, and a person opening a chat on the quest should be able to reach them.
    """
    return [str(entry.get("rep_id")) for entry in persona_entries_on_duty(autopilot_cfg, now)]


def persona_instructions_for(autopilot_cfg: Dict[str, Any], rep_id: Optional[str]) -> Optional[str]:
    """A character's own standing instructions for this quest, or None.

    The WHOLE roster is searched, not only today's on-duty entries: a goal can name a character
    through its ``assignee_rep_id`` on any day at all, and a character working this quest without
    their standing brief would be a different character than the one the person configured. The
    first entry for that rep with non-empty instructions wins, so a roster that lists the same
    character twice is read the same way ``persona_entries_on_duty`` reads it.

    Truncated defensively at ``MAX_INSTRUCTIONS_CHARS``, the same cap and the same warning the
    quest-level field gets. As there, the text itself is never logged: it is the person's private
    content and can run to 8 KB.
    """
    if not rep_id:
        return None
    wanted = str(rep_id)
    for entry in (autopilot_cfg.get("personas") or []):
        if str(entry.get("rep_id") or "") != wanted:
            continue
        instructions = str(entry.get("instructions") or "").strip()
        if not instructions:
            continue
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            log.warning("autopilot: persona %s instructions truncated from %d to %d characters",
                        wanted, len(instructions), MAX_INSTRUCTIONS_CHARS)
            instructions = instructions[:MAX_INSTRUCTIONS_CHARS]
        return instructions
    return None


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
    dismissed_any = False
    for t in tasks:
        title = (t.get("title") or t.get("text") or "").strip().splitlines()
        label = (title[0] if title else "(untitled task)")[:90]
        outcome = str(t.get("result") or "").strip().replace("\n", " ")
        # A dismissal is the person saying this run was not worth their attention. It is the only
        # unprompted signal the feed produces, and it is cheap for them to give, so it is worth
        # more than its size: several dismissals in a row say the work itself is off, not that the
        # summary was long.
        dismissed = " [they cleared this from their feed]" if t.get("dismissed_at") else ""
        dismissed_any = dismissed_any or bool(dismissed)
        lines.append(f"  Task [{t.get('status')}] {label}{dismissed}"
                     + (f" -> {outcome[:280]}" if outcome else ""))
    if dismissed_any:
        lines.append("  A cleared run is feedback, not a failure and not a request: they saw it "
                     "and did not want it. Treat it as a signal about what to surface, and never "
                     "as work to redo, re-send or ask them about.")
    if not (done_goals or open_goals or tasks):
        lines.append("  No recorded activity. Treat the plan's schedule as untouched, and if that "
                     "is because work slipped, say so rather than repeating the same instruction.")
    return "\n".join(lines)


def next_steps_from_pass(goals: List[Dict[str, Any]],
                         adopted_tasks: Optional[List[Dict[str, Any]]] = None, *,
                         scope_label: str = "", updated: str = "",
                         previous: Optional[Dict[str, Any]] = None,
                         reflection_note: str = "",
                         insights_note: str = "") -> NextSteps:
    """The pass's own conclusion about what comes next, as the canonical artifact.

    Deterministic and LLM-free, like ``propose_next_goal``: the pass has already done the selecting
    (current scope, ai_help, incomplete, persona batching), so the artifact is a restatement of that
    decision, not a second opinion about it. Asking a model to re-derive it here would spend a call
    to produce a DIFFERENT answer from the one the pass just acted on, which is the exact drift this
    artifact exists to remove.

    One line per target goal (its deadline included, since "next" and "by when" are the same
    question), then one per adopted recurring task, then the previous period's unfinished goals as
    carry-over.

    ``reflection_note`` is one condensed line from the person's own latest reflection, and
    ``insights_note`` one from their newest unacted capture; both are carried into the artifact's
    ``note`` slot. They are context for the list, never steps: they explain WHY these are the next
    steps, and a reader who disagrees with the list can see what it was written against instead of
    guessing. An insight is explicitly NOT promoted to a step here — the person captured it, they
    did not commit to it, and quietly turning a note-to-self into a planned action is how an
    artifact stops being trustworthy.
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
    note = "\n".join(n for n in ((reflection_note or "").strip(),
                                 (insights_note or "").strip()) if n)
    return NextSteps(steps=steps, carrying_over=carrying, source="the autopilot pass",
                     scope=scope_label or "", updated=updated or "", note=note)


# Always told to a batch run, with or without previous-period rows to read it against.
#
# A recurring pass has no way to observe whether a person did the thing it asked for. Left to
# infer, it treats its own assignment as the event -- it asked yesterday, a day passed, so it
# moves on and asks for something else. The person then receives a new instruction every period
# while the first one is still untouched, which reads as the AI not noticing them at all. The
# fix is not more memory; it is being explicit that completion is a claim only the person can
# make, and that "re-sequence" means moving when work happens, never quietly substituting a
# fresh item for an untouched one.
_CONFIRMATION_RULE = (
    "WHAT COUNTS AS DONE. Only the person confirms their own work: a goal they completed, a note "
    "or reply they wrote, or an artifact they produced. None of these are confirmation -- that "
    "you assigned it, that an earlier run's plan listed it, that the period ended, or that you "
    "read the material yourself. You may never record their work as done on their behalf.\n"
    "While something you asked of them is unconfirmed, repeat THAT item rather than replacing it "
    "with a fresh one of the same kind, and say how long it has been outstanding. Re-sequencing "
    "means changing WHEN the remaining work happens; it never means swapping an untouched item "
    "for a different one, because someone who has not done the first thing is not helped by being "
    "handed a second. If the same item goes unconfirmed three times, stop reissuing it and make "
    "what to do about it the question you raise -- shrink it, swap it, or drop it."
)


_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions for this quest, written by the person who owns it. They are the "
    "specification for this run: they say what to produce and how. Everything below is the "
    "material to apply them to. Follow them verbatim where they are specific, and where an "
    "instruction and a goal's \"Done when\" disagree about what finishing that goal means, do "
    "what the goal says and note the conflict in your result."
)

# The same idea one level down, for the instructions a character carries on this quest. Two things
# are said explicitly rather than left to position. WHO it is addressed to, because this block sits
# directly under a quest-wide block that describes a different job, and "instructions for this
# character" is not actionable unless the text names the character. And WHICH governs, because two
# specifications in one brief with nothing ranking them is how a run quietly follows the more
# general one. ``{who}`` is filled with the persona's name when one is resolved.
_PERSONA_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions {who} on this quest, written by the person who owns it. They describe "
    "the job this character does here, which is not the same job the quest-wide instructions "
    "describe. They are MORE SPECIFIC than those, so where the two disagree, follow these and say "
    "in your result which you followed and why. The rule about goals still holds: where an "
    "instruction and a goal's \"Done when\" disagree about what finishing that goal means, do what "
    "the goal says and note the conflict."
)


def compose_batch_text(quest_outcome: str, goals: List[Dict[str, Any]],
                       persona: Optional[str] = None, *,
                       scope_label: Optional[str] = None,
                       adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                       next_steps: Optional[str] = None,
                       previous: Optional[Dict[str, Any]] = None,
                       reflection: Optional[str] = None,
                       insights: Optional[str] = None,
                       instructions: Optional[str] = None,
                       persona_instructions: Optional[str] = None) -> str:
    """The batch task's text: what period this run owns, the goals and AI tasks in it, what the
    person themselves last said about the work, and what the previous period actually produced.

    The last two are what keep a recurring pass from starting cold every time. A daily pass that
    cannot see yesterday's goals and task results has no way to notice that the plan slipped, so it
    reissues the same instruction while the human falls further behind. Feeding the previous
    period's goal completion and task outcomes in makes continuity the default.

    ``reflection`` is the person's own latest daily/period reflection (``runner.reflections``), and
    ``insights`` the captures they have made and not yet acted on since the last pass
    (``runner.insights``). Those two are the only inputs here written BY them rather than derived
    from rows: everything else describes what the system recorded, while these say what the person
    made of it and what occurred to them in between, so they are what should break ties about which
    of several eligible goals actually matters this run.

    The insights block carries each capture's own category tags and asks the READER to judge which
    apply to this quest. That is deliberate: matching a tag against a quest or goal name in code
    would be a fixed string rule that silently drops every insight whose wording it did not
    anticipate, which is exactly what this repository's hard rule #3 forbids.

    ``instructions``, when the quest carries any, is the FOURTH block: right after ``Scope:`` and
    before the first ``Goal:`` block, verbatim, never summarized/reflowed/rewritten (it is the one
    input here the person authored as a specification, not material to interpret). Its precedence
    over the goals below it is stated in the framing sentence, not just implied by position -- a
    specification that arrived after the material it governs would read as commentary on work
    already planned. Absent, this emits nothing and the composed text is byte-identical to before
    this parameter existed.

    ``persona_instructions`` is the same thing one level down: the standing instructions THIS
    character carries on this quest, emitted immediately after the quest-wide block and before the
    first ``Goal:`` block, equally verbatim. It exists because one roster can hold characters doing
    genuinely different jobs on the same quest -- the weekday worker advancing the goals and the
    Saturday reviewer looking at the quest from outside -- and handing both the identical
    quest-wide brief describes neither of them. Its precedence over that brief is stated in its own
    framing sentence for the same reason the quest-wide one states its own. Absent, this likewise
    emits nothing and composes byte-identically to before the parameter existed.

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
    if instructions:
        parts.append(_INSTRUCTIONS_PREAMBLE + "\n\n" + instructions)
    if persona_instructions:
        who = f"for {persona} specifically" if persona else "for this character specifically"
        parts.append(_PERSONA_INSTRUCTIONS_PREAMBLE.format(who=who) + "\n\n"
                     + persona_instructions)
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
    if insights:
        # Alongside the reflection, for the same reason and with the same framing: it is the
        # person's own material, and it is here to be judged rather than obeyed. The block already
        # carries the "decide which of these apply" instruction, since that judgment belongs to the
        # reader and never to a tag match in this code.
        parts.append(insights)
    if previous:
        parts.append(_summarize_previous(previous))
    parts.append(_CONFIRMATION_RULE)
    return "\n\n".join(parts)


def _batch_title(goals: List[Dict[str, Any]],
                 adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                 instructions: Optional[str] = None,
                 persona_instructions: Optional[str] = None) -> Optional[str]:
    """A short label for the task list: what this batch is ABOUT.

    Without one the server derives a title from the first line of the text, which is the "Act as
    ..." persona line, so every autopilot task in the list is titled after its persona instead of
    its work. Named goals win; an adoption-only batch is titled after the task it took over.

    Instructions are the remaining fallbacks, for a batch with no goals and nothing adopted (the
    always-work rule's instructions-only case): the first non-empty line, stripped of leading
    Markdown furniture ("#", "-", "*", ">") and capped at 80 characters -- so an instructions-only
    batch is titled after what the person asked for rather than the "Act as ..." persona line. That
    title becomes the mail SUBJECT once send-on-completion lands, which is why it matters here and
    not just cosmetically.

    ``persona_instructions`` is tried BEFORE the quest-wide ``instructions``, because a batch that
    exists only because a character has their own standing job on this quest is about THAT job, and
    titling it after the quest's general brief would name work this run is not doing. Falls back to
    "Autopilot run" when instructions of either kind were passed but no line has real content after
    stripping.
    """
    names = [(g.get("name") or "").strip() for g in goals]
    names = [n for n in names if n]
    if not names and adopted_tasks:
        first = str((adopted_tasks[0].get("title") or adopted_tasks[0].get("text") or "")).strip()
        names = [first.splitlines()[0]] if first else []
    if names:
        title = names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} more)"
        return title[:120]
    for source in (persona_instructions, instructions):
        if not source:
            continue
        for line in source.splitlines():
            stripped = _MD_TITLE_FURNITURE_RE.sub("", line).strip()
            if stripped:
                return stripped[:80]
    if persona_instructions or instructions:
        return "Autopilot run"
    return None


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
    """What one autopilot pass did -- the pass task's own reported result (see ``summary_text``).

    ``created`` holds one dict per item the pass created, and it holds NAMES, not just ids:
    ``{task_id, kind, title, quest_id, quest_label, persona_label, awaiting_approval,
    goal_names, adopted_titles}``. That is not decoration. This result is read by a person, in
    their quest, and a line like "Created 1 task(s): atask_d2014273cff6" tells them nothing they
    can act on: it names an internal id instead of the work, and it describes the pass's own
    bookkeeping as if it were the point. The point is what is now being worked on, said in the
    words the person themselves gave that work.
    """
    ran_at: datetime
    dry_run: bool = False
    created: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)      # {quest_id, quest_label, reason}
    proposals: List[Dict[str, Any]] = field(default_factory=list)    # dry-run "would create" items
    errors: List[Dict[str, Any]] = field(default_factory=list)       # {quest_id, quest_label, error}
    # Bookkeeping writes the backend ACCEPTED (200) but did not actually persist. Kept separate
    # from ``errors`` because the pass itself succeeded; what failed is the cadence/miss_streak
    # memory, which silently degrades the NEXT pass (a last_pass_at that never sticks means the
    # cadence gate can never fire). Surfaced in the reported summary so it can never pass silently.
    bookkeeping_warnings: List[Dict[str, Any]] = field(default_factory=list)  # {quest_id, detail}
    # Quests whose canonical next-steps artifact this pass rewrote: {quest_id, path, quest_target}.
    next_steps_refreshed: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def created_task_ids(self) -> List[str]:
        """The ids of everything this pass created, for the gates and for callers that link rows.

        Ids belong HERE, in a field code reads, and not in the prose a person reads.
        """
        return [str(c.get("task_id")) for c in self.created if c.get("task_id")]

    def summary_text(self) -> str:
        """What this pass set in motion, in plain words.

        This is what the pass task reports, so it is what a person sees on their quest until the
        work itself finishes -- at which point the consumer replaces it with the work's OWN output
        (see the module docstring). So it says what is running and what is waiting on them, and it
        never presents the pass's own bookkeeping as the result.
        """
        if self.dry_run:
            return self._dry_run_text()
        lines: List[str] = []
        running = [c for c in self.created if not c.get("awaiting_approval")]
        awaiting = [c for c in self.created if c.get("awaiting_approval")]
        if running:
            lines.append(_count_line(len(running), "Autopilot started one piece of work:",
                                     "Autopilot started {n} pieces of work:"))
            lines.extend(_describe_created(c) for c in running)
        # Proposals are separated from work: a proposed goal is not a task queued behind an
        # approval, it is a suggestion to accept or reject, and saying "before it can run" of a
        # goal that does not exist yet describes the wrong thing.
        proposals = [c for c in awaiting if c.get("kind") == "goal_proposal"]
        awaiting_work = [c for c in awaiting if c.get("kind") != "goal_proposal"]
        if awaiting_work:
            if lines:
                lines.append("")
            lines.append(_count_line(len(awaiting_work),
                                     "Waiting for your approval before it can run:",
                                     "Waiting for your approval before they can run:"))
            lines.extend(_describe_created(c) for c in awaiting_work)
        if proposals:
            if lines:
                lines.append("")
            lines.append(_count_line(len(proposals),
                                     "A goal proposed for you to accept or reject:",
                                     "{n} goals proposed for you to accept or reject:"))
            lines.extend(_describe_created(c) for c in proposals)
        if self.next_steps_refreshed:
            if lines:
                lines.append("")
            lines.append("Next steps rewritten for:")
            for n in self.next_steps_refreshed:
                lines.append(f"  - {n.get('quest_label') or n.get('quest_id')}: {n.get('path')}")
        if self.skipped:
            if lines:
                lines.append("")
            lines.append(_count_line(len(self.skipped), "Nothing started on one quest:",
                                     "Nothing started on {n} quests:"))
            for s in self.skipped:
                lines.append(f"  - {s.get('quest_label') or s.get('quest_id')}: {s.get('reason')}")
        if self.errors:
            if lines:
                lines.append("")
            lines.append(_count_line(len(self.errors), "One quest hit an error:",
                                     "{n} quests hit an error:"))
            for e in self.errors:
                lines.append(f"  - {e.get('quest_label') or e.get('quest_id')}: {e.get('error')}")
        if self.bookkeeping_warnings:
            if lines:
                lines.append("")
            lines.append(
                f"WARNING: autopilot bookkeeping did not persist on "
                f"{len(self.bookkeeping_warnings)} quest(s). The cadence gate reads "
                f"last_pass_at, so until this is fixed those quests are considered due on EVERY "
                f"pass (the per-quest cadence cannot hold them back):")
            for w in self.bookkeeping_warnings:
                label = w.get("quest_label") or w.get("quest_id")
                lines.append(f"  - {label}: {w.get('detail')}")
        if not lines:
            lines.append("No quest has autopilot switched on, so this pass had nothing to work on.")
        return "\n".join(lines)

    def _dry_run_text(self) -> str:
        """The dry run's report: what a real pass WOULD do, nothing created.

        Kept id-bearing where a real pass's report is not: a dry run is read while setting the
        thing up, by someone checking that the right goals and the right recurring tasks were
        picked, and an id is what they check against.
        """
        lines = ["Autopilot dry run: nothing was created. Here is what WOULD happen:"]
        for p in self.proposals:
            label = p.get("quest_label") or p.get("quest_id")
            if p.get("kind") == "goal_proposal":
                lines.append(f"  - Propose a goal on {label}: {p.get('title')}")
                continue
            names = ", ".join(n for n in (p.get("goal_names") or []) if n)
            line = (f"  - Work on {label} as {p.get('persona_label') or 'the assistant'} "
                    f"({p.get('scope')}): {names or p.get('goal_ids')}")
            # Adoption CLOSES the user's own recurring tasks, so a report that omitted it
            # would hide the most consequential thing the pass does.
            adopted = p.get("adopted_task_ids")
            if adopted:
                line += f", adopting and closing recurring task(s) {adopted}"
            # Which brief this run would be working to. On a roster where two characters do
            # different jobs, this is the line that shows the routing landed the right way round.
            instructions_from = p.get("instructions_from")
            if instructions_from:
                line += f", working {instructions_from}"
            lines.append(line)
        for s in self.skipped:
            lines.append(f"  - Skip {s.get('quest_label') or s.get('quest_id')}: {s.get('reason')}")
        for e in self.errors:
            lines.append(f"  - Error on {e.get('quest_label') or e.get('quest_id')}: {e.get('error')}")
        if not (self.proposals or self.skipped or self.errors):
            lines.append("  - Nothing: no quest has autopilot switched on.")
        return "\n".join(lines)


def _count_line(n: int, one: str, many: str) -> str:
    """``one`` when there is exactly one of something, otherwise ``many`` with ``{n}`` filled in.

    A person reading "Created 1 task(s)" is reading a template that was never finished.
    """
    return one if n == 1 else many.format(n=n)


def _quest_label(quest: Dict[str, Any], quest_id: str) -> str:
    """A quest said the way its owner would say it: its outcome, or the id if it has none.

    The outcome IS the quest's name in the product (a quest is stated as the outcome it is for), so
    this is the phrase the person recognizes. Truncated, because an outcome can be a paragraph and
    this goes in a one-line report.
    """
    label = str(quest.get("outcome") or "").strip().splitlines()[0] if quest.get("outcome") else ""
    if not label:
        return quest_id
    return label if len(label) <= 80 else label[:77].rstrip() + "..."


def _goal_name(goal: Dict[str, Any]) -> str:
    return str(goal.get("name") or "").strip() or "(untitled goal)"


def _task_label(task: Dict[str, Any]) -> str:
    """A task said by its title, falling back to its first line. Never a bare id."""
    text = str(task.get("title") or task.get("text") or "").strip()
    first = text.splitlines()[0].strip() if text else ""
    if not first:
        return str(task.get("id") or task.get("task_id") or "an unnamed task")
    return first if len(first) <= 80 else first[:77].rstrip() + "..."


def _describe_created(item: Dict[str, Any]) -> str:
    """One created item, described by NAME: the work, whose quest it serves, who is doing it."""
    title = str(item.get("title") or "").strip() or "an unnamed piece of work"
    line = f"  - {title}"
    quest_label = str(item.get("quest_label") or "").strip()
    # Which quest, unless the title already says so. A proposed goal is titled after the quest's
    # own outcome, so naming the quest again gave "Next step toward: <outcome> (on <outcome>)".
    if quest_label and quest_label.lower() not in title.lower():
        line += f" (on {quest_label})"
    persona = str(item.get("persona_label") or "").strip()
    if persona:
        line += f", as {persona}"
    adopted = [t for t in (item.get("adopted_titles") or []) if t]
    if adopted:
        # Adoption takes over tasks the PERSON scheduled and closes the originals, so it can never
        # go unsaid: they would otherwise be waiting for runs that are never going to happen.
        line += (". It also takes over, and closes, the recurring task"
                 f"{'s' if len(adopted) > 1 else ''} you had scheduled: {', '.join(adopted)}")
    return line


class AutopilotPass:
    """Runs ONE autopilot pass against a Quest client. See the module docstring for the algorithm.

    ``client`` is a ``QuestClient`` (or any object with the same methods this class calls:
    ``list_quests``, ``get_quest_autopilot``, ``list_quest_goals``, ``get_goal``, ``list_tasks``,
    ``list_open_decisions_for_quest``, ``create_task``, ``update_task``,
    ``update_quest_autopilot``, and optionally ``create_goal``, ``get_daily_reflection``,
    ``get_period_reflection``, ``get_insights_collection`` and ``list_collection_entries`` -- a
    client missing the optional ones simply composes a batch without that material, exactly as
    before they existed).

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
                 backpressure: bool = False,
                 adopt_recurring_default: Optional[bool] = None,
                 quest_folder_map: Optional[Dict[str, str]] = None,
                 now: Optional[Callable[[], datetime]] = None):
        self._client = client
        self._team_id = team_id or ""
        self._persona_resolver = persona_resolver
        self._daily_budget = daily_budget if daily_budget and daily_budget > 0 else DEFAULT_TEAM_DAILY_BUDGET
        self._backpressure = bool(backpressure)
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
        # Insights are USER-scoped too, so the pass reads them ONCE, over the widest window it
        # could need, and each quest narrows that one result to its own ``last_pass_at`` in memory.
        # ``None`` means "not read yet" (an empty context is a legitimate result and must not be
        # mistaken for a cache miss).
        self._insights_cache: Optional[InsightsContext] = None
        # The id of the pass task currently being run, set in ``run`` and stamped onto everything
        # this pass creates (see _create_autopilot_task). None until a pass starts.
        self._pass_task_id: Optional[str] = None

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

    def _instructions_source(self, persona: Optional[str],
                             persona_instructions: Optional[str],
                             instructions: Optional[str]) -> Optional[str]:
        """Whose standing instructions this batch is carrying, in words, for the dry-run report.

        Both can apply at once, and when they do BOTH are named: the character's brief governs
        where the two disagree, but the quest's still rides into the same run, and a report that
        hid it would be describing a shorter brief than the one that gets sent.
        """
        sources: List[str] = []
        if persona_instructions:
            who = self._persona_label(persona) or "this character"
            sources.append(f"{who}'s own standing instructions")
        if instructions:
            sources.append("this quest's standing instructions")
        return ", plus ".join(sources) if sources else None

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

    def _insights(self, autopilot_cfg: Dict[str, Any]) -> InsightsContext:
        """The person's unacted captures since THIS quest's last pass.

        Read once per pass (insights are user-scoped, so re-reading them per quest would be the
        same entries at N times the cost), then narrowed per quest against the ``last_pass_at``
        this pass is about to overwrite. That field already exists for exactly this question -- it
        is the pass's own record of when it last looked at this quest -- so "what has the person
        captured since I last ran" needs no separate freshness tracker, and cannot drift out of
        step with the cadence gate that reads the same stamp.

        A quest that has never had a pass (no ``last_pass_at``, or an unparsable one) sees the
        whole default window rather than nothing: on a first run, everything recent IS new.

        Best-effort by construction (``collect_unacted_insights`` never raises and returns an empty
        context for a client with no insights methods), so a backend or client without these
        endpoints composes exactly the batch text it composed before.
        """
        if self._insights_cache is None:
            self._insights_cache = collect_unacted_insights(self._client, now=self._now())
            found = len(self._insights_cache.insights)
            log.info("autopilot: read %d unacted insight(s) from the last %d days",
                     found, self._insights_cache.window_days)
        return self._insights_cache.narrow_to(_parse_dt(autopilot_cfg.get("last_pass_at")))

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
        # The pass task's OWN id, stamped onto everything this pass creates as ``parent_task_id``.
        # That link is what lets a consumer answer "what did autopilot actually do" with the work
        # itself rather than with a list of ids: the created task's result can be rolled back onto
        # the pass row that made it. Without it, the two rows are unrelated as far as the data is
        # concerned, and the pass can only ever report its own bookkeeping.
        self._pass_task_id = str(task.get("id") or task.get("task_id") or "") or None
        # A per-quest pass (the hybrid schedule, quest_autopilot_design.md's autopilot spec
        # section A) carries its quest's id in ``goal_id`` -- the team pass carries none. Scope
        # this run to exactly that one quest instead of scanning the whole team.
        only_quest_id = str(task.get("goal_id") or "") or None

        quests = self._eligible_quests(only_quest_id)
        budget_used = 0 if dry_run else self._count_autopilot_tasks_today()

        for quest in quests:
            quest_id = str(quest.get("quest_id") or quest.get("id") or "")
            if not quest_id:
                continue
            label = _quest_label(quest, quest_id)
            try:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id, label,
                              f"today's budget of {self._daily_budget} autopilot task(s) is used up")
                    continue
                gate_reason = self._gate_quest(quest, quest_id)
                if gate_reason:
                    self._skip(result, quest_id, label, gate_reason)
                    continue
                budget_used = self._run_one_quest(quest, quest_id, dry_run, budget_used, result)
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
        # The characters on duty today who carry standing instructions of their OWN. Each is a
        # separate job on this quest, so each is a separate batch below: the always-work rule is
        # per PERSONA, not per quest. A quest-level rule would let one character's goal work
        # satisfy it and leave the Saturday reviewer, whose whole existence on this quest IS their
        # instructions, silent on the day they were rostered for. Read from the on-duty entries
        # rather than the whole roster, since being on duty is what makes today their day.
        instructed_on_duty: List[str] = []
        for entry in persona_entries_on_duty(autopilot_cfg, self._now()):
            rep_id = str(entry.get("rep_id") or "")
            if rep_id and str(entry.get("instructions") or "").strip():
                instructed_on_duty.append(rep_id)
        if instructed_on_duty:
            log.info("autopilot: quest %s has %d persona(s) on duty with standing instructions",
                     quest_id, len(instructed_on_duty))

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
        # What they have captured and not yet acted on since this quest's own last pass. Same
        # user-scoped read, cached for the pass; the per-quest cutoff is applied in memory.
        insights = self._insights(autopilot_cfg)
        insights_text = insights.as_text() or None

        quest_label = _quest_label(quest, quest_id)
        produced = False
        # The always-work rule: standing instructions that describe a deliverable must produce a
        # batch per due pass EVEN WHEN no ai_help goal is in the current scope -- otherwise the
        # migration case (a hand-authored daily brief replaced by instructions) silently stops on
        # any day the quest has no eligible goal. It applies per WRITER of instructions, so both
        # the quest-wide field and each instructed persona on duty join the condition here.
        # ``instructed_on_duty`` is a separate term rather than folded into ``instructions``
        # because the two are independent: a quest can hand every character the same brief, hand
        # each a different one, or both, and a character rostered for today with their own brief
        # has work to do whether or not the quest itself carries one.
        # With either kind set, the goal-proposal ``elif`` becomes unreachable for this quest,
        # which is deliberate (see the comment at the ``elif`` below) -- goal proposals stay
        # untouched for every quest that carries NO instructions of either kind.
        if target_goals or adopted or instructions or instructed_on_duty:
            if target_goals or adopted:
                batches = self._batches_with_adopted(target_goals, adopted, autopilot_cfg)
                # The case this whole feature exists for: the day's goals go to whoever routing
                # resolves (the weekday worker), and each instructed persona on duty who did not
                # already get a batch gets their own EMPTY one, so their standing job happens on
                # the same pass instead of waiting for a day with no goals in it.
                covered = {persona for persona, _goals, _tasks in batches}
                for rep in instructed_on_duty:
                    if rep not in covered:
                        covered.add(rep)
                        batches.append((rep, [], []))
            elif instructed_on_duty:
                # No goals and nothing adopted, but characters are rostered today with their own
                # instructions: one batch each. They still carry the quest-wide instructions too,
                # if the quest has any -- the two are layered, never either/or.
                log.info("autopilot: quest %s -- no eligible goal this pass, working the standing "
                        "instructions of %d persona(s) on duty", quest_id, len(instructed_on_duty))
                batches = [(rep, [], []) for rep in instructed_on_duty]
            else:
                # Instructions-only: exactly one batch, no goals and nothing adopted. Persona
                # comes from the quest's roster on duty today (first entry, day-matched before
                # unrestricted), else the consumer's fallback resolver, else no persona --
                # the same precedence goal-driven batching uses, just with no goal to resolve it
                # FROM.
                log.info("autopilot: quest %s -- no eligible goal this pass, working the "
                        "standing instructions alone", quest_id)
                on_duty = personas_on_duty(autopilot_cfg, self._now())
                if on_duty:
                    persona = on_duty[0]
                elif self._persona_resolver is not None:
                    try:
                        resolved = self._persona_resolver({})
                        persona = str(resolved) if resolved else None
                    except Exception:  # noqa: BLE001 -- a bad fallback must never break a pass
                        log.info("autopilot: persona fallback_resolver raised while resolving an "
                                "instructions-only batch; treating as no match", exc_info=True)
                        persona = None
                else:
                    persona = None
                batches = [(persona, [], [])]
            for persona, goals, tasks in batches:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id, quest_label,
                               f"today's budget of {self._daily_budget} autopilot task(s) ran out "
                               f"part-way through this quest")
                    break
                # Resolved per BATCH, from the whole roster rather than today's on-duty entries:
                # a goal's own ``assignee_rep_id`` can put a character on this quest on a day
                # their ``days`` do not name, and they should still arrive with their brief.
                persona_instructions = persona_instructions_for(autopilot_cfg, persona)
                title = _batch_title(goals, tasks, instructions=instructions,
                                     persona_instructions=persona_instructions)
                if dry_run:
                    result.proposals.append({
                        "quest_id": quest_id, "quest_label": quest_label, "kind": "work_batch",
                        "persona": persona, "persona_label": self._persona_label(persona),
                        "goal_ids": [g.get("id") for g in goals],
                        "goal_names": [_goal_name(g) for g in goals] or
                                      (["standing instructions"]
                                       if (instructions or persona_instructions) else []),
                        "scope": scope_label,
                        "adopted_task_ids": [t.get("id") or t.get("task_id") for t in tasks],
                        # Whose instructions are driving this batch. A dry run is read by someone
                        # checking that the right character was picked for the right job, and
                        # "standing instructions" alone does not say whose -- which is the only
                        # question a two-character roster raises.
                        "instructions_from": self._instructions_source(
                            persona, persona_instructions, instructions),
                    })
                    produced = True
                    # A dry-run still SIMULATES budget consumption (one unit per batch that
                    # WOULD be created), so the report honestly shows a later quest going quiet
                    # once the budget is exhausted -- exactly what a real pass would do.
                    budget_used += 1
                    continue
                task_id = self._create_batch_task(quest, quest_id, persona, goals, mode,
                                                  title=title,
                                                  scope_label=scope_label, adopted_tasks=tasks,
                                                  next_steps=standing_next_steps,
                                                  previous=previous,
                                                  reflection=reflection_text,
                                                  insights=insights_text,
                                                  instructions=instructions,
                                                  persona_instructions=persona_instructions)
                if task_id:
                    result.created.append({
                        "task_id": task_id, "kind": "work_batch", "quest_id": quest_id,
                        "quest_label": quest_label, "title": title,
                        "persona_label": self._persona_label(persona),
                        "awaiting_approval": mode != "act",
                        "goal_names": [_goal_name(g) for g in goals],
                        "adopted_titles": [_task_label(t) for t in tasks],
                    })
                    budget_used += 1
                    produced = True
                    self._close_adopted(tasks, task_id, quest_id, result)
            if produced and not dry_run:
                self._refresh_next_steps(quest_id, target_goals, adopted, scope_label, previous,
                                         result, quest_label=quest_label,
                                         reflection_note=reflections.one_line(),
                                         insights_note=insights.one_line())
        # The goal-proposal path is untouched: with instructions set -- the quest's own, or any
        # carried by a persona on duty today -- the branch above always matches, so this ``elif``
        # is unreachable for that quest. Proposals keep their exact current behaviour for quests
        # WITHOUT instructions of either kind, and no double-proposing can occur. Instructions are
        # deliberately never threaded into ``propose_next_goal``.
        elif planning == "plan_and_work":
            if budget_used < self._daily_budget:
                skipped_because = self._handle_proposal(quest, quest_id, quest_label, mode,
                                                        dry_run, result)
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

    # --- gates -------------------------------------------------------------------------------

    def _eligible_quests(self, only_quest_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The quest(s) this pass should work.

        ``only_quest_id`` set (a per-quest pass, the hybrid schedule's own series): ONE
        ``_quest_state`` read, no ``list_quests`` call at all -- cheaper than the team pass, and
        the point of a dedicated series is that it already knows which quest it is for. Opted in
        -> that one quest. No longer opted in (mode flipped back to off since the series was
        created) -> ``[]``, logged at INFO; the poller retires the now-orphaned pass on its next
        sweep, this method just declines to work it.

        ``only_quest_id`` None: every opted-in quest (``autopilot.mode`` in suggest/act). A pass
        created by this runner always names its quest, so this is the unscoped fallback -- what a
        pass with no quest id can still honestly mean -- not a second scheduling shape.

        TWO reads per quest either way, deliberately. The team quest LISTING
        (``GET /api/teams/{team_id}/quests``) returns only
        ``{quest_id, outcome, completed, owner_user_ids}`` -- it does NOT include the ``autopilot``
        block. Reading the opt-in mode off those rows would find no ``autopilot`` on ANY quest,
        treat every one as mode "off", and make the whole feature a silent no-op forever. The
        ``autopilot`` settings live on the full QuestState, so we fetch it per quest
        (``get_quest_autopilot`` -> ``GET /api/quests/{quest_id}/state``) and merge it onto the
        listing row.
        """
        if only_quest_id:
            state = self._quest_state(only_quest_id)
            autopilot_cfg = (state.get("autopilot") or {}) if state else {}
            mode = str(autopilot_cfg.get("mode") or "off")
            if mode not in ("suggest", "act"):
                log.info("autopilot: quest %s mode=%r -- no longer opted in; this pass should be "
                        "retired", only_quest_id, mode)
                return []
            return [{
                "quest_id": only_quest_id,
                "outcome": state.get("outcome") or "",
                "autopilot": autopilot_cfg,
            }]

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
                            quest_label: str = "",
                            reflection_note: str = "",
                            insights_note: str = "") -> None:
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
                                          reflection_note=reflection_note,
                                          insights_note=insights_note)
        try:
            published = publish_next_steps(self._client, quest_id, folder, next_steps)
        except Exception as e:  # noqa: BLE001 -- the artifact must never fail an otherwise-good pass
            log.warning("autopilot: could not refresh next steps for quest %s", quest_id,
                        exc_info=True)
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "quest_label": quest_label or quest_id,
                 "detail": f"the next-steps artifact was not refreshed ({type(e).__name__}: {e})"})
            return
        result.next_steps_refreshed.append({
            "quest_id": quest_id, "path": published.sync_path,
            "quest_target": published.quest_target,
            "quest_label": quest_label or quest_id,
        })
        if published.detail:
            # The local file is current either way; what did not happen is the Quest-side write, and
            # a silently local-only artifact is how the two views drift apart again.
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "quest_label": quest_label or quest_id,
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
        # tz= the quest's own run_timezone; absent one, cadence_due falls back to the runner's
        # local clock (never UTC). This is the SAME predicate the poller's schedule-correction
        # sweep reads (A3 of the autopilot spec), so the schedule and this gate can never disagree
        # about whether today's occurrence is due.
        #
        # A pending "Run now" request satisfies the cadence gate on its own. Without that, the
        # button would be a no-op on exactly the day someone is most likely to press it (the quest
        # already ran and they want another pass), and a no-op that reports success is the silent
        # failure this codebase bans.
        if (not cadence_due(autopilot_cfg, self._now(), tz=autopilot_cfg.get("run_timezone"))
                and not run_requested(autopilot_cfg)):
            return "cadence not due yet"
        if self._backpressure and self._has_backpressure(quest_id):
            return "backpressure: a previous autopilot task for this quest is still open"
        # Behind the SAME opt-in as task backpressure, and for the same reason. An unresolved
        # decision is a question the person has not answered yet; treating it as a stop sign makes
        # their silence an instruction to down tools, and the quest stays frozen for as long as
        # they do not get to it. 02ba2de removed that reading for an unfinished TASK but left it
        # standing here, which is the more direct case: a quest with one open question stopped
        # dead while everything independent of that question sat there, workable. The run sees the
        # open decision in its context and is told to carry on around it.
        if self._backpressure and self._has_open_hold_decision(quest_id):
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
        """True when a task AUTOPILOT ITSELF authored for this quest is still open, so the last
        batch it produced is not finished and it should not stack another on top.

        OFF unless ``RunnerConfig.autopilot_backpressure`` is set. It used to be unconditional,
        which meant one unapproved suggestion or one unanswered decision stopped a quest outright
        and indefinitely -- the person's silence read as a stop sign. Nobody asked for that
        behaviour and it is the opposite of how a good colleague handles a pending question.

        "Not finished" covers two different things, and only one of them is about the person:
        ``queued``/``in_progress`` is work that has not RUN yet (the AI's own backlog), while
        ``needs_you``/``suggested`` is genuinely waiting on them -- a decision to resolve, a
        proposal to approve. Either way the answer is the same, which is why one check covers both.

        Only autopilot-authored tasks count. A recurring task the person set up themselves (a daily
        brief, say) is not autopilot's backlog and must never gate it.

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

        It also carries ``parent_task_id`` -- the id of the PASS that created it. That link is what
        makes "what did autopilot do today" answerable with the work itself: a consumer can roll
        the finished task's own output back onto the pass row, so the person reads the work instead
        of the scanner's bookkeeping. A client whose ``create_task`` predates the argument simply
        creates the task without it, exactly as before.
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
        if self._pass_task_id:
            kwargs["parent_task_id"] = self._pass_task_id
        try:
            created = self._client.create_task(text, **kwargs) or {}
        except TypeError:
            # An older/stand-in client without ``parent_task_id``. The link is an improvement to
            # how the pass reports, never a requirement for it to work, so lose the link rather
            # than the task.
            kwargs.pop("parent_task_id", None)
            created = self._client.create_task(text, **kwargs) or {}
        task_id = created.get("id") or created.get("task_id")
        if not task_id:
            return None
        return str(task_id)

    def _create_batch_task(self, quest: Dict[str, Any], quest_id: str, persona: Optional[str],
                           goals: List[Dict[str, Any]], mode: str, *,
                           title: Optional[str] = None,
                           scope_label: Optional[str] = None,
                           adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                           next_steps: Optional[str] = None,
                           previous: Optional[Dict[str, Any]] = None,
                           reflection: Optional[str] = None,
                           insights: Optional[str] = None,
                           instructions: Optional[str] = None,
                           persona_instructions: Optional[str] = None) -> Optional[str]:
        text = compose_batch_text(str(quest.get("outcome") or ""), goals,
                                  self._persona_label(persona),
                                  scope_label=scope_label, adopted_tasks=adopted_tasks,
                                  next_steps=next_steps, previous=previous,
                                  reflection=reflection, insights=insights,
                                  instructions=instructions,
                                  persona_instructions=persona_instructions)
        try:
            return self._create_autopilot_task(
                quest, quest_id, text, mode, persona=persona,
                title=title if title is not None
                else _batch_title(goals, adopted_tasks, instructions=instructions,
                                  persona_instructions=persona_instructions))
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

    def _open_proposal(self, quest_id: str) -> Optional[Dict[str, Any]]:
        """A proposed-goal task from an earlier pass that is still waiting on the person, or None.

        A proposal is not work: it is one question ("shall this be a goal?"), and asking it again
        because it has not been answered yet is how a person ends up with the same suggestion in
        their list every single day, and the same "Waiting for your approval" line in every pass
        report. Reported 2026-08-22, on a quest whose proposals had been repeating: "not useful at
        all".

        This is NOT the backpressure gate (which is opt-in, off by default, and deliberately so:
        an unanswered question must not stop a quest from doing work that is independent of it).
        It is narrower and always on, because a duplicate of a pending question is never the work
        that was blocked; it is only more of the question.
        """
        try:
            tasks = self._client.list_tasks(
                team_id=self._team_id or None, goal_id=quest_id) or []
        except Exception:  # noqa: BLE001 -- fail OPEN: a bad read must not silence a proposal
            log.info("autopilot: could not check for an open proposal on quest %s", quest_id,
                     exc_info=True)
            return None
        for t in tasks:
            if not self._is_autopilot_authored(t):
                continue
            if str(t.get("status", "")).strip().lower() not in OPEN_TASK_STATUSES:
                continue
            if str(t.get("text") or "").lstrip().startswith(PROPOSAL_TEXT_PREFIX):
                return t
        return None

    def _handle_proposal(self, quest: Dict[str, Any], quest_id: str, quest_label: str, mode: str,
                         dry_run: bool, result: AutopilotResult) -> Optional[str]:
        """Propose the quest's next goal, unless the last pass's proposal is still unanswered.

        Returns the skip reason when nothing was proposed, so the caller can report it as a skip
        and leave the budget alone.
        """
        title, description = propose_next_goal(quest)
        pending = None if dry_run else self._open_proposal(quest_id)
        if pending is not None:
            asked = _task_label(pending)
            return (f"a proposed goal from an earlier pass is still waiting for your yes or no "
                    f"({asked})")
        if dry_run:
            result.proposals.append({
                "quest_id": quest_id, "quest_label": quest_label, "kind": "goal_proposal",
                "title": title, "description": description,
            })
            return None
        created_goal_id = self._maybe_create_goal(quest_id, title, description, mode)
        task_text = f"{PROPOSAL_TEXT_PREFIX} {title}\n\n{description}"
        if created_goal_id:
            task_text += f"\n\n(Created as goal {created_goal_id} on this quest.)"
        # A proposed goal is ALWAYS surfaced for a human to accept, even on an `act` quest: the
        # design keeps AI-created goals reviewable ("attributed and editable"), so force suggested.
        task_id = self._create_autopilot_task(
            quest, quest_id, task_text, mode, title=title, force_suggested=True)
        if task_id:
            # ALWAYS awaiting approval: a proposed goal is surfaced for a human to accept even on
            # an ``act`` quest (see _create_autopilot_task's force_suggested).
            #
            # The title here is the proposal's own title, with no "Proposed goal:" prefix glued
            # on: the report prints proposals under their own heading, and prefixing them there
            # too produced "Proposed goal: Next step toward: <outcome> (on <outcome>)" -- the same
            # words three times in one line.
            result.created.append({
                "task_id": task_id, "kind": "goal_proposal", "quest_id": quest_id,
                "quest_label": quest_label, "title": title,
                "awaiting_approval": True,
            })
        return None

    def _update_pass_bookkeeping(self, quest_id: str, quest_label: str,
                                 autopilot_cfg: Dict[str, Any],
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
                {"quest_id": quest_id, "quest_label": quest_label or quest_id,
                 "detail": f"update raised {type(e).__name__}: {e}"})
            return
        echoed = (resp or {}).get("autopilot")
        if not isinstance(echoed, dict):
            return  # nothing echoed back to check against (an older/mock client); assume nothing
        unpersisted = [k for k, v in fields.items() if echoed.get(k) != v]
        if unpersisted:
            detail = (f"the backend accepted the PATCH but did not persist {unpersisted} "
                      f"(its autopilot update schema does not accept these bookkeeping fields)")
            log.warning("autopilot: quest %s -- %s", quest_id, detail)
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "quest_label": quest_label or quest_id, "detail": detail})

    def _skip(self, result: AutopilotResult, quest_id: str, quest_label: str,
              reason: str) -> None:
        log.info("autopilot: skipping quest %s (%s)", quest_id, reason)
        result.skipped.append({"quest_id": quest_id, "quest_label": quest_label,
                               "reason": reason})
