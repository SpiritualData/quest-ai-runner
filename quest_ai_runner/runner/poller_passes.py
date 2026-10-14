"""Autopilot and quest-pass scheduling: ensure, create, retune and retire pass tasks.

A mixin of ``Poller`` (see ``poller.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from datetime import date, timedelta

from .autopilot import AUTOPILOT_PASS_KIND, OPEN_TASK_STATUSES, cadence_due, run_requested
from .quest_client import QuestApiError
from ..adapters.bootstrap_reporter import (
    check_should_send_monthly_report,
    generate_bootstrap_report,
    get_corpus_stats,
    mark_report_sent,
)
from .local_time import now_in_zone, today_in_zone
from .poller_schedule import (
    _foreign_series_looks_alive,
    _next_allowed_date,
    _quest_pass_days,
    _quest_pass_recurrence,
    _run_request_signature,
    log,
)


class PassesMixin:
    """Autopilot and quest-pass scheduling: ensure, create, retune and retire pass tasks."""

    def _ensure_autopilot_pass(self) -> None:
        """Make every opted-in quest's autopilot pass series EXIST, and keep each one in tune.

        Autopilot is deliberately implemented as a task rather than a daemon (no cron, pausable
        from the same UI, auditable in the same activity stream). The original gap was that
        NOTHING created that task at all: opting a quest into Suggest/Act saved the setting and
        produced silence forever. This closes the loop from the runner side -- the lane that would
        execute the pass is the one that ensures it exists.

        EVERY opted-in quest has its own recurring pass series (``goal_id`` == that quest's id),
        at its own run time -- created when missing, retuned when its schedule drifts from what
        the quest now says (including the backend's UTC-midnight spawn-date bug), and retired when
        the quest stops being eligible. A quest that names no run time of its own is not a
        different case: it is given the lane's default hour when its schedule is read, so there is
        one kind of pass and one path that makes it. Any surviving team-wide series (no
        ``goal_id``) is retired on sight.

        Liveness: "exactly one open occurrence" per SERIES, keyed by ``goal_id``. A quest's group
        can also hold a one-off CATCH-UP pass (see ``_split_pass_occurrences``), which shares the
        ``goal_id`` but is not part of the series and never stands in for it.

        THE LIVENESS READ IS PER QUEST, and used to be one team-wide list (incident, 2026-09-17).
        This method used to advertise "still ONE ``list_tasks`` call per scan, no list call per
        quest" as a deliberate property. It was wrong, and quietly so. The team-wide listing is
        OWNER-SCOPED by the backend (``list_assistant_tasks`` filters on the authenticated
        ``user_id``; ``team_id`` narrows that set, it does not widen it), while a pass created
        against a quest is owned by the QUEST's owner, with the creating account recorded only as
        ``created_by``. So on any quest whose owner is not the lane's own account, the team-wide
        list never returned the pass the lane itself had created: the liveness check read "no open
        pass" on every single scan and created another recurring series each time. Measured live:
        three series, each a weekly recurrence, created for one quest inside six minutes, which
        would have meant three briefs a day. It went unnoticed for as long as it did because the
        one lane anybody watched happened to run a quest owned by its own account. The per-quest
        listing (``goal_id=<quest>``) is answered by ``list_assistant_tasks_for_goal``, which is
        scoped to the quest rather than to a user, so it returns the pass whoever owns it. The
        cost is one extra list call per OPTED-IN quest per scan, and that is the right trade: the
        saved call was buying a liveness answer that was not true.

        A FAILED read never creates. ``list_tasks`` returns ``[]`` on any error, which is
        indistinguishable from "no pass exists" and is what turned a rate-limit burst into
        duplicate series. The reads here go through ``list_tasks_or_none``, and a ``None`` (the
        read failed) skips that quest entirely until the next scan.

        Best-effort throughout: any failure is logged and retried next scan. A missing or
        out-of-tune pass task is a degraded feature, never a reason to skip the ordinary task
        pickup that follows.
        """
        if not self.cfg.autopilot_ensure_pass_task:
            return
        if not self.client.configured or not (self.cfg.team_id or self.cfg.team_ids):
            return
        try:
            # ONE owner-scoped read per scan (see ``_list_owned_pass_tasks`` for why it is one).
            # It serves three purposes: retiring a surviving team-wide series (no ``goal_id``,
            # so no per-quest read can find it), the team-wide grouping for quests that are not
            # opted in, and the owned half of each opted-in quest's liveness check.
            existing = self._list_owned_pass_tasks()
            if existing is None:
                log.warning("autopilot: the pass listing failed -- not creating or retiring "
                            "anything this scan")
                return
            open_by_series: Dict[str, List[Dict[str, Any]]] = {}
            for t in existing:
                if str(t.get("status", "")).strip().lower() not in OPEN_TASK_STATUSES:
                    continue
                open_by_series.setdefault(str(t.get("goal_id") or ""), []).append(t)
            snapshot = self._quest_schedule_snapshot()
            self._retire_team_pass(open_by_series.get("", []))
            self._ensure_quest_pass_tasks(open_by_series, snapshot, owned=existing)
        except Exception as e:  # noqa: BLE001 -- never let this block the scan
            log.warning("autopilot: could not ensure the recurring pass task(s) (%s) — "
                        "will retry next scan", e)

    def _retire_team_pass(self, occurrences: List[Dict[str, Any]]) -> None:
        """Stop the old team-wide series (a pass with no ``goal_id``).

        It is superseded rather than disabled: every opted-in quest now has its own pass at its
        own run time, so a team-wide scanner has nothing left to scan. Retiring it here is what
        lets a deployment that already had one converge by itself, instead of leaving an orphan
        firing daily forever."""
        if occurrences:
            self._retire_quest_pass("(team-wide)", occurrences,
                                    "superseded by per-quest passes")

    # --- the pass series: one per opted-in quest ---------------------------------------------

    def _quest_schedule_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """``{quest_id: {team_id, mode, run_time, run_timezone, cadence, last_pass_at,
        has_instructions, env_id}}`` for every quest on every team this lane discovers from
        (``discovery_team_ids``, which is the one home team unless the lane serves several),
        refreshed at most every
        ``cfg.autopilot_settings_refresh_seconds`` (default 300s / 5 minutes) and reused from
        cache in between.

        Built from ``list_quests`` (the slim team listing) plus one ``get_quest_autopilot`` per
        quest -- the same two-read shape ``AutopilotPass._eligible_quests`` uses, and for the same
        reason: the team listing carries no ``autopilot`` block at all. One unreadable quest is
        skipped with a log; it never voids the rest of the snapshot.

        MUST be refreshed before task discovery in ``run_once`` so the timezone map ``tz_for``
        reads (``_pass_timezone_for``) is warm on the very first scan -- the existing call order
        already guarantees this, since ``_ensure_autopilot_pass`` (which calls this) runs before
        ``discover_due``.

        Cost and its consequence, worth knowing before touching the refresh interval: a settings
        change (a new run time, new instructions, mode flipped off) takes effect within one
        refresh interval, not on the very next scan. That is why the interval is config and not a
        constant -- a deployment that wants faster convergence sets it lower, at the cost of more
        reads.
        """
        now_ts = time.monotonic()
        if (self._quest_schedule_cache_at is not None
                and (now_ts - self._quest_schedule_cache_at)
                < max(0, self.cfg.autopilot_settings_refresh_seconds)):
            return self._quest_schedule_cache
        snapshot: Dict[str, Dict[str, Any]] = {}
        # One listing per team this lane discovers from, merged into ONE map. Quest ids are
        # globally unique, so there is nothing to collide; a quest seen twice (a lane whose home
        # team is also in the discovery set) is simply read once. Each entry REMEMBERS the team
        # it came from -- that is what ``_create_quest_pass`` needs to create a quest's pass on
        # the quest's OWN team rather than on the lane's home team.
        # ``cfg.team_ids`` when the lane serves several teams, else the home team EXACTLY as
        # before (including the empty/owner-scoped case). Deliberately not ``discovery_team_ids``:
        # that one folds in ``discovery_team_id``, and a lane that overrides discovery to
        # owner-scoped while keeping a home team would silently switch this listing from its
        # team's quests to every quest its owner can see. Which quests get a pass is not the same
        # question as which queue the lane reads.
        for team in (list(self.cfg.team_ids) or [self.cfg.team_id]):
            for row in (self.client.list_quests(team_id=team or None) or []):
                quest_id = str(row.get("quest_id") or row.get("id") or "")
                if quest_id and quest_id not in snapshot:
                    snapshot[quest_id] = {"team_id": team or self.cfg.team_id}
        opted_in = 0
        for quest_id, entry in list(snapshot.items()):
            try:
                state = self.client.get_quest_autopilot(quest_id) or {}
            except Exception:  # noqa: BLE001 -- one bad quest never voids the snapshot
                log.info("autopilot: could not read quest %s's schedule this refresh -- skipping",
                         quest_id, exc_info=True)
                # Drop the team-only stub: a half-filled entry is not a schedule, and every
                # reader of this map (the timezone lookup, the pass ensure/retune path) treats a
                # present entry as one it can act on.
                snapshot.pop(quest_id, None)
                continue
            autopilot_cfg = state.get("autopilot") or {}
            instructions = str(autopilot_cfg.get("instructions") or "").strip()
            mode = str(autopilot_cfg.get("mode") or "off")
            # A quest that names no run time of its own runs at the lane's default hour. The
            # default is applied HERE, once, which is what keeps there being ONE kind of pass:
            # every opted-in quest has a run time, so every one gets its own series. Branching on
            # "did this quest set a time" instead would buy a second pass shape and nothing else.
            run_time = (str(autopilot_cfg.get("run_time") or "").strip()
                        or self.cfg.autopilot_pass_time)
            snapshot[quest_id] = {
                # The team this quest was LISTED under: the team its pass task must be created
                # on. On a single-team lane this is always the lane's own team, so nothing
                # changes; on a multi-team lane it is the only thing that keeps a pass off the
                # wrong team.
                "team_id": entry.get("team_id") or self.cfg.team_id,
                "mode": mode,
                "run_time": run_time,
                "run_timezone": str(autopilot_cfg.get("run_timezone") or "").strip() or None,
                "cadence": autopilot_cfg.get("cadence"),
                "last_pass_at": autopilot_cfg.get("last_pass_at"),
                "run_requested_at": autopilot_cfg.get("run_requested_at"),
                "has_instructions": bool(instructions),
                "env_id": autopilot_cfg.get("env_id"),
                # The roster, so the schedule can skip days the day rule would gate anyway (see
                # ``_quest_pass_days``). Not read anywhere else in the snapshot today.
                "personas": autopilot_cfg.get("personas") or [],
            }
            if mode in ("suggest", "act"):
                opted_in += 1
        self._quest_schedule_cache = snapshot
        self._quest_schedule_cache_at = now_ts
        log.info("autopilot: read schedules for %d quest(s), %d opted in",
                 len(snapshot), opted_in)
        return snapshot

    def autopilot_team_for(self, quest_id: str) -> str:
        """The team a quest's autopilot work should be created on (``AutopilotPass``'s resolver).

        Reads the ALREADY-CACHED schedule snapshot rather than refreshing it: this is called from
        the pass, which runs on a worker thread, and a refresh there would fire a quest listing
        per team plus a state read per quest off the hot path that the scan loop already keeps
        warm (``_ensure_autopilot_pass`` refreshes it before every discovery). A quest missing
        from the snapshot falls back to the client's own (cached) owning-team lookup, and then to
        the lane's home team, which is exactly what every call site used before this existed.
        """
        entry = (self._quest_schedule_cache or {}).get(quest_id) or {}
        team = str(entry.get("team_id") or "")
        if team:
            return team
        try:
            team = self.client.owning_team_for(quest_id) or ""
        except Exception:  # noqa: BLE001 -- resolution is a refinement; never fail a pass over it
            team = ""
        return team or self.cfg.team_id or ""

    def _pass_timezone_for(self, task: Dict[str, Any]) -> Optional[str]:
        """The IANA zone a PASS task's local due-check should compare in.

        Only a per-quest pass occurrence (``task_kind == "autopilot"`` carrying a ``goal_id``)
        gets one, via that quest's own ``run_timezone``. Every other task -- the team pass, and
        ordinary work -- returns ``None`` so ``_due_now_locally`` keeps today's naive-local
        comparison for them: an ``assistant_task`` carries no timezone of its own, and inventing
        one for a non-pass task is a separate feature, out of scope here.
        """
        if str(task.get("task_kind") or "").strip().lower() != AUTOPILOT_PASS_KIND:
            return None
        quest_id = str(task.get("goal_id") or "").strip()
        if not quest_id:
            return None
        entry = self._quest_schedule_snapshot().get(quest_id)
        return (entry or {}).get("run_timezone") or None

    def _expected_quest_occurrence(self, entry: Dict[str, Any], *,
                                   honour_run_request: bool = True) -> Tuple[str, str]:
        """The catch-up formula (autopilot spec A3): ``today_in_tz`` if the quest's cadence is
        due, else tomorrow -- computed from the exact SAME predicate ``cadence_due`` uses inside
        the pass itself, so the schedule and the gate can never disagree. A late run therefore
        never consumes the next day's occurrence (the corrected date comes from the clock and
        ``last_pass_at``, never from the previous occurrence's own date) and never produces two
        briefs (once a pass has run today, this returns tomorrow, and ``cadence_due`` is a second,
        independent guard inside the pass).

        A pending "Run now" request (``run_requested``) overrides both halves: the date is today,
        and the time is pulled back to the current local time when ``run_time`` is still ahead of
        it, so the occurrence is due the moment the runner next looks instead of at a run_time
        that may be hours away (or already gone, which needs no pulling back). This is the whole
        of "run now" on the schedule side -- the same series, the same occurrence, moved. A "Run
        now" is a person's explicit request THIS moment, so it is not rolled past an excluded day
        the way the ordinary cadence path below is -- the day rule still gates it once the pass
        actually runs (nothing overrides that), so it is reported as a skip like any other, never
        silently dropped.

        Off a roster day nobody is rostered for, the ordinary (non-"Run now") date is rolled
        forward to the next day the roster would actually let something happen -- see
        ``_quest_pass_days``. Without this, a quest whose roster excludes a day still got a same-
        day occurrence created and then immediately skipped once it ran, which is exactly the
        no-op noise this schedule exists to avoid creating in the first place.

        ``honour_run_request=False`` is the caller saying THIS request has already been served, so
        only the ordinary cadence path applies. That is not an optimisation, it is the bound on
        this whole method: see ``_ensure_one_quest_pass``, which owns the decision.

        Returns ``(expected_date "YYYY-MM-DD", expected_time "HH:MM")``.
        """
        zone = entry.get("run_timezone")
        run_time = entry.get("run_time") or "00:00"
        now = self._now()
        today = today_in_zone(zone, now)
        if honour_run_request and run_requested(entry):
            return today.isoformat(), min(run_time, now_in_zone(zone, now).strftime("%H:%M"))
        expected_date = today if cadence_due(entry, now, tz=zone) else today + timedelta(days=1)
        allowed_days = _quest_pass_days(entry, expected_date)
        if allowed_days:
            expected_date = _next_allowed_date(expected_date, allowed_days)
        return expected_date.isoformat(), run_time

    def _list_pass_tasks(self, *, team_id: Optional[str] = None,
                         goal_id: Optional[str] = None,
                         status: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Read pass-kind tasks, returning ``None`` when the READ ITSELF failed.

        The whole point is that None and ``[]`` are different answers here: every caller's next
        move on an empty list is a create, and a create on a failed read is how one rate-limited
        half hour produced three duplicate pass series (2026-09-17). A client too old to offer
        ``list_tasks_or_none`` degrades to the swallowing ``list_tasks``, which is the old
        behaviour and no worse than it.
        """
        strict = getattr(self.client, "list_tasks_or_none", None)
        if callable(strict):
            return strict(team_id=team_id, goal_id=goal_id, status=status,
                          task_kind=AUTOPILOT_PASS_KIND)
        return self.client.list_tasks(team_id=team_id, goal_id=goal_id, status=status,
                                      task_kind=AUTOPILOT_PASS_KIND)

    def _list_owned_pass_tasks(self) -> Optional[List[Dict[str, Any]]]:
        """This account's pass-kind tasks on EVERY team, in one read, for the scan's team-wide view.

        One owner-scoped read with no team filter. ``team_id`` only ever narrows the owner's list,
        so this is a superset of the per-team reads it replaces, and it costs one call per scan
        instead of one per team.

        WHY ONE CALL (incident, 2026-10-11): the backend caps an UNFILTERED ``GET
        /api/assistant-tasks`` at 10 a minute per user (``RateLimitMiddleware``, the
        ``task_list_unfiltered`` rule). The per-team reads here, plus one owner-scoped read per
        opted-in quest, were about eleven unfiltered calls on every 60-second housekeeping scan,
        so the lane was refused on nearly every scan. Discovery and the fast lane use filters the
        backend exempts from that cap, and were never affected.

        A failed read is ``None``, never ``[]``: the caller's next move is to retire what is not in
        the list, and a partial answer read as complete is how a rate-limited read retires a live
        series.

        ONE READ PER OPEN STATUS, not one unfiltered read (2026-10-11). An unfiltered owner list is
        the newest 200 tasks, so an account with more than 200 newer tasks lost its OLDER open
        passes from this view. The caller then read "no pass" and created a duplicate series, or
        failed to retire a legacy one. A status filter is a queue read: it is not capped at 200 and
        is not counted against the unfiltered limit. Only open rows matter to every caller here.
        """
        merged: List[Dict[str, Any]] = []
        seen: set = set()
        for status in sorted(OPEN_TASK_STATUSES):
            rows = self._list_pass_tasks(team_id="", status=status)
            if rows is None:
                return None
            for row in rows:
                task_id = str(row.get("id") or row.get("task_id") or "")
                if task_id and task_id in seen:
                    continue
                if task_id:
                    seen.add(task_id)
                merged.append(row)
        return merged

    def _open_quest_pass_occurrences(self, quest_id: str,
                                     owned: Optional[List[Dict[str, Any]]] = None
                                     ) -> Optional[List[Dict[str, Any]]]:
        """This quest's OPEN pass occurrences, read per quest so ownership cannot hide them.

        ``goal_id=<quest>`` is answered by the backend's quest-scoped listing rather than the
        caller's own task list, so a pass owned by the quest's human owner comes back even though
        the lane's account merely created it. Returns ``None`` when the read failed (never an
        empty list, which a caller would act on by creating).

        ``owned`` is the scan's owner-scoped listing, already read once by ``_ensure_autopilot_pass``
        and passed down so every quest shares it. Only a standalone caller with no scan read reads
        it here.

        The quest-scoped listing alone is NOT enough, and is merged with this account's own
        owner-scoped listing. INCIDENT (2026-09-25, four dissertation briefs in one morning): the
        backend's goal-scoped listing collapses each recurring series to its latest MEANINGFUL
        occurrence for the review UI (``_collapse_recurring_autopilot_duplicates``), so once a
        series has run, its queued next occurrence is hidden there. The lane therefore saw two of
        a quest's three live series, could never clean up the third, and read a quest whose only
        series was hidden as having none (and created another). The owner-scoped listing applies
        no cycle collapsing (it is also the discovery poll, where hiding a queued row would strand
        it), so every open occurrence this lane owns is visible in it. Either read failing is a
        failed read: acting on half the picture is how a duplicate gets created.
        """
        scoped = self._list_pass_tasks(goal_id=quest_id)
        if scoped is None:
            return None
        if owned is None:
            # team_id="" is "no team filter": this account's passes on every team, since the
            # quest's own team is not necessarily the lane's home team.
            owned = self._list_owned_pass_tasks()
            if owned is None:
                return None
        open_rows: List[Dict[str, Any]] = []
        seen: set = set()
        for t in list(scoped) + list(owned):
            if (str(t.get("status", "")).strip().lower() not in OPEN_TASK_STATUSES
                    or str(t.get("goal_id") or "") != quest_id):
                continue
            key = str(t.get("id") or t.get("task_id") or "") or str(id(t))
            if key in seen:
                continue
            seen.add(key)
            open_rows.append(t)
        return open_rows

    def _ensure_quest_pass_tasks(self, open_by_series: Dict[str, List[Dict[str, Any]]],
                                 snapshot: Dict[str, Dict[str, Any]],
                                 owned: Optional[List[Dict[str, Any]]] = None) -> None:
        """One recurring pass series PER opted-in quest that has set its own ``run_time``: created
        when missing, retuned when its open occurrence drifts from what the quest now says,
        retired when the quest is no longer eligible. Each quest is isolated from the others --
        one quest's failure here never blocks another's.

        An opted-in quest's occurrences are read per quest (see ``_open_quest_pass_occurrences``),
        because the team-wide grouping passed in is owner-scoped and silently misses a pass owned
        by someone else. A quest that is NOT opted in is handled from the team-wide grouping
        alone: the only thing left to do for it is retire what is open, and a pass this account
        cannot even see is one it cannot cancel either (an owner-scoped PATCH on it 404s), so
        reading per quest there would buy a call per scan and a warning per scan and no outcome.
        """
        for quest_id, entry in snapshot.items():
            try:
                team_wide = open_by_series.get(quest_id, [])
                if str(entry.get("mode") or "off") in ("suggest", "act"):
                    occurrences = self._open_quest_pass_occurrences(quest_id, owned=owned)
                    if occurrences is None:
                        log.warning("autopilot: could not read quest %s's open pass occurrences "
                                    "-- creating nothing for it this scan", quest_id)
                        continue
                else:
                    occurrences = team_wide
                self._ensure_one_quest_pass(quest_id, entry, occurrences)
            except Exception as e:  # noqa: BLE001 -- one quest's pass never blocks another's
                log.warning("autopilot: could not ensure quest %s's own pass (%s) — will retry "
                            "next scan", quest_id, e)

    @staticmethod
    def _split_pass_occurrences(
            occurrences: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split a quest's open pass occurrences into ``(series, catch_ups)`` on the one fact that
        actually tells them apart: a SERIES occurrence carries a ``recurrence``, a one-off
        CATCH-UP carries none.

        That test is structural rather than a label because the backend's spawner is structural:
        it copies ``recurrence`` onto the next occurrence, so anything holding one is a link in a
        repeating chain, and anything holding none is a single task that ends when it ends.

        INCIDENT (2026-09-05) -- what happens without the split. Every open task of the pass kind
        was grouped by ``goal_id`` alone, so a one-off pass created by hand to unblock a stuck
        "Run now" was counted as one of the quest's series occurrences. Three consequences, in
        order: the quest was reported as having "2 open pass occurrences"; ``_retune_quest_pass``
        then stamped a daily ``recurrence`` onto that one-off (it writes a recurrence whenever the
        current one differs from expected, and "absent" differs), converting it into a SECOND
        series; and when it completed, the backend spawned its own next occurrence, leaving the
        quest with two passes a day and an orphan chain that would have kept going. The structural
        rule that follows, and the whole reason this function exists: a catch-up is never retuned
        and never counted as the quest's pass, so it can never acquire a recurrence and can never
        become a series.
        """
        series: List[Dict[str, Any]] = []
        catch_ups: List[Dict[str, Any]] = []
        for occ in occurrences:
            recurrence = occ.get("recurrence")
            # Free text ("daily") and the structured object are both real recurrences; an empty
            # string is how a retirement CLEARS one, so it counts as none.
            present = (bool(recurrence.strip()) if isinstance(recurrence, str)
                       else bool(recurrence))
            (series if present else catch_ups).append(occ)
        return series, catch_ups

    def _unwritable_pass_occurrences(
            self, occurrences: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The occurrences this lane can SEE but can never write to, because another account owns
        them.

        A task's PATCH route is owner-scoped, so a retune or a retire aimed at a pass owned by
        somebody else comes back 404 no matter how often it is retried. That is the state every
        pass created before passes carried ``assignee_user_id`` is in: created by this account on
        a quest a human owns, therefore owned by that human. The lane must recognise it instead of
        looping on it, and above all must not answer it by creating yet another pass (the
        occurrence is real and alive, it simply belongs to someone else).

        Answering needs ``cfg.lane_user_id``, since nothing else tells the lane which account it
        authenticates as. Unset, or a row that carries no owner at all, means "assume our own" and
        behave exactly as before.
        """
        lane = (self.cfg.lane_user_id or "").strip()
        if not lane:
            return []
        return [o for o in occurrences
                if (str(o.get("user_id") or "").strip() or lane) != lane]

    def _ensure_one_quest_pass(self, quest_id: str, entry: Dict[str, Any],
                               occurrences: List[Dict[str, Any]]) -> None:
        """Bring ONE quest's pass into line: its series exists and holds the expected schedule,
        and a pending "Run now" the series cannot absorb gets a one-off catch-up instead.

        An occurrence owned by ANOTHER account is INERT and is excluded from every decision here.
        It cannot run: task discovery is scoped to the caller's own ``user_id``
        (quest-backend ``assistant_task_storage.list_assistant_tasks`` builds
        ``query = {"user_id": ...}`` and only narrows from there), and no lane authenticates as
        that owner. It cannot be retuned, retired or cancelled either, because the task PATCH is
        owner-scoped and 404s. So it is a dead row, and counting it as proof that the quest's pass
        exists is what leaves the quest with NO runnable pass at all, forever, silently.

        It is still logged every scan, because a human can clear it in the app and the ids are what
        they need.

        When this lane has no writable series of its own, a foreign SERIES occurrence still gets
        one narrow say: if it ``_foreign_series_looks_alive`` (see that function), this lane holds
        off creating a competing series of its own, on the theory that another account's lane is
        actually servicing this quest right now. A foreign series that looks stale/abandoned does
        NOT hold anything off -- this lane creates its own exactly as if the foreign occurrence
        were not there, which is the fix for the failure mode this method used to accept
        unconditionally: a quest whose foreign pass is permanently dead (created before passes
        carried ``assignee_user_id``, or that account's lane is gone for good) would otherwise
        never get a pass again, silently, until a human clears it in the app.

        Only the SERIES occurrences steer anything here (the duplicate warning, the retune, and
        the create-when-missing all count those alone). The catch-ups are read for exactly one
        purpose: to know whether a pending request already has a pass on the way, since a request
        stays pending until a pass stamps ``last_pass_at`` and every scan in between would
        otherwise create another one.
        """
        mode = str(entry.get("mode") or "off")
        foreign = self._unwritable_pass_occurrences(occurrences)
        if foreign:
            log.warning("autopilot: quest %s has %d open pass occurrence(s) owned by another "
                        "account (%s) -- this lane can neither run nor cancel them (an "
                        "owner-scoped PATCH 404s), and nothing can run them, so they are ignored "
                        "for liveness and this lane keeps its own pass. Clear them in the app when "
                        "convenient; passes created from now on are assigned to this lane's own "
                        "account.",
                        quest_id, len(foreign),
                        [o.get("id") or o.get("task_id") for o in foreign])
        writable = [o for o in occurrences if o not in foreign]

        if mode not in ("suggest", "act"):
            # Retire EVERYTHING open for this quest, catch-ups included: mode off means no pass of
            # any shape should still be waiting to run. Only the ones this lane owns, though: the
            # rest would be a 404 per occurrence per scan and no outcome.
            if writable:
                self._retire_quest_pass(quest_id, writable, "mode off")
            return

        # WRITABLE only: a foreign-owned occurrence is inert (see this method's docstring), so it
        # neither stands in for the series nor counts toward the duplicate warning.
        series, catch_ups = self._split_pass_occurrences(writable)
        if len(series) > 1:
            # A quest has exactly ONE pass series. Every extra one is a full second pass a day --
            # its own batch, its own deep runs, its own mail -- so it is retired, not merely
            # tolerated. INCIDENT (2026-09-25): this branch used to warn and act on the earliest
            # only, leaving the others alive; three live series on the dissertation quest all went
            # due at 06:30, two ran concurrently, and the person got four briefs.
            #
            # Kept: the earliest scheduled_date, oldest created_at on a tie (the long-standing one).
            # An extra that is RUNNING right now is left to finish, since cancelling mid-run would
            # abandon work already under way; its terminal status spawns a next occurrence, which
            # is queued and is retired by the next scan.
            ordered = sorted(series, key=lambda o: (str(o.get("scheduled_date") or ""),
                                                    str(o.get("created_at") or "")))
            keep, extras = ordered[0], ordered[1:]
            retirable = [o for o in extras
                         if str(o.get("status", "")).strip().lower() != "in_progress"]
            log.warning("autopilot: quest %s has %d open pass occurrences (%s) -- keeping %s and "
                        "retiring %d duplicate series", quest_id, len(series),
                        [o.get("id") or o.get("task_id") for o in series],
                        keep.get("id") or keep.get("task_id"), len(retirable))
            if retirable:
                self._retire_quest_pass(quest_id, retirable, "duplicate series")
            series = [keep]

        # A pending "Run now" is honoured exactly ONCE, and this is the only place that decides it.
        #
        # INCIDENT (2026-09-21, 410 passes on one quest in 19 hours). ``run_requested``'s contract
        # is that the request clears itself: "a finished pass stamps last_pass_at, which makes the
        # request older and therefore spent... there is no state that can be left stuck ON (a pass
        # that runs forever)". That is false for every path where the pass runs and SKIPS: a
        # gate-skipped quest returns before ``_update_pass_bookkeeping`` ever runs (see
        # ``AutopilotPass.run``), so nothing stamps ``last_pass_at`` and the request stays pending.
        # A "Run now" pressed on a day the quest's roster excludes therefore spun a closed loop:
        # ``_expected_quest_occurrence`` returned TODAY at the current minute, the created pass was
        # due on sight, it ran, the day rule skipped it, it closed leaving no open series, and the
        # next scan two minutes later created another one. Nothing bounded it but the calendar.
        #
        # One press means one pass. Serving it once is what makes that true no matter WHY the pass
        # it produced did not stamp anything, which is the part that matters: the stuck-ON state
        # above was only the first way to reach a loop that had no bound of its own. After the
        # request is spent the quest falls back to the ordinary cadence path, which is already
        # bounded, because ``_quest_pass_days`` rolls an excluded day forward to one the roster
        # actually allows, so the occurrence it creates is not due on sight.
        #
        # The person still sees the outcome: the pass is a real task with a real result saying why
        # it did nothing. A request that produced a pass has been answered, even by a skip.
        run_request_sig = _run_request_signature(quest_id, entry)
        honour_run_request = bool(run_request_sig) and not self.state.seen(run_request_sig)
        expected_date, expected_time = self._expected_quest_occurrence(
            entry, honour_run_request=honour_run_request)
        if not series:
            # An open catch-up is not a series and must not suppress this: the quest would be left
            # with one run and no producer once that run closed. A foreign SERIES occurrence is
            # different: if it still looks alive, another account's lane appears to already be
            # servicing this quest, and creating our own here is exactly the duplicate-series bug
            # this heuristic exists to prevent (see _foreign_series_looks_alive).
            foreign_series, _ = self._split_pass_occurrences(foreign)
            alive_foreign = [o for o in foreign_series
                             if _foreign_series_looks_alive(o, entry, self._now())]
            if alive_foreign:
                log.warning(
                    "autopilot: quest %s has no pass of its own, but a foreign-owned series (%s) "
                    "still looks live (not overdue, or recently updated) -- this quest's "
                    "autopilot appears to already be served by another account's live lane, not "
                    "creating a second series. If that lane ever stops, a human needs to "
                    "clear/reassign the stale pass in the app before this lane will create its "
                    "own.",
                    quest_id, [o.get("id") or o.get("task_id") for o in alive_foreign])
                return
            self._create_quest_pass(quest_id, entry, expected_date, expected_time)
            self._spend_run_request(quest_id, run_request_sig, honour_run_request)
            return
        if series[0] in foreign:
            # Alive, so nothing to create; unwritable, so nothing to retune. Already logged above.
            return
        outcome = self._retune_quest_pass(quest_id, entry, series[0], expected_date, expected_time)
        if outcome == "date_conflict" and honour_run_request and not catch_ups:
            self._create_quest_catchup_pass(quest_id, entry)
        self._spend_run_request(quest_id, run_request_sig, honour_run_request)

    def _spend_run_request(self, quest_id: str, signature: str, honoured: bool) -> None:
        """Record that this quest's pending "Run now" has been served, so it is never served twice.

        Called only from the branches that actually acted on it (created the pass, or moved the
        series' occurrence onto today), never from the ones that returned without writing anything,
        so a request held off by a live foreign series is still pending when that lane goes away.
        """
        if not honoured or not signature:
            return
        self.state.mark(signature)
        log.info("autopilot: quest %s's pending run request has been served by a pass; it will "
                 "not be served again (press Run now again for another)", quest_id)

    def _create_quest_pass(self, quest_id: str, entry: Dict[str, Any], expected_date: str,
                           expected_time: str) -> None:
        env_id = entry.get("env_id") or self.cfg.env_id or None
        recurrence = _quest_pass_recurrence(entry, expected_time,
                                            date.fromisoformat(expected_date))
        created = self.client.create_task(
            "Autopilot pass for this quest: work its current-scope goals and its standing "
            "instructions.",
            title="Autopilot pass",
            # THE QUEST'S team, not the lane's home team. A lane can discover from several teams
            # (RunnerConfig.team_ids), and a pass filed on the wrong team is a pass the quest's
            # own people cannot see, pause or audit. The snapshot recorded which team each quest
            # was listed under; the home team is only the fallback for an entry that somehow has
            # none. On a single-team lane the two are the same value.
            team_id=entry.get("team_id") or self.cfg.team_id,
            goal_id=quest_id,
            source="chat",
            task_kind=AUTOPILOT_PASS_KIND,
            recurrence=recurrence,
            scheduled_date=expected_date,
            scheduled_time=expected_time,
            env_id=env_id,
            # The lane that ensures this pass exists is the lane that runs it. Without this the
            # backend makes the QUEST'S OWNER the executor, and since discovery is owner-scoped,
            # a pass on a human-owned quest would sit queued forever, invisible to every lane
            # (see RunnerConfig.lane_user_id). None keeps the old default-executor behaviour.
            assignee_user_id=self.cfg.lane_user_id or None,
        ) or {}
        log.info("autopilot: created quest %s's own pass (%s at %s %s, first occurrence %s)",
                 quest_id, recurrence["frequency"], expected_time,
                 entry.get("run_timezone") or "runner-local", expected_date)

    def _create_quest_catchup_pass(self, quest_id: str, entry: Dict[str, Any]) -> None:
        """A ONE-OFF pass for a pending "Run now" that the quest's series cannot absorb.

        WHY THIS EXISTS (incident, 2026-09-05). "Run now" stamps ``run_requested_at``, and the
        schedule side of honouring it is ``_expected_quest_occurrence`` returning TODAY so the
        series' open occurrence moves onto today. That works right up until today's slot is
        already taken: the backend holds a unique index on (series, scheduled_date), so a quest
        whose pass already ran this morning cannot have a SECOND occurrence of the same series
        dated today. The PATCH came back 409, the retune logged the conflict and gave up, and the
        request was simply never honoured -- the run slipped to the series' next occurrence
        tomorrow, or further on a roster where nobody is on duty tomorrow. That is precisely the
        case the button exists for, so silently slipping it is the whole bug.

        The two obvious implementations are both wrong. Moving the series onto today fights the
        index and loses, every time, for as long as the request stays pending. Running the pass
        inline from the poller would make the one run the person explicitly asked for the one run
        nobody can see, pause, or audit. So the request gets a REAL task like every other pass:
        same ``task_kind``, same quest, same env, scheduled for now in the quest's own zone, and
        explicitly NO recurrence. That respects the index instead of fighting it and leaves the
        series' own schedule exactly where it was.

        Not creating a second one is the caller's job, and it is what ``_split_pass_occurrences``
        buys it: a pending request stays pending until a pass stamps ``last_pass_at``, so without
        that check every scan in between would add another catch-up.
        """
        zone = entry.get("run_timezone")
        now_local = now_in_zone(zone, self._now())
        env_id = entry.get("env_id") or self.cfg.env_id or None
        self.client.create_task(
            "Autopilot pass for this quest (requested run): work its current-scope goals and its "
            "standing instructions.",
            title="Autopilot pass (requested)",
            # Same rule as the series pass: the QUEST'S own team (see ``_create_quest_pass``).
            team_id=entry.get("team_id") or self.cfg.team_id,
            goal_id=quest_id,
            source="chat",
            task_kind=AUTOPILOT_PASS_KIND,
            # No ``recurrence`` argument at all, deliberately: that absence is the ONLY thing
            # keeping this a one-off rather than a second daily series, both for the backend's
            # spawner and for ``_split_pass_occurrences`` on the next scan.
            scheduled_date=now_local.date().isoformat(),
            scheduled_time=now_local.strftime("%H:%M"),
            env_id=env_id,
            # Same reason as the series pass: this lane has to be the executor or discovery never
            # returns the task it just created (see RunnerConfig.lane_user_id).
            assignee_user_id=self.cfg.lane_user_id or None,
        )
        log.info("autopilot: quest %s's series cannot move onto today, so its pending run request "
                 "gets a one-off catch-up pass (no recurrence) at %s %s", quest_id,
                 now_local.strftime("%H:%M"), entry.get("run_timezone") or "runner-local")

    def _retune_quest_pass(self, quest_id: str, entry: Dict[str, Any],
                           occurrence: Dict[str, Any], expected_date: str,
                           expected_time: str) -> str:
        """PATCH the quest's open occurrence to ``expected_date``/``expected_time``/the roster's
        current recurrence shape when any of those differ from what it currently holds -- zero
        writes when they already match (the steady state must stay quiet). This is what makes a
        changed ``run_time`` (or a roster edit that adds/removes a day) take effect (the spawner
        inherits ``recurrence`` from the task document, so without this a change would never reach
        a future occurrence) AND what corrects the backend's UTC-dated spawn (A3).

        Only ever call this on a SERIES occurrence. Retuning a one-off catch-up would stamp a
        daily ``recurrence`` on it (an absent recurrence differs from the expected one, so it goes
        into the diffs) and quietly promote it into a second series, which is exactly what
        happened on 2026-09-05. ``_split_pass_occurrences`` is what keeps that call from being
        made.

        Returns what the caller has to branch on: ``"ok"`` when the occurrence now holds the
        expected schedule (written, or already matching -- both are success), ``"date_conflict"``
        when the backend refused the move because that date is already taken (409), and
        ``"failed"`` for anything else. A bare boolean would not do the job: only the date
        conflict has a second course of action open to it (a one-off catch-up for a pending run),
        while any other failure is simply the next scan's retry, and collapsing the two would turn
        a transient API error into a stream of catch-up tasks.
        """
        task_id = str(occurrence.get("id") or occurrence.get("task_id") or "")
        current_date = str(occurrence.get("scheduled_date") or "")
        current_time = str(occurrence.get("scheduled_time") or "")
        current_recurrence = occurrence.get("recurrence")
        if not isinstance(current_recurrence, dict):
            current_recurrence = {}
        expected_recurrence = _quest_pass_recurrence(entry, expected_time,
                                                      date.fromisoformat(expected_date))

        diffs: Dict[str, Any] = {}
        if current_date != expected_date:
            diffs["scheduled_date"] = expected_date
        if current_time != expected_time:
            diffs["scheduled_time"] = expected_time
        if current_recurrence != expected_recurrence:
            diffs["recurrence"] = expected_recurrence
        if not diffs:
            return "ok"  # steady state -- must stay quiet, no write

        settings_changed = current_time != expected_time or current_recurrence != expected_recurrence
        reason = "settings changed" if settings_changed else "spawn date corrected for timezone"
        try:
            self.client.update_task(task_id, diffs)
            log.info("autopilot: retuned quest %s's pass %s %s -> %s %s (%s)",
                     quest_id, current_date, current_time, expected_date, expected_time, reason)
            return "ok"
        except QuestApiError as e:
            if e.status == 409:
                log.warning("autopilot: could not retune quest %s's pass -- schedule conflict "
                            "(%s); leaving the occurrence alone", quest_id, e)
                return "date_conflict"
            log.warning("autopilot: could not retune quest %s's pass (%s) -- will retry next "
                        "scan", quest_id, e)
            return "failed"
        except Exception as e:  # noqa: BLE001 -- one quest's retune failure never blocks another
            log.warning("autopilot: could not retune quest %s's pass (%s) -- will retry next scan",
                        quest_id, e)
            return "failed"

    def _retire_quest_pass(self, quest_id: str, occurrences: List[Dict[str, Any]],
                           reason: str) -> None:
        """Stop a quest's pass series in ONE PATCH per occurrence: ``recurrence`` cleared AND
        ``status`` cancelled together. Order matters, and it must be one call: a bare
        ``status: "cancelled"`` PATCH would still spawn one more occurrence off the just-cancelled
        document (verified live behaviour, not theory -- see C4/C7 of the autopilot spec).

        Re-verified by hand on 2026-09-05 and this is exactly right, so do not "simplify" it into
        a status-only PATCH. The re-queue fires on ANY terminal status, cancelled included, so
        cancelling a recurring occurrence spawns the next one and the series you just retired
        comes straight back. Pausing the recurrence first and then retiring is what actually stops
        it, and doing both in one call is what leaves no window in between."""
        for occ in occurrences:
            task_id = str(occ.get("id") or occ.get("task_id") or "")
            if not task_id:
                continue
            try:
                self.client.update_task(task_id, {"recurrence": "", "status": "cancelled"})
                log.info("autopilot: retired quest %s's pass %s (%s)", quest_id, task_id, reason)
            except Exception as e:  # noqa: BLE001 -- one quest's retire failure never blocks another
                log.warning("autopilot: could not retire quest %s's pass %s (%s) — will retry "
                            "next scan", quest_id, task_id, e)

    def _send_bootstrap_report_if_due(self) -> None:
        """Send monthly bootstrap reports for the corpus, if configured.

        This is a light, best-effort housekeeping task that runs once per scan. It tracks when
        bootstrap completes and sends a monthly email report with corpus statistics, changes,
        warnings, and remediation suggestions. Reports are opt-in via
        QUEST_BOOTSTRAP_REPORTS=1 and require a cards_dir and quest id to send to.
        """
        if not self.client.configured or not self.cfg.quest_api_key:
            return

        cards_dir = getattr(self.cfg, "cards_dir", None)
        if not cards_dir:
            return

        try:
            if not check_should_send_monthly_report(cards_dir):
                return

            quest_id = getattr(self.cfg, "team_quest_id", None) or self.cfg.team_id
            if not quest_id:
                return

            report = generate_bootstrap_report(cards_dir, str(Path(cards_dir).parent.parent))
            stats = get_corpus_stats(cards_dir)

            try:
                self.client.send_quest_email(
                    quest_id,
                    subject=report["subject"],
                    body=report["body"],
                    rep_id="qar",
                )
                mark_report_sent(cards_dir, stats["card_count"], stats["file_count"])
                log.info("bootstrap report sent for quest %s (cards: %d, files: %d)",
                         quest_id, stats["card_count"], stats["file_count"])
            except Exception as e:  # noqa: BLE001 -- email failure never blocks the lane
                log.debug("bootstrap report not sent: %s", e)
        except Exception as e:  # noqa: BLE001 -- never block housekeeping on report errors
            log.debug("bootstrap report check failed: %s", e)
