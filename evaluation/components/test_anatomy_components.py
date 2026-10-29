"""Offline tests for select_a_rep, hold_the_long_view, act_as_the_body and admit_to_the_mind: the case files are well
formed, the selected arm runs, and each scorer separates a good function from a bad one. No model call, no network.
Run: .venv/bin/python3 -m pytest evaluation/components/test_anatomy_components.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from act_as_the_body import score as body_score  # noqa: E402
from admit_to_the_mind import score as admit_score  # noqa: E402
from hold_the_long_view import score as view_score  # noqa: E402
from select_a_rep import score as rep_score  # noqa: E402

METER = common.CallMeter(max_calls=0)


def cases(name):
    return json.loads((HERE / name / "cases.json").read_text())


def test_case_files_have_unique_ids_and_enough_cases():
    for name in ("select_a_rep", "hold_the_long_view", "act_as_the_body", "admit_to_the_mind"):
        found = cases(name)
        assert len({c["id"] for c in found}) == len(found) >= 9


def test_select_a_rep_scores_the_label_and_the_floor_loses():
    live = common.load_impl("persona_resolver_precedence", "select_a_rep")(METER)
    floor = common.load_impl("first_registered", "select_a_rep")(METER)
    live_ok = sum(rep_score.score_case(live, c)["ok"] for c in cases("select_a_rep"))
    floor_ok = sum(rep_score.score_case(floor, c)["ok"] for c in cases("select_a_rep"))
    assert live_ok > floor_ok


def test_hold_the_long_view_carries_a_decline_and_the_floor_does_not():
    live = common.load_impl("separate_stores", "hold_the_long_view")(METER)
    floor = common.load_impl("no_carry", "hold_the_long_view")(METER)
    case = next(c for c in cases("hold_the_long_view") if c["id"] == "HLV-01")
    assert view_score.score_case(live, case)["ok"]
    assert not view_score.score_case(floor, case)["ok"]


def test_act_as_the_body_does_not_repeat_a_change_and_the_blind_replay_does():
    live = common.load_impl("card_and_reads", "act_as_the_body")(METER)
    blind = common.load_impl("act_as_the_body.broken.blind_replay:build", "act_as_the_body")(METER)
    case = next(c for c in cases("act_as_the_body") if c["id"] == "ATB-04")
    assert body_score.score_case(live, case)["ok"]
    assert not body_score.score_case(blind, case)["ok"]


def test_admit_to_the_mind_counts_an_out_of_scope_item_as_admitted():
    live = common.load_impl("recency_window", "admit_to_the_mind")(METER)
    floor = common.load_impl("admit_nothing", "admit_to_the_mind")(METER)
    by_id = {c["id"]: c for c in cases("admit_to_the_mind")}
    assert admit_score.score_case(live, by_id["ADM-01"])["ok"]
    assert not admit_score.score_case(live, by_id["ADM-03"])["ok"]
    assert not admit_score.score_case(floor, by_id["ADM-01"])["ok"]


def test_hold_the_long_view_reports_every_intent_level_and_each_level_has_cases():
    found = cases("hold_the_long_view")
    for level in view_score.LEVELS:
        assert sum(1 for c in found if c.get("level") == level) >= 1
    live = common.load_impl("separate_stores", "hold_the_long_view")(METER)
    rows = [view_score.score_case(live, c) for c in found]
    line = view_score.aggregate(rows)
    assert "quest" in line and "goal 2/2" in line and "rep 2/2" in line
