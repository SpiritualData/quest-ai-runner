"""Which milestone of a quest a short cue concerns, found by code before the planner plans.

Role: an arm of the Spec Selection idea (choose, from an ESOI specification, the few records a request concerns) for the records a quest keeps
in Quest. A cue such as "get the store release moving" names no record; the quest's solutions (the strategies, each with its milestones and
metric) are scored against it by the words they share, weighted by how rare each word is among them, and the best is handed to the planner as a
block of facts: the solution id, what done means, the metric and its current value, the owners, and the keys the write uses. Nothing here reads the
model's output, and nothing is written. When two solutions are close the one the quest lists first wins: a quest lists its solutions in the order
its owners ranked them, so that is the one the quest itself puts first.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

STOP = frozenset("""a about all also an and any are as at be been but by can could do does for from get go going had has have how i in into is it its let lets
make me more my need new next not now of on one or our out please so some than that the their them then there these they this to up us want was we were what
when which who will with would you your moving move progress forward""".split())
NAME_WEIGHT, METRIC_WEIGHT, TEXT_WEIGHT = 3.0, 2.0, 1.0
MIN_SCORE = 2.0
CLOSE = 0.85           # a runner-up at this share of the best score counts as a tie
VALUE_CHARS = 220
CODE = re.compile(r"^([A-Za-z]{1,3}\d{1,3})\b")
OWNERS = re.compile(r"\bOwners?:\s*([^.;]*)")


def words(text: Any) -> List[str]:
    out = []
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        token = token[:-1] if len(token) > 3 and token.endswith("s") else token
        if token not in STOP and len(token) > 2:
            out.append(token)
    return out


def metrics_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], solution_id: str) -> List[Tuple[str, Mapping[str, Any]]]:
    """The metrics that measure a solution, from the metric records' own ``solution_ids``."""
    return [(mid, m) for mid, m in (tables.get("metrics") or {}).items() if solution_id in (m.get("solution_ids") or [])]


def solutions_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> List[Tuple[str, Mapping[str, Any]]]:
    """The solutions that are milestones of the quest (they state what done means and are measured), in the order the quest lists them."""
    return [(rid, row) for rid, row in (tables.get("solutions") or {}).items()
            if str(row.get("milestones") or "").strip() and (metrics_of(tables, rid) or row.get("measures_of_success"))]


def scored(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str) -> List[Tuple[float, str, Mapping[str, Any]]]:
    solutions = solutions_of(tables)
    docs: List[Dict[str, set]] = []
    for rid, row in solutions:
        linked = [m for _, m in metrics_of(tables, rid)]
        docs.append({"name": set(words(row.get("name"))),
                     "metric": set(w for m in linked for w in words(m.get("name"))),
                     "text": set(w for field in ("milestones", "purpose", "measures_of_success", "outputs") for w in words(row.get(field)))})
    wanted = set(words(cue))
    containing = {w: sum(1 for d in docs if any(w in part for part in d.values())) for w in wanted}
    result = []
    for (rid, row), doc in zip(solutions, docs):
        score = 0.0
        for w in wanted:
            if not containing.get(w):
                continue
            idf = math.log(1 + len(docs) / containing[w])
            score += idf * max(NAME_WEIGHT * (w in doc["name"]), METRIC_WEIGHT * (w in doc["metric"]), TEXT_WEIGHT * (w in doc["text"]))
        result.append((score, rid, row))
    return result


def select(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str) -> Optional[Tuple[str, Mapping[str, Any], List[str]]]:
    """``(solution id, its row, ids of the close runners-up)`` for the milestone the cue concerns, else None when nothing fits well enough."""
    ranked = scored(tables, cue)
    if not ranked:
        return None
    best = max(score for score, _, _ in ranked)
    if best < MIN_SCORE:
        return None
    close = [(rid, row) for score, rid, row in ranked if score >= CLOSE * best]     # in the quest's own order, so the first is the one it lists first
    return close[0][0], close[0][1], [rid for rid, _ in close[1:]]


def fact_block(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str, monday: str = "") -> str:
    """The facts for the planner, or "" when no milestone fits the cue."""
    picked = select(tables, cue)
    if picked is None:
        return ""
    rid, row, ties = picked
    linked = metrics_of(tables, rid)
    metric_ids = [mid for mid, _ in linked]
    metric = linked[0][1] if linked else None
    name = str(row.get("name") or rid)
    code = (CODE.match(name) or [None, ""])[1]
    owners = (OWNERS.search(str(row.get("purpose") or "")) or [None, ""])[1].strip()
    owners = re.sub(r"\s*\(rep_\w+\)", "", owners)
    lines = [f"CUE FACTS (found by code from the quest's own records, not by you). The cue concerns the milestone {name!r}, solution {rid}.",
             f"- Done when: {str(row.get('milestones') or '').strip()[:VALUE_CHARS]}"]
    if metric:
        lines.append(f"- Metric {metric_ids[0]} {metric.get('name')!r}, current value as recorded: {str(metric.get('record') or '').strip()[:VALUE_CHARS]}")
    if owners:
        lines.append(f"- Owners: {owners}")
    if ties:
        lines.append("- Close runners-up (the quest lists this one first): " + ", ".join(f"{t} {str((tables['solutions'].get(t) or {}).get('name') or '')[:50]!r}" for t in ties))
    if code:
        lines.append(f"- Keys: the next step is the goal titled '{code} next step: week of {monday or '<Monday date>'}' (create_or_update_quest_goal, assignee one of the owners, "
                     f"spec_solution_id {rid}"
                     + (f", spec_metric_id {metric_ids[0]}" if metric_ids else "") + f"); a deliverable you write is its own goal titled '{code} <short name>'. "
                     "The tool reports created, updated or already there.")
    lines.append("Use them: do not ask which milestone, and do not ask for facts listed here.")
    return "\n".join(lines)


def facts_for_turn(registry: Any, quest_id: str, cue: str, today: Any = None) -> str:
    """The fact block for a turn pinned to ``quest_id``, read through the registry's ``read_spec_records`` tool; "" when the lane lacks the tool, the records cannot be
    read, or no milestone fits. Never raises: a turn must not fail for want of a hint."""
    import datetime
    try:
        spec = registry.get("read_spec_records") if registry is not None else None
        if spec is None or not quest_id or not str(cue or "").strip():
            return ""
        from quest_ai_runner.the_world.tools.tools import ToolContext
        result = registry.invoke("read_spec_records", {"quest_id": quest_id}, ToolContext(quest_id=quest_id))
        tables = (getattr(result, "data", None) or {}).get("records") if result.ok else None
        if not tables:
            return ""
        day = today or datetime.date.today()
        monday = (day - datetime.timedelta(days=day.weekday())).isoformat()
        return fact_block(tables, cue, monday)
    except Exception:  # noqa: BLE001
        return ""
