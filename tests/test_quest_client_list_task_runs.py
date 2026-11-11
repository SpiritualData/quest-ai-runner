"""``QuestClient.list_task_runs`` reads one task's runs (newest first) from the paged runs route and never raises."""
from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestApiError, QuestClient, QuestNotConfigured
from quest_ai_runner.the_consciousness.terminal.cli_quest import _quest_method_is_read_only


def _client(response=None, error=None):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        captured.update(method=method, path=path, params=params)
        if error:
            raise error
        return response

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_reads_the_runs_route_with_the_paging_params_and_returns_the_runs_list():
    runs = [{"run_id": "r2", "status": "done"}, {"run_id": "r1", "status": "failed"}]
    client, captured = _client({"runs": runs, "count": 2, "total": 9, "limit": 5, "offset": 4})
    assert client.list_task_runs("task_1", limit=5, offset=4) == runs
    assert captured == {"method": "GET", "path": "/api/assistant-tasks/task_1/runs", "params": {"limit": 5, "offset": 4}}


def test_defaults_to_the_first_twenty():
    client, captured = _client({"runs": []})
    assert client.list_task_runs("task_1") == [] and captured["params"] == {"limit": 20, "offset": 0}


def test_a_failed_read_or_an_odd_answer_is_an_empty_list():
    for error in (QuestApiError("boom"), QuestNotConfigured("no key")):
        assert _client(error=error)[0].list_task_runs("task_1") == []
    assert _client(["not", "a", "dict"])[0].list_task_runs("task_1") == []
    assert _client({"runs": None})[0].list_task_runs("task_1") == []


def test_the_quest_command_lists_it_as_a_read_method():
    assert _quest_method_is_read_only("list_task_runs")
