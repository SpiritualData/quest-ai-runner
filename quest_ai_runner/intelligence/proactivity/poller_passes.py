"""Autopilot and quest-pass scheduling: ensure, create, retune and retire pass tasks.

A mixin of ``Poller`` (see ``poller.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from datetime import date, timedelta

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import AUTOPILOT_PASS_KIND, OPEN_TASK_STATUSES
from quest_ai_runner.intelligence.proactivity.autopilot_pending import pending_work_for
from quest_ai_runner.intelligence.proactivity.autopilot_units import AUTOPILOT_RUN_KIND
from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestApiError
from quest_ai_runner.intelligence.subconscious.cards_from_files.bootstrap_reporter import (
    check_should_send_monthly_report, generate_bootstrap_report, get_corpus_stats, mark_report_sent)
from quest_ai_runner.consciousness.request_intake.local_time import now_in_zone, today_in_zone
from quest_ai_runner.intelligence.proactivity.poller_schedule import log


class PassesMixin:
    """Autopilot and quest-pass scheduling: ensure, create, retune and retire pass tasks."""

    def _ensure_autopilot_pass(self) -> None:
        """Retire the pass tasks left over from before named autopilots, then stop looking.

        The backend schedules autopilots now (one ``autopilot_run`` task per run), so no pass series is created here any more. A deployment that
        migrated has its open legacy pass tasks cancelled by the migration; any that remain (a lane that was down, a task owned by another
        account) are retired by this scan. The first scan that finds none open sets ``_legacy_passes_retired`` and every later scan returns at once.

        A FAILED read never retires (``None`` is not "nothing open"), and the read is one status-filtered list per open status, never the unfiltered
        owner list the backend caps (see ``_list_owned_pass_tasks``). Best-effort throughout: a failure is logged and retried next scan, and never
        blocks the ordinary task pickup that follows.
        """
        if not self.cfg.autopilot_ensure_pass_task:
            return
        if not self.client.configured or not (self.cfg.team_id or self.cfg.team_ids):
            return
        if getattr(self, "_legacy_passes_retired", False):
            return          # one cheap check per scan once a scan has found no open pass task of the old kind
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
            if not any(open_by_series.values()):
                # Nothing of the old kind is open anywhere this lane can see: the retirement is done, and the lane stops listing for it.
                self._legacy_passes_retired = True
                log.info("autopilot: no leftover pass task of the old kind is open; retirement is done")
                return
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
                    snapshot[quest_id] = {"team_id": team or self.cfg.team_id,
                                          "archived": bool(row.get("archived"))}
        opted_in = 0
        # One small call for every quest's autopilot block (about 1 KB each) instead of every quest's whole state (about 80 KB
        # each, 15 quests every 5 minutes). None = an older backend: read each quest's state as before.
        schedules = None
        try:
            fetch = getattr(self.client, "get_autopilot_schedules", None)
            schedules = fetch(list(snapshot)) if callable(fetch) else None
        except Exception:  # noqa: BLE001 -- the per-quest read below is the fallback
            log.info("autopilot: schedules read failed; reading each quest's state", exc_info=True)
        for quest_id, entry in list(snapshot.items()):
            try:
                state = (schedules.get(quest_id) or {}) if schedules is not None else (self.client.get_quest_autopilot(quest_id) or {})
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
            if state.get("archived") or entry.get("archived"):
                # An archived quest gets no pass whatever its stored config says (the backend also
                # turns the mode off on archive). It stays in the snapshot as "off" rather than
                # being dropped, so the existing off-mode path retires a pass it still has.
                mode = "off"
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
                "has_instructions": bool(autopilot_cfg["has_instructions"]) if "has_instructions" in autopilot_cfg else bool(instructions),
                "env_id": autopilot_cfg.get("env_id"),
                # Pending work the lane's config names for this quest (0 when none): the schedule
                # reads it through the same ``due_trigger`` the pass's own gate does.
                "pending_work": (lambda p: p.count if p else 0)(pending_work_for(
                    quest_id, getattr(self.cfg, "autopilot_pending_work", None),
                    self.cfg.quest_folder_map)),
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
        ordinary work -- returns ``None`` so ``tasks_due_now`` keeps today's naive-local
        comparison for them: an ``assistant_task`` carries no timezone of its own, and inventing
        one for a non-pass task is a separate feature, out of scope here.
        """
        if str(task.get("task_kind") or "").strip().lower() not in (AUTOPILOT_PASS_KIND, AUTOPILOT_RUN_KIND):
            return None
        quest_id = str(task.get("goal_id") or "").strip()
        if not quest_id:
            return None
        entry = self._quest_schedule_snapshot().get(quest_id)
        return (entry or {}).get("run_timezone") or None

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
                          task_kind=AUTOPILOT_PASS_KIND, slim=True)
        return self.client.list_tasks(team_id=team_id, goal_id=goal_id, status=status,
                                      task_kind=AUTOPILOT_PASS_KIND, slim=True)

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
        """Retire what is left of ONE quest's old pass series. Nothing here creates a pass any more.

        Autopilots run on the backend's schedule now: its scheduler queues one ``autopilot_run`` task per run and this lane only executes it, so a
        recurring pass task (``task_kind == "autopilot"``) has no work left to do and every one still open is retired, catch-ups included. Only the ones
        this lane owns: an occurrence owned by another account is a 404 per scan and no outcome (it is logged for a human to clear).
        """
        foreign = self._unwritable_pass_occurrences(occurrences)
        if foreign:
            log.warning("autopilot: quest %s has %d open pass occurrence(s) owned by another account (%s) -- this lane cannot cancel them "
                        "(an owner-scoped PATCH 404s); clear them in the app when convenient",
                        quest_id, len(foreign), [o.get("id") or o.get("task_id") for o in foreign])
        writable = [o for o in occurrences if o not in foreign]
        if writable:
            self._retire_quest_pass(quest_id, writable, "autopilots run on the backend's schedule now")

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
