"""create_quest_goal's owner and link arguments, the ensure-one goal tool (created, updated in place, unchanged), the spec read and edit tools,
and the dry-run allowlist naming them. A fake Quest client stands in for the API."""
from quest_ai_runner.the_body.hands.quest_operations import tools_quest_goals as goals
from quest_ai_runner.the_body.hands.quest_operations import tools_quest_intents as intents
from quest_ai_runner.the_body.hands.quest_operations import tools_quest_standard as standard
from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import WriteResult
from quest_ai_runner.the_world.tools import dry_run_writes as dry
from quest_ai_runner.the_world.tools.tools import ToolContext, build_tool_registry
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

CREDS = {"QUEST_BASE_URL": "https://example.invalid", "QUEST_API_KEY": "key"}
MEMBERS = [{"user_id": "u1", "name": "Ann Example", "email": "ann@example.invalid"},
           {"user_id": "u2", "name": "Bo Example", "email": "bo@example.invalid"}]


class FakeClient:
    def __init__(self, goals=None, listing_ok=True, held=False):
        self.goals, self.listing_ok, self.held, self.calls = list(goals or []), listing_ok, held, []

    def list_assignable_members(self, quest_id):
        return MEMBERS

    def list_quest_goals(self, quest_id, **kw):
        return {"period_groups": [{"goals": self.goals}]} if self.listing_ok else {}

    def create_goal(self, title, **kw):
        self.calls.append(("create", title, kw))
        return WriteResult({"id": "g_new", "title": title}, id="g_new")

    def update_goal(self, goal_id, fields):
        self.calls.append(("update", goal_id, fields))
        return WriteResult({"id": goal_id}, ToolStatus.HELD if self.held else ToolStatus.APPLIED)

    def assign_goal(self, goal_id, user_id, **kw):
        self.calls.append(("assign", goal_id, user_id))
        return WriteResult({"id": goal_id})


ARGS = dict(title="P1 next step: week of 2026-11-02", period="2026_W45", quest_id="q1", user_asked_for_this_field=True,
            assignee="Ann", spec_solution_id="esr_s", spec_metric_id="esr_m", metric_target_value=5)


def ensure(client, **over):
    return goals.create_or_update_quest_goal_spec(CREDS, client_factory=lambda: client).handler({**ARGS, **over}, ToolContext(quest_id="q1"))


def test_create_goal_sends_owner_and_links():
    client = FakeClient()
    spec = standard.create_quest_goal_spec(CREDS, client_factory=lambda: client)
    assert spec.handler(ARGS, ToolContext(quest_id="q1")).ok
    kind, title, kw = client.calls[0]
    assert kw["assigned_to_user_id"] == "u1" and kw["spec_solution_id"] == "esr_s" and kw["spec_metric_id"] == "esr_m" and kw["metric_target_value"] == 5


def test_unknown_assignee_creates_nothing():
    client = FakeClient()
    result = ensure(client, assignee="Nobody")
    assert not result.ok and "Members: Ann Example, Bo Example" in result.text and client.calls == []


def test_ensure_creates_when_absent():
    client = FakeClient()
    result = ensure(client)
    assert result.ok and result.data["state"] == "created" and client.calls[0][0] == "create"


def test_ensure_updates_in_place_and_never_duplicates():
    existing = {"id": "g1", "name": ARGS["title"], "period": "2026_W45", "assigned_to_user_id": "u2", "spec_solution_id": "esr_old"}
    client = FakeClient([existing])
    result = ensure(client)
    assert result.ok and result.data["state"] == "updated"
    kinds = [c[0] for c in client.calls]
    assert "create" not in kinds and ("assign", "g1", "u1") in client.calls
    update = next(c for c in client.calls if c[0] == "update")
    assert update[2]["spec_solution_id"] == "esr_s" and "period" not in update[2]


def test_ensure_unchanged_when_all_match():
    existing = {"id": "g1", "name": ARGS["title"], "period": "2026_W45", "assigned_to_user_id": "u1", "spec_solution_id": "esr_s",
                "spec_metric_id": "esr_m", "metric_target_value": 5}
    client = FakeClient([existing])
    result = ensure(client)
    assert result.ok and result.data["state"] == "unchanged" and client.calls == [] and result.status is ToolStatus.NOOP


def test_ensure_repeat_with_same_description_is_unchanged_and_marker_is_kept():
    existing = {"id": "g1", "name": ARGS["title"], "period": "2026_W45", "assigned_to_user_id": "u1", "spec_solution_id": "esr_s",
                "spec_metric_id": "esr_m", "metric_target_value": 5, "description": "Kept by Quest AI: Next step."}
    client = FakeClient([existing])
    result = ensure(client, description="Next step.")
    assert result.data["state"] == "unchanged" and client.calls == []
    assert ensure(FakeClient(), description="Next step.").ok


def test_ensure_does_not_write_when_goals_unreadable_or_held_is_not_done():
    assert not ensure(FakeClient(listing_ok=False)).ok
    existing = {"id": "g1", "name": ARGS["title"], "period": "2026_W45", "assigned_to_user_id": "u1"}
    held = ensure(FakeClient([existing], held=True))
    assert not held.ok and held.status is ToolStatus.HELD


def test_abandoned_goal_with_same_title_is_not_a_match():
    client = FakeClient([{"id": "g0", "name": ARGS["title"], "abandoned": True}])
    assert ensure(client).data["state"] == "created"


def test_registered_with_planner_guidance():
    registry = build_tool_registry(CREDS)
    for name, mutates in (("create_or_update_quest_goal", True), ("apply_spec_edit", True), ("read_spec_records", False)):
        spec = registry.get(name)
        assert spec is not None and spec.mutates is mutates and spec.when_to_use
    assert "title" in registry.get("create_or_update_quest_goal").when_to_use or "next step" in registry.get("create_or_update_quest_goal").when_to_use


def test_apply_spec_edit_rejects_a_malformed_change():
    spec = intents.apply_spec_edit_spec(CREDS, client_factory=lambda: FakeClient())
    assert not spec.handler({"quest_id": "q1", "change": {"kind": "nope"}}, ToolContext()).ok


def test_allowlist_names_nested_and_prefix_arguments():
    dry.set_allowances([{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title~": "P1 next step: week of ", "assignee": "Sam"}},
                        {"tool": "apply_spec_edit", "args": {"quest_id": "q1", "change.kind": "milestone_add", "change.target": "esr_x"}}])
    try:
        assert dry.tool_allowed("create_or_update_quest_goal", {"quest_id": "q1", "title": "P1 next step: week of 2026-11-02", "assignee": "Sam"})
        assert not dry.tool_allowed("create_or_update_quest_goal", {"quest_id": "q1", "title": "P1 next step: week of 2026-11-02", "assignee": "Kim"})
        assert dry.tool_allowed("apply_spec_edit", {"quest_id": "q1", "change": {"kind": "milestone_add", "target": "esr_x", "key": "k"}})
        assert not dry.tool_allowed("apply_spec_edit", {"quest_id": "q1", "change": {"kind": "record_add", "target": "esr_x"}})
    finally:
        dry.set_allowances([])


def test_dry_run_tells_a_quest_less_call_to_name_the_quest_not_that_it_was_blocked(monkeypatch):
    monkeypatch.setenv("QAR_DRY_RUN_WRITES", "1")
    spec = goals.create_or_update_quest_goal_spec(CREDS, client_factory=lambda: FakeClient())
    before = len(dry.RECORDER.writes)
    result = dry.tool_call(spec, spec.name, {"title": "t"})
    assert not result.ok and "no quest is known" in result.text and len(dry.RECORDER.writes) == before


def test_a_rule_value_may_be_a_list_of_owners_matched_by_first_name():
    dry.set_allowances([{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1", "assignee~": ["Kim", "Hana"]}}])
    try:
        assert dry.tool_allowed("create_or_update_quest_goal", {"quest_id": "q1", "assignee": "Kim Okoye"})
        assert dry.tool_allowed("create_or_update_quest_goal", {"quest_id": "q1", "assignee": "hana"})
        assert not dry.tool_allowed("create_or_update_quest_goal", {"quest_id": "q1", "assignee": "Mary"})
    finally:
        dry.set_allowances([])


def test_the_ensure_tool_takes_the_arguments_the_planner_sends_for_a_next_step():
    client = FakeClient()
    spec = goals.create_or_update_quest_goal_spec(CREDS, client_factory=lambda: client)
    args = {"title": "P3 next step: week of 2026-11-02", "assignee": "Ann", "spec_solution_id": "esr_s", "due_date": "2026-11-06", "period": "week"}
    result = spec.handler(args, ToolContext(quest_id="q1"))          # no period format, no asked flag, a due day
    assert result.ok and client.calls[0][0] == "create"
    kw = client.calls[0][2]
    assert kw["scheduled_date"] == "2026-11-06" and kw["assigned_to_user_id"] == "u1" and kw["period"].count("_W") == 1
    assert not spec.handler({**args, "user_asked_for_this_field": False}, ToolContext(quest_id="q1")).ok


def test_periods_are_normalised():
    import datetime
    from quest_ai_runner.the_body.hands.quest_operations import goal_links
    day = datetime.date(2026, 11, 6)
    assert goal_links.normalize_period("week", day) == "2026_W45" and goal_links.normalize_period(None, day) == "2026_W45"
    assert goal_links.normalize_period("2026_W46", day) == "2026_W46" and goal_links.normalize_period("this month", day) == "2026_11"
