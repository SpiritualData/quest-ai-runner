"""Staged plans: the planner's ``deep_subtasks`` as a list of lists (core/orchestrator_staged_plan.py).

Pinned here, fully offline (scripted provider, recording runners):

* a flat list is normalized exactly as before, with the feature on or off, and a nested list is
  accepted only when ``OrchestratorConfig.staged_plans`` is on;
* the planner schema offers nesting only when the feature is on, and is otherwise the same object;
* the caps hold: top level, steps per sequence, tasks in all;
* a nested plan runs in process in order (a step of several tasks together), as the runner always could;
* a deep runner that offers ``run_staged_plan`` receives the plan as ordered stages and nothing runs
  in process; one that hands it back (None) gets the in-process run; a flat plan or a plan with the
  feature off is never offered;
* ``run_when`` survives normalization and defaults to ``always``.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from hubofhubs.ports.deep import DeepResult
from hubofhubs.ports.events import Mode
from quest_ai_runner.core.adapters_values import PlanDecision
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.orchestrator_decide_tool import (
    DECIDE_TOOL, DEFERRED_RUNNER_KEY, decide_tool_for,
)
from quest_ai_runner.core.orchestrator_decisions import normalize_decision
from quest_ai_runner.core.orchestrator_turn import _Emitter
from quest_ai_runner.core.orchestrator_staged_plan import (
    count_tasks, is_staged, normalize_staged_subtasks, plan_stages,
)

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import RecordingRunner, ScriptedProvider


def task(name: str, run_when: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"goal": name, "brief": f"do {name}"}
    if run_when:
        out["run_when"] = run_when
    return out


def cfg(**kw) -> OrchestratorConfig:
    return OrchestratorConfig(staged_plans=True, **kw)


# --- normalization ---------------------------------------------------------------------------------

def test_a_flat_list_is_unchanged_with_the_feature_on_or_off():
    raw = {"action": "deep", "deep_subtasks": [task("A"), task("B"), {"goal": "", "brief": ""}, "junk"]}
    off = normalize_decision(raw, OrchestratorConfig()).deep_subtasks
    assert off == [{"goal": "A", "brief": "do A"}, {"goal": "B", "brief": "do B"}]
    on = normalize_decision(raw, cfg()).deep_subtasks
    assert [(t["goal"], t["brief"], t["run_when"]) for t in on] == [("A", "do A", "always"), ("B", "do B", "always")]


def test_a_flat_list_keeps_its_cap_of_four():
    raw = {"action": "deep", "deep_subtasks": [task(f"T{i}") for i in range(7)]}
    assert len(normalize_decision(raw, OrchestratorConfig()).deep_subtasks) == 4
    assert len(normalize_decision(raw, cfg()).deep_subtasks) == 4


def test_a_nested_list_is_accepted_only_with_the_feature_on():
    raw = {"action": "deep", "deep_subtasks": [[task("A"), task("B", "prev_not_met")]]}
    assert normalize_decision(raw, OrchestratorConfig()).deep_subtasks == []
    nested = normalize_decision(raw, cfg()).deep_subtasks
    assert len(nested) == 1 and [t["goal"] for t in nested[0]] == ["A", "B"]
    assert nested[0][1]["run_when"] == "prev_not_met"


def test_an_unknown_run_when_is_always_and_steps_of_tasks_are_kept():
    got = normalize_staged_subtasks([[task("A"), [task("B1", "sometimes"), task("B2")], task("C")]],
                                    max_items=4, max_stages=4, max_tasks=8)
    assert [[t["run_when"] for t in step] if isinstance(step, list) else step["run_when"] for step in got[0]] == [
        "always", ["always", "always"], "always"]


def test_the_caps_on_steps_and_on_tasks_hold():
    long_chain = [[task(f"S{i}") for i in range(9)]]
    steps = normalize_staged_subtasks(long_chain, max_items=4, max_stages=4, max_tasks=8)
    assert len(steps[0]) == 4
    many = [[task(f"a{i}") for i in range(4)], [task(f"b{i}") for i in range(4)], task("c"), task("d")]
    capped = normalize_staged_subtasks(many, max_items=4, max_stages=4, max_tasks=8)
    assert count_tasks(capped) == 8


def test_a_single_step_sequence_is_just_the_task():
    got = normalize_staged_subtasks([[task("A")]], max_items=4, max_stages=4, max_tasks=8)
    assert got == [{"goal": "A", "brief": "do A", "run_when": "always"}]


# --- stages ------------------------------------------------------------------------------------------

def test_plan_stages_orders_the_steps_and_puts_flat_tasks_in_stage_zero():
    subtasks = normalize_staged_subtasks(
        [task("X"), [task("A"), [task("B1"), task("B2", "prev_met")], task("C", "prev_not_met")]],
        max_items=4, max_stages=4, max_tasks=8)
    stages = plan_stages(subtasks)
    assert [[t["goal"] for t in stage] for stage in stages] == [["X", "A"], ["B1", "B2"], ["C"]]
    assert is_staged(subtasks)
    assert not is_staged([task("A"), task("B")])
    assert is_staged([task("A", "prev_met")])


# --- the schema ---------------------------------------------------------------------------------------

def test_the_planner_schema_is_unchanged_without_the_feature_and_nests_with_it():
    assert decide_tool_for(False, False) is DECIDE_TOOL
    items = DECIDE_TOOL["input_schema"]["properties"]["deep_subtasks"]["items"]
    assert items["type"] == "object"
    staged = decide_tool_for(False, False, staged_plans=True)["input_schema"]["properties"]["deep_subtasks"]
    variants = staged["items"]["anyOf"]
    assert any(v["type"] == "object" for v in variants) and any(v["type"] == "array" for v in variants)
    assert "run_when" in variants[0]["properties"]
    # The shared schema object was not touched.
    assert DECIDE_TOOL["input_schema"]["properties"]["deep_subtasks"]["items"]["type"] == "object"


# --- running a plan -------------------------------------------------------------------------------------

class OrderRunner:
    """Records the order goals START and FINISH in, thread safely."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: List[str] = []

    def run_goal(self, *, goal: str, brief: str, model=None, max_turns=None, context_preamble=None) -> DeepResult:
        with self.lock:
            self.events.append(f"start:{goal}")
        with self.lock:
            self.events.append(f"end:{goal}")
        return DeepResult(met=True, output=f"{goal} done")


class OfferingRunner:
    """A queue-style runner that takes a staged plan itself."""

    starts_background_work = True

    def __init__(self, answer: Optional[DeepResult]) -> None:
        self.answer = answer
        self.offers: List[Dict[str, Any]] = []

    def run_staged_plan(self, *, goal: str, stages: List[List[Dict[str, Any]]]) -> Optional[DeepResult]:
        self.offers.append({"goal": goal, "stages": stages})
        return self.answer


def orchestrator(inline: Any, *, staged: bool = True, queue: Any = None) -> Orchestrator:
    provider = ScriptedProvider([], [{"met": True}] * 12)
    return Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        deep_runner=inline, deep_runners=({DEFERRED_RUNNER_KEY: queue} if queue is not None else None),
        config=OrchestratorConfig(staged_plans=staged))


class Sink:
    def update(self, event, mode) -> None:
        return None


def run_plan(orch: Orchestrator, subtasks: List[Any], **kw):
    plan = PlanDecision(action="deep", goal="Do the work", rationale="deep", deep_subtasks=subtasks)
    return orch._run_deep(plan, "please do it", "claude-sonnet", emit=_Emitter(Sink(), Mode.LIVE, lambda t: None),
                          ctx_meta={}, **kw)


def test_a_nested_plan_runs_in_process_in_order_with_a_step_of_tasks_together():
    runner = OrderRunner()
    subtasks = normalize_staged_subtasks([[task("A"), [task("B1"), task("B2")], task("C")]],
                                         max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orchestrator(runner), subtasks)
    assert result.kind == "deep" and len(result.deep_results) == 4
    events = runner.events
    assert events.index("end:A") < min(events.index("start:B1"), events.index("start:B2"))
    assert max(events.index("end:B1"), events.index("end:B2")) < events.index("start:C")


def test_a_runner_that_offers_staged_plans_gets_the_ordered_stages_and_nothing_runs_in_process():
    inline = RecordingRunner()
    queue = OfferingRunner(DeepResult(met=True, deferred=True, output="Queued a plan"))
    subtasks = normalize_staged_subtasks([[task("A"), task("B", "prev_met"), task("C", "prev_not_met")]],
                                         max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orchestrator(inline, queue=queue), subtasks, user_ordered=True)
    assert inline.calls == []
    assert len(queue.offers) == 1
    stages = queue.offers[0]["stages"]
    assert [[(t["goal"], t["run_when"]) for t in s] for s in stages] == [
        [("A", "always")], [("B", "prev_met")], [("C", "prev_not_met")]]
    assert result.kind == "deep" and result.deep_results[0].output == "Queued a plan"
    assert result.goals == ["A", "B", "C"]


def test_a_runner_that_hands_the_plan_back_gets_the_in_process_run():
    inline = RecordingRunner()
    queue = OfferingRunner(None)
    subtasks = normalize_staged_subtasks([[task("A"), task("B")]], max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orchestrator(inline, queue=queue), subtasks)
    assert len(queue.offers) == 1
    assert [c["goal"] for c in inline.calls] == ["A", "B"]
    assert result.kind == "deep"


def test_a_flat_plan_or_the_feature_off_never_offers_the_plan():
    inline = RecordingRunner()
    queue = OfferingRunner(DeepResult(met=True, output="x"))
    run_plan(orchestrator(inline, queue=queue), [task("A"), task("B")])
    assert queue.offers == []
    inline2 = RecordingRunner()
    queue2 = OfferingRunner(DeepResult(met=True, output="x"))
    run_plan(orchestrator(inline2, staged=False, queue=queue2), [[task("A"), task("B")]])
    assert queue2.offers == [] and [c["goal"] for c in inline2.calls] == ["A", "B"]
