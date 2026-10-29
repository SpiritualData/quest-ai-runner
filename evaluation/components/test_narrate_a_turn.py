"""Offline tests for narrate_a_turn: the cases name their sources, the real Narrator runs in every arm, the floor says a fixed line and
asks no model, the scorer's checks by code reject a dashed, figured or generic beat before a grader is asked, a supported specific beat
passes, and the deliberately broken baseline fails every case. A stub stands in for ``claude -p``; no model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_narrate_a_turn.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
import claude_judge  # noqa: E402
from narrate_a_turn import arms, score  # noqa: E402

CASES = json.loads((HERE / "narrate_a_turn" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


class StubProvider:
    """Stands in for ``claude -p``: the beat prompt gets ``beat``, the grader prompt gets ``verdict``."""

    def __init__(self, beat, verdict=None):
        self.beat, self.verdict, self.asked = beat, verdict or {"supported": True, "unsupported_claims": []}, []
        self.tokens_in = self.tokens_out = 0
        self.cost_usd = 0.0

    def answer(self, messages, *, model=None, system=None, step=None):  # noqa: ARG002
        text = " ".join(m["content"] for m in messages)
        self.asked.append(text)
        return json.dumps(self.verdict) if "SUPPORTED" in text else self.beat


def arm(name, provider=None, monkeypatch=None):
    if provider is not None:
        monkeypatch.setattr(claude_judge, "cli_provider", lambda meter, model: provider)
    return common.load_impl(name, "narrate_a_turn")(common.CallMeter(max_calls=40))


def test_every_case_names_its_source_and_ids_are_unique():
    assert len(BY_ID) == len(CASES) >= 12
    assert all(len(c["source"]) > 40 and c["names"] for c in CASES)
    assert {c["kind"] for c in CASES} == {"specific", "repeat"}


def test_the_floor_says_a_fixed_line_asks_no_model_and_names_nothing(monkeypatch):
    provider = StubProvider("unused")
    floor = arm("no_beat", provider, monkeypatch)
    rows = [score.score_case(floor, BY_ID[i]) for i in ("NAT-01", "NAT-10")]
    assert [r["ok"] for r in rows] == [False, False] and all(r["unspecific"] for r in rows)
    assert provider.asked == [], "a fixed line that names nothing is rejected by code, without the grader"


def test_the_floor_is_right_to_stay_quiet_where_the_line_would_repeat(monkeypatch):
    floor = arm("no_beat", StubProvider("unused"), monkeypatch)
    row = score.score_case(floor, BY_ID["NAT-12"])
    assert row["ok"] and row["quiet"]


def test_a_supported_specific_beat_passes_and_costs_one_grader_call(monkeypatch):
    provider = StubProvider("I'll add the swim sessions to this week's goals.")
    row = score.score_case(arm("model_beat", provider, monkeypatch), BY_ID["NAT-01"])
    assert row["ok"] and not row["contract"] and not row["unspecific"]
    assert len(provider.asked) == 2 and "SUPPORTED" in provider.asked[1]


def test_an_unsupported_claim_fails(monkeypatch):
    provider = StubProvider("I see your swim goal is already half done.",
                            {"supported": False, "unsupported_claims": ["the goal is half done"]})
    row = score.score_case(arm("model_beat", provider, monkeypatch), BY_ID["NAT-01"])
    assert not row["ok"] and row["unsupported"]


@pytest.mark.parametrize("beat,problem", [
    ("I'll log the ride – 22 km of cycling.", "dash"),
    ("I found 7 rides in your Run Log.", "figure not in the turn"),
    ("Looking into your Run Log ride.", "not first person"),
    ("I'll log the ride in your run log, then check your totals, then compare weeks, then summarise how the knee is doing overall and what that means for the rest of the month.", "words"),
])
def test_the_checks_by_code_reject_before_the_grader_is_asked(beat, problem, monkeypatch):
    provider = StubProvider(beat)
    row = score.score_case(arm("model_beat", provider, monkeypatch), BY_ID["NAT-03"])
    assert not row["ok"] and any(problem in p for p in row["contract"])
    assert len(provider.asked) == 1, "only the beat call, no grader"


def test_an_empty_beat_is_wrong_for_a_message_with_something_to_name(monkeypatch):
    row = score.score_case(arm("model_beat", StubProvider(""), monkeypatch), BY_ID["NAT-02"])
    assert not row["ok"] and row["quiet"]


def test_the_broken_baseline_fails_every_case_and_asks_no_grader():
    broken = common.load_impl("narrate_a_turn.broken.trust_everything:build", "narrate_a_turn")(common.CallMeter())
    rows = [score.score_case(broken, c) for c in CASES]
    assert not any(r["ok"] for r in rows)


def test_the_aggregate_reports_value_time_and_cost():
    rows = [dict(ok=True, kind="specific", beat_seconds=3.0, calls=2), dict(ok=False, kind="repeat", quiet=True, calls=1, late=True, beat_seconds=7.0)]
    text = score.aggregate(rows)
    assert "value 1/2" in text and "later than 5s 1" in text and "1.50 model calls per case" in text


def test_the_first_beat_prompt_asks_for_an_i_sentence_and_no_invented_date():
    from quest_ai_runner.core.orchestrator_turn import NARRATE_BEAT_SPEC
    text = NARRATE_BEAT_SPEC.template
    assert "starts with I" in text and "today's date" in text
