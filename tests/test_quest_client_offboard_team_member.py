"""QuestClient.offboard_team_member -- POST /api/teams/{team_id}/members/{user_id}/offboard."""
from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient


def test_offboard_posts_mode_receiver_and_dry_run():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured.update(method=method, path=path, body=body)
        return {"ok": True, "dry_run": True}

    client._request = fake_request  # type: ignore[assignment]
    client.offboard_team_member("team_1", "user_9", receiver_user_id="user_2", dry_run=True)
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/teams/team_1/members/user_9/offboard"
    assert captured["body"] == {"mode": "remove", "dry_run": True, "rep_action": "disable", "receiver_user_id": "user_2"}


def test_offboard_omits_receiver_so_the_team_manager_is_the_default():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    seen = {}
    client._request = lambda method, path, *, params=None, body=None: seen.update(body=body) or {"ok": True}  # type: ignore[assignment]
    client.offboard_team_member("team_1", "user_9", mode="archive")
    assert "receiver_user_id" not in seen["body"] and seen["body"]["mode"] == "archive"
