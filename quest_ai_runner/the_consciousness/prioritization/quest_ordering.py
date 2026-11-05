"""Quest's arm of the hub's Prioritization: the order Quest puts its own work in.

The hub's ``stated_fields`` arm orders by what a person stated and the fields any data carries. Quest's data has rules of its own, which the hub does not
carry (they were moved here from the hub's code and cases), so this arm competes with it under the hub's metric on the hub's cases plus Quest's:

- a quest list: ranked quests first, ascending (0 is highest), the rest in the order given; a person's own entry in ``rank_by_person`` (person to rank)
  decides, else the quest model's legacy ``rank`` counts only for the quest's ``owner`` (an item with no ``owner`` carries the person's own rank);
- the autopilot's default instructions: open asks first, then goals of the periods running, then the system's own ideas (``DEFAULT_KIND_ORDER``; a kind
  that is not listed sits with the goals), unless the person or the consumer states another ``kind_order``;
- goals: the shorter ``horizon`` first (today, week, month, quarter, year; none last), after the person's rank and before the deadline, then the nearest
  deadline with undated goals last in the order given;
- open asks: the oldest ``asked_at`` first.

What a person stated (``order``, ``deferred``, ``not_before``, ``only_kinds``, ``feedback``) still outranks all of this. The order is Quest's; the shape of the
answer (each item's place, its weighted milestone) is the hub's, taken from its ``as_given`` arm, so a change to the hub's answer reaches this arm unchanged.
"""
import functools
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from esoi import implementation
from hubofhubs.api import AssemblyConfig, Stated, assemble

DEFAULT_KIND_ORDER = ("ask", "goal", "idea")
HORIZONS = ("today", "week", "month", "quarter", "year")
NEVER = datetime.max.replace(tzinfo=timezone.utc)
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
def hub_shape():
    """The hub's own arm that keeps the order given: it gives each item its place and weighted milestone, which Quest does not restate."""
    return assemble(AssemblyConfig(solutions=("prioritization",), selections={"prioritization": "as_given"}))["prioritization"]


def own_rank(item: Mapping[str, Any], person: str) -> Optional[int]:
    """The person's own rank of a quest: their entry in ``rank_by_person``, else the legacy ``rank`` when it is theirs to give (they created the quest)."""
    entry = whole((item.get("rank_by_person") or {}).get(person))
    if entry is not None:
        return entry
    legacy = whole(item.get("rank"))
    if legacy is not None and ("owner" not in item or str(item.get("owner") or "") == person):
        return legacy
    return None


@implementation("prioritization", "quest_ordering")
class QuestOrdering:
    """Orders Quest's quests, asks, goals and ideas by Quest's rules, with what the person stated first."""
    REQUIRES = ()     # standard library plus the hub

    def prioritize(self, person: str = "", request: str = "", implied: Optional[Mapping[str, Any]] = None, candidates: Sequence[Mapping[str, Any]] = (),
                   stated: Any = None, now: str = "") -> Mapping[str, Any]:
        said = Stated.of(stated if stated is not None else implied)
        at = moment(now) or datetime.now(timezone.utc)
        kinds = tuple(said.kind_order) or DEFAULT_KIND_ORDER
        feedback = {str(f.get("about")): str(f.get("direction")) for f in said.feedback}
        present = {str(c.get("id")) for c in candidates}
        held: List[Dict[str, Any]] = []
        kept: List[Tuple[Tuple, Mapping[str, Any], List[str]]] = []
        for index, item in enumerate(candidates):
            ident, kind = str(item.get("id")), str(item.get("kind") or "")
            if said.only_kinds and kind not in said.only_kinds:
                held.append({"id": ident, "reason": f"the person limited the work to {', '.join(said.only_kinds)}"})
                continue
            wait = moment(said.not_before.get(ident))
            if wait is not None and at < wait:
                held.append({"id": ident, "reason": f"the person said not before {said.not_before[ident]}"})
                continue
            key, why = self.key(item, index, person, said, kinds, present, feedback, at)
            kept.append((key, item, why))
        kept.sort(key=lambda row: row[0])
        answer = dict(hub_shape().prioritize(candidates=[row[1] for row in kept], stated=said, now=now))
        answer["ordered"] = [dict(placed, reason="; ".join(row[2]) or "no rule applies, kept in the order given", metric=self.metric(row[1], placed["metric"]))
                             for placed, row in zip(answer["ordered"], kept)]
        answer["held"] = held
        return answer

    @staticmethod
    def metric(item: Mapping[str, Any], shaped: Mapping[str, Any]) -> Dict[str, Any]:
        """The hub's metric form, with a goal's horizon as its due when it has no deadline."""
        return dict(shaped, due=shaped["due"] or str(item.get("horizon") or ""))

    def key(self, item: Mapping[str, Any], index: int, person: str, said: Stated, kinds: Sequence[str], present: set, feedback: Mapping[str, str],
            now: datetime) -> Tuple[Tuple, List[str]]:
        ident, kind = str(item.get("id")), str(item.get("kind") or "")
        why: List[str] = []
        if ident in said.order:
            stance, spot = 0, said.order.index(ident)
            why.append("the person ordered it")
        elif ident in said.deferred:
            stance, spot = 4, 0
            why.append("the person deferred it")
        else:
            spot = 0
            direction = feedback.get(ident) or feedback.get(kind) or ""
            stance = {"sooner": 1, "later": 3}.get(direction, 2)
            if stance != 2:
                why.append(f"the person's feedback implies {direction}")
        sits = kind if kind in kinds else "goal"
        tier = kinds.index(sits) if sits in kinds else len(kinds)
        why.append(f"kind {kind} is {tier + 1} of {len(kinds)} in the order of kinds")
        blocked = any(str(b) in present for b in item.get("blocked_by") or ())
        if blocked:
            why.append("it waits for " + ", ".join(str(b) for b in item["blocked_by"] if str(b) in present))
        rank = own_rank(item, person)
        rank_key = (0, rank) if rank is not None else (1, 0)
        if rank is not None:
            why.append(f"ranked {rank}")
        horizon = str(item.get("horizon") or "")
        shorter = HORIZONS.index(horizon) if horizon in HORIZONS else len(HORIZONS)
        if horizon in HORIZONS:
            why.append(f"horizon {horizon}")
        deadline = moment(item.get("deadline"))
        if deadline is not None:
            why.append(f"due {item['deadline']}" + (" and overdue" if deadline < now else ""))
        asked = moment(item.get("asked_at"))
        if asked is not None:
            why.append(f"asked {item['asked_at']}")
        waiting = len(item.get("waiting") or ())
        if waiting:
            why.append(f"{waiting} waiting")
        nudged = moment(item.get("last_nudged_at")) or EARLIEST
        return (stance, spot, tier, blocked, rank_key, shorter, deadline or NEVER, asked or NEVER, -waiting, nudged, index), why
