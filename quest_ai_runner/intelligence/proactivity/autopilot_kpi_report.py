"""KPIs in an autopilot run: the brief section that asks for them and the end-of-run report that sends them to the backend.

A KPI is the human-readable impact of an autopilot (not the ESOI metric, which is the objective function the work is iterated against; a KPI may name
the metric it reports on). Two kinds: ``count`` (what THIS run did, never a running total) and ``percent`` (the current percentage, 0 to 100). Each
value may carry one blocker: one sentence on what held it back. The backend stores them (``app/business/quests/autopilot_kpis.py``) and the
Autopilots sheet shows them.

The run ends its output with a ``KPI REPORT`` block holding one JSON object; ``parse_report`` reads the LAST such block, ``report_run`` posts it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

START = "KPI REPORT"
END = "END KPI REPORT"
_BLOCK = re.compile(rf"^[ \t>*#`-]*{START}[ \t*`:]*\n(.*?)\n[ \t>*#`-]*{END}", re.S | re.M)


def feedback_lines(feedback: Optional[Dict[str, Any]]) -> List[str]:
    """What people said since earlier runs (the sheet's one-press verdict on the last run, the KPI comments), as brief lines; [] when nothing."""
    feedback = feedback or {}
    lines: List[str] = []
    reaction = ((feedback.get("last_run") or {}).get("reaction") or "")
    if reaction == "helpful":
        lines.append("- The person marked your last run Helpful: keep what worked.")
    elif reaction == "not_helpful":
        lines.append("- The person marked your last run Not helpful: change your approach this run, and say in one line what you changed.")
    for c in feedback.get("comments") or []:
        text = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
        if text:
            lines.append(f"- {c.get('author') or 'A team member'} on the {c.get('kpi') or 'KPI'} row: \"{text[:400]}\"")
    if lines:
        lines = ["Feedback from people on this autopilot (act on it this run):"] + lines + [""]
    return lines


def brief_section(kpis: List[Dict[str, Any]], feedback: Optional[Dict[str, Any]] = None) -> str:
    """The part of a run brief that asks the run to report its KPIs. ``kpis`` are the autopilot's stored definitions (may be empty); ``feedback`` is
    what people said on the Autopilots sheet (the last run's verdict, KPI comments)."""
    lines = ["## KPIs", ""] + feedback_lines(feedback)
    if kpis:
        lines.append("This autopilot reports these KPIs. Report each one for THIS run:")
        for k in kpis:
            how = "the number of things done in this run only, never a running total" if k.get("kind") == "count" else "the current percentage, 0 to 100"
            metric = f" It reports on ESOI metric {k['metric_id']}." if k.get("metric_id") else ""
            lines.append(f"- {k['name']} ({k['kind']}: {how}). Counts: {k.get('definition') or ''}{metric}")
    else:
        lines += ["This autopilot has no KPI yet. Define its own in `new_kpis`: usually one, and more only when the work has separate impacts that a single number",
                  "would hide (for example cost, data usage and security are three). Each needs a short `name` (at most 40 characters), a `kind` (`count` or",
                  "`percent`), a `definition` saying what counts toward the number or how it is determined, and `metric_id` when it reports on one ESOI metric of the quest.",
                  "Then report a value for each."]
    lines += ["",
              f"End your output with this block and nothing after it (valid JSON; `blocker` is ONE sentence on what held the KPI back this run, or \"\" when nothing did;",
              "give one blocker only, unless two are fully independent, then name the one that matters most):",
              "", START, '{"values": [{"name": "<KPI name>", "value": 0, "blocker": ""}]' + ('' if kpis else ', "new_kpis": [{"name": "", "kind": "count", "definition": "", "metric_id": ""}]') + "}", END]
    return "\n".join(lines)


def parse_report(text: str) -> Optional[Dict[str, Any]]:
    """The last KPI REPORT block of ``text`` as a dict with a ``values`` list, or None when there is none or it is not valid."""
    blocks = _BLOCK.findall(text or "")
    for raw in reversed(blocks):
        body = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("values"), list):
            return data
    return None


def readable_lines(data: Dict[str, Any]) -> List[str]:
    """The report as lines a person reads in the run's output: '- Replies: 3 (blocked: The inbox login expired.)'."""
    lines = []
    for item in data.get("values") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("kpi_id") or "KPI")
        value = item.get("value")
        blocker = re.sub(r"\s+", " ", str(item.get("blocker") or "")).strip()
        lines.append(f"- {name}: {value}" + (f" (blocked: {blocker})" if blocker else ""))
    return lines


def with_readable_kpis(text: str, output: str) -> str:
    """``text`` (what the person reads) with any raw KPI REPORT block removed and the run's KPIs added as plain lines under 'KPIs this run'.
    ``text`` unchanged when the run reported none."""
    data = parse_report(output) or parse_report(text)
    if data is None:
        return text
    stripped = _BLOCK.sub("", text or "").rstrip()
    lines = readable_lines(data)
    if not lines:
        return stripped
    return (stripped + "\n\n**KPIs this run**\n" + "\n".join(lines)).strip()


def report_run(client: Any, quest_id: str, autopilot_id: str, run_task_id: str, output: str) -> bool:
    """Send the run's KPI report to the backend. True when it was recorded; never raises (a finished run stays finished)."""
    data = parse_report(output)
    if data is None:
        log.info("autopilot %s run %s ended without a KPI REPORT block", autopilot_id, run_task_id)
        return False
    new = [{k: v for k, v in n.items() if k in ("name", "kind", "definition", "metric_id") and v not in (None, "")} for n in data.get("new_kpis") or [] if isinstance(n, dict)]
    values = [{k: v for k, v in item.items() if k in ("kpi_id", "name", "value", "blocker")} for item in data["values"]
              if isinstance(item, dict) and isinstance(item.get("value"), (int, float)) and not isinstance(item.get("value"), bool)]
    if len(values) != len([i for i in data["values"] if isinstance(i, dict)]):
        log.warning("autopilot %s run %s reported a KPI without a number; those were left out", autopilot_id, run_task_id)
    try:
        result = client.post_autopilot_kpi_report(quest_id, autopilot_id, run_task_id, values, new or None)
        if not result:  # a WriteResult is falsy when Quest refused or failed the write without raising
            log.warning("autopilot %s run %s: Quest did not record the KPI report: %s", autopilot_id, run_task_id, getattr(result, "detail", "") or result)
            return False
        body = getattr(result, "data", None) if not isinstance(result, dict) else result
        unknown = (body or {}).get("unknown") if isinstance(body, dict) else None
        if unknown:
            log.warning("autopilot %s run %s reported KPIs the autopilot does not have: %s", autopilot_id, run_task_id, unknown)
            return False
        return True
    except Exception as error:  # noqa: BLE001 -- bookkeeping must not fail a finished run
        log.warning("could not record KPIs for autopilot %s run %s: %s", autopilot_id, run_task_id, error)
        return False
