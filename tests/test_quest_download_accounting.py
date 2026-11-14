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


def test_task_count_reads_ask_the_server_to_filter_and_still_filter_the_rows():
    """The budget count must not download every finished task: created_after/task_kind/source go to the server, and an older
    server that ignores them (answers a superset) gives the same answer."""
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient

    c = QuestClient("http://x", "qsk_test", team_id="team_1")
    seen = {}

    def fake(method, path, **kw):
        seen.update(kw["params"])
        return {"tasks": [{"id": "new", "created_at": "2026-11-13T10:00:00Z", "task_kind": "autopilot_work"},
                          {"id": "old", "created_at": "2026-11-01T10:00:00Z", "task_kind": "autopilot_work"},
                          {"id": "other", "created_at": "2026-11-13T10:00:00Z", "task_kind": "plain"}]}
    c._request = fake
    got = c.list_tasks(status="done", task_kind="autopilot_work", created_after="2026-11-12")
    assert seen["created_after"] == "2026-11-12" and seen["task_kind"] == "autopilot_work" and seen["status"] == "done"
    assert [t["id"] for t in got] == ["new"]


def test_slim_task_lists_ask_the_server_to_leave_out_progress_and_result():
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient

    c = QuestClient("http://x", "qsk_test", team_id="team_1")
    seen = []
    c._request = lambda method, path, **kw: seen.append(dict(kw["params"])) or {"tasks": []}
    c.list_tasks(status="done", slim=True)
    c.list_tasks(status="done")
    assert seen[0]["slim"] == "true" and "slim" not in seen[1]       # opt-in: a caller that wants results still gets them


def test_an_unchanged_task_list_is_answered_from_the_kept_copy():
    """The server says unchanged and sends no rows; the client returns what it kept, and a changed list replaces it."""
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient

    c = QuestClient("http://x", "qsk_test", team_id="team_1")
    asked = []
    answers = [{"tasks": [{"task_id": "a"}], "etag": "e1"},
               {"tasks": [], "unchanged": True, "etag": "e1"},
               {"tasks": [{"task_id": "a"}, {"task_id": "b"}], "etag": "e2"}]
    c._request = lambda method, path, **kw: asked.append(dict(kw["params"])) or answers.pop(0)
    q = {"status": "queued", "for_execution": "true", "team_id": "team_1"}
    assert [t["task_id"] for t in c.discover_due(team_id="team_1")] == ["a"]
    assert "want_etag" in asked[0] and "if_none_match" not in asked[0]
    got = c.discover_due(team_id="team_1")                               # due_before differs; the question is the same
    assert [t["task_id"] for t in got] == ["a"] and asked[1]["if_none_match"] == "e1"
    got.append({"task_id": "mutated"})                                    # a caller's edits never reach the kept copy
    assert [t["task_id"] for t in c.discover_due(team_id="team_1")] == ["a", "b"]
    assert asked[2]["if_none_match"] == "e1"


def test_a_server_without_etags_is_used_as_before():
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient

    c = QuestClient("http://x", "qsk_test", team_id="team_1")
    c._request = lambda method, path, **kw: {"tasks": [{"task_id": "a"}]}
    assert [t["task_id"] for t in c.discover_due(team_id="team_1")] == ["a"]
    assert [t["task_id"] for t in c.discover_due(team_id="team_1")] == ["a"]
    assert c.__dict__.get("task_list_cache") == {}
