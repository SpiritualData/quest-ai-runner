"""A message pushed by conversation id must reach the run of that conversation.

The Quest chat pushes a voice barge-in to the shared inbox under its conversation id. The turn is
run with ``conv_id=<that id>`` and, in a quest-bound chat, ``quest_id=<the quest>``. The
orchestrator's auto-wired drain used to read ``quest_id or context conversation_id ...`` and never
looked at ``conv_id``, so in a quest chat the message sat in memory under the conversation id while
the drain read the quest id. These tests pin that the drain reads the conversation id first.
"""
from quest_ai_runner.core.inbox import InMemoryInbox
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval


def build(provider, inbox):
    cfg = OrchestratorConfig()
    cfg.overseer = False
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=cfg, input_inbox=inbox)


def answer_provider():
    return StubProvider(decisions=[{"action": "answer", "model_tier": "haiku", "rationale": "ok"}])


def test_quest_bound_chat_drains_the_conversation_id():
    inbox = InMemoryInbox()
    inbox.push("conv-1", "actually make it shorter")
    provider = answer_provider()
    build(provider, inbox).run("hello", quest_id="quest-9", conv_id="conv-1")
    assert "actually make it shorter" in provider.plan_prompts[0]
    assert inbox.peek("conv-1") == []


def test_chat_without_a_quest_drains_the_conversation_id():
    inbox = InMemoryInbox()
    inbox.push("conv-2", "and add a summary")
    provider = answer_provider()
    build(provider, inbox).run("hello", conv_id="conv-2")
    assert "and add a summary" in provider.plan_prompts[0]


def test_other_conversation_is_not_drained():
    inbox = InMemoryInbox()
    inbox.push("conv-other", "not for you")
    provider = answer_provider()
    build(provider, inbox).run("hello", quest_id="quest-9", conv_id="conv-1")
    assert "not for you" not in provider.plan_prompts[0]
    assert inbox.peek("conv-other") == ["not for you"]


def test_quest_keyed_push_still_drains():
    inbox = InMemoryInbox()
    inbox.push("quest-9", "keyed by quest")
    provider = answer_provider()
    build(provider, inbox).run("hello", quest_id="quest-9", conv_id="conv-1")
    assert "keyed by quest" in provider.plan_prompts[0]
