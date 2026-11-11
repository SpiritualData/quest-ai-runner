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

A quest worked by several characters has one Solution per area and one character per area, so the choice is made per
character, and which Solutions are a character's is a fact of the specification, not of a prompt: the Solution carries an
Implementation record whose ``filepath`` is ``autopilot:<rep ids>`` (``linked_solutions``), because the autopilot run is
one of the ways the Solution is carried out. ``readiness_for`` picks among those only. A character the specification
links to nothing falls back to the line ``SOLUTIONS YOU ITERATE: <record ids>`` in its standing instructions
(``named_solutions``), and without that line the pick is quest-wide, as before. The specification is read once per quest
per pass (``read_readiness``) and rendered once per character.

Best-effort throughout: a client without these calls, a quest without a specification and a failed read all give
``None`` and the pass composes the brief it composed before.
"""
from __future__ import annotations

import ast
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import quest_ai_runner
from hubofhubs.api import ordered_ids

from hubofhubs.api import prompt_spec as hub_prompt_spec

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
            and not r.get("derived")]


SOLUTIONS_LINE = re.compile(r"^[ \t>*#-]*SOLUTIONS YOU ITERATE:(.*)$", re.IGNORECASE | re.MULTILINE)
_RECORD_ID = re.compile(r"\b[A-Za-z]+_[0-9A-Za-z]{6,}\b")


def named_solutions(instructions: Optional[str]) -> Optional[List[str]]:
    """The record ids on a character's ``SOLUTIONS YOU ITERATE:`` line, in the order written: None when the instructions
    carry no such line (the character names nothing, so the pick is quest-wide), ``[]`` when the line names no id (the
    character iterates no Solution, a reviewer for example)."""
    found = SOLUTIONS_LINE.search(str(instructions or ""))
    if found is None:
        return None
    return list(dict.fromkeys(_RECORD_ID.findall(found.group(1))))


AUTOPILOT_PATH = "autopilot:"


def linked_solutions(records: List[Dict[str, Any]], rep_id: Optional[str]) -> List[str]:
    """The record ids of the Solutions the specification links to the character ``rep_id``, in the specification's order: a
    Solution is linked when one of its Implementation records has a ``filepath`` of ``autopilot:<rep ids>`` naming that
    character. ``[]`` when it links none (or no character is given), so the caller falls back to the instructions."""
    rep = str(rep_id or "").strip()
    if not rep:
        return []
    linked = set()
    for record in records:
        fields = record.get("fields") or {}
        path = str(fields.get("filepath") or "").strip()
        if record.get("type") == "implementation" and path.lower().startswith(AUTOPILOT_PATH) and rep in _RECORD_ID.findall(path):
            linked.update(_ids(fields.get("solution_id")))
    return [sol["recordId"] for sol in solutions_of(records) if sol.get("recordId") in linked]


def pick_solution(records: List[Dict[str, Any]], only: Optional[List[str]] = None) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]]:
    """``(solution, metric or None, why)`` for the Solution this run iterates, or None when there is no Solution. ``only``
    narrows the choice to those record ids (the Solutions a character's instructions name).

    The order is the hub's Prioritization (Quest's ordering arm), asked with what each Solution carries: a Solution with a milestone comes before one without
    (a milestone is the next priority to work, and a bare strategy line is not one); within that, a Solution with no Metric record comes first (measuring it is
    the work), then one whose Metric reads 'not yet measured', then the Metric measured longest ago. Ties keep the specification's own order."""
    metrics = {r["recordId"]: r for r in records if r.get("type") == "metric" and r.get("recordId")}
    candidates, found = [], []
    chosen_from = [sol for sol in solutions_of(records) if only is None or sol.get("recordId") in only]
    for index, sol in enumerate(chosen_from):
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
        candidate = {"id": str(index), "kind": "solution-with-milestone" if has_milestone else "solution"}
        if metric is None or stamp == NEVER:
            candidate["rank"] = 0 if metric is None else 1
        else:
            candidate["last_nudged_at"] = stamp
        candidates.append(candidate)
        found.append((sol, metric, why))
    if not candidates:
        return None
    stated = {"kind_order": ["solution-with-milestone", "solution"]}
    answer = quest_ai_runner.run("prioritization", {"candidates": candidates, "subject": "goals", "stated": stated, "now": datetime.now(timezone.utc).isoformat()})
    return found[int(ordered_ids(answer["ordered"])[0])]


def _gaps(records: List[Dict[str, Any]]) -> List[str]:
    sols = solutions_of(records)
    gaps = []
    if not sols:
        gaps.append("no Solution")
    elif not any(r.get("type") == "metric" for r in records):
        gaps.append("no Metric record at all")
    return gaps


NO_SOLUTION_NAMED = ("ESOI READINESS: your instructions name no Solution to iterate (their SOLUTIONS YOU ITERATE line lists none), so "
                     "this run iterates none. Do the job your instructions give, and call record_metric_result with not_applicable "
                     "and that reason unless you measured a Metric yourself.")


def render(spec: Dict[str, Any], picked: Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]],
           refreshed: bool, scope: str = "") -> str:
    records = spec.get("records") or []
    lines = ["ESOI READINESS (worked out by the runner from get_spec; the choice below is made, so do not spend the run "
             "choosing)."]
    if scope:
        lines.append(scope)
    gaps = _gaps(records)
    if gaps:
        lines.append(f"Gaps in the specification: {', '.join(gaps)}."
                     + (" The runner already asked the server to refresh it." if refreshed else "")
                     + " Filling them is part of this run, but no tool creates a Solution or Metric on its own (the person "
                       "confirms them): write the proposed Solution (approach, next milestone, and a measurable Metric "
                       "with number, source and date) in a quest note, and never over a field the person edited. " + hub_prompt_spec.SOLUTION_ORDER_RULE)
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
        lines.append("Re-measure it before acting and after, cheapest cascading stage first and a costlier one only when the "
                     "previous stage's gate is met and the spend is allowed (a stage not run stays empty, with the reason, "
                     "never an estimate), write the new value into the Metric's record field "
                     "(edit_spec_record, with number, source and date), call record_metric_result with the metric id and "
                     "before/after numbers, and put the 'Solution <name>: metric <name> before ... after ... change ...' "
                     "line in a quest note.")
    return "\n".join(lines)


def refreshed_metric_values(client: Any, quest_id: str, spec: Optional[Dict[str, Any]], getter: Any) -> Optional[Dict[str, Any]]:
    """The specification after the quest's metric records that name a source were refreshed from it (``metric_refresh``): the specification read again
    when any record was written, else the one given. Best-effort: a failed refresh never costs the pass its brief."""
    if not spec or not callable(getattr(client, "edit_spec_record", None)):
        return spec
    try:
        from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import metric_refresh
        results = metric_refresh.refresh_metric_values(client, quest_id, spec)
        for result in results:
            if result["status"] not in ("written", "unchanged"):
                log.info("autopilot: metric %s of quest %s not refreshed (%s): %s", result["record_id"], quest_id, result["status"], result.get("reason", ""))
        return (getter("quest", quest_id) or spec) if metric_refresh.written(results) else spec
    except Exception as e:  # noqa: BLE001 -- the refresh is a help for the brief, never a gate
        log.info("autopilot: metric refresh failed for quest %s: %s", quest_id, e)
        return spec


def read_readiness(client: Any, quest_id: str) -> Optional[Tuple[Dict[str, Any], bool]]:
    """``(specification, refreshed)`` for ``quest_id``, read (and refreshed when thin) ONCE per quest per pass, or None when it
    cannot be read. Never raises."""
    getter = getattr(client, "get_spec", None)
    if not callable(getter):
        return None
    try:
        spec = getter("quest", quest_id)
        spec = refreshed_metric_values(client, quest_id, spec, getter)
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
        return (spec, refreshed) if spec else None
    except Exception as e:  # noqa: BLE001 -- an extra for the brief, never a reason to lose the pass
        log.warning("autopilot: ESOI readiness unavailable for quest %s: %s", quest_id, e)
        return None


def readiness_for(read: Optional[Tuple[Dict[str, Any], bool]], instructions: Optional[str] = None,
                  rep_id: Optional[str] = None) -> Optional[str]:
    """The readiness block for ONE character's run, from the specification ``read_readiness`` read: the pick is made among
    the Solutions the specification links to the character ``rep_id`` (``linked_solutions``), else among the ones that
    character's ``instructions`` name, else quest-wide. Never raises."""
    if read is None:
        return None
    spec, refreshed = read
    try:
        records = spec.get("records") or []
        named = named_solutions(instructions)
        linked = linked_solutions(records, rep_id)
        if linked:
            scope = ("Chosen among the Solutions this quest's specification links to you (" + ", ".join(linked) + ", each by an "
                     "Implementation record whose filepath is autopilot:<your rep id>): these are yours on this quest, and the "
                     "other Solutions belong to other runs.")
            if named is not None and set(named) != set(linked):
                scope += (" Your instructions' SOLUTIONS YOU ITERATE line names a different set (" + (", ".join(named) or "none")
                          + "); the specification is the source of truth, so say so in your result and the line gets corrected.")
            return render(spec, pick_solution(records, only=linked), refreshed, scope=scope)
        if named is None:
            return render(spec, pick_solution(records), refreshed)
        if not named:
            return NO_SOLUTION_NAMED
        known = [i for i in named if any(sol.get("recordId") == i for sol in solutions_of(records))]
        if not known:
            return render(spec, pick_solution(records), refreshed,
                          scope="Your instructions name Solutions to iterate (" + ", ".join(named) + ") that are not Solutions of this quest's "
                                "specification, so the choice below is quest-wide. Say so in your result, so the line is corrected.")
        return render(spec, pick_solution(records, only=known), refreshed,
                      scope="Chosen among the Solutions your instructions name (" + ", ".join(known) + "): these are yours on this quest, "
                            "and the other Solutions belong to other runs.")
    except Exception as e:  # noqa: BLE001 -- an extra for the brief, never a reason to lose the run
        log.warning("autopilot: ESOI readiness could not be rendered: %s", e)
        return None


def esoi_readiness(client: Any, quest_id: str, instructions: Optional[str] = None, rep_id: Optional[str] = None) -> Optional[str]:
    """The readiness block for ``quest_id`` (for the character ``rep_id`` whose ``instructions`` are given), or None when it
    cannot be built. Never raises."""
    return readiness_for(read_readiness(client, quest_id), instructions, rep_id)
