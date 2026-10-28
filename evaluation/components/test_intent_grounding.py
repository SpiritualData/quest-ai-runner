"""Offline tests for intent_grounding: the cases validate and name their sources, the prompt differs between the arms by the
intent block only, a control composes the same bytes for both arms, the chat_quality verdict is reached with a stub judge, and the
deliberately broken baseline fails every case by code with no model asked. No model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_intent_grounding.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from intent_grounding import arms, score  # noqa: E402

CASES = json.loads((HERE / "intent_grounding" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}
INTENT = [c for c in CASES if c["kind"] == "intent"]
CONTROLS = [c for c in CASES if c["kind"] == "control"]


class StubJudge:
    """A judge provider that passes every rubric item, so the path from reply to verdict can be walked offline."""

    def __init__(self):
        self.asked = 0

    def answer(self, messages, *, model=None, system=None):  # noqa: ARG002
        self.asked += 1
        text = messages[-1]["content"]
        n = int(text.split("exactly ")[1].split(" ")[0])
        pivots = [line.split(" (quest")[0].lstrip("- ") for line in text.split("## Context the answer MUST draw on (pivots)")[1]
                  .split("##")[0].splitlines() if line.startswith("- ")]
        return json.dumps({"rubric": [{"id": i, "item": "x", "pass": True, "evidence": "q"} for i in range(1, n + 1)], "score": 1.0,
                           "routing_ok": True, "routing_note": "", "side_effects_ok": True, "side_effects_note": "",
                           "context_used": [{"pivot": p, "used": True, "changed_answer": True, "evidence": "q"} for p in pivots],
                           "summary": "stub", "failure_class": None})


def test_every_case_validates_names_its_source_and_ids_are_unique():
    assert len(BY_ID) == len(CASES) >= 10
    for case in CASES:
        arms.validate(case)
        assert len(case["source"]) > 60
    assert len(INTENT) >= 5 and len(CONTROLS) >= 4


def test_the_arms_differ_by_the_intent_block_and_nothing_else():
    for case in INTENT:
        with_frame, without = arms.user_prompt(case, True), arms.user_prompt(case, False)
        assert arms.INTENT_FRAME_PREAMBLE in with_frame and case["frame"] in with_frame
        assert "Serves:" not in without and arms.INTENT_FRAME_PREAMBLE not in without
        assert with_frame.replace(arms.INTENT_FRAME_PREAMBLE + "\n" + case["frame"].strip() + "\n\n", "") == without


def test_a_control_composes_the_same_bytes_for_both_arms():
    for case in CONTROLS:
        assert arms.user_prompt(case, True) == arms.user_prompt(case, False)


def test_no_prompt_or_case_text_carries_a_dash_or_a_real_identifier():
    blob = json.dumps(CASES, ensure_ascii=False) + arms.SYSTEM + "".join(arms.user_prompt(c, True) for c in CASES)
    assert "—" not in blob and "–" not in blob and "/home/" not in blob and "@" not in blob


def test_a_reply_that_names_the_gaps_is_passed_through_the_chat_quality_verdict():
    case = BY_ID["IG-INT-03"]
    judge = StubJudge()
    good = 'Two gaps: ideal "Deep sleep" has no solution, and solution "Rest routine" has no quest.'
    record = arms.grade(case, good, judge)
    assert record["passed"] and judge.asked == 1 and record["score"] == 1.0
    missing = arms.grade(case, "Nothing is missing, keep running.", StubJudge())
    assert not missing["passed"] and missing["precheck"]["hard_failures"] and missing["score"] == 0.0


def test_a_hard_check_failure_spends_no_judge_call():
    judge = StubJudge()
    record = arms.grade(BY_ID["IG-INT-03"], "Keep going — you are doing well.", judge)
    assert judge.asked == 0 and not record["passed"]


def test_the_baseline_fails_every_case_by_code_and_asks_no_model():
    from intent_grounding.broken import invent_a_quest
    broken = invent_a_quest.build(common.CallMeter(max_calls=0))
    rows = [score.score_case(broken, c) for c in CASES]
    assert not any(r["ok"] for r in rows) and all(r["hard"] for r in rows)


def test_the_scorer_reports_intent_cases_and_controls_apart():
    rows = [dict(ok=True, kind="intent", score=1.0, calls=2), dict(ok=False, kind="intent", score=0.4, calls=2),
            dict(ok=True, kind="control", score=0.9, calls=2)]
    line = score.aggregate(rows)
    assert "2/3" in line and "intent 1/2" in line and "control 1/1" in line
