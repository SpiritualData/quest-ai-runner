"""A ``tools call`` process writes one line of download accounting when it exits, so one-shot shell calls show up in the data-usage log."""
import json

from quest_ai_runner.body.internal_context.quest.client import quest_client_transport as transport
from quest_ai_runner.consciousness.terminal import __main__ as tools_main


def test_summary_line_names_tool_total_and_top_endpoints():
    transport.reset_process_downloads()
    transport.record_download("GET", "/api/quests/me", 700_000)
    transport.record_download("GET", "/api/teams/team_abc/quests/quest_1/goals", 1_200)
    line = transport.process_download_line("list_quest_goals")
    assert line.startswith("tools call list_quest_goals downloaded 701200 bytes in 2 requests")
    assert "GET /api/quests/me x1 = 700000" in line
    assert line.index("/api/quests/me") < line.index("quest_1/goals")


def test_no_requests_no_line():
    transport.reset_process_downloads()
    assert transport.process_download_line("x") == ""


def test_main_appends_the_line_to_the_log_file(monkeypatch, tmp_path):
    log_file = tmp_path / "tool_downloads.log"
    monkeypatch.setenv("QAR_TOOL_DOWNLOAD_LOG", str(log_file))
    transport.reset_process_downloads()

    class Registry:
        def invoke(self, name, args, ctx):
            transport.record_download("GET", "/api/asks", 2_325)
            from quest_ai_runner.world.tools.tools import ToolResult
            return ToolResult(ok=True, text="fine")

    monkeypatch.setattr(tools_main, "build_tool_registry", lambda: Registry())
    assert tools_main.main(["call", "list_asks"]) == 0
    text = log_file.read_text()
    assert "list_asks downloaded 2325 bytes in 1 requests" in text and "GET /api/asks x1 = 2325" in text
    assert text.count("\n") == 1
