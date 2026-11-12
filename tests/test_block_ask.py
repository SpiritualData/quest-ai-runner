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


def real_state(own_fields):
    """``GET /api/quests/{id}/state`` as production answers it (read 2026-11-11): the quest's own fields, team ids included, sit under
    ``state``; the top level carries ``team_id: null`` and ``team_name: null`` beside other envelope keys. A reader of the top level finds no team."""
    return {"quest_id": "q1", "state": {"quest_id": "q1", "user_id": OTHER, "name": "A quest", "completed": False, **own_fields},
            "field_guidance": {}, "metric_readings": [], "is_shared_with_me": False, "shared_by_user_id": None, "share_access": None,
            "category_names": [], "backlog_count": 0, "team_id": None, "team_name": None, "org_id": None, "org_name": None}


def base_routes(state, block_answer, goals=GOALS):
    return [
        ("GET", "/api/teams/decisions/for-quest", 200, []),
        ("GET", "/api/quests/q1/state", 200, real_state(state)),
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


# --- the runner's own stuck-on-a-person ask (file_blocked_ask -> sink -> block) ------------------------------------------------

from types import SimpleNamespace

from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestDecisionSink
from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import block_type_of, blocked_goal_of
from quest_ai_runner.the_intelligence.mind.response_writer import orchestrator_turn_messages as messages


def stuck_turn(meta, verdict=None, **verdict_fields):
    client = QuestClient("https://quest.example", "key", team_id="team_a")
    orch = SimpleNamespace(escalation=QuestDecisionSink(client), _concise_decision_summary=lambda q: q)
    res = SimpleNamespace(output="", decision_id=None, observations=[], observations_reported=False, coverage=None)
    verdict = verdict or {"blocker": "needs_person", "question": "Please share the store login.", **verdict_fields}
    meta = {"live_user_turn": False, "asker_user_id": OWNER, "quest_id": "q1", "participants": [{"user_id": OWNER, "name": "Owner"}], **meta}
    return messages.file_blocked_ask(orch, None, meta, res, verdict, question=verdict.get("question", "")), res


def test_the_goal_and_block_type_come_from_structure_only():
    assert blocked_goal_of({"quest_id": "q1", "related_goal_id": "g1", "goal_id": "q1"}) == "g1"
    assert blocked_goal_of({"quest_id": "q1", "goal_id": "q1"}) == ""          # a quest-scoped task carries the quest's id as goal_id
    assert blocked_goal_of({"quest_id": "q1", "goal_id": "g9"}) == "g9"
    assert blocked_goal_of({}) == ""
    assert block_type_of("tool_failure") == "needs_access" and block_type_of("needs_person") == "needs_information"


def test_the_runners_stuck_ask_on_a_goal_is_filed_as_a_block_and_its_decision_is_the_blocks(monkeypatch):
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    filed, res = stuck_turn({"related_goal_id": "g1"})
    assert filed is True and res.decision_id == "d_block"
    posts = [r for r in seen if r[0] == "POST"]
    assert [p[1] for p in posts] == ["/api/teams/team_b/process/blocks"] and posts[0][2]["type"] == "needs_information"
    assert not [r for r in seen if r[1].endswith("/decisions")]


def test_an_ask_meant_for_someone_else_stays_a_plain_ask(monkeypatch):
    """The block would go to the goal's owner; the verifier named a different teammate, so no block is filed."""
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    meta = {"related_goal_id": "g1", "participants": [{"user_id": OWNER, "name": "Owner"}, {"user_id": OTHER, "name": "Other"}]}
    filed, res = stuck_turn(meta, asked_of="member", member_ref=OTHER)
    assert filed is True and res.decision_id == "plain1"
    assert not [r for r in seen if "process/blocks" in r[1]]


def test_a_run_on_a_quest_alone_files_the_plain_ask(monkeypatch):
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    filed, res = stuck_turn({"goal_id": "q1"})
    assert filed is True and res.decision_id == "plain1" and not [r for r in seen if "process/blocks" in r[1]]


def test_a_quest_without_a_team_or_a_refused_block_files_the_plain_ask(monkeypatch):
    routed(monkeypatch, base_routes({"team_id": None, "team_ids": []}, (200, BLOCK)))
    assert stuck_turn({"related_goal_id": "g1"})[1].decision_id == "plain1"
    routed(monkeypatch, base_routes(STATE, (422, {"detail": "refused"})))
    assert stuck_turn({"related_goal_id": "g1"})[1].decision_id == "plain1"


def test_the_person_typing_is_still_asked_in_the_reply_not_through_a_block(monkeypatch):
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    filed, res = stuck_turn({"related_goal_id": "g1", "live_user_turn": True})
    assert filed is False and res.decision_id is None and "store login" in res.output
    assert not [r for r in seen if r[0] == "POST"]


def test_a_block_a_worker_raised_with_its_own_tool_is_adopted_as_the_turns_ask():
    res = SimpleNamespace(output="", decision_id=None, observations=[], observations_reported=False, coverage=None)
    entry = {"decision_id": "d_block", "assignee_user_id": OWNER, "summary": "Blocked: needs the store login"}
    assert messages.adopt_turn_ask(SimpleNamespace(), None, {"asker_user_id": OWNER}, res, [entry]) is True
    assert res.decision_id == "d_block"


def test_an_ask_for_the_runners_own_account_goes_to_the_goals_owner_as_a_block(monkeypatch):
    """A run nobody is typing into has the AI account as its asker; that account cannot answer, so the goal's person gets the block."""
    routes = [("GET", "/api/teams/whoami", 200, {"user_id": "user_ai"}), *base_routes(STATE, (200, BLOCK))]
    seen = routed(monkeypatch, routes)
    filed, res = stuck_turn({"related_goal_id": "g1", "asker_user_id": "user_ai", "participants": []})
    assert filed is True and res.decision_id == "d_block"
    assert [r[1] for r in seen if r[0] == "POST"] == ["/api/teams/team_b/process/blocks"]


def test_an_ai_owned_goal_with_the_ai_as_asker_stays_a_plain_ask_for_a_person(monkeypatch):
    """No block (the goal's owner is the AI), and the plain ask goes to the person the run works for, never the AI account."""
    goals = {"period_groups": [{"goals": [{"id": "g1", "assigned_to_user_id": "user_ai"}]}]}
    seen = routed(monkeypatch, [("GET", "/api/teams/whoami", 200, {"user_id": "user_ai"}), *base_routes(STATE, (200, BLOCK), goals=goals)])
    filed, res = stuck_turn({"related_goal_id": "g1", "asker_user_id": "user_ai", "requester_user_id": "user_ai",
                             "task_owner_user_id": OTHER, "participants": []})
    assert res.decision_id == "plain1" and not [r for r in seen if "process/blocks" in r[1]]
    plain = [r for r in seen if r[0] == "POST" and r[1].endswith("/decisions")][0]
    assert plain[2]["assigned_to_user_id"] == OTHER


def test_an_ask_with_nobody_but_the_ai_account_to_take_it_is_refused_not_given_to_the_ai(monkeypatch):
    goals = {"period_groups": [{"goals": [{"id": "g1", "assigned_to_user_id": "user_ai"}]}]}
    seen = routed(monkeypatch, [("GET", "/api/teams/whoami", 200, {"user_id": "user_ai"}), *base_routes(STATE, (200, BLOCK), goals=goals)])
    filed, res = stuck_turn({"related_goal_id": "g1", "asker_user_id": "user_ai", "participants": []})
    assert res.decision_id is None and not [r for r in seen if r[0] == "POST"]


def test_a_task_run_with_a_goal_owner_as_neither_asker_nor_requester_files_the_block_for_the_owner(monkeypatch):
    """No asker at all (the run's meta carries only the quest and the goal): the goal's owner is who the ask is for."""
    routes = [("GET", "/api/teams/whoami", 200, {"user_id": "user_ai"}), *base_routes(STATE, (200, BLOCK))]
    seen = routed(monkeypatch, routes)
    filed, res = stuck_turn({"related_goal_id": "g1", "asker_user_id": "", "participants": []})
    assert filed is True and res.decision_id == "d_block"
    assert [r[1] for r in seen if r[0] == "POST"] == ["/api/teams/team_b/process/blocks"]


def test_the_create_decision_tool_from_a_task_run_resolves_an_assignee_and_never_the_ai_account(monkeypatch):
    """A test run's plain ask was refused ("Assigned to user id is required"): the AI account asked and nobody resolved."""
    seen = routed(monkeypatch, [("GET", "/api/teams/whoami", 200, {"user_id": "user_ai"}), *base_routes(STATE, (200, BLOCK))])
    client = QuestClient("https://quest.example", "key", team_id="team_a")
    spec = create_decision_spec({}, client_factory=lambda: client)
    meta = {"asker_user_id": "user_ai", "requester_user_id": "user_ai", "task_owner_user_id": OTHER}
    result = spec.handler({"summary": "Need the login", "quest_id": "q1"}, ToolContext(user_id="user_ai", meta=meta))
    assert result.status is ToolStatus.APPLIED
    plain = [r for r in seen if r[0] == "POST" and r[1].endswith("/decisions")][0]
    assert plain[2]["assigned_to_user_id"] == OTHER
    seen.clear()
    spec.handler({"summary": "Need the other thing", "quest_id": "q1", "blocked_goal_id": "g1"},
                 ToolContext(user_id="user_ai", meta={"asker_user_id": "user_ai"}))
    plain = [r for r in seen if r[0] == "POST" and r[1].endswith("/decisions")]
    assert not plain or plain[0][2]["assigned_to_user_id"] == OWNER   # the goal's owner, else nobody: never user_ai


def test_the_create_decision_tool_takes_the_runs_goal_when_only_a_block_type_is_given(monkeypatch):
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    client = QuestClient("https://quest.example", "key", team_id="team_a")
    ctx = ToolContext(meta={"related_goal_id": "g1"})
    result = create_decision_spec({}, client_factory=lambda: client).handler(
        {"summary": "Need the login", "quest_id": "q1", "block_type": "needs_access"}, ctx)
    assert result.status is ToolStatus.APPLIED and [r[1] for r in seen if r[0] == "POST"] == ["/api/teams/team_b/process/blocks"]


def test_a_sink_ask_filed_as_a_block_carries_the_full_question_conversation_recipient_key_and_deadline_and_reads_the_goal_once(monkeypatch):
    """The block route used to get only a 300-character reason, and the goal's team and owner were read twice per ask (once to pick
    the assignee, once to file): the person got a cut question with no link back to the conversation waiting on it."""
    from datetime import datetime, timedelta, timezone
    from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestDecisionSink
    seen = routed(monkeypatch, base_routes(STATE, (200, BLOCK)))
    question = "Which store account should the run use?\nA) The main login\nB) A new one\n" + "Context. " * 60
    due = datetime(2030, 1, 2, 9, tzinfo=timezone.utc)
    esc = Escalation(summary=question, quest_id="q1", blocked_goal_id="g1", block_type="needs_decision", deadline=due,
                     conv_id="qaconv_1", notify_user_id=OTHER, dedupe_key="turn-gap-1")
    client = QuestClient("https://quest.example", "key", team_id="team_a")
    assert QuestDecisionSink(client).escalate(esc) == "d_block"
    body = [r for r in seen if r[0] == "POST" and "process/blocks" in r[1]][0][2]
    assert len(body["reason"]) <= 300 and body["question"] == question.strip()
    assert body["conv_id"] == "qaconv_1" and body["notify_user_id"] == OTHER and body["dedupe_key"] == "turn-gap-1"
    assert body["deadline"] == due.isoformat()
    assert len([r for r in seen if r[0] == "GET" and r[1].startswith("/api/quests/q1/state")]) == 1
    assert len([r for r in seen if r[0] == "GET" and "/members" in r[1]]) <= 2


# --- a block's ask goes to the goal's owner: never filed onto a goal the person this ask is for does not own ------------------


def test_a_block_is_filed_only_for_the_goal_owner_the_ask_resolved_to():
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import blocked_ask

    class Client:
        def __init__(self):
            self.expected = []

        def report_block(self, quest_id, goal_id, block_type, reason, *, expected_owner=""):
            self.expected.append(expected_owner)
            return {"block": {"decision_id": "d1"}, "created": True}

    client = Client()
    args = {"blocked_goal_id": "g1", "block_type": "needs_access"}
    assert blocked_ask(client, args, "q1", "Need the login") is None          # nobody to ask: no block, the plain ask goes out
    assert client.expected == []
    assert blocked_ask(client, args, "q1", "Need the login", "", OWNER).status is ToolStatus.APPLIED
    assert client.expected == [OWNER]                                         # the backend is told whose goal it must be
