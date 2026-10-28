"""Offline tests for draft_a_metric: every label names its source, the real MetricDraft runs in each arm, a scripted model that follows the
label passes, the floor proposes nothing, the deliberately broken baseline is caught as inventing values on thin evidence, and the scorer
refuses a proposal whose evidence the card does not hold. No model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_draft_a_metric.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from draft_a_metric import arms, score  # noqa: E402

CASES = json.loads((HERE / "draft_a_metric" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def arm(name):
    return common.load_impl(name, "draft_a_metric")(common.CallMeter(max_calls=0))


def test_every_case_names_its_source_and_a_label():
    assert len(BY_ID) == len(CASES) >= 4
    assert {c["kind"] for c in CASES} == {"propose", "thin"}
    assert all(len(c["source"]) > 60 and "propose" in c["label"] for c in CASES)
    assert all(c["card"]["hasNewContext"] for c in CASES if c["kind"] == "propose")


def test_no_case_carries_a_dash_an_address_or_a_real_path():
    blob = json.dumps(CASES, ensure_ascii=False)
    assert "—" not in blob and "–" not in blob and "/home/" not in blob and "@" not in blob


def test_a_scripted_model_that_follows_the_label_passes_through_the_real_draft_turn():
    for case in CASES:
        label = case["label"]
        if label["propose"]:
            decision = {"propose": True, "values": label["values"], "evidence": [{"summary": case["card"]["notesSinceBaseline"][0]["text"]}]}
        else:
            decision = {"propose": False, "reason": "no new notes"}
        got = arms.run_draft(case, arms.ScriptedProvider(decision))
        assert got["model_calls"] == 1
        assert score.score_case(lambda c, got=got: got, case)["ok"], case["id"]


def test_the_floor_proposes_nothing_and_is_right_only_when_the_evidence_is_thin():
    floor = arm("ask_owner")
    rows = {c["id"]: score.score_case(floor, c) for c in CASES}
    assert [i for i, r in rows.items() if r["ok"]] == ["DAM-03", "DAM-04"] and not any(r["invented"] for r in rows.values())


def test_the_broken_baseline_invents_a_value_on_thin_evidence_and_gets_the_values_wrong():
    broken = common.load_impl("draft_a_metric.broken.invent_a_value:build", "draft_a_metric")(common.CallMeter(max_calls=0))
    rows = [score.score_case(broken, c) for c in CASES]
    assert not any(r["ok"] for r in rows) and sum(r["invented"] for r in rows) == 2 and all(r["ungrounded"] for r in rows)


def test_a_proposal_that_cites_what_the_card_does_not_hold_is_not_grounded():
    case = BY_ID["DAM-02"]
    decision = {"propose": True, "values": {"Kilometres": 24}, "evidence": [{"summary": "Won a marathon in Berlin last spring"}]}
    row = score.score_case(lambda c: arms.run_draft(c, arms.ScriptedProvider(decision)), case)
    assert not row["ok"] and row["ungrounded"]


def test_a_second_model_call_is_over_the_cap():
    row = score.score_case(lambda c: dict(status="nothing", values=None, evidence=[], model_calls=2, text=""), BY_ID["DAM-03"])
    assert row["over_cap"] and not row["ok"]


def test_the_scorer_reports_value_and_inventions_apart():
    line = score.aggregate([dict(ok=True, kind="propose", calls=1), dict(ok=False, kind="thin", calls=1, invented=True)])
    assert "1/2" in line and "propose 1/1" in line and "invented values on thin evidence 1" in line
