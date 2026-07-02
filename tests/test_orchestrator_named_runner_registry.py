"""Named deep-runner registry (``deep_runners`` + ``deep_runner_classifier``) with NO single
default ``deep_runner`` wired.

Regression coverage for a real bug: several gates across the orchestrator tested
``self.deep_runner is not None`` as "can this turn actually execute deep work". That was correct
only for the single-runner wiring style. A consumer that instead wires ``deep_runner=None`` plus a
named registry (``deep_runners={"code": ..., "text": ..., "delegate": ...}`` selected per-goal by
``deep_runner_classifier``) has real execution capability, but every one of those gates silently
treated it as "no runner configured" -- so an action ("add a goal", "fix this bug") that the
planner correctly routed to "deep" (or that a safety net tried to defer to "deep" after an
answer merely DESCRIBED the action) never actually ran. The user saw a plausible-sounding reply
with no code, no execution, and no real effect on their data.

These tests wire ONLY the named registry (exactly as a multi-runner consumer does) and assert the
work is actually carried out through it.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult, EVENT_EXEC, ProgressEvent, StreamSink
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator

from .conftest import StubProvider, StubRetrieval
from .test_guard import GuardProvider


class _NamedRunner:
    """A DeepRunner registered under a name in ``deep_runners`` — accepts ``emit`` so exec-event
    forwarding can be asserted, mirroring a real command-execution runner."""

    def __init__(self, met: bool = True, output: str = "done"):
        self._met = met
        self._output = output
        self.calls: List[Dict[str, Any]] = []
        self.emitted: List[ProgressEvent] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None,
                 emit=None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "model": model})
        if emit is not None:
            emit(ProgressEvent(type=EVENT_EXEC, data={"phase": "code", "code": "create_goal(...)"}))
            emit(ProgressEvent(type=EVENT_EXEC, data={"phase": "done"}))
        return DeepResult(met=self._met, output=self._output)


def _classifier_always(key: str):
    def _fn(user_message: str, goal: str, brief: str) -> str:
        return key
    return _fn


def _orch_named_registry(provider, retrieval, runner, *, key="code", **kw):
    """An Orchestrator wired EXACTLY as a multi-runner consumer does: no single ``deep_runner``,
    only the named registry + classifier."""
    return Orchestrator(
        retrieval=retrieval, provider=provider, registry=ModelRegistry(provider),
        deep_runner=None,
        deep_runners={key: runner},
        deep_runner_classifier=_classifier_always(key),
        **kw,
    )


def test_deep_action_actually_executes_via_named_registry():
    # The core regression: planner correctly routes to "deep", but with no single deep_runner
    # wired the turn used to bail out immediately and report NOTHING executed.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Add the goal", "deep_brief": "add a goal to run a 5k",
         "rationale": "in-app data operation"},
    ])
    runner = _NamedRunner(met=True, output="Added the goal 'Run a 5k' to your quest.")
    res = _orch_named_registry(provider, StubRetrieval(), runner).run("add a goal to run a 5k")

    assert res.kind == "deep"
    assert runner.calls, "the named runner must actually be invoked, not silently skipped"
    assert res.deep_results and res.deep_results[0].met is True
    assert "Run a 5k" in res.deep_results[0].output


def test_exec_events_stream_through_named_registry_runner():
    # The other half of the same bug: even when the runner DID execute, exec events (the
    # generated-code / raw-output ticks the UI shows) were dropped because the capability check
    # was computed against ``self.deep_runner`` (None), not the actually-selected runner.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Add the goal", "deep_brief": "add it", "rationale": "op"},
    ])
    runner = _NamedRunner(met=True)
    captured: List[Dict[str, Any]] = []
    sink = StreamSink(forward=captured.append)

    res = _orch_named_registry(provider, StubRetrieval(), runner).run(
        "add a goal", sink=sink)
    assert res.kind == "deep"
    exec_phases = [e.get("data", {}).get("phase") for e in captured if e.get("type") == EVENT_EXEC]
    assert "code" in exec_phases and "done" in exec_phases


def test_unknown_classifier_key_falls_back_to_default_runner():
    # Belt-and-braces: if the classifier returns a key that isn't registered, fall back to the
    # single default deep_runner (when one happens to also be wired) instead of silently no-op'ing.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do it", "deep_brief": "do it", "rationale": "op"},
    ])
    named_runner = _NamedRunner(met=True, output="named ran")
    default_runner = _NamedRunner(met=True, output="default ran")
    res = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        deep_runner=default_runner,
        deep_runners={"code": named_runner},
        deep_runner_classifier=_classifier_always("unknown_key"),
    ).run("do it")
    assert res.kind == "deep"
    assert default_runner.calls and not named_runner.calls
    assert res.deep_results[0].output == "default ran"


def test_broken_promise_guard_remediates_via_named_registry():
    # The exact reported bug: the planner ANSWERS, hallucinating a completed action ("I've added a
    # measurable outcome") for a request that should have been a real in-app data operation ("create
    # a goal") -- nothing actually ran. With only the named registry wired (no single deep_runner,
    # the real Quest AI wiring), the broken-promise guard's remediation used to be silently disabled
    # (its "self.deep_runner is not None" gate), so the false claim could only be rewritten to an
    # honest non-answer -- it could never actually CARRY OUT the action. Fixed, the guard detects the
    # unsupported claim and remediates by actually running the goal through the named registry.
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "describe"}],
        verify_verdicts=[{"verdict": "unsupported"}],
        answer_text="I've added a measurable outcome for your quest.",
    )
    runner = _NamedRunner(met=True, output="Added the goal 'Run a 5k' to your quest.")
    res = _orch_named_registry(provider, StubRetrieval(), runner).run(
        "create a goal to run a 5k this week")
    assert res.kind == "answer"
    assert runner.calls, "expected the guard's remediation to actually execute via the named registry"
    # The remediation run succeeded and re-verified as supported -> the reply is kept, not rewritten.
    assert res.claim_corrected is False
