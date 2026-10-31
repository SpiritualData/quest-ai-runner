"""Offline tests of the mixed card and specification component: the scorer's rules, the fixture world, and that the cases
fail the broken baseline. No model call; the arm test needs the local embedder and skips without it.
Run:  .venv/bin/python3 -m pytest evaluation/components/context_and_specs/test_context_and_specs.py -q"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402,F401
from context_and_specs import score, world  # noqa: E402
from context_and_specs.broken import keyword_overlap  # noqa: E402
from esoi.spec_search import served_by_solution  # noqa: E402


def fixed(out):
    return lambda task, tags: out


def case(**kw):
    return {"id": "T", "kind": "t", "args": {"task": "x"}, **kw}


def test_scorer_budget_flag_nothing_and_calls():
    crowd = [f"r{i}" for i in range(score.BUDGET)] + ["wanted"]
    assert not score.score_case(fixed({"items": crowd}), case(want=[["wanted"]]))["ok"]          # buried past the budget
    assert score.score_case(fixed({"items": ["wanted"]}), case(want=[["wanted"]]))["ok"]
    assert not score.score_case(fixed({"items": ["p"]}), case(want=[["p"]], no_solution=True))["ok"]
    assert not score.score_case(fixed({"items": ["s"], "no_solution_yet": True}), case(want=[["s"]]))["ok"]   # R1 risk
    assert not score.score_case(fixed({"items": ["s"]}), case(nothing=True))["ok"]
    assert not score.score_case(fixed({"items": ["s"], "calls": 1}), case(want=[["s"]]))["ok"]


def test_live_only_cases_are_left_out_offline():
    ids = [c["id"] for c in score.load_cases()]
    assert "CAS-016" not in ids and len(ids) == 17


def test_fixture_world_has_unsolved_records_and_fixture_tags():
    w = world.World(vector=False)
    served = served_by_solution(w.record_views)
    assert "prob-fixture-reminders-wrong-time-zone" not in served
    assert "fixture-household-meals" not in served
    assert w.index["n"] == len(w.index["entries"]) and w.index["df"]["pantry"] >= 1
    assert set(w.tags.solution_tags) == {"persist-cards", "cache-cards", "detect-stale-cards"}


def test_the_cases_fail_the_broken_baseline():
    search = keyword_overlap.build(None)
    rows = [score.score_case(search, c) for c in score.load_cases()]
    failed = {c["kind"] for c, r in zip(score.load_cases(), rows) if not r["ok"]}
    assert {"unrelated", "unsolved_problem", "spec_only"} <= failed
    assert sum(r["ok"] for r in rows) < len(rows) // 2


def test_unified_arm_flags_no_solution_and_leaves_unrelated_empty():
    pytest.importorskip("fastembed")
    try:
        from context_and_specs.impls import solutions_and_problems_with_links as arm
        search = arm.build(None)
    except RuntimeError as exc:   # the local model is not cached here
        pytest.skip(str(exc))
    by_id = {c["id"]: c for c in score.load_cases()}
    for cid in ("CAS-006", "CAS-007", "CAS-015"):
        assert score.score_case(search, by_id[cid])["ok"], cid
