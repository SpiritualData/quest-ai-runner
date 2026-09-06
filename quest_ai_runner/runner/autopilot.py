"""Autopilot — the recurring "autopilot pass" task that scans opted-in quests and makes progress.

Design of record: ``quest_autopilot_design.md`` (Part B). In one sentence: Autopilot is itself a
recurring assistant task (``task_kind == "autopilot"``, routed by ``runner.executor`` before the
normal deep-run path). Each pass:

  1. Lists the team's quests; keeps the ones opted in (``autopilot.mode`` in ``suggest``/``act``).
  2. Gates each, cheapest first: a team-wide daily budget, per-quest cadence, the roster's day
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
     period review) and the insights they have CAPTURED but not yet acted on since this quest's
     last pass (``runner.insights``, with the person's own category tags shown beside each one).
     Both are read once per pass, since both are user-scoped rather than per quest. Everything
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

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .insights import InsightsContext, collect_unacted_insights
from .local_time import now_in_zone
# The client's OWN period formats, borrowed rather than restated: a goal this module proposes is
# created through ``QuestClient.create_goal``, so the shape it must satisfy is that client's, and a
# second copy here would be a second thing to keep in step with the backend's period_utils.
from .quest_client import _PERIOD_RE as _GOAL_PERIOD_RE
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

# The standing briefs a batch works to where the person has written none of their own. There are
# TWO, and they default INDEPENDENTLY, because they answer two different questions: the quest-wide
# brief says HOW to work and how to deliver and applies to every character on the quest, while the
# persona brief says WHAT to work on and is one character's own job here. A quest whose owner wrote
# only one of them keeps their text for that slot and gets the built-in one for the other, rather
# than losing the built-in floor for both the moment they write a line anywhere.
#
# The authoritative copies are the backend's: a current quest payload carries them read-only as
# ``autopilot.default_quest_instructions`` and ``autopilot.default_persona_instructions``
# (server-derived, never client-settable), and THOSE values always win here. That is the point of
# serving them: improving the text on the server improves every quest that never wrote its own
# brief, immediately, with no client release and no per-quest migration.
#
# These bundled copies exist for the other case. Against a backend that predates the fields -- or
# any client that simply does not serve them -- a character on duty would otherwise have no
# specification at all and the pass would go silent, which is the exact failure these defaults were
# introduced to remove. So the runner degrades to a real brief rather than to nothing. They are a
# fallback and never a second source of truth: if the two ever disagree, the server's text is the
# one that ran.
# The texts below are VERBATIM copies of the server's, word for word, and must stay that way. They
# were written and confirmed by a person, so a paraphrase here is not a smaller version of the same
# brief: an earlier draft of this constant tightened the "doable without thinking" paragraph and
# lost its concrete list ("the link to open, the route and the place to run, the command to
# paste"), which is the entire instruction that paragraph exists to give. Two texts that differ are
# two briefs, and the one that runs against an older backend would be the weaker one. The backend's
# own runner-contract test asserts both pairs equal, so a reword here fails there. Copy them, never
# re-word them.

# HOW to work and how to deliver. The default for the QUEST-WIDE ``instructions`` slot, so it
# applies to every character the quest puts on duty.
BUNDLED_DEFAULT_QUEST_INSTRUCTIONS = (
    "Ground every claim about where things stand in this quest's own outcome, goals, notes and "
    "files, never in this prompt. If the plan has slipped, say so plainly and say what you would "
    "change.\n\n"
    "When you hand the person something to do, make it doable without thinking. Give the exact "
    "action with its specifics already filled in: the link to open, the route and the place to "
    "run, the command to paste, the message to send and who to send it to. Never a category of "
    "task, and never something they have to work out before they can start.\n\n"
    "Your result is what reaches the person, so write it as the finished thing rather than a "
    "report about doing it. No preamble, and no second copy underneath. Keep it short enough to "
    "read on a phone. Use Markdown for formatting, never raw HTML, and never em dashes.\n\n"
    "End with what is left, and with anything only the person can decide."
)

# WHAT to work on. The default for a character's OWN ``instructions`` on this quest, so it is the
# job description a run gets when nobody has written one for that character.
BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS = (
    "Work this quest today. Read its outcome, its current goals at every horizon, and what has "
    "happened on it recently, then pick the one or two things that most move it forward right now "
    "and do them.\n\n"
    "Decide from the quest and its context whether your role here is to do the work yourself or to "
    "set the person up to do it. Both are real. Some quests want a draft, a comparison, or a "
    "decision worked through with its options. Others want the state of things read back clearly, "
    "or the next steps laid out for the person to carry out. Judge which this quest is asking for "
    "rather than defaulting to one."
)

# How many of a horizon's current goals the goal ladder lists before it says "+N more". Eight is
# chosen against a real quest that carries around twenty goals in one month: enough that a person
# recognizes their own plan in the list, few enough that the ladder stays context rather than
# becoming the bulk of the prompt. Overridable per call (``current_goal_ladder``).
DEFAULT_LADDER_PER_SCOPE = 8

# How each horizon is named in the ladder. The period id rides alongside in parentheses, so the
# reader gets both the word they think in and the exact period the goals are filed under.
_LADDER_SCOPE_LABELS = {
    "day": "Today",
    "week": "This week",
    "month": "This month",
    "quarter": "This quarter",
    "year": "This year",
}

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


def _current_period_groups(goals_payload: Dict[str, Any], now: datetime
                           ) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    """Every period group that is CURRENT at ``now``, finest scope first: ``(scope, period, goals)``.

    THE ONE period matcher in this module. Both readers of "what period is this quest in right
    now" go through it -- ``current_scope_label`` (which period the pass says it is working in) and
    ``current_goal_ladder`` (every current horizon, as the context the run serves). A second
    matcher would be a second place to get the backend's separator formats wrong, and the two
    answers drifting apart is precisely how a run would end up told it is advancing a month that is
    not the current one. Groups within a scope keep the payload's own order.
    """
    groups = goals_payload.get("period_groups") or []
    current: List[Tuple[str, str, List[Dict[str, Any]]]] = []
    for scope in _SCOPE_ORDER:
        key = _current_period_key(scope, now)
        for group in groups:
            if str(group.get("time_scope", "")).strip().lower() != scope:
                continue
            if str(group.get("period", "")).strip() == key:
                current.append((scope, str(key), list(group.get("goals") or [])))
    return current


def current_scope_label(goals_payload: Dict[str, Any], now: datetime) -> str:
    """WHICH PERIOD this quest is planning in right now, e.g. ``"day:2026-07-12"``.

    The FINEST current period group in the quest's ``list_quest_goals`` grouping (day beats week
    beats month beats quarter beats year), or ``"unscoped"`` when no group is current at all (a
    quest with only "custom"-scoped goals, or with none).

    This is a LABEL, not a selection. It used to be the by-product of picking which goals a pass
    would work, and it survived that job's removal because four other things genuinely depend on
    knowing the quest's horizon: ``_reflection_periods`` (which period review to read first),
    ``_previous_period_summary`` (what "last period" means for this quest), the ``Scope:`` line in
    the batch text, and ``goal_period_for_scope`` (what period a newly proposed goal is filed
    under). None of those is answerable without it, and none of them needs a goal picked.

    An empty current group still names the scope. Having planned today and completed everything in
    it does not move the quest into next week, and a label is a statement about the calendar rather
    than about how much work is left.
    """
    for scope, key, _goals in _current_period_groups(goals_payload, now):
        return f"{scope}:{key}"
    return "unscoped"


def _ladder_order_key(goal: Dict[str, Any]) -> Tuple[int, str]:
    """Sort key for one ladder rung: nearest deadline first, undated last.

    Payload order breaks every tie, since ``sorted`` is stable -- so a horizon whose goals carry no
    deadlines at all is listed exactly as the person's own plan lists them.
    """
    deadline = str(goal.get("deadline") or "").strip()
    return (0, deadline) if deadline else (1, "")


def current_goal_ladder(goals_payload: Dict[str, Any], now: datetime,
                        per_scope_limit: int = DEFAULT_LADDER_PER_SCOPE
                        ) -> List[Dict[str, Any]]:
    """The person's CURRENT goals at EVERY horizon: day, week, month, quarter, year.

    THE ONLY WAY GOALS REACH A RUN. Goals are context for autopilot and never an assignment to it:
    nothing here or anywhere else in this module picks a goal, hands one to a character, or turns
    one into work. What a run produces is defined by its persona's standing instructions; the
    ladder is what that output has to add up to (see ``compose_batch_text``'s framing, which says
    so to the reader in as many words).

    Returns one rung per current horizon that has anything in it, finest first::

        [{"scope": "day", "period": "2026-07-12",
          "goals": [{"id": ..., "name": ..., "deadline": ...}, ...],
          "more": 0}, ...]

    Three decisions worth stating, because none of them is an oversight:

      * EVERY current goal is here, including the ones the person is plainly doing themselves. A
        goal the AI will never touch is still what its work has to add up to, and hiding it
        produces a run that optimizes a fragment of a plan it cannot see. Only ``completed``
        excludes a goal, because a finished one is no longer something to move toward.
      * NAMES ONLY (plus a deadline where the goal has one). This is orientation, not a brief:
        pasting descriptions and criteria for a whole year of goals would bury the run's own
        instructions, which are the thing that actually says what to produce.
      * CAPPED per horizon at ``per_scope_limit``, nearest deadline first, with the count of what
        was left out. A real quest carries around twenty goals in a single month, and an uncapped
        dump would cost more attention than the ladder buys back.

    Empty list when no horizon is current or every current one is finished, which composes to
    nothing at all rather than to an empty heading.
    """
    limit = per_scope_limit if per_scope_limit and per_scope_limit > 0 else DEFAULT_LADDER_PER_SCOPE
    by_scope: Dict[str, Dict[str, Any]] = {}
    for scope, period, goals in _current_period_groups(goals_payload, now):
        rung = by_scope.setdefault(scope, {"period": period, "goals": []})
        rung["goals"].extend(g for g in goals if not g.get("completed"))
    ladder: List[Dict[str, Any]] = []
    for scope in _SCOPE_ORDER:
        rung = by_scope.get(scope)
        if not rung or not rung["goals"]:
            continue  # a horizon with nothing current is omitted, not shown as empty
        ordered = sorted(rung["goals"], key=_ladder_order_key)
        shown = ordered[:limit]
        ladder.append({
            "scope": scope,
            "period": rung["period"],
            "goals": [{
                "id": str(g.get("id") or ""),
                "name": _goal_name(g),
                "deadline": str(g.get("deadline") or "").strip(),
            } for g in shown],
            "more": len(ordered) - len(shown),
        })
    return ladder


def goal_period_for_scope(scope_label: str, now: datetime) -> str:
    """The ``period`` to file a newly proposed goal under, from this pass's own scope label.

    ``create_goal`` REQUIRES a period and there is no universal default for one, so it has to be
    derived. The pass already knows the answer: ``current_scope_label`` hands back labels like
    ``"day:2026-07-12"`` or ``"month:2026_09"``, whose right-hand side is already the backend's own
    period id. An ``"unscoped"`` quest has no current period at all, so it falls back to the
    current MONTH: a proposal is a suggestion about what to do next, and a month is the coarsest
    horizon that still says "soon" without pinning the person to a day they did not choose.

    The result is validated against ``QuestClient``'s own accepted formats (the five the backend's
    ``period_utils`` parses) rather than a shape invented here, so a malformed or unfamiliar label
    degrades to the month key instead of riding through to a 400.
    """
    _scope, _sep, period = str(scope_label or "").partition(":")
    period = period.strip()
    if period and _GOAL_PERIOD_RE.match(period):
        return period
    return str(_current_period_key("month", now))


def _weekday_abbrev(now: datetime) -> str:
    return now.strftime("%a")  # "Mon", "Tue", ...


class _HeldPersona:
    """The type of :data:`PERSONA_HELD`. A singleton, so ``is`` is the whole test."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- a debugging aid, never user-facing
        return "PERSONA_HELD"


# Returned by the persona resolvers instead of a rep id when today's answer to "who works this" is
# NOBODY: the work belongs to a character the person rostered for other days.
#
# It is a third answer on purpose, and the reason is a real incident. A quest had Bailey rostered
# Mon-Fri and Batman rostered Sat. On a Saturday, Bailey produced work and emailed the owner,
# because a work item carrying her ``assignee_rep_id`` outranked her own roster entry -- her days
# were advisory. They are authoritative now, and this constant is what makes that expressible:
# ``None`` already means "the plain assistant", so without a distinct value a held item would
# either fall through to the plain assistant or be handed to whichever character happens to be on
# duty. Both are the same mistake as running it on the wrong day. The person chose WHO does this
# work; the only thing the calendar decides is WHEN.
#
# The item that still names a rep is an ADOPTED RECURRING TASK (``assignee_rep_id`` on the task the
# person scheduled). Goals no longer name one at all: they are context for a run, never an
# assignment to one, so nothing about a goal can put a character on duty or take one off.
PERSONA_HELD = _HeldPersona()

# What the persona resolvers can answer: a rep id, ``None`` (the plain assistant), or
# ``PERSONA_HELD`` (nobody today -- this work waits for the day its character is rostered for).
ResolvedPersona = Union[str, None, _HeldPersona]


def _rostered_rep_ids(autopilot_cfg: Dict[str, Any]) -> set:
    """Every rep_id the quest's roster names, ``instructions_only`` entries included.

    Appearing in the roster at all is what puts a character under the day rule: the person opened
    that entry and set their days. A character with NO entry has no day setting to follow, so work
    assigned to them is not held on any day.
    """
    return {str(p.get("rep_id")) for p in (autopilot_cfg.get("personas") or []) if p.get("rep_id")}


def _work_routing_entries(autopilot_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The roster entries work routing can use: every entry not flagged ``instructions_only``.

    A roster made up only of ``instructions_only`` entries is, for routing, no roster at all (the
    flag says exactly that: behave as though this character were not in the roster). That is what
    keeps the day rule from turning an unassigned recurring task into work nobody may do.
    """
    return [p for p in (autopilot_cfg.get("personas") or [])
            if not _truthy(p.get("instructions_only"))]


def resolve_persona(autopilot_cfg: Dict[str, Any], now: datetime,
                    fallback_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                    fallback_context: Optional[Dict[str, Any]] = None) -> ResolvedPersona:
    """Which character this quest routes UNNAMED work to TODAY, or ``PERSONA_HELD`` when the honest
    answer is "nobody, today".

    THE DAY RULE COMES FIRST AND NOTHING OVERRIDES IT: a character works a quest only on the days
    their own roster entry names. Everything below decides how work is routed AMONG the characters
    the person rostered for today; none of it can reach past that set.

      1. A quest ``autopilot.personas`` entry whose ``days`` include today, EXCLUDING entries
         flagged ``instructions_only`` (checked first, so an explicit day-restricted entry wins
         over an unrestricted one for the same day).
      2. A quest ``autopilot.personas`` entry with NO ``days`` restriction, again excluding
         ``instructions_only`` entries (applies any day).
      3. ``PERSONA_HELD`` when the roster DOES name work-routing characters but none of them is on
         duty today. Their work waits; handing one character's work to whoever happens to be around
         is its own wrong answer, and falling through to the plain assistant is the bug that made
         this rule necessary (a quest with standing instructions ran as the plain assistant on days
         nobody was rostered for).
      4. A consumer-injected fallback (e.g. the existing card-vote resolver), given
         ``fallback_context`` (the item being routed, or ``{}`` when there is none).
      5. ``None`` -- the plain assistant persona (no character voice).

    Steps 4 and 5 are reachable ONLY when the roster names no work-routing character at all, which
    is every quest that never configured one. Those quests behave exactly as they always have.

    There is no step reading an ``assignee_rep_id`` off a goal any more, because a goal no longer
    carries one: goals are context for a run and never an assignment to one. The one item that
    still names a rep is an adopted recurring task, and ``resolve_task_persona`` below is where
    that reading lives, so the naming is beside the only input that still exists for it.

    Why ``instructions_only`` is excluded from 1 and 2: a roster entry is two different statements
    at once ("this character is on duty today" and "this character is who unnamed work goes to"),
    and rule 1 beating rule 2 makes the second statement greedy. A quest with a weekday worker and
    a Saturday specialist would read, on Saturday, as "the specialist takes everything" -- so the
    specialist inherits work they were never meant to touch. ``instructions_only`` says this entry
    is only the FIRST statement: the character is on duty and works their own standing
    instructions, and routing behaves as though they were not in the roster at all.
    """
    routing_entries = _work_routing_entries(autopilot_cfg)
    today = _weekday_abbrev(now)
    for persona in routing_entries:
        days = persona.get("days")
        if days and today in days:
            rep_id = persona.get("rep_id")
            if rep_id:
                return str(rep_id)
    for persona in routing_entries:
        if not persona.get("days"):
            rep_id = persona.get("rep_id")
            if rep_id:
                return str(rep_id)
    if any(p.get("rep_id") for p in routing_entries):
        return PERSONA_HELD
    if fallback_resolver is not None:
        try:
            resolved = fallback_resolver(dict(fallback_context or {}))
            if resolved:
                return str(resolved)
        except Exception:  # noqa: BLE001 -- a bad fallback must never break a pass
            log.info("autopilot: persona fallback_resolver raised; treating as no match",
                     exc_info=True)
    return None


def resolve_task_persona(task: Dict[str, Any], autopilot_cfg: Dict[str, Any], now: datetime,
                         fallback_resolver: Optional[Callable[[Dict[str, Any]],
                                                              Optional[str]]] = None
                         ) -> ResolvedPersona:
    """Who works an ADOPTED RECURRING TASK today, under the same day rule.

    A recurring task the person scheduled can name a character in its own ``assignee_rep_id``, and
    that naming is honoured FIRST: it is a human choosing who does this specific thing, which is
    more specific than any roster-wide preference. What it cannot do is beat that character's DAYS,
    which is a statement by the same human about when they work this quest at all. So:

      * named character on duty today, or carrying no roster entry at all (no entry means the
        person set no days for them, so there is no day setting to follow) -> that character;
      * named character rostered but NOT on duty today -> ``PERSONA_HELD``, and the caller leaves
        the occurrence queued to run on its own rather than re-assigning it to whoever is around;
      * no name on the task -> whatever ``resolve_persona`` routes for this quest today, so an
        unassigned occurrence rides along with the character already working the quest instead of
        spawning a second run.
    """
    assignee = task.get("assignee_rep_id")
    if assignee:
        assignee = str(assignee)
        on_duty = {str(entry.get("rep_id"))
                   for entry in persona_entries_on_duty(autopilot_cfg, now)
                   if entry.get("rep_id")}
        if assignee in on_duty or assignee not in _rostered_rep_ids(autopilot_cfg):
            return assignee
        return PERSONA_HELD
    return resolve_persona(autopilot_cfg, now, fallback_resolver, fallback_context=task)


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

    The WHOLE roster is searched, not only today's on-duty entries. One character can hold
    several entries -- a day-restricted one carrying their instructions plus a catch-all that is
    what actually puts them on duty today -- and a character working this quest without their
    standing brief would be a different character than the one the person configured. The first
    entry for that rep with non-empty instructions wins, so a roster that lists the same character
    twice is read the same way ``persona_entries_on_duty`` reads it. (Since the day rule landed,
    every character this quest routes work to today is on duty today; searching the whole roster
    is what makes WHICH of their entries carries the text irrelevant.)

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


def default_quest_instructions_for(autopilot_cfg: Dict[str, Any]) -> str:
    """The QUEST-WIDE brief a batch works to when the quest itself carries none: how to work here
    and how to deliver, for every character on the quest.

    The SERVER'S value wins whenever the payload carries one:
    ``autopilot.default_quest_instructions`` is derived read-only by quest-backend, so the text a
    run works to is whatever that backend currently says it is, and improving it there improves
    every quest that never wrote its own. ``BUNDLED_DEFAULT_QUEST_INSTRUCTIONS`` is only what an
    older backend (or any client that does not serve the field) degrades to, so a character on duty
    still has a specification instead of a silent pass. See that constant for why the copy exists
    at all.
    """
    served = str(autopilot_cfg.get("default_quest_instructions") or "").strip()
    return served or BUNDLED_DEFAULT_QUEST_INSTRUCTIONS


def default_persona_instructions_for(autopilot_cfg: Dict[str, Any]) -> str:
    """The PERSONA brief a batch works to when that character carries none: what to work on.

    Resolved exactly like ``default_quest_instructions_for`` and from its own served field
    (``autopilot.default_persona_instructions``), because the two slots default independently: a
    quest that wrote a quest-wide brief and no character brief still needs this one, and the
    reverse is just as common.
    """
    served = str(autopilot_cfg.get("default_persona_instructions") or "").strip()
    return served or BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS


def split_held_for_another_day(tasks: List[Dict[str, Any]], autopilot_cfg: Dict[str, Any],
                              now: datetime
                              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split adopted recurring tasks into ``(workable today, held)`` under the day rule.

    Held means the task names a character the person rostered for other days, or that the roster's
    work-routing characters are all off duty today: either way nobody may work it today, and it
    comes back on a day one of them does. Held is not lost. The caller simply does not ADOPT a held
    occurrence, so it stays queued and runs as the person scheduled it, which is the same direction
    ``_close_adopted`` fails in (duplicated work is recoverable, lost work is not).

    Deliberately called BEFORE anything is composed for these tasks, so a held occurrence becomes
    no part of this run: nothing this character is asked to do, and no close on a task somebody
    else's day owns. The incident this rule comes from was a character working another's item on
    the wrong day, and that is what stays impossible.

    The fallback resolver is not consulted here: it is only ever reached for a quest whose roster
    names no work-routing character, and such a quest can hold nothing, so passing it would spend a
    consumer callback on a decision it cannot change.
    """
    workable: List[Dict[str, Any]] = []
    held: List[Dict[str, Any]] = []
    for task in tasks:
        if resolve_task_persona(task, autopilot_cfg, now) is PERSONA_HELD:
            held.append(task)
        else:
            workable.append(task)
    return workable, held


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


def next_steps_from_pass(current_goals: List[Dict[str, Any]],
                         adopted_tasks: Optional[List[Dict[str, Any]]] = None, *,
                         scope_label: str = "", updated: str = "",
                         previous: Optional[Dict[str, Any]] = None,
                         reflection_note: str = "",
                         insights_note: str = "") -> NextSteps:
    """The pass's own conclusion about what comes next, as the canonical artifact.

    Deterministic and LLM-free, like ``propose_next_goal``: the pass has already read the quest's
    current scope, its goals at that scope and the recurring tasks it took over, so the artifact is
    a restatement of what it acted on, not a second opinion about it. Asking a model to re-derive
    it here would spend a call to produce a DIFFERENT answer from the one the pass just acted on,
    which is the exact drift this artifact exists to remove.

    ``current_goals`` is the quest's own goals at the scope it is working in (the ladder's finest
    rung), which is what "what is next for this quest" means to the person who wrote them. One line
    each, with the deadline where there is one, since "next" and "by when" are the same question.
    Then one line per adopted recurring task, then the previous period's unfinished goals as
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
    for goal in current_goals:
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
    "material to apply them to. Follow them verbatim where they are specific."
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
    "in your result which you followed and why."
)

# The framing for a slot the person has not written, one per level. Both say outright that the
# text is the built-in one rather than theirs, because the written preambles above claim authorship
# ("written by the person who owns it") and that claim is simply false of a default: a run told it
# about words nobody wrote would report back against a standard nobody set. The honest framing is
# also the useful one, since it tells the run this is the floor and that anything the person later
# writes replaces it outright.
#
# Everything else about the two blocks is unchanged, and deliberately so: a defaulted slot occupies
# the same position and carries the same authority as a written one, because it is the brief that
# actually governs the run.
_DEFAULT_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions for this quest. Nobody has written a brief for this quest yet, so this "
    "is the built-in one, and it is the specification for this run: it says what to produce and "
    "how. Everything below is the material to apply it to. The moment the person writes "
    "instructions of their own for this quest, theirs replace this entirely."
)

# The persona-level default. It states the same precedence its written counterpart states, because
# the precedence is a property of the LEVEL and not of who wrote the text: this block still
# describes one character's own job, and still governs where it disagrees with the quest-wide
# block. ``{who}`` is filled with the persona's name when one is resolved.
_DEFAULT_PERSONA_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions {who} on this quest. Nobody has written a brief for this character "
    "here yet, so this is the built-in one. It describes the job this character does here, which "
    "is not the same job the quest-wide instructions describe. It is MORE SPECIFIC than those, so "
    "where the two disagree, follow this and say in your result which you followed and why. The "
    "moment the person writes instructions for this character, theirs replace this entirely."
)


# The framing for the goal ladder (``current_goal_ladder``). Three things have to be said outright
# rather than left to position, because the block LOOKS like a work list and a run that reads it as
# one does exactly the wrong thing with it: that these goals are the person's and not this run's
# assignment, that goals the AI will never touch are on the list on purpose, and that the job is to
# make this run's output add up to them. Without the last sentence the ladder is just more text;
# with it, it is the check the run applies to its own result.
_GOAL_LADDER_PREAMBLE = (
    "THE PERSON'S CURRENT GOALS, at every horizon they plan in. This is the frame around this run, "
    "not a list of work for it. This run does not own these goals: do not try to finish them, do "
    "not report on them, and never record any of them as done. Goals the person is plainly doing "
    "themselves are included deliberately, because work that ignores them is work pulling against "
    "the plan rather than with it.\n"
    "Before you finish, check that what you produced actually serves these goals and adds up to "
    "the horizons above them. If it does not, say so plainly in your result instead of leaving the "
    "person to notice."
)


def _render_goal_ladder(ladder: List[Dict[str, Any]]) -> str:
    """The goal ladder as the text a run reads: the framing, then one short line per goal."""
    lines = [_GOAL_LADDER_PREAMBLE]
    for rung in ladder:
        scope = str(rung.get("scope") or "")
        label = _LADDER_SCOPE_LABELS.get(scope, scope or "This period")
        period = str(rung.get("period") or "").strip()
        lines.append(f"{label}{f' ({period})' if period else ''}:")
        for goal in rung.get("goals") or []:
            name = str(goal.get("name") or "").strip() or "(untitled goal)"
            deadline = str(goal.get("deadline") or "").strip()
            lines.append(f"  - {name}{f' (by {deadline})' if deadline else ''}")
        more = int(rung.get("more") or 0)
        if more > 0:
            # Said, never silently dropped: a person who plans twenty goals in a month must not be
            # shown eight of them as though that were the whole plan.
            lines.append(f"  +{more} more")
    return "\n".join(lines)


def compose_batch_text(quest_outcome: str,
                       persona: Optional[str] = None, *,
                       scope_label: Optional[str] = None,
                       adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                       next_steps: Optional[str] = None,
                       previous: Optional[Dict[str, Any]] = None,
                       reflection: Optional[str] = None,
                       insights: Optional[str] = None,
                       instructions: Optional[str] = None,
                       persona_instructions: Optional[str] = None,
                       default_quest_instructions: Optional[str] = None,
                       default_persona_instructions: Optional[str] = None,
                       goal_ladder: Optional[List[Dict[str, Any]]] = None) -> str:
    """The batch task's text: what this run is asked to produce, the period and goals it serves,
    what the person themselves last said about the work, and what the previous period produced.

    The last two are what keep a recurring pass from starting cold every time. A daily pass that
    cannot see yesterday's goals and task results has no way to notice that the plan slipped, so it
    reissues the same instruction while the human falls further behind. Feeding the previous
    period's goal completion and task outcomes in makes continuity the default.

    ``reflection`` is the person's own latest daily/period reflection (``runner.reflections``), and
    ``insights`` the captures they have made and not yet acted on since the last pass
    (``runner.insights``). Those two are the only inputs here written BY them rather than derived
    from rows: everything else describes what the system recorded, while these say what the person
    made of it and what occurred to them in between, so they are what should break ties about what
    actually matters this run.

    The insights block carries each capture's own category tags and asks the READER to judge which
    apply to this quest. That is deliberate: matching a tag against a quest or goal name in code
    would be a fixed string rule that silently drops every insight whose wording it did not
    anticipate, which is exactly what this repository's hard rule #3 forbids.

    ``instructions``, when the quest carries any, is the block right after ``Scope:``: verbatim,
    never summarized/reflowed/rewritten (it is the one input here the person authored as a
    specification, not material to interpret). It comes before the material it governs, not after,
    because a specification that arrived after that material would read as commentary on work
    already planned. Absent, this emits nothing and the composed text is byte-identical to before
    this parameter existed.

    ``persona_instructions`` is the same thing one level down: the standing instructions THIS
    character carries on this quest, emitted immediately after the quest-wide block, equally
    verbatim. It exists because one roster can hold characters doing genuinely different jobs on
    the same quest -- the weekday worker carrying the ordinary work and the Saturday reviewer
    looking at the quest from outside -- and handing both the identical quest-wide brief describes
    neither of them. Its precedence over that brief is stated in its own framing sentence for the
    same reason the quest-wide one states its own. Absent, this likewise emits nothing and composes
    byte-identically to before the parameter existed.

    ``default_quest_instructions`` and ``default_persona_instructions`` are the FLOOR under those
    two, one each, and the layering rule lives here so there is exactly one place that decides it.
    THE TWO SLOTS DEFAULT INDEPENDENTLY: the quest-wide slot carries ``instructions`` when the
    person wrote them and its default otherwise, the persona slot carries ``persona_instructions``
    when the person wrote them and its default otherwise, and both slots are then emitted exactly
    as two written briefs are, quest-wide first and persona second. The two are not alternatives at
    all: one says how to work and how to deliver, the other says what to work on, so a quest that
    lost the second because its owner wrote the first would be a quest whose runs no longer know
    what they are for. A defaulted slot is framed as the built-in brief rather than as the
    person's, so a run is never told they wrote words they did not. Each default absent, that slot
    falls back to the written value alone, which is what keeps every direct caller's composition
    unchanged.

    ``goal_ladder`` (from ``current_goal_ladder``) is emitted after the brief: the run reads what
    it is asked to produce, then the picture that output has to fit into. It is the person's
    current goals at every horizon, and its framing says outright that they are context to serve
    rather than work to do. It is THE ONLY WAY A GOAL REACHES A RUN: no goal is ever composed as
    an assignment here. Absent or empty, this emits nothing and composes byte-identically to before
    the parameter existed.

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
        # Naming the period still matters, and for the reason it always did: a week's or a month's
        # worth of plan, read by a single run, reads as "do all of this today", which is both
        # discouraging and wrong. What changed is what the period holds. Its goals are context now
        # and never this run's assignment, so this line says which horizon the run is standing in
        # and stops there. Telling the run to advance the goals below would contradict the ladder
        # a few blocks down, which says in as many words that the run does not own them.
        parts.append(f"Scope: this quest's {scope_label}. That is the period this run sits inside, "
                     f"and it is longer than one session: it says which horizon everything here "
                     f"belongs to, and it does not make the period's contents this run's workload. "
                     f"What to produce is for the standing instructions to say. Where the period "
                     f"itself has slipped or moved on, say so plainly.")
    # THE LAYERING RULE, and it is two rules that never look at each other. Each slot takes what
    # the person wrote for THAT slot, or its own built-in brief, and the framing is the only thing
    # that changes with the answer. A written brief at one level says nothing about the other
    # level, so it can never silence it.
    quest_brief = instructions or default_quest_instructions
    if quest_brief:
        preamble = _INSTRUCTIONS_PREAMBLE if instructions else _DEFAULT_INSTRUCTIONS_PREAMBLE
        parts.append(preamble + "\n\n" + quest_brief)
    persona_brief = persona_instructions or default_persona_instructions
    if persona_brief:
        who = f"for {persona} specifically" if persona else "for this character specifically"
        template = (_PERSONA_INSTRUCTIONS_PREAMBLE if persona_instructions
                    else _DEFAULT_PERSONA_INSTRUCTIONS_PREAMBLE)
        parts.append(template.format(who=who) + "\n\n" + persona_brief)
    if goal_ladder:
        # After the brief, deliberately: the run reads what it is asked to produce, then the goals
        # that output has to add up to. The reverse order reads as "here is a list of work, and
        # here is some more work", which is the misreading the framing above spends its words
        # preventing.
        parts.append(_render_goal_ladder(goal_ladder))
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


def _batch_title(adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                 instructions: Optional[str] = None,
                 persona_instructions: Optional[str] = None) -> str:
    """A short label for the task list: what this batch is ABOUT.

    Without one the server derives a title from the first line of the text, which is the "Act as
    ..." persona line, so every autopilot task in the list is titled after its persona instead of
    its work. An adoption batch is titled after the task it took over; everything else is titled
    after the brief it is working to.

    A brief's title is its first non-empty line, stripped of leading Markdown furniture ("#", "-",
    "*", ">") and capped at 80 characters. That title is also the SUBJECT of the mail a finished run
    sends, which is why it matters here and not just cosmetically.

    ONLY WRITTEN BRIEFS MAY TITLE A BATCH, and that is the whole reason the defaults are not in
    this chain. A default is the same text on every quest that has not written one, so titling from
    it would name every task on every such quest "Ground every claim about where things stand...",
    forever, in the list and in the person's inbox. A title has to say what makes THIS run
    different, and a built-in brief says nothing of the kind. So the run composes with the
    effective text and is titled from the written text alone.

    The order is the same precedence the brief itself has. ``persona_instructions`` first, because
    a batch that exists because a character has their own standing job on this quest is about THAT
    job, and titling it after the quest's general brief would name work this run is not doing. Then
    the quest-wide ``instructions``. Then the plain "Autopilot run", which is what a fully defaulted
    quest gets: honest, and short enough that a person reading their list sees the quest's name
    doing the work instead.
    """
    if adopted_tasks:
        first = str((adopted_tasks[0].get("title") or adopted_tasks[0].get("text") or "")).strip()
        lines = first.splitlines()
        if lines and lines[0].strip():
            return lines[0].strip()[:120]
    for source in (persona_instructions, instructions):
        if not source:
            continue
        for line in source.splitlines():
            stripped = _MD_TITLE_FURNITURE_RE.sub("", line).strip()
            if stripped:
                return stripped[:80]
    return "Autopilot run"


def propose_next_goal(quest: Dict[str, Any]) -> Tuple[str, str]:
    """A deterministic (LLM-free, so this stays fast/offline-testable) proposed next-goal (title,
    description) in service of the quest's stated outcome, used when ``planning=="plan_and_work"``
    and the pass produced no work batch at all."""
    outcome = (quest.get("outcome") or "this quest's outcome").strip()
    title = f"Next step toward: {outcome}"
    description = (
        f'Propose and take the next concrete step toward "{outcome}". This pass produced no work '
        "on the quest, and the quest allows autopilot to plan as well as work. Define a specific, "
        "checkable outcome for this goal before starting on it."
    )
    return title, description


@dataclass
class AutopilotResult:
    """What one autopilot pass did -- the pass task's own reported result (see ``summary_text``).

    ``created`` holds one dict per item the pass created, and it holds NAMES, not just ids:
    ``{task_id, kind, title, quest_id, quest_label, persona_label, awaiting_approval,
    adopted_titles}``. That is not decoration. This result is read by a person, in
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
            line = (f"  - Work on {label} as {p.get('persona_label') or 'the assistant'} "
                    f"({p.get('scope')})")
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
    ``list_quests``, ``get_quest_autopilot``, ``list_quest_goals``, ``list_tasks``,
    ``list_open_decisions_for_quest``, ``create_task``, ``update_task``,
    ``update_quest_autopilot``, and optionally ``create_goal``, ``get_daily_reflection``,
    ``get_period_reflection``, ``get_insights_collection`` and ``list_collection_entries`` -- a
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

    def instructions_source(self, persona: Optional[str],
                             persona_instructions: Optional[str],
                             instructions: Optional[str]) -> str:
        """Which briefs this batch is carrying, in words, for the dry-run report.

        BOTH slots are always named, because both are always filled: the character's brief governs
        where the two disagree, but the quest's rides into the same run, and a report that hid it
        would be describing a shorter brief than the one that gets sent. A slot the person has not
        written is named as the built-in default for that level rather than as theirs, which is the
        whole value of this line on a quest nobody has configured yet, and the thing a person
        checking their setup is actually looking for: whether the text they wrote is the text that
        will run.
        """
        who = self._persona_label(persona) or "this character"
        return ", plus ".join((
            f"{who}'s own standing instructions" if persona_instructions
            else "the built-in default brief for a character",
            "this quest's standing instructions" if instructions
            else "the built-in default brief for a quest",
        ))

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
        # The floor under each instruction level, read once and independently: the quest-wide one
        # fills the quest slot when this quest wrote nothing, the persona one fills each
        # character's slot when that character has nothing of their own. The backend serves the
        # authoritative texts on the quest payload; these degrade to the bundled copies against an
        # older one.
        default_quest_instructions = default_quest_instructions_for(autopilot_cfg)
        default_persona_instructions = default_persona_instructions_for(autopilot_cfg)

        goals_payload = self._client.list_quest_goals(quest_id, team_id=self._team_id or None) or {}
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
                task_id = self._create_batch_task(quest, quest_id, persona, mode,
                                                  title=title,
                                                  scope_label=scope_label, adopted_tasks=tasks,
                                                  next_steps=standing_next_steps,
                                                  previous=previous,
                                                  reflection=reflection_text,
                                                  insights=insights_text,
                                                  instructions=instructions,
                                                  persona_instructions=persona_instructions,
                                                  default_quest_instructions=(
                                                      default_quest_instructions),
                                                  default_persona_instructions=(
                                                      default_persona_instructions),
                                                  goal_ladder=goal_ladder)
                if task_id:
                    result.created.append({
                        "task_id": task_id, "kind": "work_batch", "quest_id": quest_id,
                        "quest_label": quest_label, "title": title,
                        "persona_label": self._persona_label(persona),
                        "awaiting_approval": mode != "act",
                        "adopted_titles": [_task_label(t) for t in tasks],
                    })
                    budget_used += 1
                    produced = True
                    self._close_adopted(tasks, task_id, quest_id, result)
            if produced and not dry_run:
                self._refresh_next_steps(quest_id, current_goals, adopted, scope_label, previous,
                                         result, quest_label=quest_label,
                                         reflection_note=reflections.one_line(),
                                         insights_note=insights.one_line())
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

    def _batches_for_pass(self, autopilot_cfg: Dict[str, Any], adopted: List[Dict[str, Any]]
                          ) -> List[Tuple[Optional[str], List[Dict[str, Any]]]]:
        """This pass's batches for one quest: ``[(persona, adopted tasks for them), ...]``.

        ONE PER CHARACTER ON DUTY TODAY, in the roster's own on-duty order (day-restricted entries
        before unrestricted ones). Being on duty is the whole condition, because every character on
        duty now has a brief to work to at both levels: what the person wrote there, or the
        built-in default for it. An ``instructions_only`` entry is included like any other -- the flag only keeps that
        character out of the routing that hands an UNASSIGNED recurring task to somebody.

        A quest with NO roster gets exactly ONE batch, for whoever the consumer's fallback resolver
        names, or for the plain assistant when it names nobody. That is the unconfigured quest, and
        it still does its default briefs rather than nothing. (An empty on-duty list can only mean
        an empty roster here: ``_gate_quest``'s day rule has already skipped a quest whose roster
        names people but puts none of them on duty today.)

        Adopted tasks are then filed into the batch of whoever ``resolve_task_persona`` names, so
        an unassigned occurrence rides along with the character already working the quest instead
        of spawning a second run for the same character, and one naming somebody else gets its own
        batch. Anything the day rule holds is skipped here rather than re-routed: the caller has
        already held and reported it, and this is the belt to that braces, so a held task can never
        become a batch keyed on a marker that names no character.
        """
        now = self._now()
        merged: Dict[Optional[str], List[Dict[str, Any]]] = {}
        order: List[Optional[str]] = []

        def slot(persona: Optional[str]) -> List[Dict[str, Any]]:
            if persona not in merged:
                merged[persona] = []
                order.append(persona)
            return merged[persona]

        on_duty = persona_entries_on_duty(autopilot_cfg, now)
        for entry in on_duty:
            rep_id = entry.get("rep_id")
            if rep_id:
                slot(str(rep_id))
        if not on_duty:
            slot(self._fallback_persona())
        for task in adopted:
            persona = resolve_task_persona(task, autopilot_cfg, now, self._persona_resolver)
            if persona is PERSONA_HELD:
                continue
            slot(str(persona) if persona else None).append(task)
        return [(persona, merged[persona]) for persona in order]

    def _fallback_persona(self) -> Optional[str]:
        """The consumer's own answer to "who is this quest's character", or None.

        Reached only for a quest whose roster names nobody, which is every quest that never
        configured one. A resolver that raises is treated as no match: a bad consumer callback must
        never break a pass.
        """
        if self._persona_resolver is None:
            return None
        try:
            resolved = self._persona_resolver({})
            return str(resolved) if resolved else None
        except Exception:  # noqa: BLE001 -- a bad fallback must never break a pass
            log.info("autopilot: persona fallback_resolver raised while resolving this quest's "
                     "batch; treating as no match", exc_info=True)
            return None

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

    def _refresh_next_steps(self, quest_id: str, current_goals: List[Dict[str, Any]],
                            adopted: List[Dict[str, Any]], scope_label: str,
                            previous: Optional[Dict[str, Any]],
                            result: AutopilotResult, *,
                            quest_label: str = "",
                            reflection_note: str = "",
                            insights_note: str = "") -> None:
        """Write this pass's conclusion as the quest's canonical next steps (folder + Quest).

        Only called when the pass actually PRODUCED work, and never on a dry run. A pass that
        produced nothing must leave the artifact alone: overwriting a considered answer with "no
        current target" on a day the quest happens to be gated or quiet would make the artifact less
        trustworthy than the guesswork it replaces.
        """
        folder = self._quest_folder_map.get(str(quest_id))
        if not folder:
            return
        updated = self._now().astimezone(timezone.utc).strftime("%Y-%m-%d")
        next_steps = next_steps_from_pass(current_goals, adopted, scope_label=scope_label,
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
        # THE DAY RULE, as a gate. With a roster configured, a day nobody was rostered for is a day
        # this quest does no work at all -- config plus clock only, which is why it sits here with
        # the other cheap checks and before any goal fetch or model call.
        #
        # Without it the pass reached the standing-instructions branch on such a day, fell from
        # ``personas_on_duty[0]`` to the consumer's fallback resolver to ``None``, and ran the
        # quest as the plain assistant on a day the person had rostered nobody. That is the same
        # incident from the other side: the day setting has to decide whether a quest runs at all,
        # not only who runs it.
        #
        # A quest with NO roster is untouched here: it set no days, so there is nothing to follow.
        if (autopilot_cfg.get("personas")
                and not persona_entries_on_duty(autopilot_cfg, self._now())):
            return (f"no character on this quest's roster is rostered for "
                    f"{self._now().strftime('%A')}")
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
        would have been invisible to ``_has_backpressure``, which looks tasks up by quest id. This
        field never held a goal id and never will: a task is linked to its QUEST, and goals reach
        the run as context in its text (see ``compose_batch_text``).

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

        When the quest's ``autopilot`` block carries a ``model``, it is passed through as the
        created task's own model/tier override -- which model the deep worker runs THIS quest's
        autopilot work on. Unset means the lane's own default model ladder applies, exactly as
        before this field existed. This is the single choke point every autopilot-created task
        (work batches and goal proposals alike) passes through, so both inherit the setting.
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
        autopilot_cfg = quest.get("autopilot") or {}
        env_id = autopilot_cfg.get("env_id")
        if env_id:
            kwargs["env_id"] = env_id
        model = autopilot_cfg.get("model")
        if model:
            kwargs["model"] = model
        if self._pass_task_id:
            kwargs["parent_task_id"] = self._pass_task_id
        try:
            created = self._client.create_task(text, **kwargs) or {}
        except TypeError:
            # An older/stand-in client without ``parent_task_id`` and/or ``model``. Both are
            # improvements to how the pass reports/routes, never a requirement for it to work, so
            # lose the refinement rather than the task.
            kwargs.pop("parent_task_id", None)
            kwargs.pop("model", None)
            created = self._client.create_task(text, **kwargs) or {}
        task_id = created.get("id") or created.get("task_id")
        if not task_id:
            return None
        return str(task_id)

    def _create_batch_task(self, quest: Dict[str, Any], quest_id: str, persona: Optional[str],
                           mode: str, *,
                           title: Optional[str] = None,
                           scope_label: Optional[str] = None,
                           adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                           next_steps: Optional[str] = None,
                           previous: Optional[Dict[str, Any]] = None,
                           reflection: Optional[str] = None,
                           insights: Optional[str] = None,
                           instructions: Optional[str] = None,
                           persona_instructions: Optional[str] = None,
                           default_quest_instructions: Optional[str] = None,
                           default_persona_instructions: Optional[str] = None,
                           goal_ladder: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
        text = compose_batch_text(str(quest.get("outcome") or ""),
                                  self._persona_label(persona),
                                  scope_label=scope_label, adopted_tasks=adopted_tasks,
                                  next_steps=next_steps, previous=previous,
                                  reflection=reflection, insights=insights,
                                  instructions=instructions,
                                  persona_instructions=persona_instructions,
                                  default_quest_instructions=default_quest_instructions,
                                  default_persona_instructions=default_persona_instructions,
                                  goal_ladder=goal_ladder)
        try:
            return self._create_autopilot_task(
                quest, quest_id, text, mode, persona=persona,
                title=title if title is not None
                else _batch_title(adopted_tasks, instructions=instructions,
                                  persona_instructions=persona_instructions))
        except Exception as e:  # noqa: BLE001 -- surfaced to the caller's per-quest try/except
            log.error("autopilot: task creation failed for quest %s: %s", quest_id, e,
                      exc_info=True)
            raise

    def _maybe_create_goal(self, quest_id: str, title: str, description: str, mode: str,
                          scope_label: str) -> Optional[str]:
        """Create the REAL, typed Goal behind a proposal, when the client can and the quest allows.

        Two conditions, both unchanged: the client must expose ``create_goal`` (a client without
        one still gets the proposal TASK, just no goal object), and the quest must be in ``act``
        mode, since creating a goal on a ``suggest`` quest would be autopilot writing the plan the
        person asked to approve first.

        The call is ``create_goal(title, quest_id=..., period=..., description=...)``.
        ``period`` is REQUIRED by both the client and ``POST /api/planning/goals`` (the backend
        derives the goal's deadline and time_scope from it), so it is derived from this pass's own
        scope label by ``goal_period_for_scope`` rather than guessed. Nothing marks the goal as
        AI-workable, because a goal is not a unit of AI work: it is the plan the work serves, and
        the flag that used to say otherwise no longer exists on a goal at all.

        HISTORY, because the failure mode matters more than the fix. This method called
        ``create_goal(quest_id, name=title, description=..., ai_help=True, created_by="ai")``:
        the quest id landed in ``title``, ``name`` and ``created_by`` are not parameters at all,
        and the required ``period`` was missing. It could not bind, so every call raised TypeError
        the moment ``QuestClient.create_goal`` existed, and AI-proposed goals were silently never
        created. Nothing noticed for two reasons at once, and the pair is the lesson: the branch is
        UNREACHABLE for any quest carrying standing instructions (the always-work rule matched
        first, and now matches for every quest), so it effectively never ran, and the bare
        ``except`` below downgraded a
        programming error to one INFO-shaped warning line indistinguishable from a backend that
        simply lacks the endpoint. Absence of "create_goal failed" in the logs was therefore not
        evidence that it worked.

        Hence the split below: a TypeError is a bad call signature in THIS file, logged at ERROR
        with a traceback so the next one is visible on the day it lands, while any other exception
        stays a warning, because a missing or misbehaving optional endpoint genuinely must not fail
        the pass. Neither propagates: the proposal task is still created either way, and it carries
        the whole proposal in its text.

        ``created_by="ai"`` was DROPPED rather than fixed. The endpoint's request model
        (``CreateGoalRequest``) has no attribution field, its base config ignores unknown keys, and
        the handler hardcodes ``source="user"`` on every goal it creates. Passing an attribution
        field would have looked like attribution while carrying none.
        """
        create_goal = getattr(self._client, "create_goal", None)
        if not callable(create_goal) or mode != "act":
            return None
        period = goal_period_for_scope(scope_label, self._now())
        try:
            created = create_goal(title, quest_id=quest_id, period=period,
                                  description=description) or {}
            return created.get("id") if isinstance(created, dict) else None
        except TypeError:
            # A call this file got wrong, not an endpoint the deployment lacks. Loud, with a
            # traceback, and still not fatal to the pass.
            log.error("autopilot: create_goal call is not compatible with this client's signature "
                      "(quest %s, period %s); no goal was created", quest_id, period,
                      exc_info=True)
            return None
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
                         dry_run: bool, result: AutopilotResult,
                         scope_label: str = "") -> Optional[str]:
        """Propose the quest's next goal, unless the last pass's proposal is still unanswered.

        Returns the skip reason when nothing was proposed, so the caller can report it as a skip
        and leave the budget alone.

        ``scope_label`` is this pass's own scope (``current_scope_label``), carried through only so
        a goal created from the proposal is filed under the period the quest is actually working
        in. See ``_maybe_create_goal``: the period is required and cannot be defaulted centrally.
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
        created_goal_id = self._maybe_create_goal(quest_id, title, description, mode, scope_label)
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
