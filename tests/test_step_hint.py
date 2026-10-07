"""The optional ``step`` hint (``core.adapters.STEP_*``), mirroring the existing ``reasoning`` hint.

Pinned here:

  * ``Orchestrator._plan``, ``_verify_goal``, and ``_grounded_answer`` each pass the matching
    ``STEP_*`` constant through to ``provider.plan``/``provider.answer`` -- but ONLY to a provider
    that declares a ``step`` keyword (or ``**kwargs``); a provider with the pre-existing, narrower
    signature is called with no ``step`` kwarg at all, so it keeps working unmodified.
  * ``MultiProvider.plan``/``.answer`` forward a given ``step`` to the wrapped provider under the
    same accepts-check, and omit it for a wrapped provider that does not declare it.

Fully offline: every provider here is a fake that records calls and returns scripted values. No
network call and no real LLM call is made anywhere in this file.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.multi_provider import MultiProvider
from quest_ai_runner.core.adapters import STEP_PLAN, STEP_REPLY, STEP_VERIFY
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator

from .conftest import StubProvider, StubRetrieval


class _StepRecordingProvider(StubProvider):
    """A ModelProvider whose ``plan``/``answer`` DECLARE ``step`` and record every value given."""

    def __init__(self, decisions: Optional[List[Dict[str, Any]]] = None,
                 answer_text: str = "STUB ANSWER"):
        super().__init__(decisions=decisions or [], answer_text=answer_text)
        self.plan_steps: List[Optional[str]] = []
        self.answer_steps: List[Optional[str]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             step: Optional[str] = None) -> Dict[str, Any]:
        self.plan_steps.append(step)
        return super().plan(prompt, model=model, tool_schema=tool_schema)

    def answer(self, messages, *, model, system: Optional[str] = None,
               step: Optional[str] = None) -> str:
        self.answer_steps.append(step)
        return super().answer(messages, model=model, system=system)


def _orch(provider, **kw) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# A provider that DECLARES ``step`` receives the right role string.
# ---------------------------------------------------------------------------

def test_plan_passes_step_plan_to_a_provider_that_declares_it():
    provider = _StepRecordingProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    orch = _orch(provider)
    orch._plan("a flight plan for Thursday's launch", "", "", [])
    assert provider.plan_steps == [STEP_PLAN]


def test_verify_goal_passes_step_verify_to_a_provider_that_declares_it():
    provider = _StepRecordingProvider(decisions=[{"met": True, "reason": "done"}])
    orch = _orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "the worker output")
    assert verdict is not None and verdict["met"] is True
    assert error is None
    assert provider.plan_steps == [STEP_VERIFY]


def test_grounded_answer_passes_step_reply_to_a_provider_that_declares_it():
    provider = _StepRecordingProvider()
    orch = _orch(provider)
    model = ModelRegistry(provider).resolve_tier("sonnet")
    out = orch._grounded_answer("what's the launch window?", "", "", [], model, False)
    assert isinstance(out, str) and out
    assert provider.answer_steps == [STEP_REPLY]


# ---------------------------------------------------------------------------
# A provider WITHOUT ``step`` (or ``**kwargs``) is called with no ``step`` kwarg at all.
#
# ``StubProvider.plan``/``.answer`` have the pre-existing, narrow signature (``model``,
# ``tool_schema`` / ``system`` only -- no ``step``, no ``**kwargs``), so Python itself raises
# TypeError if a caller ever forwarded ``step`` to one of these anyway. A plain pass (no
# exception) is the proof the hint was withheld, not just a happy coincidence.
# ---------------------------------------------------------------------------

def test_plan_omits_step_for_a_provider_that_does_not_declare_it():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    orch = _orch(provider)
    orch._plan("a flight plan for Thursday's launch", "", "", [])  # no TypeError
    assert provider.plan_calls == 1


def test_verify_goal_omits_step_for_a_provider_that_does_not_declare_it():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = _orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "the worker output")  # no TypeError
    assert verdict is not None and verdict["met"] is True
    assert error is None


def test_grounded_answer_omits_step_for_a_provider_that_does_not_declare_it():
    provider = StubProvider(decisions=[])
    orch = _orch(provider)
    model = ModelRegistry(provider).resolve_tier("sonnet")
    out = orch._grounded_answer("what's the launch window?", "", "", [], model, False)  # no TypeError
    assert isinstance(out, str) and out
    assert provider.answer_calls == 1


# ---------------------------------------------------------------------------
# MultiProvider forwards ``step`` to the wrapped provider under the same accepts-check.
# ---------------------------------------------------------------------------

class _WrappedWithStep:
    """A minimal ModelProvider that DECLARES ``step`` and records it."""

    def __init__(self):
        self.plan_steps: List[Optional[str]] = []
        self.answer_steps: List[Optional[str]] = []
        self.tokens_in = 0
        self.tokens_out = 0

    def plan(self, prompt, *, model, tool_schema, layers=None, step=None):
        self.plan_steps.append(step)
        return {"action": "answer", "model": model}

    def answer(self, messages, *, model, system=None, layers=None, step=None):
        self.answer_steps.append(step)
        return f"answered by {model}"

    def list_models(self):
        return ["gemini-3.1-flash-lite"]


class _WrappedWithoutStep:
    """A minimal ModelProvider with the pre-existing, narrower signature -- no ``step``, no
    ``**kwargs`` -- so it raises TypeError if ``step`` is ever forwarded to it."""

    def __init__(self):
        self.plan_calls = 0
        self.answer_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def plan(self, prompt, *, model, tool_schema, layers=None):
        self.plan_calls += 1
        return {"action": "answer", "model": model}

    def answer(self, messages, *, model, system=None, layers=None):
        self.answer_calls += 1
        return f"answered by {model}"

    def list_models(self):
        return ["gemini-3.1-flash-lite"]


def test_multi_provider_forwards_step_to_a_wrapped_plan_that_declares_it():
    wrapped = _WrappedWithStep()
    mp = MultiProvider(wrapped)
    result = mp.plan("do the thing", model="gemini-3.1-flash-lite",
                     tool_schema={"name": "decide"}, step=STEP_PLAN)
    assert result == {"action": "answer", "model": "gemini-3.1-flash-lite"}
    assert wrapped.plan_steps == [STEP_PLAN]


def test_multi_provider_forwards_step_to_a_wrapped_answer_that_declares_it():
    wrapped = _WrappedWithStep()
    mp = MultiProvider(wrapped)
    result = mp.answer([{"role": "user", "content": "hi"}], model="gemini-3.1-flash-lite",
                       step=STEP_REPLY)
    assert result == "answered by gemini-3.1-flash-lite"
    assert wrapped.answer_steps == [STEP_REPLY]


def test_multi_provider_omits_step_for_a_wrapped_plan_that_does_not_declare_it():
    wrapped = _WrappedWithoutStep()
    mp = MultiProvider(wrapped)
    result = mp.plan("do the thing", model="gemini-3.1-flash-lite",
                     tool_schema={"name": "decide"}, step=STEP_PLAN)  # no TypeError
    assert result == {"action": "answer", "model": "gemini-3.1-flash-lite"}
    assert wrapped.plan_calls == 1


def test_multi_provider_omits_step_for_a_wrapped_answer_that_does_not_declare_it():
    wrapped = _WrappedWithoutStep()
    mp = MultiProvider(wrapped)
    result = mp.answer([{"role": "user", "content": "hi"}], model="gemini-3.1-flash-lite",
                       step=STEP_REPLY)  # no TypeError
    assert result == "answered by gemini-3.1-flash-lite"
    assert wrapped.answer_calls == 1


def test_multi_provider_plan_and_answer_work_with_no_step_given_at_all():
    # The default (no caller passes step=...) must stay byte-for-byte the old behavior on either
    # kind of wrapped provider.
    for wrapped in (_WrappedWithStep(), _WrappedWithoutStep()):
        mp = MultiProvider(wrapped)
        mp.plan("do the thing", model="gemini-3.1-flash-lite", tool_schema={"name": "decide"})
        mp.answer([{"role": "user", "content": "hi"}], model="gemini-3.1-flash-lite")
    # No exception for either shape is the assertion.
