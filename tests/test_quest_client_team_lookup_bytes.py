"""The owning-team lookup of a one-shot process must not download the account's whole quest list.

Evidence 2026-11-20: ``tools call list_quest_goals`` fetched ``GET /api/quests/me`` (0.79 MB on the wire) in every fresh
process just to learn one quest's team; ``GET /api/quests/{id}/state`` carries ``state.team_id`` in ~17 KB, and a small
on-disk cache answers every later process with no request at all.
"""
import json
import urllib.error

import pytest

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient
from quest_ai_runner.body.internal_context.quest.client import quest_client_transport as transport


class Resp:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


@pytest.fixture
def urls(monkeypatch):
    seen = []

    def fake(req, timeout=None):
        url = req.full_url
        seen.append(url)
        if url.endswith("/api/quests/q1/state"):
            return Resp({"quest_id": "q1", "state": {"team_id": "team-owner"}})
        if url.endswith("/api/quests/me"):
            return Resp([{"quest_id": "q1", "team_id": "team-owner"}])
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(transport, "open_url", fake)
    return seen


def client():
    return QuestClient("http://quest.example", "qsk_test")


def test_team_is_read_from_the_one_quest_state_not_the_account_list(urls):
    assert client().owning_team_for("q1") == "team-owner"
    assert urls == ["http://quest.example/api/quests/q1/state"]


def test_a_quest_state_without_a_team_falls_back_to_the_account_list(monkeypatch, urls):
    def fake(req, timeout=None):
        urls.append(req.full_url)
        if req.full_url.endswith("/api/quests/q1/state"):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return Resp([{"quest_id": "q1", "team_id": "team-owner"}])

    monkeypatch.setattr(transport, "open_url", fake)
    assert client().owning_team_for("q1") == "team-owner"
    assert urls[-1].endswith("/api/quests/me")


def test_a_second_process_reads_the_team_from_disk_with_no_request(monkeypatch, tmp_path, urls):
    monkeypatch.setenv("QAR_QUEST_CACHE_DIR", str(tmp_path))
    assert client().owning_team_for("q1") == "team-owner"
    urls.clear()
    assert client().owning_team_for("q1") == "team-owner"      # a fresh client = a fresh process
    assert urls == []


def test_forgetting_the_team_forgets_it_on_disk_too(monkeypatch, tmp_path, urls):
    monkeypatch.setenv("QAR_QUEST_CACHE_DIR", str(tmp_path))
    first = client()
    first.owning_team_for("q1")
    assert first.forget_owning_team("q1") is True
    urls.clear()
    assert client().owning_team_for("q1") == "team-owner"
    assert len(urls) == 1, "re-resolved from the server after the forget"


def test_without_a_cache_dir_nothing_is_written(monkeypatch, tmp_path, urls):
    monkeypatch.delenv("QAR_QUEST_CACHE_DIR", raising=False)
    client().owning_team_for("q1")
    urls.clear()
    client().owning_team_for("q1")
    assert len(urls) == 1
