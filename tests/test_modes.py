"""The TWO PRODUCT MODES + the ProgressSink discipline (the frozen streaming interface).

These tests pin the contract quest-backend and the cockpit import:

  * LIVE mode streams EVERY event type through a StreamSink.
  * BACKGROUND mode (MilestoneSink) surfaces ONLY result / decision / milestone / done —
    planning / reading / re-planning / status chatter is dropped.
  * a deep/confirm still escalates correctly in BOTH modes.
  * the LIVE→BACKGROUND handoff: detach mid-run -> the result is delivered via the background
    sink, NOT the live one.
  * run_stream yields events then the terminal OrchestratorResult.
"""
from quest_ai_runner.core.adapters import (
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_MILESTONE,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    SURFACING_EVENTS,
    Mode,
    MilestoneSink,
    ProgressEvent,
    StreamSink,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig, OrchestratorResult

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


class CollectSink:
    """A ProgressSink that records (event_dict, mode) for every update it receives."""

    def __init__(self):
        self.events = []

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        self.events.append((event.to_dict(), mode))

    def types(self):
        return [e["type"] for e, _ in self.events]


# --- LIVE: streams every event type ---------------------------------------------

def test_live_mode_streams_all_event_types_through_streamsink():
    # plan -> read -> re-plan -> answer: exercises plan, read, replan, status, result, done.
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "need it"},
        {"action": "answer", "rationale": "have it"},
    ])
    retrieval = StubRetrieval({"README.md": "GROUNDING fact"})

    forwarded = []
    sink = StreamSink(lambda ev: forwarded.append(ev))
    res = _orch(provider, retrieval).run("q?", mode=Mode.LIVE, sink=sink)

    assert res.kind == "answer"
    types = [ev["type"] for ev in forwarded]
    # LIVE forwards the full chatter: status ticks, the plan/replan, the read, result, done.
    for expected in (EVENT_STATUS, EVENT_PLAN, EVENT_READ, EVENT_REPLAN, EVENT_RESULT, EVENT_DONE):
        assert expected in types, f"LIVE stream missing {expected}: {types}"
    # The result event carries the reply text.
    result_ev = next(ev for ev in forwarded if ev["type"] == EVENT_RESULT)
    assert result_ev.get("text") == res.text


# --- BACKGROUND: MilestoneSink drops chatter, surfaces only result/decision/milestone/done ---

def test_background_mode_milestonesink_drops_planning_and_reading():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "need it"},
        {"action": "answer", "rationale": "have it"},
    ])
    retrieval = StubRetrieval({"README.md": "GROUNDING fact"})

    surfaced = []
    sink = MilestoneSink(
        on_result=lambda ev: surfaced.append(ev),
        on_done=lambda ev: surfaced.append(ev),
        on_milestone=lambda ev: surfaced.append(ev),
        on_decision=lambda ev: surfaced.append(ev),
    )
    res = _orch(provider, retrieval).run("q?", mode=Mode.BACKGROUND, sink=sink)

    assert res.kind == "answer"
    surfaced_types = [ev.type for ev in surfaced]
    # ONLY result + done surface — no plan/read/replan/status leaked through.
    assert EVENT_RESULT in surfaced_types
    assert EVENT_DONE in surfaced_types
    assert EVENT_PLAN not in surfaced_types
    assert EVENT_READ not in surfaced_types
    assert EVENT_REPLAN not in surfaced_types
    assert EVENT_STATUS not in surfaced_types
    # Every surfaced event is in the canonical surfacing set (policy enforced once, in the sink).
    assert all(t in SURFACING_EVENTS for t in surfaced_types)


def test_background_milestonesink_uses_collectsink_to_prove_drop():
    """A sink that records EVERYTHING it is asked to surface — proves the orchestrator emits
    chatter but the MilestoneSink policy is what drops it (orchestrator emits same in both modes)."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"grep": "x"}], "rationale": "locate"},
        {"action": "answer", "rationale": "done"},
    ])
    retrieval = StubRetrieval({"a.md": "x marks"})

    # Compare: a CollectSink in LIVE sees chatter; a MilestoneSink in BACKGROUND must not surface it.
    live = CollectSink()
    _orch(provider, retrieval).run("q", mode=Mode.LIVE, sink=live)
    assert EVENT_PLAN in live.types() and EVENT_READ in live.types()

    provider2 = StubProvider(decisions=[
        {"action": "read", "reads": [{"grep": "x"}], "rationale": "locate"},
        {"action": "answer", "rationale": "done"},
    ])
    surfaced = []
    ms = MilestoneSink(on_result=lambda ev: surfaced.append(ev.type),
                       on_done=lambda ev: surfaced.append(ev.type))
    _orch(provider2, StubRetrieval({"a.md": "x marks"})).run("q", mode=Mode.BACKGROUND, sink=ms)
    assert EVENT_PLAN not in surfaced and EVENT_READ not in surfaced
    assert EVENT_RESULT in surfaced


# --- confirm/deep escalate correctly in BOTH modes ------------------------------

def test_confirm_escalates_in_live_mode():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Approve $50?", "rationale": "money"}])
    sink_events = []
    sink = StreamSink(lambda ev: sink_events.append(ev))
    res = _orch(provider, StubRetrieval(), escalation=StubEscalation("dec_live")).run(
        "buy", mode=Mode.LIVE, sink=sink, quest_id="q1")
    assert res.kind == "confirm" and res.decision_id == "dec_live"
    decision_evs = [ev for ev in sink_events if ev["type"] == EVENT_DECISION]
    assert decision_evs and decision_evs[0]["decision_id"] == "dec_live"


def test_confirm_escalates_in_background_mode():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Approve $50?", "rationale": "money"}])
    surfaced = []
    sink = MilestoneSink(on_decision=lambda ev: surfaced.append(ev))
    res = _orch(provider, StubRetrieval(), escalation=StubEscalation("dec_bg")).run(
        "buy", mode=Mode.BACKGROUND, sink=sink, quest_id="q1")
    assert res.kind == "confirm" and res.decision_id == "dec_bg"
    assert surfaced and surfaced[0].type == EVENT_DECISION
    assert surfaced[0].decision_id == "dec_bg"


def test_deep_emits_milestone_in_background_mode():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Write the doc", "deep_brief": "do it", "rationale": "work"},
        {"met": True, "reason": "written"},  # goal verification
    ])
    surfaced = []
    sink = MilestoneSink(on_milestone=lambda ev: surfaced.append(("milestone", ev.text)),
                         on_result=lambda ev: surfaced.append(("result", ev.text)))
    res = _orch(provider, StubRetrieval(), deep_runner=StubDeepRunner(met=True, output="written")).run(
        "write the doc", mode=Mode.BACKGROUND, sink=sink)
    assert res.kind == "deep" and res.deep_results[0].met is True
    kinds = [k for k, _ in surfaced]
    assert "milestone" in kinds   # the completed goal is a real milestone
    assert "result" in kinds      # plus the final result


# --- LIVE -> BACKGROUND handoff -------------------------------------------------

def test_live_to_background_handoff_delivers_result_via_background_sink():
    """If the consumer disconnects mid-run, the run continues and the result is delivered via the
    BACKGROUND (MilestoneSink) path, NOT the dropped live stream."""
    # Two-step plan so detach triggers after the first step (live stream gets the early chatter,
    # the final result must land on the background sink).
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "need it"},
        {"action": "answer", "rationale": "answer now"},
    ])
    retrieval = StubRetrieval({"README.md": "fact"})

    live_events = []
    live = StreamSink(lambda ev: live_events.append(ev))
    bg_surfaced = []
    bg = MilestoneSink(on_result=lambda ev: bg_surfaced.append(ev),
                       on_done=lambda ev: bg_surfaced.append(ev))

    # Detach as soon as the first plan/status fires: detach_check returns True after 1 call.
    calls = {"n": 0}

    def detach_check():
        calls["n"] += 1
        return calls["n"] >= 2   # let the very first emission go live, then detach

    res = _orch(provider, retrieval).run(
        "q?", mode=Mode.LIVE, sink=live, background_sink=bg, detach_check=detach_check)

    assert res.kind == "answer"
    # The final result was NOT delivered to the live stream...
    assert all(ev["type"] != EVENT_RESULT for ev in live_events), \
        "result leaked to the dropped live stream"
    # ...it was delivered via the background MilestoneSink.
    assert any(ev.type == EVENT_RESULT for ev in bg_surfaced), \
        "result not delivered via the background sink after detach"
    assert any(ev.type == EVENT_DONE for ev in bg_surfaced)


def test_no_detach_keeps_everything_on_live_sink():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    live_events = []
    live = StreamSink(lambda ev: live_events.append(ev))
    bg_surfaced = []
    bg = MilestoneSink(on_result=lambda ev: bg_surfaced.append(ev))
    res = _orch(provider, StubRetrieval()).run(
        "hi", mode=Mode.LIVE, sink=live, background_sink=bg, detach_check=lambda: False)
    assert res.kind == "answer"
    assert any(ev["type"] == EVENT_RESULT for ev in live_events)   # stayed live
    assert bg_surfaced == []                                       # bg never used


# --- run_stream generator -------------------------------------------------------

def test_run_stream_yields_events_then_result():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "need it"},
        {"action": "answer", "rationale": "have it"},
    ])
    retrieval = StubRetrieval({"README.md": "GROUNDING fact"})

    items = list(_orch(provider, retrieval).run_stream("q?", mode=Mode.LIVE))
    # Last item is the terminal result; everything before is an event dict.
    final = items[-1]
    assert isinstance(final, OrchestratorResult)
    assert final.kind == "answer"
    events = items[:-1]
    assert all(isinstance(e, dict) for e in events)
    types = [e["type"] for e in events]
    assert EVENT_RESULT in types and EVENT_DONE in types


# --- deep-run EXECUTION-lifecycle streaming (emit) ------------------------------

from quest_ai_runner.core.adapters import EVENT_EXEC, DeepResult  # noqa: E402


class EmitDeepRunner:
    """A DeepRunner whose run_goal accepts ``emit`` and reports an EXECUTION tick through it —
    proving the orchestrator threads the live emitter to opt-in runners, and the sink's mode
    policy then decides whether that texture surfaces."""

    def __init__(self):
        self.got_emit = False

    def run_goal(self, *, goal, brief, model=None, max_turns=None, emit=None) -> DeepResult:
        self.got_emit = emit is not None
        if emit is not None:
            emit(ProgressEvent(type=EVENT_EXEC, text="executing",
                               data={"phase": "executing", "attempt": 1}))
        return DeepResult(met=True, output="ran")


def test_live_mode_streams_exec_events_to_opt_in_runner():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do it", "deep_brief": "do", "rationale": "work"}])
    runner = EmitDeepRunner()
    forwarded = []
    sink = StreamSink(lambda ev: forwarded.append(ev))
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "do it", mode=Mode.LIVE, sink=sink)
    assert res.kind == "deep" and runner.got_emit is True
    exec_evs = [ev for ev in forwarded if ev["type"] == EVENT_EXEC]
    assert exec_evs, f"LIVE stream missing exec event: {[e['type'] for e in forwarded]}"
    assert exec_evs[0]["data"]["phase"] == "executing"


def test_background_mode_drops_exec_texture():
    """EVENT_EXEC is intermediate texture — a MilestoneSink (BACKGROUND) must drop it, like
    plan/read/status chatter. The runner still gets the emitter; only surfacing differs."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do it", "deep_brief": "do", "rationale": "work"}])
    runner = EmitDeepRunner()
    surfaced = []
    sink = MilestoneSink(on_result=lambda ev: surfaced.append(ev.type),
                         on_milestone=lambda ev: surfaced.append(ev.type),
                         on_done=lambda ev: surfaced.append(ev.type))
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "do it", mode=Mode.BACKGROUND, sink=sink)
    assert res.kind == "deep" and runner.got_emit is True
    assert EVENT_EXEC not in surfaced
    assert EVENT_RESULT in surfaced


def test_runner_without_emit_param_is_not_passed_emit():
    """A legacy run_goal(*, goal, brief, model, max_turns) must keep working — the orchestrator
    inspects the signature and does NOT pass ``emit`` to it (no double-call, no TypeError)."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do it", "deep_brief": "do", "rationale": "work"},
        {"met": True, "reason": "ok"},  # goal verification
    ])
    legacy = StubDeepRunner(met=True, output="ok")
    sink = StreamSink(lambda ev: None)
    res = _orch(provider, StubRetrieval(), deep_runner=legacy).run(
        "do it", mode=Mode.LIVE, sink=sink)
    assert res.kind == "deep" and res.deep_results[0].met is True
    assert len(legacy.calls) == 1                 # called exactly once
    assert "emit" not in legacy.calls[0]          # emit never forced onto a legacy signature


# --- back-compat: run with no event args is unchanged ---------------------------

def test_run_without_event_args_still_works():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    res = _orch(provider, StubRetrieval()).run("hi")
    assert res.kind == "answer"
    assert res.text is not None
