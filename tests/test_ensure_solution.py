"""The deliverable's own solution is created complete (problem, ideal, approach, milestone), field by valid field; a found home is unchanged."""
import json
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_rubric

FULL = {"name": "tutorial scripts", "purpose": "Short spoken scripts that teach one task each.", "approach": "Write one script per task from the product's own steps.",
        "problem": {"what": "New users cannot start without a guide", "who": "New users", "why": "They leave in the first week"},
        "ideal": {"name": "Every new user starts alone", "principle": "A guide for each first task"},
        "milestone": "Six scripts meet every requirement"}


class Edits:
    def __init__(self):
        self.calls, self.tables = [], {}

    def apply(self, change):
        edit = change["edit"]
        self.calls.append(edit)
        self.tables.setdefault(edit["table"], {})[edit["key"]] = edit["fields"]
        return {"applied": True, "created": True, "target": edit["key"]}


def cue():
    return SimpleNamespace(name="Tutorials published", records_text="Milestone: six tutorials", tables={}, quest_id="q", rid="m")


def make(data):
    edits = Edits()
    return edits, milestone_rubric.ensure_solution(cue(), {"name": "tutorial scripts for the next tutorial"}, edits, lambda prompt: json.dumps(data))


def test_the_created_records_carry_every_field_and_link_by_id():
    edits, found = make(FULL)
    assert found["id"] == "Tutorial scripts" and found["created"] is True
    solution = edits.tables["solutions"]["Tutorial scripts"]
    assert solution["approach"].startswith("Write") and solution["milestones"] == FULL["milestone"] and solution["purpose"]
    assert solution["problem_ids"] == ["New users cannot start without a guide"] and solution["ideal_ids"] == ["Every new user starts alone"]
    assert edits.tables["problems"]["New users cannot start without a guide"]["who_has_it"] == "New users"
    assert edits.tables["ideals"]["Every new user starts alone"]["problem_ids"] == solution["problem_ids"]
    assert [c["table"] for c in edits.calls] == ["problems", "ideals", "solutions"]


def test_partial_json_writes_only_valid_fields():
    edits, found = make({"name": "tutorial scripts", "purpose": "tbd", "approach": "The team writes scripts.", "problem": {"what": "<what>"}, "ideal": "x", "milestone": ""})
    assert found["row"] == {"name": "Tutorial scripts"} and list(edits.tables) == ["solutions"]


def test_no_kind_creates_nothing():
    edits, found = make({"purpose": "x"})
    assert found is None and not edits.calls


def test_the_old_kind_key_still_names_the_kind():
    assert make({"kind": "option comparisons", "purpose": "Compares options."})[1]["id"] == "Option comparisons"
