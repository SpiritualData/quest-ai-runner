"""Offline tests for record_owner_arms: the pass order, the flock wrapper on every command, the API opt-in and budget, the dirty-tree
refusal, and the stop on a failing or zero-call arm. No model call, no subprocess of the real measure.py. Run:
.venv/bin/python3 -m pytest evaluation/components/test_record_owner_arms.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "justified_model_spend"))
import record_owner_arms as rec  # noqa: E402

MANIFEST = rec.load_manifest()
CASES = json.loads((HERE / "justified_model_spend" / "cases.json").read_text())


def test_every_case_arm_is_in_the_manifest_with_a_tier():
    listed = {(a["component"], a["impl"]) for a in MANIFEST["arms"]}
    for case in CASES:
        for side in ("keep", "ablation"):
            assert (case[side]["component"], case[side]["impl"]) in listed, case["id"]


def test_the_order_is_floors_then_paid_arms_then_ablation():
    plan = rec.build_plan(MANIFEST, api=True)
    tiers = [rec.TIER_ORDER.index(a["tier"]) for a in MANIFEST["arms"]]
    assert tiers == sorted(tiers)
    assert plan[-1][0] == "justified_model_spend/spec_registry_ablation"
    assert plan[0][0].endswith("rules_only")


def test_every_command_runs_under_the_heavy_lock_and_records():
    for label, cmd in rec.build_plan(MANIFEST, api=True):
        assert cmd[:3] == ["flock", rec.LOCK, rec.PYTHON] and "--record" in cmd, label


def test_a_paid_arm_refuses_a_zero_call_run():
    for arm in MANIFEST["arms"]:
        cmd = rec.arm_command(arm)
        assert ("--min-calls" in cmd) == (arm["tier"] != "offline"), arm["impl"]


def test_api_arms_need_the_flag_and_fit_the_cap():
    assert not any(a["tier"] == "api" for a in rec.select_arms(MANIFEST))
    assert any(a["tier"] == "api" for a in rec.select_arms(MANIFEST, api=True))
    assert 0 < rec.api_estimate(MANIFEST, api=True) < 0.05
    assert rec.main(["--plan", "--api", "--api-usd-cap", "0.001"]) == 2


def test_a_narrowed_pass_skips_the_ablation_arm():
    assert all("spec_registry_ablation" not in label for label, _ in rec.build_plan(MANIFEST, tiers=["offline"]))
    assert [label for label, _ in rec.build_plan(MANIFEST, only=["judge_live_decisions/live_judges"])] == ["judge_live_decisions/live_judges"]


def test_a_dirty_tree_is_refused_before_anything_runs():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=" M quest_ai_runner/core/x.py\n M evaluation/traces/a.json\n M evaluation/components/results.csv\n M notes.md\n")
    assert rec.main([], run=run) == 3 and len(calls) == 1 and calls[0][0] == "git" and "-uno" in calls[0]


def test_the_pass_stops_at_the_first_failing_arm():
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        return SimpleNamespace(returncode=1 if len(ran) == 2 else 0)
    plan = rec.build_plan(MANIFEST)
    assert rec.run_pass(MANIFEST, plan, api_cap=0.05, run=run, say=lambda s: None) == 1 and len(ran) == 2


def test_the_pass_stops_when_api_spend_passes_the_cap():
    ran = []
    plan = rec.build_plan(MANIFEST, tiers=["api"], api=True)
    code = rec.run_pass(MANIFEST, plan, api_cap=0.01, run=lambda c, **k: (ran.append(c), SimpleNamespace(returncode=0))[1],
                        cost_of=lambda comp, impl: 0.02, say=lambda s: None)
    assert code == 2 and len(ran) == 1


def test_the_production_report_reads_the_persisted_log_and_never_guesses(tmp_path):
    import production
    from quest_ai_runner.usage import CallLog
    path = tmp_path / "calls.jsonl"
    assert production.report(path, "watch-a-run.overseer") == ([f"no recorded production call of watch-a-run.overseer in {path}"], False)
    for seconds in (1.0, 3.0):
        CallLog(str(path)).append("watch-a-run.overseer", 100, 10, 0.0, seconds)
    lines, found = production.report(path, "watch-a-run.overseer")
    assert found and "2 calls" in lines[0] and "avg 2.00 max 3.00" in lines[0]
