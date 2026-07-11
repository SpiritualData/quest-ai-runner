"""The internal channel never carries the person's own words, and never fabricates a reply.

Round 2 of the leaking-chat-bubble fix. The first pass gave the four REPLY-producing calls a voice
contract (see test_reply_voice_separation.py), which killed the third-person narration inside the
answer. Two leaks survived it, because the answer stage does not produce them:

  1. THE META-ECHO ("Understood as: Hello. How can I help you with your Quests today?") comes from
     the goal-condition calls, ``_derive_goal_condition`` and ``_understand_input``. Those went out
     with NO system prompt, so a cheap model handed a bare "Hello" did the only thing it knows how to
     do with a greeting: it answered it. That answer became the turn's goal condition, and the goal
     condition is what the understanding event carries.

  2. THE VERBATIM CONVERSATION DUMP in the retrieval panel. A conversation-history card is titled
     with the raw user turn it came from (core/turn_context_store.py), and both the card titles and
     the source items rode out on EVENT_CONTEXT untouched, so a consumer that rendered them replayed
     the chat back at the person: "Hi", "User: Hi...", "Hello".

So: a greeting must cost no LLM call and emit no understanding event; the goal-condition calls must
carry GOAL_CONDITION_SYSTEM; and nothing leaving on EVENT_CONTEXT may quote the conversation.
"""
from typing import Any, List

from quest_ai_runner.core.adapters import EVENT_CONTEXT, EVENT_UNDERSTANDING, ProgressEvent
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    GOAL_CONDITION_SYSTEM,
    Orchestrator,
    _project_card_metadata_for_event,
    _project_sources_for_event,
    _safe_event_title,
    is_small_talk,
)

from .conftest import StubProvider, StubRetrieval


class _RecordingSink:
    def __init__(self) -> None:
        self.events: List[ProgressEvent] = []

    def update(self, event: ProgressEvent, mode: Any = None) -> None:
        self.events.append(event)

    def of_type(self, event_type: str) -> List[ProgressEvent]:
        return [e for e in self.events if e.type == event_type]


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# 1. The meta-echo: a greeting has nothing to resolve
# ---------------------------------------------------------------------------

def test_small_talk_recognized():
    for msg in ["Hello", "hi", "Hey!", "thanks", "Thank you so much", "good morning", "ok", "Got it"]:
        assert is_small_talk(msg), msg


def test_a_greeting_that_carries_a_real_request_is_not_small_talk():
    # The short-circuit must not swallow a real request just because it opens politely.
    assert not is_small_talk("hi, can you create a daily shadow work habit for me")
    assert not is_small_talk("thanks, now show me my quests")


def test_greeting_derives_no_goal_condition_and_costs_no_llm_call():
    """"Hello" must not become "Hello. How can I help you with your Quests today?"."""
    provider = StubProvider(decisions=[], answer_text="Hello. How can I help you with your Quests?")
    orch = _orch(provider, StubRetrieval())

    goal_condition, constraints = orch._derive_goal_condition("Hello")

    assert goal_condition == "Hello", "the greeting is its own goal condition, unembellished"
    assert constraints is None
    assert provider.answer_calls == 0, "a greeting must cost zero LLM calls"


def test_goal_condition_call_carries_the_understanding_contract():
    """The 5th call the first fix missed. It must not go out bare."""
    provider = StubProvider(decisions=[], answer_text="Create a habit named Daily Shadow Work")
    orch = _orch(provider, StubRetrieval())

    orch._derive_goal_condition("make me a daily shadow work reflection habit")

    assert provider.answer_calls == 1
    assert GOAL_CONDITION_SYSTEM in provider.answer_systems, \
        "goal-condition calls must pass system=GOAL_CONDITION_SYSTEM"
    assert None not in provider.answer_systems, "no understanding call may go out with no system"


def test_understanding_contract_forbids_answering_the_message():
    """A future prompt edit must not silently drop the clauses that stop the model replying."""
    text = GOAL_CONDITION_SYSTEM.lower()
    assert "never answer the message" in text
    assert "never greet" in text
    assert "not in a conversation" in text
    assert "understood as" in text, "it must forbid emitting the preamble itself"
    assert "—" not in GOAL_CONDITION_SYSTEM, "brand voice: no em dashes"


# ---------------------------------------------------------------------------
# 2. The verbatim conversation dump on EVENT_CONTEXT
# ---------------------------------------------------------------------------

def test_conversation_card_title_is_described_not_quoted():
    assert _safe_event_title({"title": "Hi", "adapter": "turn"}) == "Conversation turn"
    assert _safe_event_title(
        {"title": "User: Hi, I want to talk about shadow work", "adapter": ""}
    ) == "Conversation turn"


def test_ordinary_card_title_survives_but_is_flattened():
    assert _safe_event_title({"title": "quest-creation-guide.md", "adapter": "vector"}) \
        == "quest-creation-guide.md"
    assert len(_safe_event_title({"title": "x" * 200, "adapter": "vector"})) <= 80


def test_projected_cards_never_carry_a_raw_turn():
    cards = [
        {"id": "1", "title": "Hello, I have been journaling", "adapter": "turn"},
        {"id": "2", "title": "README.md", "adapter": "vector"},
    ]
    titles = [c["title"] for c in _project_card_metadata_for_event(cards)]

    assert titles == ["Conversation turn", "README.md"]
    assert not any("journaling" in t for t in titles)


def test_projected_sources_drop_free_text_items_and_keep_counts():
    sources = [
        {"adapter": "turn", "label": "conversation history",
         "items": ["User: Hi\nAssistant: Hello there", "User: Hello"]},
        {"adapter": "vector", "label": "semantic match",
         "items": ["docs/quest-creation-guide.md", "README.md"]},
    ]
    turn_src, vector_src = _project_sources_for_event(sources)

    assert "items" not in turn_src, "raw conversation turns must not leave the process"
    assert turn_src["item_count"] == 2, "the count still tells a debug surface what was drawn on"
    assert turn_src["label"] == "conversation history"
    assert vector_src["items"] == ["docs/quest-creation-guide.md", "README.md"], "paths are fine"


def test_context_event_quotes_nothing_and_is_tagged_internal():
    """End to end: whatever a turn retrieves, the context event carries counts, not content."""
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "have it"},
    ])
    sink = _RecordingSink()
    orch = _orch(provider, StubRetrieval({"README.md": "pricing is $9/mo."}))

    orch.run("what did we discuss", sink=sink)

    for event in orch_context_events(sink):
        assert event.data.get("internal") is True, "consumers need this to route it away from the bubble"
        blob = (event.text or "") + str(event.data)
        assert "User:" not in blob, "the context event must never quote a conversation turn"


def orch_context_events(sink: _RecordingSink) -> List[ProgressEvent]:
    return sink.of_type(EVENT_CONTEXT)


def test_greeting_emits_no_understanding_event():
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "greeting"},
    ], answer_text="Hi. What would you like to work on?")
    sink = _RecordingSink()
    orch = _orch(provider, StubRetrieval())

    orch.run("Hello", sink=sink)

    assert sink.of_type(EVENT_UNDERSTANDING) == [], \
        "a bare greeting must never produce an 'Understood as: ...' echo"
