"""Answered-ask sweep: every ask a person answered with "have my AI act on it later" gets acted on.

A mixin of ``Poller`` (see ``poller.py``): methods only, state lives on the instance.

WHY. Quest marks an answered ask "waiting for your AI" (``ai_action_status`` pending) and only a
``done`` run on that quest settles it (``ContextUpdateBundle.settle_answered_asks``). A quest with
no scheduled run never got one, so those answers sat there for weeks. Waiting for a run to happen
to come by is a hope, not a structure: this sweep makes the run exist.

WHAT. Once per interval, for each quest that has pending answered asks, make sure ONE open
assistant task exists on it. That task's run reads the quest's context, which carries the answers
(``DecisionsSource``), acts on them, and finishing ``done`` marks them acted. Idempotent: an open
task of this kind on the quest (queued, in progress, needs you, suggested) is reused, never
doubled, and a failed read of either list creates nothing.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import OPEN_TASK_STATUSES
from quest_ai_runner.intelligence.proactivity.poller_schedule import log

ANSWERED_ASKS_TITLE = "Act on answered asks"
ANSWERED_ASKS_TEXT = (
    "Answers the person gave to asks on this quest are waiting for you. Read them in the context "
    "(answered asks, with their own words and whether they approved, declined or passed), act on "
    "each one, and do not ask it again. If an answer needs no action, say so in the result.")
SWEEP_MIN_SECONDS = 300


def _quest_of(row: Dict[str, Any]) -> str:
    return str(row.get("quest_id") or row.get("goal_id") or row.get("questId") or "").strip()


class AnswersMixin:
    """One queued assistant task per quest that has answered asks waiting for the AI."""

    def _ensure_answered_ask_tasks(self) -> List[str]:
        """Create (or reuse) one task per quest with pending answered asks. Returns the quest ids a
        task was created for. Best-effort and throttled: never raises, never blocks pickup."""
        if not getattr(self.cfg, "answered_asks_sweep", True):
            return []
        if not self.client.configured:
            return []
        last = getattr(self, "_answered_sweep_at", None)
        now = time.monotonic()
        if last is not None and now - last < SWEEP_MIN_SECONDS:
            return []
        self._answered_sweep_at = now
        created: List[str] = []
        try:
            lister = getattr(self.client, "list_pending_answers", None)
            if not callable(lister):
                return []
            quests: List[str] = []
            for row in lister() or []:
                q = _quest_of(row) if isinstance(row, dict) else ""
                if q and q not in quests:
                    quests.append(q)
            for quest_id in quests:
                try:
                    open_task = self._open_answered_task(quest_id)
                    if open_task is None or open_task:
                        continue                       # unreadable (create nothing) or one is on its way
                    self._create_answered_task(quest_id)
                    created.append(quest_id)
                except Exception as e:  # noqa: BLE001 -- one quest never blocks another
                    log.warning("answered asks: could not queue a task on quest %s (%s)", quest_id, e)
        except Exception as e:  # noqa: BLE001 -- housekeeping never blocks pickup
            log.warning("answered asks: sweep failed (%s); retrying next interval", e)
        return created

    def _open_answered_task(self, quest_id: str) -> Optional[Dict[str, Any]]:
        """The open sweep task on this quest (``{}`` when none), or ``None`` when the read failed.

        Read per quest with ``goal_id`` so a task owned by the quest's human is still seen."""
        rows = self.client.list_tasks_or_none(goal_id=quest_id, slim=True)
        if rows is None:
            return None
        for t in rows:
            if (str(t.get("status") or "").strip().lower() in OPEN_TASK_STATUSES
                    and str(t.get("title") or "").strip() == ANSWERED_ASKS_TITLE):
                return t
        return {}

    def _create_answered_task(self, quest_id: str) -> None:
        team = ""
        try:
            team = self.client.owning_team_for(quest_id) or ""
        except Exception:  # noqa: BLE001 -- the client's own default team applies
            team = ""
        lane = (getattr(self.cfg, "lane_user_id", "") or "").strip()
        result = self.client.create_task(
            ANSWERED_ASKS_TEXT, team_id=team or None, goal_id=quest_id, source="review",
            title=ANSWERED_ASKS_TITLE, assignee_user_id=lane or None)
        log.info("answered asks: queued a task on quest %s (%s)", quest_id, getattr(result, "status", ""))
