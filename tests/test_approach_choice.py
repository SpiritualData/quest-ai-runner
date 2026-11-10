"""The approach block and its implementation records, with a stub decide (no model call)."""
import json

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.approach_choice import approach_block

PROPOSALS = {"most direct": {"name": "Direct draft", "how": "Write it straight away.", "disadvantage": "Misses edge cases."},
             "reuses the most": {"name": "Reuse the template", "how": "Adapt the existing template.", "disadvantage": "Inherits its limits."},
             "careful specialist": {"name": "Specialist review", "how": "Draft, then check a list.", "disadvantage": "Slower."}}


def decide(prompt):
    if "Which one would best meet" in prompt:
        return json.dumps({"index": 1, "reason": "fits"})
    return json.dumps(next(v for k, v in PROPOSALS.items() if k in prompt))


def test_the_block_names_the_chosen_way_its_weakness_and_the_ways_not_taken():
    text, records = approach_block("make a script", ["short"], decide, solution_id="s1")
    assert text.startswith("Approach chosen for this run: ") and "Its known weakness: " in text and "Considered and not taken: " in text
    assert len(records) == 3 and [r["selected"] for r in records] == [True, False, False]
    assert records[0]["description"] and records[0]["disadvantages"] and records[0]["solution_id"] == "s1"
    assert all(r["name"] in text for r in records)


def test_no_approach_gives_nothing():
    assert approach_block("x", [], lambda prompt: "") == ("", [])
