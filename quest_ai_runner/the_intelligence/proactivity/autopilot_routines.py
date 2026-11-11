"""Named single-purpose autopilots ("routines"): a second autopilot on a quest that does ONE kind of work and nothing else.

The quest's ordinary autopilot (``autopilot_gates`` / ``autopilot.py``) works the quest: goals, tasks, plan. A routine is the other shape the
same quest can carry, configured in ``autopilot.routines`` (the person's settings) and stamped in ``autopilot.routine_runs`` (this module's
bookkeeping): a ``purpose`` (the one kind of work), its own ``instructions``, its own days and hour, and a gate that belongs to the purpose.

What stays the hub's and what is here:
- WHEN a routine is due is the hub's Proactivity (``quest_schedule.due``): the routine's days, hour and zone as a ``window``, with the last pass
  as ``last_run``, so a routine is due once per occasion and not again until the next one.
- WHAT the pass has to do is the purpose's own question (``RoutinePurpose.due_items``). A purpose with nothing due is REFUSED: the pass
  stamps the occasion as spent and creates nothing, so a quiet week costs one bookkeeping write and no model call.
- The pass writes nothing but its one work task and its own ``routine_runs`` stamp. It never reads or writes goals, never lists or updates
  tasks, never plans, so a routine cannot do the quest's ordinary work however its instructions are worded.

The work itself is one ``autopilot_work`` task (``WorkMixin._create_autopilot_task``) that the lane runs like any other deep run; its text is
composed here from the quest's outcome, the purpose's standing rules, what is due and the routine's instructions.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from hubofhubs.api import WINDOW_DAYS

from quest_ai_runner.the_consciousness.request_intake.local_time import now_in_zone
from quest_ai_runner.the_intelligence.proactivity.autopilot_defaults import MAX_INSTRUCTIONS_CHARS
from quest_ai_runner.the_intelligence.proactivity.autopilot_work import WorkMixin
from quest_ai_runner.the_intelligence.proactivity.quest_schedule import due
from quest_ai_runner.the_self.integrity.prompts.prompt_specs import US_SPELLING_RULE

log = logging.getLogger("quest-ai-runner.autopilot")

OPTED_IN_MODES = ("suggest", "act", "reactive")
DEFAULT_GRACE_MINUTES = 360
# The one place the writing rule for every routine's work text is stated.
NO_EM_DASH_RULE = ("Write no em dashes anywhere (notes, records, decision requests): use a colon, a comma, parentheses or a new sentence. "
                   + US_SPELLING_RULE)


@dataclass(frozen=True)
class RoutinePurpose:
    """The one kind of work a routine does, and the question that decides whether there is any today.

    ``due_items(now)`` returns what the purpose has to do now (one mapping per item, with at least ``id`` and ``what``); an empty list
    refuses the pass. ``scope`` says in one sentence what the work is; ``rules`` are the standing rules of that work, verbatim in every
    pass; ``allowed_writes`` names the only Quest writes the work may make, so the text can say everything else is out of bounds.
    """
    name: str
    scope: str
    rules: str
    due_items: Callable[[datetime], Sequence[Mapping[str, Any]]]
    allowed_writes: Sequence[str] = ("add_quest_note", "create_decision")


PURPOSES: Dict[str, RoutinePurpose] = {}


def register_purpose(purpose: RoutinePurpose) -> RoutinePurpose:
    """Make ``purpose`` available to routines by its name (a consumer's own purposes ride the same registry)."""
    PURPOSES[purpose.name] = purpose
    return purpose


@dataclass
class RoutineOutcome:
    """What one routine pass did, for the log and the supervised dry pass."""
    quest_id: str
    routine: str
    status: str            # "created", "refused", "skipped", "dry-run"
    reason: str = ""
    task_id: Optional[str] = None
    items: List[Mapping[str, Any]] = field(default_factory=list)
    text: str = ""
    stamped: bool = False


def routines_of(autopilot_cfg: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The quest's routines as written by the person (a malformed entry without a name is dropped)."""
    return [dict(r) for r in (autopilot_cfg.get("routines") or []) if isinstance(r, Mapping) and str(r.get("name") or "").strip()]


def routine_schedule(routine: Mapping[str, Any], autopilot_cfg: Mapping[str, Any]) -> Dict[str, Any]:
    """The routine as one hub schedule: its days, hour and zone as a ``window``, the last pass as ``last_run``."""
    days = [str(d).strip().lower()[:3] for d in (routine.get("days") or [])] or list(WINDOW_DAYS)
    name = str(routine["name"])
    zone = str(routine.get("run_timezone") or autopilot_cfg.get("run_timezone") or "")
    window = {"days": days, "run_time": str(routine.get("run_time") or "00:00"), "time_zone": zone,
              "grace_minutes": int(routine.get("grace_minutes") or DEFAULT_GRACE_MINUTES)}
    return {"name": name, "request": f"routine {name}", "window": window,
            "last_run": str((autopilot_cfg.get("routine_runs") or {}).get(name) or "")}


def occasion_key(routine: Mapping[str, Any], autopilot_cfg: Mapping[str, Any], now: datetime) -> str:
    """The local date of the occasion, so one process never runs the same routine twice in a day even if its stamp does not stick."""
    zone = str(routine.get("run_timezone") or autopilot_cfg.get("run_timezone") or "")
    return now_in_zone(zone, now).date().isoformat()


def compose_routine_text(outcome: str, routine: Mapping[str, Any], purpose: RoutinePurpose, items: Sequence[Mapping[str, Any]], *,
                         dry_run: bool = False) -> str:
    """The work task's text: the quest's outcome, the one purpose and its bounds, what is due, then the routine's own instructions verbatim."""
    name = str(routine["name"])
    instructions = str(routine.get("instructions") or "").strip()[:MAX_INSTRUCTIONS_CHARS]
    due_lines = "\n".join(f"- {item.get('what') or item.get('id')}" for item in items)
    allowed = ", ".join(purpose.allowed_writes)
    parts = [
        f"Routine pass: {name} ({purpose.name}) on a quest whose outcome is: {outcome or '(none stated)'}",
        f"This pass does ONE kind of work and nothing else: {purpose.scope}",
        "Out of bounds, however the instructions below are worded: planning, creating or editing goals, creating or updating tasks, replying to "
        f"asks, and any other work on the quest. The only Quest writes you may make are: {allowed}.",
        f"Standing rules of this work:\n{purpose.rules.strip()}",
        f"Due now ({len(items)}):\n{due_lines}",
        f"Instructions of this routine (the person's specification, verbatim):\n{instructions}",
        NO_EM_DASH_RULE,
    ]
    if dry_run:
        parts.append("DRY RUN: describe what you would do and change nothing.")
    return "\n\n".join(parts)


class RoutinePass(WorkMixin):
    """Runs the due routines of one quest. Shares the work-task creation of the ordinary pass and nothing else of it.

    ``client`` needs ``get_quest_autopilot``, ``create_task`` and ``update_quest_autopilot`` (the same ``QuestClient`` the ordinary pass uses).
    ``purposes`` defaults to the module registry; ``now`` is a clock for tests.
    """

    def __init__(self, client: Any, *, team_resolver: Optional[Callable[[str], str]] = None, team_id: str = "",
                 lane_user_id: Optional[str] = None, purposes: Optional[Mapping[str, RoutinePurpose]] = None,
                 now: Optional[Callable[[], datetime]] = None, structure_watch: Optional[Callable[[str, bool], Any]] = None):
        self._structure_watch = structure_watch      # the hub's structure watch over the quest, once per routine run (see ``autopilot_structure_watch``)
        self._client = client
        self._team_resolver = team_resolver
        self._team_id = team_id or ""
        self._lane_user_id = (lane_user_id or "").strip() or None
        self._purposes = purposes if purposes is not None else PURPOSES
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._pass_task_id: Optional[str] = None
        self._spent: set = set()
        self._lock = threading.Lock()

    def _team_for(self, quest_id: str) -> str:
        return (self._team_resolver(quest_id) if self._team_resolver else "") or self._team_id

    def run_quest(self, quest_id: str, *, dry_run: bool = False, only: Optional[str] = None, ignore_schedule: bool = False) -> List[RoutineOutcome]:
        """Every routine of the quest that is due now, run once. ``dry_run`` reads and composes but writes nothing and spends nothing;
        ``ignore_schedule`` (a supervised dry pass only) evaluates a routine that is not due yet. ``only`` names one routine."""
        state = self._client.get_quest_autopilot(quest_id) or {}
        cfg = state.get("autopilot") or {}
        quest = {"quest_id": quest_id, "outcome": state.get("outcome") or "", "autopilot": cfg}
        outcomes: List[RoutineOutcome] = []
        for routine in routines_of(cfg):
            if only and routine["name"] != only:
                continue
            outcome = self.run_routine(quest, routine, dry_run=dry_run, ignore_schedule=ignore_schedule)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def run_routine(self, quest: Mapping[str, Any], routine: Mapping[str, Any], *, dry_run: bool = False,
                    ignore_schedule: bool = False) -> Optional[RoutineOutcome]:
        """One routine, gates cheapest first. ``None`` means not due (nothing to report); anything else is an outcome."""
        quest_id, cfg, name = str(quest["quest_id"]), quest.get("autopilot") or {}, str(routine["name"])
        now = self._now()

        def skipped(reason: str) -> RoutineOutcome:
            log.info("autopilot routine %s on %s: skipped (%s)", name, quest_id, reason)
            return RoutineOutcome(quest_id, name, "skipped", reason)

        if str(cfg.get("mode") or "off") not in OPTED_IN_MODES:
            return skipped("the quest's autopilot is not switched on")
        if routine.get("enabled") is False:
            return skipped("the routine is disabled")
        purpose = self._purposes.get(str(routine.get("purpose") or ""))
        if purpose is None:
            return skipped(f"this runner has no purpose named {routine.get('purpose')!r}")
        key = (quest_id, name, occasion_key(routine, cfg, now))
        if not dry_run and key in self._spent:
            return None
        if not ignore_schedule and not due([routine_schedule(routine, cfg)], now):
            return None
        items = list(purpose.due_items(now))
        if not items:
            if dry_run:
                return RoutineOutcome(quest_id, name, "dry-run", f"refused: nothing is due for {purpose.name}")
            return self.refuse(quest_id, name, f"nothing is due for {purpose.name}", key, now)
        if self._structure_watch is not None:
            self._structure_watch(quest_id, dry_run)
        text = compose_routine_text(str(quest.get("outcome") or ""), routine, purpose, items, dry_run=dry_run)
        if dry_run:
            return RoutineOutcome(quest_id, name, "dry-run", f"would create one {purpose.name} task for {len(items)} due item(s)", items=items, text=text)
        with self._lock:
            if key in self._spent:
                return None
            self._spent.add(key)
        task_id = self._create_autopilot_task(dict(quest), quest_id, text, str(cfg.get("mode")),
                                              title=f"Routine: {name}")
        stamped = self.stamp(quest_id, name, now)
        return RoutineOutcome(quest_id, name, "created", f"created one {purpose.name} task for {len(items)} due item(s)", task_id, items, text, stamped)

    def refuse(self, quest_id: str, name: str, reason: str, key: tuple, now: datetime) -> RoutineOutcome:
        """A pass the gate refused: the occasion is spent (one stamp, no task), so the question is not asked again until the next one."""
        with self._lock:
            self._spent.add(key)
        stamped = self.stamp(quest_id, name, now)
        log.info("autopilot routine %s on %s: refused (%s)", name, quest_id, reason)
        return RoutineOutcome(quest_id, name, "refused", reason, stamped=stamped)

    def stamp(self, quest_id: str, name: str, now: datetime) -> bool:
        """Record the pass in ``routine_runs`` (a key-by-key merge on the server, so the person's settings are never rewritten) and verify it stuck."""
        stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            echoed = ((self._client.update_quest_autopilot(quest_id, {"routine_runs": {name: stamp}}) or {}).get("autopilot") or {})
        except Exception:  # noqa: BLE001 -- bookkeeping never fails a pass; the in-process spent set still holds the occasion
            log.warning("autopilot routine %s on %s: could not record the pass", name, quest_id, exc_info=True)
            return False
        stuck = (echoed.get("routine_runs") or {}).get(name) == stamp
        if echoed and not stuck:
            log.warning("autopilot routine %s on %s: the backend did not persist routine_runs", name, quest_id)
        return stuck or not echoed
