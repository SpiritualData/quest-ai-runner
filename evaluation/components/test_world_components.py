"""Offline tests for evolve_by_metric, core_ml_tasks, know_the_world, act_in_the_world and think_critically: the case files are well
formed, a scripted ideal (each case's own label replayed) passes every case, the floor and the live arm score as the records say,
and the broken baseline fails. Each scorer is validated here before any row is recorded. No model call, no network.
Run: .venv/bin/python3 -m pytest evaluation/components/test_world_components.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

METER = common.CallMeter(max_calls=0)


def cases(name):
    return json.loads((HERE / name / "cases.json").read_text())


def passed(name, fn):
    score = __import__(f"{name}.score", fromlist=["score_case"])
    return sum(bool(score.score_case(fn, c)["ok"]) for c in cases(name))


def arm(name, spec):
    return common.load_impl(spec, name)(METER)


def test_case_files_have_unique_ids():
    for name in ("evolve_by_metric", "core_ml_tasks", "know_the_world", "act_in_the_world", "think_critically"):
        found = cases(name)
        assert len({c["id"] for c in found}) == len(found) >= 8


def test_evolve_by_metric_ideal_live_floor_and_broken_separate():
    ideal = lambda kind, args: next(c for c in cases("evolve_by_metric") if c["kind"] == kind and c["args"] == args)["expect"]["eq"]  # noqa: E731
    n = len(cases("evolve_by_metric"))
    assert passed("evolve_by_metric", ideal) == n
    live = passed("evolve_by_metric", arm("evolve_by_metric", "structure_library"))
    floor = passed("evolve_by_metric", arm("evolve_by_metric", "keep_current"))
    broken = passed("evolve_by_metric", arm("evolve_by_metric", "evolve_by_metric.broken.newest_row_wins:build"))
    assert live == n and floor < live and broken < live


def test_think_critically_ideal_live_floor_and_broken_separate():
    def ideal(case):
        if not case["has_metric"]:
            return {"rows": [], "proposed": bool(case["changed"]), "response": ""}
        return {"rows": [{"before": case["before"], "after": case["after"], "source": "measured"}], "proposed": False,
                "response": "retry" if case["after"] < case["before"] else ""}
    n = len(cases("think_critically"))
    assert passed("think_critically", ideal) == n
    live = passed("think_critically", arm("think_critically", "deep_runs_only"))
    floor = passed("think_critically", arm("think_critically", "instruction_only"))
    broken = passed("think_critically", arm("think_critically", "think_critically.broken.claims_held:build"))
    assert broken < floor < live < n


def test_think_critically_live_arm_holds_deep_runs_and_nothing_else():
    from think_critically import score
    live = arm("think_critically", "deep_runs_only")
    by_id = {c["id"]: c for c in cases("think_critically")}
    assert score.score_case(live, by_id["TCR-01"])["ok"]
    assert not score.score_case(live, by_id["TCR-03"])["ok"]   # a regression is read, not answered
    assert not score.score_case(live, by_id["TCR-06"])["ok"]   # a chat turn passes through nothing


def test_act_in_the_world_ideal_floor_and_broken_separate():
    n = len(cases("act_in_the_world"))
    live = passed("act_in_the_world", arm("act_in_the_world", "scripted_ideal"))
    floor = passed("act_in_the_world", arm("act_in_the_world", "rules_only"))
    broken = passed("act_in_the_world", arm("act_in_the_world", "act_in_the_world.broken.act_on_everything:build"))
    assert live == n and floor < live and broken < floor


def test_act_in_the_world_ids_point_at_end_to_end_cases():
    known = {c["id"] for c in common.load_cases("end_to_end")}
    assert all(c["source"] in known for c in cases("act_in_the_world"))


def test_know_the_world_ideal_live_floor_and_broken_separate():
    def ideal(case):
        if case["supported"]:
            return {"text": f"{case['claim']} [page]({case['gold']})", "cited": [case["gold"]], "retrieved": [case["gold"]]}
        return {"text": "I could not find a source for this.", "cited": [], "retrieved": []}
    n = len(cases("know_the_world"))
    assert passed("know_the_world", ideal) == n
    live = passed("know_the_world", arm("know_the_world", "web_with_support_check"))
    floor = passed("know_the_world", arm("know_the_world", "memory_only"))
    broken = passed("know_the_world", arm("know_the_world", "know_the_world.broken.cite_first_result:build"))
    assert live == n and floor == 0 and broken < live


def test_know_the_world_rejects_a_citation_the_research_did_not_retrieve():
    from know_the_world import score
    case = cases("know_the_world")[0]
    fabricated = lambda c: {"text": f"{c['claim']} [x](https://made.up/x)", "cited": ["https://made.up/x"], "retrieved": [c["gold"]]}  # noqa: E731
    assert not score.score_case(fabricated, case)["ok"]


def test_core_ml_tasks_ideal_live_floor_and_broken_separate():
    def ideal(case):
        return {"measured": case["expect_measured"], "rate": 1.0 if case["expect_measured"] else None}
    n = len(cases("core_ml_tasks"))
    assert passed("core_ml_tasks", ideal) == n
    live = passed("core_ml_tasks", arm("core_ml_tasks", "labelled_rows"))
    floor = passed("core_ml_tasks", arm("core_ml_tasks", "none_mapped"))
    broken = passed("core_ml_tasks", arm("core_ml_tasks", "core_ml_tasks.broken.record_exists:build"))
    assert broken < floor < live < n
