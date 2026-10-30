"""Offline tests of the lean-launch experiment's pre-registered rule and its arms (``deep_runner/launch_experiment``): no model, no
process. Run: .venv/bin/python3 -m pytest evaluation/components/test_deep_launch_experiment.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (puts the repository on the path)
from deep_runner.launch_experiment import decide  # noqa: E402

from quest_ai_runner.core.deep_launch import DeepLaunchSettings  # noqa: E402


def runs(arm, met_flags, tokens_each):
    return [dict(arm=arm, case=f"c{i}", met=m, false_done=0, input_tokens=tokens_each, cache_read_tokens=0,
                 cache_creation_tokens=0, output_tokens=0, total_cost_usd=0.1, wall_seconds=10) for i, m in enumerate(met_flags)]


def test_an_arm_that_keeps_the_met_rate_and_cuts_tokens_enough_replaces_the_default():
    rows = runs("A", [1] * 10 + [0] * 2, 1000) + runs("B", [1] * 9 + [0] * 3, 600)
    got = decide.decide(rows, "B")
    assert got["replaces"] and got["met_within_one_task"] and got["token_drop"] >= 0.3


def test_losing_two_tasks_or_cutting_less_than_thirty_percent_does_not_replace():
    fewer = decide.decide(runs("A", [1] * 10 + [0] * 2, 1000) + runs("B", [1] * 8 + [0] * 4, 300), "B")
    assert not fewer["replaces"] and not fewer["met_within_one_task"]
    dearer = decide.decide(runs("A", [1] * 10 + [0] * 2, 1000) + runs("B", [1] * 10 + [0] * 2, 800), "B")
    assert not dearer["replaces"]


def test_an_unmeasured_arm_never_replaces():
    assert not decide.decide(runs("A", [1, 1], 1000) + runs("B", [0, 0], 1), "B")["replaces"]
    assert not decide.decide(runs("A", [1, 1], 1000) + runs("B", [1], 1), "B")["replaces"]


def test_more_false_done_is_flagged_for_review():
    rows = runs("A", [1, 1, 1], 1000) + runs("B", [1, 1, 1], 500)
    rows[3]["false_done"] = 1
    assert decide.decide(rows, "B")["flag_for_review"]


def test_the_arms_are_valid_launch_settings_and_a_is_today_s_launch():
    assert DeepLaunchSettings(**{k: (tuple(v) if k == "tools" else v) for k, v in decide.ARMS["A"].items()}).is_default()
    for name in ("B", "C"):
        spec = {k: v for k, v in decide.ARMS[name].items() if k != "cards_in_brief"}
        spec["tools"] = tuple(spec["tools"])
        assert not DeepLaunchSettings(**spec).is_default(), name
    assert decide.ARMS["C"]["max_turns"] == 6 and "max_turns" not in decide.ARMS["B"]
