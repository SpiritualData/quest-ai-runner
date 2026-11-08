"""The value gate (D x G x L x M) on SYNTHETIC records (invented content): a progress turn passes only with a measured AI increment, and a request that is
itself bookkeeping is never held to it.
Run:  python -m pytest evaluation/components/chat_quality/test_value_gate.py -q"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import value_gate  # noqa: E402

PROGRESS = {"id": "X-1", "ai_doable": [{"kind": "deliverable", "item": "the script for video 1"}], "deliverable_rubric": ["has an opening", "has a call to action"],
            "increment_measure": {"name": "scripts meeting the rubric", "method": "rubric", "lands_on": {"collection": "goals", "title_prefix": "M1 "}}}


def tool_read(name, data, text="done"):
    return {"source": f"tool {name} {{}}", "text": text + "\n" + json.dumps(data)}


def verdict(passing=True, failure_class=None):
    items = [{"item": item, "pass": passing} for item in value_gate.rubric_items(PROGRESS)]
    return {"rubric": [{"item": "replies", "pass": True}] + items, "failure_class": failure_class}


GOOD_KEEP = tool_read("keep_milestone_deliverable", {"state": "created", "goal": {"name": "M1 Videos: script 1"},
                                                     "increment": {"kind": "deliverable", "measure_id": "m-child", "before": 0, "after": 1, "method": "rubric", "changed": True}})
REREAD = tool_read("read_spec_records", {"records": {}})
OWNER_ONLY = [{"kind": "write", "collection": "goals", "method": "update_one", "payload": json.dumps({"$set": {"assigned_to_user_id": "u1", "scheduled_date": "2030-01-04"}})}]


def test_a_bookkeeping_only_progress_turn_fails_the_gate():
    gate = value_gate.evaluate(PROGRESS, [tool_read("create_or_update_quest_goal", {"state": "updated", "goal": {"name": "M1 next step"}})], OWNER_ONLY, verdict())
    assert gate["applies"] and gate["value"] == 0 and gate["D"] == 0 and gate["why"].startswith("D:")


def test_a_measured_kept_deliverable_with_the_metric_read_again_passes():
    gate = value_gate.evaluate(PROGRESS, [GOOD_KEEP, REREAD], [{"kind": "write", "collection": "goals", "method": "insert_one"}], verdict())
    assert gate == dict(gate, value=1, D=1, G=1, L=1, M=1)


def test_each_factor_can_fail_on_its_own():
    assert value_gate.evaluate(PROGRESS, [GOOD_KEEP, REREAD], [], verdict(passing=False))["G"] == 0
    assert value_gate.evaluate(PROGRESS, [GOOD_KEEP, REREAD], [], verdict(failure_class="hallucinated_fact"))["G"] == 0
    elsewhere = tool_read("keep_milestone_deliverable", {"state": "created", "goal": {"name": "Other goal"},
                                                         "increment": {"kind": "deliverable", "measure_id": "m", "before": 0, "after": 1, "method": "rubric"}})
    assert value_gate.evaluate(PROGRESS, [elsewhere, REREAD], [], verdict())["L"] == 0
    assert value_gate.evaluate(PROGRESS, [GOOD_KEEP], [], verdict())["M"] == 0          # the milestone metric was not read again
    claimed = tool_read("keep_milestone_deliverable", {"state": "updated", "goal": {"name": "M1 x"},
                                                       "increment": {"kind": "deliverable", "measure_id": "m", "before": 1, "after": 1, "method": "rubric", "changed": True}})
    assert value_gate.evaluate(PROGRESS, [claimed, REREAD], [], verdict())["M"] == 0
    said = tool_read("keep_milestone_deliverable", {"state": "updated", "goal": {"name": "M1 x"},
                                                    "increment": {"kind": "deliverable", "measure_id": "m", "before": 0, "after": 1, "method": "writer"}})
    assert value_gate.evaluate(PROGRESS, [said, REREAD], [], verdict())["M"] == 0       # the writer's own say-so is no measurement


def test_a_bookkeeping_request_is_not_held_to_the_gate():
    bookkeeping = {"id": "X-2", "rubric": ["the weekly check-in is on the quest"]}
    gate = value_gate.evaluate(bookkeeping, [tool_read("create_or_update_quest_goal", {"state": "created"})], OWNER_ONLY, {"rubric": []})
    assert gate == {"applies": False, "value": None, "why": "not a progress case"}
