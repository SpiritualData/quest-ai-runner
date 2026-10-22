"""Offline tests of the combined QAR metric over a fixture results log: no model call, no arm is run.
Run:  .venv/bin/python3 -m pytest evaluation/components/qar_overall/test_qar_overall.py -q"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402,F401
from qar_overall import combine, score  # noqa: E402
from qar_overall.broken import latest_row_any_age  # noqa: E402
from qar_overall.impls import recorded_rows  # noqa: E402


def row(passed, cases, sha="fresh", cost="0.0", calls="0", date="2026-10-22"):
    return {"passed": str(passed), "cases": str(cases), "git_sha": sha, "cost_usd": cost, "model_calls": calls,
            "date": date, "cases_sha": "c"}


LOG = {
    "a": {"x": [row(9, 10)]},
    "b": {"y": [row(4, 4, sha="old")]},
    "chat": {"live": [row(5, 10, cost="0.25", calls="40")]},
}


@pytest.fixture
def fixture_log(monkeypatch):
    monkeypatch.setattr(combine, "history", lambda: LOG)
    monkeypatch.setattr(combine, "whole_set", lambda comp: 20 if comp == "chat" else 10 if comp == "a" else 4)
    monkeypatch.setattr(combine, "current", lambda comp, r: "" if r and r["git_sha"] == "fresh" else
                        ("no recorded row" if r is None else "stale, code changed since old"))


def args(**extra):
    base = {"rule": "pass_rate", "what": "fixture",
            "cheap": [{"arm": "a/x", "share": 0.5}, {"arm": "b/y", "share": 0.5}],
            "full": [{"arm": "a/x", "share": 0.5}, {"arm": "planned_one", "share": 0.5, "planned": True, "planned_cases": 10}]}
    base.update(extra)
    return base


def test_a_stale_arm_is_unmeasured_never_a_pass(fixture_log):
    got = combine.read(args())
    cheap = got["tiers"]["cheap"]
    assert cheap["measured"] == 0.5 and cheap["floor"] == 0.45 and cheap["value"] == 0.9
    assert [a["current"] for a in cheap["arms"]] == [True, False]
    full = got["tiers"]["full"]
    assert full["measured"] == 0.5 and full["arms"][1]["planned"] and not full["arms"][1]["current"]


def test_headline_floor_and_ceiling(fixture_log, monkeypatch):
    monkeypatch.setattr(combine, "submetric_weights", lambda subs: ({s: 1.0 / len(subs) for s in subs}, {}, 0.0))
    readings = {"S1": combine.read(args()), "S2": combine.read(args(cheap=[{"arm": "a/x", "share": 1.0}]))}
    c = combine.combine(readings, "cheap")
    assert c["measured_weight"] == 0.75
    assert c["floor"] == pytest.approx(0.675, abs=1e-3)          # 0.5 * 0.45 + 0.5 * 0.9
    assert c["ceiling"] == pytest.approx(0.925, abs=1e-3)        # floor + the unmeasured quarter
    assert c["headline"] == pytest.approx(0.9, abs=1e-3)         # measured weight only, renormalised


def test_pass_per_dollar_scales_down_dear_turns(fixture_log):
    r = LOG["chat"]["live"][0]   # 5 passing turns for 0.25: 0.05 a passing turn, exactly the budget
    assert combine.pass_per_dollar(r) == pytest.approx(0.5)
    dear = dict(r, cost_usd="2.5")   # ten times the budget per passing turn
    assert combine.pass_per_dollar(dear) == pytest.approx(0.05)
    assert combine.pass_per_dollar(dict(r, passed="0")) == 0.0
    assert combine.pass_per_dollar(dict(r, cost_usd="")) == 0.0   # an unknown price is never free


def test_paid_estimates_come_from_recorded_rows(fixture_log):
    got = combine.read({"rule": "pass_per_dollar", "what": "chat",
                        "cheap": [{"arm": "chat/live", "share": 1.0, "paid": True, "cheap_cases": 10}],
                        "full": [{"arm": "chat/live", "share": 1.0, "paid": True}]})
    assert got["tiers"]["cheap"]["arms"][0]["estimate"] == 0.25
    assert got["tiers"]["full"]["arms"][0]["estimate"] == 0.5     # per-case cost times the whole set of twenty
    assert not got["tiers"]["full"]["arms"][0]["current"]          # a sample is not a full-set row


def test_scorer_fails_the_broken_baseline_and_not_the_honest_reading(fixture_log):
    case = {"id": "S1", "args": args()}
    honest = score.score_case(recorded_rows.build(None), case)
    broken = score.score_case(latest_row_any_age.build(None), case)
    assert not honest["ok"] and "only 50% measured" in honest["note"]
    assert not broken["ok"] and "counts b/y as measured" in broken["note"]
    clean = {"id": "S2", "args": args(cheap=[{"arm": "a/x", "share": 1.0}])}
    assert score.score_case(recorded_rows.build(None), clean)["ok"]


def test_weights_follow_the_ideals_and_sum_to_one():
    subs = [c["id"] for c in combine.load_definitions()]
    weights, serving, unserved = combine.submetric_weights(subs)
    assert sum(weights.values()) + unserved == pytest.approx(1.0)
    assert all(serving[i] for i in combine.settings()["ideals"]), "every ideal of QS1 is served by a sub-metric"


def test_definitions_are_valid():
    for case in combine.load_definitions():
        assert case["args"]["rule"] in combine.RULES
        for tier in combine.TIERS:
            specs = case["args"].get(tier) or []
            if specs:
                assert sum(s["share"] for s in specs) == pytest.approx(1.0), (case["id"], tier)
            for s in specs:
                assert s.get("planned") or "/" in s["arm"]
