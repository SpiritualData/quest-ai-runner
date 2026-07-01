"""Minimal-intervention OVERSEER: digest builder, the four signals, and the wiring in run().

Offline, no network. Follows tests/test_orchestrator.py's StubProvider/StubRetrieval pattern; the
stub subclasses that split planner vs overseer calls are defined LOCALLY here (conftest.py is not
touched). The overseer resolves its own model and (via a single-provider ModelRegistry) shares the
provider with the planner, so a local StubProvider subclass routes plan() calls to a separate
overseer queue by detecting the overseer prompt.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import (
    EVENT_OVERSEER,
    EVENT_PLAN,
    Mode,
    MilestoneSink,
    ProgressEvent,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.overseer import (
    OVERSEER_PROMPT,
    OverseerSignal,
    build_digest,
    oversee,
)

from .conftest import StubDeepRunner, StubProvider, StubRetrieval


# The marker that identifies an overseer plan() call (the prompt is the OVERSEER_PROMPT).
_OVERSEER_MARK = "OVERSEER"


class OverseerStubProvider(StubProvider):
    """StubProvider that routes plan() calls: overseer prompts draw from ``overseer_signals``,
    everything else from the normal ``decisions`` queue. Tracks how many overseer calls happened."""

    def __init__(self, decisions: List[Dict[str, Any]], *,
                 overseer_signals: Optional[List[Dict[str, Any]]] = None,
                 answer_text: str = "STUB ANSWER"):
        super().__init__(decisions, answer_text=answer_text)
        self._overseer_signals = list(overseer_signals or [])
        self.overseer_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if _OVERSEER_MARK in prompt and "minimal-intervention" in prompt.lower():
            self.overseer_calls += 1
            if self._overseer_signals:
                return self._overseer_signals.pop(0)
            return {"signal": "proceed"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


class RaisingOverseerProvider(StubProvider):
    """Like OverseerStubProvider but the overseer plan() call RAISES, to prove it degrades to
    proceed and the run still completes."""

    def __init__(self, decisions: List[Dict[str, Any]], answer_text: str = "STUB ANSWER"):
        super().__init__(decisions, answer_text=answer_text)
        self.overseer_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if _OVERSEER_MARK in prompt and "minimal-intervention" in prompt.lower():
            self.overseer_calls += 1
            raise RuntimeError("overseer provider blew up")
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# (a) overseer=False -> zero overseer calls and identical event stream to baseline.
# ---------------------------------------------------------------------------

def _events(sink_events: List[ProgressEvent]) -> List[str]:
    return [e.type for e in sink_events]


class _CaptureSink:
    def __init__(self):
        self.events: List[ProgressEvent] = []

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        self.events.append(event)


def test_overseer_off_is_zero_calls_and_identical_stream():
    decisions = [{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}]

    base_provider = OverseerStubProvider(list(decisions))
    base_sink = _CaptureSink()
    _orch(base_provider, StubRetrieval(),
          config=OrchestratorConfig(overseer=False, answer_goal_max_iterations=1)).run(
        "explain X", sink=base_sink)

    off_provider = OverseerStubProvider(list(decisions), overseer_signals=[{"signal": "proceed"}])
    off_sink = _CaptureSink()
    _orch(off_provider, StubRetrieval(),
          config=OrchestratorConfig(overseer=False, answer_goal_max_iterations=1)).run(
        "explain X", sink=off_sink)

    assert off_provider.overseer_calls == 0
    assert base_provider.overseer_calls == 0
    # Identical event streams (no EVENT_OVERSEER anywhere).
    assert _events(off_sink.events) == _events(base_sink.events)
    assert EVENT_OVERSEER not in _events(off_sink.events)


# ---------------------------------------------------------------------------
# (b) proceed is a no-op but still emits EVENT_OVERSEER with signal proceed.
# ---------------------------------------------------------------------------

def test_proceed_is_noop_but_emits_overseer_event():
    provider = OverseerStubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}],
        overseer_signals=[{"signal": "proceed", "reason": "on track"}],
    )
    sink = _CaptureSink()
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(overseer=True, answer_goal_max_iterations=1)).run(
        "explain X", sink=sink)
    assert res.kind == "answer"
    # An answer on step 1 consults the overseer at both hook points (in-loop + answer checkpoint);
    # both proceed, so both emit an EVENT_OVERSEER and neither changes the outcome.
    assert provider.overseer_calls >= 1
    ov = [e for e in sink.events if e.type == EVENT_OVERSEER]
    assert len(ov) == provider.overseer_calls
    assert all(e.data.get("signal") == "proceed" for e in ov)
    assert res.overseer_signals and all(s["signal"] == "proceed" for s in res.overseer_signals)


# ---------------------------------------------------------------------------
# (c) redirect injects "COURSE CORRECTION: <hint>" into the NEXT planner prompt.
# ---------------------------------------------------------------------------

def test_redirect_injects_course_correction_into_next_planner_prompt():
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read step 1"},
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer step 2"},
        ],
        overseer_signals=[{"signal": "redirect", "hint": "focus on the config file, not the readme"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    res = _orch(provider, retrieval,
                config=OrchestratorConfig(overseer=True, answer_goal_max_iterations=1)).run(
        "explain X", sink=_CaptureSink())
    assert res.kind == "answer"
    # The second planner prompt (step 2) must carry the course correction observation.
    planner_prompts = [p for p in provider.plan_prompts if _OVERSEER_MARK not in p]
    assert len(planner_prompts) >= 2
    assert "COURSE CORRECTION: focus on the config file, not the readme" in planner_prompts[1]


# ---------------------------------------------------------------------------
# (d) answer_now short-circuits a read-looping planner -> exit_reason overseer_answer_now.
# ---------------------------------------------------------------------------

def test_answer_now_short_circuits_read_loop():
    # Planner would keep reading forever; the overseer says answer_now on step 1.
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"}
            for _ in range(10)
        ],
        overseer_signals=[{"signal": "answer_now", "reason": "enough gathered"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    res = _orch(provider, retrieval,
                config=OrchestratorConfig(overseer=True, max_steps=10,
                                          answer_goal_max_iterations=1)).run("q", sink=_CaptureSink())
    assert res.kind == "answer"
    assert res.exit_reason == "overseer_answer_now"
    assert res.steps == 1  # short-circuited on the first plan step
    # Hook A said answer_now (step 1); the answer checkpoint (Hook B) then consults once more.
    assert provider.overseer_calls >= 1
    assert res.overseer_signals[0]["signal"] == "answer_now"


# ---------------------------------------------------------------------------
# (e) escalate forces deep with a stub deep runner -> exit_reason overseer_escalated.
# ---------------------------------------------------------------------------

def test_escalate_forces_deep():
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"}
            for _ in range(10)
        ],
        overseer_signals=[{"signal": "escalate", "reason": "needs real execution"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    runner = StubDeepRunner(met=True, output="did the deep work")
    res = _orch(provider, retrieval, deep_runner=runner,
                config=OrchestratorConfig(overseer=True, max_steps=10)).run("do it",
                                                                            sink=_CaptureSink())
    assert res.kind == "deep"
    assert res.exit_reason == "overseer_escalated"
    assert len(res.deep_results) == 1 and res.deep_results[0].met is True


# ---------------------------------------------------------------------------
# (f) an overseer call that raises degrades to proceed and the run completes.
# ---------------------------------------------------------------------------

def test_overseer_raise_degrades_to_proceed():
    provider = RaisingOverseerProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}],
    )
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(overseer=True, answer_goal_max_iterations=1)).run(
        "explain X", sink=_CaptureSink())
    assert res.kind == "answer"  # run still completed
    assert provider.overseer_calls >= 1  # the overseer was consulted (and blew up)


# ---------------------------------------------------------------------------
# (g) overseer_max_signals=1 -> exactly one overseer call across a multi-step run.
# ---------------------------------------------------------------------------

def test_max_signals_caps_consultations():
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"},
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"},
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer"},
        ],
        overseer_signals=[{"signal": "proceed"}, {"signal": "proceed"}, {"signal": "proceed"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    res = _orch(provider, retrieval,
                config=OrchestratorConfig(overseer=True, overseer_max_signals=1, max_steps=5,
                                          answer_goal_max_iterations=1)).run("q", sink=_CaptureSink())
    assert res.kind == "answer"
    assert provider.overseer_calls == 1
    assert res.overseer_signals is not None and len(res.overseer_signals) == 1


# ---------------------------------------------------------------------------
# (h) build_digest respects the char budget, includes pass + token counts, excludes full bodies.
# ---------------------------------------------------------------------------

def test_build_digest_budget_and_contents():
    big_body = "SECRET_FULL_BODY " * 500  # a full observation body that must NOT appear verbatim
    summaries = ["READ config.py [head]: brief one-line summary"]
    digest = build_digest(
        question="What does the config do?",
        step=2,
        max_steps=6,
        plan_action="read",
        plan_rationale="looking at config",
        plan_goal="Understand config",
        observation_summaries=summaries,
        tokens_in=1234,
        tokens_out=567,
        elapsed_seconds=3.0,
        max_elapsed_seconds=60.0,
        gathered_chars=9000,
        max_gathered_chars=40000,
        consecutive_reads=2,
        char_budget=400,
    )
    assert len(digest) <= 400
    assert "PASS: 2 of 6" in digest
    assert "1234" in digest and "567" in digest  # token counts present
    assert big_body.strip() not in digest  # full observation body excluded


def test_oversee_unknown_or_error_returns_proceed():
    class _Bad:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "nonsense"}
    assert oversee(_Bad(), "m", "digest").signal == "proceed"

    class _Boom:
        def plan(self, prompt, *, model, tool_schema):
            raise RuntimeError("boom")
    assert oversee(_Boom(), "m", "digest").signal == "proceed"

    class _Redirect:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "redirect", "hint": "do X instead", "reason": "off subject"}
    sig = oversee(_Redirect(), "m", "digest")
    assert sig.signal == "redirect" and sig.hint == "do X instead"

    # A non-redirect signal must never carry a hint.
    class _AnswerNowWithHint:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "answer_now", "hint": "should be dropped"}
    sig2 = oversee(_AnswerNowWithHint(), "m", "digest")
    assert sig2.signal == "answer_now" and sig2.hint == ""


def test_overseer_prompt_forbids_em_dashes():
    # The authored prompt must not itself contain an em dash and must instruct against them.
    assert "—" not in OVERSEER_PROMPT
    assert "em dash" in OVERSEER_PROMPT.lower()


# ---------------------------------------------------------------------------
# (i) EVENT_OVERSEER passes through a MilestoneSink (BACKGROUND); EVENT_PLAN does not.
# ---------------------------------------------------------------------------

def test_overseer_event_surfaces_in_milestone_sink_but_plan_does_not():
    seen_overseer: List[ProgressEvent] = []
    sink = MilestoneSink(on_overseer=lambda e: seen_overseer.append(e))
    # EVENT_PLAN is chatter — dropped by a MilestoneSink.
    sink.update(ProgressEvent(type=EVENT_PLAN, action="read", step=1), Mode.BACKGROUND)
    assert seen_overseer == []
    # EVENT_OVERSEER always surfaces — forwarded to on_overseer.
    sink.update(ProgressEvent(type=EVENT_OVERSEER, step=1,
                              data={"signal": "redirect", "hint": "h"}), Mode.BACKGROUND)
    assert len(seen_overseer) == 1
    assert seen_overseer[0].data.get("signal") == "redirect"
