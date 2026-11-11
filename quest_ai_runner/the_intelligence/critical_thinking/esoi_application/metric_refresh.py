"""Refresh a quest's measured values from the sources its metric records name, so a value is kept in ONE place: the metric record.

Role: a flow step of ESOI Application (the metric-source reading). For each metric record of a quest whose ``source`` column names a source, the lane's
``read_metric_source`` command is run (``tools_metric_source.read_source``), and the reading is written into that record's own value field (``record``) as
one LEADING line, ``Current value: <value> (as of <date>, from <source label>)``, with ``measured_through`` set to the reading's ``as_of``. An older such
line is replaced and the rest of the record's text is kept exactly as it is, so what a person wrote is changed only in that one line.

The step is idempotent (the same reading writes nothing), never guesses (a source that cannot be read leaves the record as it is and is reported in
words), never moves a value back in time (a reading older than the record's ``measured_through`` is left alone), and goes through Quest's own edit rules
unchanged (a field a person edited, a record that moved, an edit held for a person: each is reported, never counted as done).

It runs inside an autopilot pass as part of the ESOI readiness step (``autopilot_esoi.esoi_readiness``), once per quest per real pass, and on demand as
``QuestClient.refresh_metric_values(quest_id)`` (``quest-ai-runner quest refresh_metric_values <quest id> --write``).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_body.internal_context.quest.spec_records import outcome as write_outcome
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.tools_metric_source import MetricSourceError, configured_sources, read_source, shown

log = logging.getLogger("quest_ai_runner.metric_refresh")

CURRENT_VALUE = "Current value:"
VALUE_LINE = re.compile(r"^Current value:")


def value_line(value: Any, as_of: str, source: str) -> str:
    """The one line a metric record leads with: ``Current value: <value> (as of <YYYY-MM-DD>, from <source label>)``."""
    return f"{CURRENT_VALUE} {shown(value)} (as of {as_of}, from {source})"


def with_leading_value_line(record: str, line: str) -> str:
    """``record`` led by exactly one ``Current value:`` line, ``line``. Every older such line is dropped (the first one's place is taken by the new
    line, which leads), and every other line is kept as it was, in order."""
    rest = [text for text in str(record or "").split("\n") if not VALUE_LINE.match(text)]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join([line] + rest)


def metric_records(spec: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The metric records of a specification that name a source."""
    return [r for r in (spec.get("records") or []) if r.get("type") == "metric" and str((r.get("fields") or {}).get("source") or "").strip()
            and not r.get("derived")]


def refresh_one(client: Any, record: Mapping[str, Any], read: Callable[[str], Mapping[str, Any]]) -> Dict[str, Any]:
    """The refresh of one metric record: ``{"record_id", "name", "source", "status", ...}`` where status is ``written``, ``unchanged``, ``gap`` (the
    source could not be read), ``stale`` (the reading is older than the record's), ``held`` (waits for a person) or ``refused`` (Quest declined)."""
    fields = record.get("fields") or {}
    source = str(fields.get("source")).strip()
    result: Dict[str, Any] = {"record_id": str(record.get("recordId")), "name": str(fields.get("name") or ""), "source": source}
    try:
        reading = read(source)
    except MetricSourceError as error:
        return {**result, "status": "gap", "reason": str(error)}
    as_of = str(reading["as_of"])
    kept = str(fields.get("measured_through") or "").strip()
    if kept and kept > as_of:
        return {**result, "status": "stale", "reason": f"the source reports {as_of}, older than the {kept} the record holds"}
    before = str(fields.get("record") or "")
    after = with_leading_value_line(before, value_line(reading["value"], as_of, reading["source"]))
    if after == before and kept == as_of:
        return {**result, "status": "unchanged", "line": after.split("\n")[0]}
    changes = {"record": after, "measured_through": as_of}
    answer = client.edit_spec_record(str(record["recordId"]), changes, int(record.get("revision") or 1))
    held = write_outcome(answer)
    if held is not None:
        return {**result, "status": "held" if held["status"] == "proposed" else "refused", "reason": held["reason"]}
    return {**result, "status": "written", "line": after.split("\n")[0], "before": {"record": before, "measured_through": kept},
            "after": {"record": after, "measured_through": as_of}}


def refresh_metric_values(client: Any, quest_id: str, spec: Optional[Mapping[str, Any]] = None,
                          read: Optional[Callable[[str], Mapping[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Refresh every metric record of ``quest_id`` that names a source; one result per record (see ``refresh_one``). ``spec`` is the specification when the
    caller already read it; ``read`` replaces the configured source reader (tests). Raises ``RuntimeError`` when the specification cannot be read, so
    "nothing to refresh" is never confused with "could not look"."""
    if read is None and not configured_sources():
        return []
    spec = spec if spec is not None else client.get_spec("quest", quest_id)
    if spec is None:
        raise RuntimeError(f"the specification of {quest_id} could not be read just now, so no metric value was refreshed")
    reader = read or read_source
    return [refresh_one(client, record, reader) for record in metric_records(spec)]


def written(results: Sequence[Mapping[str, Any]]) -> bool:
    return any(r.get("status") == "written" for r in results)
