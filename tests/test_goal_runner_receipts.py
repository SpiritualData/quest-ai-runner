"""Structural receipts of a deep run, read from the worker's session record (core/goal_runner_receipts.py). Offline."""
import json
from pathlib import Path
from types import SimpleNamespace

from quest_ai_runner.core import goal_runner_receipts as rr
from quest_ai_runner.core.goal_runner import SubprocessGoalRunner
from quest_ai_runner.core.interim_policy import done_text


def use(i, name, **inp):
    return {"message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def result(i, error=False):
    return {"message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": error}]}}


def session(tmp_path, rows):
    path = tmp_path / "s1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def records_of(monkeypatch, path):
    monkeypatch.setattr(rr, "resolve_session_file", lambda *a, **k: path)
    return rr.tool_records("w", "s1")


ROWS = [use("a", "WebSearch", query="q"), result("a"), use("b", "WebSearch", query="q2"), result("b", True),
        use("c", "WebFetch", url="https://example.org/p"), result("c"), use("d", "WebFetch", url="https://example.org/x"),
        result("d", True), use("e", "Write", file_path="/x/out.md", content="héllo"), result("e"),
        use("f", "Edit", file_path="/x/out.md", new_string="abc"), result("f", True),
        use("g", "Bash", command="ls"), result("g"), use("h", "Read", file_path="/x")]


def test_lines_count_structure_only(tmp_path, monkeypatch):
    lines = rr.receipt_lines(records_of(monkeypatch, session(tmp_path, ROWS)))
    assert lines == ["Ran 2 web searches, 1 returned results",
                     "Tried to open 2 pages: 1 loaded, 1 could not be opened",
                     "Wrote or changed 1 file, 6 bytes of new text",   # the failed edit is not counted; é is two bytes
                     "Made 2 other tool calls (reading files, running commands)"]
    joined = " ".join(lines)
    assert "/x" not in joined and "example.org" not in joined and "—" not in joined


def test_resumed_run_counts_only_its_own(tmp_path, monkeypatch):
    path = session(tmp_path, ROWS)
    before = rr.receipt_ids(records_of(monkeypatch, path))
    with open(path, "a") as fh:
        fh.write(json.dumps(use("z", "WebSearch", query="more")) + "\n" + json.dumps(result("z")) + "\n")
    after = rr.tool_records("w", "s1")
    assert rr.receipt_lines(after, before) == ["Ran 1 web search, 1 returned results"]


def test_unreadable_record_gives_nothing(monkeypatch):
    monkeypatch.setattr(rr, "resolve_session_file", lambda *a, **k: None)
    assert rr.tool_records("w", "s1") == {} and rr.receipt_lines({}) == []


def test_runner_reports_receipts_so_the_interim_done_is_not_empty(tmp_path, monkeypatch):
    path = session(tmp_path, ROWS)
    monkeypatch.setattr(rr, "resolve_session_file", lambda *a, **k: path)
    runner = SubprocessGoalRunner.__new__(SubprocessGoalRunner)
    runner.cfg = SimpleNamespace(working_dir="w")
    monkeypatch.setattr(SubprocessGoalRunner, "goal_settings", lambda self: SimpleNamespace(claude_config_dir=None))
    res = SimpleNamespace(session_id="s1", observations=[], observations_reported=False, changes_landed=0)
    runner.count_landed(res, None, set())
    assert res.observations_reported is True and len(res.observations) == 4
    assert done_text(receipts=res.observations, coverage=None).startswith("- Ran 2 web searches")


def test_a_file_written_by_a_shell_command_leaves_no_write_receipt_but_the_run_still_reports(tmp_path, monkeypatch):
    """A worker that wrote through Bash shows as a tool call only: the record is non-empty (so 'nothing recorded' is never claimed of
    a run that did something) and no write is asserted that the session record cannot prove."""
    rows = [use("a", "Bash", command="printf x > out.txt"), result("a")]
    lines = rr.receipt_lines(records_of(monkeypatch, session(tmp_path, rows)))
    assert lines == ["Made 1 other tool call (reading files, running commands)"]
