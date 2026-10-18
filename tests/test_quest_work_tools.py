"""create_task, assign_goal and complete_goal: spawned and handed-over work stays on the plan.

Each behaviour here is one a team lost work to before it existed: a follow-up that served no goal,
a near-duplicate goal for work the plan already named, a hand-over with no reason for the new owner.
"""
from quest_ai_runner.core import tools_quest_work as work
from quest_ai_runner.core.tools import ToolContext, ToolRegistry, build_tool_registry

CREDS = {"QUEST_BASE_URL": "https://quest.example", "QUEST_API_KEY": "key"}
MEMBERS = [{"user_id": "u_ana", "name": "Ana Reyes", "email": "ana@example.org"},
           {"user_id": "u_ben", "name": "Ben Okafor", "email": "ben@example.org"}]
GOALS = {"period_groups": [{"goals": [
    {"id": "g_parent", "name": "Launch the referral program", "assigned_to_user_id": "u_ana"},
    {"id": "g_news", "name": "Send the March newsletter to partners", "assigned_to_user_id": "u_ben"},
    {"id": "g_done", "name": "Write the partner outreach script", "completed": True}]}]}


class FakeClient:
    def __init__(self, task=None, goals=GOALS, created=None):
        self.calls = []
        self.task = task or {}
        self.goals = goals
        self.created = created if created is not None else {"id": "atask_new"}
        self.assigned = None
        self.tasks = []

    def get_task(self, task_id):
        return self.task

    def list_quest_goals(self, quest_id):
        return self.goals

    def list_assignable_members(self, quest_id):
        return MEMBERS

    def resolve_outcome_id(self, quest_id, outcome):
        return ("out_a", None) if outcome in ("out_a", "subscribers") else (None, f"{outcome!r} is not an outcome.")

    def create_task(self, text, **kw):
        self.calls.append(("create_task", text, kw))
        return dict(self.created)

    def assign_goal(self, goal_id, who, **kw):
        self.calls.append(("assign_goal", goal_id, who, kw))
        return {"id": goal_id, "assigned_to_user_id": who}

    def list_tasks(self, **kw):
        return self.tasks

    def complete_goal(self, goal_id, *, completed=True):
        self.calls.append(("complete", goal_id, completed))
        return {"id": goal_id, "completed": completed}


def run(builder, client, args, quest_id="quest_1", task_id="atask_parent"):
    spec = builder(CREDS, client_factory=lambda: client)
    return ToolRegistry([spec]).invoke(spec.name, args, ToolContext(quest_id=quest_id, task_id=task_id))


def test_a_follow_up_with_no_goal_defaults_to_the_goal_its_task_serves():
    client = FakeClient(task={"goal_id": "quest_1", "related_goal_id": "g_parent"})
    result = run(work.create_task_spec, client, {"text": "Draft the referral emails"})
    assert result.ok and "g_parent" in result.text
    kw = client.calls[0][2]
    assert kw["related_goal_id"] == "g_parent" and kw["parent_task_id"] == "atask_parent" and kw["goal_id"] == "quest_1"


def test_work_on_a_quest_must_say_which_goal_it_serves():
    client = FakeClient(task={"goal_id": "quest_1"})
    result = run(work.create_task_spec, client, {"text": "Draft the referral emails"})
    assert not result.ok and "which goal" in result.text
    assert client.calls == []


def test_an_explicit_goal_beats_the_default_and_both_declarations_are_refused():
    client = FakeClient(task={"related_goal_id": "g_parent"})
    assert run(work.create_task_spec, client, {"text": "x", "related_goal_id": "g_news"}).ok
    assert client.calls[0][2]["related_goal_id"] == "g_news"
    both = run(work.create_task_spec, client, {"text": "x", "related_goal_id": "g_news", "new_goal": {"title": "t"}})
    assert not both.ok and len(client.calls) == 1


def test_a_new_goal_is_declared_structurally_and_resolved_before_the_task_is_made():
    client = FakeClient(task={"related_goal_id": "g_parent"},
                        created={"id": "atask_new", "spawned_goal": {"id": "g_new", "name": "Book the venue", "reused": False}})
    result = run(work.create_task_spec, client, {"text": "Find a venue", "new_goal": {
        "title": "Book the venue", "assigned_to_user_id": "ben", "deadline": "2026-11-02", "outcome_id": "subscribers"}})
    assert result.ok and "g_new" in result.text
    goal = client.calls[0][2]["new_goal"]
    assert goal == {"title": "Book the venue", "deadline": "2026-11-02", "outcome_id": "out_a", "assigned_to_user_id": "u_ben"}
    assert "related_goal_id" not in client.calls[0][2]


def test_a_new_goal_that_looks_like_an_existing_one_shows_it_first_and_proceeds_once_declared_distinct():
    client = FakeClient()
    first = run(work.create_task_spec, client, {"text": "x", "new_goal": {"title": "Send March newsletter to the partners"}})
    assert not first.ok and "g_news" in first.text and "g_done" not in first.text
    assert client.calls == []
    again = run(work.create_task_spec, client, {"text": "x", "new_goal": {
        "title": "Send March newsletter to the partners", "distinct_from_existing": True}})
    assert again.ok
    assert "distinct_from_existing" not in client.calls[0][2]["new_goal"]


def test_an_unknown_assignee_or_outcome_creates_nothing():
    client = FakeClient()
    bad_person = run(work.create_task_spec, client, {"text": "x", "new_goal": {"title": "Plan the launch", "assigned_to_user_id": "zed"}})
    bad_outcome = run(work.create_task_spec, client, {"text": "x", "new_goal": {"title": "Plan the launch", "outcome_id": "zzz"}})
    assert not bad_person.ok and "Nobody" in bad_person.text
    assert not bad_outcome.ok and client.calls == []


def test_a_task_with_no_quest_needs_no_goal_and_cannot_declare_one():
    client = FakeClient()
    assert run(work.create_task_spec, client, {"text": "Tidy my notes"}, quest_id=None, task_id=None).ok
    assert not run(work.create_task_spec, client, {"text": "x", "related_goal_id": "g"}, quest_id=None, task_id=None).ok


def test_a_hand_over_needs_a_reason_and_a_next_step_and_names_the_person_by_words():
    client = FakeClient()
    thin = run(work.assign_goal_spec, client, {"goal_id": "g_news", "assigned_to_user_id": "Ana"})
    assert not thin.ok and client.calls == []
    client.tasks = [{"related_goal_id": "g_news", "assignee_user_id": "u_ana", "status": "queued"},
                    {"related_goal_id": "g_other", "assignee_user_id": "u_ana", "status": "queued"}]
    done = run(work.assign_goal_spec, client, {
        "goal_id": "g_news", "assigned_to_user_id": "Ana", "reason": "Ben is out this week", "next_step": "Send the draft"})
    assert done.ok and "1 open AI task" in done.text
    assert client.calls[0] == ("assign_goal", "g_news", "u_ana", {"reason": "Ben is out this week", "next_step": "Send the draft"})


def test_a_refused_hand_over_says_so():
    class Refusing(FakeClient):
        def assign_goal(self, *a, **kw):
            raise RuntimeError("Cannot assign to someone who is not a member of this quest")
    result = run(work.assign_goal_spec, Refusing(), {
        "goal_id": "g", "assigned_to_user_id": "u_ana", "reason": "r", "next_step": "n"})
    assert not result.ok and "not a member" in result.text


def test_complete_goal_wraps_the_checkbox_and_reports_a_refusal():
    client = FakeClient()
    assert run(work.complete_goal_spec, client, {"goal_id": "g_news"}).ok
    assert client.calls[-1] == ("complete", "g_news", True)

    class Refusing(FakeClient):
        def complete_goal(self, goal_id, *, completed=True):
            return {}
    assert not run(work.complete_goal_spec, Refusing(), {"goal_id": "g_news"}).ok


def test_the_operations_are_registered_and_create_task_is_never_withheld_by_relevance():
    registry = build_tool_registry(CREDS)
    for name in ("create_task", "assign_goal", "complete_goal"):
        spec = registry.get(name)
        assert spec and spec.when_to_use and spec.when_not_to_use
    assert not registry.get("create_task").on_request_only
    assert "create_task" in [s.name for s in registry.select("spawn a follow-up task for the launch goal")]


def test_the_client_sends_the_goal_fields_the_backend_accepts():
    from quest_ai_runner.runner.quest_client import QuestClient
    sent = []
    client = QuestClient(base_url="https://quest.example", api_key="k", team_id="t")
    client._request = lambda method, path, **kw: sent.append((method, path, kw.get("body"))) or {"id": "x"}
    client.create_task("do it", goal_id="q", related_goal_id="g", new_goal=None, parent_task_id="p")
    client.create_task("do it", goal_id="q", new_goal={"title": "T"})
    client.assign_goal("g1", "u1", reason="r", next_step="n")
    assert sent[0][2]["related_goal_id"] == "g" and "new_goal" not in sent[0][2]
    assert sent[1][2]["new_goal"] == {"title": "T"}
    assert sent[2] == ("PUT", "/api/planning/goals/g1/assign",
                       {"assigned_to_user_id": "u1", "reason": "r", "next_step": "n"})
