"""QuestClient daily reflection notes: list, save (tagged for the reflection's date) and edit."""
from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient


def client_with(responses):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls = []

    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        calls.append(dict(method=method, path=path, params=params, body=body))
        return responses.get((method, path), {})

    client._request = fake_request  # type: ignore[assignment]
    return client, calls


def test_list_reads_the_days_notes():
    notes = [{"note_id": "n1", "quest_id": "q1", "quote": "I did it."}]
    client, calls = client_with({("GET", "/api/daily-plan/reflection-notes"): {"notes": notes}})
    assert client.list_daily_reflection_notes("2026-11-23") == notes
    assert calls[0]["params"] == {"date": "2026-11-23"}


def test_save_tags_the_note_for_the_reflection_date_and_returns_its_id():
    saved = [{"note_id": "old", "text": "x"}, {"note_id": "n9", "text": "I finished it.", "source_date": "2026-11-23"}]
    client, calls = client_with({("POST", "/api/quests/q1/notes"): saved})
    result = client.save_daily_reflection_note("q1", "I finished it.", date="2026-11-23", author_label="Call notes")
    body = calls[0]["body"]
    assert body["source"] == "daily_reflection" and body["source_date"] == "2026-11-23"
    assert body["text"] == "I finished it." and body["author_label"] == "Call notes"
    assert result.id == "n9"


def test_plain_note_has_no_source_fields():
    client, calls = client_with({})
    client.add_quest_note("q1", "plain")
    assert "source" not in calls[0]["body"] and "source_date" not in calls[0]["body"]


def test_edit_changes_only_the_text():
    client, calls = client_with({})
    client.edit_daily_reflection_note("q1", "n9", "better")
    assert calls[0]["method"] == "PATCH" and calls[0]["path"] == "/api/quests/q1/notes/n9"
    assert calls[0]["body"] == {"text": "better"}
