"""The poller's part of routines: each scan, run the routines that the cached schedule snapshot says are due.

The snapshot (``PassesMixin._quest_schedule_snapshot``, refreshed on the lane's settings interval) already carries every quest's routines, so
the per-scan check is local and asks the hub's Proactivity only for quests that have an enabled routine. A due routine is then read fresh
(one quest read) by ``RoutinePass`` so a pass never runs on stale settings. Routines are independent of the quest's ordinary pass series:
no series task, no change to its schedule.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import quest_ai_runner.the_intelligence.proactivity.autopilot_purpose_learning  # noqa: F401 -- registers the learning purpose
from quest_ai_runner.the_intelligence.proactivity.autopilot_routines import OPTED_IN_MODES, RoutinePass, routine_schedule, routines_of
from quest_ai_runner.the_intelligence.proactivity.quest_schedule import due

log = logging.getLogger("quest-ai-runner.poller")


class RoutinesMixin:
    """Runs due routines. Needs ``cfg``, ``client``, ``_now``, ``autopilot_team_for`` and ``_quest_schedule_snapshot`` from the poller."""

    def due_routine_names(self, snapshot: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
        """``{quest_id: [routine names due now]}`` read from the snapshot alone (no Quest call)."""
        now, found = self._now(), {}
        for quest_id, entry in snapshot.items():
            if entry.get("mode") not in OPTED_IN_MODES:
                continue
            cfg = {"routines": entry.get("routines"), "routine_runs": entry.get("routine_runs"), "run_timezone": entry.get("run_timezone")}
            names = [r["name"] for r in routines_of(cfg) if r.get("enabled") is not False and due([routine_schedule(r, cfg)], now)]
            if names:
                found[quest_id] = names
        return found

    def run_due_routines(self) -> None:
        """Best-effort, like the pass producer: a failure is logged and retried next scan, never blocks task pickup."""
        if not self.cfg.autopilot_ensure_pass_task or not self.client.configured:
            return
        try:
            due_names = self.due_routine_names(self._quest_schedule_snapshot())
            if not due_names:
                return
            if getattr(self, "routine_pass", None) is None:
                self.routine_pass = RoutinePass(self.client, team_resolver=self.autopilot_team_for, team_id=self.cfg.team_id or "",
                                                lane_user_id=self.cfg.lane_user_id, now=self._now,
                                                structure_watch=getattr(self, "_structure_watch", None))
            for quest_id, names in due_names.items():
                for name in names:
                    outcome = self.routine_pass.run_quest(quest_id, only=name)
                    for item in outcome:
                        log.info("autopilot routine %s on %s: %s %s", item.routine, quest_id, item.status, item.reason)
        except Exception as e:  # noqa: BLE001 -- never let this block the scan
            log.warning("autopilot: could not run due routines (%s) -- will retry next scan", e)
