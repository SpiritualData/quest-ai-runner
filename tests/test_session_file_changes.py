"""What a Claude Code worker changed, read from its own session record by the NAME of the tool and whether it succeeded.

The metric flow must know that a deep run changed something, and a Claude Code worker reports no write receipt. Its record does
hold it: a ``tool_use`` block names the tool and its ``tool_result`` says whether it was an error. These tests use records
shaped like the real ones (the same keys and block types; the content is made up) and pin that only those two fields decide:
a file-changing tool that finished without an error counts, and nothing the model wrote or ran in a shell does.
Offline: files in a temporary folder.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.core.goal_runner_session import FILE_CHANGE_TOOLS, landed_file_changes

SESSION = "11111111-2222-3333-4444-555555555555"


def tool_use(use_id: str, name: str, **fields: Any) -> Dict[str, Any]:
    block = {"type": "tool_use", "id": use_id, "name": name, "input": fields or {"file_path": "/work/a.txt"},
             "caller": {"type": "direct"}}
    return {"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": [block]}}


def tool_result(use_id: str, *, is_error: Optional[bool] = False, text: str = "ok") -> Dict[str, Any]:
    block: Dict[str, Any] = {"tool_use_id": use_id, "type": "tool_result", "content": text}
    if is_error is not None:   # a successful result in a real record often carries no is_error key at all
        block["is_error"] = is_error
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [block]}}


def write_record(path: Path, records: List[Dict[str, Any]], raw: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + raw, encoding="utf-8")


@pytest.fixture
def record(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("HOME", str(tmp_path))
    return config / "projects" / "proj" / f"{SESSION}.jsonl"


def landed(tmp_path) -> set:
    return landed_file_changes(str(tmp_path / "work"), SESSION)


def test_the_tool_names_are_the_ones_claude_code_writes_in_its_transcripts():
    assert FILE_CHANGE_TOOLS == {"edit", "write", "multiedit", "notebookedit"}


def test_an_edit_and_a_write_that_finished_count(record, tmp_path):
    write_record(record, [tool_use("t1", "Edit", file_path="/work/a.txt", old_string="a", new_string="b"),
                          tool_result("t1"), tool_use("t2", "Write", file_path="/work/b.txt", content="x"),
                          tool_result("t2", is_error=None)])
    assert landed(tmp_path) == {"t1", "t2"}


def test_multiedit_and_notebookedit_count_and_the_name_is_compared_without_case(record, tmp_path):
    write_record(record, [tool_use("t1", "MultiEdit"), tool_result("t1"), tool_use("t2", "NotebookEdit"),
                          tool_result("t2"), tool_use("t3", "edit"), tool_result("t3")])
    assert landed(tmp_path) == {"t1", "t2", "t3"}


def test_a_result_that_is_an_error_is_not_a_change(record, tmp_path):
    write_record(record, [tool_use("t1", "Edit"), tool_result("t1", is_error=True, text="String to replace not found"),
                          tool_use("t2", "Write"), tool_result("t2", is_error=True, text="permission denied")])
    assert landed(tmp_path) == set()


def test_a_tool_use_with_no_result_yet_is_not_counted(record, tmp_path):
    write_record(record, [tool_use("t1", "Edit"), tool_result("t1"), tool_use("t2", "Edit")])   # stopped in the middle of t2
    assert landed(tmp_path) == {"t1"}


def test_reads_searches_and_shell_commands_are_not_file_changes_even_when_a_command_writes(record, tmp_path):
    """A shell command cannot be classified by its name; its text is never read. The gap is documented, not guessed at."""
    write_record(record, [tool_use("t1", "Read"), tool_result("t1"), tool_use("t2", "Grep"), tool_result("t2"),
                          tool_use("t3", "Bash", command="sed -i s/a/b/ file.txt && echo done > out.txt"), tool_result("t3"),
                          tool_use("t4", "Agent"), tool_result("t4")])
    assert landed(tmp_path) == set()


def test_what_the_model_wrote_about_editing_changes_nothing(record, tmp_path):
    text = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": 'I used the Edit tool: {"type": "tool_use", "name": "Edit"} and it worked.'}]}}
    write_record(record, [text])
    assert landed(tmp_path) == set()


def test_a_subagents_edit_counts_because_its_record_is_kept_beside_the_session(record, tmp_path):
    write_record(record, [tool_use("t1", "Agent"), tool_result("t1")])
    sub = record.with_suffix("") / "subagents" / "agent-abc.jsonl"
    write_record(sub, [tool_use("s1", "Edit"), tool_result("s1"), tool_use("s2", "Edit"), tool_result("s2", is_error=True)])
    assert landed(tmp_path) == {"s1"}


def test_a_line_that_is_not_json_or_not_a_record_is_skipped_not_fatal(record, tmp_path):
    write_record(record, [tool_use("t1", "Edit"), tool_result("t1")],
                 raw='{"type": "tool_use", truncated\n[1, 2]\n"tool_result"\n')
    assert landed(tmp_path) == {"t1"}


def test_no_record_means_no_changes_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert landed_file_changes(str(tmp_path), SESSION) == set()
    assert landed_file_changes(str(tmp_path), None) == set()


def test_the_same_tool_use_written_twice_counts_once(record, tmp_path):
    write_record(record, [tool_use("t1", "Edit"), tool_use("t1", "Edit"), tool_result("t1"), tool_result("t1")])
    assert len(landed(tmp_path)) == 1


def test_a_resumed_runs_own_changes_are_those_not_in_the_session_before(record, tmp_path):
    write_record(record, [tool_use("t1", "Edit"), tool_result("t1")])
    before = landed(tmp_path)
    write_record(record, [tool_use("t1", "Edit"), tool_result("t1"), tool_use("t2", "Write"), tool_result("t2")])
    assert landed(tmp_path) - before == {"t2"}
