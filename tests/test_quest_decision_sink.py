"""QuestDecisionSink must never file a decision nobody is assigned to see.

Before this, ``escalate()`` sent ``assignee_user_id=escalation.assignee or self._default_assignee``
straight through: if BOTH were empty, ``create_decision`` still POSTed with no
``assigned_to_user_id`` at all, and the decision sat in nobody's queue forever with no error
anywhere. The fix adds a resolution order (explicit -> configured default -> the quest's own
owner) and refuses to call the API at all when none of those resolve, logging why instead.

Also covers the deadline default (``QAR_DECISION_DEFAULT_DEADLINE_HOURS`` / constructor override)
and the ``open_decision_ids_for_quest`` recovery-probe capability. All offline: QuestClient's
transport (_request) is stubbed, no network, no key.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import Escalation
from quest_ai_runner.runner.quest_client import QuestClient, QuestDecisionSink


def fake_client(*, quest: Optional[Dict[str, Any]] = None,
                existing_open: Optional[List[Dict[str, Any]]] = None):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls: List[Dict[str, Any]] = []

    def fake_request(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        if path == "/api/teams/decisions/for-quest":
            return list(existing_open or [])
        if path == "/api/teams/team_1/quests/quest_1":
            return quest if quest is not None else {}
        if path == "/api/teams/team_1/decisions":
            return {"decision_id": "dec_new"}
        raise AssertionError(f"unexpected request: {method} {path}")

    client._request = fake_request  # type: ignore[assignment]
    return client, calls


# --- assignee resolution order -----------------------------------------------------------------

def test_explicit_assignee_wins_over_default():
    client, calls = fake_client()
    sink = QuestDecisionSink(client, default_assignee_user_id="user_default")
    decision_id = sink.escalate(Escalation(summary="Approve X?", assignee="user_explicit",
                                          quest_id="quest_1"))
    assert decision_id == "dec_new"
    post = next(c for c in calls if c["path"] == "/api/teams/team_1/decisions")
    assert post["body"]["assigned_to_user_id"] == "user_explicit"


def test_configured_default_used_when_no_explicit_assignee():
    client, calls = fake_client()
    sink = QuestDecisionSink(client, default_assignee_user_id="user_default")
    sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1"))
    post = next(c for c in calls if c["path"] == "/api/teams/team_1/decisions")
    assert post["body"]["assigned_to_user_id"] == "user_default"


def test_falls_back_to_quest_owner_when_no_explicit_or_default():
    client, calls = fake_client(quest={"quest_id": "quest_1", "owner_user_ids": ["user_owner"]})
    sink = QuestDecisionSink(client)  # no default_assignee_user_id configured
    decision_id = sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1"))
    assert decision_id == "dec_new"
    post = next(c for c in calls if c["path"] == "/api/teams/team_1/decisions")
    assert post["body"]["assigned_to_user_id"] == "user_owner"


def test_refuses_to_file_an_unaddressed_decision(caplog):
    """No explicit assignee, no default, and the quest has no recorded owner: the sink must NOT
    POST at all (an unaddressed decision would sit in nobody's queue forever), and must log why."""
    client, calls = fake_client(quest={"quest_id": "quest_1", "owner_user_ids": []})
    sink = QuestDecisionSink(client)
    with caplog.at_level("WARNING"):
        decision_id = sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1"))
    assert decision_id == ""
    assert not any(c["path"] == "/api/teams/team_1/decisions" for c in calls)
    assert any("no explicit assignee" in r.message for r in caplog.records)


def test_refuses_when_no_quest_id_either():
    client, calls = fake_client()
    sink = QuestDecisionSink(client)
    decision_id = sink.escalate(Escalation(summary="Approve X?"))
    assert decision_id == ""
    assert not any(c["path"] == "/api/teams/team_1/decisions" for c in calls)


# --- deadline default ---------------------------------------------------------------------------

def test_explicit_deadline_passed_through():
    client, calls = fake_client(quest={"quest_id": "quest_1", "owner_user_ids": ["user_owner"]})
    sink = QuestDecisionSink(client)
    deadline = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)
    sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1", deadline=deadline))
    post = next(c for c in calls if c["path"] == "/api/teams/team_1/decisions")
    assert post["body"]["deadline"] == deadline.isoformat()


def test_default_deadline_hours_applied_when_escalation_has_none():
    client, calls = fake_client(quest={"quest_id": "quest_1", "owner_user_ids": ["user_owner"]})
    sink = QuestDecisionSink(client, default_deadline_hours=24)
    before = datetime.now(timezone.utc)
    sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1"))
    post = next(c for c in calls if c["path"] == "/api/teams/team_1/decisions")
    assert "deadline" in post["body"]
    sent = datetime.fromisoformat(post["body"]["deadline"])
    assert before + timedelta(hours=23) < sent < before + timedelta(hours=25)


def test_no_deadline_synthesized_when_unconfigured(monkeypatch):
    monkeypatch.delenv("QAR_DECISION_DEFAULT_DEADLINE_HOURS", raising=False)
    client, calls = fake_client(quest={"quest_id": "quest_1", "owner_user_ids": ["user_owner"]})
    sink = QuestDecisionSink(client)
    sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1"))
    post = next(c for c in calls if c["path"] == "/api/teams/team_1/decisions")
    assert "deadline" not in post["body"]


def test_env_var_default_deadline_used_when_no_constructor_value(monkeypatch):
    monkeypatch.setenv("QAR_DECISION_DEFAULT_DEADLINE_HOURS", "6")
    client, calls = fake_client(quest={"quest_id": "quest_1", "owner_user_ids": ["user_owner"]})
    sink = QuestDecisionSink(client)
    before = datetime.now(timezone.utc)
    sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1"))
    post = next(c for c in calls if c["path"] == "/api/teams/team_1/decisions")
    sent = datetime.fromisoformat(post["body"]["deadline"])
    assert before + timedelta(hours=5) < sent < before + timedelta(hours=7)


# --- open_decision_ids_for_quest (orphan-decision recovery probe) -------------------------------

def test_open_decision_ids_for_quest_returns_ids():
    client, calls = fake_client(existing_open=[
        {"decision_id": "dec_a", "status": "open"},
        {"decision_id": "dec_b", "status": "open"},
        {"decision_id": "dec_c", "status": "resolved"},
    ])
    sink = QuestDecisionSink(client)
    assert sink.open_decision_ids_for_quest("quest_1") == frozenset({"dec_a", "dec_b"})


def test_open_decision_ids_for_quest_empty_on_failure():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def raising(method, path, **kwargs):
        raise RuntimeError("boom")

    client._request = raising  # type: ignore[assignment]
    sink = QuestDecisionSink(client)
    assert sink.open_decision_ids_for_quest("quest_1") == frozenset()
