"""The standard quest-data tools: one per quest field, plus goals, strategies and habits.

They are the ONLY path an AI write to quest data may take, and each one carries the writing
standard for the one thing it writes (``core/quest_field_instructions.py``). Offline: a fake
QuestClient records what each tool asked the API to do, so these assert the contract (the right
field, the structured ``user_requested`` verdict passed through untouched, an honest report when
the backend turns a change into an ask, and items the user did not ask for never created).
"""
import pytest

from quest_ai_runner.core.quest_field_instructions import (
    GOAL_PERIOD_NUANCES,
    ITEM_INSTRUCTIONS,
    QUEST_FIELD_INSTRUCTIONS,
    goal_instructions_text,
    period_kind,
    render_compact_block,
    render_markdown,
)
from quest_ai_runner.core.tools import (
    QUEST_DATA_TOOLS,
    QUEST_FIELD_TOOLS,
    ToolContext,
    add_measurable_outcome_spec,
    add_quest_strategy_spec,
    build_tool_registry,
    create_quest_goal_spec,
    create_quest_experiment_spec,
    create_quest_habit_spec,
    find_quest_experiments_spec,
    quest_field_tool_spec,
    update_quest_experiment_spec,
)

CREDS = {"QUEST_BASE_URL": "https://example.invalid", "QUEST_API_KEY": "key"}


class FakeQuestClient:
    def __init__(self, response=None, quest=None):
        self.response = {"quest_id": "quest_abc"} if response is None else response
        self.quest = quest or {}
        self.calls = []

    def edit_quest_field(self, quest_id, fields, *, actor="ai", user_requested=False):
        self.calls.append(("edit", quest_id, dict(fields), actor, user_requested))
        return self.response

    def get_my_quest(self, quest_id):
        return {"quest_id": quest_id, "state": self.quest}

    def create_goal(self, title, *, period, quest_id=None, **kw):
        self.calls.append(("goal", quest_id, title, period, kw))
        return {"id": "g1", "title": title}

    def add_quest_strategy(self, quest_id, title, description="", *, actor="ai",
                           user_requested=False):
        self.calls.append(("strategy", quest_id, title, description, actor, user_requested))
        return self.response

    experiments = [
        {"research_question_id": "rq_default", "kind": "default", "status": "open",
         "question": "What will it take to run a marathon?"},
        {"research_question_id": "rq_sleep", "kind": "custom", "status": "experimenting",
         "question": "Does a cold shower help my sleep?", "hypothesis": "Yes",
         "context": "Evenings after training"},
        {"research_question_id": "rq_diet", "kind": "custom", "status": "open",
         "question": "Which diet best supports my long runs?"},
    ]

    def list_experiments(self, quest_id):
        self.calls.append(("list_experiments", quest_id))
        return list(self.experiments)

    def create_experiment(self, quest_id, question, **kw):
        self.calls.append(("create_experiment", quest_id, question, kw))
        return {"research_question_id": "rq_new", "status": "open", "question": question}

    def update_experiment(self, quest_id, rid, **changes):
        self.calls.append(("update_experiment", quest_id, rid, changes))
        return {"research_question_id": rid, "status": changes.get("status", "open")}

    def create_habit(self, quest_id, strategy_id, name, *, frequency="daily", **kw):
        self.calls.append(("habit", quest_id, strategy_id, name, frequency, kw))
        return {"id": "h1"}


def factory(client):
    return lambda: client


# --- the instructions themselves ----------------------------------------------------------------

def test_every_named_field_and_item_has_written_instructions():
    for key in ("outcome", "current_state", "preferences", "measurable_outcomes", "scope",
                "purpose", "acceptance_criteria", "quest_goal", "quest_completion_criteria"):
        spec = QUEST_FIELD_INSTRUCTIONS[key]
        assert spec.what_it_is and spec.good_looks_like and spec.rules
        assert spec.weak_example and spec.strong_example
    assert set(ITEM_INSTRUCTIONS) == {"goal", "strategy", "habit"}


@pytest.mark.parametrize("period,kind", [("2026-10-11", "day"), ("2026_W41", "week"),
                                         ("2026_10", "month"), ("2026_Q4", "quarter"),
                                         ("2026", "year"), ("week", "week"), ("soon", None)])
def test_period_kind(period, kind):
    assert period_kind(period) == kind


def test_goal_instructions_cover_every_period_and_narrow_to_one():
    assert set(GOAL_PERIOD_NUANCES) == {"day", "week", "month", "quarter", "year"}
    everything = goal_instructions_text()
    assert all(n in everything for n in GOAL_PERIOD_NUANCES.values())
    week_only = goal_instructions_text("2026_W41")
    assert GOAL_PERIOD_NUANCES["week"] in week_only
    assert GOAL_PERIOD_NUANCES["year"] not in week_only


def test_review_document_and_compact_block_carry_every_section():
    doc = render_markdown()
    compact = render_compact_block()
    for spec in [*QUEST_FIELD_INSTRUCTIONS.values(), *ITEM_INSTRUCTIONS.values()]:
        assert f"## {spec.label}" in doc
        assert spec.label.upper() in compact
    assert "—" not in doc


# --- one tool per field ---------------------------------------------------------------------------

def test_every_quest_field_tool_carries_its_own_standard_and_only_its_own():
    for field_name, tool in QUEST_FIELD_TOOLS.items():
        spec = quest_field_tool_spec(field_name, {}, client_factory=factory(FakeQuestClient()))
        assert spec.name == tool
        assert QUEST_FIELD_INSTRUCTIONS[field_name].good_looks_like in spec.description
        others = [s for k, s in QUEST_FIELD_INSTRUCTIONS.items() if k != field_name]
        assert not any(o.good_looks_like in spec.description for o in others)
        assert "never write code" in spec.when_to_use.lower()


def test_a_field_tool_writes_only_its_field_as_the_ai_with_the_verdict():
    client = FakeQuestClient()
    spec = quest_field_tool_spec("current_state", {}, client_factory=factory(client))
    result = spec.handler({"quest_id": "q1", "value": "Week 3, 12 miles",
                           "user_asked_for_this_field": True}, ToolContext())
    assert result.ok
    assert client.calls == [("edit", "q1", {"current_state": "Week 3, 12 miles"}, "ai", True)]


def test_verdict_defaults_to_false_and_quest_comes_from_context():
    client = FakeQuestClient()
    quest_field_tool_spec("outcome", {}, client_factory=factory(client)).handler(
        {"value": "x"}, ToolContext(quest_id="quest_ctx"))
    assert client.calls[0][1] == "quest_ctx"
    assert client.calls[0][4] is False


def test_a_change_held_for_approval_is_reported_as_not_applied():
    client = FakeQuestClient({"applied": False, "reason": "autopilot_off", "decision_id": "d1"})
    result = quest_field_tool_spec("outcome", {}, client_factory=factory(client)).handler(
        {"quest_id": "q1", "value": "new", "user_asked_for_this_field": True}, ToolContext())
    assert result.ok and "NOT applied" in result.text and "autopilot_off" in result.text


def test_failed_or_empty_writes_report_nothing_written():
    client = FakeQuestClient({})
    spec = quest_field_tool_spec("purpose", {}, client_factory=factory(client))
    assert "Nothing was written" in spec.handler({"quest_id": "q1", "value": "v"},
                                                 ToolContext()).text
    assert not spec.handler({"quest_id": "q1", "value": "  "}, ToolContext()).ok
    assert not spec.handler({"value": "v"}, ToolContext()).ok


def test_scope_is_not_gated_by_the_backend_so_the_tool_refuses_an_unrequested_write():
    client = FakeQuestClient()
    spec = quest_field_tool_spec("scope", {}, client_factory=factory(client))
    assert not spec.handler({"quest_id": "q1", "value": "In scope: x"}, ToolContext()).ok
    assert client.calls == []
    assert spec.handler({"quest_id": "q1", "value": "In scope: x",
                         "user_asked_for_this_field": True}, ToolContext()).ok


# --- measurable outcomes, goals, strategies, habits ---------------------------------------------

def test_measurable_outcome_is_appended_keeping_the_existing_list():
    existing = [{"id": "m1", "text": "Reach 1,000 subscribers", "completed": False}]
    client = FakeQuestClient(quest={"measurable_outcomes": existing})
    spec = add_measurable_outcome_spec({}, client_factory=factory(client))
    result = spec.handler({"quest_id": "q1", "text": "Publish 40 videos", "target_value": 40,
                           "target_unit": "videos", "user_asked_for_this_field": True},
                          ToolContext())
    assert result.ok
    _, _, fields, actor, requested = client.calls[0]
    assert fields["measurable_outcomes"][0] == existing[0]
    assert fields["measurable_outcomes"][1]["target_value"] == 40
    assert (actor, requested) == ("ai", True)


def test_duplicate_measurable_outcome_is_not_added():
    client = FakeQuestClient(quest={"measurable_outcomes": [{"text": "Publish 40 videos"}]})
    spec = add_measurable_outcome_spec({}, client_factory=factory(client))
    assert "Not added" in spec.handler({"quest_id": "q1", "text": "publish 40 videos"},
                                       ToolContext()).text
    assert client.calls == []


def test_goal_tool_carries_every_period_nuance_and_needs_the_users_ask():
    client = FakeQuestClient()
    spec = create_quest_goal_spec({}, client_factory=factory(client))
    assert all(n in spec.description for n in GOAL_PERIOD_NUANCES.values())
    refused = spec.handler({"quest_id": "q1", "title": "Run 10 km", "period": "2026_W41"},
                           ToolContext())
    assert not refused.ok and client.calls == []
    ok = spec.handler({"quest_id": "q1", "title": "Run 10 km", "period": "2026_W41",
                       "criteria": "GPS shows 10 km", "user_asked_for_this_field": True},
                      ToolContext())
    assert ok.ok
    assert client.calls == [("goal", "q1", "Run 10 km", "2026_W41",
                             {"criteria": "GPS shows 10 km"})]


def test_strategy_tool_passes_actor_and_verdict_and_reports_a_held_change():
    client = FakeQuestClient({"applied": False, "reason": "not_user_requested"})
    spec = add_quest_strategy_spec({}, client_factory=factory(client))
    result = spec.handler({"quest_id": "q1", "title": "Progressive long run"}, ToolContext())
    assert result.ok and "NOT applied" in result.text
    assert client.calls[0][4:] == ("ai", False)


def test_habit_tool_defaults_to_the_first_accepted_strategy_and_needs_the_users_ask():
    client = FakeQuestClient(quest={"strategies": [{"id": "s0", "accepted": False},
                                                   {"id": "s1", "accepted": True}]})
    spec = create_quest_habit_spec({}, client_factory=factory(client))
    assert not spec.handler({"quest_id": "q1", "name": "Run 20 min"}, ToolContext()).ok
    result = spec.handler({"quest_id": "q1", "name": "Run 20 min", "frequency": "weekly",
                           "days": ["Mon", "Wed"], "user_asked_for_this_field": True},
                          ToolContext())
    assert result.ok
    assert client.calls == [("habit", "q1", "s1", "Run 20 min", "weekly",
                             {"days": ["Mon", "Wed"]})]


def test_habit_without_any_strategy_is_refused():
    client = FakeQuestClient(quest={"strategies": []})
    spec = create_quest_habit_spec({}, client_factory=factory(client))
    result = spec.handler({"quest_id": "q1", "name": "Run", "user_asked_for_this_field": True},
                          ToolContext())
    assert not result.ok and "add_quest_strategy" in result.text


# --- the catalog ----------------------------------------------------------------------------------

def test_the_quest_data_tools_are_standard_whenever_quest_credentials_exist():
    registry = build_tool_registry({**CREDS, "QAR_STANDARD_TOOLS": "1"})
    assert set(QUEST_DATA_TOOLS) <= set(registry.names())
    assert "update_quest_fields" not in registry.names()  # the generic writer is gone
    assert all(registry.get(n).mutates for n in QUEST_DATA_TOOLS)


def test_absent_without_quest_credentials():
    assert not set(QUEST_DATA_TOOLS) & set(build_tool_registry({"QAR_STANDARD_TOOLS": "1"}).names())


def test_a_field_request_selects_that_fields_tool():
    registry = build_tool_registry(CREDS)
    assert registry.select("change my current state to week 3", k=5)[0].name == \
        "update_quest_current_state"
    assert "create_quest_goal" in [s.name for s in
                                   registry.select("add a goal for this week", k=5)]


# --- experiments ----------------------------------------------------------------------------------

def test_find_experiments_lists_and_searches_by_text_without_writing():
    client = FakeQuestClient()
    spec = find_quest_experiments_spec({}, client_factory=factory(client))
    assert not spec.mutates
    listed = spec.handler({"quest_id": "q1"}, ToolContext())
    assert listed.ok and "rq_sleep" in listed.text and "rq_diet" in listed.text
    found = spec.handler({"quest_id": "q1", "query": "my sleep shower"}, ToolContext())
    assert found.ok and found.data[0]["research_question_id"] == "rq_sleep" and len(found.data) == 1
    none = spec.handler({"quest_id": "q1", "query": "meditation"}, ToolContext())
    assert none.ok and none.data == [] and "No experiment" in none.text
    assert spec.handler({"quest_id": "q1", "query": "the"}, ToolContext()).data == []  # no usable word
    assert all(c[0] == "list_experiments" for c in client.calls)


def test_find_experiments_reports_a_failed_read_not_an_empty_list():
    class Broken(FakeQuestClient):
        def list_experiments(self, quest_id):
            raise RuntimeError("503")
    spec = find_quest_experiments_spec({}, client_factory=factory(Broken()))
    assert not spec.handler({"quest_id": "q1"}, ToolContext()).ok


def test_create_experiment_needs_the_users_ask_and_passes_only_given_fields():
    client = FakeQuestClient()
    spec = create_quest_experiment_spec({}, client_factory=factory(client))
    assert not spec.handler({"quest_id": "q1", "question": "Does oat milk help?"}, ToolContext()).ok
    assert client.calls == []
    result = spec.handler({"quest_id": "q1", "question": "Does oat milk help?", "hypothesis": "",
                           "linked_goal_ids": ["g1"], "user_asked_for_this_field": True},
                          ToolContext(quest_id="q1"))
    assert result.ok and "rq_new" in result.text
    assert client.calls == [("create_experiment", "q1", "Does oat milk help?",
                             {"linked_goal_ids": ["g1"]})]


def test_update_experiment_sends_only_changed_keys_and_validates_status():
    client = FakeQuestClient()
    spec = update_quest_experiment_spec({}, client_factory=factory(client))
    base = {"quest_id": "q1", "research_question_id": "rq_sleep"}
    assert not spec.handler({**base, "status": "concluded"}, ToolContext()).ok  # no ask
    assert not spec.handler({**base, "user_asked_for_this_field": True}, ToolContext()).ok  # no change
    assert not spec.handler({**base, "status": "done", "user_asked_for_this_field": True},
                            ToolContext()).ok
    ok = spec.handler({**base, "status": "concluded", "conclusion": "Slept better",
                       "user_asked_for_this_field": True}, ToolContext())
    assert ok.ok
    assert client.calls == [("update_experiment", "q1", "rq_sleep",
                             {"status": "concluded", "conclusion": "Slept better"})]


def test_experiment_tools_are_standard_and_selected_by_their_words():
    registry = build_tool_registry(CREDS)
    assert {"find_quest_experiments", "create_quest_experiment",
            "update_quest_experiment"} <= set(registry.names())
    assert not registry.get("find_quest_experiments").mutates
    assert registry.select("conclude my sleep experiment", k=3)[0].name in (
        "update_quest_experiment", "find_quest_experiments")
    assert registry.select("start an experiment on cold showers", k=3)[0].name == \
        "create_quest_experiment"


def test_experiment_search_ignores_filler_stems_and_the_default_questions_context():
    from quest_ai_runner.core.tools import search_experiments
    rows = FakeQuestClient.experiments + [
        {"research_question_id": "rq_ctx", "kind": "default", "status": "open",
         "question": "What will it take to run a marathon?",
         "context": "sleep sleep sleep outcome current state"}]
    ids = lambda q: [r["research_question_id"] for r in search_experiments(rows, q)]
    assert ids("find the experiment about") == []
    assert ids("sleeping showers") == ["rq_sleep"]  # stemmed; the default's context is not searched
    assert set(ids("marathon")) == {"rq_default", "rq_ctx"}
    assert ids("current state outcome") == []  # only the default question text counts, not its context


@pytest.mark.parametrize("message,expected", [
    ("show me the experiments on this quest", "find_quest_experiments"),
    ("find my sleep experiment", "find_quest_experiments"),
    ("what did my diet experiment conclude", "find_quest_experiments"),
    ("start an experiment on cold showers and sleep", "create_quest_experiment"),
    ("set up a new experiment to test oats before long runs", "create_quest_experiment"),
    ("add a trial to see if stretching helps", "create_quest_experiment"),
    ("conclude my sleep experiment", "update_quest_experiment"),
    ("rename the diet experiment and change its status", "update_quest_experiment"),
    ("update the hypothesis on my experiment", "update_quest_experiment"),
])
def test_experiment_phrasings_select_a_right_tool_offline(message, expected):
    """Cheap tool-surface routing check: no model call, the catalog's own selection."""
    names = [s.name for s in build_tool_registry(CREDS).select(message, k=3)]
    assert expected in names, names


def test_experiment_purpose_sub_questions_and_assumptions_flow_through_create_update_and_find():
    framing = {"purpose": "Tie sleep to my energy goal",
               "sub_questions": [{"question": "Does it shorten time to fall asleep?",
                                  "answer_method": "compare weekly averages"}, {"question": "Does it help mornings?"}],
               "assumptions": ["Meditating for a long period without getting lost in thought helps"]}
    client = FakeQuestClient()
    create = create_quest_experiment_spec({}, client_factory=factory(client))
    assert create.handler({"quest_id": "q1", "question": "Does a cold shower help?", **framing,
                           "user_asked_for_this_field": True}, ToolContext()).ok
    assert client.calls[-1] == ("create_experiment", "q1", "Does a cold shower help?", framing)
    assert {"purpose", "sub_questions", "assumptions"} <= set(create.parameters["properties"])

    update = update_quest_experiment_spec({}, client_factory=factory(client))
    assert update.handler({"quest_id": "q1", "research_question_id": "rq_sleep",
                           "assumptions": ["Sleep is measured by the tracker"],
                           "user_asked_for_this_field": True}, ToolContext()).ok
    assert client.calls[-1] == ("update_experiment", "q1", "rq_sleep",
                                {"assumptions": ["Sleep is measured by the tracker"]})

    rows = [{"research_question_id": "rq_a", "kind": "custom", "question": "Does X help?", "status": "open",
             **framing}, {"research_question_id": "rq_b", "kind": "custom", "question": "Other", "status": "open"}]
    found = find_quest_experiments_spec({}, client_factory=factory(FakeQuestClient()))
    client2 = FakeQuestClient()
    client2.experiments = rows
    found = find_quest_experiments_spec({}, client_factory=factory(client2))
    hit = found.handler({"quest_id": "q1", "query": "weekly averages"}, ToolContext())
    assert [r["research_question_id"] for r in hit.data] == ["rq_a"]
    assert "purpose: Tie sleep" in hit.text and "how: compare weekly averages" in hit.text
    assert "assumptions: Meditating" in hit.text
    plain = found.handler({"quest_id": "q1"}, ToolContext())
    assert "rq_b" in plain.text
