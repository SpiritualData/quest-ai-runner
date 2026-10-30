"""Offline tests for the prompt_size component (no model call). Run:
.venv/bin/python3 -m pytest evaluation/components/test_prompt_size.py"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401
from prompt_size import fixtures  # noqa: E402
from prompt_size.score import duplicate_blocks, score_case  # noqa: E402

CASES = json.loads((Path(__file__).parent / "prompt_size" / "cases.json").read_text())


def test_duplicate_counter_sees_repeated_context_update_blocks_and_paragraphs():
    block = fixtures.update_bundle(2, 300).as_prompt_block()
    assert duplicate_blocks(block) == (0, 0)
    assert duplicate_blocks("\n\n".join([block] * 16))[0] == 15          # the 2026-10-06 shape
    para = fixtures.prose(1, 400)
    assert duplicate_blocks(para + "\n\n" + para + "\n\nshort\n\nshort") == (0, 1)


def test_scorer_fails_a_prompt_with_duplicated_blocks_even_under_the_ceiling():
    case = dict(id="t", point="context_update_bundle", fixture="healthy")
    block = fixtures.update_bundle(2, 300).as_prompt_block()
    r = score_case(lambda point, inputs: block + "\n\n" + block, case)
    assert not r["ok"] and r["dups"] >= 1 and "duplicated" in r["note"]
    assert score_case(lambda point, inputs: block, case)["ok"]


def test_scorer_fails_a_prompt_over_its_derived_budget_and_reports_headroom():
    from prompt_size import budgets
    budget = budgets.derive("planner")["budget_chars"]
    case = dict(id="t", point="planner", fixture="healthy")
    assert not score_case(lambda point, inputs: "x" * (budget + 1), case)["ok"]
    at_budget = score_case(lambda point, inputs: "x" * budget, case)
    assert at_budget["ok"] and at_budget["headroom"] == 0.0 and at_budget["budget"] == budget
    assert score_case(lambda point, inputs: "x" * (budget // 2), case)["headroom"] == pytest.approx(0.5, abs=0.001)


def test_budgets_come_from_the_window_a_share_of_it_and_the_cost_per_call():
    from prompt_size import budgets
    from quest_ai_runner.core import prompt_budget
    planner = budgets.derive("planner")
    assert planner["window_tokens"] == prompt_budget.context_window_tokens("claude-haiku-5-5")
    assert planner["budget_tokens"] == min(planner["window_bound"], planner["cost_bound"])
    assert planner["window_bound"] == int(planner["window_tokens"] * budgets.INPUTS["planner"]["window_share"])
    price = budgets.price_per_mtok(planner["model"]) / 1_000_000
    assert planner["cost_bound"] == int(budgets.INPUTS["planner"]["usd_cap_per_run"] / planner["calls"] / price)
    assert planner["budget_chars"] == int(planner["budget_tokens"] * prompt_budget.CHARS_PER_TOKEN)
    deep = budgets.derive("deep_prompt")
    assert deep["window_bound"] == planner["window_tokens"] - prompt_budget.WORKER_RESERVED_TOKENS
    assert budgets.derive("autopilot_batch")["budget_tokens"] == int(deep["budget_tokens"] * 0.5)
    top = budgets.tier_models()
    assert top[budgets.cheapest_tier()] == top["fast"] == "claude-haiku-5-5"


def test_a_tighter_spend_cap_lowers_the_budget(monkeypatch):
    from prompt_size import budgets
    before = budgets.derive("planner")["budget_tokens"]
    monkeypatch.setitem(budgets.INPUTS["planner"], "usd_cap_per_run", 0.003)
    assert budgets.derive("planner")["budget_tokens"] < before


def test_incident_fixtures_really_carry_the_duplicated_blocks_the_live_path_must_remove():
    from quest_ai_runner.core.prompt_budget import count_blocks
    deep = fixtures.build_inputs(dict(point="deep_prompt", fixture="incident"))
    assert count_blocks(deep["preamble"] + deep["brief"]) >= 16
    auto = fixtures.build_inputs(dict(point="autopilot_batch", fixture="incident"))
    assert len(auto["adopted_tasks"]) >= 6 and all(count_blocks(t["text"]) == 1 for t in auto["adopted_tasks"])


@pytest.mark.parametrize("case", [c for c in CASES if c["point"] != "planner"], ids=lambda c: c["id"])
def test_live_path_meets_its_budget_with_no_duplicates(case):
    from prompt_size.impls import live
    r = score_case(live.build(common.CallMeter()), case)
    assert r["ok"], r["note"]


def test_live_planner_healthy_fixture_passes():
    from prompt_size.impls import live
    meter = common.CallMeter()
    case = next(c for c in CASES if c["id"] == "PSZ-001")
    assert score_case(live.build(meter), case)["ok"] and meter.calls == 0


@pytest.mark.parametrize("case", [c for c in CASES if c["fixture"] == "production"], ids=lambda c: c["id"])
def test_production_sized_sweep_has_a_real_p95_under_the_budget(case):
    from prompt_size.impls import live
    r = score_case(live.build(common.CallMeter()), case)
    assert r["ok"], r["note"]
    assert r["p95_chars"] < r["chars"] <= r["budget"]
