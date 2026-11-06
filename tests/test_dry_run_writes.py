"""QAR_DRY_RUN_WRITES: reads pass, every write is blocked and recorded, the deep run is stubbed, nothing leaves the process.

The process-wide guard is installed once per test and removed after it; every request here is refused or answered before a socket opens."""
import os
import urllib.request

import pytest

from quest_ai_runner.the_world.tools import dry_run_writes as dry
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setenv(dry.ENV, "1")
    monkeypatch.setenv(dry.SCRATCH_ENV, str(tmp_path / "scratch"))
    dry.STATE["scratch"] = None
    dry.RECORDER.clear()
    assert dry.install()
    yield tmp_path
    dry.uninstall()
    dry.STATE["scratch"] = None


def test_unset_changes_nothing(monkeypatch):
    monkeypatch.delenv(dry.ENV, raising=False)
    assert dry.install() is False and not dry.STATE["installed"]
    assert dry.quest_exchange("POST", "/api/assistant-tasks", {"text": "x"}) is None
    spec = ToolSpec(name="make_thing", description="d", handler=lambda a, c: ToolResult(ok=True, text="ran"), mutates=True)
    assert ToolRegistry([spec]).invoke("make_thing", {}).text == "ran"
    runner = object()
    assert dry.wrap_deep(runner) is runner


def test_quest_reads_pass_and_writes_are_answered_and_recorded(on):
    assert dry.quest_exchange("GET", "/api/quests/me") is None
    status, payload = dry.quest_exchange("POST", "/api/assistant-tasks", {"text": "buy milk"})
    assert status == 200 and payload["dry_run"] is True
    assert dry.quest_exchange("PATCH", "/api/quests/q1/field", {"a": 1})[0] == 200
    assert dry.RECORDER.reads["quest /api/quests/me"] == 1
    assert [w["kind"] for w in dry.RECORDER.writes] == ["quest api", "quest api"]
    assert "POST /api/assistant-tasks" in dry.RECORDER.lines()[0]


def test_tools_that_mutate_are_recorded_not_run(on):
    ran = []
    reader = ToolSpec(name="look", description="d", mutates=False, handler=lambda a, c: ran.append("look") or ToolResult(ok=True, text="seen"))
    writer = ToolSpec(name="change", description="d", mutates=True, handler=lambda a, c: ran.append("change") or ToolResult(ok=True, text="did"))
    registry = ToolRegistry([reader, writer])
    assert registry.invoke("look", {}, ToolContext()).text == "seen"
    assert registry.invoke("change", {}, ToolContext()).ok
    assert ran == ["look"]
    assert dry.RECORDER.reads["tool look"] == 1 and dry.RECORDER.writes[0]["tool"] == "change"


def test_http_writes_are_refused_and_reads_pass_the_guard(on):
    with pytest.raises(dry.DryRunBlocked):
        urllib.request.urlopen(urllib.request.Request("http://example.invalid/x", data=b"{}", method="POST"))
    with pytest.raises(dry.DryRunBlocked):
        urllib.request.urlopen(urllib.request.Request("http://example.invalid/x", method="DELETE"))
    assert [w["kind"] for w in dry.RECORDER.writes] == ["http", "http"]
    before = len(dry.RECORDER.writes)
    with pytest.raises(Exception) as caught:        # a GET is let through to the network, which is unreachable here
        urllib.request.urlopen("http://example.invalid/x", timeout=1)
    assert not isinstance(caught.value, dry.DryRunBlocked) and len(dry.RECORDER.writes) == before


def test_read_only_posts_are_the_listed_ones_only():
    assert dry.is_read_post("api.notion.com", "/v1/databases/abcdef0123456789/query")
    assert dry.is_read_post("localhost", "/collections/c/points/search")
    assert not dry.is_read_post("localhost", "/collections/c/points")
    assert not dry.is_read_post("api.notion.com", "/v1/pages")


def test_file_writes_go_to_a_scratch_copy(on, tmp_path):
    outside = os.path.expanduser("~/dry_run_probe_should_not_exist.txt")
    with open(outside, "w") as handle:
        handle.write("x")
    assert not os.path.exists(outside)
    assert dry.RECORDER.writes[0]["kind"] == "file"
    with pytest.raises(dry.DryRunBlocked):
        os.remove(outside)
    scratch_file = tmp_path / "scratch" / "ok.txt"
    scratch_file.parent.mkdir(parents=True, exist_ok=True)
    scratch_file.write_text("fine")
    assert scratch_file.read_text() == "fine"


def test_subprocess_only_the_toolless_model_cli(on):
    import subprocess
    with pytest.raises(dry.DryRunBlocked):
        subprocess.run(["touch", "/tmp/dry-run-probe-never"])
    with pytest.raises(dry.DryRunBlocked):
        dry.check_process(["claude", "-p", "--dangerously-skip-permissions", "--tools", ""])
    with pytest.raises(dry.DryRunBlocked):
        dry.check_process(["claude", "-p", "--output-format", "json"])
    dry.check_process(["git", "-C", "/some/repo", "rev-parse", "--short", "HEAD"])
    with pytest.raises(dry.DryRunBlocked):
        dry.check_process(["git", "push", "origin", "main"])
    dry.check_process(["claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config"])
    assert dry.RECORDER.model_calls == 1


def test_the_deep_run_is_stubbed(on):
    pytest.importorskip("hubofhubs")
    class Real:
        def run_goal(self, **kw):
            raise AssertionError("the real worker must not run")
    stub = dry.wrap_deep(Real())
    result = stub.run_goal(goal="g", brief="the brief")
    assert result.met and result.deferred
    assert dry.RECORDER.writes[0]["kind"] == "deep run" and dry.RECORDER.briefs[0]["brief"] == "the brief"
    assert set(dry.wrap_deep({"a": Real()})) == {"a"}
