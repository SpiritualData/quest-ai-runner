"""Offline tests of the handoff page renderer: a fixture, the content merge, the stale banner and the real records.
Run:  .venv/bin/python3 -m pytest evaluation/components/qar_overall/test_render_handoff.py -q"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qar_overall import render_handoff as rh  # noqa: E402


def fixture(stale=0):
    tier = dict(value=0.8, measured=1.0, floor=0.8)
    sub = dict(id="QME1", name="Chat — turns", weight=1.0, tiers={"cheap": tier, "full": tier}, arms=[], cheap_only=False)
    overall = dict(headline=0.8, floor=0.8, ceiling=0.8, measured_weight=1.0, unserved=0.0)
    return dict(generated="2026-10-29 10:00", generated_iso="2026-10-29T10:00:00+00:00", commit="abc1234", today="2026-10-29",
                subs=[sub], overall={"cheap": overall, "full": overall}, bar=0.8, unserved=0.0,
                stale=dict(count=stale, total=3, reasons={"code changed since it was recorded": stale}, arms=[]),
                parts=[dict(part="Mind", solution="s", metric="m", state="measured", why="", passed=3, cases=4, date="2026-10-29")],
                prompts=dict(rows=[], unmeasured=2, total=2), hubs=[dict(area="a", status="x", age=3, hubs=1, candidates=0)],
                e2e=dict(state="measured", why="", passed=1, cases=2, date="2026-10-29", kinds={"web": 2}),
                proactive=dict(arms=[dict(impl="g", state="measured", why="", passed=1, cases=2, date="2026-10-29")],
                               levels={"quest": 2}, classes={"nudge": 2}), examples=[])


CONTENT = {"decisions": [{"decision": "D1", "answer": "A1", "whose": "joshua"}, {"decision": "D2", "answer": "A2", "whose": "/joshua"}],
           "unfinished": [{"item": "Thing", "state": "open"}], "summary": "Dash — here",
           "breakdowns": {"end_to_end": {"row_date": "2026-10-29", "parts": {"web": [2, 2]}}}}


def test_renders_all_sections_without_dashes():
    page = rh.render(fixture(), CONTENT)
    for text in ("Overall QAR metric", "Every part", "End to end", "Proactive guidance", "Prompt spend audit", "Hub of hubs",
                 "Decisions", "Unfinished work", "Generated 2026-10-29 10:00 UTC from commit abc1234"):
        assert text in page
    assert "—" not in page and "–" not in page


def test_content_merges_and_labels_whose():
    page = rh.render(fixture(), CONTENT)
    assert "Your answer" in page and "/joshua" in page and "Thing" in page and "2/2" in page


def test_stale_banner_only_when_stale():
    assert "are stale" in rh.render(fixture(stale=2), CONTENT)
    assert "are stale" not in rh.render(fixture(), CONTENT)


def test_runs_on_the_real_records():
    page = rh.render(rh.collect(), CONTENT)
    assert "Sub-metrics" in page and "QME9" in page and "—" not in page
