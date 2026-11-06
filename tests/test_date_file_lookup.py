"""Finding files by the date in their name: the resolver, the server find, the map section, the client and the code gate. Offline."""
from __future__ import annotations

import threading
from datetime import date, timedelta

import pytest

from quest_ai_runner.the_world.tools import tool_guards
from quest_ai_runner.the_world.tools.connected_system_operations.composite_retrieval_adapter import CompositeRetrievalAdapter
from quest_ai_runner.the_world.tools.file_reading.date_references import (
    date_references_in, name_date, name_in_span, newest_dated, resolve_date_reference)
from quest_ai_runner.the_world.tools.file_reading.files_server import FileOperations, build_server
from quest_ai_runner.the_world.tools.file_reading.remote_files_adapter import RemoteFilesAdapter

TOKEN = "t" * 12 + "-test-token-0123456789"
FRIDAY = date(2026, 11, 6)


def test_relative_references_resolve_from_today():
    assert resolve_date_reference("yesterday", FRIDAY) == (date(2026, 11, 5), date(2026, 11, 5))
    assert resolve_date_reference("last Thursday", FRIDAY) == (date(2026, 11, 5), date(2026, 11, 5))
    assert resolve_date_reference("last friday", FRIDAY) == (date(2026, 10, 30), date(2026, 10, 30))
    assert resolve_date_reference("this week", FRIDAY) == (date(2026, 11, 2), date(2026, 11, 8))
    assert resolve_date_reference("last week", FRIDAY) == (date(2026, 10, 26), date(2026, 11, 1))
    assert resolve_date_reference("3 days ago", FRIDAY) == (date(2026, 11, 3), date(2026, 11, 3))
    assert resolve_date_reference("2026-11-01..2026-11-03", FRIDAY) == (date(2026, 11, 1), date(2026, 11, 3))
    assert resolve_date_reference("sometime", FRIDAY) is None


def test_references_are_read_from_the_persons_message():
    found = date_references_in("our meeting transcript yesterday for the office hour", FRIDAY)
    assert [phrase for phrase, _ in found] == ["yesterday"]
    assert date_references_in("what do you suggest next?", FRIDAY) == []


def test_name_dates():
    assert name_date("2026-11-05-office-hour-abc.txt") == date(2026, 11, 5)
    assert name_date("notes-12345.txt") is None
    assert name_in_span("a/b/2026-11-05-x.txt", (date(2026, 11, 5), date(2026, 11, 5)))
    assert not name_in_span("2026-11-04-x.txt", (date(2026, 11, 5), date(2026, 11, 5)))


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "served"
    folder = root / "meetings" / "transcripts"
    folder.mkdir(parents=True)
    for name in ("2026-11-03-weekly.txt", "2026-11-05-office-hour.txt", "2026-11-05-accountability.txt", "2026-10-20-old.txt"):
        (folder / name).write_text("words")
    (root / "readme.md").write_text("hi")
    return root


def test_server_find_by_date_and_map_section(tree):
    ops = FileOperations(str(tree))
    found = ops.find("", None, None, "2026-11-05")
    assert sorted(r["path"] for r in found["results"]) == [
        "meetings/transcripts/2026-11-05-accountability.txt", "meetings/transcripts/2026-11-05-office-hour.txt"]
    span = ops.find("", None, None, "2026-11-03..2026-11-05")
    assert len(span["results"]) == 3
    assert ops.find("", None, None, "no idea")["ok"] is False
    # The map names the folder of date-named files even though it is three levels down.
    assert "meetings/transcripts/ (4 files)" in ops.tree_map()["map"]


@pytest.fixture()
def remote(tree):
    server = build_server(str(tree), TOKEN, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    yield RemoteFilesAdapter(alias="box", base_url=f"http://{host}:{port}", token=TOKEN)
    server.shutdown()
    server.server_close()


def test_client_find_by_relative_date(remote):
    spec = {"files": "box", "op": "find", "date": "2026-11-04"}
    assert "no files matched" in remote.query(spec).text
    spec["date"] = "2026-11-05"
    text = remote.query(spec).text
    assert "2026-11-05-office-hour.txt" in text and "2026-11-03-weekly.txt" not in text
    assert remote.query({"files": "box", "op": "find", "date": "never"}).kind == "error"


def test_gate_builds_a_find_per_file_source_from_the_message(remote):
    retrieval = CompositeRetrievalAdapter([remote])
    reads = tool_guards.dated_file_reads_owed("the meeting transcript yesterday for the office hour", retrieval, FRIDAY)
    assert reads == [{"query": {"files": "box", "op": "find", "date": "yesterday", "max_results": 40}}]
    assert tool_guards.dated_file_reads_owed("what should Mary do next?", retrieval, FRIDAY) == []
    assert tool_guards.dated_file_reads_owed("yesterday", None, FRIDAY) == []


def test_newest_dated_needs_three_files_per_folder():
    rows = newest_dated(["a/2026-11-01-x", "a/2026-11-02-x", "a/2026-11-03-x", "b/2026-11-05-x"], FRIDAY)
    assert rows == [("a", ["2026-11-03-x", "2026-11-02-x", "2026-11-01-x"], 3)]


def test_the_loop_searches_by_date_before_asking_the_person_to_paste():
    """A planner that would answer "paste it" for a file named by yesterday's date runs the dated find first."""
    import json
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider

    calls = []

    def transport(url, headers, body, timeout):
        calls.append((url.rsplit("/", 1)[-1], json.loads(body)))
        return 200, json.dumps({"ok": True, "results": [{"path": "m/2026-11-05-office-hour.txt", "size": 5, "modified": "x"}]}).encode()

    remote = RemoteFilesAdapter(alias="box", base_url="http://example.invalid", token=TOKEN, transport=transport)
    ask = {"action": "answer", "user_intent": "ask", "rationale": "asks", "pending_read": []}
    provider = StubProvider([ask, ask])
    config = OrchestratorConfig()
    config.overseer = False
    orch = Orchestrator(retrieval=CompositeRetrievalAdapter([remote]), provider=provider, registry=ModelRegistry(provider), config=config)
    orch.run("what did we agree in the meeting transcript from yesterday?")
    finds = [body for op, body in calls if op == "find"]
    assert finds and finds[0]["pattern"] == f"*{(date.today() - timedelta(days=1)).isoformat()}*"
