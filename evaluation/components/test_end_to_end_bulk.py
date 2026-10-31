"""Offline tests for the bulk-analysis cases of end_to_end (cases_bulk.json: the synthetic longitudinal cases). The real orchestrator
runs each case with the done-evidence feature on, a scripted planner and a deep worker standing on a capped, generated collection.
A worker that reads one capped page fails the scorer. No model call, no network, never the live arm.
Run: .venv/bin/python3 -m pytest evaluation/components/test_end_to_end_bulk.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from end_to_end import drive, score, world  # noqa: E402

CASES = json.loads((HERE / "end_to_end" / "cases_bulk.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def run(case, capped_only=False):
    return drive.run_bulk(case, common.CallMeter(max_calls=60), capped_only=capped_only)


def test_every_bulk_case_passes_through_the_real_orchestrator_with_a_careful_worker():
    results = {c["id"]: score.judge(run(c), c) for c in CASES}
    assert all(r["ok"] for r in results.values()), {k: r["note"] for k, r in results.items() if not r["ok"]}


def test_a_worker_that_reads_one_capped_page_fails_the_coverage_cases():
    for case_id in ("E2E-COV-01", "E2E-COV-06"):
        verdict = score.judge(run(BY_ID[case_id], capped_only=True), BY_ID[case_id])
        assert not verdict["ok"], case_id
    assert "not the true month" in score.judge(run(BY_ID["E2E-COV-06"], capped_only=True), BY_ID["E2E-COV-06"])["note"]


def test_the_honest_stop_reads_six_pages_emits_interim_and_offers_the_rest():
    got = run(BY_ID["E2E-COV-02"])
    assert got["bulk"]["covered"] == 1200 and len(got["interim"]) >= 1
    assert "Coverage: 1,200 of 5,200 entries" in got["deep_output"] and "continue from entries 1,201" in got["deep_output"]


def test_the_simple_count_makes_no_preflight_call_and_no_interim_event():
    got = run(BY_ID["E2E-COV-05"])
    assert got["preflight_calls"] == 0 and got["interim"] == []


def test_an_analysis_makes_exactly_one_preflight_call_and_the_world_serves_a_capped_collection():
    assert run(BY_ID["E2E-COV-01"])["preflight_calls"] == 1
    bulk = world.World(bulk=BY_ID["E2E-COV-01"]["world"]["bulk"]).bulk
    assert bulk.count() == 344 and len(bulk.find(0)) == 200 and len(bulk.find(200)) == 144
    assert world.World(history=[{"role": "user", "text": "hi"}]).transcript() == "User: hi"


def test_every_bulk_case_names_its_source_and_has_no_em_dash():
    assert len(CASES) == 6 and all(len(c["source"]) > 30 for c in CASES)
    assert "—" not in json.dumps(CASES)
