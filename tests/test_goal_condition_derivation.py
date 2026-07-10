"""Fix 13: goal-condition ESTABLISHMENT is a separate concern from context-FETCHING, and now always
happens for every input, not just ones that needed conversation context.

`Orchestrator._derive_goal_condition` is the new helper: for a SELF-CONTAINED input (the one that
skips Step 1's context-fetch path, see `test_conversation_understanding.py`), it makes ONE cheap-
tier LLM call to restate the message as a concrete, checkable done-standard. This file tests that
helper directly (success + safe-fallback-on-error), plus which model TIER it uses.

Offline, no network.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator

from .conftest import StubProvider, StubRetrieval


def _orch(provider, **kw):
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), **kw)


class _RecordingAnswerProvider(StubProvider):
    """Records every model id + prompt passed to answer(), and replays scripted replies."""

    def __init__(self, replies: List[str]):
        super().__init__(decisions=[])
        self._replies = list(replies)
        self.answer_models: List[str] = []
        self.answer_prompts: List[str] = []

    def answer(self, messages, *, model, system=None) -> str:
        self.answer_models.append(model)
        prompt = "\n".join(m["content"] for m in messages)
        self.answer_prompts.append(prompt)
        if self._replies:
            return self._replies.pop(0)
        return super().answer(messages, model=model, system=system)


class _RaisingAnswerProvider(StubProvider):
    """answer() always raises, to prove _derive_goal_condition degrades safely."""

    def __init__(self):
        super().__init__(decisions=[])

    def answer(self, messages, *, model, system=None) -> str:
        raise RuntimeError("provider blew up")


def test_derive_goal_condition_restates_a_checkable_done_standard():
    provider = _RecordingAnswerProvider(replies=["Add a --dry-run flag to poll and commit it"])
    orch = _orch(provider)
    out, constraints = orch._derive_goal_condition("do the dry-run thing we talked about")
    assert out == "Add a --dry-run flag to poll and commit it"
    assert out != "do the dry-run thing we talked about"
    assert constraints is None


def test_derive_goal_condition_echoes_an_already_concrete_instruction():
    message = "Fix the pricing calculation bug in the checkout flow"
    provider = _RecordingAnswerProvider(replies=[message])
    orch = _orch(provider)
    out, constraints = orch._derive_goal_condition(message)
    assert out == message
    assert constraints is None


def test_derive_goal_condition_uses_a_cheap_tier_never_best():
    """This runs on EVERY turn, so it must stay cheap: 'fast', never 'best'/'quality'/'opus'."""
    provider = _RecordingAnswerProvider(replies=["restated"])
    orch = _orch(provider)
    orch._derive_goal_condition("some message")
    registry = ModelRegistry(provider)
    expected_fast = registry.resolve_tier("fast")
    expected_best = registry.resolve_tier("best")
    assert provider.answer_models == [expected_fast]
    assert expected_fast != expected_best


def test_derive_goal_condition_falls_back_to_raw_message_on_provider_error():
    """Fails safe: any exception from the provider degrades to the raw message unchanged, never
    raises, never breaks the run."""
    provider = _RaisingAnswerProvider()
    orch = _orch(provider)
    out, constraints = orch._derive_goal_condition("do the thing")
    assert out == "do the thing"
    assert constraints is None


def test_derive_goal_condition_falls_back_to_raw_message_on_empty_reply():
    provider = _RecordingAnswerProvider(replies=["   "])  # blank/whitespace-only reply
    orch = _orch(provider)
    out, constraints = orch._derive_goal_condition("do the thing")
    assert out == "do the thing"
    assert constraints is None


def test_derive_goal_condition_prompt_needs_no_conversation_history():
    """The derivation prompt is self-contained: it must not reference/require conversation
    context, since this path runs precisely when no context-fetch happened."""
    provider = _RecordingAnswerProvider(replies=["restated"])
    orch = _orch(provider)
    orch._derive_goal_condition("do the thing")
    assert len(provider.answer_prompts) == 1
    prompt = provider.answer_prompts[0]
    assert "do the thing" in prompt
    # No em dashes in the authored prompt (repo-wide copy convention).
    assert "—" not in prompt
