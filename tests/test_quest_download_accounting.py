"""Download accounting in the Quest client transport: ids collapse, bytes add up, a report is due on the interval."""
from quest_ai_runner.body.internal_context.quest.client import quest_client_transport as t


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
    from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient

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
    from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient

    c = QuestClient("http://x", "qsk_test", team_id="team_1")
    seen = []
    c._request = lambda method, path, **kw: seen.append(dict(kw["params"])) or {"tasks": []}
    c.list_tasks(status="done", slim=True)
    c.list_tasks(status="done")
    assert seen[0]["slim"] == "true" and "slim" not in seen[1]       # opt-in: a caller that wants results still gets them


def test_a_list_read_for_results_asks_the_server_to_leave_out_only_the_progress_logs():
    """An autopilot pass reads a quest's tasks for their results. The progress logs were nearly all of a 3,064 KB reply for nine
    tasks on 2026-11-16, so that read asks for the list without them; an older server that still sends them is cut down here."""
    from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient

    c = QuestClient("http://x", "qsk_test", team_id="team_1")
    seen = []
    c._request = lambda method, path, **kw: seen.append(dict(kw["params"])) or {
        "tasks": [{"id": "a", "result": "what the run produced", "progress": [{"text": "step"}] * 3}]}
    got = c.list_tasks(goal_id="quest_1", without_progress=True)
    assert seen[0]["without_progress"] == "true" and "slim" not in seen[0]
    assert got == [{"id": "a", "result": "what the run produced"}]
    c.list_tasks(goal_id="quest_2", without_progress=False)
    assert "without_progress" not in seen[1]                           # a caller that reads the log asks for it by name


def test_the_autopilot_pass_reads_prior_tasks_without_their_progress_logs():
    import inspect

    from quest_ai_runner.intelligence.proactivity import autopilot

    source = inspect.getsource(autopilot)
    read = source[source.index("quest_tasks: Optional[List[Dict[str, Any]]] = self._client.list_tasks("):]
    assert "without_progress=True" in read[:read.index(") or []")]


def test_an_unchanged_task_list_is_answered_from_the_kept_copy():
    """The server says unchanged and sends no rows; the client returns what it kept, and a changed list replaces it."""
    from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient

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
    from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient

    c = QuestClient("http://no-etags", "qsk_test", team_id="team_1")
    c._request = lambda method, path, **kw: {"tasks": [{"task_id": "a"}]}
    assert [t["task_id"] for t in c.discover_due(team_id="team_1")] == ["a"]
    assert [t["task_id"] for t in c.discover_due(team_id="team_1")] == ["a"]
    from quest_ai_runner.body.internal_context.quest.client.quest_client_read_cache import READ_CACHE
    scope = READ_CACHE.scope_of(c)
    assert not [key for key in READ_CACHE.etags if key[0] == scope]      # no etag in the reply, so none is kept


def test_the_run_history_read_leaves_out_progress_logs():
    """The run history uses only status, dates, title and result. Its unfiltered read of a quest's tasks was one 59 MB reply,
    sent twice, on the shared lane on 2026-11-16 (nearly all progress logs), so it asks for the list without them."""
    import inspect

    from quest_ai_runner.consciousness.request_intake import executor_context

    source = inspect.getsource(executor_context)
    read = source[source.index("def _fetch_run_history"):]
    assert "without_progress=True" in read[:read.index("except Exception")]


def test_recurring_adoption_and_handover_reads_are_slim():
    """Reading the quest's queued tasks to adopt a recurring one, or to count handed-over ones, needs no progress log or result text."""
    from quest_ai_runner.intelligence.proactivity.autopilot_context import PassContextMixin
    from quest_ai_runner.body.hands.quest_operations import tools_quest_work as w
    import inspect

    seen = {}

    class C:
        def list_tasks(self, **kw):
            seen.update(kw)
            return []

    class Probe(PassContextMixin):
        _client = C()
        def _team_for(self, q): return "team_1"
        def _is_autopilot_authored(self, t): return False
        def _now(self):
            import datetime
            return datetime.datetime.now()

    Probe()._due_recurring_tasks("quest_1")
    assert seen.get("slim") is True
    assert "slim=True" in inspect.getsource(w).split("client.list_tasks(goal_id=quest_id")[1].split("\n")[0]


def test_a_quest_task_list_leaves_out_progress_logs_unless_a_caller_asks_for_them():
    """The default read of one quest's tasks carried every progress log: 52.1 MB in one reply (2026-11-24, production access log)."""
    from quest_ai_runner.body.internal_context.quest.client.quest_client_tasks import TasksMixin

    class Client(TasksMixin):
        team_id = None

        def __init__(self):
            self.asked = []

        def get_task_list(self, params):
            self.asked.append(dict(params))
            return [{"id": "t1", "text": "marker", "result": "r", "progress": [{"text": "line"}]}]

    client = Client()
    assert client.list_tasks_or_none(goal_id="q1") == [{"id": "t1", "text": "marker", "result": "r"}]
    assert client.list_tasks(goal_id="q1") == [{"id": "t1", "text": "marker", "result": "r"}]
    assert [p.get("without_progress") for p in client.asked] == ["true", "true"]
    assert client.list_tasks(goal_id="q1", without_progress=False)[0]["progress"] and "without_progress" not in client.asked[-1]
