"""The ack-arm scorer: the metric's fixed selection rule and the three-item majority, with a fake judge (no model call)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
from narrate_a_turn import ack_arms  # noqa: E402


def rec(i, ack, ack_s, thought, thought_s):
    return {"id": i, "input": "How is my marathon plan going?", "reply": "Your plan is on track.", "reads": [{"source": "goals"}],
            "model_ack_text": ack, "model_ack_seconds": ack_s, "model_ack_calls": [1],
            "planner_first_thought": {"rationale": thought, "seconds": thought_s}}


def test_selection_rule_gate_then_share_then_cost():
    arms = {"model_ack": dict(passing=5, inside_gate=True, extra_calls_per_turn=1),
            "mind_thought": dict(passing=6, inside_gate=False, extra_calls_per_turn=0),
            "hybrid": dict(passing=5, inside_gate=True, extra_calls_per_turn=1)}
    assert ack_arms.select(arms, 6) in ("model_ack", "hybrid"), "an arm over the gate is never chosen alone"
    arms["mind_thought"]["inside_gate"] = True
    assert ack_arms.select(arms, 6) == "mind_thought", "within one case in six the cheaper arm wins"
    arms["mind_thought"]["passing"] = 2
    assert ack_arms.select(arms, 6) in ("model_ack", "hybrid")
    assert ack_arms.select({"a": dict(passing=1, inside_gate=False, extra_calls_per_turn=0)}, 6) == "none inside the gate"


def test_all_three_items_must_pass_by_majority_and_a_missing_line_fails():
    from hubofhubs.api import extract_json_object  # noqa: F401
    calls = []

    def fake_ask(prompt):
        calls.append(prompt)
        # quote a word that is in the evidence shown so the view counts; pass every item except 'nothing false'
        verdict = "fail" if "NOTHING false" in prompt else "pass"
        return '{"pick": "%s", "quote": "marathon plan"}' % verdict
    records = [rec("A", "I'll look at your marathon plan.", 1.8, "I'll read your goals.", 6.0)]
    got = ack_arms.score(records, fake_ask)
    assert got["arms"]["model_ack"]["rows"][0]["ok"] is False
    assert got["arms"]["model_ack"]["median_seconds"] == 1.8 and got["arms"]["model_ack"]["inside_gate"]
    assert got["arms"]["mind_thought"]["median_seconds"] == 6.0 and not got["arms"]["mind_thought"]["inside_gate"]
    assert ack_arms.judge_line(records[0], "", fake_ask) == {"ok": False, "items": {}, "note": "no line"}
