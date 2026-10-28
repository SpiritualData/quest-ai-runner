"""Offline tests for hand_off_a_goal: every label names its source, the real goal_handoff code runs in each arm, the floor never hands a
goal to the AI, the deliberately broken baseline is caught by its wrong yes verdicts, and a scripted judge walks both case kinds. No model
call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_hand_off_a_goal.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from hand_off_a_goal import arms, score  # noqa: E402

CASES = json.loads((HERE / "hand_off_a_goal" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def arm(name):
    return common.load_impl(name, "hand_off_a_goal")(common.CallMeter(max_calls=0))


def test_every_case_names_its_source_and_a_label():
    assert len(BY_ID) == len(CASES) >= 11
    assert {c["kind"] for c in CASES} == {"assignee", "doability"}
    for case in CASES:
        assert len(case["source"]) > 60 and "ai_can_do" in case["label"]
        assert case["kind"] == "doability" or case["label"]["assignee"] in {m["user_id"] for m in case["members"]}
    assert sum(1 for c in CASES if c["kind"] == "doability" and c["label"]["ai_can_do"]) >= 4
    assert sum(1 for c in CASES if c["kind"] == "doability" and not c["label"]["ai_can_do"]) >= 4


def test_no_case_carries_a_dash_an_address_or_a_real_path():
    blob = json.dumps(CASES, ensure_ascii=False)
    assert "—" not in blob and "–" not in blob and "/home/" not in blob and "@" not in blob


def test_the_floor_hands_nothing_to_the_ai_and_asks_no_model():
    floor = arm("creator_keeps_it")
    rows = [score.score_case(floor, c) for c in CASES]
    assert not any(r["wrong_yes"] for r in rows)
    assert all(r["ok"] for r, c in zip(rows, CASES) if c["kind"] == "doability" and not c["label"]["ai_can_do"])
    assert floor(BY_ID["HOG-AS-01"])["assignee"] == "u_olive"


def test_a_scripted_judge_that_follows_the_label_passes_every_case_through_the_real_code():
    def judge_for(case):
        label = case["label"]
        if case["kind"] == "doability":
            return lambda prompt: json.dumps({"ai_can_do": label["ai_can_do"], "reason": "x"})
        return lambda prompt: json.dumps({"assignee": label["assignee"], "ai_can_do": label["ai_can_do"], "reason": "x"})
    for case in CASES:
        got = arms.run_case(case, judge_for(case))
        assert score.score_case(lambda c, got=got: got, case)["ok"], case["id"]


def test_the_prompts_the_judge_is_given_carry_the_goal_and_the_roster():
    seen = []
    arms.run_case(BY_ID["HOG-AS-01"], lambda p: (seen.append(p), '{"assignee": "u_bob", "ai_can_do": false}')[1])
    arms.run_case(BY_ID["HOG-DO-02"], lambda p: (seen.append(p), '{"ai_can_do": false}')[1])
    assert "Ask Bob Baker for the contract" in seen[0] and "u_olive: Olive Owner (quest owner)" in seen[0]
    assert "Call the bank" in seen[1] and "Close the loan" in seen[1] and "Mornings only" in seen[1]


def test_an_unreadable_verdict_keeps_the_goal_with_the_person():
    assert arms.run_doability(BY_ID["HOG-DO-01"], lambda p: "no json here")["ai_can_do"] is False


def test_the_broken_baseline_is_caught_by_its_wrong_yes_verdicts_and_fails_every_assignee_case():
    broken = common.load_impl("hand_off_a_goal.broken.rep_takes_all:build", "hand_off_a_goal")(common.CallMeter(max_calls=0))
    rows = [score.score_case(broken, c) for c in CASES]
    assert sum(r["wrong_yes"] for r in rows) >= 4
    assert not any(r["ok"] for r, c in zip(rows, CASES) if c["kind"] == "assignee")
    floor_rows = [score.score_case(arm("creator_keeps_it"), c) for c in CASES]
    assert sum(r["wrong_yes"] for r in rows) > sum(r["wrong_yes"] for r in floor_rows)


def test_the_scorer_reports_value_and_reversals_apart():
    line = score.aggregate([dict(ok=True, kind="doability", calls=1), dict(ok=False, kind="doability", calls=1, wrong_yes=True),
                            dict(ok=True, kind="assignee", calls=1)])
    assert "2/3" in line and "doability 1/2" in line and "assignee 1/1" in line and "wrong yes" in line and "2/3 verdicts a person would not reverse" in line
