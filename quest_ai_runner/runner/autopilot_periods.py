"""Autopilot timing: cadence, period keys and bounds, the goal ladder.

Split out of ``autopilot.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

from .quest_client import _PERIOD_RE as _GOAL_PERIOD_RE
from .local_time import now_in_zone
from .autopilot_defaults import DEFAULT_LADDER_PER_SCOPE, _CADENCE_DAYS, _SCOPE_ORDER, _goal_name


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


def has_new_ask_since(asks: List[Dict[str, Any]], last_pass_at: Optional[str]) -> bool:
    """Whether any of ``asks`` (a quest's rows from ``QuestClient.list_asks``) landed AFTER
    ``last_pass_at`` -- the whole predicate REACTIVE mode gates on (see ``_gate_quest``).

    Never run before (no ``last_pass_at``) means anything at all counts as new: a reactive quest
    that has never passed has nothing to compare against, and reads its first ask as due rather
    than silently waiting for a second one to arrive.

    Deliberately keyed on ``occurred_at`` (when the ask was actually made), not on the ledger's
    bookkeeping fields (``state``, an internal update timestamp): the trigger this answers is "did
    someone ask something new", and a person editing an old ask's classification or a run marking
    one accepted is not that event. An ask with no parseable ``occurred_at`` is skipped rather than
    treated as either always-new or never-new -- a malformed row must not itself decide the gate.

    Pure and dependency-free like ``cadence_due``/``run_requested`` above, for the same reason:
    directly unit-testable with no client or clock to fake.
    """
    since = _parse_dt(last_pass_at)
    if since is None:
        return bool(asks)
    for ask in asks:
        occurred = _parse_dt(ask.get("occurred_at"))
        if occurred is not None and occurred > since:
            return True
    return False


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
