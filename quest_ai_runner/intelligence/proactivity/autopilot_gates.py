"""Which runs may go ahead: the extra-run allowance, backpressure and holds.

A mixin of ``AutopilotPass`` (see ``autopilot.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import (
    AUTOPILOT_LEGACY_SOURCE, AUTOPILOT_WORK_KIND, EXTRA_RUN_KEY_PREFIXES, OPEN_TASK_STATUSES, TASK_STATUSES_FOR_COUNT, log)
from quest_ai_runner.intelligence.proactivity.autopilot_units import AUTOPILOT_RUN_KIND
from quest_ai_runner.intelligence.proactivity.autopilot_periods import _parse_dt
from quest_ai_runner.intelligence.proactivity.autopilot_pending import PendingWork, pending_work_for
from quest_ai_runner.intelligence.proactivity.autopilot_result import AutopilotResult
from quest_ai_runner.intelligence.proactivity.quest_asks_brief import fetch_asks


class GatesMixin:
    """Which runs may go ahead: the extra-run allowance, backpressure and holds."""

    # --- gates -------------------------------------------------------------------------------

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

    def _pending_work(self, quest_id: str) -> Optional[PendingWork]:
        """The quest's configured pending work (``RunnerConfig.autopilot_pending_work``), if any."""
        return pending_work_for(quest_id, self._pending_work_specs, self._quest_folder_map)

    def quest_asks(self, quest_id: str) -> Optional[List[Dict[str, Any]]]:
        """The quest's ask rows, read ONCE per pass however many readers want them (the reactive
        gate and the What You Asked section of each batch). ``None`` = the read failed, kept apart
        from ``[]`` and cached too, so a failing backend is asked once, not once per reader."""
        cache = self.__dict__.setdefault("asks_cache", {})
        if quest_id not in cache:
            cache[quest_id] = fetch_asks(self._client, quest_id)
        return cache[quest_id]

    @staticmethod
    def _is_autopilot_authored(task: Dict[str, Any]) -> bool:
        """Whether a task was created by autopilot (so it counts against the budget/backpressure).

        The marker is ``task_kind == "autopilot_work"`` (persistent, never overwritten by a claim).
        ``source == "autopilot"`` is ALSO accepted so that, if the backend's source enum later
        gains that value, rows stamped the new way still count -- and any already-created row is
        never miscounted as human work."""
        return (task.get("task_kind") == AUTOPILOT_WORK_KIND
                or task.get("source") == AUTOPILOT_LEGACY_SOURCE)

    def _extra_runs_today(self, quest_id: str, autopilot_id: str, this_task_id: str) -> Dict[str, int]:
        """How many AUTOMATIC EXTRA runs (``occurrence_key`` starting ``reply:`` or ``extra:``) were created today: this autopilot's, and the team's.

        One read per task status, never an unfiltered owner list (the unfiltered list is the newest 200 tasks and is counted against the backend's
        10-a-minute unfiltered cap; a status filter is a queue read that is neither). The run being planned is not counted against itself.
        """
        scope = self._team_for(quest_id) or self._team_id or None
        today = self._now().date()
        since = (today - timedelta(days=1)).isoformat()
        seen: set = set()
        used = {"autopilot": 0, "team": 0}
        for status in TASK_STATUSES_FOR_COUNT:
            for t in self._client.list_tasks(team_id=scope, status=status, created_after=since, slim=True) or []:
                task_id = str(t.get("id") or t.get("task_id") or "")
                if task_id and (task_id in seen or task_id == this_task_id):
                    continue
                seen.add(task_id)
                if t.get("task_kind") != AUTOPILOT_RUN_KIND or not str(t.get("occurrence_key") or "").startswith(EXTRA_RUN_KEY_PREFIXES):
                    continue
                created = _parse_dt(t.get("created_at") or t.get("scheduled_at"))
                if created is None or created.date() != today:
                    continue
                used["team"] += 1
                if str(t.get("autopilot_id") or "") == autopilot_id and str(t.get("goal_id") or "") == quest_id:
                    used["autopilot"] += 1
        return used

    def _has_backpressure(self, quest_id: str, autopilot_id: str, this_task_id: str = "") -> bool:
        """True when an earlier run of THIS autopilot on this quest is still open, so it should not stack another on top.

        OFF unless ``RunnerConfig.autopilot_backpressure`` is set (the person's silence is not a stop sign). Per autopilot: another autopilot's
        unfinished run, or a task a person set up themselves, never gates it. ``queued``/``in_progress`` is work that has not run yet, while
        ``needs_you``/``suggested`` waits on a person; either way the answer is the same. A task's link to its quest is its ``goal_id``."""
        tasks = self._client.list_tasks(team_id=self._team_for(quest_id) or None, goal_id=quest_id, slim=True) or []
        return any(
            t.get("task_kind") == AUTOPILOT_RUN_KIND and str(t.get("autopilot_id") or "") == autopilot_id
            and str(t.get("id") or t.get("task_id") or "") != this_task_id
            and str(t.get("status", "")).strip().lower() in OPEN_TASK_STATUSES
            for t in tasks)

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

    def _skip(self, result: AutopilotResult, quest_id: str, quest_label: str,
              reason: str, unit: Optional[Dict[str, Any]] = None) -> None:
        log.info("autopilot: skipping quest %s%s (%s)", quest_id, f" autopilot {unit['id']}" if unit else "", reason)
        row = {"quest_id": quest_id, "quest_label": quest_label, "reason": reason}
        if unit is not None:
            row["autopilot"] = unit["id"]
            row["autopilot_name"] = label_of(unit)
        result.skipped.append(row)
