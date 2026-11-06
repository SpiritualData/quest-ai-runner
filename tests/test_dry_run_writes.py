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


def blocked(method, path, body=None):
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import QuestApiError
    with pytest.raises(QuestApiError) as caught:
        dry.quest_exchange(method, path, body)
    return caught.value


def test_quest_reads_pass_and_writes_are_refused_and_recorded(on):
    assert dry.quest_exchange("GET", "/api/quests/me") is None
    error = blocked("POST", "/api/assistant-tasks", {"text": "buy milk"})
    assert error.status == 403 and "blocked in test mode" in str(error)
    assert blocked("PATCH", "/api/quests/q1/field", {"a": 1}).status == 403
    assert dry.RECORDER.reads["quest /api/quests/me"] == 1
    assert [w["kind"] for w in dry.RECORDER.writes] == ["quest api", "quest api"]
    assert "POST /api/assistant-tasks" in dry.RECORDER.lines()[0]


def test_tools_that_mutate_are_recorded_not_run(on):
    ran = []
    reader = ToolSpec(name="look", description="d", mutates=False, handler=lambda a, c: ran.append("look") or ToolResult(ok=True, text="seen"))
    writer = ToolSpec(name="change", description="d", mutates=True, handler=lambda a, c: ran.append("change") or ToolResult(ok=True, text="did"))
    registry = ToolRegistry([reader, writer])
    assert registry.invoke("look", {}, ToolContext()).text == "seen"
    refused = registry.invoke("change", {}, ToolContext())
    assert not refused.ok and "Blocked in test mode" in refused.text
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
    assert os.remove(outside) is None and dry.RECORDER.writes[-1]["internal"] is True       # housekeeping outside scratch: skipped quietly
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
    assert not result.met and result.exhausted and "Blocked in test mode" in result.output
    assert dry.RECORDER.writes[0]["kind"] == "deep run" and dry.RECORDER.briefs[0]["brief"] == "the brief"
    assert set(dry.wrap_deep({"a": Real()})) == {"a"}


def test_a_bounded_run_makes_only_the_allowed_write_for_real(on):
    ran = []
    def note(args, ctx):
        ran.append(("note", dry.quest_exchange("POST", "/api/quests/q1/notes", {"text": "t"})))   # the tool's own Quest call
        return ToolResult(ok=True, text="saved")
    allowed = ToolSpec(name="add_note", description="d", mutates=True, handler=note)
    other = ToolSpec(name="add_goal", description="d", mutates=True, handler=lambda a, c: ran.append("goal") or ToolResult(ok=True, text="did"))
    registry = ToolRegistry([allowed, other])
    dry.set_allowances([{"tool": "add_note", "args": {"quest_id": "q1"}}])
    try:
        assert registry.invoke("add_note", {"quest_id": "q1", "text": "hi"}, ToolContext()).text == "saved"
        assert ran == [("note", None)]                                  # the Quest call inside the allowed tool went through
        registry.invoke("add_note", {"quest_id": "other", "text": "hi"}, ToolContext())   # same tool, other quest: refused
        registry.invoke("add_goal", {}, ToolContext())
        assert len(ran) == 1 and [w["tool"] for w in dry.RECORDER.writes] == ["add_note", "add_goal"]
        assert blocked("POST", "/api/quests/q1/notes", {}).status == 403        # outside the allowed call: still refused
        dry.set_allowances([{"method": "POST", "path": "/api/planning/goals"}])
        assert dry.quest_exchange("POST", "/api/planning/goals", {}) is None and blocked("DELETE", "/api/planning/goals", {}).status == 403
        assert len(dry.RECORDER.snapshot()["performed"]) == 3
    finally:
        dry.set_allowances([])


def test_a_blocked_housekeeping_write_logged_by_its_caller_makes_no_noise(on, caplog):
    import logging
    log = logging.getLogger("dry-run-test")
    with caplog.at_level(logging.INFO):
        try:
            raise dry.DryRunBlocked("dry run: PUT x was not sent")
        except dry.DryRunBlocked:
            log.exception("card cache: could not write listing.json")
        log.warning("Quest card backend PUT /api/cards/a failed: DryRunBlocked: dry run")
        log.warning("an ordinary warning")
    assert [r.getMessage() for r in caplog.records] == ["an ordinary warning"]
    assert dry.RECORDER.snapshot()["skipped_housekeeping"] == 2


def test_model_calls_are_labelled_by_the_start_of_their_system_prompt(on):
    dry.check_process(["claude", "-p", "--system-prompt", "You plan the next step.  Choose one.", "--tools", ""])
    assert dry.RECORDER.snapshot()["model_log"][0]["label"].endswith("| You plan the next step. Choose one.")
