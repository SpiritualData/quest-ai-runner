"""QuestClient's read-only scheduling methods: the owner's links and the bookings made on a link.

Same shape as test_quest_client_planning_and_reps.py: a fake ``_request`` captures the call, no network.
"""
import pytest

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestApiError, QuestClient, QuestNotConfigured


def client_with_response(response):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return response

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


BOOKING = {
    "id": "bk_1", "linkId": "lnk_1", "visitorName": "Ann", "visitorEmail": "ann@example.com",
    "fieldValues": {}, "start": "2026-11-20T15:00:00Z", "end": "2026-11-20T15:30:00Z",
    "status": "confirmed", "createdAt": "2026-11-18T09:00:00Z",
}


def test_list_link_bookings_is_a_get_on_the_bookings_route_and_returns_the_list():
    client, captured = client_with_response([BOOKING])
    assert client.list_link_bookings("lnk_1") == [BOOKING]
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/scheduling/links/lnk_1/bookings"
    assert captured["body"] is None


def test_list_link_bookings_empty_list_means_no_bookings():
    client, _ = client_with_response([])
    assert client.list_link_bookings("lnk_1") == []


def test_list_link_bookings_non_list_response_is_an_error_not_zero():
    client, _ = client_with_response({"detail": "unexpected"})
    with pytest.raises(QuestApiError):
        client.list_link_bookings("lnk_1")


def test_list_link_bookings_requires_a_link_id():
    client, captured = client_with_response([])
    with pytest.raises(QuestNotConfigured):
        client.list_link_bookings("")
    assert captured == {}


def test_list_link_bookings_is_not_configured_without_key():
    client = QuestClient("", "", team_id="team_1")
    with pytest.raises(QuestNotConfigured):
        client.list_link_bookings("lnk_1")


def test_list_link_bookings_api_error_propagates():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(method, path, *, params=None, body=None):
        raise QuestApiError(f"Quest API {method} {path} -> 404: not found")

    client._request = failing_request  # type: ignore[assignment]
    with pytest.raises(QuestApiError):
        client.list_link_bookings("lnk_1")


def test_list_scheduling_links_hits_the_links_route():
    links = [{"id": "lnk_1", "slug": "super-psychic-academy-intro-6g3408", "isActive": True}]
    client, captured = client_with_response(links)
    assert client.list_scheduling_links() == links
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/scheduling/links"
