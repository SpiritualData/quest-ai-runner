"""Offline tests for assist_a_terminal_session: the case file names a source for every label, the current arm (a reference
model at the provider boundary) and the rules floor run the real command code, and the scorer separates them from the
deliberately broken baseline. No model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_assist_a_terminal_session.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from assist_a_terminal_session import arms, score  # noqa: E402

METER = common.CallMeter(max_calls=40)
CASES = json.loads((HERE / "assist_a_terminal_session" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def arm(name):
    return common.load_impl(name, "assist_a_terminal_session")(common.CallMeter(max_calls=40))


def test_every_case_names_its_source_and_ids_are_unique():
    assert len(BY_ID) == len(CASES) >= 16
    assert all(len(c["source"]) > 40 for c in CASES)
    assert {c["kind"] for c in CASES} == {"ack", "classify", "judge", "gate"}


def test_the_reference_arm_passes_the_code_around_the_call_and_shows_the_missing_ack_guard():
    current = arm("fast_model_helpers")
    results = {c["id"]: score.score_case(current, c)["ok"] for c in CASES}
    assert results["AST-ACK-01"] and results["AST-ACK-03"] and results["AST-ACK-05"] and results["AST-CLS-06"]
    assert not results["AST-ACK-07"] and not results["AST-ACK-08"]   # the terminal prints a breaching acknowledgement as it is


def test_no_task_id_means_no_acknowledgement_and_no_model_request():
    case = BY_ID["AST-ACK-03"]
    for name in ("fast_model_helpers", "rules_only"):
        row = score.score_case(arm(name), case)
        assert row["ok"], (name, row)


def test_the_floor_prints_the_queued_line_and_asks_no_model():
    out = arm("rules_only")(BY_ID["AST-ACK-01"])
    assert out["stdout"].startswith("Queued: Send the board") and out["provider_requests"] == 0


def test_the_floor_classifies_by_shape_and_leaves_the_rest_unclassified():
    floor = arm("rules_only")
    assert score.score_case(floor, BY_ID["AST-CLS-05"])["ok"]
    assert floor({"kind": "classify", "ask": {"key": "k", "text": "Thanks for the update"}, "label": {"kind": "context"}})["raw"] is None


def test_the_broken_baseline_fails_what_the_cases_label():
    broken = common.load_impl("assist_a_terminal_session.broken.trust_everything:build", "assist_a_terminal_session")(METER)
    ok = sum(score.score_case(broken, c)["ok"] for c in CASES)
    floor = arm("rules_only")
    assert ok < sum(score.score_case(floor, c)["ok"] for c in CASES) - 5


def test_contract_problems_names_the_breaks_of_the_ack_prompt():
    assert score.contract_problems("I am looking into your request.") == []
    assert score.contract_problems("") == ["empty"] and "dash" in score.contract_problems("a — b")
    assert any("words" in p for p in score.contract_problems("word " * 21))
