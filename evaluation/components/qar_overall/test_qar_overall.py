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


def test_report_leads_with_the_floor_and_names_what_the_headline_leaves_out(fixture_log, monkeypatch):
    monkeypatch.setattr(combine, "submetric_weights", lambda subs: ({s: 1.0 / len(subs) for s in subs},
                                                                    {"QI1": list(subs)}, 0.0))
    monkeypatch.setattr(combine, "settings", lambda config=None: {"ideals": {"QI1": 1.0}, "reasons": {}, "bar": 0.8,
                                                                  "cost_per_passing_turn_usd": 0.05})
    rows = [{"id": "S1", "area": "Half measured", "reading": combine.read(args())},
            {"id": "S2", "area": "Fully measured", "reading": combine.read(args(cheap=[{"arm": "a/x", "share": 1.0}]))}]
    text = combine.report(rows)
    quick = next(line for line in text.splitlines() if line.startswith("QUICK"))
    assert quick.index("FLOOR") < quick.index("measured") < quick.index("headline") < quick.index("ceiling")
    assert "The headline leaves out S1 Half measured (0.250 of its weight 0.500)." in text


# ---------------------------------------------------------------- freshness follows the declared dependencies

@pytest.fixture
def fresh_row_changes(monkeypatch):
    """A row that is otherwise current; ``changes`` is what commits since its sha touched."""
    state = {"files": []}
    monkeypatch.setattr(combine, "cases_now", lambda comp: "c")
    monkeypatch.setattr(combine, "committed_changes", lambda sha: state["files"])
    return state


def test_a_change_to_an_unrelated_component_leaves_the_row_current(fresh_row_changes):
    fresh_row_changes["files"] = ["evaluation/components/narrate_a_turn/score.py", "quest_ai_runner/core/reply_guards.py",
                                  "evaluation/components/test_something.py"]
    assert combine.dependency_patterns("staleness") is not None
    assert combine.current("staleness", row(6, 6, sha="abc1234")) == ""


def test_a_change_to_a_declared_dependency_makes_the_row_stale(fresh_row_changes):
    patterns = combine.dependency_patterns("staleness")
    own = [p for p in patterns if p.startswith("quest_ai_runner/") and "*" not in p][:1] or ["evaluation/components/staleness/score.py"]
    for changed in (own[0], "evaluation/components/staleness/impls/new.py", "evaluation/components/common.py"):
        fresh_row_changes["files"] = [changed]
        assert combine.current("staleness", row(6, 6, sha="abc1234")).startswith("stale"), changed


def test_a_component_that_declares_nothing_keeps_the_conservative_rule(fresh_row_changes, monkeypatch):
    monkeypatch.setattr(combine, "dependency_patterns", lambda comp: None)
    fresh_row_changes["files"] = ["evaluation/components/narrate_a_turn/score.py"]
    assert combine.current("staleness", row(6, 6, sha="abc1234")).startswith("stale")


def test_the_dependency_patterns_name_no_whole_tree_for_a_single_solution_component():
    patterns = combine.dependency_patterns("staleness")
    assert "evaluation/components/staleness/*" in patterns and "quest_ai_runner/*" not in patterns
