import json
import os
import time

from quest_ai_runner.core.goal_runner import subagent_work_in_flight


def _write(path, recs):
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _use(i):
    return {"message": {"content": [{"type": "tool_use", "id": i, "name": "Agent", "input": {}}]}}


def _res(i):
    return {"message": {"content": [{"type": "tool_result", "tool_use_id": i}]}}


def test_pending_agent_call_is_not_idle(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_use("a")])
    assert subagent_work_in_flight(p)


def test_finished_agent_call_with_no_subagent_files_is_idle(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_use("a"), _res("a")])
    assert subagent_work_in_flight(p) is None


def test_fresh_subagent_file_is_not_idle(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_res("a")])
    d = tmp_path / "s" / "subagents"
    d.mkdir(parents=True)
    (d / "agent-x.jsonl").write_text("{}")
    assert subagent_work_in_flight(p)


def test_stale_subagent_file_is_idle(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_res("a")])
    d = tmp_path / "s" / "subagents"
    d.mkdir(parents=True)
    f = d / "agent-x.jsonl"
    f.write_text("{}")
    old = time.time() - 3600
    os.utime(f, (old, old))
    assert subagent_work_in_flight(p) is None
