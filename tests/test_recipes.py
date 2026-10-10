"""Recipe fast path (core/recipes.py): an operation worked out once is replayed before any context
search, a near-miss is not, and nothing a recipe does can make a turn fail."""
import pytest

from .conftest import StubProvider, StubRetrieval
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.recipes import RecipeStore, content_tokens, similarity
from quest_ai_runner.core.tools import ToolRegistry, ToolResult, ToolSpec


class CountingAssembler:
    """A context assembler that records whether turn-start context search ever started."""
    def __init__(self):
        self.calls = 0

    def assemble(self, query, meta=None):
        self.calls += 1
        return None

    def record(self, *a, **k):
        pass


def habit_registry(calls, *, mutates=True, fail=False):
    def handler(args, ctx):
        calls.append(dict(args))
        if fail:
            return ToolResult(ok=False, text="habit not found")
        return ToolResult(ok=True, text=f"Marked {args['habit']} done.")
    return ToolRegistry([ToolSpec(
        name="complete_habit", description="Mark a habit done today.", handler=handler,
        when_to_use="mark a habit done", when_not_to_use="reading habits", mutates=mutates,
        keywords=("habit", "done"),
        parameters={"type": "object", "required": ["habit"],
                    "properties": {"habit": {"type": "string"}}})])


def make_orch(provider, tools, store, assembler=None, **cfg_kw):
    cfg = OrchestratorConfig(**cfg_kw)
    cfg.overseer = False
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=cfg, tools=tools,
                        recipes=store, context_assembler=assembler)


LEARN = [
    {"action": "tool", "rationale": "tool covers it",
     "tool_calls": [{"name": "complete_habit", "args": {"habit": "meditation"}}]},
    {"action": "answer", "rationale": "done"},
]


def learned_store(calls=None, **kw):
    store = RecipeStore()
    provider = StubProvider(decisions=list(LEARN))
    make_orch(provider, habit_registry(calls if calls is not None else []), store, **kw).run(
        "mark my meditation habit done", quest_id="q1")
    return store


def test_similarity_ranks_the_same_operation_above_a_different_one():
    a = content_tokens("mark my meditation habit done")
    assert similarity(a, content_tokens("mark the yoga habit as done")) > \
        similarity(a, content_tokens("what is the weather in Lisbon"))


def test_a_successful_planner_tool_call_is_learned_as_a_recipe():
    store = learned_store()
    recipes = store.all()
    assert len(recipes) == 1
    assert recipes[0].tool == "complete_habit"
    assert recipes[0].examples == ["mark my meditation habit done"]
    assert recipes[0].scope_tags == ["quest:q1"]


def test_a_failed_tool_call_is_not_learned():
    store = RecipeStore()
    provider = StubProvider(decisions=list(LEARN))
    make_orch(provider, habit_registry([], fail=True), store).run(
        "mark my meditation habit done", quest_id="q1")
    assert store.all() == []


def test_matching_request_runs_the_recipe_before_context_search_or_planning():
    store = learned_store()
    calls, assembler = [], CountingAssembler()
    provider = StubProvider(decisions=[{"applies": True, "args": {"habit": "yoga"}}])
    res = make_orch(provider, habit_registry(calls), store, assembler).run(
        "mark my yoga habit done", quest_id="q1")
    assert res.kind == "answer" and res.exit_reason == "recipe"
    assert res.text == "Marked yoga done."
    assert calls == [{"habit": "yoga"}]
    assert assembler.calls == 0                 # context search never started
    assert provider.plan_calls == 1             # the one small args call, no planner
    assert provider.answer_calls == 0
    assert res.execution_record.any_success


def test_near_miss_falls_through_to_the_normal_planner():
    store = learned_store()
    calls = []
    provider = StubProvider(decisions=[
        {"applies": False, "args": {}},
        {"action": "answer", "rationale": "it is a question"},
    ])
    res = make_orch(provider, habit_registry(calls), store).run(
        "mark my meditation habit done streak report", quest_id="q1")
    assert calls == []
    assert res.exit_reason != "recipe"
    assert provider.plan_calls >= 2             # the args call, then the normal planner


def test_unrelated_request_pays_for_no_extra_call():
    store = learned_store()
    with_store = StubProvider(decisions=[{"action": "answer", "rationale": "chat"}])
    make_orch(with_store, habit_registry([]), store).run("tell me about the weather", quest_id="q1")
    without = StubProvider(decisions=[{"action": "answer", "rationale": "chat"}])
    make_orch(without, habit_registry([]), None).run("tell me about the weather", quest_id="q1")
    assert with_store.plan_calls == without.plan_calls


def test_a_recipe_learned_in_one_quest_is_not_offered_to_another():
    store = learned_store()
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "other quest"}])
    calls = []
    res = make_orch(provider, habit_registry(calls), store).run(
        "mark my meditation habit done", quest_id="q2")
    assert calls == [] and res.exit_reason != "recipe"


def test_failing_replay_takes_the_normal_path():
    store = learned_store()
    provider = StubProvider(decisions=[
        {"applies": True, "args": {"habit": "yoga"}},
        {"action": "answer", "rationale": "tool failed; explain"},
    ])
    res = make_orch(provider, habit_registry([], fail=True), store).run(
        "mark my yoga habit done", quest_id="q1")
    assert res.exit_reason != "recipe"


def test_exact_repeat_of_a_read_only_recipe_needs_no_model_call():
    store = RecipeStore()
    store.learn("list my habits for today", "complete_habit", {"habit": "all"}, scope_tags=["quest:q1"])
    calls = []
    provider = StubProvider(decisions=[])
    res = make_orch(provider, habit_registry(calls, mutates=False), store).run(
        "List my habits for today!", quest_id="q1")
    assert res.exit_reason == "recipe" and provider.plan_calls == 0
    assert calls == [{"habit": "all"}]


def test_mutating_exact_repeat_still_rederives_its_arguments():
    store = learned_store()
    provider = StubProvider(decisions=[{"applies": True, "args": {"habit": "meditation"}}])
    res = make_orch(provider, habit_registry([]), store).run(
        "mark my meditation habit done", quest_id="q1")
    assert res.exit_reason == "recipe" and provider.plan_calls == 1


def test_disabled_flag_and_missing_store_leave_the_run_untouched():
    store = learned_store()
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}])
    res = make_orch(provider, habit_registry([]), store, recipe_fast_path=False).run(
        "mark my meditation habit done", quest_id="q1")
    assert res.exit_reason != "recipe"
    provider2 = StubProvider(decisions=[{"action": "answer", "rationale": "x"}])
    res2 = make_orch(provider2, habit_registry([]), None).run(
        "mark my meditation habit done", quest_id="q1")
    assert res2.exit_reason != "recipe"


def test_store_persists_and_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "r" / "recipes.json"
    s1 = RecipeStore(path)
    s1.learn("mark my meditation habit done", "complete_habit", {"habit": "meditation"})
    assert len(RecipeStore(path).all()) == 1
    path.write_text("{not json")
    assert RecipeStore(path).all() == []


def test_single_word_requests_are_never_learned_or_matched():
    store = RecipeStore()
    assert store.learn("yes", "complete_habit", {"habit": "x"}) is None
    store.learn("mark my meditation habit done", "complete_habit", {"habit": "meditation"})
    assert store.match("yes") == []


def test_skeleton_ignores_payload_words_so_free_text_requests_still_match():
    store = RecipeStore()
    store.learn("add a note saying call the dentist on Friday", "add_note",
                {"text": "call the dentist on Friday"})
    assert store.match("add a note saying buy oat milk and two lemons for the weekend")
    # the operation's words are absent: a different request, however much payload it shares
    assert store.match("remind me to call the dentist on Friday") == []
