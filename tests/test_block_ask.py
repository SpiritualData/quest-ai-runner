"""create_decision raises a BLOCK (the quest team's three-day hand-off) when the worker names the goal it is stuck on.

The real client runs over a routed stand-in for the network: each request is answered by the first route whose path
contains the key. A block that cannot be filed (no team, unknown goal, refused) falls back to the plain ask.
"""
import io
import json
import urllib.error
import urllib.request

from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import create_decision_spec
from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient
from quest_ai_runner.the_world.tools.tools import ToolContext
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

from .fake_quest_transport import FakeResponse

OWNER, OTHER = "user_owner", "user_other"
GOALS = {"period_groups": [{"goals": [{"id": "g1", "name": "Ship it", "assigned_to_user_id": OWNER}]}]}


def routed(monkeypatch, routes):
    """Install a network stand-in. ``routes`` is a list of (method, path fragment, status, body); returns the request log."""
    seen = []

    def fake(req, timeout=None):
        path = req.full_url.split("quest.example", 1)[1]
        seen.append((req.get_method(), path, json.loads(req.data) if req.data else None))
        for method, fragment, status, body in routes:
            if method == req.get_method() and fragment in path:
                if status >= 400:
                    raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(json.dumps(body).encode()))
                return FakeResponse(status, body)
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def base_routes(state, block_answer, goals=GOALS):
    return [
        ("GET", "/api/teams/decisions/for-quest", 200, []),
        ("GET", "/api/quests/q1/state", 200, {"quest_id": "q1", "state": state}),   # the quest's fields sit under "state"
        ("GET", "/goals/", 404, {}),   # the reference backend has no single-goal route
        ("GET", "/quests/q1/goals", 200, goals),
        ("GET", "/api/teams/team_a/members", 200, {"members": [{"user_id": OTHER}]}),
        ("GET", "/api/teams/team_b/members", 200, {"members": [{"user_id": OWNER}]}),
        ("POST", "/process/blocks", *block_answer),
        ("POST", "/decisions", 200, {"decision_id": "plain1"}),
    ]


def ask(args):
    client = QuestClient("https://quest.example", "key", team_id="team_a")
    spec = create_decision_spec({}, client_factory=lambda: client)
    ctx = ToolContext()
    return spec.handler({"summary": "Need the store login", "quest_id": "q1", **args}, ctx)


BLOCK = {"block": {"block_id": "blk_1", "decision_id": "d_block", "step_owner_id": OWNER}, "created": True}
STATE = {"team_id": "team_a", "team_ids": ["team_b"]}


def test_a_named_goal_and_type_files_a_block_under_the_team_the_owner_belongs_to(monkeypatch):
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    result = ask({"blocked_goal_id": "g1", "block_type": "needs_access", "block_reason": "Needs the store login"})
    assert result.status is ToolStatus.APPLIED and "d_block" in result.text
    posts = [r for r in seen if r[0] == "POST"]
    assert posts == [("POST", "/api/teams/team_b/process/blocks",
                      {"goal_id": "g1", "type": "needs_access", "reason": "Needs the store login"})]


def test_the_reason_defaults_to_the_summary_and_is_cut_to_the_limit(monkeypatch):
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    ask({"summary": "x " * 400, "blocked_goal_id": "g1", "block_type": "needs_decision"})
    reason = [r for r in seen if r[0] == "POST"][0][2]["reason"]
    assert len(reason) <= 300 and reason.endswith("...")


def test_an_open_block_is_not_filed_twice(monkeypatch):
    routed(monkeypatch, base_routes(STATE, (200, {**BLOCK, "created": False})))
    result = ask({"blocked_goal_id": "g1", "block_type": "needs_access"})
    assert result.status is ToolStatus.NOOP and "d_block" in result.text


def test_without_a_goal_or_type_it_is_the_plain_ask(monkeypatch):
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    assert ask({"blocked_goal_id": "g1"}).status is ToolStatus.APPLIED
    assert not [r for r in seen if "process/blocks" in r[1]]
    assert [r for r in seen if r[1].endswith("/decisions")]


def test_a_quest_with_no_team_falls_back_to_the_plain_ask(monkeypatch):
    seen = routed(monkeypatch, base_routes({"team_id": None, "team_ids": []}, (200, BLOCK)))
    result = ask({"blocked_goal_id": "g1", "block_type": "needs_access"})
    assert result.status is ToolStatus.APPLIED and "plain1" in result.text
    assert not [r for r in seen if "process/blocks" in r[1]]


def test_an_unknown_goal_falls_back_to_the_plain_ask(monkeypatch):
    routed(monkeypatch, base_routes(STATE, (200, BLOCK), goals={"period_groups": []}))
    assert "plain1" in ask({"blocked_goal_id": "gone", "block_type": "needs_access"}).text


def test_a_refused_block_falls_back_to_the_plain_ask(monkeypatch):
    routed(monkeypatch, base_routes(STATE, (422, {"detail": "That step does not belong to this team's quests"})))
    assert "plain1" in ask({"blocked_goal_id": "g1", "block_type": "needs_access"}).text


def test_the_primary_team_is_used_when_the_owner_is_on_none_of_the_teams(monkeypatch):
    seen = routed(monkeypatch, base_routes({"team_id": "team_a", "team_ids": []}, (200, BLOCK),
                                           goals={"period_groups": [{"goals": [{"id": "g1", "assigned_to_user_id": "stranger"}]}]}))
    ask({"blocked_goal_id": "g1", "block_type": "needs_access"})
    assert [r[1] for r in seen if r[0] == "POST"] == ["/api/teams/team_a/process/blocks"]
