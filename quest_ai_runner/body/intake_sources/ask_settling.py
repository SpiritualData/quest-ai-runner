"""Which answered asks are acted on, found by exact decision id.

An answered ask leaves the person's "waiting for your AI" queue when something marks it acted on.
These helpers find the ids with evidence behind them, so every path that acts on an ask clears it:
the run's own result naming it, the run saying it needs no action, a parked action Quest has run,
and (read-only reconcile) a later done task or quest note that names it. Matching is by the exact
``teamdec_...`` id only; nothing here creates a task.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("quest-ai-runner")

_ID = re.compile(r"\bteamdec_[A-Za-z0-9]+\b")
# A run states per id that an ask needs no action: a line "NO ACTION NEEDED: teamdec_a, teamdec_b".
_NO_ACTION_LINE = re.compile(r"no[ _-]action[ _-]needed\s*:?(.*)", re.IGNORECASE)

# Ids whose mark-acted write failed; the next settle (any run) sends them again.
UNSETTLED: Set[str] = set()

# The all-quests read is one request per quest: bound the fan-out and reuse the answer within a pass.
MAX_QUESTS_PER_READ = 150
READ_WORKERS = 4
READ_CACHE_SECONDS = 20.0
read_cache: Dict[int, Tuple[float, Any, Tuple[Dict[str, Dict[str, Any]], bool]]] = {}

NO_ACTION_MARKER = "NO ACTION NEEDED: <ask id>, <ask id>"


def ids_named_in(text: Any) -> List[str]:
    """Every decision id written in ``text``, first seen first, no repeats."""
    return list(dict.fromkeys(_ID.findall(str(text or ""))))


def ids_marked_no_action(text: Any) -> List[str]:
    """The ids a run listed on a "NO ACTION NEEDED:" line (this line and the next, for a wrapped list)."""
    lines = str(text or "").splitlines()
    found: List[str] = []
    for i, line in enumerate(lines):
        m = _NO_ACTION_LINE.search(line)
        if m:
            found.extend(_ID.findall(m.group(1) + " " + (lines[i + 1] if i + 1 < len(lines) else "")))
    return list(dict.fromkeys(found))


def is_executed_parked(row: Dict[str, Any]) -> bool:
    """A parked approval whose prepared action Quest has now run."""
    return bool(row.get("ai_action_parked")) and bool(row.get("executed"))


def mark_failed(result: Any) -> bool:
    """True when a ``mark_decisions_acted`` answer says the write did not take effect (a falsy WriteResult)."""
    return result is not None and not isinstance(result, (dict, list)) and not bool(result)


def _when(value: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _awaits_ai_action(row: Dict[str, Any]) -> bool:
    from quest_ai_runner.body.intake_sources.context_sources_quest import _awaits_ai_action as rule
    return rule(row)


def read_pending(client: Any, quest_id: str = "", reuse: bool = False) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """``({decision_id: row}, verified)`` of the caller's pending answered asks. Read-only.

    ``GET /api/teams/decisions`` only sees the key's own team, so the per-quest route
    (``list_decisions_for_quest``, same rule as the context updates: answered, ``ai_action_status`` pending)
    is the main source: one request for ``quest_id``, else one per quest of ``list_my_quests`` (at most ``MAX_QUESTS_PER_READ``,
    ``READ_WORKERS`` at a time). ``reuse=True`` (callers within one pass) serves an all-quests read from the last
    ``READ_CACHE_SECONDS``; the default always reads fresh.
    ``list_pending_answers`` is merged in as an extra source. ``verified`` is False when no read could be
    made or the quest list came back empty, so "nothing waiting" cannot be trusted.
    """
    if reuse and not quest_id:
        hit = read_cache.get(id(client))
        if hit and hit[1] is client and time.monotonic() - hit[0] < READ_CACHE_SECONDS:
            return {k: dict(v) for k, v in hit[2][0].items()}, hit[2][1]
    found: Dict[str, Dict[str, Any]] = {}
    verified = True

    def add(rows: Any, quest: str = "", filtered: bool = False) -> None:
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("decision_id") or not (filtered or _awaits_ai_action(r)):
                continue
            row = dict(r)
            if quest and not row.get("quest_id"):
                row["quest_id"] = quest
            if quest_id and row.get("quest_id") and row["quest_id"] != quest_id:
                continue
            found.setdefault(str(row["decision_id"]), row)

    per_quest = getattr(client, "list_decisions_for_quest", None)
    if callable(per_quest):
        quests = [quest_id] if quest_id else []
        if not quest_id:
            lister = getattr(client, "list_my_quests", None)
            try:
                mine = (lister() if callable(lister) else []) or []
            except Exception as e:  # noqa: BLE001
                log.warning("could not list quests for pending answered asks (%s)", e)
                mine = []
            quests = [str(q.get("quest_id") or q.get("id")) for q in mine if isinstance(q, dict) and (q.get("quest_id") or q.get("id"))]
            verified = bool(quests)
            if len(quests) > MAX_QUESTS_PER_READ:
                log.warning("%d quests: reading pending asks for the first %d only", len(quests), MAX_QUESTS_PER_READ)
                quests = quests[:MAX_QUESTS_PER_READ]
                verified = False

        def read_one(q: str) -> Tuple[str, Any, bool]:
            try:
                return q, per_quest(q), True
            except Exception as e:  # noqa: BLE001 -- a read failure only means fewer ids this time
                log.warning("could not list decisions for %s (%s)", q, e)
                return q, None, False

        with ThreadPoolExecutor(max_workers=max(1, min(READ_WORKERS, len(quests)))) as pool:
            results = list(pool.map(read_one, quests))
        for q, rows, ok in results:
            if ok:
                add(rows, q)
            else:
                verified = False
    else:
        verified = False
    lister = getattr(client, "list_pending_answers", None)
    if callable(lister):
        try:
            add(lister(), filtered=True)  # the server already filtered these
        except Exception as e:  # noqa: BLE001
            log.warning("could not list pending answered asks (%s)", e)
    if not quest_id:
        read_cache[id(client)] = (time.monotonic(), client, ({k: dict(v) for k, v in found.items()}, verified))
    return found, verified


def pending_ids(client: Any, quest_id: str = "", reuse: bool = False) -> Dict[str, Dict[str, Any]]:
    """``{decision_id: row}`` of the caller's pending answered asks (one quest's when ``quest_id`` is given). Read-only."""
    return read_pending(client, quest_id, reuse)[0]


def reconcile_pending_asks(client: Any, quest_ids: Optional[Sequence[str]] = None,
                           apply: bool = True) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Clear pending answered asks that a LATER done task result or quest note on the same quest names by id.

    Creates no task and writes nothing except ``mark_decisions_acted`` on ids with that evidence
    (nothing at all when ``apply`` is False). Returns ``(cleared_ids, still_pending_rows)``.
    """
    rows = list(pending_ids(client).values())
    if quest_ids:
        rows = [r for r in rows if r.get("quest_id") in set(quest_ids)]
    by_quest: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_quest.setdefault(str(r.get("quest_id") or ""), []).append(r)
    cleared: List[str] = []
    for quest, asks in by_quest.items():
        if not quest:
            continue
        evidence: List[Tuple[Optional[datetime], str]] = []
        try:
            for t in client.list_tasks(goal_id=quest, status="done", without_progress=True) or []:
                evidence.append((_when(t.get("completed_at") or t.get("updated_at")), str(t.get("result") or "")))
            for n in client.list_quest_notes(quest) or []:
                evidence.append((_when(n.get("created_at")), str(n.get("text") or n.get("body") or "")))
        except Exception as e:  # noqa: BLE001 -- no evidence read, nothing cleared on this quest
            log.warning("reconcile: could not read evidence on %s (%s)", quest, e)
            continue
        for ask in asks:
            asked = _when(ask.get("resolved_at"))
            did = str(ask["decision_id"])
            if asked and any(at and at >= asked and did in ids_named_in(text) for at, text in evidence):
                cleared.append(did)
    if cleared and apply:
        try:
            read_cache.pop(id(client), None)  # the queue is about to change
            if mark_failed(client.mark_decisions_acted(cleared)):
                log.warning("reconcile: marking %d ask(s) acted on did not take effect", len(cleared))
                return [], rows
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile: could not mark %d ask(s) acted on (%s)", len(cleared), e)
            return [], rows
    return cleared, [r for r in rows if str(r.get("decision_id")) not in set(cleared)]
