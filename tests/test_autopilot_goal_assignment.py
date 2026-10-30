"""Autopilot picks an assignee for the goal it creates, and hands AI-doable assigned goals to the
assignee's rep. Both are best-effort and never fail a pass."""
import json

from quest_ai_runner.runner.autopilot import AutopilotPass
from quest_ai_runner.runner.goal_handoff import choose_assignee, run_goal_handoff
from quest_ai_runner.runner.quest_client import QuestApiError
from quest_ai_runner.runner.quest_client_support import WriteResult

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
        # What the real client returns: a typed result, not a dict.
        return WriteResult.from_payload({"id": f"goal_{len(self.created_goals)}"}, 201)

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


def test_invalid_judged_id_falls_back_to_the_quest_owner():
    # A named stranger is a judge failure, not "I cannot tell": the goal must not go ownerless.
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB])
    _create(c, _judge({"assignee": "u_stranger"}))
    assert c.created_goals[0]["assigned_to_user_id"] == "u_alice"


def test_explicit_null_from_the_judge_leaves_the_goal_ownerless_for_the_owner_to_be_asked():
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB])
    _create(c, _judge({"assignee": None, "ai_can_do": False}))
    assert c.created_goals[0]["assigned_to_user_id"] is None


def test_no_judge_single_member_is_assigned_and_multiple_go_to_the_owner():
    one = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE])
    _create(one)
    assert one.created_goals[0]["assigned_to_user_id"] == "u_alice"
    many = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB])
    _create(many)
    assert many.created_goals[0]["assigned_to_user_id"] == "u_alice"
    # No owner flagged: the first human member.
    no_owner = AssignClient(quests=[_quest("q1", mode="act")], members=[{**BOB}, {**ALICE, "is_owner": False}])
    _create(no_owner)
    assert no_owner.created_goals[0]["assigned_to_user_id"] == "u_bob"


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
    assert choose_assignee(boom, [ALICE, BOB], outcome="", title="t", description="")["assignee"] == "u_alice"
    assert choose_assignee(lambda p: "not json", [ALICE, BOB], outcome="", title="t",
                           description="")["assignee"] == "u_alice"


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


def test_dict_preferences_reach_the_prompt_as_json_not_python_repr():
    seen = []

    def spy(prompt):
        seen.append(prompt)
        return json.dumps({"assignee": "u_bob", "ai_can_do": False})

    class DictPrefsClient(AssignClient):
        def get_quest(self, quest_id, *, team_id=None):
            return {"outcome": "Launch", "current_state": "", "preferences": {"no_weekends": True}}

    c = DictPrefsClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB])
    _create(c, spy)
    assert '"no_weekends": true' in seen[0]
    assert "'no_weekends'" not in seen[0]


def test_ai_members_are_never_chosen_or_listed():
    from quest_ai_runner.runner.goal_handoff import choose_assignee, human_member_ids
    rep = {"user_id": "u_rep", "name": "Bob's rep", "role": "ai_service"}
    assert human_member_ids([ALICE, rep]) == ["u_alice"]
    seen = []

    def judge(prompt):
        seen.append(prompt)
        return json.dumps({"assignee": "u_rep", "ai_can_do": True})

    out = choose_assignee(judge, [ALICE, BOB, rep], outcome="o", title="t", description="d")
    # The rep is never offered or accepted; the goal falls back to the human quest owner.
    assert out == {"assignee": "u_alice", "ai_can_do": False} and "u_rep" not in seen[0]
    # one human plus an AI rep still counts as a single-human quest
    assert choose_assignee(None, [ALICE, rep], outcome="o", title="t", description="d")["assignee"] == "u_alice"


def test_handoff_prompt_asks_for_full_sufficiency_and_bans_em_dashes():
    seen = []

    def spy(prompt):
        seen.append(prompt)
        return json.dumps({"ai_can_do": True})

    c = AssignClient(reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    run_goal_handoff(c, spy, _payload({"id": "g1", "name": "Draft the FAQ", "assigned_to_user_id": "u_bob"}),
                     team_id="team1")
    prompt = " ".join(seen[0].split())
    assert "completely" in prompt and "partly" in prompt
    assert "writing test files" in prompt and "reading figures" in prompt
    assert "no em dashes" in prompt and "—" not in prompt


def test_handoff_never_judges_a_goal_nobody_owns_and_a_second_pass_is_a_no_op():
    c = AssignClient(reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    open_goal = {"id": "g1", "name": "Draft the FAQ", "assigned_to_user_id": "u_bob"}
    unowned = {"id": "g2", "name": "Draft the newsletter", "assigned_to_user_id": None}
    judge = _judge({"ai_can_do": True})
    assert run_goal_handoff(c, judge, _payload(open_goal, unowned), team_id="team1") == [
        {"goal_id": "g1", "handling": "rep"}]
    # The backend stamped g1 with ai_handling and a decision time; a second pass skips it.
    taken = {**open_goal, "ai_handling": "rep", "ai_handling_decided_at": "2026-10-12"}
    assert run_goal_handoff(c, judge, _payload(taken, unowned), team_id="team1") == []
    assert len(c.handoffs) == 1


def test_handoff_stamps_me_once_when_the_verdict_is_unreadable():
    c = AssignClient(reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    payload = _payload({"id": "g1", "name": "Call the bank", "assigned_to_user_id": "u_bob"})
    out = run_goal_handoff(c, lambda prompt: "not json at all", payload, team_id="team1")
    assert out == [{"goal_id": "g1", "handling": "me"}]
    assert c.handoffs == [("g1", "me", "rep_bob", "rep")]


def test_the_id_of_a_goal_created_through_the_real_client_result_reaches_the_hand_over():
    """The client returns a WriteResult, never a dict: the goal's id is read from it, so the rep is handed the goal it was made for."""
    c = AssignClient(quests=[_quest("q1", mode="act")], members=[ALICE, BOB],
                     reps=[{"rep_id": "rep_bob", "owner_user_id": "u_bob"}])
    created = c.create_goal("t", period="2026-W44")
    assert isinstance(created, WriteResult) and not isinstance(created, dict)
    assert _create(c, _judge({"assignee": "u_bob", "ai_can_do": True})) == "goal_2"
    assert c.handoffs == [("goal_2", "rep", "rep_bob", "rep")]
