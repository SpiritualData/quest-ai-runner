"""Quest's arm of the hub's Prioritization and the Quest events adapter of Intake Sources. Offline, no model, no network."""
import json
from pathlib import Path

import quest_ai_runner
from quest_ai_runner.the_body.intake_sources.quest_events import OLDEST, QuestEvents, SOURCE
from quest_ai_runner.the_consciousness.prioritization.quest_ordering import QuestOrdering

CASES = json.loads((Path(__file__).resolve().parent.parent / "evaluation" / "components" / "order_quest_work" / "cases.json").read_text())
NOW = "2026-07-12T09:00:00Z"


def ids(answer):
    return [item["id"] for item in answer["ordered"]]


def test_every_quest_case_comes_back_in_the_reviewed_order():
    arm = QuestOrdering()
    assert len(CASES) == 13
    for case in CASES:
        args = case["args"]
        got = ids(arm.prioritize(person=args["person"], candidates=args["candidates"], stated=args.get("stated"), now=args["now"]))
        assert got == case["expect"], case["id"]


def test_every_case_names_the_quest_test_or_rule_it_came_from():
    assert all(case["source"] and case["origin"].startswith("rule: ") for case in CASES)


def test_a_person_own_rank_entry_beats_the_creators_legacy_rank():
    quests = [{"id": "legacy", "kind": "quest", "owner": "p", "rank": 0}, {"id": "mine", "kind": "quest", "owner": "o", "rank": 9, "rank_by_person": {"p": 0}},
              {"id": "later", "kind": "quest", "owner": "o", "rank_by_person": {"p": 1}}]
    assert ids(QuestOrdering().prioritize(person="p", candidates=quests, now=NOW)) == ["legacy", "mine", "later"]


def test_what_the_person_stated_comes_before_quest_rules():
    candidates = [{"id": "a1", "kind": "ask"}, {"id": "g1", "kind": "goal", "horizon": "today"}, {"id": "i1", "kind": "idea"}]
    answer = QuestOrdering().prioritize(candidates=candidates, stated={"order": ["i1"], "deferred": ["a1"], "kind_order": ["goal", "ask"]}, now=NOW)
    assert ids(answer) == ["i1", "g1", "a1"]
    held = QuestOrdering().prioritize(candidates=candidates, stated={"only_kinds": ["ask"], "not_before": {"a1": "2026-07-13T00:00:00Z"}}, now=NOW)
    assert ids(held) == [] and {h["id"] for h in held["held"]} == {"a1", "g1", "i1"}


def test_the_answer_has_the_hubs_shape_and_a_reason_that_names_the_rule():
    answer = QuestOrdering().prioritize(candidates=[{"id": "g1", "kind": "goal", "horizon": "week"}, {"id": "a1", "kind": "ask", "asked_at": "2026-07-01T00:00:00Z"}], now=NOW)
    first, second = answer["ordered"]
    assert (first["id"], first["rank"], second["rank"]) == ("a1", 1, 2) and abs(first["metric"]["weight"] + second["metric"]["weight"] - 1) < 1e-3
    assert "asked 2026-07-01" in first["reason"] and "horizon week" in second["reason"] and second["metric"]["due"] == "week"


def test_quest_selects_its_own_arm_under_the_hubs_prioritization_seat():
    answer = quest_ai_runner.run("prioritization", {"person": "", "candidates": [{"id": "g", "kind": "goal", "horizon": "year"}, {"id": "a", "kind": "ask"}], "now": NOW}, environ={})
    assert ids(answer) == ["a", "g"]


def test_quest_events_are_the_asks_as_events_at_the_time_they_were_made():
    adapter = QuestEvents([{"text": "do x", "author": "ana", "occurred_at": "2026-07-01T00:00:00Z"}, {"text": "no time"}])
    assert adapter.sources()[0]["kind"] == SOURCE
    events = adapter.pull(SOURCE, limit=10)
    assert [e["at"] for e in events] == ["2026-07-01T00:00:00Z", OLDEST] and events[0]["person"] == "ana"
    assert adapter.pull("elsewhere") == [] and len(adapter.pull(SOURCE, limit=1)) == 1
