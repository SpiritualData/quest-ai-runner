"""Offline validation of the judge_live_decisions scorer before any live rung is recorded: the no-call floor, a scripted ideal (answers
each case's own label) and a scripted broken baseline (always the wrong answer) must separate, and a hand-written good output passes.
No model call. Run: .venv/bin/python3 -m pytest evaluation/components/test_judge_live_decisions.py -q -p no:cacheprovider"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

CASES = common.load_cases("judge_live_decisions")


def run(fn):
    from judge_live_decisions import score
    return [score.score_case(fn, case)["ok"] for case in CASES]


def ideal(kind, args):  # noqa: ARG001
    case = next(c for c in CASES if c["args"] is args)
    return dict(case["expect"]["eq"])


def broken(kind, args):  # noqa: ARG001
    case = next(c for c in CASES if c["args"] is args)
    if kind == "reach":
        return {"reach": "world" if case["expect"]["eq"]["reach"] != "world" else "inside", "covered": True}
    return {"chosen": ["q9"] * len(args["items"])}


def test_the_floor_ideal_and_broken_baseline_separate():
    floor = common.load_impl("no_call_floor", "judge_live_decisions")(common.CallMeter())
    assert 0 < sum(run(floor)) < len(CASES)       # right where the fallback is right, wrong where the call must help
    assert all(run(ideal)) and not any(run(broken))


def test_the_floor_is_right_on_the_cases_it_should_be_and_only_those():
    floor = common.load_impl("no_call_floor", "judge_live_decisions")(common.CallMeter())
    right = {c["id"] for c, ok in zip(CASES, run(floor)) if ok}
    assert right == {"JLD-04", "JLD-05", "JLD-07", "JLD-11", "JLD-12"}


def test_a_good_hand_written_answer_passes_and_a_near_miss_fails():
    from judge_live_decisions import score
    case = next(c for c in CASES if c["id"] == "JLD-08")
    assert score.score_case(lambda kind, args: {"reach": "outside", "covered": True}, case)["ok"]
    assert not score.score_case(lambda kind, args: {"reach": "outside", "covered": False}, case)["ok"]
