"""Quest as the records location of ESOI Application: the specification of a quest, read and edited through the spec endpoints of Quest.

Role: QAR's arm of the hub's ``RecordsSource`` port (``read(subject)``, ``apply(edit)``), so the hub's edit logic works on a quest's own
problems, ideals, solutions, implementations, metrics and milestones with no copy of them on disk. Every call goes through the Quest client
(or an object with the same three methods: ``get_spec``, ``create_spec_record``, ``edit_spec_record``), so Quest's own rules hold unchanged: the
caller's access, the quest's autopilot switch, a field a person edited being theirs (FIELD_LOCKED), a revision that moved (REVISION_CONFLICT),
and an edit of a row derived from the quest's own fields becoming an ask for the owner (HTTP 202). The three ways a write can end are
reported as the port's three statuses: ``applied``, ``proposed`` (held until a person says yes, never reported as done) and ``refused``
(the reason in plain words).

An addition is ensure-one: a record of the same table whose key column (the name, or the problem sentence) matches, ignoring case and spacing,
and which sits under the same solution for an implementation, is the record, so a repeat creates nothing.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Mapping, Optional

TABLES = {"problem": "problems", "ideal": "ideals", "solution": "solutions", "implementation": "implementations", "metric": "metrics"}
RECORD_TYPES = {table: record_type for record_type, table in TABLES.items()}
KEY_COLUMN = {"problems": "problem", "ideals": "name", "solutions": "name", "implementations": "name", "metrics": "name"}
COLUMNS = {
    "problems": ("problem", "who_has_it", "why_it_matters", "why_hard", "existing_attempts", "ideal_ids"),
    "ideals": ("name", "principle", "state_description", "hints_of_measurement", "problem_ids"),
    "solutions": ("name", "description", "purpose", "approach", "out_of_scope", "milestones", "knowledge", "measures_of_success", "parameters", "inputs",
                  "outputs", "effects", "usage_notes", "problem_ids", "ideal_ids", "interface_name", "selected", "authoritative_metric_id"),
    "implementations": ("solution_id", "filepath", "name", "description", "advantages", "disadvantages", "uses_solution_ids", "ideal_ids"),
    "metrics": ("name", "purpose", "record", "disadvantages", "cascading_conditionals", "solution_ids", "command"),
}
LIST_COLUMNS = {"ideal_ids", "problem_ids", "solution_ids", "uses_solution_ids"}


def same(a: Any, b: Any) -> bool:
    """Two keys name the same record when they match ignoring case and spacing."""
    return re.sub(r"\s+", " ", str(a or "")).strip().casefold() == re.sub(r"\s+", " ", str(b or "")).strip().casefold()


def as_value(column: str, value: Any) -> Any:
    """A link column is a list on the wire; a value given as ids joined by ``;`` or ``,`` becomes that list."""
    if column in LIST_COLUMNS and not isinstance(value, (list, tuple)):
        return [part.strip() for part in re.split(r"[;,]", str(value)) if part.strip()]
    return value


def said_plainly(answer: Mapping[str, Any]) -> str:
    """Why Quest did not make a write, in words that never read as success."""
    if answer.get("outcomeUnknown"):
        return "Quest did not answer, so the change may or may not have been made; read the specification again before trying again"
    code = str(answer.get("code") or "")
    if code == "FIELD_LOCKED":
        return "a person edited " + (", ".join(answer.get("lockedFields") or []) or "that field") + ", so it is theirs and is not changed"
    if code == "REVISION_CONFLICT":
        return "the record changed since it was read; read it again and decide whether the edit still applies"
    if answer.get("status") == 403 or code == "FORBIDDEN":
        return "not permitted: " + str(answer.get("message") or "you may not change this specification")
    return str(answer.get("message") or "Quest refused the change") + (" (" + code + ")" if code else "")


def outcome(answer: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The port's answer for a write that did not simply land, or None when it landed."""
    if answer.get("pending"):
        return {"status": "proposed", "decision": answer.get("decisionId"), "reason": "it is now an ask for the owner, who has to approve it"}
    if answer.get("refused"):
        return {"status": "refused", "reason": said_plainly(answer)}
    return None


class QuestSpecRecords:
    """The records of one quest (``subject_type`` ``quest``) or intent, kept where Quest keeps them. ``read`` returns ``{table: {record id:
    {column: value}}}``; ``apply`` carries out one normalized edit of the hub's ``RecordFieldEdit``."""

    def __init__(self, client: Any, subject_type: str = "quest"):
        self.client = client
        self.subject_type = subject_type
        self.seen: Dict[str, Mapping[str, Any]] = {}

    def read(self, subject: str) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
        spec = self.client.get_spec(self.subject_type, subject)
        if spec is None:
            raise RuntimeError("the specification could not be read just now (" + str(getattr(self.client, "last_error", "") or "no answer") + "); it is not known to be empty")
        tables: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.seen = {}
        for record in spec.get("records") or []:
            table = TABLES.get(str(record.get("type")))
            if table is None:
                continue
            fields = record.get("fields") or {}
            tables.setdefault(table, {})[str(record["recordId"])] = {column: fields.get(column, "") for column in COLUMNS[table]}
            self.seen[str(record["recordId"])] = record
        return tables

    def apply(self, edit: Mapping[str, Any]) -> Mapping[str, Any]:
        subject = str(edit.get("subject") or "")
        tables = self.read(subject)     # fresh: the revision an edit is made on is the one just read
        if edit["op"] == "add":
            return self.add(subject, tables, str(edit["table"]), str(edit["key"]), dict(edit["fields"]))
        if edit["op"] == "milestone":
            return self.milestone(str(edit["target"]), str(edit["key"]), dict(edit["fields"]))
        record = self.seen[str(edit["target"])]
        field = str(edit["field"])
        before = (record.get("fields") or {}).get(field, "")
        answer = self.client.edit_spec_record(str(edit["target"]), {field: as_value(field, edit["value"])}, int(record.get("revision") or 1))
        return outcome(answer) or {"status": "applied", "before": before}

    def add(self, subject: str, tables: Mapping[str, Mapping[str, Mapping[str, Any]]], table: str, key: str, fields: Dict[str, Any]) -> Mapping[str, Any]:
        column = KEY_COLUMN[table]
        for record_id, row in tables.get(table, {}).items():
            if same(row.get(column), key) and (table != "implementations" or not fields.get("solution_id") or row.get("solution_id") == fields["solution_id"]):
                return {"status": "unchanged", "target": record_id}
        written = {name: as_value(name, value) for name, value in fields.items()}
        written.setdefault(column, key)
        answer = self.client.create_spec_record(self.subject_type, subject, RECORD_TYPES[table], written)
        return outcome(answer) or {"status": "applied", "target": answer.get("recordId") or key}

    def milestone(self, target: str, sentence: str, fields: Dict[str, Any]) -> Mapping[str, Any]:
        """One more milestone on a solution's ladder, once: the whole structured list goes back with the new entry last, so the entries and the
        times they were met are kept as Quest has them."""
        record = self.seen[target]
        entries: List[Dict[str, Any]] = [dict(entry) for entry in record.get("milestoneEntries") or []]
        if any(same(entry.get("sentence"), sentence) for entry in entries):
            return {"status": "unchanged"}
        entries.append({"id": "m" + hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:10], "sentence": sentence, "strategy": str(fields.get("strategy") or ""),
                        "metricId": fields.get("metric") or None, "direction": "up", "final": False, "optional": False})
        answer = self.client.edit_spec_record(target, {}, int(record.get("revision") or 1), milestone_entries=entries)
        return outcome(answer) or {"status": "applied", "before": ""}
