"""A quest's measured values live in ONE place, the metric record: the refresh reads each named source and writes the reading as the record's leading
``Current value:`` line plus ``measured_through``, leaving the rest of the text alone, writing nothing on a repeat, and reporting a gap, a stale reading or a
refusal in words. Offline: Quest is a dictionary, the sources are tiny python programs."""
import json
import sys

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import autopilot_esoi as esoi
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import metric_refresh as refresh
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import tools_metric_source as source_tool

PRINTS = 'import json;print(json.dumps({"value": 5, "as_of": "2026-11-06", "source": "Billing report"}))'
LINE = "Current value: 5 (as of 2026-11-06, from Billing report)"


def metric(rid, record, source="paid", through="", revision=3, name="Paid subscribers"):
    return {"type": "metric", "recordId": rid, "revision": revision, "fields": {"name": name, "record": record, "source": source, "measured_through": through}}


class Quest:
    """The three spec calls the refresh uses, over a dictionary."""
    def __init__(self, *records, refuse=None):
        self.records, self.edits, self.refuse = list(records), [], refuse

    def get_spec(self, kind, ident):
        return {"records": self.records}

    def edit_spec_record(self, rid, fields, revision, **_):
        if self.refuse:
            return self.refuse
        self.edits.append((rid, fields, revision))
        record = next(r for r in self.records if r["recordId"] == rid)
        record["fields"].update(fields)
        record["revision"] += 1
        return {"recordId": rid}


def reader(**by_name):
    def read(name):
        value = by_name[name]
        if isinstance(value, Exception):
            raise value
        return value
    return read


READING = {"value": 5, "as_of": "2026-11-06", "source": "Billing report", "detail": {}}


def test_the_reading_becomes_the_leading_line_and_the_rest_of_the_text_is_kept():
    quest = Quest(metric("m1", "Counted from paid invoices.\nTarget is 1000.", through="2026-10-05"))
    [result] = refresh.refresh_metric_values(quest, "q", read=reader(paid=READING))
    assert result["status"] == "written"
    assert quest.edits == [("m1", {"record": LINE + "\nCounted from paid invoices.\nTarget is 1000.", "measured_through": "2026-11-06"}, 3)]
    assert result["before"]["measured_through"] == "2026-10-05" and result["after"]["measured_through"] == "2026-11-06"


def test_an_older_value_line_is_replaced_wherever_it_sits_and_never_doubled():
    old = "Counted from invoices.\nCurrent value: 0 (as of 2026-10-05, from Billing report)\nTarget is 1000."
    quest = Quest(metric("m1", old, through="2026-10-05"))
    refresh.refresh_metric_values(quest, "q", read=reader(paid=READING))
    assert quest.records[0]["fields"]["record"] == LINE + "\nCounted from invoices.\nTarget is 1000."
    assert quest.records[0]["fields"]["record"].count("Current value:") == 1


def test_the_same_reading_twice_writes_once():
    quest = Quest(metric("m1", "Notes.", through=""))
    for _ in range(3):
        results = refresh.refresh_metric_values(quest, "q", read=reader(paid=READING))
    assert len(quest.edits) == 1 and results[0]["status"] == "unchanged"


def test_only_records_naming_a_source_are_touched_and_a_failed_source_changes_nothing():
    quest = Quest(metric("m1", "By hand.", source=""), metric("m2", "Keep me.", source="paid"), metric("m3", "Keep me too.", source="down"),
                  {"type": "solution", "recordId": "s1", "fields": {"name": "A", "source": "paid"}})
    results = refresh.refresh_metric_values(quest, "q", read=reader(paid=READING, down=source_tool.MetricSourceError("the down source failed: boom")))
    assert [(r["record_id"], r["status"]) for r in results] == [("m2", "written"), ("m3", "gap")]
    assert "boom" in results[1]["reason"] and quest.records[2]["fields"]["record"] == "Keep me too."
    assert [e[0] for e in quest.edits] == ["m2"]


def test_a_reading_older_than_the_record_is_left_alone():
    quest = Quest(metric("m1", LINE, through="2026-11-07"))
    [result] = refresh.refresh_metric_values(quest, "q", read=reader(paid=READING))
    assert result["status"] == "stale" and quest.edits == []


def test_a_locked_field_or_a_held_edit_is_reported_never_counted_as_written():
    locked = {"refused": True, "status": 409, "code": "FIELD_LOCKED", "lockedFields": ["record"]}
    [result] = refresh.refresh_metric_values(Quest(metric("m1", "x"), refuse=locked), "q", read=reader(paid=READING))
    assert result["status"] == "refused" and "record" in result["reason"]
    [result] = refresh.refresh_metric_values(Quest(metric("m1", "x"), refuse={"pending": True, "decisionId": "d1"}), "q", read=reader(paid=READING))
    assert result["status"] == "held"


def test_an_unreadable_specification_is_an_error_not_an_empty_refresh():
    class Blind:
        def get_spec(self, *_):
            return None
    try:
        refresh.refresh_metric_values(Blind(), "q", read=reader())
    except RuntimeError as error:
        assert "could not be read" in str(error)
    else:
        raise AssertionError("expected an error")


def test_no_configured_source_means_nothing_is_read(monkeypatch):
    monkeypatch.delenv(source_tool.ENV_VAR, raising=False)
    quest = Quest(metric("m1", "x"))
    assert refresh.refresh_metric_values(quest, "q") == [] and quest.edits == []


def test_the_configured_source_command_is_the_default_reader(monkeypatch):
    monkeypatch.setenv(source_tool.ENV_VAR, json.dumps({"paid": {"command": [sys.executable, "-I", "-c", PRINTS], "timeout_seconds": 20}}))
    quest = Quest(metric("m1", "x"))
    [result] = refresh.refresh_metric_values(quest, "q")
    assert result["status"] == "written" and quest.records[0]["fields"]["record"].startswith(LINE)


def test_the_autopilot_readiness_step_refreshes_before_it_builds_the_brief(monkeypatch):
    monkeypatch.setenv(source_tool.ENV_VAR, json.dumps({"paid": {"command": [sys.executable, "-I", "-c", PRINTS], "timeout_seconds": 20}}))
    solution = {"type": "solution", "recordId": "s1", "fields": {"name": "Grow", "milestones": "M1", "metric_ids": ["m1"]}}
    quest = Quest(solution, metric("m1", "Not yet measured."))
    quest.refresh_spec = lambda *a, **k: {}
    text = esoi.esoi_readiness(quest, "q")
    assert LINE in text and quest.records[1]["fields"]["measured_through"] == "2026-11-06"
