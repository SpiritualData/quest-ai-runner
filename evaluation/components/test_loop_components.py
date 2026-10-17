"""Offline tests for feedback_loop, feed_context_sources, receive_input_mid_run and the offline arm of
recipe_fast_path. No model call, no network. Pins each live implementation's score, the known gaps it keeps on
purpose, and that the floor/baseline discriminate (the floors of the invariant components are in ``broken/`` and
are failed by test_components.py).
Run: .venv/bin/python3 -m pytest evaluation/components/test_loop_components.py -q -p no:cacheprovider"""
import importlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

# (component, impl) -> case ids that fail BY DESIGN (a gap the live code has today); everything else must pass
EXPECTED_FAILS = {
    ("feedback_loop", "ledger_guidance"): {"FL-06", "FL-09", "FL-10", "FL-16"},
    ("feed_context_sources", "update_engine"): set(),
    ("receive_input_mid_run", "in_memory"): set(),
    ("recipe_fast_path", "recipe_replay_offline"): set(),
    ("recipe_fast_path", "planner_only_offline"): set(),
}
BASELINES = {"feedback_loop": ("no_memory", "dump_all")}


def run(comp, impl):
    score = importlib.import_module(f"{comp}.score")
    fn = common.load_impl(impl, comp)(common.CallMeter(max_calls=1000))
    rows = []
    for case in json.loads((HERE / comp / "cases.json").read_text()):
        row = score.score_case(fn, case)
        row["id"] = case["id"]
        rows.append(row)
    return rows


@pytest.mark.parametrize("comp,impl", sorted(EXPECTED_FAILS))
def test_live_implementation_fails_only_its_known_gaps(comp, impl):
    failed = {r["id"] for r in run(comp, impl) if not r["ok"]}
    assert failed == EXPECTED_FAILS[(comp, impl)]


@pytest.mark.parametrize("comp", sorted(BASELINES))
def test_floors_and_baselines_discriminate(comp):
    live = {"feedback_loop": "ledger_guidance"}[comp]
    live_passed = sum(r["ok"] for r in run(comp, live))
    for impl in BASELINES[comp]:
        assert sum(r["ok"] for r in run(comp, impl)) < live_passed


def test_offline_recipe_arm_replays_and_saves_calls():
    meter_replay, meter_plain = common.CallMeter(), common.CallMeter()
    score = importlib.import_module("recipe_fast_path.score")
    cases = json.loads((HERE / "recipe_fast_path" / "cases.json").read_text())
    replay = common.load_impl("recipe_replay_offline", "recipe_fast_path")(meter_replay)
    plain = common.load_impl("planner_only_offline", "recipe_fast_path")(meter_plain)
    replayed = sum(bool(score.score_case(replay, c).get("recipe")) for c in cases)
    for c in cases:
        score.score_case(plain, c)
    assert replayed >= 15
    assert meter_replay.calls < meter_plain.calls
