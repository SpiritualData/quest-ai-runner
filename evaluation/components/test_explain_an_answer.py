"""Offline tests for explain_an_answer: the cases name their sources, the real writer and payload builder run in every arm, the floor
is the trace-only payload with no model call, the phrase checks reject a fabricated step before a grader is asked, the grader's
unsupported and omitted verdicts decide, readability is reported apart from the metric, and the broken baseline fails every case. A
stub stands in for ``claude -p``; no model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_explain_an_answer.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
import claude_judge  # noqa: E402
from explain_an_answer import arms, score  # noqa: E402

CASES = json.loads((HERE / "explain_an_answer" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}
WRITTEN = {"understood": "You wanted the notes tidied.", "approach": "Opened your notes and tidied them.",
           "confidence": "High, the edit went through."}


class StubProvider:
    """Stands in for ``claude -p``: the explanation call (a plan) gets ``written``, the grader (an answer) gets ``verdict``."""

    def __init__(self, written=None, verdict=None):
        self.written, self.verdict = written or WRITTEN, verdict or {"unsupported": [], "omitted": []}
        self.asked = []
        self.tokens_in = self.tokens_out = 0
        self.cost_usd = 0.0

    def plan(self, prompt, *, model=None, tool_schema=None, step=None, layers=None):  # noqa: ARG002
        self.asked.append(prompt)
        return dict(self.written)

    def answer(self, messages, *, model=None, system=None, step=None):  # noqa: ARG002
        self.asked.append(messages[-1]["content"])
        return json.dumps(self.verdict)


def arm(name, provider, monkeypatch):
    monkeypatch.setattr(claude_judge, "cli_provider", lambda meter, model: provider)
    return common.load_impl(name, "explain_an_answer")(common.CallMeter(max_calls=40))


def test_every_case_names_its_source_and_lists_what_the_note_must_carry():
    assert len(BY_ID) == len(CASES) >= 10
    assert all(len(c["source"]) > 40 and c["required"] for c in CASES)


def test_the_floor_is_the_trace_only_payload_and_asks_no_model_to_write(monkeypatch):
    provider = StubProvider()
    out = arm("trace_template", provider, monkeypatch)(BY_ID["EXA-01"])
    assert "notes.md" in out["text"] and "tidy the notes" in out["text"] and out["prose"].strip() == ""
    assert provider.asked == []


def test_the_current_arm_writes_prose_through_the_real_writer_and_keeps_the_recorded_facts(monkeypatch):
    provider = StubProvider()
    out = arm("model_explanation", provider, monkeypatch)(BY_ID["EXA-01"])
    assert "You wanted the notes tidied." in out["text"] and "notes.md" in out["text"]
    assert "Tidy the notes" in provider.asked[0] and "Nothing was read" not in provider.asked[0]


def test_a_faithful_complete_note_passes_and_a_floor_is_not_readable(monkeypatch):
    row = score.score_case(arm("model_explanation", StubProvider(), monkeypatch), BY_ID["EXA-01"])
    assert row["ok"] and row["readable"]
    floor = score.score_case(arm("trace_template", StubProvider(), monkeypatch), BY_ID["EXA-01"])
    assert floor["ok"] and not floor["readable"]


def test_a_fabricated_step_is_rejected_by_phrase_before_the_grader(monkeypatch):
    provider = StubProvider({**WRITTEN, "approach": "You asked, so I searched the web and compared sources."})
    row = score.score_case(arm("model_explanation", provider, monkeypatch), BY_ID["EXA-02"])
    assert not row["ok"] and "web" in row["note"]
    assert len(provider.asked) == 1


def test_the_graders_unsupported_and_omitted_steps_fail_the_case(monkeypatch):
    provider = StubProvider(verdict={"unsupported": ["says it was saved"], "omitted": ["the failed send"]})
    row = score.score_case(arm("model_explanation", provider, monkeypatch), BY_ID["EXA-03"])
    assert not row["ok"] and not row["faithful"] and not row["complete"]


def test_a_figure_for_a_metric_in_the_prose_is_a_fabrication(monkeypatch):
    provider = StubProvider({**WRITTEN, "approach": "You asked, and the greeting rate rose from 0.5 to 0.75."})
    row = score.score_case(arm("model_explanation", provider, monkeypatch), BY_ID["EXA-10"])
    assert not row["ok"] and "figure" in row["note"]


def test_readable_problems_name_machinery_and_dashes():
    assert score.readable_problems("You can see the notes were tidied.") == []
    assert any("machinery" in p for p in score.readable_problems("You can see the card was loaded in step two."))
    assert "dash" in score.readable_problems("You asked – and I did it.")


def test_the_broken_baseline_fails_every_case():
    broken = common.load_impl("explain_an_answer.broken.invent_a_process:build", "explain_an_answer")(common.CallMeter())
    assert not any(score.score_case(broken, c)["ok"] for c in CASES)
