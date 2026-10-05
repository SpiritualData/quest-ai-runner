"""Autopilot picks an assignee for the goal it creates, and hands AI-doable assigned goals to the
assignee's rep. Both are best-effort and never fail a pass."""
import json

from quest_ai_runner.runner.autopilot import AutopilotPass
from quest_ai_runner.runner.goal_handoff import choose_assignee, run_goal_handoff
from quest_ai_runner.runner.quest_client import QuestApiError

from .test_autopilot import FakeAutopilotClient, _now, _quest

ALICE = {"user_id": "u_alice", "name": "Alice", "email": "a@x.org", "is_owner": True}
BOB = {"user_id": "u_bob", "name": "Bob", "email": "b@x.org", "is_owner": False}


class AssignClient(FakeAutopilotClient):
    def __init__(self, *args, members=(), reps=(), refuse_assignee=False, **kw):
        super().__init__(*args, **kw)
        self.members = list(members)
        self.reps = list(reps)
        self.refuse_assignee = refuse_assignee
        self.created_goals = []
        self.handoffs = []

    def list_assignable_members(self, quest_id):
        return self.members

    def list_team_reps(self, *, team_id=None):
        return self.reps

    def create_goal(self, title, *, period, quest_id=None, description=None,
                    assigned_to_user_id=None):
        if assigned_to_user_id and self.refuse_assignee:
            raise QuestApiError("400 not a member")
        self.created_goals.append({"title": title, "assigned_to_user_id": assigned_to_user_id})
        return {"id": f"goal_{len(self.created_goals)}"}

    def set_goal_ai_handling(self, goal_id, handling, *, rep_id=None, decided_by="rep"):
        self.handoffs.append((goal_id, handling, rep_id, decided_by))
        return {}


def _judge(payload):
    return lambda prompt: json.dumps(payload)


def _create(client, judge=None):
    passer = AutopilotPass(client, team_id="team1", now=_now, judge=judge)
    return passer._maybe_create_goal("q1", "A title", "desc", "act", "")


def test_judge_picks_a_member():
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB])
    assert _create(c, _judge({"assignee": "u_bob", "ai_can_do": False})) == "goal_1"
    assert c.created_goals[0]["assigned_to_user_id"] == "u_bob"
    assert c.handoffs == []


def test_invalid_judged_id_is_dropped_to_shared():
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB])
    _create(c, _judge({"assignee": "u_stranger"}))
    assert c.created_goals[0]["assigned_to_user_id"] is None


def test_no_judge_single_member_is_assigned_and_multiple_are_shared():
    one = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE])
    _create(one)
    assert one.created_goals[0]["assigned_to_user_id"] == "u_alice"
    many = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB])
    _create(many)
    assert many.created_goals[0]["assigned_to_user_id"] is None


def test_refused_assignee_retries_shared():
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE], refuse_assignee=True)
    assert _create(c) == "goal_1"
    assert c.created_goals[0]["assigned_to_user_id"] is None


def test_ai_doable_new_goal_is_handed_to_the_assignees_rep():
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB],
                     reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    _create(c, _judge({"assignee": "u_bob", "ai_can_do": True}))
    assert c.handoffs == [("goal_1", "rep", "rep_bob", "rep")]


def test_judge_failure_is_nonfatal():
    def boom(prompt):
        raise RuntimeError("down")
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE])
    assert _create(c, boom) == "goal_1"
    assert choose_assignee(boom, [ALICE, BOB], outcome="", title="t", description="")["assignee"] is None


def _payload(*goals):
    return {"period_groups": [{"goals": list(goals)}]}


def test_handoff_judges_only_undecided_assigned_goals_with_a_rep():
    c = AssignClient(reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob", "is_default": True}])
    payload = _payload(
        {"id": "g_ok", "name": "Research X", "assigned_to_user_id": "u_bob"},
        {"id": "g_dec", "name": "d", "assigned_to_user_id": "u_bob",
         "ai_handling_decided_at": "2026-01-01"},
        {"id": "g_none", "name": "n", "assigned_to_user_id": None},
        {"id": "g_norep", "name": "r", "assigned_to_user_id": "u_alice"},
        {"id": "g_done", "name": "c", "assigned_to_user_id": "u_bob", "completed": True},
    )
    out = run_goal_handoff(c, _judge({"ai_can_do": True}), payload, team_id="team1")
    assert out == [{"goal_id": "g_ok", "handling": "rep"}]
    assert c.handoffs == [("g_ok", "rep", "rep_bob", "rep")]


def test_handoff_stamps_me_when_not_doable_and_survives_judge_failure():
    c = AssignClient(reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    payload = _payload({"id": "g1", "name": "Call the bank", "assigned_to_user_id": "u_bob"})
    run_goal_handoff(c, _judge({"ai_can_do": False}), payload, team_id="team1")
    assert c.handoffs == [("g1", "me", "rep_bob", "rep")]

    def boom(prompt):
        raise RuntimeError("down")
    c2 = AssignClient(reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    assert run_goal_handoff(c2, boom, payload, team_id="team1") == []


def test_assignee_prompt_carries_quest_context_and_member_roles():
    seen = []

    def spy(prompt):
        seen.append(prompt)
        return json.dumps({"assignee": "u_bob", "ai_can_do": False})

    class QuestAwareClient(AssignClient):
        def get_quest(self, quest_id, *, team_id=None):
            return {"outcome": "Launch the course", "current_state": "Videos half filmed",
                    "preferences": "No weekend work"}

    admin_bob = {**BOB, "role": "admin"}
    c = QuestAwareClient(quests=[_quest("q1", mode="act")], members=[ALICE, admin_bob])
    _create(c, spy)
    prompt = seen[0]
    assert "Launch the course" in prompt and "Videos half filmed" in prompt
    assert "No weekend work" in prompt
    assert "u_bob: Bob (team admin)" in prompt and "u_alice: Alice (quest owner)" in prompt
    assert "—" not in prompt


def test_handoff_prompt_carries_state_and_preferences():
    seen = []

    def spy(prompt):
        seen.append(prompt)
        return json.dumps({"ai_can_do": False})

    c = AssignClient(reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    run_goal_handoff(c, spy, _payload({"id": "g1", "name": "Call the bank", "assigned_to_user_id": "u_bob"}),
                     team_id="team1", outcome="Close the loan", current_state="Docs signed",
                     preferences="Mornings only")
    assert "Close the loan" in seen[0] and "Docs signed" in seen[0] and "Mornings only" in seen[0]
