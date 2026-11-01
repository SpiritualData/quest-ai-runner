"""Offline tests for the automatic tidy of leftover eval asks (runner.tidy_leftover_asks): no network, no real cancel.

Run:  .venv/bin/python3 -m pytest evaluation/components/chat_quality/test_tidy_asks.py -q
Kept beside the harness for the reason test_card_sweep.py gives (importing ``world`` needs this machine's dev-lane env)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runner  # noqa: E402


def test_every_swept_candidate_is_cancelled_and_counted(monkeypatch):
    cancelled = []
    monkeypatch.setattr(runner.W, "sweep_cancel_candidates", lambda: [{"decision_id": "d1"}, {"decision_id": "d2"}])
    monkeypatch.setattr(runner.W, "cancel_decisions", lambda ids: cancelled.extend(ids) or [])
    assert runner.tidy_leftover_asks() == 2
    assert cancelled == ["d1", "d2"]


def test_nothing_to_tidy_cancels_nothing_and_a_failure_never_stops_the_run(monkeypatch):
    monkeypatch.setattr(runner.W, "sweep_cancel_candidates", lambda: [])
    monkeypatch.setattr(runner.W, "cancel_decisions", lambda ids: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert runner.tidy_leftover_asks() == 0

    def boom():
        raise RuntimeError("backend down")
    monkeypatch.setattr(runner.W, "sweep_cancel_candidates", boom)
    assert runner.tidy_leftover_asks() == 0
