"""The two client calls around answered asks parked with "don't have my AI act on it yet"."""
from quest_ai_runner.body.internal_context.quest.client.quest_client_decisions import DecisionsMixin


class RecordingClient(DecisionsMixin):
    def __init__(self, response=None):
        self.response = response
        self.requests = []
        self.writes = []

    def _request(self, method, path, params=None, **kwargs):
        self.requests.append((method, path, params))
        return self.response

    def _write(self, method, path, body=None, label=""):
        self.writes.append((method, path, body))
        return "written"


def test_list_pending_answers_asks_for_resolved_pending_rows_and_returns_the_items():
    client = RecordingClient({"items": [{"decision_id": "d1"}], "next_cursor": None})
    assert client.list_pending_answers() == [{"decision_id": "d1"}]
    method, path, params = client.requests[0]
    assert (method, path) == ("GET", "/api/teams/decisions")
    assert params["ai_action_status"] == "pending"
    assert params["status"] == "resolved"


def test_list_pending_answers_is_empty_when_nothing_comes_back():
    assert RecordingClient(None).list_pending_answers() == []


def test_run_pending_answers_runs_everything_without_a_quest_and_one_quest_with_it():
    client = RecordingClient()
    client.run_pending_answers()
    client.run_pending_answers(quest_id="quest_1")
    assert client.writes == [
        ("POST", "/api/teams/decisions/run-pending", {}),
        ("POST", "/api/teams/decisions/run-pending", {"quest_id": "quest_1"}),
    ]
