"""Socket-level transport errors must become QuestApiError, never escape raw.

Real-world failure (2026-07): the fast lane's ``wait_for_interactive`` long-poll documents
"Never raises", but a server that holds past the padded socket timeout makes ``urlopen``'s
read raise a RAW ``TimeoutError`` — which is neither ``HTTPError`` nor ``URLError``, so
``_request`` let it escape, every "never raises" caller contract broke, and the poller logged
a full traceback ("fast lane iteration failed") every iteration instead of retrying calmly.
"""
import urllib.request

import pytest

from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestApiError, QuestClient


def make_client():
    return QuestClient("https://quest.example", "test-api-key", team_id="team_1")


def test_request_wraps_socket_timeout_as_quest_api_error(monkeypatch):
    def raising_urlopen(req, timeout=None):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", raising_urlopen)
    with pytest.raises(QuestApiError, match="transport error"):
        make_client()._request("GET", "/api/assistant-tasks")


def test_request_wraps_connection_reset_as_quest_api_error(monkeypatch):
    def raising_urlopen(req, timeout=None):
        raise ConnectionResetError("Connection reset by peer")

    monkeypatch.setattr(urllib.request, "urlopen", raising_urlopen)
    with pytest.raises(QuestApiError, match="transport error"):
        make_client()._request("GET", "/api/assistant-tasks")


def test_wait_for_interactive_swallows_socket_timeout(monkeypatch):
    def raising_urlopen(req, timeout=None):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", raising_urlopen)
    # The documented contract: an empty/timed-out/errored wait returns None, never raises.
    assert make_client().wait_for_interactive(timeout=0.1) is None


def test_a_path_with_no_id_is_refused_before_sending(monkeypatch):
    sent = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req))
    for path in ("/api/quests/None/state", "/api/quests//state", "/api/quests/undefined/goals", "/api/quests/null"):
        with pytest.raises(QuestApiError, match="has no id"):
            make_client()._request("GET", path)
    assert sent == []


def test_a_filled_path_and_a_trailing_slash_are_not_refused():
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client_transport import missing_id_segment
    assert missing_id_segment("/api/quests/quest_1/state") is None
    assert missing_id_segment("/api/assistant-tasks/") is None
    assert missing_id_segment("/api/quests/quest_1/state?x=None") is None
