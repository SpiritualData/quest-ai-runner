"""The reply channel carries ONLY the reply: no meta-echo, no self-narration, no sources.

The bug these cover: the answer stage used to call ``provider.answer()`` with no system prompt at
all, so the only instructions the model saw were the grounding and merge blocks, which are phrased
ABOUT the person in the third person ("Answer the user's latest message...", "The user asked: ...").
The model mirrored that voice, and the chat bubble came back carrying internal machinery: an echo of
the request ("Understood as: ..."), a third-person narration of the model's own plan ("The user
expressed interest in ... I will create a habit titled ..."), and a recital of which files and cards
had been retrieved.

The contract now: internal material travels on its own typed events (EVENT_UNDERSTANDING,
EVENT_CONTEXT, EVENT_STATUS, narration EVENT_PARTIAL), and REPLY_VOICE_SYSTEM is passed as the
``system=`` argument of every call that produces text the person reads.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.adapters import EVENT_UNDERSTANDING, ProgressEvent
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    REPLY_VOICE_SYSTEM,
    Orchestrator,
    _grounding_block,
    restates_meaningfully,
)

from .conftest import StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


class _RecordingSink:
    """Captures every ProgressEvent the orchestrator emits, by type."""

    def __init__(self) -> None:
        self.events: List[ProgressEvent] = []

    def update(self, event: ProgressEvent, mode: Any = None) -> None:
        self.events.append(event)

    def of_type(self, event_type: str) -> List[ProgressEvent]:
        return [e for e in self.events if e.type == event_type]


# ---------------------------------------------------------------------------
# The voice contract reaches the model
# ---------------------------------------------------------------------------

def test_grounded_answer_passes_the_reply_voice_system_prompt():
    # The regression itself: before the fix, system was None on every answer call, and the model
    # had nothing telling it that its output IS the message.
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "have it"},
    ])
    res = _orch(provider, StubRetrieval({"README.md": "pricing is $9/mo."})).run("What's the price?")

    assert res.kind == "answer"
    # The cheap goal-condition derivation call is deliberately NOT a reply, so it gets no voice
    # contract. The real answer call does. At least one call carried it, and none of the calls that
    # produce user-visible text went out bare.
    assert REPLY_VOICE_SYSTEM in provider.answer_systems


def test_reply_voice_forbids_every_leak_the_bug_showed():
    # A rule per symptom in the original bubble, so a future prompt edit cannot silently drop one.
    text = REPLY_VOICE_SYSTEM
    assert "Understood as:" in text                  # the meta-echo of the request
    assert 'never call them "the user"' in text.lower()   # third-person narration of the person
    assert "Narration of your reasoning" in text     # "I will now create..." self-narration
    assert "Reviewing N sources" in text             # the RAG retrieval listing
    assert "Sources reviewed" in text                # ditto
    assert "Selected context for" in text            # raw tool/agent state
    assert "Write ONLY the final reply." in text


def test_reply_voice_forbids_em_dashes():
    # Standing brand-voice rule, and the prompt must not itself contain one.
    assert "em dash" in REPLY_VOICE_SYSTEM
    assert "—" not in REPLY_VOICE_SYSTEM


# ---------------------------------------------------------------------------
# The internal blocks are labelled internal
# ---------------------------------------------------------------------------

def test_grounding_block_marks_context_internal_and_forbids_quoting_it():
    block = _grounding_block("some context", [], False)
    assert "INTERNAL" in block
    assert "never name its sources" in block
    # And it no longer opens the door to narrating the plan back.
    assert "no account of your reasoning" in block


# ---------------------------------------------------------------------------
# "Understood as: ..." only fires when it actually adds information
# ---------------------------------------------------------------------------

def test_trivial_restatement_is_not_a_new_understanding():
    # The exact shape from the bug report: "Hello" came back as "Hello." from the cheap tier, the
    # byte-for-byte != guard fired, and the person got their own greeting echoed above the reply.
    assert restates_meaningfully("Hello.", "Hello") is False
    assert restates_meaningfully("hello", "Hello") is False
    assert restates_meaningfully("  Hello!  ", "Hello") is False


def test_real_resolution_is_still_an_understanding():
    assert restates_meaningfully(
        "Create a daily habit named 'Daily Shadow Work Reflection'", "ok do it") is True


def test_no_understanding_event_when_the_goal_condition_only_repunctuates():
    # Drive the real run: a self-contained greeting whose derived goal condition differs only by a
    # period must emit no EVENT_UNDERSTANDING at all.
    class _EchoWithPeriod(StubProvider):
        def answer(self, messages, *, model, system=None) -> str:
            out = super().answer(messages, model=model, system=system)
            joined = "\n".join(
                m["content"] for m in messages if isinstance(m.get("content"), str))
            # The goal-condition derivation prompt is the one that embeds the raw message.
            if "done-standard" in joined or "GOAL_CONDITION" in joined:
                return "Hello."
            return out

    provider = _EchoWithPeriod(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "chit-chat"},
    ])
    sink = _RecordingSink()
    res = _orch(provider, StubRetrieval({})).run("Hello", sink=sink)

    assert res.kind == "answer"
    assert sink.of_type(EVENT_UNDERSTANDING) == []


def test_understanding_event_is_flagged_internal_when_it_does_fire():
    # It still fires when the resolution is real, but it is tagged so a consumer knows it belongs in
    # a debug/details surface, never in the reply bubble.
    class _Resolver(StubProvider):
        def answer(self, messages, *, model, system=None) -> str:
            out = super().answer(messages, model=model, system=system)
            joined = "\n".join(
                m["content"] for m in messages if isinstance(m.get("content"), str))
            if "done-standard" in joined or "GOAL_CONDITION" in joined:
                return "Report the current price of the product"
            return out

    provider = _Resolver(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "have it"},
    ])
    sink = _RecordingSink()
    _orch(provider, StubRetrieval({"README.md": "pricing is $9/mo."})).run("price?", sink=sink)

    understandings = sink.of_type(EVENT_UNDERSTANDING)
    assert understandings, "a real resolution should still surface on the internal channel"
    data: Dict[str, Any] = understandings[0].data or {}
    assert data.get("internal") is True
