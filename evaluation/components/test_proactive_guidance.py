"""Offline tests for proactive_guidance: the cases are labelled in both classes at all three intent levels and name their source, the floor
asks no reply model and fails every nudge case, the scorer's checks by code catch an invented figure, a dash, a nudge ahead of the answer and
a nudge where none belongs before a grader is asked, a good nudge and a good restraint pass through a stub grader, and the deliberately broken
baseline fails every case and asks no grader. No model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_proactive_guidance.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
import claude_judge  # noqa: E402
from proactive_guidance import arms, score  # noqa: E402

CASES = json.loads((HERE / "proactive_guidance" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


class StubProvider:
    """Stands in for ``claude -p``: every grader prompt gets ``verdict``; a reply prompt gets ``text``."""

    def __init__(self, verdict=None, text=""):
        self.verdict, self.text, self.asked = verdict or {}, text, 0
        self.tokens_in = self.tokens_out = 0
        self.cost_usd = 0.0

    def answer(self, messages, *, model=None, system=None, step=None):  # noqa: ARG002
        self.asked += 1
        return json.dumps(self.verdict) if "Grade only the reply" in messages[-1]["content"] else self.text


def test_cases_cover_both_classes_at_every_level_and_name_their_source():
    assert len({c["id"] for c in CASES}) == len(CASES) >= 24
    for level in score.LEVELS:
        for klass in ("nudge", "restraint"):
            assert sum(1 for c in CASES if c["level"] == level and c["class"] == klass) >= 4
    for case in CASES:
        assert len(case["source"]) > 100 and "2026-10-27" in case["source"]
        assert case["milestone_keys"] and case["metric_values"] and case["plain_answer"] and case["context"]
        assert (case["expected_level"] == case["level"]) if case["class"] == "nudge" else case["expected_level"] is None
    kinds = {c["kind"] for c in CASES if c["class"] == "restraint"}
    assert {"urgent", "narrow", "unrelated", "emotional", "repeated"} <= kinds


def test_every_milestone_and_reading_is_named_in_the_context_it_belongs_to():
    for case in CASES:
        context = arms.context_text(case)
        assert any(score.names(score.tokens(context), k) for k in case["milestone_keys"]), case["id"]
        assert any(score.reads(score.numbers(context), v) or v in context for v in case["metric_values"]), case["id"]


def test_no_case_or_prompt_carries_a_dash_a_path_or_an_address():
    blob = json.dumps(CASES, ensure_ascii=False) + arms.GUIDANCE_DIRECTIVE + arms.BREVITY
    assert "—" not in blob and "–" not in blob and "/home/" not in blob and "@" not in blob


def test_the_floor_asks_no_reply_model_and_fails_every_nudge_case_by_code(monkeypatch):
    stub = StubProvider({"responds": True, "pointer": False, "pointer_level": "none", "grounded": True, "awkward": False})
    monkeypatch.setattr(claude_judge, "cli_provider", lambda meter, model: stub)
    floor = common.load_impl("no_nudge", "proactive_guidance")(common.CallMeter(max_calls=40))
    rows = [score.score_case(floor, c) for c in CASES]
    assert not any(r["ok"] for r in rows if r["class"] == "nudge")
    assert all(r["ok"] for r in rows if r["class"] == "restraint")
    assert stub.asked == sum(1 for c in CASES if c["class"] == "restraint")   # only graders, no replies, none for the failed nudges


def test_a_grounded_short_nudge_after_the_answer_passes_and_a_wrong_level_fails():
    case = BY_ID["PG-N-Q1"]
    reply = "Log the distance, your pace and how your legs felt. Your next milestone is the 25 km long run by 15 November, and you are at 21 km."
    good = {"responds": True, "pointer": True, "pointer_level": "quest", "grounded": True, "awkward": False, "reason": "fits"}
    assert score.score_case(lambda c: {"reply": reply, "provider": StubProvider(good)}, case)["ok"]
    wrong = dict(good, pointer_level="goal")
    assert not score.score_case(lambda c: {"reply": reply, "provider": StubProvider(wrong)}, case)["ok"]


@pytest.mark.parametrize("reply,problem", [
    ("Log the distance. Your 25 km long run is next and you are at 22 km.", "figure not in the case"),
    ("Log the distance — your 25 km long run is next at 21 km.", "dash"),
    ("Your 25 km long run is next at 21 km. Log the distance and pace.", "nudge ahead of the answer"),
    ("Log the distance and pace.", "names no real milestone"),
])
def test_checks_by_code_reject_before_the_grader_is_asked(reply, problem):
    stub = StubProvider({})
    row = score.score_case(lambda c: {"reply": reply, "provider": stub}, BY_ID["PG-N-Q1"])
    assert not row["ok"] and problem in row["note"] and stub.asked == 0


def test_a_nudge_in_a_restraint_case_is_awkward_by_code():
    stub = StubProvider({})
    reply = "Check your provider status page first. Also, customer number 30 is your next milestone, at 24 of 50."
    row = score.score_case(lambda c: {"reply": reply, "provider": stub}, BY_ID["PG-R-Q1"])
    assert not row["ok"] and row["awkward"] and row["nudged"] and stub.asked == 0


def test_a_plain_answer_to_a_restraint_case_passes_with_one_grader_call():
    stub = StubProvider({"responds": True, "pointer": False, "pointer_level": "none", "grounded": True, "awkward": False, "reason": "plain"})
    case = BY_ID["PG-R-Q3"]
    row = score.score_case(lambda c: {"reply": c["plain_answer"], "provider": stub}, case)
    assert row["ok"] and not row["awkward"] and stub.asked == 1


def test_the_broken_baseline_fails_every_case_and_asks_no_grader():
    broken = common.load_impl("proactive_guidance.broken.nudge_every_time:build", "proactive_guidance")(common.CallMeter(max_calls=0))
    rows = [score.score_case(broken, c) for c in CASES]
    assert not any(r["ok"] for r in rows)
    assert sum(1 for r in rows if r["class"] == "restraint" and r["awkward"]) == sum(1 for c in CASES if c["class"] == "restraint")


def test_the_aggregate_is_two_sided_and_reports_every_level():
    rows = [dict(ok=True, awkward=False, level="quest", **{"class": "nudge"}), dict(ok=False, awkward=True, level="quest", **{"class": "restraint"}),
            dict(ok=True, awkward=False, level="goal", **{"class": "restraint"})]
    line = score.aggregate(rows)
    assert "two-sided value 0.75" in line and "nudge cases right 1/1" in line and "restraint cases right 1/2" in line
    assert "awkward on restraint cases 1/2" in line and "rep nudge 0/0" in line
