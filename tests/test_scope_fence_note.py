"""A turn bound to a quest is TOLD its scope, in the context the planner and the answer both read.

Cause found in a recorded chat miss: a chat pinned to one quest was asked "what's my launch budget cap?", a
question whose answer is recorded only on a different quest of the same account. The card fence kept the other
quest's cards out, but the model's own reads returned that quest's records and a cheap model answers from what it
holds. The product ruling for this check: using another quest's private data to answer a quest-specific question is a
failure. The scope now rides the turn's structured quest ids (set by the caller, never read from model words)
into the context view once per turn; an unbound turn gets nothing, so a question that compares quests still works.

Fully offline, no model calls.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.scope_tags import SCOPE_FENCE_NOTE, scope_fence_block

from .conftest import StubProvider, StubRetrieval

HEAD = "--- THIS CONVERSATION'S SCOPE (set by the system) ---"


def test_the_note_names_the_ids_and_is_empty_for_an_unbound_turn():
    one = scope_fence_block(["quest_a"])
    assert one.startswith(HEAD) and "quest quest_a" in one and "that quest" in one
    many = scope_fence_block(["quest_a", "quest_b"])
    assert "quests quest_a, quest_b" in many and "those quests" in many
    for unbound in (None, [], "", [None, ""]):
        assert scope_fence_block(unbound) == "", unbound
    assert "—" not in SCOPE_FENCE_NOTE and "{" not in one


def test_the_note_keeps_what_belongs_to_no_quest_usable():
    assert "Things that belong to no quest" in scope_fence_block(["quest_a"])


def build(provider, **kw):
    config = OrchestratorConfig()
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval({"a.md": "GROUNDING cap"}), provider=provider,
                        registry=ModelRegistry(provider), config=config, **kw)


def answers(provider):
    return "\n".join(str(m["content"]) for m in provider.last_answer_messages)


def test_a_bound_turn_shows_the_scope_to_the_planner_and_to_the_answer():
    provider = StubProvider([
        {"action": "read", "reads": [{"rel_path": "a.md"}], "user_intent": "ask", "rationale": "read"},
        {"action": "answer", "user_intent": "ask", "rationale": "answer", "pending_read": []}])
    build(provider).run("What's my launch budget cap?", context_meta={"quest_ids": ["quest_fit"]})
    assert all(HEAD in prompt and "quest quest_fit" in prompt for prompt in provider.plan_prompts[:1])
    assert HEAD in answers(provider) and "quest quest_fit" in answers(provider)
    # The note rides ahead of the context once; it is not repeated by later steps' context.
    assert provider.plan_prompts[0].count(HEAD) == 1


def test_a_turn_given_a_single_quest_id_is_bound_too():
    provider = StubProvider([{"action": "answer", "user_intent": "ask", "rationale": "a", "pending_read": []}])
    build(provider).run("What's my cap?", quest_id="quest_one")
    assert HEAD in provider.plan_prompts[0] and "quest quest_one" in provider.plan_prompts[0]


def test_an_unbound_turn_gets_no_note_so_a_comparison_across_quests_still_works():
    provider = StubProvider([{"action": "answer", "user_intent": "ask", "rationale": "a", "pending_read": []}])
    build(provider).run("Which is bigger, my launch expenses or my bathroom costs?")
    assert HEAD not in provider.plan_prompts[0] and HEAD not in answers(provider)


def test_a_turn_that_is_not_a_persons_message_gets_no_note():
    provider = StubProvider([{"action": "answer", "user_intent": "ask", "rationale": "a", "pending_read": []}])
    build(provider).run("Summarise the week", context_meta={"quest_ids": ["quest_fit"]},
                        message_is_user_turn=False)
    assert HEAD not in provider.plan_prompts[0]
