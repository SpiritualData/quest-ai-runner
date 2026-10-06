"""QuestClient.update_outcome_progress: the AI-actor progress write and how a refusal surfaces.

Same fake-transport pattern as test_quest_client_goal_updates.py: a fake ``_request`` captures the
call, no network. The CLI test drives ``quest-ai-runner quest update_outcome_progress`` end to end
against that fake to prove the --write gate and that the refusal text reaches stderr verbatim.
"""
import pytest

from quest_ai_runner import cli
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient

REFUSAL = ("A progress note must name concrete evidence (a goal, file, metric or result), "
           "not a generic line such as 'making progress'.")


def client_with(handler):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    client._request = handler  # type: ignore[assignment]
    return client


def test_posts_to_the_ai_progress_route_with_numbers_and_returns_the_outcome():
    captured = {}

    def handler(method, path, *, params=None, body=None, timeout_override=None):
        captured.update(method=method, path=path, body=body)
        return {"quest_id": "q1", "outcome": {"id": "o1", "progress_pct": 40.0}}

    result = client_with(handler).update_outcome_progress(
        "q1", "o1", "40", "List export shows 400 subscribers", current_value="400")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/quests/q1/measurable-outcomes/o1/ai-progress"
    assert captured["body"] == {"progress_pct": 40.0, "note": "List export shows 400 subscribers",
                                "current_value": 400.0}
    assert result == {"id": "o1", "progress_pct": 40.0}


def test_current_value_is_omitted_when_not_given():
    captured = {}

    def handler(method, path, *, params=None, body=None, timeout_override=None):
        captured["body"] = body
        return {"outcome": {}}

    client_with(handler).update_outcome_progress("q1", "o1", 10, "Two partners signed")
    assert "current_value" not in captured["body"]


def test_a_refused_note_raises_with_the_backends_reason_verbatim():
    def handler(method, path, *, params=None, body=None, timeout_override=None):
        raise QuestApiError(f'Quest API {method} {path} -> 400: {{"detail":"{REFUSAL}"}}', status=400)

    with pytest.raises(QuestApiError) as caught:
        client_with(handler).update_outcome_progress("q1", "o1", 10, "making progress")
    assert str(caught.value) == f"Not done: {REFUSAL}"
    assert caught.value.status == 400


def test_a_server_error_is_not_reworded_as_a_refusal():
    def handler(method, path, *, params=None, body=None, timeout_override=None):
        raise QuestApiError(f"Quest API {method} {path} -> 500: boom", status=500)

    with pytest.raises(QuestApiError) as caught:
        client_with(handler).update_outcome_progress("q1", "o1", 10, "Two partners signed")
    assert "Not done" not in str(caught.value)


def test_a_non_numeric_percent_is_refused_before_any_request():
    def handler(*args, **kwargs):
        raise AssertionError("must not call the API")

    with pytest.raises(QuestApiError, match="must be numbers"):
        client_with(handler).update_outcome_progress("q1", "o1", "lots", "Two partners signed")


def test_cli_needs_write_and_prints_the_refusal_to_stderr(monkeypatch, capsys):
    def handler(method, path, *, params=None, body=None, timeout_override=None):
        raise QuestApiError(f'Quest API {method} {path} -> 400: {{"detail":"{REFUSAL}"}}', status=400)

    monkeypatch.setattr(cli, "load_quest_client", lambda config=None: client_with(handler))
    argv = ["quest", "update_outcome_progress", "q1", "o1", "10", "making progress"]

    assert cli.main(argv) == 2  # a write method without --write is refused
    assert "--write" in capsys.readouterr().err

    assert cli.main(argv + ["--write"]) == 1
    assert capsys.readouterr().err.strip() == f"Not done: {REFUSAL}"
