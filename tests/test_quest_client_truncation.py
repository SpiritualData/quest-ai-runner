"""QuestClient.create_decision must cap an over-long summary at Quest's 4000-char condition limit.

Quest stores a decision summary as a goal CONDITION (max 4000 chars server-side). A verbose
planner question/clarification could exceed that and the POST was rejected with
"Goal condition is limited to 4000 characters (got NNNNN)". The client now truncates at the single
boundary to Quest so the request always succeeds, regardless of which caller built the summary.
"""
from quest_ai_runner.runner.quest_client import QuestClient


def _client_capturing_body():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def _fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"decision_id": "dec_1"}

    client._request = _fake_request  # type: ignore[assignment]
    return client, captured


def test_create_decision_truncates_over_long_summary():
    client, captured = _client_capturing_body()
    huge = "x" * 11918  # the real-world failure: an 11918-char summary
    client.create_decision(huge, kind="approve")
    sent = captured["body"]["summary"]
    assert len(sent) <= 4000, f"summary not capped: {len(sent)} chars"
    assert sent.endswith("[...truncated]")


def test_create_decision_leaves_short_summary_unchanged():
    client, captured = _client_capturing_body()
    short = "Approve sending this email to the donor?"
    client.create_decision(short, kind="approve")
    assert captured["body"]["summary"] == short  # untouched when within the limit
