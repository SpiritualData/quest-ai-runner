"""Offline tests for the judges, overseer_signals, deep_run, deep_review, reply_guards, model_providers,
web_search, card_quality and claude_code_ab components. No model call, no network.
Run: .venv/bin/python3 -m pytest evaluation/components/test_core_adapter_components.py -q -p no:cacheprovider"""
import importlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
import expect  # noqa: E402

# component -> (selected/offline solution, baseline, ids that fail BY DESIGN because they pin a known gap)
OFFLINE = {
    "judges": ("qar_judges", "trust_raw", {"JUD-029"}),
    "deep_run": ("goal_contract", "naive", set()),
    "reply_guards": ("qar_guards", "passthrough", set()),
    "model_providers": ("multi_provider", "single_provider", {"MPR-080", "MPR-101"}),
    "web_search": ("fallback_chain", "serper", set()),
    "deep_review": ("rule_floor", None, {"DRV-037"}),
    "overseer_signals": ("digest_probe", None, set()),
    "card_quality": ("seeded_keyword_store", "seeded_keyword_open", {"CRD-001", "CRD-002", "CRD-004"}),
    "claude_code_ab": ("idf_hint", None, {"CAB-002", "CAB-004"}),
}
LIVE = {"overseer_signals": "live_model", "deep_review": "live_reviewer", "card_quality": "live_updater_hybrid",
        "claude_code_ab": "cold_claude"}


def cases(comp):
    return json.loads((HERE / comp / "cases.json").read_text())


def run(comp, impl, only=None):
    score = importlib.import_module(f"{comp}.score")
    fn = common.load_impl(impl, comp)(common.CallMeter(max_calls=0))   # max_calls=0: any model call aborts
    rows = []
    for c in cases(comp):
        if only and c["id"] not in only:
            continue
        r = score.score_case(fn, c)
        r["id"] = c["id"]
        rows.append(r)
    return rows


def test_expect_checks():
    assert expect.check({"a": 1, "text": "hello world"}, {"eq": {"a": 1}, "contains": ["hello"], "not_contains": ["x"], "max_len": 11}) == []
    assert len(expect.check({"a": 2, "text": "hi"}, {"eq": {"a": 1}, "contains": ["zz"], "min_len": 5})) == 3
    assert expect.check({"w": 0.5}, {"approx": {"w": [0.5, 0.01]}}) == []
    assert expect.check({"w": None}, {"approx": {"w": [0.5, 0.01]}})
    res = expect.score_kind_case(lambda k, a: 1 / 0, {"kind": "k", "args": {}, "expect": {}})
    assert not res["ok"] and "ZeroDivisionError" in res["note"]


@pytest.mark.parametrize("comp", sorted(OFFLINE))
def test_cases_are_well_formed(comp):
    cs = cases(comp)
    assert len({c["id"] for c in cs}) == len(cs) and len(cs) >= 5
    if "kind" in cs[0]:
        known = {c["kind"] for c in cs}
        assert all("expect" in c and "args" in c for c in cs), known


@pytest.mark.parametrize("comp", sorted(OFFLINE))
def test_offline_solution_fails_only_its_documented_gaps(comp):
    impl, _base, gaps = OFFLINE[comp]
    rows = run(comp, impl)
    assert {r["id"] for r in rows if not r["ok"]} == gaps, [r["note"] for r in rows if not r["ok"]]


@pytest.mark.parametrize("comp", [c for c, v in OFFLINE.items() if v[1]])
def test_baseline_never_beats_the_selected_solution(comp):
    impl, base, _ = OFFLINE[comp]
    good, floor = sum(r["ok"] for r in run(comp, impl)), sum(r["ok"] for r in run(comp, base))
    assert floor <= good
    if comp not in ("web_search",):
        assert floor < good, f"{base} should be a real floor for {impl}"


def test_every_new_impl_module_exposes_build_and_live_arms_are_not_run_here():
    for comp in OFFLINE:
        for p in sorted((HERE / comp / "impls").glob("[a-z]*.py")):
            assert hasattr(importlib.import_module(f"{comp}.impls.{p.stem}"), "build"), p
    for comp, impl in LIVE.items():
        assert (HERE / comp / "impls" / f"{impl}.py").is_file()


def test_judges_pin_the_prior_escalation_shedding_gap():
    row = run("judges", "qar_judges", only={"JUD-029"})[0]
    assert not row["ok"] and "outcome: refused" in row["note"]    # tight budget sheds the refusal history first


def test_pricing_family_gap_is_the_opus_spelling():
    rows = {r["id"]: r for r in run("model_providers", "multi_provider", only={"MPR-080", "MPR-101"})}
    assert not rows["MPR-080"]["ok"] and not rows["MPR-101"]["ok"]


def test_deep_review_scorer_splits_false_stops_from_missed_stops():
    import deep_review.score as s
    keep = {"elapsed": 1, "idle": 1, "tail": "", "want_stop": False}
    stop = {"elapsed": 1, "idle": 1, "tail": "", "want_stop": True}
    rows = [s.score_case(lambda e, i, t: {"stop": True}, keep), s.score_case(lambda e, i, t: {"stop": False}, stop)]
    assert [r["ok"] for r in rows] == [False, False]
    assert "false stops 1/1" in s.aggregate(rows) and "missed stops 1/1" in s.aggregate(rows)
    assert not s.score_case(lambda e, i, t: {"stop": None}, stop)["ok"]


def test_overseer_scorer_checks_digest_evidence_and_the_signal():
    import overseer_signals.score as s
    case = cases("overseer_signals")[6]                                  # fix-and-commit, draft only describes
    assert s.score_case(lambda d: {"signal": "escalate_deep"}, case)["ok"]
    assert not s.score_case(lambda d: {"signal": "proceed"}, case)["ok"]
    assert s.score_case(lambda d: {"signal": None}, case)["ok"]
    assert "not measured" in s.aggregate([s.score_case(lambda d: {"signal": None}, case)])
    thin = dict(case, must_show=["NOT IN ANY DIGEST"])
    assert not s.score_case(lambda d: {"signal": None}, thin)["ok"]


def test_card_quality_scorer_rejects_spraying_every_card():
    import card_quality.score as s
    c = {"id": "x", "query": "q", "expect_id": "col_rev_5512", "allowed_extra": []}
    assert s.score_case(lambda q, w: {"view": "col_rev_5512"}, c)["ok"]
    assert not s.score_case(lambda q, w: {"view": "col_rev_5512 col_trip_3300"}, c)["ok"]
    leak = s.score_case(lambda q, w: {"view": "col_trip_3300"}, dict(c, expect_id=None))
    assert not leak["ok"] and leak["leak"]
    assert s.score_case(lambda q, w: {"view": ""}, dict(c, expect_id=None))["ok"]


def test_web_search_per_impl_expectation_and_cache_case():
    rows = {r["id"]: r for r in run("web_search", "serper")}
    assert rows["WEB-016"]["ok"] and rows["WEB-018"]["ok"]            # serper is expected to fail over-case WEB-016
    chain = {r["id"]: r for r in run("web_search", "fallback_chain")}
    assert chain["WEB-016"]["ok"] and chain["WEB-017"]["ok"]


def test_claude_code_ab_targets_are_real_files():
    assert all((common.REPO / c["target"]).is_file() for c in cases("claude_code_ab"))
    assert sum(1 for c in cases("claude_code_ab") if c["wrong_hint"]) == 1


def test_legacy_scripts_are_thin_wrappers():
    for name in ("overseer_signals_eval", "deep_review_eval", "card_quality_eval"):
        text = (HERE.parent / f"{name}.py").read_text()
        assert "components" in text and "run.py" in text and len(text.splitlines()) < 30
