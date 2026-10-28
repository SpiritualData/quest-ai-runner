"""Offline tests for justified_model_spend: the case file names a source and a budget source for every prompt, the ablation
arm reads per-call traces and passes only a prompt that beats its ablation inside its budget, the registry-only arm and
the broken baseline do not make those checks, and the share is weighted by average spend. Synthetic traces; no model
call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_justified_model_spend.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from justified_model_spend import score  # noqa: E402

CASES = json.loads((HERE / "justified_model_spend" / "cases.json").read_text())
CASE = {"id": "T", "spec_id": "assist-a-terminal-session.ack", "case_kinds": ["ack"],
        "keep": {"component": "k", "impl": "live"}, "ablation": {"component": "k", "impl": "rules"},
        "budget": {"cost_usd_per_turn": 0.001, "seconds_per_call": 2, "tokens": None}}


def keep_trace(oks=(True, True), cost=0.0005, seconds=1.0):
    turns = [{"id": f"c{i}", "kind": "ack", "ok": ok, "calls": 1} for i, ok in enumerate(oks)]
    calls = [{"spec_id": CASE["spec_id"], "tokens_in": 50, "tokens_out": 10, "cost_usd": cost, "seconds": seconds,
              "turn": i + 1, "critical_path": True} for i in range(len(oks))]
    return {"label": "eval-trace", "turns": turns, "calls": calls, "summary": {"model": "m"}}


def rules_trace(oks):
    return {"turns": [{"id": f"c{i}", "kind": "ack", "ok": ok, "calls": 0} for i, ok in enumerate(oks)], "calls": []}


@pytest.fixture
def traces(monkeypatch):
    def set_traces(keep, rules):
        monkeypatch.setattr(score, "trace", lambda comp, impl: keep if impl == "live" else rules)
        monkeypatch.setattr(score, "owner_row_current", lambda case: "")
    return set_traces


def test_every_case_names_its_sources_and_a_registered_prompt():
    assert len({c["id"] for c in CASES}) == len(CASES) >= 4
    assert all(c["source"] and c["budget_source"] and c["owner_metric"] for c in CASES)
    assert all(score.registered(c["spec_id"]) for c in CASES)


def test_a_call_that_beats_its_ablation_inside_its_budget_passes(traces):
    traces(keep_trace((True, True)), rules_trace((True, False)))
    got = score.judge(CASE)
    assert got["verdict"] == "pass" and got["stats"]["fire_rate"] == 1.0 and abs(got["avg_spend"] - 0.0005) < 1e-9


def test_a_call_that_only_ties_its_ablation_fails(traces):
    traces(keep_trace((True, True)), rules_trace((True, True)))
    got = score.judge(CASE)
    assert got["verdict"] == "fail" and "does not beat its ablation" in got["reasons"][0]


def test_a_call_over_its_cost_or_time_budget_fails_on_the_maximum(traces):
    traces(keep_trace((True, True), cost=0.002), rules_trace((False, False)))
    assert any("over budget" in r for r in score.judge(CASE)["reasons"])
    traces(keep_trace((True, True), seconds=3.0), rules_trace((False, False)))
    assert any("per call over budget" in r for r in score.judge(CASE)["reasons"])


def test_no_ablation_trace_on_the_same_cases_is_a_failure_not_a_pass(traces):
    traces(keep_trace((True, True)), rules_trace((True,)))
    assert any("no ablation trace" in r for r in score.judge(CASE)["reasons"])


def test_the_registry_only_arm_ignores_the_ablation_and_the_budget(traces):
    traces(keep_trace((True, True), cost=0.5), rules_trace((True, True)))
    registry = common.load_impl("spec_registry", "justified_model_spend")(common.CallMeter())
    ablation = common.load_impl("spec_registry_ablation", "justified_model_spend")(common.CallMeter())
    assert registry(CASE)["verdict"] == "pass" and ablation(CASE)["verdict"] == "fail"


def test_the_broken_baseline_passes_everything_and_the_ablation_arm_does_not():
    broken = common.load_impl("justified_model_spend.broken.count_everything:build", "justified_model_spend")(common.CallMeter())
    rows = [score.score_case(broken, c) for c in CASES]
    assert all(r["ok"] for r in rows)
    real = common.load_impl("spec_registry_ablation", "justified_model_spend")(common.CallMeter())
    assert score.spend_share([score.score_case(real, c) for c in CASES]) < 1.0


def test_the_share_is_weighted_by_average_spend():
    rows = [{"ok": True, "avg_spend": 0.003}, {"ok": False, "avg_spend": 0.001}]
    assert score.spend_share(rows) == 0.75 and score.spend_share([]) == 0.0


def test_unmeasured_lists_registered_prompts_that_have_no_case():
    left = score.unmeasured()
    assert "verify-goal.verdict" in left and "spec-selection.batch-judge" not in left
