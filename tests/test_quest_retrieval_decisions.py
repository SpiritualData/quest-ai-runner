"""QuestRetrievalAdapter surfaces a quest's decisions (open + resolved) as part of its context.

Without this, a rep with no memory of past turns had no way to see a decision it (or an earlier
run) already raised on the same quest, so it could re-raise a duplicate open decision or re-ask a
question that was already answered. All offline: QuestClient's transport (_request) is stubbed,
no network, no key.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.quest_retrieval_adapter import QuestRetrievalAdapter
from quest_ai_runner.runner.quest_client import QuestClient


def client_with_decisions(decisions: List[Dict[str, Any]], *,
                          quest: Optional[Dict[str, Any]] = None) -> QuestClient:
    """A real QuestClient whose transport is stubbed to answer get_quest / list_quest_goals /
    list_decisions_for_quest with canned data, so the adapter exercises the real client methods
    (request shaping, response-shape handling) rather than a hand-rolled double."""
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    quest = quest if quest is not None else {"quest_id": "quest_1", "outcome": "Ship the thing"}

    def fake_request(method, path, *, params=None, body=None):
        if path == "/api/teams/team_1/quests/quest_1":
            return quest
        if path == "/api/teams/team_1/quests/quest_1/goals":
            return {}  # falsy: the adapter's goals rendering is skipped either way
        if path == "/api/teams/decisions/for-quest":
            assert params == {"quest_id": "quest_1"}
            return decisions
        raise AssertionError(f"unexpected request: {method} {path}")

    client._request = fake_request  # type: ignore[assignment]
    return client


def quest_context_text(decisions: List[Dict[str, Any]]) -> str:
    adapter = QuestRetrievalAdapter(client_with_decisions(decisions))
    obs = adapter.query({"kind": "quest_context", "quest_id": "quest_1"})
    assert obs.kind == "query"
    return obs.text or ""


def test_open_decision_is_surfaced_and_labelled():
    text = quest_context_text([
        {"decision_id": "dec_1", "status": "open", "summary": "Approve sending the donor email?"},
    ])
    assert "OPEN: Approve sending the donor email?" in text
    assert "do not duplicate" in text.lower()


def test_resolved_decision_shows_resolution_and_response():
    text = quest_context_text([
        {"decision_id": "dec_2", "status": "resolved", "summary": "Use the June copy or July?",
         "resolution": "approved", "response_text": "Go with July", "resolved_by_name": "Joshua"},
    ])
    assert "RESOLVED: Use the June copy or July?" in text
    assert "approved" in text
    assert "Go with July" in text
    assert "Joshua" in text
    assert "do not re-ask" in text.lower()


def test_auto_resolved_decision_says_so_without_a_resolver_name():
    text = quest_context_text([
        {"decision_id": "dec_3", "status": "resolved", "summary": "Proceed by Friday?",
         "resolution": "proceed", "auto_resolved": True},
    ])
    assert "auto-resolved" in text


def test_open_decisions_come_before_resolved_ones_regardless_of_input_order():
    text = quest_context_text([
        {"decision_id": "dec_r", "status": "resolved", "summary": "Older resolved ask",
         "resolution": "approved"},
        {"decision_id": "dec_o", "status": "open", "summary": "Newer open ask"},
    ])
    assert text.index("OPEN: Newer open ask") < text.index("RESOLVED: Older resolved ask")


def test_decisions_are_capped_at_ten():
    decisions = [{"decision_id": f"dec_{i}", "status": "open", "summary": f"Ask number {i}"}
                for i in range(15)]
    text = quest_context_text(decisions)
    assert sum(1 for i in range(15) if f"Ask number {i}" in text) == 10


def test_no_decisions_means_no_decisions_block():
    text = quest_context_text([])
    assert "Decisions already raised" not in text


def test_degrades_gracefully_when_client_lacks_list_decisions_for_quest():
    """A consumer's own client (or an older pin) may not implement the new method at all --
    the adapter must not raise, and must simply omit the decisions block."""

    class BareClient:
        configured = True

        def get_quest(self, quest_id, **kwargs):
            return {"quest_id": quest_id, "outcome": "Ship the thing"}

        def list_quest_goals(self, quest_id, **kwargs):
            return {}

    adapter = QuestRetrievalAdapter(BareClient())
    obs = adapter.query({"kind": "quest_context", "quest_id": "quest_1"})
    assert obs.kind == "query"
    assert "Decisions already raised" not in (obs.text or "")
