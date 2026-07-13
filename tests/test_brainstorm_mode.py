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
import time
from typing import Any, Dict, List

from quest_ai_runner.core.adapters import EVENT_MODE_SIGNAL, StreamSink
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    BRAINSTORM_HELD_WORK_ACK_NOTE,
    BRAINSTORM_NO_ACTION_ACK_NOTE,
    MODE_RELEASE_TOOL,
    Orchestrator,
    OrchestratorConfig,
    normalize_decision,
)

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


class ReleaseJudgeProvider(StubProvider):
    """StubProvider plus a scripted verdict for the BRAINSTORM-RELEASE judge call.

    The judge call is routed by tool name, so it never consumes a scripted planner decision and a
    test can assert exactly how many times it ran (zero on normal turns).
    """

    def __init__(self, decisions, *, release: bool = False,
                 verdict: Any = None, fail: bool = False, delay: float = 0.0, **kw):
        super().__init__(decisions, **kw)
        self._release = release
        self._verdict = verdict          # a raw dict/str verdict, overrides `release`
        self._fail = fail                # raise instead of answering
        self._delay = delay              # sleep before answering (timeout tests)
        self.judge_calls = 0
        self.judge_prompts: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Any:
        if tool_schema.get("name") == MODE_RELEASE_TOOL["name"]:
            self.judge_calls += 1
            self.judge_prompts.append(prompt)
            if self._fail:
                raise RuntimeError("release-judge provider is down")
            if self._delay:
                time.sleep(self._delay)
            if self._verdict is not None:
                return self._verdict
            return {"release_brainstorm": self._release, "reason": "stub verdict"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


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
# THE EXIT AUTHORITY: while the latch is held, only the dedicated release judge can open it.
# The planner (cheap tier) judges a subject-matter imperative ("create a goal called X and add it
# to my plan") as "the user is asking to proceed" and emits exit_brainstorm; honoring that broke
# the latch mid-turn and executed work in a conversation the user had put on hold. The judge
# decides instead, once per latched turn, and its fail-safe direction is HOLD.
# ---------------------------------------------------------------------------

def _brainstorm_cfg(**kw) -> OrchestratorConfig:
    return OrchestratorConfig(execution_mode="brainstorm", mode_signals_enabled=True, **kw)


def test_release_judge_verdict_releases_the_latch_same_turn():
    provider = ReleaseJudgeProvider(decisions=[
        {"action": "deep", "goal": "Ship the plan we discussed", "deep_brief": "do it",
         "rationale": "user said go"},
        {"met": True, "reason": "done"},           # goal verification
    ], release=True)
    runner = StubDeepRunner(met=True, output="shipped")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=_brainstorm_cfg()).run("ok, go ahead and do it now")
    assert provider.judge_calls == 1               # exactly one extra call, on a latched turn
    assert res.kind == "deep"                      # acted, despite the brainstorm config
    assert res.mode_signal == "exit_brainstorm"    # the judge's verdict is what the consumer sees
    assert runner.calls and runner.calls[0]["goal"] == "Ship the plan we discussed"


def test_latched_planner_exit_signal_is_ignored_when_the_judge_holds():
    """THE REGRESSION: a subject-matter imperative makes the cheap planner emit exit_brainstorm.
    While latched, that signal must not release anything: the judge said hold, so the work is
    held and the reply acknowledges it."""
    provider = ReleaseJudgeProvider(decisions=[
        {"action": "deep", "goal": "Create the goal", "deep_brief": "create it",
         "rationale": "planner read the imperative as permission to act",
         "mode_signal": "exit_brainstorm"},
    ], release=False)
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner, config=_brainstorm_cfg()).run(
        "Create a goal on this quest called Morning Pages and add it to my plan")
    assert provider.judge_calls == 1
    assert res.kind == "answer"
    assert runner.calls == []                      # nothing executed
    assert res.mode_signal is None                 # the planner's exit never reaches the consumer
    assert _ACK_MARKER in _answer_prompt(provider)  # and the reply says nothing ran


def test_release_judge_failure_holds_the_latch():
    """Fail-safe direction: a provider failure in the judge leaves the latch ON, even when the
    planner is shouting exit_brainstorm."""
    provider = ReleaseJudgeProvider(decisions=[
        {"action": "deep", "goal": "Do it", "deep_brief": "do it", "rationale": "r",
         "mode_signal": "exit_brainstorm"},
    ], fail=True)
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner, config=_brainstorm_cfg()).run(
        "set that up")
    assert res.kind == "answer"
    assert runner.calls == []
    assert res.mode_signal is None


def test_release_judge_unusable_verdict_holds_the_latch():
    for verdict in ({"reason": "no verdict field"}, {}, "not a dict", None, 42):
        provider = ReleaseJudgeProvider(decisions=[
            {"action": "deep", "goal": "Do it", "deep_brief": "do it", "rationale": "r",
             "mode_signal": "exit_brainstorm"},
        ], verdict=verdict)
        runner = StubDeepRunner(met=True)
        res = _orch(provider, StubRetrieval(), deep_runner=runner, config=_brainstorm_cfg()).run(
            "book it")
        assert res.kind == "answer", f"verdict {verdict!r} must hold the latch"
        assert runner.calls == []
        assert res.mode_signal is None


def test_release_judge_timeout_holds_the_latch(monkeypatch):
    monkeypatch.setenv("QAR_MODE_RELEASE_TIMEOUT_SECONDS", "0.05")
    provider = ReleaseJudgeProvider(decisions=[
        {"action": "deep", "goal": "Do it", "deep_brief": "do it", "rationale": "r",
         "mode_signal": "exit_brainstorm"},
    ], release=True, delay=0.5)                    # would release, but too slow
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner, config=_brainstorm_cfg()).run(
        "add that to my plan")
    assert res.kind == "answer"
    assert runner.calls == []
    assert res.mode_signal is None


def test_release_judge_never_runs_on_normal_turns():
    """Cost is bounded to brainstorm turns: an unlatched turn makes ZERO extra calls, even with
    mode signals enabled."""
    provider = ReleaseJudgeProvider(decisions=[
        {"action": "deep", "goal": "Write the one-pager", "deep_brief": "do it",
         "rationale": "real work"},
        {"met": True, "reason": "done"},
    ], release=True)
    runner = StubDeepRunner(met=True, output="written")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(mode_signals_enabled=True)).run("write the one-pager")
    assert provider.judge_calls == 0
    assert res.kind == "deep"


def test_release_judge_never_runs_when_signals_are_disabled():
    """A consumer that drives execution_mode from its own state (signals off) has no exit channel,
    so the judge does not run and the latch simply holds."""
    provider = ReleaseJudgeProvider(decisions=[
        {"action": "deep", "goal": "Do it", "deep_brief": "do it", "rationale": "r"},
    ], release=True)
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("ok go ahead and do it")
    assert provider.judge_calls == 0
    assert res.kind == "answer"
    assert runner.calls == []


def test_release_verdict_surfaces_as_event_with_reason():
    provider = ReleaseJudgeProvider(decisions=[{"action": "answer", "rationale": "acting now"}],
                                    release=True)
    events: List[Dict[str, Any]] = []
    sink = StreamSink(lambda ev: events.append(ev))
    res = _orch(provider, StubRetrieval(), config=_brainstorm_cfg()).run(
        "we're done brainstorming, act on this", sink=sink)
    assert res.mode_signal == "exit_brainstorm"
    mode_events = [e for e in events if e["type"] == EVENT_MODE_SIGNAL]
    assert len(mode_events) == 1
    assert mode_events[0]["data"]["signal"] == "exit_brainstorm"
    assert mode_events[0]["data"]["reason"]    # why the judge released, for the consumer's log


def test_release_judge_prompt_carries_the_subject_matter_distinction():
    provider = ReleaseJudgeProvider(decisions=[{"action": "answer", "rationale": "held"}],
                                    release=False)
    _orch(provider, StubRetrieval(), config=_brainstorm_cfg()).run("send her an email about it")
    prompt = provider.judge_prompts[0]
    assert "SUBJECT MATTER" in prompt
    assert "send her an email about it" in prompt   # the message under judgment
    assert "false" in prompt                       # the safe direction is spelled out


def test_latched_planner_prompt_tells_the_planner_it_does_not_own_the_exit():
    provider = ReleaseJudgeProvider(decisions=[{"action": "answer", "rationale": "held"}],
                                    release=False)
    _orch(provider, StubRetrieval(), config=_brainstorm_cfg()).run("create a goal called X")
    planner_prompt = provider.plan_prompts[0]      # judge calls never land here (routed by name)
    assert "SUBJECT MATTER is NOT a mode release" in planner_prompt
    assert "already judged" in planner_prompt


def test_judge_brainstorm_release_holds_on_empty_message():
    provider = ReleaseJudgeProvider(decisions=[], release=True)
    orch = _orch(provider, StubRetrieval(), config=_brainstorm_cfg())
    released, reason = orch.judge_brainstorm_release("   ")
    assert released is False
    assert provider.judge_calls == 0
    assert "holding" in reason


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
# Brainstorm no-action acknowledgment: when the latch suppresses a directive, the answer
# prompt carries guidance to say out loud that nothing ran (with permission to skip/soften);
# a plain question injects nothing; unlatched prompts never carry the steer.
# ---------------------------------------------------------------------------

_ACK_MARKER = "BRAINSTORM MODE: NOTHING WAS EXECUTED THIS TURN"
_HONESTY_MARKER = "Never say or imply that you have acted"
_HELD_MARKER = "held off only because of brainstorm mode"


def _answer_prompt(provider) -> str:
    """Everything the LAST answer call saw: its system prompt (where a per-turn reply directive
    rides) plus its messages."""
    system = str(provider.answer_systems[-1] or "") if provider.answer_systems else ""
    return system + "\n" + "\n".join(str(m["content"]) for m in provider.last_answer_messages)


def test_brainstorm_directive_message_injects_no_action_ack_steer():
    # "fix the login bug" trips the message-intent prefilter whose escalation the latch
    # suppresses: the answer prompt must carry the acknowledgment guidance instead.
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "talking it through"}])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("fix the login bug")
    assert res.kind == "answer"
    assert runner.calls == []
    prompt = _answer_prompt(provider)
    assert _ACK_MARKER in prompt
    assert BRAINSTORM_NO_ACTION_ACK_NOTE in prompt
    # The steer is guidance, never an absolute rule: permission to skip/soften rides along.
    assert "skip or soften" in prompt
    # Message-only directive (the planner never tried to act): no held-work variant.
    assert _HELD_MARKER not in prompt


def test_brainstorm_degraded_deep_injects_held_work_ack():
    # The planner chose "deep" and the latch degraded it: the steer must also say the work
    # was ready to start and will begin on a go-ahead.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do the thing", "deep_brief": "do it", "rationale": "r"},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("what if we...")
    assert res.kind == "answer"
    assert runner.calls == []
    prompt = _answer_prompt(provider)
    assert _ACK_MARKER in prompt
    assert BRAINSTORM_HELD_WORK_ACK_NOTE in prompt


def test_brainstorm_suppressed_deferred_deep_injects_held_work_ack():
    # The planner set deferred_deep despite the latch: the same acknowledgment guidance
    # applies (the work would have started right after the answer).
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "answering",
         "deferred_deep": {"goal": "Do it after answering"}},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("idea: what about X")
    assert res.kind == "answer"
    assert runner.calls == []
    prompt = _answer_prompt(provider)
    assert _ACK_MARKER in prompt
    assert _HELD_MARKER in prompt


def test_brainstorm_plain_question_carries_the_ack_with_permission_to_skip():
    """A musing is not forced into a robotic disclaimer: the note rides along (its honesty floor
    applies to every latched reply) but explicitly tells the model to skip the acknowledgment when
    the user was only thinking out loud. Held work is NOT claimed."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "weighing options"}])
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(execution_mode="brainstorm")).run(
        "what are the tradeoffs of approach A?")
    assert res.kind == "answer"
    prompt = _answer_prompt(provider)
    assert _ACK_MARKER in prompt
    assert _HONESTY_MARKER in prompt
    assert "skip or soften" in prompt
    assert _HELD_MARKER not in prompt
    for p in provider.plan_prompts:            # never leaks into the PLANNER prompt
        assert _ACK_MARKER not in p


def test_unlatched_prompts_never_carry_the_ack_steer():
    # Normal mode, same directive message, no deep capability (so the turn stays a single
    # answer call): neither the planner prompts nor the answer prompt may contain the steer.
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "plain"}])
    res = _orch(provider, StubRetrieval()).run("fix the login bug")
    assert res.kind == "answer"
    assert _ACK_MARKER not in _answer_prompt(provider)
    assert _HELD_MARKER not in _answer_prompt(provider)
    for p in provider.plan_prompts:
        assert _ACK_MARKER not in p


class _AckAwareProvider(StubProvider):
    """A stub whose answer() behaves on the steer: it acknowledges the no-action state only
    when the acknowledgment guidance is actually present in its prompt."""

    def answer(self, messages, *, model, system=None) -> str:
        self.answer_calls += 1
        self.last_answer_messages = messages
        self.all_answer_messages.append(list(messages))
        self.answer_systems.append(system)
        joined = str(system or "") + "\n" + "\n".join(str(m["content"]) for m in messages)
        if _ACK_MARKER in joined:
            return ("I have not acted on this because we are in brainstorm mode. "
                    "Say the word and I will go ahead.")
        return "Here is my thinking on that."


def test_brainstorm_directive_reply_acknowledges_no_action_behaviorally():
    provider = _AckAwareProvider(decisions=[{"action": "answer", "rationale": "talking"}])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("fix the login bug")
    assert res.kind == "answer"
    assert runner.calls == []
    assert "have not acted" in res.text
    assert "brainstorm" in res.text


def test_normal_mode_reply_carries_no_forced_acknowledgment():
    provider = _AckAwareProvider(decisions=[{"action": "answer", "rationale": "plain"}])
    res = _orch(provider, StubRetrieval()).run("what are the tradeoffs of approach A?")
    assert res.kind == "answer"
    assert "have not acted" not in res.text


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


# ---------------------------------------------------------------------------
# THE TWO INVARIANTS (a latched turn):
#   1. it ESCALATES NOTHING and EXECUTES NOTHING, via ANY action or escalation path,
#   2. EVERY reply-producing path carries the no-action acknowledgment guidance.
# Both were broken in the real product even though the stubs above passed: "clarify" (which the
# planner note used to invite) escalated a real decision-request through the escalation sink, and
# the two EARLY terminal paths (clarify, read-budget wrap-up) never reached the acknowledgment,
# which was folded in at the main answer's grounding only.
# ---------------------------------------------------------------------------

def test_brainstorm_clarify_raises_no_decision_request():
    """A latched turn may not park an ask: "clarify" degrades to an answer that asks in the reply."""
    provider = StubProvider(decisions=[
        {"action": "clarify", "rationale": "ambiguous",
         "clarification": {"question": "Which project do you mean?",
                           "options": ["Quest", "Research"], "allow_free_input": True}},
    ])
    escalation = StubEscalation()
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner, escalation=escalation,
                config=OrchestratorConfig(execution_mode="brainstorm")).run("Set that up.")
    assert res.kind == "answer"          # not "confirm"
    assert res.decision_id is None
    assert escalation.raised == []       # ZERO decision-requests from a latched turn
    assert runner.calls == []            # and nothing executed
    prompt = _answer_prompt(provider)
    assert _ACK_MARKER in prompt         # the reply still says nothing ran
    assert "Which project do you mean?" in prompt   # and the question rides into the reply
    assert "Quest" in prompt and "Research" in prompt


def test_brainstorm_planner_note_no_longer_invites_clarify():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider, StubRetrieval(),
          config=OrchestratorConfig(execution_mode="brainstorm")).run("mulling something over")
    note = provider.plan_prompts[0]
    assert '"deep", "confirm"' in note and '"clarify" are ALL UNAVAILABLE' in note
    assert 'use "read" or "answer" only' in note


def test_brainstorm_read_budget_wrapup_carries_the_ack():
    """The budget/cap wrap-up is a reply path that returns EARLY (it never reaches the main answer
    assembly), so it has to carry the acknowledgment itself."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "grounding"},
    ])
    runner = StubDeepRunner(met=True)
    escalation = StubEscalation()
    res = _orch(provider, StubRetrieval({"README.md": "GROUNDING notes"}), deep_runner=runner,
                escalation=escalation,
                config=OrchestratorConfig(execution_mode="brainstorm", max_steps=1)).run(
        "fix the login bug")
    assert res.kind == "answer"
    assert res.exit_reason == "read_budget"
    assert runner.calls == []
    assert escalation.raised == []
    prompt = _answer_prompt(provider)
    assert _ACK_MARKER in prompt         # the early-return reply path carries the note too
    assert _HONESTY_MARKER in prompt


def test_brainstorm_read_budget_wrapup_in_normal_mode_unchanged():
    """The same cap in NORMAL mode still wraps up with the best-effort answer and no brainstorm
    steer of any kind (the wrap-up change is brainstorm-only)."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "grounding"},
        {"met": True, "reason": "done"},
    ])
    runner = StubDeepRunner(met=True, output="did it")
    res = _orch(provider, StubRetrieval({"README.md": "GROUNDING notes"}), deep_runner=runner,
                config=OrchestratorConfig(max_steps=1)).run("fix the login bug")
    assert res.kind == "answer"          # gathered something -> best-effort answer, as before
    assert res.exit_reason == "read_budget"
    assert _ACK_MARKER not in _answer_prompt(provider)
    assert _HONESTY_MARKER not in _answer_prompt(provider)


def test_brainstorm_regenerated_answers_keep_the_ack():
    """The goal-verification loop and the overseer redirect REGENERATE the reply. Every one of
    those regenerations grounds through the same builder, so none of them can ship a reply that
    was never told the turn was held."""
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "talking it through"},
        {"met": False, "reason": "thin", "next_action": "go deeper"},   # forces one regeneration
        {"met": True, "reason": "good"},
    ])
    res = _orch(provider, StubRetrieval(), deep_runner=StubDeepRunner(met=True),
                config=OrchestratorConfig(execution_mode="brainstorm",
                                          answer_goal_max_iterations=2)).run("fix the login bug")
    assert res.kind == "answer"
    # Only the REPLY calls (the ones carrying the grounding block); the turn also makes cheap
    # non-reply calls (goal-condition derivation, verification).
    replies = [str(sysp or "") + "\n" + "\n".join(str(m["content"]) for m in msgs)
               for sysp, msgs in zip(provider.answer_systems, provider.all_answer_messages)
               if any("GROUNDING CONTEXT" in str(m["content"]) for m in msgs)]
    assert len(replies) >= 2                     # the first answer plus at least one regeneration
    assert all(_ACK_MARKER in p for p in replies)


def test_brainstorm_understanding_clarify_does_not_escalate():
    """Stage 1 (input understanding) can also want to stop and ask. While latched it must not:
    the question rides into the reply and the loop runs on."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "best reading"}])
    escalation = StubEscalation()
    runner = StubDeepRunner(met=True)
    orch = _orch(provider, StubRetrieval(), deep_runner=runner, escalation=escalation,
                 config=OrchestratorConfig(execution_mode="brainstorm"))
    orch._understand_input = lambda *a, **kw: ("Do the thing we discussed", "",
                                               "Which thing did you mean?")
    orch._needs_context_to_understand = lambda _m: True
    orch.conversation_store = object()   # only presence is checked before stage 1 runs
    res = orch.run("Do the thing we discussed", conv_id="c1")
    assert res.kind == "answer"
    assert res.decision_id is None
    assert escalation.raised == []
    assert runner.calls == []
    prompt = _answer_prompt(provider)
    assert _ACK_MARKER in prompt
    assert "Which thing did you mean?" in prompt


def test_brainstorm_understanding_clarify_still_escalates_in_normal_mode():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r"}])
    escalation = StubEscalation()
    orch = _orch(provider, StubRetrieval(), escalation=escalation)
    orch._understand_input = lambda *a, **kw: ("do it", "", "Which thing did you mean?")
    orch._needs_context_to_understand = lambda _m: True
    orch.conversation_store = object()
    res = orch.run("Do the thing we discussed", conv_id="c1")
    assert res.kind == "confirm"
    assert escalation.raised          # unchanged: a normal turn still asks the human


# ---------------------------------------------------------------------------
# CAPABILITY INTROSPECTION: a consumer's compat probe must be able to tell whether the library it
# loaded actually has the judged latch (a stale build must never look like it holds). These are the
# stable public names it keys on; do not rename them without updating the consumers.
# ---------------------------------------------------------------------------

def test_brainstorm_latch_is_introspectable_from_the_public_core_namespace():
    import quest_ai_runner.core as core

    assert hasattr(core.Orchestrator, "judge_brainstorm_release")
    fields = core.OrchestratorConfig.__dataclass_fields__
    for name in ("execution_mode", "mode_signals_enabled", "mode_release_tier"):
        assert name in fields, name
    assert "mode_signal" in core.OrchestratorResult.__dataclass_fields__
    assert core.MODE_RELEASE_TOOL["name"] == "brainstorm_release_verdict"
    assert core.EVENT_MODE_SIGNAL == "mode_signal"
    for name in ("MODE_RELEASE_TOOL", "MODE_RELEASE_PROMPT", "BRAINSTORM_NO_ACTION_ACK_NOTE",
                 "EVENT_MODE_SIGNAL"):
        assert name in core.__all__, name
