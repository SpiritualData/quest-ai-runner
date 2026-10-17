"""Offline tests for the ESOI behaviour dataset: well-formed, covers all 13 principles, and its checks
tell the reference good reply from the reference bad one. No model call."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from esoi_behavior import score  # noqa: E402

CASES = common.load_cases("esoi_behavior")


def test_dataset_is_well_formed_and_covers_every_principle():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) and 60 <= len(ids) <= 80
    primary = {c["principle"].split(";")[0] for c in CASES}
    assert primary == {f"P{n}" for n in range(1, 14)}
    for c in CASES:
        assert c["scenario"].strip() and c["expected_behavior"].strip() and c["must_have"].strip(), c["id"]
        assert "—" not in " ".join(str(v) for v in c.values()), f"{c['id']} has an em dash"
        for p in score.split_patterns(c["must_have"]) + score.split_patterns(c["must_not"]):
            re.compile(p)
    assert any(c["heldout"] for c in CASES)


def test_checks_accept_the_good_reference_and_reject_the_bad_one():
    for c in CASES:
        assert not score.check(c["good_reference"], c), (c["id"], score.check(c["good_reference"], c))
        assert score.check(c["bad_reference"], c), f"{c['id']}: the bad reference passes the checks"


def test_run_component_offline_arms(tmp_path):
    for impl, expect_pass in (("reference_good", len(CASES)), ("reference_bad", 0)):
        fn = common.load_impl(impl, "esoi_behavior")(common.CallMeter(0))
        rows = [score.score_case(fn, c) for c in CASES]
        assert sum(r["ok"] for r in rows) == expect_pass


def test_judged_scoring_uses_regex_only_as_a_prefilter():
    from esoi_behavior import judge
    case = next(c for c in CASES if c["id"] == "ESOI-002")  # must_not forbids a registry or framework
    calls = []

    def fn(c):
        return c["bad_reference"]
    fn.judge = lambda c, reply: calls.append(reply) or (True, "fine")
    row = score.score_case(fn, case)
    assert not row["ok"] and not row["judged"] and not calls  # the prefilter failed it with no judge call

    def other(c):
        return "Removed the spaces with one line, no extra tool."  # no must_have word, no must_not hit
    other.judge = lambda c, reply: (True, "does the small fix")
    row = score.score_case(other, case)
    assert row["ok"] and row["judged"] and not row["regex_ok"]  # the judge decides what the prefilter lets through
    assert "judge" in score.aggregate([row])
    assert judge.parse_verdict('{"verdict": "pass", "reason": "ok"}') == (True, "ok")
    assert judge.parse_verdict("not json")[0] is False and judge.parse_verdict('{"verdict": "maybe"}')[0] is False
    assert "FAILURE MODE" in judge.judge_prompt(case, "reply") and case["good_reference"] not in judge.judge_prompt(case, "x")


def test_in_situ_prompt_carries_the_standing_instructions_only_in_the_standing_arm():
    from esoi_behavior import insitu
    from quest_ai_runner.core.goal_runner_contract import compose_goal_prompt
    case = CASES[0]
    with_layer, without = insitu.compose(case, compose_goal_prompt, True), insitu.compose(case, compose_goal_prompt, False)
    assert "Evolvable Structure that Optimizes Intent" in with_layer and "Evolvable Structure" not in without
    for prompt in (with_layer, without):
        assert case["scenario"] in prompt and "HEADLESS RUN CONTRACT" in prompt and insitu.SANDBOX_NOTE in prompt


def test_judge_component_covers_every_case_and_the_regex_baseline_agrees_by_construction():
    from esoi_judge import score as judge_score
    judge_cases = common.load_cases("esoi_judge")
    assert {c["case_id"] for c in judge_cases} == {c["id"] for c in CASES} and len(judge_cases) == 2 * len(CASES)
    fn = common.load_impl("regex_only", "esoi_judge")(common.CallMeter(0))
    rows = [judge_score.score_case(fn, c) for c in judge_cases]
    assert all(r["ok"] for r in rows), [r["note"] for r in rows if not r["ok"]]
