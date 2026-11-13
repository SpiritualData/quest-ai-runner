"""Download accounting in the Quest client transport: ids collapse, bytes add up, a report is due on the interval."""
from quest_ai_runner.the_body.internal_context.quest.client import quest_client_transport as t


def test_endpoint_key_collapses_ids_and_drops_the_query():
    assert t.endpoint_key("GET", "/api/quests/quest_0123456789ab/goals?x=1") == "GET /api/quests/quest_*/goals"
    assert t.endpoint_key("GET", "/api/assistant-tasks") == "GET /api/assistant-tasks"


def test_report_names_the_biggest_endpoint_once_per_interval():
    t._downloads.clear()
    t._downloads_since[0] = 0.0
    assert t.record_download("GET", "/api/quests/quest_0123456789ab/goals", 3_000_000, now=10.0) is None
    t.record_download("GET", "/api/quests/quest_ba9876543210/goals", 1_000_000, now=20.0)
    t.record_download("GET", "/api/assistant-tasks", 500_000, now=30.0)
    line = t.record_download("GET", "/api/assistant-tasks", 500_000, now=t.DOWNLOAD_REPORT_SECONDS + 1)
    assert line.startswith("Quest API downloaded 5.00 MB")
    assert line.index("GET /api/quests/quest_*/goals x2 = 4.00 MB") < line.index("GET /api/assistant-tasks x2 = 1.00 MB")
    assert t.record_download("GET", "/api/assistant-tasks", 1, now=t.DOWNLOAD_REPORT_SECONDS + 2) is None   # counters reset
