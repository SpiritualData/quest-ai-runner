"""QuestClient.list_org_reflection_notes -- GET /api/orgs/{org_id}/reflection-notes."""
import inspect

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient


def client_returning(response):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured.update(method=method, path=path, params=params)
        return response

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_signature_names_the_range_and_filters():
    names = list(inspect.signature(QuestClient.list_org_reflection_notes).parameters)
    for name in ("org_id", "member_user_ids", "start_date", "end_date", "levels", "summary", "limit", "offset"):
        assert name in names


def test_sends_filters_and_unwraps_the_data_envelope():
    client, captured = client_returning({"success": True, "data": {"notes": [], "total": 0}})
    out = client.list_org_reflection_notes(
        "org_1", member_user_ids=["u1", "u2"], start_date="2026-11-02", end_date="2026-11-08",
        levels=["daily", "weekly"], summary=True, limit=50, offset=10)
    assert out == {"notes": [], "total": 0}
    assert captured["method"] == "GET" and captured["path"] == "/api/orgs/org_1/reflection-notes"
    assert captured["params"] == {
        "summary": "true", "limit": 50, "offset": 10, "member_user_id": ["u1", "u2"],
        "level": ["daily", "weekly"], "start_date": "2026-11-02", "end_date": "2026-11-08"}


def test_omits_unset_filters():
    client, captured = client_returning({"data": {"notes": []}})
    client.list_org_reflection_notes("org_1")
    assert captured["params"] == {"summary": "false", "limit": 100, "offset": 0}
