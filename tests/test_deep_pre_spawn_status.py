"""What a deep run says and costs BEFORE its worker spawns.

Reported from a terminal chat on a very large card store: the turn-start context assembly timed
out ("Context assembly timed out after 30.0s with no partial result"), the deep run was handed off,
and then nothing was shown for minutes. Cause: the deep run assembled the same request's context a
SECOND time, synchronously, with no bound and no status line, while the first (timed-out) assembly
was still running on the same store. Pinned here:

* every pre-spawn wait is announced first, in order, and only when it really happens;
* the per-goal assembly is bounded by the turn-start budget (a soft ``assembly_deadline`` in
  ``meta``, a WARNING on timeout) and the run proceeds without that context instead of hanging;
* a single-goal run reuses the turn-start assembly (finished: handed over; still running: waited
  for, never duplicated), a fan-out's subgoals still assemble for themselves;
* the reuse never crosses scope tags, and a declined escalation never pays for an assembly;
* a status never names a model for a runner that does not run the ladder's model.

Fully offline: scripted provider, stub assemblers and runners.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, List, Optional, Tuple

from quest_ai_runner.core.adapters import (
    AssembledContext,
    DeepResult,
    EVENT_STATUS,
    Mode,
    PlanDecision,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.orchestrator_deep_context import CONTEXT_DROPPED_STATUS
from quest_ai_runner.core.orchestrator_turn import TurnCardCache, _Emitter

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider

GATHERING = "Gathering what I need to start."
# Words a person should never meet in a status line of the default view (they are for the log and the details).
INTERNAL_TERMS = ("deep run", "deep worker", "deep task", "QAR", "card", "corpus", "[Pass")
# What the turn-start step derives from the user's message (the query the turn-start assembly runs
# for), fixed here so a test can name it.
CONDITION = "Parser fixed"


class FixedConditionProvider(ScriptedProvider):
    """The scripted provider, whose cheap-tier call answers with one fixed done-standard."""

    def answer(self, messages, *, model, system=None) -> str:
        return CONDITION


class Timeline:
    """One ordered record of everything the person was shown and every worker start."""

    def __init__(self) -> None:
        self.entries: List[Tuple[str, str]] = []
        self.lock = threading.Lock()

    def update(self, event, mode) -> None:
        with self.lock:
            self.entries.append((event.type, event.text or ""))

    def worker_started(self, goal: str) -> None:
        with self.lock:
            self.entries.append(("worker", goal))

    def statuses(self) -> List[str]:
        return [text for kind, text in self.entries if kind == EVENT_STATUS]

    def index_of(self, kind: str, text: str) -> int:
        for i, entry in enumerate(self.entries):
            if entry[0] == kind and entry[1].startswith(text):
                return i
        raise AssertionError(f"{kind} {text!r} not in {self.entries}")

    def status_index(self, text: str) -> int:
        return self.index_of(EVENT_STATUS, text)

    def worker_index(self, goal: str) -> int:
        return self.index_of("worker", goal)


class OrderedRunner:
    """A deep runner that notes WHEN it started on the shared timeline and what it was given."""

    def __init__(self, timeline: Timeline, *, uses_model: bool = True,
                 starts_background: bool = False) -> None:
        self.timeline = timeline
        self.uses_deep_model = uses_model
        self.starts_background_work = starts_background
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        self.timeline.worker_started(goal)
        self.calls.append({"goal": goal, "model": model, "context_preamble": context_preamble or ""})
        return DeepResult(met=True, output="done")


class CountingAssembler:
    """Returns a view built from the query, counts calls, can be slow or held until released."""

    def __init__(self, *, delay: float = 0.0, gate: Optional[threading.Event] = None) -> None:
        self.delay = delay
        self.gate = gate
        self.calls: List[str] = []
        self.metas: List[Dict[str, Any]] = []

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        self.calls.append(task_text)
        self.metas.append(dict(meta or {}))
        if self.gate is not None:
            self.gate.wait(timeout=30)
        elif self.delay:
            time.sleep(self.delay)
        return AssembledContext(context_view=f"CTX[{task_text}]")

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None


def build(runner: Any, assembler: Any, *, plans: Optional[List[Dict[str, Any]]] = None,
          **kw: Any) -> Orchestrator:
    provider = FixedConditionProvider(plans=plans or [], verdicts=[{"met": True}] * 4)
    return Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        deep_runner=runner, context_assembler=assembler,
        config=OrchestratorConfig(deep_model_ladder=["haiku", "sonnet", "opus"]), **kw)


def deep_plan(*goals: str, difficulty: str = "normal") -> Dict[str, Any]:
    return {"action": "deep", "goal": "Do the work", "rationale": "deep",
            "deep_difficulty": difficulty, "deep_difficulty_reason": "because",
            "deep_subtasks": [{"goal": g, "brief": f"do {g}"} for g in goals]}


def run_deep_directly(orch: Orchestrator, timeline: Timeline, *goals: str,
                      card_context: Any = None, ctx_meta: Optional[Dict[str, Any]] = None,
                      **kw: Any):
    """Call ``_run_deep`` itself, so there is no turn-start assembly to reuse."""
    plan = PlanDecision(action="deep", goal="Do the work", rationale="deep",
                        deep_difficulty="normal",
                        deep_subtasks=[{"goal": g, "brief": f"do {g}"} for g in goals])
    emit = _Emitter(timeline, Mode.LIVE, lambda text: None)
    return orch._run_deep(plan, "please do the work", "claude-sonnet", emit=emit,
                          ctx_meta=ctx_meta if ctx_meta is not None else {},
                          card_context=card_context, **kw)


# --- every pre-spawn wait is announced first, in order ------------------------------------------

def test_the_statuses_come_before_the_worker_and_in_order():
    timeline = Timeline()
    runner = OrderedRunner(timeline)
    orch = build(runner, CountingAssembler())
    run_deep_directly(orch, timeline, "Fix the parser bug")

    gathering = timeline.status_index(GATHERING)
    working = timeline.status_index("Working on: Fix the parser bug")
    starting = timeline.status_index("Starting on sonnet")
    worker = timeline.worker_index("Fix the parser bug")
    assert gathering < working < starting < worker


def test_a_pinned_model_still_says_the_worker_is_starting_and_on_what():
    timeline = Timeline()
    orch = build(OrderedRunner(timeline), CountingAssembler())
    run_deep_directly(orch, timeline, "Fix the parser bug", model_hint="fable")

    starting = timeline.status_index("Starting the work on fable.")
    assert timeline.status_index(GATHERING) < starting < timeline.worker_index("Fix the parser bug")
    assert not [t for t in timeline.statuses() if t.startswith("Starting on")]


def test_a_runner_that_does_not_run_the_model_never_names_a_model_or_a_deep_run():
    timeline = Timeline()
    runner = OrderedRunner(timeline, uses_model=False)
    orch = build(None, CountingAssembler(), deep_runners={"code": runner},
                 deep_runner_classifier=lambda user_message, goal, brief: "code")
    run_deep_directly(orch, timeline, "Fix the parser bug")

    statuses = timeline.statuses()
    decide = timeline.status_index("Deciding how to run this work")
    gathering = timeline.status_index(GATHERING)
    assert decide < gathering < timeline.worker_index("Fix the parser bug")
    for text in statuses:
        assert "Starting" not in text and not [t for t in INTERNAL_TERMS if t.lower() in text.lower()]
        assert "sonnet" not in text and "haiku" not in text and "opus" not in text


def test_no_status_of_a_run_uses_an_internal_word_whatever_runner_it_goes_to():
    """A person reads "the work", never "the deep run" or "the deep worker" (those words stay in the log and the details)."""
    seen = []
    for uses_model in (True, False):
        timeline = Timeline()
        runner = OrderedRunner(timeline, uses_model=uses_model)
        orch = build(runner if uses_model else None, CountingAssembler(),
                     **({} if uses_model else {"deep_runners": {"code": runner},
                                               "deep_runner_classifier": lambda user_message, goal, brief: "code"}))
        run_deep_directly(orch, timeline, "Fix the parser bug")
        seen += timeline.statuses()
    assert GATHERING in seen and "Starting on sonnet" in " ".join(seen)
    assert [t for t in seen if t.startswith("Starting")], "the model-running worker says it is starting"
    assert not [t for t in seen if any(word.lower() in t.lower() for word in INTERNAL_TERMS)], seen


def test_no_status_uses_an_em_dash():
    timeline = Timeline()
    orch = build(OrderedRunner(timeline), CountingAssembler())
    run_deep_directly(orch, timeline, "Fix the parser bug")
    assert timeline.statuses() and not [t for t in timeline.statuses() if "\u2014" in t]


def test_nothing_is_said_about_gathering_when_there_is_nothing_to_gather():
    timeline = Timeline()
    orch = build(OrderedRunner(timeline), None)
    run_deep_directly(orch, timeline, "Fix the parser bug")
    assert not [t for t in timeline.statuses() if t.startswith("Gathering")]


# --- the per-goal assembly is bounded ------------------------------------------------------------

def test_a_hung_per_goal_assembly_is_bounded_and_the_run_proceeds(monkeypatch, caplog):
    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "0.2")
    gate = threading.Event()
    assembler = CountingAssembler(gate=gate)
    timeline = Timeline()
    runner = OrderedRunner(timeline)
    orch = build(runner, assembler)
    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger="quest-ai-runner.orchestrator"):
            result = run_deep_directly(orch, timeline, "Fix the parser bug")
        elapsed = time.monotonic() - started
    finally:
        gate.set()

    assert elapsed < 5.0, "the run must not wait on a hung assembly"
    assert result.kind == "deep" and result.deep_results[0].met
    assert runner.calls and "CTX[" not in runner.calls[0]["context_preamble"]
    assert any("timed out" in r.message.lower() and "proceeds without" in r.message.lower()
               for r in caplog.records), "the timeout must be a loud WARNING"
    assert timeline.status_index(GATHERING) < timeline.status_index(CONTEXT_DROPPED_STATUS) \
        < timeline.worker_index("Fix the parser bug")


def test_the_per_goal_assembly_carries_the_soft_deadline_and_leaves_the_turn_meta_alone(monkeypatch):
    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "5")
    assembler = CountingAssembler()
    timeline = Timeline()
    orch = build(OrderedRunner(timeline), assembler)
    turn_meta: Dict[str, Any] = {"quest_id": "q1"}
    before = time.monotonic()
    run_deep_directly(orch, timeline, "Fix the parser bug", ctx_meta=turn_meta)

    deadline = assembler.metas[0].get("assembly_deadline")
    assert isinstance(deadline, float) and before + 1.0 < deadline <= time.monotonic() + 5.0
    assert "assembly_deadline" not in turn_meta, "a deadline left on the shared meta would bound later reads"


def test_a_failing_per_goal_assembly_never_raises():
    class Exploding(CountingAssembler):
        def assemble(self, task_text, *, meta=None):
            raise RuntimeError("store unavailable")

    timeline = Timeline()
    runner = OrderedRunner(timeline)
    result = run_deep_directly(build(runner, Exploding()), timeline, "Fix the parser bug")
    assert result.kind == "deep" and runner.calls


# --- a single goal reuses the turn-start assembly ------------------------------------------------

def test_a_single_goal_reuses_the_finished_turn_start_assembly():
    assembler = CountingAssembler()
    timeline = Timeline()
    runner = OrderedRunner(timeline)
    orch = build(runner, assembler, plans=[deep_plan("Fix the parser bug")])
    result = orch.run("fix the parser", sink=timeline)

    assert result.kind == "deep"
    assert assembler.calls == [CONDITION], "the request must be assembled once, not twice"
    assert f"CTX[{CONDITION}]" in runner.calls[0]["context_preamble"]
    assert not [t for t in timeline.statuses() if t.startswith("Gathering")], \
        "reusing a finished assembly is instant, so nothing is announced"


def test_fanned_out_subgoals_still_assemble_for_themselves():
    assembler = CountingAssembler()
    timeline = Timeline()
    runner = OrderedRunner(timeline)
    orch = build(runner, assembler, plans=[deep_plan("Fix the parser bug", "Add the export feature")])
    orch.run("do both things", sink=timeline)

    assert sorted(assembler.calls) == sorted(
        [CONDITION, "Fix the parser bug", "Add the export feature"])
    by_goal = {c["goal"]: c["context_preamble"] for c in runner.calls}
    assert "CTX[Fix the parser bug]" in by_goal["Fix the parser bug"]
    assert "CTX[Add the export feature]" not in by_goal["Fix the parser bug"]
    assert "CTX[Add the export feature]" in by_goal["Add the export feature"]
    for goal in by_goal:
        assert timeline.status_index(f"Gathering what I need for: {goal}") < timeline.worker_index(goal)


def test_a_sequential_group_is_not_a_single_goal():
    """The planner never produces a sequential group, but a caller may hand one in: two tasks in one
    group are two goals, so neither may borrow the turn-start assembly."""
    assembler = CountingAssembler()
    timeline = Timeline()
    runner = OrderedRunner(timeline)
    orch = build(runner, assembler)
    cache = TurnCardCache(assembler, {})
    cache.note_turn_start_result(AssembledContext(context_view="TURN_START_VIEW"))
    plan = PlanDecision(action="deep", goal="Do the work", rationale="deep", deep_subtasks=[
        [{"goal": "Step one", "brief": "one"}, {"goal": "Step two", "brief": "two"}]])
    emit = _Emitter(timeline, Mode.LIVE, lambda text: None)
    orch._run_deep(plan, "please do both steps", "claude-sonnet", emit=emit, ctx_meta={},
                   card_context=cache)

    assert sorted(assembler.calls) == ["Step one", "Step two"]
    assert all("TURN_START_VIEW" not in c["context_preamble"] for c in runner.calls)


def test_a_turn_start_assembly_that_lands_late_is_joined_not_repeated(monkeypatch):
    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "0.3")
    assembler = CountingAssembler(delay=0.5)
    timeline = Timeline()
    runner = OrderedRunner(timeline)
    orch = build(runner, assembler, plans=[deep_plan("Fix the parser bug")])
    result = orch.run("fix the parser", sink=timeline)

    assert result.kind == "deep"
    assert assembler.calls == [CONDITION], "joining the in-flight assembly, not starting a second"
    assert f"CTX[{CONDITION}]" in runner.calls[0]["context_preamble"]
    assert timeline.status_index(GATHERING) < timeline.worker_index("Fix the parser bug")


def test_a_turn_start_assembly_that_never_lands_is_waited_on_once_then_dropped(monkeypatch, caplog):
    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "0.2")
    gate = threading.Event()
    assembler = CountingAssembler(gate=gate)
    timeline = Timeline()
    runner = OrderedRunner(timeline)
    orch = build(runner, assembler, plans=[deep_plan("Fix the parser bug")])
    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger="quest-ai-runner.orchestrator"):
            result = orch.run("fix the parser", sink=timeline)
        elapsed = time.monotonic() - started
    finally:
        gate.set()

    assert result.kind == "deep" and runner.calls
    assert elapsed < 5.0
    assert assembler.calls == [CONDITION], "no second assembly competes with the stuck first one"
    assert "CTX[" not in runner.calls[0]["context_preamble"]
    messages = [r.message.lower() for r in caplog.records]
    assert any("turn-start" in m and "proceeds without" in m for m in messages)
    assert timeline.status_index(GATHERING) < timeline.status_index(CONTEXT_DROPPED_STATUS) \
        < timeline.worker_index("Fix the parser bug")


# --- the reuse never crosses a scope fence, and a declined run pays for nothing ------------------

def test_a_result_assembled_for_one_quest_never_serves_another():
    assembler = CountingAssembler()
    orch = build(OrderedRunner(Timeline()), assembler)
    cache = TurnCardCache(assembler, {"scope_tags": ["quest:a"]})
    cache.note_turn_start_result(AssembledContext(context_view="QUEST_A_ONLY"))

    other = orch.bounded_goal_assembly(
        "goal", {"scope_tags": ["quest:b"]}, turn_context=cache,
        ctx_meta={"scope_tags": ["quest:b"]})
    assert other is not None and other.context_view == "CTX[goal]", "must assemble fresh for quest b"
    assert assembler.calls == ["goal"]

    same = orch.bounded_goal_assembly(
        "goal", {"scope_tags": ["quest:a"]}, turn_context=cache,
        ctx_meta={"scope_tags": ["quest:a"]})
    assert same is not None and same.context_view == "QUEST_A_ONLY"
    assert assembler.calls == ["goal"], "the same scope reuses it"


def test_a_declined_background_escalation_pays_for_no_assembly():
    assembler = CountingAssembler()
    timeline = Timeline()
    runner = OrderedRunner(timeline, starts_background=True)
    orch = build(runner, assembler)
    result = run_deep_directly(orch, timeline, "Fix the parser bug",
                               self_initiated=True, user_ordered=False)

    assert result.deep_results[0].declined_background
    assert assembler.calls == []
    assert not runner.calls
    assert not [t for t in timeline.statuses() if t.startswith(("Gathering", "Starting"))]


# --- TurnCardCache.turn_start_context ------------------------------------------------------------

def test_turn_start_context_has_nothing_to_offer_before_a_turn_start_assembly():
    assert TurnCardCache(CountingAssembler(), {}).turn_start_context(0.05) == (None, "")


def test_turn_start_context_hands_back_what_the_collect_used_even_a_partial():
    cache = TurnCardCache(CountingAssembler(), {})
    partial = AssembledContext(context_view="PART", partial=True)
    cache.note_turn_start_result(partial)
    assert cache.turn_start_context(0.05) == (partial, "reused")


def test_turn_start_context_waits_for_a_running_fetch_and_says_so():
    cache = TurnCardCache(CountingAssembler(), {})
    future: Future = Future()
    cache.register_prefetch("q", future)
    waited: List[bool] = []
    threading.Timer(0.1, lambda: future.set_result(AssembledContext(context_view="LATE"))).start()

    assembled, origin = cache.turn_start_context(2.0, on_wait=lambda: waited.append(True))

    assert origin == "joined" and assembled.context_view == "LATE" and waited == [True]
    assert cache.results["q"] is assembled, "a full late result is cached for later reads too"
    assert cache.turn_start_context(0.05)[1] == "reused"


def test_turn_start_context_does_not_announce_a_wait_for_a_fetch_that_already_finished():
    cache = TurnCardCache(CountingAssembler(), {})
    future: Future = Future()
    future.set_result(AssembledContext(context_view="DONE"))
    cache.register_prefetch("q", future)
    waited: List[bool] = []

    assert cache.turn_start_context(0.05, on_wait=lambda: waited.append(True))[1] == "joined"
    assert waited == []


def test_turn_start_context_reports_a_fetch_still_running_after_the_budget():
    cache = TurnCardCache(CountingAssembler(), {})
    cache.register_prefetch("q", Future())
    assert cache.turn_start_context(0.05) == (None, "pending")


def test_turn_start_context_leaves_a_failed_fetch_to_the_caller():
    cache = TurnCardCache(CountingAssembler(), {})
    future: Future = Future()
    future.set_exception(RuntimeError("store unavailable"))
    cache.register_prefetch("q", future)
    assert cache.turn_start_context(0.05) == (None, "")
