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


def index_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    """A specification index of the quest's milestones in the shape ``spec-selection`` reads: one ``solution`` entry per milestone (its name as the
    title, its milestone, purpose and measures as the summary, and the names of the metrics that measure it)."""
    entries = []
    for rid, row in solutions_of(tables):
        linked = metrics_of(tables, rid)
        entries.append({"id": rid, "kind": "solution", "title": str(row.get("name") or ""),
                        "summary": " ".join(str(row.get(f) or "") for f in ("milestones", "purpose", "measures_of_success", "outputs")),
                        "metric_title": " ".join(str(m.get("name") or "") for _, m in linked)})
    return {"version": 1, "entries": entries}


def rank(index: Mapping[str, Any], cue: str) -> List[Tuple[float, Mapping[str, Any]]]:
    """``(score, entry)`` for the entries the cue fits well enough, in the quest's own order among near-equals: the best, then every entry within ``CLOSE`` of it."""
    entries = list(index.get("entries") or [])
    docs = [{"name": set(words(e.get("title"))), "metric": set(words(e.get("metric_title"))), "text": set(words(e.get("summary")))} for e in entries]
    wanted = set(words(cue))
    containing = {w: sum(1 for d in docs if any(w in part for part in d.values())) for w in wanted}
    scores = []
    for entry, doc in zip(entries, docs):
        score = 0.0
        for w in wanted:
            if containing.get(w):
                idf = math.log(1 + len(docs) / containing[w])
                score += idf * max(NAME_WEIGHT * (w in doc["name"]), METRIC_WEIGHT * (w in doc["metric"]), TEXT_WEIGHT * (w in doc["text"]))
        scores.append((score, entry))
    best = max((sc for sc, _ in scores), default=0.0)
    if best < MIN_SCORE:
        return []
    return [(sc, e) for sc, e in scores if sc >= CLOSE * best]


def select(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str) -> Optional[Tuple[str, Mapping[str, Any], List[str]]]:
    """``(solution id, its row, ids of the close runners-up)`` for the milestone the cue concerns, chosen by the ``spec-selection`` arm for a quest's records
    (``quest-records-idf``), else None when nothing fits well enough."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.spec_selection import quest_records_idf
    chosen = quest_records_idf(cue, index_of(tables)).ids
    if not chosen:
        return None
    solutions = dict(solutions_of(tables))
    return chosen[0], solutions[chosen[0]], chosen[1:]


def first_sentence(text: str, limit: int = 160) -> str:
    """The start of a recorded value, cut at a sentence end or the limit: enough to say where the measure stands."""
    text = " ".join(text.split())
    cut = re.search(r"[.;](\s|$)", text[:limit])
    return (text[:cut.start()] if cut else text[:limit]).strip()


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
    value = first_sentence(str(metric.get("record") or "")) if metric else ""
    lines = [f"CUE FACTS (found by code from the quest's own records, not by you). The cue concerns the milestone {name!r}, solution {rid}.",
             f"- Milestone: {name} | Done when: {str(row.get('milestones') or '').strip()[:VALUE_CHARS].rstrip('.')} | Current value: {value or 'not recorded yet'}."]
    if metric:
        lines.append(f"- Metric record {metric_ids[0]} ({metric.get('name')}); its full recorded text is in read_spec_records.")
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
