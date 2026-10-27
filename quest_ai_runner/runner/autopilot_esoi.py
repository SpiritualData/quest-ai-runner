"""The ESOI readiness block of an autopilot brief: which Solution this run iterates, picked by code.

The standing rule says every run improves one Solution of the quest's specification and reports its Metric before and
after. Left to the model, three things went wrong on live quests: a quest whose specification held only the derived plan
and a few legacy strategies (no Metric, no milestone) gave the run nothing to measure; the run had to read the whole
specification to choose a Solution, and picked the same one or none; and nothing refreshed the specification before the
run. This module does the logistics so the model only does the judgment:

  * ``esoi_readiness`` reads the specification (``get_spec``), asks the server to write what is missing once
    (``refresh_spec``, reason ``autopilot``, hash-gated so it never rewrites an unchanged specification), and
  * names ONE Solution to iterate, chosen by ``pick_solution`` (a gap first, else the Solution whose Metric was
    measured longest ago, so every Solution is visited in turn), with its next milestone and Metric record.

Best-effort throughout: a client without these calls, a quest without a specification and a failed read all give
``None`` and the pass composes the brief it composed before.
"""
from __future__ import annotations

import ast
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("quest_ai_runner.autopilot")

NEVER = "0000"
_UNMEASURED = ("not yet measured", "not measured", "unmeasured")


def _ids(value: Any) -> List[str]:
    """A link cell as a list: the API sends a list, or its string form."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return _ids(ast.literal_eval(text))
        except (ValueError, SyntaxError):
            pass
    return [part.strip(" -'\"") for part in text.replace("\n", ",").split(",") if part.strip(" -'\"")]


def _measured_at(metric: Dict[str, Any]) -> str:
    """When the metric's record was last written ('' for never), and '' when the record says it was not measured."""
    text = str((metric.get("fields") or {}).get("record") or "").strip()
    if not text or text.lower().startswith(_UNMEASURED):
        return NEVER
    return str(((metric.get("fieldMeta") or {}).get("record") or {}).get("at") or NEVER)


def solutions_of(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The Solutions worth iterating: the derived plan is the quest's own words, not a Solution to improve."""
    return [r for r in records if r.get("type") == "solution"
            and not str(r.get("recordId") or "").startswith("derived:")]


def pick_solution(records: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]]:
    """``(solution, metric or None, why)`` for the Solution this run iterates, or None when there is no Solution.

    A Solution with a milestone comes before one without (a milestone is the next priority to work, and a bare
    strategy line is not one). Within that: a Solution with no Metric record (measuring it is the work), then one whose
    Metric reads 'not yet measured', then the Metric measured longest ago. Ties keep the specification's own order."""
    metrics = {r["recordId"]: r for r in records if r.get("type") == "metric" and r.get("recordId")}
    ranked = []
    for index, sol in enumerate(solutions_of(records)):
        fields = sol.get("fields") or {}
        linked = [metrics[i] for i in _ids(fields.get("metric_ids")) if i in metrics]
        linked += [m for m in metrics.values() if sol.get("recordId") in _ids((m.get("fields") or {}).get("solution_ids"))
                   and m not in linked]
        metric = min(linked, key=_measured_at) if linked else None
        stamp = _measured_at(metric) if metric else ""
        has_milestone = bool(str(fields.get("milestones") or "").strip())
        why = ("it has no Metric yet, so defining and measuring one is the work" if metric is None
               else "its Metric was never measured" if stamp == NEVER
               else f"its Metric was last measured {stamp[:10]}, the longest ago")
        ranked.append(((not has_milestone, metric is not None, stamp, index), sol, metric, why))
    if not ranked:
        return None
    _, sol, metric, why = min(ranked, key=lambda row: row[0])
    return sol, metric, why


def _gaps(records: List[Dict[str, Any]]) -> List[str]:
    sols = solutions_of(records)
    gaps = []
    if not sols:
        gaps.append("no Solution")
    elif not any(r.get("type") == "metric" for r in records):
        gaps.append("no Metric record at all")
    return gaps


def render(spec: Dict[str, Any], picked: Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]],
           refreshed: bool) -> str:
    records = spec.get("records") or []
    lines = ["ESOI READINESS (worked out by the runner from get_spec; the choice below is made, so do not spend the run "
             "choosing)."]
    gaps = _gaps(records)
    if gaps:
        lines.append(f"Gaps in the specification: {', '.join(gaps)}."
                     + (" The runner already asked the server to refresh it." if refreshed else "")
                     + " Filling them is part of this run, but no tool creates a Solution or Metric on its own (the person "
                       "confirms them): write the proposed Solution (approach, next milestone, and a measurable Metric "
                       "with number, source and date) in a quest note, and never over a field the person edited.")
    if picked is None:
        lines.append("There is no Solution to iterate yet. Propose one with its Metric in a quest note (see the gaps), do "
                     "the quest's next goal work, and call record_metric_result with not_applicable and that reason.")
        return "\n".join(lines)
    sol, metric, why = picked
    f = sol.get("fields") or {}
    lines.append(f"Iterate this Solution: {f.get('name') or sol.get('recordId')} ({sol.get('recordId')}), because {why}.")
    milestone = str(f.get("milestones") or "").strip()
    lines.append("Next milestone (work this): " + (milestone.splitlines()[0][:400] if milestone else
                 "none written. Write one as a milestone on the Solution (edit_spec_record, unless the person edited "
                 "it) and as a goal where your brief lets you plan, else propose it in a quest note."))
    if metric:
        mf = metric.get("fields") or {}
        lines.append(f"Metric: {mf.get('name')} ({metric.get('recordId')}). Current record: "
                     f"{str(mf.get('record') or '').strip()[:300]}. Measure with: {str(mf.get('command') or 'not written').strip()[:200]}.")
        lines.append("Re-measure it before acting and after, write the new value into the Metric's record field "
                     "(edit_spec_record, with number, source and date), call record_metric_result with the metric id and "
                     "before/after numbers, and put the 'Solution <name>: metric <name> before ... after ... change ...' "
                     "line in a quest note.")
    return "\n".join(lines)


def esoi_readiness(client: Any, quest_id: str) -> Optional[str]:
    """The readiness block for ``quest_id``, or None when it cannot be built. Never raises."""
    getter = getattr(client, "get_spec", None)
    if not callable(getter):
        return None
    try:
        spec = getter("quest", quest_id)
        refreshed = False
        if not spec or _gaps((spec or {}).get("records") or []):
            refresher = getattr(client, "refresh_spec", None)
            if callable(refresher):
                try:
                    refresher("quest", quest_id, reason="autopilot")
                    refreshed = True
                    spec = getter("quest", quest_id) or spec
                except Exception as e:  # noqa: BLE001 -- the refresh is a help, not a gate
                    log.info("autopilot: spec refresh failed for quest %s: %s", quest_id, e)
        if not spec:
            return None
        return render(spec, pick_solution(spec.get("records") or []), refreshed)
    except Exception as e:  # noqa: BLE001 -- an extra for the brief, never a reason to lose the pass
        log.warning("autopilot: ESOI readiness unavailable for quest %s: %s", quest_id, e)
        return None
