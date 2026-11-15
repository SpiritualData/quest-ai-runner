"""Quest's arm of the hub's Prioritization: it COMPOSES the hub's ``stated_fields`` arm and overrides only Quest's own rules.

Everything generic is the hub's, called through ``hubofhubs.api``: what the person stated (order, deferrals, not-before, only-kinds, feedback, kind order), a blocked
item after the unblocked, the person's own rank, the deadline, the age of an ask, the people waiting, the time since a nudge, for asks what answering frees and the
effort, for goals the met and ended milestones and what a milestone frees, and the ``batch_limit``. The answer (each item's place, reason and weighted milestone) is
the hub's. This arm adds only what Quest's data and code say, and hands it to the hub in the fields the hub already reads:

- a quest list: a person's own entry in ``rank_by_person`` decides, else the quest model's legacy ``rank`` counts only for the quest's ``owner`` (an item with no
  ``owner`` carries the person's own rank);
- the work to start, with no kind order stated: open asks, then goals of the periods running, then the system's own ideas (``DEFAULT_KIND_ORDER``);
- goals: the shorter ``horizon`` first (today, week, month, quarter, year; none last) after the person's rank and before the deadline; a ``completed`` goal after
  every open one, blocked or not (the hub puts a blocked item after the unblocked before it reads the rank, so ``completed_last`` places the completed ones after it has ordered); a goal the person's AI rep handles (``ai_handling == "rep"``) after the person's own;
- the tie-breaks the hub leaves to the order given: undated asks put to the person newest first by ``created_at``, a milestone's metric measured longest ago first;
- one to one: a quest's period goal IS the milestone of the solution it is for, linked by ``spec_solution_id`` (carried here as ``solution_id``): ``as_milestone``
  gives the goal in the hub's milestone fields, and the goal has no ranking of its own.

Horizon, completed and rep-handled are folded with the person's rank into the one ``rank`` the hub reads (``ladder_rank``), so the hub's own rules (blocked
first, then the deadline and the rest) apply after them. Open asks oldest first and the nearest deadline with undated last are the hub's rules, not restated here.
"""
import dataclasses
import functools
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from esoi import implementation
from hubofhubs.api import AssemblyConfig, Stated, assemble

DEFAULT_KIND_ORDER = ("ask", "goal", "idea")
HORIZONS = ("today", "week", "month", "quarter", "year")
EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def moment(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        found = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


def whole(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@functools.lru_cache(maxsize=1)
def hub_arm():
    """The hub's own ``stated_fields`` arm, which does the generic ordering and gives the answer its shape."""
    return assemble(AssemblyConfig(solutions=("prioritization",), selections={"prioritization": "stated_fields"}))["prioritization"]


def own_rank(item: Mapping[str, Any], person: str) -> Optional[int]:
    """The person's own rank of a quest: their entry in ``rank_by_person``, else the legacy ``rank`` when it is theirs to give (they created the quest)."""
    entry = whole((item.get("rank_by_person") or {}).get(person))
    if entry is not None:
        return entry
    legacy = whole(item.get("rank"))
    if legacy is not None and ("owner" not in item or str(item.get("owner") or "") == person):
        return legacy
    return None


def as_milestone(goal: Mapping[str, Any]) -> Mapping[str, Any]:
    """A quest's period goal in the hub's milestone fields. One to one: the goal IS the milestone of the solution it is for (Quest has no field naming the
    milestone itself: the goal row with its ``spec_solution_id``, carried as ``solution_id``, is the milestone). Its ``solution`` is that id, so what the person said about
    the solution counts for the goal; its ``deadline`` is the milestone's target time. A goal that names no solution yet is returned as it is, never given a second milestone."""
    return dict(goal, solution=str(goal["solution_id"])) if goal.get("solution_id") else goal


def ladder_mark(item: Mapping[str, Any], person: str) -> Tuple:
    """Where Quest puts an item on its ladder, before the deadline: open before completed, the person's own before an AI rep's, ranked before unranked, ascending, then the shorter horizon."""
    rank = own_rank(item, person)
    horizon = str(item.get("horizon") or "")
    return (bool(item.get("completed")), item.get("ai_handling") == "rep", (0, rank) if rank is not None else (1, 0), HORIZONS.index(horizon) if horizon in HORIZONS else len(HORIZONS))


def ladder_rank(candidates: Sequence[Mapping[str, Any]], person: str) -> List[Optional[int]]:
    """The one ``rank`` the hub reads for each candidate: its place among the distinct places of ``ladder_mark``, or None for all when they are all the same."""
    marks = [ladder_mark(c, person) for c in candidates]
    places = sorted(set(marks))
    return [places.index(m) if len(places) > 1 else None for m in marks]


def tie_order(item: Mapping[str, Any]) -> Tuple[float, datetime]:
    """What breaks a tie the hub leaves to the order given: the newest ``created_at`` first, then the metric measured longest ago (none measured first)."""
    created = moment(item.get("created_at"))
    return (-created.timestamp() if created is not None else 0.0, moment(item.get("last_nudged_at")) or EARLIEST)


@implementation("prioritization", "quest_ordering")
class QuestOrdering:
    """The hub's ``stated_fields`` arm with Quest's own rules handed to it as the fields it reads."""
    REQUIRES = ()     # standard library plus the hub

    def prioritize(self, person: str = "", request: str = "", implied: Optional[Mapping[str, Any]] = None, candidates: Sequence[Mapping[str, Any]] = (),
                   stated: Any = None, now: str = "", subject: str = "work", batch_limit: int = 0) -> Mapping[str, Any]:
        said = Stated.of(stated if stated is not None else implied)
        if subject == "work" and not said.kind_order:
            said = dataclasses.replace(said, kind_order=DEFAULT_KIND_ORDER)
        given = sorted(candidates, key=tie_order)
        places = ladder_rank(given, person)
        handed = []
        for item, place in zip(given, places):
            item = as_milestone(item) if subject == "goals" else item
            handed.append({**{k: v for k, v in item.items() if k != "rank"}, **({} if place is None else {"rank": place})})
        answer = dict(hub_arm().prioritize(person=person, candidates=handed, stated=said, now=now, subject=subject, batch_limit=batch_limit))
        by_id = {str(c.get("id")): (c, place) for c, place in zip(given, places)}
        ordered = [self.explained(placed, *by_id[placed["id"]], person) for placed in answer["ordered"]]
        if subject == "goals":
            ordered = [dict(placed, rank=place) for place, placed in enumerate(self.completed_last(ordered, by_id), start=1)]
        answer["ordered"] = ordered
        return answer

    @staticmethod
    def completed_last(ordered: Sequence[Dict[str, Any]], by_id: Mapping[str, Tuple[Mapping[str, Any], Optional[int]]]) -> List[Dict[str, Any]]:
        """A completed goal after every open one, whether or not the open one is blocked. The hub puts a blocked item after the unblocked before it reads the rank
        Quest folds ``completed`` into, so on its own a blocked open goal would sort after a completed one; the order within each group is the hub's."""
        done = [placed for placed in ordered if by_id[placed["id"]][0].get("completed")]
        return [placed for placed in ordered if placed not in done] + done

    @staticmethod
    def explained(placed: Mapping[str, Any], item: Mapping[str, Any], place: Optional[int], person: str) -> Dict[str, Any]:
        """The hub's place with Quest's reasons: the hub's ``ranked`` is the place on Quest's ladder, so it is replaced by what Quest ranked by, and the
        goal's horizon is the ``due`` of its milestone when it has no deadline."""
        parts = [p for p in str(placed["reason"]).split("; ") if not p.startswith("ranked ")]
        rank = own_rank(item, person)
        if rank is not None:
            parts.append(f"ranked {rank}")
        for field, text in (("completed", "it is completed"), ("horizon", f"horizon {item.get('horizon')}")):
            if item.get(field) and (field != "horizon" or item["horizon"] in HORIZONS):
                parts.append(text)
        if item.get("ai_handling") == "rep":
            parts.append("its AI rep handles it")
        metric = dict(placed["metric"], due=placed["metric"]["due"] or str(item.get("horizon") or ""))
        return dict(placed, reason="; ".join(parts) or "no rule applies, kept in the order given", metric=metric)
