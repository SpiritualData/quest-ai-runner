"""A deep run that ran out of TURNS says so, instead of listing "common causes".

Claude Code's ``--output-format json`` envelope states how the run ended in its ``subtype``
(``success``, ``error_max_turns``, ...). The runner was throwing that field away and reporting
every non-zero exit with the same hedge -- "Common causes: the turn or token budget ran out, or
the worker itself errored" -- which is a guess printed next to the answer.

It matters because the two cases call for opposite responses. A crash means something is broken.
Running out of turns means the work may be DONE and merely unconfirmed: the live case that
prompted this was a daily dissertation brief whose final acts were sending the mail and writing
its goal note, reported to its owner as a flat failure because the turn budget ended one beat
later. The fix is diagnosis, not leniency -- an unconfirmed goal is still not a met goal.
"""
from __future__ import annotations

import json

from .test_deep_failure_session_diagnostics import run_with_worker, session_dir  # noqa: F401


def envelope(subtype: str, result: str = "", *, is_error: bool = True) -> bytes:
    """A Claude Code result envelope as the worker prints it on stdout."""
    return json.dumps({
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "usage": {"input_tokens": 1200, "output_tokens": 300},
        "total_cost_usd": 0.02,
    }).encode()


def test_turn_budget_exhaustion_is_named_not_guessed_at(session_dir, monkeypatch):  # noqa: F811
    res = run_with_worker(
        monkeypatch, session_dir.parents[2], returncode=1,
        stdout=envelope("error_max_turns", "Sent the brief and added the goal note."))

    assert res.met is False
    err = res.error or ""
    assert "3-turn budget" in err                 # run_with_worker runs with max_turns=3
    assert "UNCONFIRMED" in err
    assert "still happened" in err                # the work it did is not disowned
    assert "Common causes" not in err             # the old hedge is gone for this case
    # The worker's own account of how far it got must survive onto the result.
    assert "Sent the brief" in (res.output or "")


def test_a_real_crash_still_gets_the_honest_hedge(session_dir, monkeypatch):  # noqa: F811
    """Regression guard: only the max-turns case changes. Anything else keeps the wording that
    asserts no cause, since for a crash we genuinely do not know."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=1,
                          stdout=envelope("error_during_execution", "partial work"))

    err = res.error or ""
    assert "exited 1 with no error output" in err
    assert "Common causes" in err
    assert "turn budget" not in err


def test_plain_text_worker_without_an_envelope_is_unaffected(session_dir, monkeypatch):  # noqa: F811
    """A non-Claude-Code DeepRunner prints no JSON envelope, so there is no subtype to read and
    the generic message must still apply."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=1,
                          stdout=b"just some text, no envelope")

    assert "Common causes" in (res.error or "")


def test_success_envelope_still_reports_the_goal_met(session_dir, monkeypatch):  # noqa: F811
    """The subtype plumbing must not disturb the happy path."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=0,
                          stdout=envelope("success", "done and verified", is_error=False))

    assert res.met is True
    assert res.error is None
    assert res.tokens == 1500
