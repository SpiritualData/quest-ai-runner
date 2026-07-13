"""Brainstorm execution mode: the consumer-owned no-action latch.

Covers the four contract points:
  * mode signals are OPT-IN (``mode_signals_enabled``, default off): by default the planner
    prompt and tool schema carry no mode_signal material and a stray ``mode_signal`` in the
    LLM response is ignored,
  * ``mode_signal`` parsing (when enabled) is strict and fail-safe (garbage never changes the
    mode),
  * ``execution_mode="brainstorm"`` gates every path that could ACT (planner deep/confirm,
    the deferred-deep escalation nets) while reads/answers stay fully functional,
  * default behavior (``execution_mode="normal"``, no signal) is unchanged.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.adapters import EVENT_MODE_SIGNAL, StreamSink
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    normalize_decision,
)

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# mode_signal parsing (normalize_decision): opt-in, strict values, fail-safe on garbage.
# ---------------------------------------------------------------------------

def test_mode_signal_parses_known_values():
    cfg = OrchestratorConfig(mode_signals_enabled=True)
    for value in ("enter_brainstorm", "exit_brainstorm", " Enter_Brainstorm "):
        d = normalize_decision({"action": "answer", "rationale": "r", "mode_signal": value}, cfg)
        assert d.mode_signal == value.strip().lower()


def test_mode_signal_absent_is_none():
    d = normalize_decision({"action": "answer", "rationale": "r"},
                           OrchestratorConfig(mode_signals_enabled=True))
    assert d.mode_signal is None


def test_mode_signal_garbage_fails_safe_to_none():
    cfg = OrchestratorConfig(mode_signals_enabled=True)
    for garbage in ("brainstorm", "exit", "", "yes", 42, True, ["enter_brainstorm"],
                    {"signal": "enter_brainstorm"}, None):
        d = normalize_decision({"action": "answer", "rationale": "r",
                                "mode_signal": garbage}, cfg)
        assert d.mode_signal is None, f"garbage {garbage!r} must normalize to None"


def test_mode_signal_ignored_when_disabled():
    """Default config (mode_signals_enabled=False): even a VALID mode_signal is ignored."""
    d = normalize_decision({"action": "answer", "rationale": "r",
                            "mode_signal": "enter_brainstorm"}, OrchestratorConfig())
    assert d.mode_signal is None


# ---------------------------------------------------------------------------
# Signal surfacing: event + result field; the orchestrator persists nothing.
# ---------------------------------------------------------------------------

def test_enter_signal_surfaces_on_event_and_result():
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "musing", "mode_signal": "enter_brainstorm"},
    ])
    events: List[Dict[str, Any]] = []
    sink = StreamSink(lambda ev: events.append(ev))
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(mode_signals_enabled=True)).run(
        "hold my calls, thinking out loud", sink=sink)
    assert res.kind == "answer"
    assert res.mode_signal == "enter_brainstorm"
    mode_events = [e for e in events if e["type"] == EVENT_MODE_SIGNAL]
    assert len(mode_events) == 1
    assert mode_events[0]["data"]["signal"] == "enter_brainstorm"


def test_no_signal_leaves_result_field_none():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "plain"}])
    res = _orch(provider, StubRetrieval()).run("hello")
    assert res.mode_signal is None


# ---------------------------------------------------------------------------
# Brainstorm gating: deep/confirm degrade, escalation nets are skipped,
# reads/answers keep working.
# ---------------------------------------------------------------------------

def test_brainstorm_degrades_deep_to_answer():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do the thing", "deep_brief": "do it",
         "rationale": "planner ignored the note"},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("what if we...")
    assert res.kind == "answer"
    assert runner.calls == []                     # nothing executed


def test_brainstorm_degrades_confirm_to_answer():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Proceed with X?", "rationale": "fork"},
    ])
    sink = StubEscalation()
    res = _orch(provider, StubRetrieval(), escalation=sink,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("weighing X vs Y")
    assert res.kind == "answer"
    assert sink.raised == []                      # no decision-request created


def test_brainstorm_skips_message_intent_escalation_net():
    # "fix the login bug" trips _message_requests_change, so in NORMAL mode an answer turn
    # escalates to a deferred deep run. In brainstorm the net must not add the action.
    decisions = [{"action": "answer", "rationale": "talking it through"}]
    runner_normal = StubDeepRunner(met=True)
    _orch(StubProvider(decisions=list(decisions)), StubRetrieval(),
          deep_runner=runner_normal).run("fix the login bug")
    assert runner_normal.calls, "sanity: normal mode escalates this message to deep"

    runner_brainstorm = StubDeepRunner(met=True)
    res = _orch(StubProvider(decisions=list(decisions)), StubRetrieval(),
                deep_runner=runner_brainstorm,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("fix the login bug")
    assert res.kind == "answer"
    assert runner_brainstorm.calls == []          # net skipped: nothing executed


def test_brainstorm_skips_planner_deferred_deep():
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "answering",
         "deferred_deep": {"goal": "Do it after answering"}},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("idea: what about X")
    assert res.kind == "answer"
    assert runner.calls == []


def test_brainstorm_reads_still_work():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "ground first"},
        {"action": "answer", "rationale": "grounded"},
    ])
    retrieval = StubRetrieval({"README.md": "GROUNDING fact: pricing is $9/mo."})
    res = _orch(provider, retrieval,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("thinking about pricing")
    assert res.kind == "answer"
    assert retrieval.read_calls == ["README.md"]  # full context assembly untouched
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "pricing is $9/mo" in joined


def test_brainstorm_planner_prompt_carries_the_mode_note():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider, StubRetrieval(),
          config=OrchestratorConfig(execution_mode="brainstorm")).run("mulling something over")
    assert "BRAINSTORM MODE" in provider.plan_prompts[0]

    provider2 = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider2, StubRetrieval()).run("mulling something over")
    assert "BRAINSTORM MODE" not in provider2.plan_prompts[0]


# ---------------------------------------------------------------------------
# exit_brainstorm releases the latch for the SAME turn the user asked to proceed in.
# ---------------------------------------------------------------------------

def test_exit_signal_releases_latch_same_turn():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Ship the plan we discussed", "deep_brief": "do it",
         "rationale": "user said go", "mode_signal": "exit_brainstorm"},
        {"met": True, "reason": "done"},           # goal verification
    ])
    runner = StubDeepRunner(met=True, output="shipped")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm",
                                          mode_signals_enabled=True)).run("ok, go do it")
    assert res.kind == "deep"                      # acted, despite brainstorm config
    assert res.mode_signal == "exit_brainstorm"
    assert runner.calls and runner.calls[0]["goal"] == "Ship the plan we discussed"


def test_enter_signal_gates_the_same_turn_even_in_normal_mode():
    # The user explicitly enters brainstorm in a NORMAL-mode conversation: the same turn must
    # already refuse to act, even though the consumer flips its stored mode only afterwards.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do something", "deep_brief": "x",
         "rationale": "confused planner", "mode_signal": "enter_brainstorm"},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(mode_signals_enabled=True)).run(
        "let me think out loud, no acting on this")
    assert res.kind == "answer"
    assert res.mode_signal == "enter_brainstorm"
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Mode signals are OPT-IN: with the default config (mode_signals_enabled=False) the planner
# never hears about working modes and a stray signal in a response cannot suppress actions.
# ---------------------------------------------------------------------------

def test_default_config_planner_prompt_has_no_mode_signal_material():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider, StubRetrieval()).run("let's think through the design as we go")
    assert "MODE SIGNAL" not in provider.plan_prompts[0]
    assert "mode_signal" not in provider.plan_prompts[0]
    assert "enter_brainstorm" not in provider.plan_prompts[0]


def test_default_config_tool_schema_has_no_mode_signal_field():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider, StubRetrieval()).run("hello")
    props = provider.plan_tool_schemas[0]["input_schema"]["properties"]
    assert "mode_signal" not in props


def test_enabled_config_planner_prompt_and_schema_carry_mode_signal():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider, StubRetrieval(),
          config=OrchestratorConfig(mode_signals_enabled=True)).run("hello")
    assert "MODE SIGNAL" in provider.plan_prompts[0]
    props = provider.plan_tool_schemas[0]["input_schema"]["properties"]
    assert "mode_signal" in props


def test_default_config_stray_mode_signal_is_ignored():
    """A planner misfire ("let's think through the design as we go" -> enter_brainstorm) on a
    consumer that never opted in must not suppress the turn's actions: the deep run proceeds,
    no EVENT_MODE_SIGNAL is emitted, and the result carries no signal."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Build the design", "deep_brief": "do it",
         "rationale": "misfired planner", "mode_signal": "enter_brainstorm"},
        {"met": True, "reason": "done"},           # goal verification
    ])
    runner = StubDeepRunner(met=True, output="built")
    events: List[Dict[str, Any]] = []
    sink = StreamSink(lambda ev: events.append(ev))
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "let's think through the design as we go", sink=sink)
    assert res.kind == "deep"                      # the action was NOT suppressed
    assert res.mode_signal is None
    assert runner.calls and runner.calls[0]["goal"] == "Build the design"
    assert [e for e in events if e["type"] == EVENT_MODE_SIGNAL] == []


def test_brainstorm_without_signals_gates_but_prompt_stays_signal_free():
    """A consumer may drive execution_mode purely from its own state: the latch still gates,
    but the planner prompt mentions no mode_signal vocabulary anywhere (the brainstorm note's
    exit-signal exception is part of the opt-in)."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do the thing", "deep_brief": "do it", "rationale": "r"},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("what if we...")
    assert res.kind == "answer"
    assert runner.calls == []
    assert "BRAINSTORM MODE" in provider.plan_prompts[0]
    assert "mode_signal" not in provider.plan_prompts[0]


# ---------------------------------------------------------------------------
# Normal mode with no signal: behavior unchanged.
# ---------------------------------------------------------------------------

def test_normal_mode_deep_unchanged():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Write the one-pager", "deep_brief": "do it",
         "rationale": "real work"},
        {"met": True, "reason": "written"},
    ])
    runner = StubDeepRunner(met=True, output="one-pager written")
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("write the one-pager")
    assert res.kind == "deep"
    assert res.mode_signal is None
    assert runner.calls[0]["goal"] == "Write the one-pager"
