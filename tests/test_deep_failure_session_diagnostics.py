"""A failed deep run reports what it ACTUALLY did, from its own session record.

The gap this covers: ``SubprocessGoalRunner`` derived everything a human reads about a failure
from the worker's final ``--output-format json`` envelope on stdout. A worker that does a lot of
real work and then dies BEFORE printing that envelope (killed, crashed, stuck) leaves stdout
empty, so the failure read, in full:

    "The worker exited 1 with no error output. ... Read the run output below for what it actually
    did."

with nothing below it. Meanwhile the run's own Claude Code session JSONL — the same file the live
monitor thread tails, bound deterministically by the ``--session-id`` this runner generates — held
every read, write, command and message of that work.

Now the failure paths fall back to the tail of that file, rendered through the monitor's own
``_format_message_text``. These tests pin: the fallback fires on a failed run with empty stdout,
degrades to today's bare message when the record is missing or unreadable, survives a
truncated/garbage file, and never touches a run that produced real output of its own.

Fully offline: ``subprocess.Popen`` is intercepted, ``Path.home`` is redirected into tmp_path, no
monitor thread is started (``emit`` is None), and no ``claude`` binary is ever spawned.
"""
from __future__ import annotations

import json
import subprocess as _sp
from pathlib import Path

import pytest

from quest_ai_runner.core.goal_runner import (
    SESSION_TAIL_MAX_ACTIONS,
    SubprocessConfig,
    SubprocessGoalRunner,
    read_session_activity_tail,
    resolve_session_file,
    worker_output_is_thin,
)


# --- helpers ----------------------------------------------------------------------------------

def assistant(*blocks: dict) -> str:
    """One realistic assistant record from a Claude Code session JSONL."""
    return json.dumps({
        "type": "assistant",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "message": {"role": "assistant", "model": "claude", "content": list(blocks)},
    })


def tool_result(text: str) -> str:
    """A tool_result record — these arrive as ``user`` entries and must stay out of the summary."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "abc", "content": text},
        ]},
    })


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_use(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": inp}


REALISTIC_RUN = [
    assistant(text_block("Reading the mailer to see how it sends."),
              tool_use("Read", file_path="src/mailer.py")),
    tool_result("...file contents..."),
    assistant(tool_use("Write", file_path="docs/design.md")),
    tool_result("ok"),
    assistant(text_block("Running a sanity check."),
              tool_use("Bash", command="python scripts/sanity_check.py")),
    tool_result("check passed"),
    assistant(text_block("The design doc is written; now wiring the send path.")),
]


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    """A working dir whose ``.claude/projects`` is the only place session files can be found."""
    projects = tmp_path / "work" / ".claude" / "projects" / "-work"
    projects.mkdir(parents=True)
    # Redirect home so a real machine's ~/.claude/projects can never satisfy the lookup.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    return projects


def run_with_worker(monkeypatch, working_dir, *, returncode: int, stdout: bytes,
                    stderr: bytes = b"", session_lines=None, goal: str = "ship the fix"):
    """Run the real ``SubprocessGoalRunner`` against a fake worker.

    The fake writes ``session_lines`` to the session file the runner asked for (via
    ``--session-id``) exactly like a real worker would, then exits with ``returncode`` — so the
    session record exists even though stdout may be empty.
    """

    class MockPopen:
        stdin = None

        def __init__(self, rc):
            self.returncode = rc

        def communicate(self, input=None, timeout=None):
            return (stdout, stderr)

    def fake_popen(cmd, **kw):
        session_id = cmd[cmd.index("--session-id") + 1]
        if session_lines is not None:
            projects = Path(kw["cwd"]) / ".claude" / "projects" / "-work"
            projects.mkdir(parents=True, exist_ok=True)
            (projects / f"{session_id}.jsonl").write_text("\n".join(session_lines))
        return MockPopen(returncode)

    monkeypatch.setattr(_sp, "Popen", fake_popen)
    runner = SubprocessGoalRunner(
        SubprocessConfig(working_dir=str(working_dir), claude_path="/usr/bin/claude"))
    return runner.run_goal(goal=goal, brief="do it", max_turns=3)


# --- the failure path: exit non-zero, empty stdout ---------------------------------------------

def test_failed_run_with_empty_stdout_reports_what_the_session_record_shows(session_dir, monkeypatch):
    """The live-observed failure: exit 1, no stdout, no stderr — but the session record proves a
    pile of real work. That work must reach the human-readable error."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=1, stdout=b"",
                          session_lines=REALISTIC_RUN)

    assert res.met is False
    err = res.error or ""
    # The existing honest framing is preserved (no cause is asserted).
    assert "The worker exited 1 with no error output" in err
    # ...and the real, recorded work is now attached.
    assert "session record shows it last did" in err
    assert "Read: src/mailer.py" in err
    assert "Write: docs/design.md" in err
    assert "$ python scripts/sanity_check.py" in err
    assert "The design doc is written" in err
    # Most recent last: the closing message comes after the first read.
    assert err.index("Read: src/mailer.py") < err.index("The design doc is written")
    # tool_result noise stays out.
    assert "check passed" not in err


def test_failed_run_without_a_session_record_keeps_todays_bare_message(session_dir, monkeypatch):
    """No session file (deleted, never created, a different failure mode) → exactly today's
    message, no crash, no invented content."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=1, stdout=b"", session_lines=None)

    assert res.met is False
    err = res.error or ""
    assert "The worker exited 1 with no error output" in err
    assert "session record" not in err


def test_failed_run_with_a_malformed_session_record_degrades_gracefully(session_dir, monkeypatch):
    """A session file cut off mid-write (or holding junk) yields whatever complete records it has,
    and never raises."""
    lines = [
        "not json at all",
        assistant(tool_use("Read", file_path="src/mailer.py")),
        '{"type": "assistant", "message": {"content": [{"type": "text", "te',  # truncated tail
    ]
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=1, stdout=b"", session_lines=lines)

    assert res.met is False
    assert "Read: src/mailer.py" in (res.error or "")


def test_failed_run_with_a_wholly_unparseable_session_record_falls_back(session_dir, monkeypatch):
    """Nothing renderable in the file → the bare message, not an empty "shows it last did:"."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=1, stdout=b"",
                          session_lines=["garbage", "{}", tool_result("only noise")])

    assert "The worker exited 1 with no error output" in (res.error or "")
    assert "session record" not in (res.error or "")


def test_a_run_that_produced_real_output_is_untouched(session_dir, monkeypatch):
    """The fallback engages only when stdout is genuinely empty/thin: a failed run that DID print
    its envelope still reports its own output, with no session-record block appended."""
    envelope = json.dumps({
        "result": "I refactored the mailer and updated three call sites, but the send failed "
                  "because no credentials were configured for it.",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }).encode()
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=1, stdout=envelope,
                          session_lines=REALISTIC_RUN)

    err = res.error or ""
    assert "Last output:" in err
    assert "I refactored the mailer" in err
    assert "session record" not in err


def test_successful_run_is_untouched(session_dir, monkeypatch):
    """A clean run reports met=True with no error at all — the fallback is failure-path only."""
    envelope = json.dumps({"result": "done: edited two files", "usage": {}}).encode()
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=0, stdout=envelope,
                          session_lines=REALISTIC_RUN)

    assert res.met is True
    assert res.error is None


# --- the other blind spot: exit 0 with empty output --------------------------------------------

def test_exit_zero_empty_output_still_reports_the_session_record(session_dir, monkeypatch):
    """Exit 0 + empty output normally means "the worker never ran the goal" (the missing ``-p``
    no-op). When the session record shows real work, that assertion would be FALSE — so the
    wording softens to a named possibility and the recorded work is attached."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=0, stdout=b"   \n",
                          session_lines=REALISTIC_RUN)

    assert res.met is False
    err = res.error or ""
    assert "no output" in err.lower()
    assert "cannot be confirmed to have run" in err
    assert "$ python scripts/sanity_check.py" in err


def test_exit_zero_empty_output_without_a_record_keeps_the_no_op_diagnosis(session_dir, monkeypatch):
    """With no record to contradict it, the established silent-no-op message is unchanged."""
    res = run_with_worker(monkeypatch, session_dir.parents[2], returncode=0, stdout=b"   \n", session_lines=None)

    assert res.met is False
    assert "the goal did not actually run" in (res.error or "")


# --- the extraction itself ---------------------------------------------------------------------

def test_read_session_activity_tail_caps_the_number_of_actions(session_dir):
    lines = [assistant(tool_use("Read", file_path=f"file{i}.py")) for i in range(40)]
    (session_dir / "sess.jsonl").write_text("\n".join(lines))

    block = read_session_activity_tail(str(session_dir.parents[2]), "sess")

    assert block.count("\n- ") + 1 == SESSION_TAIL_MAX_ACTIONS
    assert "file39.py" in block          # the most recent survives
    assert "file0.py" not in block       # the oldest is dropped


def test_read_session_activity_tail_only_reads_the_end_of_a_large_file(session_dir):
    """Bounded by bytes, not by record count: this runs synchronously on the failure path."""
    filler = [assistant(text_block("x" * 500)) for _ in range(50)]
    lines = filler + [assistant(tool_use("Edit", file_path="late.py"))]
    (session_dir / "big.jsonl").write_text("\n".join(lines))

    block = read_session_activity_tail(str(session_dir.parents[2]), "big", max_bytes=2000)

    assert "Edit: late.py" in block


def test_resolve_session_file_is_silent_when_nothing_matches(session_dir):
    # A decoy session from an unrelated run: the project dir resolves, but nothing matches OUR id
    # (the binding is by session id, never "whatever jsonl is lying around").
    (session_dir / "someone-elses-session.jsonl").write_text(assistant(text_block("unrelated")))

    assert resolve_session_file(str(session_dir.parents[2]), None) is None
    assert resolve_session_file(str(session_dir.parents[2]), "no-such-session") is None
    assert read_session_activity_tail(str(session_dir.parents[2]), "no-such-session") == ""


def test_unreadable_session_file_never_raises(session_dir):
    path = session_dir / "locked.jsonl"
    path.write_text(assistant(tool_use("Read", file_path="x.py")))
    path.chmod(0o000)
    try:
        assert read_session_activity_tail(str(session_dir.parents[2]), "locked") == ""
    finally:
        path.chmod(0o644)


def test_worker_output_is_thin():
    assert worker_output_is_thin("") is True
    assert worker_output_is_thin("   \n ") is True
    assert worker_output_is_thin("ok") is True
    assert worker_output_is_thin("I edited three files and ran the whole test suite green.") is False
