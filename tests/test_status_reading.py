"""Offline tests: the quest's key measure is read live through the top metric's declared source for a question about where the quest stands."""
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import status_reading

TABLES = {
    "solutions": {"top": {"name": "Plan", "solution_ids": ["child"], "authoritative_metric_id": "m_top"}, "child": {"name": "Step"}},
    "metrics": {"m_child": {"name": "Child measure", "solution_ids": ["child"], "source": "other"},
                "m_top": {"name": "Paid subscribers", "solution_ids": ["top"], "source": "paid"}},
}
READING = {"value": 0.0, "as_of": "2026-11-05", "source": "Quest revenue analytics", "detail": {"paid": 0, "trialing": 0, "mrr": 0}}


class Registry:
    def __init__(self, reading=READING, tables=TABLES, has_tool=True):
        self.calls, self.reading, self.tables, self.has_tool = [], reading, tables, has_tool

    def get(self, name):
        return object() if name != "read_metric_source" or self.has_tool else None

    def invoke(self, name, args, ctx):
        self.calls.append((name, args))
        if name == "read_spec_records":
            return SimpleNamespace(ok=True, data={"records": self.tables, "time_zone": ""}, text="")
        return SimpleNamespace(ok=self.reading is not None, data=self.reading, text="could not run")


def test_the_top_solutions_metric_that_names_a_source_is_the_quests_key_measure():
    assert status_reading.top_solution_ids(TABLES) == ["top"]
    assert status_reading.top_metric_source(TABLES) == ("Paid subscribers", "paid")
    assert status_reading.top_metric_source({"solutions": {"a": {"name": "x"}}, "metrics": {"m": {"name": "n", "solution_ids": ["a"]}}}) == ("", "")


def test_the_reading_enters_the_turn_as_one_read_with_its_value_date_label_and_detail():
    registry = Registry()
    got = status_reading.observation(registry, "q1")
    assert got["locator"] == "tool:read_metric_source"
    assert "Paid subscribers: 0 (as of 2026-11-05, from Quest revenue analytics)" in got["text"] and '"mrr": 0' in got["text"]
    assert [c for c in registry.calls if c[0] == "read_metric_source"] == [("read_metric_source", {"name": "paid"})]


def test_nothing_is_added_when_there_is_no_source_no_tool_or_no_reading():
    assert status_reading.observation(Registry(has_tool=False), "q1") is None
    assert status_reading.observation(Registry(reading=None), "q1") is None
    assert status_reading.observation(Registry(tables={"solutions": {"a": {}}, "metrics": {}}), "q1") is None
    assert status_reading.observation(None, "q1") is None
