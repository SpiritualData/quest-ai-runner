"""Which quests a pass may touch: eligibility, daily budget, backpressure and holds.

A mixin of ``AutopilotPass`` (see ``autopilot.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime

from .local_time import now_in_zone
from .autopilot_personas import persona_entries_on_duty
from .autopilot_defaults import (
    AUTOPILOT_LEGACY_SOURCE,
    AUTOPILOT_WORK_KIND,
    OPEN_TASK_STATUSES,
    TASK_STATUSES_FOR_COUNT,
    log,
)
from .autopilot_periods import _parse_dt, cadence_due, has_new_ask_since, run_requested
from .autopilot_result import AutopilotResult


class GatesMixin:
    """Which quests a pass may touch: eligibility, daily budget, backpressure and holds."""

    # --- gates -------------------------------------------------------------------------------

    def _eligible_quests(self, only_quest_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The quest(s) this pass should work.

        ``only_quest_id`` set (a per-quest pass, the hybrid schedule's own series): ONE
        ``_quest_state`` read, no ``list_quests`` call at all -- cheaper than the team pass, and
        the point of a dedicated series is that it already knows which quest it is for. Opted in
        -> that one quest. No longer opted in (mode flipped back to off since the series was
        created) -> ``[]``, logged at INFO; the poller retires the now-orphaned pass on its next
        sweep, this method just declines to work it.

        ``only_quest_id`` None: every opted-in quest (``autopilot.mode`` in suggest/act/reactive). A pass
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
            if mode not in ("suggest", "act", "reactive"):
                log.info("autopilot: quest %s mode=%r -- no longer opted in; this pass should be "
                        "retired", only_quest_id, mode)
                return []
            return [{
                "quest_id": only_quest_id,
                "outcome": state.get("outcome") or "",
                "autopilot": autopilot_cfg,
            }]

        # One listing per team this lane serves (just the home team unless ``team_ids`` was
        # given), merged. Quest ids are globally unique, so a quest seen under two teams is read
        # once. Which team each quest's work is then filed on comes from ``_team_for``, not from
        # the listing it happened to appear in, so the answer is the same however it was found.
        rows_by_quest: Dict[str, Dict[str, Any]] = {}
        for team in (self._team_ids or [self._team_id]):
            for row in (self._client.list_quests(team_id=team or None) or []):
                quest_id = str(row.get("quest_id") or row.get("id") or "")
                if quest_id and quest_id not in rows_by_quest:
                    rows_by_quest[quest_id] = row
        eligible = []
        for quest_id, row in rows_by_quest.items():
            state = self._quest_state(quest_id)
            autopilot_cfg = (state.get("autopilot") or {}) if state else {}
            mode = str(autopilot_cfg.get("mode") or "off")
            if mode not in ("suggest", "act", "reactive"):
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

    def _quest_now(self, autopilot_cfg: Dict[str, Any]) -> datetime:
        """This moment read on the QUEST'S OWN clock (``autopilot.run_timezone``), which is the
        only clock the day rule may ever be evaluated against.

        INCIDENT (2026-09-21). The runner's clock is UTC, so ``self._now().strftime('%A')`` is the
        UTC weekday. The poller decides which days a quest's series may fire on in the quest's zone
        (``_quest_pass_days`` / ``_next_allowed_date``, both anchored on ``today_in_zone``), so for
        the seven hours between local evening and UTC midnight the schedule and this gate disagreed
        about what day it was. A quest rostered Tue-Fri + Sun was scheduled on Sunday evening
        Pacific and then gate-skipped as "not rostered for Monday" -- the exact disagreement
        ``_gate_quest``'s own cadence comment promises can never happen. Reading the roster day
        here the same way the schedule reads it is what makes that promise true.

        Falls back to the runner's local clock when the zone is missing or unresolvable, which is
        ``now_in_zone``'s documented behaviour and matches ``cadence_due``'s ``tz=`` fallback.
        """
        return now_in_zone(autopilot_cfg.get("run_timezone"), self._now())

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
        # REACTIVE mode's own gate: the cadence/run_time above says WHEN this quest is allowed to
        # check in, this says whether there is anything to check in ABOUT. Skipped entirely by an
        # explicit "Run now" (run_requested), the same override cadence itself already respects --
        # a person pressing the button gets a run regardless of whether Autopilot thinks anything
        # is new. See ``has_new_ask_since`` for why this reads the asks ledger rather than notes
        # directly: an ask can arrive through any touchpoint the ledger captures, not only a note.
        if (str(autopilot_cfg.get("mode") or "off") == "reactive"
                and not run_requested(autopilot_cfg)):
            asks = self._client.list_asks(quest_id=quest_id)
            # ``None`` means the READ failed (see ``QuestClient.list_asks``), not that nothing is
            # new -- a transient backend blip or an expired key must never be read as "no new ask
            # since the last pass", or this gate quietly wedges a reactive quest shut forever.
            # Fail OPEN: run the pass rather than skip on a read we could not actually perform.
            if asks is None:
                log.warning("autopilot: could not read asks for quest %s; reactive gate failing "
                            "open (running the pass)", quest_id)
            elif not has_new_ask_since(asks, autopilot_cfg.get("last_pass_at")):
                return "reactive: no new ask since the last pass"
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
        quest_now = self._quest_now(autopilot_cfg)
        if (autopilot_cfg.get("personas")
                and not persona_entries_on_duty(autopilot_cfg, quest_now)):
            return (f"no character on this quest's roster is rostered for "
                    f"{quest_now.strftime('%A')}")
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

    def _count_autopilot_tasks_today(self, team_id: Optional[str] = None) -> int:
        """Autopilot-authored tasks created TODAY on ONE team (the daily budget's denominator).

        The Quest list route has no ``source``/``task_kind`` query filter, so this pulls the
        team's tasks and narrows client-side (``list_tasks`` does the same, honestly).

        ``team_id`` omitted means the lane's own team, exactly as this always behaved. A lane
        serving several teams passes the quest's team so each team's budget is counted against
        that team's own tasks -- see the decision recorded at the call site in ``run``."""
        # One read per task status, never an unfiltered owner list. The unfiltered list is the
        # newest 200 tasks and is counted against the backend's 10-a-minute unfiltered cap; a
        # status filter is a queue read that is neither (2026-10-11).
        scope = (team_id or self._team_id) or None
        today = self._now().date()
        seen: set = set()
        count = 0
        for status in TASK_STATUSES_FOR_COUNT:
            for t in self._client.list_tasks(team_id=scope, status=status) or []:
                task_id = str(t.get("id") or t.get("task_id") or "")
                if task_id and task_id in seen:
                    continue
                if task_id:
                    seen.add(task_id)
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
            team_id=self._team_for(quest_id) or None, goal_id=quest_id) or []
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

    def _skip(self, result: AutopilotResult, quest_id: str, quest_label: str,
              reason: str) -> None:
        log.info("autopilot: skipping quest %s (%s)", quest_id, reason)
        result.skipped.append({"quest_id": quest_id, "quest_label": quest_label,
                               "reason": reason})
